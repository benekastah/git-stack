"""Isolation and repository ownership for integration test fixtures."""

import os
import subprocess
from pathlib import Path

import pygit2

from tests.git_support import real_git
from tests.helpers import Repo


def test_global_config_is_shared_by_git_and_pygit2(repo: Repo, git_config: Path) -> None:
    assert Path(os.environ["GIT_CONFIG_GLOBAL"]) == git_config
    repo.git("config", "--global", "user.email", "fixture@example.test")

    assert pygit2.Repository(repo.path).config["user.email"] == "fixture@example.test"


def test_default_working_directory_is_outside_a_repository() -> None:
    result = subprocess.run([real_git(), "rev-parse", "--git-dir"], capture_output=True, check=False)

    assert result.returncode != 0


def test_copied_clones_use_their_own_origin(repo: Repo) -> None:
    assert Path(repo.git("remote", "get-url", "origin")) == repo.origin
    assert Path(repo.git("remote", "get-url", "origin", cwd=repo.forge)) == repo.origin
