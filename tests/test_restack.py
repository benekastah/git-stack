"""git stack restack."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from tests.helpers import Repo


def test_restack_moves_whole_stack_after_parent_changes(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"
    assert all(branch.status is not BranchStatus.NEEDS_RESTACK for branch in client.info())


def test_restack_onto_retargets_after_local_squash_merge(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    client.restack(onto="main")

    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_skips_branch_merged_locally(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")

    assert repo.branch_info("pr1").status is BranchStatus.MERGED
    repo.git("switch", "--quiet", "pr2")
    client.restack()

    assert events.SkippedMerged("pr1", "main") in emitted
    assert repo.git("branch", "--list", "pr1") == "pr1"
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_conflict_resolved_with_rebase_continue_records_new_base(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr1")

    # pr1 restacks cleanly; pr2 conflicts and pr3 is left for later.
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()

    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")
    assert repo.branch_info("pr2") == BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=True)
    client.restack()

    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_aborted_restack_keeps_previous_base(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    old_base = repo.config("pr1", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main change\n", "main touches a")
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError):
        client.restack()
    repo.git("rebase", "--abort")

    assert repo.branch_info("pr1") == BranchInfo("pr1", "main", BranchStatus.NEEDS_RESTACK, current=True)
    assert repo.config("pr1", "stackBase") == old_base


def test_aborted_restack_onto_keeps_previous_parent(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    old_base = repo.config("pr2", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr2")

    with pytest.raises(StackError):
        client.restack(onto="main")
    repo.git("rebase", "--abort")

    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.config("pr2", "stackBase") == old_base


def test_restack_onto_refuses_parent_that_isnt_a_local_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    with pytest.raises(StackError, match="origin/main isn't a local branch"):
        client.restack(onto="origin/main")

    assert repo.config("pr2", "stackParent") == "pr1"


@pytest.mark.parametrize("target", ["pr3", "pr1"])
def test_restack_onto_rejects_cycle(repo: Repo, target: str, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="would create a cycle"):
        client.restack(onto=target)

    assert repo.config("pr1", "stackParent") == "main"


def test_restack_failure_without_conflict_is_not_reported_as_conflict(
        repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    hook = repo.path / ".git" / "hooks" / "pre-rebase"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    with pytest.raises(StackError, match="restacking pr2 onto pr1 failed") as failure:
        client.restack()

    assert "conflict" not in str(failure.value)
    assert not repo.config_exists("pr2", "stackPendingParent")


def test_restack_squash_squashes_each_branch_in_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "pr2 second\n", "pr2 c2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.git("switch", "--quiet", "pr2")

    client.restack(squash=True)

    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("log", "-1", "--format=%B", "pr1") == "pr1 c1\n\npr1 c2\n\npr1 c3"
    assert repo.git("log", "-1", "--format=%B", "pr2") == "pr2 c1\n\npr2 c2"
    assert [repo.git("show", f"pr3:{name}") for name in "abc"] == ["pr1 third", "pr2 second", "pr3"]
    assert repo.git("branch", "--show-current") == "pr2"


def test_restack_squash_resolves_a_line_edited_by_every_commit_once(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "function(A, B)\n", "pr1 c1")
    client.branch("pr2")
    repo.commit("a", "function(A, B, D)\n", "pr2 c1")
    repo.commit("a", "function(A, B, E)\n", "pr2 c2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "function(A, F)\n", "pr1 c2")
    repo.git("switch", "--quiet", "pr2")

    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack(squash=True)
    repo.write("a", "function(A, F, E)\n")
    repo.git("add", "a")
    # The only conflict: continuing finishes the rebase.
    repo.git("rebase", "--continue")
    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    assert repo.git("show", "pr2:a") == "function(A, F, E)"


def test_restack_onto_suggests_tracking_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "loose")

    with pytest.raises(StackError, match="loose isn't in a stack; use `git stack track` first"):
        client.restack(onto="main")



def test_restack_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main\n", "main moved")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.restack()

    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main~")
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]
