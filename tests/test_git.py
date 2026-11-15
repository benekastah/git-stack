"""GitClient's reading of branch config and refs, which it caches."""

import os
import pathlib
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from git_stack.git import GitClient, changes_refs
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


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The git commands GitClient runs from now on, each without `git -C <dir>`
    (which the tests' own git commands don't have)."""
    calls: list[list[str]] = []
    real: Callable[..., Any] = subprocess.run

    def run(args: list[str], *rest: Any, **kwargs: Any) -> Any:
        if args[:2] == ["git", "-C"]:
            calls.append(args[3:])
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_snapshot_reads_refs_once(repo: Repo, git_calls: list[list[str]]) -> None:
    git = GitClient(repo.path)
    main = repo.git("rev-parse", "main")

    with git.snapshot():
        assert git.rev("refs/heads/main") == main
        assert git.tip("main") == main
        assert git.rev("main@{upstream}") == main
        assert git.rev("refs/remotes/origin/main") == main
        assert git.local_branch_exists("main")
        assert git.local_branches() == {"main"}
        assert not git.upstream_gone("main")

    assert [call[0] for call in git_calls] == ["for-each-ref"]


def test_snapshot_asks_git_about_refs_it_doesnt_have(repo: Repo, git_calls: list[list[str]]) -> None:
    """On a case-insensitive filesystem, git can find a ref under a name that
    differs in case, which the snapshot can't."""
    repo.git("branch", "feature")
    git = GitClient(repo.path)
    outside = (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature"))

    with git.snapshot():
        assert not git.local_branch_exists("nope")
        assert git.rev("refs/heads/nope") is None
        assert (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature")) == outside


def test_snapshot_isnt_read_for_refs_it_cant_answer(repo: Repo, git_calls: list[list[str]]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        git.rev("HEAD")
        git.rev("main~0")

    assert [call[0] for call in git_calls] == ["rev-parse", "rev-parse"]


def test_snapshot_reads_refs_again_after_git_changes_them(repo: Repo) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.local_branch_exists("new")

        git.run("branch", "new")

        assert git.local_branch_exists("new")
        assert git.rev("new@{upstream}") is None

        git.run("branch", "--set-upstream-to=origin/main", "new")

        assert git.rev("new@{upstream}") == git.tip("main")


def test_snapshot_outlives_stack_metadata_writes_but_not_upstream_changes(
    repo: Repo, git_calls: list[list[str]]
) -> None:
    git = GitClient(repo.path)
    repo.git("branch", "--no-track", "new")
    with git.snapshot():
        assert git.rev("new@{upstream}") is None
        git.config_set("new", "stackParent", "main")
        assert git.rev("new@{upstream}") is None
        assert [call[0] for call in git_calls] == ["for-each-ref", "config"]

        git.config_set("new", "remote", "origin")
        git.config_set("new", "merge", "refs/heads/main")

        assert git.rev("new@{upstream}") == git.tip("main")


def test_snapshot_ends_with_the_outermost_block(repo: Repo) -> None:
    """Changes made by other processes are seen once the snapshot ends."""
    main = repo.git("rev-parse", "main")
    repo.commit("a", "more\n", "more")
    moved = repo.git("rev-parse", "main")
    repo.git("update-ref", "refs/heads/main", main)
    git = GitClient(repo.path)
    with git.snapshot():
        with git.snapshot():
            assert git.tip("main") == main
        repo.git("update-ref", "refs/heads/main", moved)
        assert git.tip("main") == main

    assert git.tip("main") == moved


def test_snapshot_leaves_revisions_to_git(repo: Repo) -> None:
    repo.commit("a", "more\n", "more")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.rev("refs/heads/main~1") == repo.git("rev-parse", "main~1")
        assert git.rev("main") == repo.git("rev-parse", "main")


def test_snapshot_sees_a_deleted_upstream(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "feature")
    repo.git("push", "--quiet", "-u", "origin", "feature")
    repo.git("push", "--quiet", "origin", ":feature")
    repo.git("fetch", "--quiet", "--prune")
    git = GitClient(repo.path)

    with git.snapshot():
        assert git.upstream_gone("feature")
        assert not git.upstream_gone("main")
        assert git.rev("feature@{upstream}") is None


@pytest.mark.parametrize(
    ("args", "changes"),
    [
        (("rev-parse", "HEAD"), False),
        (("--literal-pathspecs", "log", "main"), False),
        (("branch", "--show-current"), False),
        (("branch", "new"), True),
        # git switch creates a branch from a remote one of its name.
        (("switch", "--quiet", "main"), True),
        (("switch", "--quiet", "--create", "new"), True),
        (("worktree", "list", "--porcelain"), False),
        (("worktree", "add", "x"), True),
        (("remote",), False),
        (("remote", "set-head", "origin", "--auto"), True),
        (("remote", "get-url", "origin"), False),
        (("ls-tree", "HEAD"), False),
        (("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"), False),
        (("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"), True),
        (("-c", "core.quotepath=off", "log"), False),
        (("-c", "log.x=y", "push", "origin"), True),
        (("config", "--get", "remote.origin.url"), False),
        (("config", "remote.origin.url", "x"), True),
        (("rebase", "main"), True),
        (("push", "origin", "main"), True),
    ],
)
def test_changes_refs(args: tuple[str, ...], changes: bool) -> None:
    assert changes_refs(args) is changes
