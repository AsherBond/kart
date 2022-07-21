import copy
import json
import os
import shutil
from pathlib import Path

import click
import pygit2

from kart.completion_shared import conflict_completer

from .cli_util import MutexOption
from .exceptions import NO_CONFLICT, InvalidOperation, NotFound, NotYetImplemented
from .geometry import geojson_to_gpkg_geom
from .merge_util import MergeContext, MergeIndex, RichConflict
from .repo import KartRepoState


def ungeojson_feature(feature, dataset):
    """Given a geojson feature belonging to dataset, returns the feature as a dict containing a gpkg geometry."""
    result = copy.deepcopy(feature["properties"])
    if dataset.geom_column_name:
        result[dataset.geom_column_name] = geojson_to_gpkg_geom(feature["geometry"])
    return result


def ungeojson_file(file_path, dataset):
    """
    Given a file containing multiple geojson features belonging to dataset,
    returns the features as dicts containing gpkg geometries.
    """
    features = json.load(file_path.open())["features"]
    return [ungeojson_feature(f, dataset) for f in features]


def write_feature_to_dataset_entry(feature, dataset, repo):
    """
    Adds the given feature to the given dataset by writing a blob to the Kart repo.
    Returns the IndexEntry that refers to that blob - this IndexEntry still needs
    to be written to the repo to complete the write.
    """
    feature_path, feature_data = dataset.encode_feature(feature)
    blob_id = repo.create_blob(feature_data)
    return pygit2.IndexEntry(feature_path, blob_id, pygit2.GIT_FILEMODE_BLOB)


def load_dataset(rich_conflict):
    # TODO - this works perfectly as long as the dataset hasn't changed structure over the course of the confict.
    # The correct behaviour is to load a dataset based not on a commit, but on the current merge index, and use this for
    # serialising resolved features etc - and, to enforce that meta changes for a dataset are resolved before feature changes.
    return rich_conflict.any_true_version.dataset


def load_file_resolve(rich_conflict, file_path):
    """Loads a feature from the given file in order to use it as a conflict resolution."""
    single_path = not rich_conflict.has_multiple_paths
    dataset_part = rich_conflict.decoded_path[1]
    if not single_path or dataset_part not in ("feature", "tile"):
        raise NotYetImplemented(
            "Sorry, only feature or tile conflicts can currently be resolved using --with-file"
        )

    dataset_part = rich_conflict.decoded_path[1]
    if dataset_part == "feature":
        return _load_file_resolve_for_feature(rich_conflict, file_path)
    elif dataset_part == "tile":
        return _load_file_resolve_for_tile(rich_conflict, file_path)
    else:
        raise RuntimeError()


def _load_file_resolve_for_feature(rich_conflict, file_path):
    dataset = load_dataset(rich_conflict)
    return [
        write_feature_to_dataset_entry(f, dataset, dataset.repo)
        for f in ungeojson_file(file_path, dataset)
    ]


def _load_file_resolve_for_tile(rich_conflict, file_path):
    from kart.lfs_util import get_local_path_from_lfs_hash, dict_to_pointer_file_bytes
    from kart.point_cloud.metadata_util import format_tile_for_pointer_file

    tilename = rich_conflict.decoded_path[2]
    dataset = load_dataset(rich_conflict)
    repo = dataset.repo
    rel_tile_path = os.path.relpath(file_path.resolve(), repo.workdir_path.resolve())
    tile_summary = dataset.get_tile_summary_from_filesystem_path(file_path)
    if not dataset.is_tile_compatible(dataset.tile_metadata["format"], tile_summary):
        # TODO: maybe support type-conversion during resolves like we do during commits.
        raise InvalidOperation(
            f"The tile at {rel_tile_path} does not match the dataset's format"
        )

    path_in_lfs_cache = get_local_path_from_lfs_hash(repo, tile_summary["oid"])
    path_in_lfs_cache.parents[0].mkdir(parents=True, exist_ok=True)
    shutil.copy(file_path, path_in_lfs_cache)
    pointer_dict = format_tile_for_pointer_file(tile_summary)
    pointer_data = dict_to_pointer_file_bytes(pointer_dict)
    blob_path = dataset.tilename_to_blob_path(tilename)
    blob_id = repo.create_blob(pointer_data)
    return [pygit2.IndexEntry(blob_path, blob_id, pygit2.GIT_FILEMODE_BLOB)]


def load_workingcopy_resolve(rich_conflict):
    """Loads a feature from the working copy in order to use it as a conflict resolution."""
    single_path = not rich_conflict.has_multiple_paths
    dataset_part = rich_conflict.decoded_path[1]
    if not single_path or dataset_part not in ("feature", "tile"):
        raise NotYetImplemented(
            "Sorry, only feature or tile conflicts can currently be resolved using --with=workingcopy"
        )

    dataset_part = rich_conflict.decoded_path[1]
    if dataset_part == "feature":
        return _load_workingcopy_resolve_for_feature(rich_conflict)
    elif dataset_part == "tile":
        return _load_workingcopy_resolve_for_tile(rich_conflict)
    else:
        raise RuntimeError()


def _load_workingcopy_resolve_for_feature(rich_conflict):
    dataset = load_dataset(rich_conflict)
    repo = dataset.repo
    table_wc = repo.working_copy.tabular
    pk = rich_conflict.decoded_path[2]
    feature = (
        table_wc.get_feature(dataset, pk, allow_schema_diff=False) if table_wc else None
    )
    if feature is None:
        raise NotFound(
            f"No feature found at {rich_conflict.label} - to resolve a conflict by deleting the feature, use --with=delete"
        )
    feature_path, feature_data = dataset.encode_feature(feature)
    blob_id = repo.create_blob(feature_data)
    return [pygit2.IndexEntry(feature_path, blob_id, pygit2.GIT_FILEMODE_BLOB)]


def _load_workingcopy_resolve_for_tile(rich_conflict):
    from kart.point_cloud.tilename_util import get_tile_path_pattern

    dataset = load_dataset(rich_conflict)
    repo = dataset.repo
    workdir = repo.working_copy.workdir
    tilename = rich_conflict.decoded_path[2]
    matching_files = []
    if workdir:
        # Get a glob that roughly matches the tiles we are looking for.
        matching_files = list((workdir.path / dataset.path).glob(f"**/{tilename}.*"))
        # Narrow it down more exactly using get_tile_path_pattern which allows for a few different extensions.
        filename_pattern = get_tile_path_pattern(tilename)
        matching_files = [
            p for p in matching_files if filename_pattern.fullmatch(p.name)
        ]

    if not matching_files:
        raise NotFound(
            f"No tile found at {rich_conflict.label} - to resolve a conflict by deleting the tile, use --with=delete"
        )
    if len(matching_files) > 1:
        click.echo(
            "Found multiple files in the working copy that could be intended as the resolution:",
            err=True,
        )
        for file in matching_files:
            click.echo(
                os.path.relpath(file.resolve(), repo.workdir_path.resolve()), err=True
            )
        raise InvalidOperation("Couldn't resolve conflict using working copy")
    return _load_file_resolve_for_tile(rich_conflict, matching_files[0])


CHOICE_ALIASES = {"working-copy": "workingcopy"}
CONTEXT_SETTINGS = dict(token_normalize_func=lambda x: CHOICE_ALIASES.get(x, x))


@click.command(context_settings=CONTEXT_SETTINGS)
@click.pass_context
@click.option(
    "--with",
    "with_version",
    type=click.Choice(["ancestor", "ours", "theirs", "delete", "workingcopy"]),
    help=(
        "Resolve the conflict with any of the following - \n"
        ' - "ancestor", "ours", or "theirs" - the versions which already exist in these commits'
        ' - "workingcopy" - the version currently found inside the working copy'
        ' - "delete" - the conflict is resolved by simply removing it'
    ),
    cls=MutexOption,
    exclusive_with=["file_path"],
)
@click.option(
    "--with-file",
    "file_path",
    type=click.Path(exists=True, dir_okay=False),
    help="Resolve the conflict by accepting the version(s) supplied in the given file.",
    cls=MutexOption,
    exclusive_with=["with_version"],
)
@click.argument(
    "conflict_label", default=None, required=True, shell_complete=conflict_completer
)
def resolve(ctx, with_version, file_path, conflict_label):
    """Resolve a merge conflict. So far only supports resolving to any of the three existing versions."""

    repo = ctx.obj.get_repo(allowed_states=KartRepoState.MERGING)
    if not (with_version or file_path):
        raise click.UsageError("Choose a resolution using --with or --with-file")

    merge_index = MergeIndex.read_from_repo(repo)
    merge_context = MergeContext.read_from_repo(repo)

    if conflict_label.endswith(":"):
        # Due to the way conflict labels are often displayed with ":ancestor" etc on the end,
        # a user could easily have an extra ":" on the end by accident.
        conflict_label = conflict_label[:-1]

    for key, conflict3 in merge_index.conflicts.items():
        rich_conflict = RichConflict(conflict3, merge_context)
        if rich_conflict.label == conflict_label:
            if key in merge_index.resolves:
                raise InvalidOperation(
                    f"Conflict at {conflict_label} is already resolved"
                )

            if file_path:
                res = load_file_resolve(rich_conflict, Path(file_path))
            elif with_version == "workingcopy":
                res = load_workingcopy_resolve(rich_conflict)
            elif with_version == "delete":
                res = []
            else:
                assert with_version in ("ancestor", "ours", "theirs")
                res = [getattr(conflict3, with_version)]
                if res == [None]:
                    click.echo(
                        f'Version "{with_version}" does not exist - resolving conflict by deleting.'
                    )
                    res = []

            merge_index.add_resolve(key, res)
            merge_index.write_to_repo(repo)
            unresolved_conflicts = len(merge_index.unresolved_conflicts)
            click.echo(f"Resolved 1 conflict. {unresolved_conflicts} conflicts to go.")
            if unresolved_conflicts == 0:
                click.echo("Use `kart merge --continue` to complete the merge")
            ctx.exit(0)

    raise NotFound(f"No conflict found at {conflict_label}", exit_code=NO_CONFLICT)
