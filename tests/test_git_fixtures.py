"""Isolation and repository ownership for integration test fixtures."""

import os
import subprocess
from pathlib import Path

import pygit2
import pytest

from tests.git_support import real_git
from tests.helpers import Repo
from tests.operations import expect_operations


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


def test_child_ignores_libgit2_config_from_the_home_directory(
    repo: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text('[remote "unused"]\n    promisor = true\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    repo.make_stack()

    with expect_operations(git=2):
        assert repo.stack_process("base").stdout.strip() == repo.git("rev-parse", "pr2")
