import logging
import time
import uuid
from contextlib import contextmanager
from enum import Enum, auto

import click
import pygit2

from kart.exceptions import NO_CHANGES, InvalidOperation, NotFound, SubprocessError
from kart import subprocess_util as subprocess
from kart.tabular.version import (
    SUPPORTED_VERSIONS,
    dataset_class_for_version,
    extra_blobs_for_version,
)
from kart.tabular.import_source import TableImportSource
from kart.tabular.pk_generation import PkGeneratingTableImportSource
from kart.timestamps import minutes_to_tz_offset

L = logging.getLogger("kart.fast_import")


class FastImportSettings:
    """
    Tuneable settings for a fast import which affect performance.
    If not set, reasonable defaults are used.
    """

    def __init__(self, *, max_pack_size=None, max_delta_depth=None):
        # Maximum size of pack files
        self.max_pack_size = max_pack_size or "2G"
        # Maximum depth of delta-compression chains
        self.max_delta_depth = max_delta_depth or 0

    def as_args(self):
        args = []
        if self.max_pack_size:
            args.append(f"--max-pack-size={self.max_pack_size}")
        if self.max_delta_depth:
            args.append(f"--depth={self.max_delta_depth}")
        return args


class ReplaceExisting(Enum):
    # Don't replace any existing datasets.
    # Imports will start from the existing HEAD state.
    DONT_REPLACE = auto()

    # Any datasets in the import will replace existing datasets with the same name.
    # Datasets not in the import will be untouched.
    GIVEN = auto()

    # All existing datasets will be replaced by the given datasets.
    ALL = auto()


class _CommitMissing(Exception):
    pass


def _safe_walk_repo(repo, from_commit):
    """
    Contextmanager. Walk the repo log, yielding each commit.
    If a commit isn't present, raises _CommitMissing.
    Avoids catching any other KeyErrors raised by pygit2 or the contextmanager body
    """
    do_raise = False
    try:
        for commit in repo.walk(from_commit.id):
            try:
                yield commit
            except KeyError:
                # we only want to catch from the `repo.walk` call,
                # not from the contextmanager body
                do_raise = True
                raise
    except KeyError:
        if do_raise:
            raise
        raise _CommitMissing


def should_compare_imported_features_against_old_features(
    repo,
    source,
    replacing_dataset,
    from_commit,
):
    """
    Returns True iff we should compare feature blobs to the previous feature blobs
    when importing.

    This prevents repo bloat after columns are added or removed from the dataset,
    by only creating new blobs when the old blob cannot be upgraded to the new
    schema.
    """
    if replacing_dataset is None:
        return False
    old_schema = replacing_dataset.schema
    if old_schema != source.schema:
        types = replacing_dataset.schema.diff_type_counts(source.schema)
        if types["pk_updates"]:
            # when the PK changes, we won't be able to match old features to new features.
            # so not much point trying.
            return False
        elif types["inserts"] or types["deletes"]:
            # however, after column adds/deletes, we want to check features against
            # old features, to avoid unnecessarily duplicating 'identical' features.
            return True

    # Walk the log until we encounter a relevant schema change
    try:
        for commit in _safe_walk_repo(repo, from_commit):
            datasets = repo.datasets(commit.oid)
            try:
                old_dataset = datasets[replacing_dataset.path]
            except KeyError:
                # no schema changes since this dataset was added.
                return False
            if old_dataset.schema != source.schema:
                # this revision had a schema change
                types = old_dataset.schema.diff_type_counts(source.schema)
                if types["pk_updates"]:
                    # if the schema change was a PK update, all features were rewritten in that
                    # revision, and since no schema changes have occurred since then, we don't
                    # have to check all features against old features.
                    return False
                elif types["inserts"] or types["deletes"]:
                    return True
    except _CommitMissing:
        # probably this was because we're in a shallow clone,
        # and the commit just isn't present.
        # Just run the feature blob comparison; worst case it's a bit slow.
        return True
    return False


@contextmanager
def git_fast_import(repo, *args):
    p = subprocess.Popen(
        ["git", "fast-import", "--done", *args],
        cwd=repo.path,
        stdin=subprocess.PIPE,
        bufsize=128 * 1024,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield p
        p.stdin.write(b"\ndone\n")
    except BrokenPipeError:
        # if git-fast-import dies early, we get an EPIPE here
        # we'll deal with it below
        pass
    else:
        p.stdin.close()
    p.wait()
    if p.returncode != 0:
        raise SubprocessError(
            f"git-fast-import error! {p.returncode}", exit_code=p.returncode
        )


def fast_import_clear_tree(*, proc, replace_ids, replacing_dataset, source):
    """
    Clears out the appropriate trees in each of the fast_import processes,
    before importing any actual data over the top.
    """
    if replacing_dataset is None:
        # nothing to do
        return
    dest_path = source.dest_path
    dest_inner_path = f"{dest_path}/{replacing_dataset.DATASET_DIRNAME}"
    if replace_ids is None:
        # Delete the existing dataset, before we re-import it.
        proc.stdin.write(f"D {source.dest_path}\n".encode("utf8"))
    else:
        # Delete and reimport any attachments at dest_path
        attachment_names = [
            obj.name for obj in replacing_dataset.tree if obj.type_str == "blob"
        ]
        for name in attachment_names:
            proc.stdin.write(f"D {dest_path}/{name}\n".encode("utf8"))
        # Delete and reimport <inner_path>/meta/
        proc.stdin.write(f"D {dest_inner_path}/meta\n".encode("utf8"))

    # We just deleted the legends, but we still need them to reimport
    # data efficiently. Copy them from the original dataset.
    for x in write_blobs_to_stream(
        proc.stdin, replacing_dataset.iter_legend_blob_data()
    ):
        pass


UNSPECIFIED = object()


def fast_import_tables(
    repo,
    sources,
    *,
    settings=None,
    verbosity=1,
    message=None,
    replace_existing=ReplaceExisting.DONT_REPLACE,
    from_commit=UNSPECIFIED,
    replace_ids=None,
    allow_empty=False,
    limit=None,
    # Advanced use - used by kart upgrade.
    header=None,
    extra_cmd_args=(),
):
    """
    Imports all of the given sources as new datasets, and commit the result.

    repo - the Kart repo to import into.
    sources - an iterable of TableImportSource objects. Each source is to be imported to source.dest_path.
    settings - optional FastImportSettings: Tuneable settings which affect performance.
    verbosity - integer:
        0: no progress information is printed to stdout.
        1: basic status information
        2: full output of `git-fast-import --stats ...`
    message - the commit-message used when generating the header. Generated if not supplied - see generate_message.
    replace_existing - See ReplaceExisting enum
    from_commit - the commit to be used as a starting point before beginning the import.
    replace_ids - list of PK values to replace, or None
    limit - maximum number of features to import per source.

    The following extra options are used by kart upgrade.
    header - the commit-header to supply git-fast-import. Generated if not supplied - see generate_header.
    extra_cmd_args - any extra args for the git-fast-import command.
    """

    if settings is None:
        settings = FastImportSettings()

    # The commit that this import is using as the basis for the new commit.
    # If we are replacing everything, we start from scratch, so from_commit is None.
    if replace_existing is ReplaceExisting.ALL:
        from_commit = None
    else:
        if from_commit is UNSPECIFIED:
            raise RuntimeError(
                "Caller should specify from_commit when requesting an import that doesn't start from scratch"
            )

    from_tree = from_commit.peel(pygit2.Tree) if from_commit else repo.empty_tree

    assert repo.table_dataset_version in SUPPORTED_VERSIONS
    extra_blobs = (
        extra_blobs_for_version(repo.table_dataset_version) if not from_commit else []
    )

    TableImportSource.check_valid(sources)

    if replace_existing == ReplaceExisting.DONT_REPLACE:
        for source in sources:
            if source.dest_path in from_tree:
                raise InvalidOperation(
                    f"Cannot import to {source.dest_path}/ - already exists in repository"
                )
        assert replace_ids is None

    # Add primary keys if needed.
    sources = PkGeneratingTableImportSource.wrap_sources_if_needed(sources, repo)

    cmd_args = settings.as_args()
    if verbosity < 2:
        cmd_args.append("--quiet")
    for arg in extra_cmd_args:
        cmd_args.append(arg)

    if verbosity >= 1:
        click.echo("Starting git-fast-import...")

    try:
        import_ref = None
        if header is None:
            # import onto a temp branch. then reset the head branch afterwards.
            import_ref = f"refs/kart-import/{uuid.uuid4()}"

            # orig_branch may be None, if head is detached
            # FIXME - this code relies upon the fact that we always either a) import at HEAD (import flow)
            # or b) Fix up the branch heads later (upgrade flow).
            orig_branch = repo.head_branch
            header = generate_header(repo, sources, message, import_ref, from_commit)

        with git_fast_import(repo, *cmd_args) as proc:
            proc.stdin.write(header.encode("utf8"))

            # Write the extra blob that records the repo's version:
            for i, blob_path in write_blobs_to_stream(proc.stdin, extra_blobs):
                if replace_existing != ReplaceExisting.ALL and blob_path in from_tree:
                    raise ValueError(f"{blob_path} already exists")

            for source in sources:
                _import_single_source(
                    repo,
                    source,
                    replace_existing,
                    from_commit,
                    proc,
                    replace_ids,
                    limit,
                    verbosity,
                )

        if import_ref is not None:
            # we created a temp branch for the import above.
            # now we need to reset the head branch to the temp branch tip.
            new_tree = repo.revparse_single(import_ref).peel(pygit2.Tree)
            if not allow_empty:
                if new_tree == from_tree:
                    raise NotFound("No changes to commit", exit_code=NO_CHANGES)

            # use the existing commit details we already imported, but use the new tree
            existing_commit = repo.revparse_single(import_ref).peel(pygit2.Commit)
            repo.create_commit(
                orig_branch or "HEAD",
                existing_commit.author,
                existing_commit.committer,
                existing_commit.message,
                new_tree.id,
                existing_commit.parent_ids,
            )
    finally:
        # remove the import branches
        if import_ref is not None and import_ref in repo.references:
            repo.references.delete(import_ref)


def _import_single_source(
    repo,
    source,
    replace_existing,
    from_commit,
    proc,
    replace_ids,
    limit,
    verbosity,
):
    """
    repo - the Kart repo to import into.
    source - an individual TableImportSource
    replace_existing - See ReplaceExisting enum
    from_commit - the commit to be used as a starting point before beginning the import.
    proc - the subprocess.Popen instance to be used
    replace_ids - list of PK values to replace, or None
    limit - maximum number of features to import per source.
    verbosity - integer:
        0: no progress information is printed to stdout.
        1: basic status information
        2: full output of `git-fast-import --stats ...`
    """
    replacing_dataset = None
    if replace_existing == ReplaceExisting.GIVEN:
        try:
            replacing_dataset = repo.datasets(refish=from_commit)[source.dest_path]
        except KeyError:
            # no such dataset; no problem
            replacing_dataset = None

        fast_import_clear_tree(
            proc=proc,
            replace_ids=replace_ids,
            replacing_dataset=replacing_dataset,
            source=source,
        )

    dataset_class = dataset_class_for_version(repo.table_dataset_version)
    dataset = dataset_class.new_dataset_for_writing(
        source.dest_path, source.schema, repo
    )

    with source:
        if limit:
            num_rows = min(limit, source.feature_count)
            num_rows_text = f"{num_rows:,d} of {source.feature_count:,d}"
        else:
            num_rows = source.feature_count
            num_rows_text = f"{num_rows:,d}"

        if verbosity >= 1:
            click.echo(
                f"Importing {num_rows_text} features from {source} to {source.dest_path}/ ..."
            )

        # Features
        t1 = time.monotonic()
        if replace_ids is not None:
            # As we iterate over IDs, also delete them from the dataset.
            # This means we don't have to load the whole list into memory.
            def _ids():
                for pk in replace_ids:
                    pk = dataset.schema.sanitise_pks(pk)
                    path = dataset.encode_pks_to_path(pk)
                    proc.stdin.write(f"D {path}\n".encode("utf8"))
                    yield pk

            id_iterator = _ids()
            src_iterator = source.get_features(id_iterator, ignore_missing=True)
        else:
            id_iterator = None
            src_iterator = source.features()

        progress_every = None
        if verbosity >= 1:
            progress_every = max(100, 100_000 // (10 ** (verbosity - 1)))

        feature_blobs_already_written = getattr(
            source, "feature_blobs_already_written", False
        )
        if feature_blobs_already_written:
            # This is an optimisation for upgrading repos in-place from V2 -> V3,
            # which are so similar we don't even need to rewrite the blobs.
            feature_blob_iter = source.feature_iter_with_reused_blobs(
                dataset, id_iterator
            )

        elif should_compare_imported_features_against_old_features(
            repo,
            source,
            replacing_dataset,
            from_commit,
        ):
            feature_blob_iter = dataset.import_iter_feature_blobs(
                repo,
                src_iterator,
                source,
                replacing_dataset=replacing_dataset,
            )
        else:
            feature_blob_iter = dataset.import_iter_feature_blobs(
                repo, src_iterator, source
            )

        for i, (feature_path, blob_data) in enumerate(feature_blob_iter):
            if feature_blobs_already_written:
                copy_existing_blob_to_stream(proc.stdin, feature_path, blob_data)
            else:
                write_blob_to_stream(proc.stdin, feature_path, blob_data)

            if i and progress_every and i % progress_every == 0:
                click.echo(f"  {i:,d} features... @{time.monotonic()-t1:.1f}s")

            if limit is not None and i == (limit - 1):
                click.secho(f"  Stopping at {limit:,d} features", fg="yellow")
                break
        t2 = time.monotonic()
        if verbosity >= 1:
            click.echo(f"Added {num_rows:,d} Features to index in {t2-t1:.1f}s")
            click.echo(f"Overall rate: {(num_rows/(t2-t1 or 1E-3)):.0f} features/s)")

        # Meta items - written second as certain importers generate extra metadata as they import features.
        for x in write_blobs_to_stream(
            proc.stdin, dataset.import_iter_meta_blobs(repo, source)
        ):
            pass

    t3 = time.monotonic()
    if verbosity >= 1:
        click.echo(f"Closed in {(t3-t2):.0f}s")


def write_blob_to_stream(stream, blob_path, blob_data):
    stream.write(f"M 644 inline {blob_path}\ndata {len(blob_data)}\n".encode("utf8"))
    stream.write(blob_data)
    stream.write(b"\n")


def write_blobs_to_stream(stream, blob_iterator):
    for i, (blob_path, blob_data) in enumerate(blob_iterator):
        write_blob_to_stream(stream, blob_path, blob_data)
        yield i, blob_path


def copy_existing_blob_to_stream(stream, blob_path, blob_sha):
    stream.write(f"M 644 {blob_sha} {blob_path}\n".encode("utf8"))


def generate_header(repo, sources, message, branch, from_commit):
    if message is None:
        message = generate_message(sources)

    author = repo.author_signature()
    committer = repo.committer_signature()
    result = (
        f"commit {branch}\n"
        f"author {author.name} <{author.email}> {author.time} {minutes_to_tz_offset(author.offset)}\n"
        f"committer {committer.name} <{committer.email}> {committer.time} {minutes_to_tz_offset(committer.offset)}\n"
        f"data {len(message.encode('utf8'))}\n{message}\n"
    )
    if from_commit:
        result += f"from {from_commit.oid}\n"
    return result


def generate_message(sources):
    first_source = next(iter(sources))
    return first_source.aggregate_import_source_desc(sources)
