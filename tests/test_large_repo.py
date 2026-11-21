"""tests/large_repo.py, with a repository small enough to build in the default run."""

import os
import pathlib
import subprocess

import pytest

from tests import large_repo

SMALL = large_repo.Recipe(history=30, branches=4, commits_per_day=10, branch_window_days=2)


def git(directory: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def built(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "built"
    large_repo.build(directory, SMALL)
    return directory


def test_it_builds_the_history_and_a_branch_for_each_of_the_recipes_branches(built: pathlib.Path) -> None:
    assert git(built / "origin.git", "rev-list", "--count", "main") == "30"
    assert len(git(built / "origin.git", "for-each-ref", "refs/heads/feature").splitlines()) == 4
    assert len(git(built / "local", "for-each-ref", "refs/heads/feature").splitlines()) == 4
    assert len(git(built / "local", "for-each-ref", "refs/remotes/origin/feature").splitlines()) == 4


def test_each_branch_forks_from_main_within_the_window_and_has_one_to_three_commits(built: pathlib.Path) -> None:
    for branch in git(built / "local", "for-each-ref", "--format=%(refname)", "refs/heads/feature").splitlines():
        own = int(git(built / "local", "rev-list", "--count", f"main..{branch}"))
        assert 1 <= own <= 3
        fork = int(
            git(built / "local", "rev-list", "--count", f"main..{git(built / 'local', 'merge-base', 'main', branch)}")
        )
        assert fork == 0
        assert int(
            git(built / "local", "rev-list", "--count", f"{git(built / 'local', 'merge-base', 'main', branch)}..main")
        ) <= (SMALL.branch_window_days * SMALL.commits_per_day)


def test_main_gets_the_recipes_commits_a_day(built: pathlib.Path) -> None:
    newest = int(git(built / "local", "log", "-1", "--format=%ct", "main"))
    a_day_older = int(git(built / "local", "log", "-1", "--format=%ct", f"main~{SMALL.commits_per_day}"))

    assert newest - a_day_older == large_repo.DAY


def test_it_builds_the_same_repository_every_time(tmp_path: pathlib.Path, built: pathlib.Path) -> None:
    large_repo.build(tmp_path / "again", SMALL)

    assert git(tmp_path / "again" / "local", "rev-parse", "main") == git(built / "local", "rev-parse", "main")


def test_the_key_changes_with_the_recipe() -> None:
    assert SMALL.key() != large_repo.Recipe(history=31, branches=4, commits_per_day=10, branch_window_days=2).key()


def test_a_copy_links_the_objects_instead_of_copying_them(tmp_path: pathlib.Path, built: pathlib.Path) -> None:
    large_repo.copy(built / "local", tmp_path / "copy")

    pack = next((built / "local" / ".git" / "objects" / "pack").glob("*.pack"))
    assert os.stat(tmp_path / "copy" / ".git" / "objects" / "pack" / pack.name).st_ino == os.stat(pack).st_ino
    assert git(tmp_path / "copy", "rev-parse", "main") == git(built / "local", "rev-parse", "main")


def test_a_copys_working_tree_and_config_are_its_own(tmp_path: pathlib.Path, built: pathlib.Path) -> None:
    large_repo.copy(built / "local", tmp_path / "copy")
    tracked = next(path for path in (tmp_path / "copy" / "src").rglob("*.py"))
    original = built / "local" / tracked.relative_to(tmp_path / "copy")
    before = original.read_text(encoding="utf-8")

    tracked.write_text("changed in place\n", encoding="utf-8")

    assert original.read_text(encoding="utf-8") == before
    assert os.stat(tmp_path / "copy" / ".git" / "config").st_nlink == 1


def test_a_copy_refuses_to_copy_what_it_cant_link(
    tmp_path: pathlib.Path, built: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr(os, "link", refuse)

    with pytest.raises(RuntimeError, match="same filesystem"):
        large_repo.copy(built / "local", tmp_path / "copy")

    assert not (tmp_path / "copy" / ".git" / "objects" / "pack").exists() or not list(
        (tmp_path / "copy" / ".git" / "objects" / "pack").glob("*.pack")
    )
