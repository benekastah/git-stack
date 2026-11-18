"""Integration roots and the ordinary stacks that use them."""

import pathlib
import shlex

import pytest
from git_client.testing import GitCall

from tests.helpers import Repo


def tip(repo: Repo, branch: str, *, remote: bool = False) -> str:
    return repo.git("rev-parse", f"refs/heads/{branch}", cwd=repo.origin if remote else None)


def source(repo: Repo, branch: str, file: str, *, tracked: bool = False) -> None:
    repo.git("switch", "--quiet", "main")
    if tracked:
        repo.client.branch(branch)
    else:
        repo.git("switch", "--quiet", "-c", branch)
    repo.commit(file, f"{branch}\n", branch)


def integration(repo: Repo, *, tracked: bool = False) -> None:
    source(repo, "one", "a", tracked=tracked)
    source(repo, "two", "b")
    repo.stack("integrate", "combined", "one", "two")


def work(repo: Repo) -> None:
    repo.git("switch", "--quiet", "combined")
    repo.stack("create", "work")
    repo.commit("c", "work\n", "work")


def test_create_refresh_and_replace_ordered_sources(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    before = tip(repo, "combined")
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == tip(repo, "one")
    assert repo.git("rev-parse", "refs/heads/combined^1^1") == tip(repo, "main")

    repo.git("switch", "--quiet", "combined")
    assert "combined (integration)" in repo.stack("info", "--offline").stdout
    git_calls.clear()
    repo.stack("integrate", "combined", "one", "two")
    assert tip(repo, "combined") == before
    assert not any(call[:2] == ["worktree", "add"] for call in git_calls)

    repo.stack("integrate", "combined", "two")
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")
    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")
    assert repo.git("branch", "--show-current") == "combined"
    assert repo.git("status", "--porcelain") == ""


@pytest.mark.parametrize("tracked", [False, True])
def test_adopt_preserves_snapshot_until_refresh(repo: Repo, tracked: bool) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    repo.git("branch", "combined", "refs/heads/one")
    before = tip(repo, "combined")
    if tracked:
        repo.stack("track", "main", "combined")
    work(repo)

    repo.stack("integrate", "--adopt", "combined", "one", "two")

    assert tip(repo, "combined") == before
    assert repo.config("work", "stackParent") == "combined"
    assert "combined" not in repo.client.stacks()
    repo.stack("integrate", "combined", "one", "two")
    assert tip(repo, "combined") != before
    assert repo.git("show", "refs/heads/combined:b") == "two"


def conflict_worktree(stderr: str) -> pathlib.Path:
    line = next(line for line in stderr.splitlines() if "merge --continue" in line)
    return pathlib.Path(shlex.split(line.strip())[2])


def test_conflict_continue_and_abort(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    result = repo.stack("integrate", "combined", "one", "two", expect_success=False)
    path = conflict_worktree(result.stderr)
    assert repo.git("branch", "--list", "combined") == ""
    (path / "a").write_text("resolved\n", encoding="utf-8")
    repo.git("add", "a", cwd=path)
    repo.git("-c", "core.editor=true", "merge", "--continue", cwd=path)

    repo.stack("integrate", "combined", "one", "two")

    assert repo.git("show", "refs/heads/combined:a") == "resolved"
    assert not path.exists()
    prior = tip(repo, "combined")
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflict again\n", "another")
    repo.stack("integrate", "combined", "one", "two", expect_success=False)
    repo.git("merge", "--abort", cwd=path)
    repo.stack("integrate", "--abort", "combined")
    assert not path.exists()
    assert tip(repo, "combined") == prior


@pytest.mark.parametrize("command", ["pull", "sync"])
def test_sync_does_not_move_an_integration_as_an_ordinary_root(repo: Repo, command: str) -> None:
    integration(repo)
    work(repo)
    before = tip(repo, "combined")

    result = repo.stack(command, expect_success=False)

    assert "rebuild it with `git stack integrate`" in result.stderr
    assert tip(repo, "combined") == before


def test_clean_leaves_integration_tip_in_place(repo: Repo) -> None:
    integration(repo)
    work(repo)
    before = tip(repo, "combined")
    repo.git("push", "--quiet", "--set-upstream", "origin", "refs/heads/combined")
    repo.commit_on_forge("combined", "d", "upstream\n", "upstream added")

    repo.stack("clean")

    assert tip(repo, "combined") == before
