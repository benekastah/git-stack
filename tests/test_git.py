"""GitClient's reading of branch config, which it caches."""

import os
import pathlib

from git_stack.git import GitClient
from tests.conftest import GIT_REPOSITORY_VARIABLES
from tests.helpers import Repo


def test_config_get_sees_changes_made_by_other_processes(repo: Repo) -> None:
    git = GitClient(repo.path)
    assert git.config_get("main", "stackParent") is None

    repo.git("config", "branch.main.stackParent", "trunk")

    assert git.config_get("main", "stackParent") == "trunk"


def test_config_get_handles_dots_in_branch_names_and_any_key_case(repo: Repo) -> None:
    repo.git("config", "branch.release.1.2.stackParent", "main")

    assert GitClient(repo.path).config_get("release.1.2", "STACKPARENT") == "main"


def test_tests_ignore_a_hooks_repository(repo: Repo) -> None:
    """The pre-push hook runs the tests with GIT_DIR set to the repository
    being pushed, which their git commands mustn't use."""
    assert not set(GIT_REPOSITORY_VARIABLES) & os.environ.keys()
    # As paths, since git writes Windows paths with forward slashes.
    assert pathlib.Path(repo.git("rev-parse", "--absolute-git-dir")).resolve() == (repo.path / ".git").resolve()
