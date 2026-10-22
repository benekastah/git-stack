"""Branches checked out in another worktree, which git won't check out, rebase or
delete here."""

import pathlib

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import StackClient
from tests.helpers import Repo


def add_worktree(repo: Repo, branch: str) -> pathlib.Path:
    path = repo.tmp / f"{branch}-worktree"
    repo.git("worktree", "add", "--quiet", str(path), branch)
    return path


def test_restack_skips_branch_in_other_worktree_and_its_children(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    worktree = add_worktree(repo, "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    pr2 = repo.git("rev-parse", "pr2")

    client.restack()

    assert events.NotRestackedInOtherWorktree("pr2", str(worktree)) in emitted
    assert events.ParentSkipped("pr3", "pr2") in emitted
    assert repo.git("rev-parse", "pr2") == pr2
    assert repo.git("status", "--porcelain", cwd=worktree) == ""
    assert repo.git("branch", "--show-current") == "pr1"


def test_restack_finishes_once_other_worktree_moves_on(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    worktree = add_worktree(repo, "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    client.restack()
    repo.git("switch", "--quiet", "--detach", cwd=worktree)

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_branch_in_other_worktree_that_needs_no_restack_is_left_quietly(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    add_worktree(repo, "pr2")
    repo.commit("c", "pr3 more\n", "pr3 c2")

    client.restack()

    assert not any(isinstance(e, events.NotRestackedInOtherWorktree) for e in emitted)


def test_sync_skips_branch_in_other_worktree_and_restacks_the_rest(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    worktree = add_worktree(repo, "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    client.sync()

    repo.assert_stacked("pr1", "main", ["pr1 c3", "pr1 c2", "pr1 c1"])
    assert events.NotRestackedInOtherWorktree("pr2", str(worktree)) in emitted
    assert events.ParentSkipped("pr3", "pr2") in emitted
    assert repo.git("status", "--porcelain", cwd=worktree) == ""


def test_sync_leaves_upstream_commits_for_branch_in_other_worktree(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    worktree = add_worktree(repo, "pr1")
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")

    client.sync()

    assert events.NotPulledInOtherWorktree("pr1", str(worktree)) in emitted
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_delete_untracks_but_keeps_merged_branch_in_other_worktree(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    worktree = add_worktree(repo, "pr1")
    repo.squash_merge_on_forge("pr1")

    client.sync(delete="local")

    assert events.NotDeletedInOtherWorktree("pr1", str(worktree)) in emitted
    repo.assert_untracked("pr1")
    assert repo.git("rev-parse", "--verify", "--quiet", "refs/heads/pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_deleting_current_branch_skips_child_in_other_worktree(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    add_worktree(repo, "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.squash_merge_on_forge("pr1")

    client.sync(delete="local")

    assert repo.git("branch", "--show-current") == "main"
    assert not repo.git("branch", "--list", "pr1")


def test_restack_squash_skips_branch_in_other_worktree(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    worktree = add_worktree(repo, "pr1")
    repo.git("switch", "--quiet", "pr2")

    client.restack(squash=True)

    assert events.NotSquashedInOtherWorktree("pr1", str(worktree)) in emitted
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]


def test_restack_stopped_in_other_worktree_is_left_to_finish_there(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 b\n", "pr1 c3")  # conflicts with pr2 c1
    repo.git("switch", "--quiet", "pr3")
    worktree = add_worktree(repo, "pr1")
    worktree_client = StackClient(GitClient(worktree))
    with pytest.raises(StackError, match="conflict"):
        worktree_client.restack()

    # Here, pr2 is neither checked out nor rebasing, but must be left alone.
    client.restack()

    assert events.NotRestackedInOtherWorktree("pr2", str(worktree)) in emitted
    (worktree / "b").write_text("resolved\n")
    repo.git("add", "b", cwd=worktree)
    repo.git("rebase", "--continue", cwd=worktree)
    worktree_client.restack()
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
