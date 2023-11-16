import logging
import re
import struct
import sys
from enum import Enum
from functools import cached_property
from pathlib import Path

import click
import pygit2

from kart import is_windows
from kart.exceptions import (
    NO_REPOSITORY,
    NO_SPATIAL_FILTER_INDEX,
    InvalidOperation,
    NotFound,
    SubprocessError,
    translate_subprocess_exit_code,
)
from kart.lfs_util import install_lfs_hooks
from kart.key_filters import RepoKeyFilter
from kart.tabular.version import (
    DEFAULT_NEW_REPO_VERSION,
    dataset_class_for_version,
    ensure_supported_repo_wide_version,
    get_repo_wide_version,
)
from kart.structure import RepoStructure
from kart import subprocess_util as subprocess
from kart.timestamps import tz_offset_to_minutes
from kart.working_copy import WorkingCopy

L = logging.getLogger("kart.repo")

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class KartRepoFiles:
    """Useful files that are found in `repo.gitdir_path`"""

    # Standard git files:
    HEAD = "HEAD"
    INDEX = "index"
    COMMIT_EDITMSG = "COMMIT_EDITMSG"
    ORIG_HEAD = "ORIG_HEAD"  # The head to reset to in case of an --abort eg `kart merge --abort`.
    MERGE_HEAD = "MERGE_HEAD"  # The head we are merging HEAD with.
    MERGE_MSG = "MERGE_MSG"  # The draft of a commit message to use for the merge.

    # Kart-specific files:
    MERGE_BRANCH = "MERGE_BRANCH"  # The branch name that we merged with, if any.
    # An index file containing the current state of the merge, including cleanly merged items, conflicts, and resolutions.
    MERGED_INDEX = "MERGED_INDEX"
    # A tree containing the current state of the merge - or near enough - it can't store unresolved conflicts:
    MERGED_TREE = "MERGED_TREE"
    # A sqlite table that maps each feature SHA to its EPSG:4326 envelope. Used for spatial filtered clones.
    FEATURE_ENVELOPES = "feature_envelopes.db"


class KartRepoState(Enum):
    NORMAL = "normal"
    MERGING = "merging"

    @classmethod
    def bad_state_message(cls, bad_state, allowed_states, command_extra):
        """Generates a generic message about a disallowed_state if no specific message is provided."""
        # Only two states exist right now so logic is pretty simple:
        cmd = click.get_current_context().command_path
        if command_extra:
            cmd = f"{cmd} {command_extra}"
        if bad_state == KartRepoState.MERGING:
            return (
                f'`{cmd}` does not work while the Kart repo is in "merging" state.\n'
                "Use `kart merge --abort` to abandon the merge and get back to the previous state."
            )
        return f'`{cmd}` only works when the Kart repo is in "merging" state, but it is in "normal" state.'


KartRepoState.ALL_STATES = (KartRepoState.NORMAL, KartRepoState.MERGING)


class KartConfigKeys:
    """
    Kart specifig config variables found in repo.config
    (which is read from the file at `repo.gitdir_path / "config"`)
    """

    # Whichever of these two variables is written, controls whether the repo is kart branded or not.
    KART_REPOSTRUCTURE_VERSION = "kart.repostructure.version"
    SNO_REPOSITORY_VERSION = "sno.repository.version"

    KART_WORKINGCOPY_LOCATION = "kart.workingcopy.location"
    SNO_WORKINGCOPY_PATH = "sno.workingcopy.path"

    KART_SPATIALFILTER_GEOMETRY = "kart.spatialfilter.geometry"
    KART_SPATIALFILTER_CRS = "kart.spatialfilter.crs"
    KART_SPATIALFILTER_REFERENCE = "kart.spatialfilter.reference"
    KART_SPATIALFILTER_OBJECTID = "kart.spatialfilter.objectid"

    # This variable was also renamed, but when tidy-style repos were added - not during rebranding.
    CORE_BARE = "core.bare"  # Newer repos use the standard "core.bare" variable.
    SNO_WORKINGCOPY_BARE = (
        "sno.workingcopy.bare"  # Older repos use this custom variable instead.
    )

    BRANDED_REPOSTRUCTURE_VERSION_KEYS = {
        "kart": KART_REPOSTRUCTURE_VERSION,
        "sno": SNO_REPOSITORY_VERSION,
    }

    BRANDED_WORKINGCOPY_LOCATION_KEYS = {
        "kart": KART_WORKINGCOPY_LOCATION,
        "sno": SNO_WORKINGCOPY_PATH,
    }


def locked_git_index(extension_name):
    """
    Returns an empty index file, but extended with a required extension in the extensions section of the index binary
    format. (Not the file extension - the filename is simply "index", it has no file extension.)
    Causes all git commands that would involve the index or working copy to fail with "unsupported extension: NAME" -
    where name is "kart" or ".sno", giving the user a clue as to which command they *should* be using.
    in that sense it is "locked" to git. Various techniques can be used to unlock it if certain git functionality is
    needed - eg marking the repository as bare so it is ignored, or removing the unsupported extension.
    """
    assert isinstance(extension_name, bytes)
    assert len(extension_name) == 4
    first_char = extension_name[0]
    assert not (first_char >= ord("A") and first_char <= ord("Z"))

    GIT_INDEX_VERSION = 2
    BASE_EMPTY_GIT_INDEX = struct.pack(">4sII", b"DIRC", GIT_INDEX_VERSION, 0)

    # Extension name must not start with A-Z, therefore is a required extension.
    # See https://git-scm.com/docs/index-format

    extension = struct.pack(">4sI", extension_name, 0)
    data = BASE_EMPTY_GIT_INDEX + extension
    # Append checksum to the end.
    return data + pygit2.hash(data).raw


LOCKED_GIT_INDEX_CONTENTS = {
    "kart": locked_git_index(b"kart"),  # These extension names must be 4 bytes long
    "sno": locked_git_index(b".sno"),  # and they must not start with a capital letter
}


class KartRepo(pygit2.Repository):
    """
    A valid pygit2.Repository, since all Kart repos are also git repos - but with some added functionality.
    Ensures the git directory structure is one of the two styles supported by Kart - "bare-style" or "tidy-style".
    For tidy-style, prevents workdir-related git commands from working by using a "locked git index".
    Helps set up Kart specific config, and adds support for pathlib Paths.

    The two styles of Kart repos:
    Originally, all Kart repos were implemented as bare git repositorys. Some had GPKG working copies, some did not.
    Since they were bare git repositories, all the git internals were immediately visible inside the root directory -
    right alongside the GPKG. For this reason, they were kind of "untidy".

    Eventually, this style of repo was named a "bare-style" Kart repo. "Bare-style" Kart repo's are always implemented
    as bare git repositories, but they may or may not have a working copy, so they may or may not be actually "bare".

    A new style of Kart repo was added - a "tidy-style" Kart repo. This type of Kart repo is implemented as a non-bare
    git repository, so the git internals are hidden in a ".kart" subfolder, leaving the root folder mostly empty as
    a place to put a GPKG file or similar. If a "tidy-style" Kart repo were to be reconfigured, it *could* have its
    working copy emoved and so be made bare. But going forward, "bare-style" Kart repos are supposed to be used for
    actual bare Kart repos, and "tidy-style" are supposed to be used for Kart repos with a working copy.

    Note: this is not enforced, especially since all legacy "bare-style" Kart repos violate this assumption.
    """

    # Kart was previously named Sno. Kart branding is now the default, but repos with Sno branding are still supported.
    BRANDING_FOR_NEW_REPOS = "kart"
    DIRNAME_FOR_NEW_REPOS = f".{BRANDING_FOR_NEW_REPOS}"

    # Directory names that we look in for a Kart / Sno repo - both continue to be supported.
    KART_DIR_NAMES = (".kart", ".sno")

    def __init__(self, path, *, validate=True):
        path = Path(path).resolve()

        for d in self.KART_DIR_NAMES:
            if (path / d).exists():
                path = path / d
                break

        try:
            super().__init__(
                str(path),
                # Instructs pygit2 not to look at the working copy or the index.
                pygit2.GIT_REPOSITORY_OPEN_BARE | pygit2.GIT_REPOSITORY_OPEN_FROM_ENV,
            )
        except pygit2.GitError:
            raise NotFound("Not an existing Kart repository", exit_code=NO_REPOSITORY)

        self.gitdir_path = Path(self.path).resolve()

        if self.is_bare:
            self.workdir_path = self.gitdir_path
        else:
            self.workdir_path = self.gitdir_path.parent.resolve()

        if validate:
            self.validate_gitdir_name()

        self.working_copy = WorkingCopy(self)

    def __repr__(self):
        return f"KartRepo({self.path!r})"

    @classmethod
    def init_repository(
        cls,
        repo_root_path,
        wc_location=None,
        bare=False,
        initial_branch=None,
        spatial_filter_spec=None,
    ):
        """
        Initialise a new Kart repo. A Kart repo is basically a git repo, except -
        - git internals are stored in .kart instead of .git
          (.git is a file that contains a reference to .kart, this is allowed by git)
        - datasets are stored in /.sno-dataset/ trees according to a particular dataset format version -
          see DATASETS_v2.md. But, this only matters when there are commits. At this stage they are not yet present.
        - there is a blob called .sno.repository.version that contains the dataset format version number - but, this
          written in the first commit. At this stage it is not yet present.
        - there is property in the repo config called kart.repostructure.version that contains the dataset format version
          number, which is used until the sno.repository.version blob is written.
        - there are extra properties in the repo config about where / how the working copy is written.
        - the .kart/index file has been extended to stop git messing things up - see LOCKED_EMPTY_GIT_INDEX.
        """
        repo_root_path = repo_root_path.resolve()
        cls._ensure_exists_and_empty(repo_root_path)
        if not bare:
            from kart.tabular.working_copy.base import TableWorkingCopy

            TableWorkingCopy.check_valid_creation_location(
                wc_location, PotentialRepo(repo_root_path)
            )

        extra_args = []
        if initial_branch is not None:
            extra_args += [f"--initial-branch={initial_branch}"]
        if bare:
            # Create bare-style repo:
            kart_repo = cls._create_with_git_command(
                [
                    "git",
                    "init",
                    "--bare",
                    *extra_args,
                    str(repo_root_path),
                ],
                gitdir_path=repo_root_path,
            )
        else:
            # Create tidy-style repo:
            dot_kart_path = repo_root_path / cls.DIRNAME_FOR_NEW_REPOS
            dot_init_path = repo_root_path / ".init"

            kart_repo = cls._create_with_git_command(
                [
                    "git",
                    "init",
                    f"--separate-git-dir={dot_kart_path}",
                    *extra_args,
                    str(dot_init_path),
                ],
                gitdir_path=dot_kart_path,
                temp_workdir_path=dot_init_path,
            )
            kart_repo.lock_git_index()

        kart_repo.write_config(
            wc_location,
            bare,
            spatial_filter_spec,
            table_dataset_version=DEFAULT_NEW_REPO_VERSION,
        )
        kart_repo.write_attributes()
        kart_repo.write_readme()
        kart_repo.activate()
        install_lfs_hooks(kart_repo)
        return kart_repo

    @classmethod
    def clone_repository(
        cls,
        clone_url,
        repo_root_path,
        clone_args,
        wc_location=None,
        bare=False,
        spatial_filter_spec=None,
        spatial_filter_after_clone=False,
    ):
        repo_root_path = repo_root_path.resolve()
        cls._ensure_exists_and_empty(repo_root_path)
        if not bare:
            from kart.tabular.working_copy.base import TableWorkingCopy

            TableWorkingCopy.check_valid_creation_location(
                wc_location, PotentialRepo(repo_root_path)
            )

        extra_args = []
        is_spatial_filter_clone = False
        if spatial_filter_spec is not None:
            # Make sure we fetch any spatial filters that might exist - we need those straight away.
            # TODO - This is a bit magic, look into further. We might need it always - or there might be another way.
            extra_args = [
                "-c",
                "remote.origin.fetch=+refs/filters/*:refs/filters/*",
            ]
            if not spatial_filter_after_clone:
                is_spatial_filter_clone = True
                partial_clone_spec = spatial_filter_spec.partial_clone_filter_spec()
                extra_args.append(partial_clone_spec)
                click.echo(
                    f"Cloning using git spatial filter extension: {partial_clone_spec}",
                    err=True,
                )

        if bare:
            kart_repo = cls._clone_with_git_command(
                [
                    "git",
                    "clone",
                    "--bare",
                    *extra_args,
                    *clone_args,
                    clone_url,
                    str(repo_root_path),
                ],
                gitdir_path=repo_root_path,
                is_spatial_filter_clone=is_spatial_filter_clone,
            )

        else:
            dot_kart_path = (
                repo_root_path if bare else repo_root_path / cls.DIRNAME_FOR_NEW_REPOS
            )
            dot_clone_path = repo_root_path / ".clone"

            kart_repo = cls._clone_with_git_command(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    f"--separate-git-dir={dot_kart_path}",
                    *extra_args,
                    *clone_args,
                    clone_url,
                    str(dot_clone_path),
                ],
                gitdir_path=dot_kart_path,
                temp_workdir_path=dot_clone_path,
                is_spatial_filter_clone=is_spatial_filter_clone,
            )
            kart_repo.lock_git_index()

        kart_repo.write_config(wc_location, bare, spatial_filter_spec)
        kart_repo.write_attributes()
        kart_repo.write_readme()
        kart_repo.activate()
        install_lfs_hooks(kart_repo)
        return kart_repo

    @classmethod
    def _create_with_git_command(cls, cmd, gitdir_path, temp_workdir_path=None):
        proc = subprocess.run_and_tee_output(cmd, tee_stderr=not is_windows)
        if proc.returncode != 0:
            raise SubprocessError(
                f"Error calling {cmd[0]} {cmd[1]}",
                exit_code=proc.returncode,
                stderr=b"" if is_windows else proc.stderr,
            )

        result = KartRepo(gitdir_path, validate=False)

        # Tidy up temp workdir - this is created as a side effect of the git command.
        if temp_workdir_path is not None and temp_workdir_path.exists():
            if (temp_workdir_path / ".git").exists():
                (temp_workdir_path / ".git").unlink()
            temp_workdir_path.rmdir()

        return result

    @classmethod
    def _clone_with_git_command(
        cls, cmd, gitdir_path, temp_workdir_path=None, is_spatial_filter_clone=False
    ):
        try:
            return cls._create_with_git_command(cmd, gitdir_path, temp_workdir_path)
        except SubprocessError as e:
            if is_spatial_filter_clone and (
                b"invalid filter-spec" in e.stderr
                or b"No filter extension found" in e.stderr
                or b"No spatial index found" in e.stderr
                or b"error reading section header" in e.stderr
            ):
                # This error was caused (or probably caused) by the remote not supporting spatial filters.
                raise InvalidOperation(
                    "Remote doesn't appear to support the spatial filter extension. To clone the entire repository and "
                    "apply the spatial filter afterwards, add the flag `--spatial-filter-after-clone`.",
                    exit_code=NO_SPATIAL_FILTER_INDEX,
                )
            else:
                raise

    @cached_property
    def diff_annotations(self):
        # DiffAnnotations is slow to import - don't move this to the top of this file.
        from .annotations import DiffAnnotations

        return DiffAnnotations(self)

    def write_config(
        self,
        wc_location=None,
        bare=False,
        spatial_filter_spec=None,
        table_dataset_version=None,
    ):
        # Whichever of these variable is written, controls whether this repo is kart branded or not.
        version_key = KartConfigKeys.BRANDED_REPOSTRUCTURE_VERSION_KEYS[
            self.BRANDING_FOR_NEW_REPOS
        ]

        if table_dataset_version is None:
            table_dataset_version = self.table_dataset_version
        self.config[version_key] = str(table_dataset_version)

        self.config["core.bare"] = bare
        # Force writing to reflogs:
        self.config["core.logAllRefUpdates"] = "always"
        # Write working copy config:
        from kart.tabular.working_copy.base import TableWorkingCopy

        TableWorkingCopy.write_config(self, wc_location, bare)
        if spatial_filter_spec:
            spatial_filter_spec.write_config(self)

        self.write_lfs_filter_config()

    def write_lfs_filter_config(self):
        # TODO - try to bundle this config with the bundled Git, instead of once per repo.
        self.config["filter.lfs.process"] = "git-lfs filter-process"
        self.config["filter.lfs.required"] = True
        self.config["filter.lfs.clean"] = "filter.lfs.clean"
        self.config["filter.lfs.smudge"] = "filter.lfs.smudge"

    def ensure_supported_version(self):
        ensure_supported_repo_wide_version(self.table_dataset_version)

    def write_attributes(self):
        info_path = self.gitdir_path / "info"
        info_path.mkdir(exist_ok=True)
        # File attributes
        with (info_path / "attributes").open("a+") as f:
            f.write("**/.*-dataset*/feature/** diff=binary merge=binary -text\n")
            f.write("**/.*-dataset*/tile/** filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.laz filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.las filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.tif filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.tiff filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.tif.aux.xml filter=lfs diff=lfs merge=lfs -text\n")
            f.write("*.tiff.aux.xml filter=lfs diff=lfs merge=lfs -text\n")

        # Files potentially in workdir that should not be checked in:
        with (info_path / "exclude").open("a+") as f:
            f.write(".git\n")
            f.write(".kart\n")
            f.write("KART_README.*\n")

        # TODO - configure sparse checkout and use it to check out attachments (possibly).
        # The files in the ODB that should be checked out are:
        # /*                     Checkout every file
        # !.kart                 But not .kart.repostructure.version or similar
        # !**/.*-dataset*/**     And not anything in a dataset folder

    def write_readme(self):
        readme_filename = f"{self.branding.upper()}_README.txt"
        readme_text = self.get_readme_text(self.is_bare, self.branding)
        try:
            self.workdir_file(readme_filename).write_text(readme_text)
        except Exception as e:
            L.warning(e)

    @classmethod
    def get_readme_text(cls, is_bare, branding):
        text = cls.KART_BARE_README if is_bare else cls.KART_NORMAL_README
        text = "\n".join(text)
        if branding == "sno":
            text = (
                text.replace(
                    KartConfigKeys.KART_WORKINGCOPY_LOCATION,
                    KartConfigKeys.SNO_WORKINGCOPY_PATH,
                )
                .replace("kartproject.org", "sno.earth")
                .replace("Kart", "Sno")
                .replace("kart", "sno")
            )
        return text

    def activate(self):
        """
        We create new+tidy repos in .kart/ but we don't write the .git file pointing to .kart/ until everything
        else is ready, and until that file is written, git or kart commands won't find the repo.
        So, if creation fails, the result will be something that doesn't work at all, not something that half
        works but is also half corrupted.
        """
        if self.is_bare:
            # Bare repos are always activated - since all the files are right there in the root directory,
            # we can't reveal them by writing the .git file. So, no action is required here.
            return

        dot_git_path = self.workdir_path / ".git"
        dot_kart_path = self.gitdir_path
        dot_kart_name = dot_kart_path.stem
        # .kart is linked from .git at this point, which means git (or kart) can find it
        # and so the repository is activated (ie, git or kart commands will work):
        dot_git_path.write_text(f"gitdir: {dot_kart_name}\n", encoding="utf-8")

        if is_windows:
            # Hide .git and .kart
            # Best effort: if it doesn't work for some reason, continue anyway.
            subprocess.call(["attrib", "+h", str(dot_git_path)])
            subprocess.call(["attrib", "+h", str(dot_kart_path)])

    @property
    def branding(self):
        if KartConfigKeys.KART_REPOSTRUCTURE_VERSION in self.config:
            return "kart"
        elif KartConfigKeys.SNO_REPOSITORY_VERSION in self.config:
            return "sno"
        # Pre V2 repos (no longer fully supported - need to be upgraded) are always Sno branded.
        if self.BRANDING_FOR_NEW_REPOS != "sno" and self.table_dataset_version < 2:
            return "sno"
        # New repo, config is not yet written. Refer to BRANDING_FOR_NEW_REPOS
        return self.BRANDING_FOR_NEW_REPOS

    @property
    def is_kart_branded(self):
        return self.branding == "kart"

    def validate_gitdir_name(self):
        if not self.is_bare and self.gitdir_path.stem not in self.KART_DIR_NAMES:
            raise NotFound(
                "Selected repo doesn't follow Kart convention of keeping internals in a '.kart' folder. Perhaps a git repo?",
                exit_code=NO_REPOSITORY,
            )

    @property
    def REPOSTRUCTURE_VERSION_KEY(self):
        return KartConfigKeys.BRANDED_REPOSTRUCTURE_VERSION_KEYS[self.branding]

    @property
    def WORKINGCOPY_LOCATION_KEY(self):
        return KartConfigKeys.BRANDED_WORKINGCOPY_LOCATION_KEYS[self.branding]

    @cached_property
    def table_dataset_version(self):
        """Returns the Kart repository version - eg 2 for 'Datasets V2' See DATASETS_v2.md"""
        return get_repo_wide_version(self)

    @property
    def workingcopy_location(self):
        """Return the path to the Kart working copy, if one exists."""
        return self.get_config_str(self.WORKINGCOPY_LOCATION_KEY)

    @property
    def spatial_filter(self):
        from .spatial_filter import SpatialFilter

        return SpatialFilter.from_repo_config(self)

    def configure_do_checkout_datasets(self, dataset_paths, do_checkout):
        for dataset_path in dataset_paths:
            key = f"dataset.{dataset_path}.checkout"
            if do_checkout:
                # Checking out a dataset is the default, we don't clutter the config with it.
                self.del_config(key)
            else:
                # Specifically mark this dataset as do-not-checkout.
                self.config[key] = False

    @property
    def non_checkout_datasets(self):
        result = set()
        config = self.config
        for entry in config:
            parts = entry.name.split(".", maxsplit=3)
            if len(parts) > 3:
                # Handle a name-containing-dots ie "dataset.NAME.CONTAINING.DOTS.checkout"
                prefix, rest = entry.name.split(".", maxsplit=1)
                parts = [prefix, *rest.rsplit(".", maxsplit=1)]
            if (
                len(parts) == 3
                and parts[0] == "dataset"
                and parts[2] == "checkout"
                and not config.get_bool(entry.name)
            ):
                result.add(parts[1])
        return result

    def get_config_str(self, key, default=None):
        return self.config[key] if key in self.config else default

    @property
    def is_partial_clone(self):
        from . import promisor_utils

        return promisor_utils.get_partial_clone_filter(self) is not None

    def lock_git_index(self):
        index_contents = LOCKED_GIT_INDEX_CONTENTS[self.branding]
        (self.gitdir_path / KartRepoFiles.INDEX).write_bytes(index_contents)

    @property
    def state(self):
        merge_head = self.gitdir_file(KartRepoFiles.MERGE_HEAD).exists()
        merged_index = self.gitdir_file(KartRepoFiles.MERGED_INDEX).exists()
        if merge_head and not merged_index:
            raise NotFound(
                'Kart repo is in "merging" state, but required file MERGED_INDEX is missing.\n'
                "Try `kart merge --abort` to return to a good state."
            )
        return KartRepoState.MERGING if merge_head else KartRepoState.NORMAL

    def structure(self, refish="HEAD", allow_unborn_head=True):
        """Get the structure of this Kart repository at a particular revision."""
        self.ensure_supported_version()
        return RepoStructure(self, refish, allow_unborn_head=allow_unborn_head)

    def datasets(
        self,
        refish="HEAD",
        *,
        repo_key_filter=RepoKeyFilter.MATCH_ALL,
        filter_dataset_type=None,
    ):
        """
        Get the datasets of this Kart repository at a particular revision.
        Equivalent to: self.structure(refish).datasets(**kwargs)
        """
        return self.structure(refish).datasets(
            repo_key_filter=repo_key_filter, filter_dataset_type=filter_dataset_type
        )

    @property
    def dataset_class(self):
        self.ensure_supported_version()
        return dataset_class_for_version(self.table_dataset_version)

    def del_config(self, key):
        config = self.config
        if key in config:
            del config[key]

    def invoke_git(self, *args, **kwargs):
        try:
            args = ["git", *args]
            subprocess.check_call(args, cwd=self.workdir_path, **kwargs)
        except subprocess.CalledProcessError as e:
            sys.exit(translate_subprocess_exit_code(e.returncode))

    def gc(self, *args):
        """Runs git-gc on the Kart repository."""
        self.invoke_git("gc", *args)

    def _ensure_exists_and_empty(dir_path):
        if dir_path.exists() and any(dir_path.iterdir()):
            raise InvalidOperation(f'"{dir_path}" isn\'t empty')
        elif not dir_path.exists():
            dir_path.mkdir(parents=True)

    @property
    def head_commit(self):
        """
        Returns the commit at the current repo HEAD. Returns None if there is no commit at HEAD - ie, head_is_unborn.
        """
        return None if self.head_is_unborn else self.head.peel(pygit2.Commit)

    @property
    def head_tree(self):
        """
        Returns the tree at the current repo HEAD. Returns None if there is no tree at HEAD - ie, head_is_unborn.
        """
        return None if self.head_is_unborn else self.head.peel(pygit2.Tree)

    @cached_property
    def empty_tree(self):
        f"""Returns the empty tree, with SHA {EMPTY_TREE_SHA}."""
        return self[EMPTY_TREE_SHA]

    @property
    def head_branch(self):
        """
        Returns the branch that HEAD is currently on. Returns None if head is not on a branch - ie, head_is_detached.
        """
        return None if self.head_is_detached else self.references["HEAD"].target

    @property
    def head_branch_shorthand(self):
        """
        Returns the shorthand for the branch that HEAD is currently on.
        Returns None if head is not on a branch - ie, head_is_detached.
        """
        if self.head_is_detached:
            return None
        return self.references["HEAD"].target.rsplit("/", 1)[-1]

    @property
    def head_remote_name(self):
        """
        Returns the name of the remote that the HEAD branch is currently tracking.
        Returns None if HEAD is not currently on a branch that is tracking a remote.
        """
        if self.head_is_detached or self.head_is_unborn:
            return None
        head_branch_shorthand = self.head_branch_shorthand
        if not head_branch_shorthand:
            return None
        branch = self.branches.get(head_branch_shorthand)
        if not branch or not branch.upstream:
            return None
        return branch.upstream.remote_name

    @property
    def head_remote(self):
        """
        Returns the pygit2.Remote object that the HEAD branch is currently tracking.
        Returns None if HEAD is not currently on a branch that is tracking a remote.
        """
        head_remote_name = self.head_remote_name
        if not head_remote_name:
            return None
        try:
            return self.remotes[head_remote_name]
        except KeyError:
            return None

    @property
    def head_remote_name_or_default(self):
        """
        Returns the name of the remote that the HEAD branch is currently tracking.
        Returns "origin" if HEAD is not currently on a branch that is tracking a remote,
        as long as origin is the name of a remote. Otherwise, returns None.
        """
        remote_name = self.head_remote_name
        if remote_name:
            return remote_name
        try:
            if self.remotes["origin"]:
                return "origin"
        except KeyError:
            return None

    _GIT_VAR_OUTPUT_RE = re.compile(
        r"^(?P<name>.*) <(?P<email>[^>]*)> (?P<time>\d+) (?P<offset>[+-]?\d+)$"
    )

    def _signature(self, person_type, **overrides):
        # 'git var' lets us use the environment variables to
        # control the user info, e.g. GIT_AUTHOR_DATE.
        # libgit2/pygit2 doesn't handle those env vars at all :(
        env_overrides = {}

        name = overrides.pop("name", None)
        if name is not None:
            env_overrides[f"GIT_{person_type}_NAME"] = name

        email = overrides.pop("email", None)
        if email is not None:
            env_overrides[f"GIT_{person_type}_EMAIL"] = email

        output = subprocess.check_output(
            ["git", "var", f"GIT_{person_type}_IDENT"],
            cwd=self.path,
            encoding="utf8",
            env_overrides=env_overrides,
        )
        m = self._GIT_VAR_OUTPUT_RE.match(output)
        kwargs = m.groupdict()
        kwargs["time"] = int(kwargs["time"])
        kwargs["offset"] = tz_offset_to_minutes(kwargs["offset"])
        kwargs.update(overrides)
        return pygit2.Signature(**kwargs)

    def author_signature(self, **overrides):
        return self._signature("AUTHOR", **overrides)

    def committer_signature(self, **overrides):
        return self._signature("COMMITTER", **overrides)

    def gitdir_file(self, rel_path):
        return self.gitdir_path / rel_path

    def workdir_file(self, rel_path):
        return self.workdir_path / rel_path

    @cached_property
    def lfs_tmp_path(self):
        result = self.gitdir_path / "lfs" / "objects" / "tmp"
        result.mkdir(parents=True, exist_ok=True)
        return result

    def write_gitdir_file(self, rel_path, text):
        assert isinstance(text, str)
        if not text.endswith("\n"):
            text += "\n"
        self.gitdir_file(rel_path).write_text(text, encoding="utf-8")

    def read_gitdir_file(self, rel_path, missing_ok=False, strip=False):
        path = self.gitdir_file(rel_path)
        if missing_ok and not path.exists():
            return None
        result = path.read_text(encoding="utf-8")
        if strip:
            result = result.strip()
        return result

    def remove_gitdir_file(self, rel_path, missing_ok=True):
        path = self.gitdir_file(rel_path)
        if missing_ok and not path.exists():
            return
        path.unlink()

    def revparse_single(self, revision):
        # FIXME: Overridden to work around https://github.com/libgit2/libgit2/issues/6123
        if revision == "@":
            revision = "HEAD"
        return super().revparse_single(revision)

    def revparse_ext(self, revision):
        # FIXME: Overridden to work around https://github.com/libgit2/libgit2/issues/6123
        if revision == "@":
            revision = "HEAD"
        return super().revparse_ext(revision)

    def merge_base(self, oid1, oid2):
        # FIXME: Overridden to work around https://github.com/koordinates/kart/issues/555
        # Caused by https://github.com/libgit2/libgit2/issues/6123
        try:
            args = ["git", "-C", self.path, "merge-base", str(oid1), str(oid2)]
            output = subprocess.check_output(args, encoding="utf8")
            return pygit2.Oid(hex=output.strip())
        except subprocess.CalledProcessError:
            return None

    KART_COMMON_README = [
        "",
        "kart status",
        "",
        'It may simply output "Empty repository. Use kart import to add some data".',
        "Follow the tutorial at https://kartproject.org for help getting started with Kart.",
        "",
        "Some more helpful commands for getting a broad view of what a Kart repository",
        "contains are:",
        "",
        "kart log      - show the history of what has been committed to this repository.",
        "kart data ls  - show the names of every dataset in this repository.",
        "",
        "This directory is the default location where Kart puts the repository's working",
        "copy, which is created as soon as there is some data to put in it. However",
        "the working copy can also be configured to be somewhere else, and may not be",
        "a file at all. To see the working copy's location, run this command:",
        "",
        "kart config kart.workingcopy.location",
        "",
        "",
    ]

    KART_NORMAL_README = [
        "This directory is a Kart repository.",
        "",
        "It may look empty, but every version of every datasets that this repository",
        'contains is stored in Kart\'s internal format in the ".kart" hidden subdirectory.',
        "To check if a directory is a Kart repository and see what is stored, run:",
    ] + KART_COMMON_README

    KART_BARE_README = [
        "This directory is a bare Kart repository.",
        "",
        "In this bare repository, the internals are visible - in files and in subdirectories",
        'like "HEAD", "objects" and "refs". These are best left untouched. Instead, use',
        "Kart commands to interact with the repository. To check if a directory is a Kart",
        "repository and see what is stored, run:",
    ] + KART_COMMON_README


class PotentialRepo:
    """
    A repo that doesn't yet exist, but which we are considering whether to create.
    Used for calling code that needs a repo as context, for example:
    >>> WorkingCopy.check_valid_creation_location(location, repo)
    """

    def __init__(self, workdir_path):
        self.workdir_path = workdir_path
        self.branding = KartRepo.BRANDING_FOR_NEW_REPOS
        self.is_kart_branded = self.branding == "kart"
