"""tests/large_repo.py, with a repository small enough to build in the default run."""

import os
import pathlib
import subprocess

import pytest

from tests import large_repo

SMALL = large_repo.Recipe(history=30, branches=4, commits_per_day=10, branch_window_days=2)


def git(directory: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], capture_output=True, encoding="utf-8", check=True
    ).stdout.strip()


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """One build for the tests, which don't change it."""
    directory = tmp_path_factory.mktemp("large") / "built"
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
        fork = git(built / "local", "merge-base", "main", branch)
        # How far back on main it forked: within the window.
        assert int(git(built / "local", "rev-list", "--count", f"{fork}..main")) <= (
            SMALL.branch_window_days * SMALL.commits_per_day
        )


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


def test_a_copy_links_only_git_objects_even_under_a_directory_named_objects(tmp_path: pathlib.Path) -> None:
    # Where the repository is says nothing of which of its files are git objects.
    built = tmp_path / "objects" / "built"
    large_repo.build(built, SMALL)
    large_repo.copy(built / "local", tmp_path / "copy")
    tracked = next((tmp_path / "copy" / "src").rglob("*.py"))
    original = built / "local" / tracked.relative_to(tmp_path / "copy")

    tracked.write_text("changed in place\n", encoding="utf-8")

    assert original.read_text(encoding="utf-8") != "changed in place\n"
    assert os.stat(tmp_path / "copy" / ".git" / "config").st_ino != os.stat(built / "local" / ".git" / "config").st_ino


def test_it_refuses_to_build_in_a_pytest_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Workers would each build the repository, and delete one another's half-built one.
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    with pytest.raises(RuntimeError, match="-n 0"):
        large_repo.ensure_one_process()


def test_it_builds_in_a_single_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    large_repo.ensure_one_process()


def test_pruning_keeps_the_current_build_and_removes_old_ones_and_leftover_copies(tmp_path: pathlib.Path) -> None:
    (tmp_path / "old-key").mkdir()
    (tmp_path / "old-key" / "file").write_text("x", encoding="utf-8")
    (tmp_path / "current" / "work" / "killed-run").mkdir(parents=True)
    (tmp_path / "current" / "built").touch()

    large_repo.prune(tmp_path, "current")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["current"]
    assert sorted(path.name for path in (tmp_path / "current").iterdir()) == ["built"]
