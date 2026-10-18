"""git stack sync."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_sync_after_parent_squash_merged(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr1")

    client.sync()

    repo.assert_untracked("pr1")
    assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr1"


def test_sync_returns_restacked_branches_to_push(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")

    assert client.sync() == ["pr2", "pr3"]


def test_sync_returns_nothing_to_push_when_up_to_date(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()

    assert client.sync() == []


def test_sync_delete_local_deletes_merged_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr1")

    client.sync(delete="local")

    assert repo.git("branch", "--list", "pr1") == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    # We were on pr1, which no longer exists, so we land on its child.
    assert repo.git("branch", "--show-current") == "pr2"


def test_sync_delete_local_lands_on_parent_of_childless_branch(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.push()
    repo.squash_merge_on_forge("pr1")

    client.sync(delete="local")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--show-current") == "main"


def test_sync_after_parent_squash_merged_with_remote_branch_kept(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    client.sync()

    assert events.RemoteBranchKept("origin/pr1", "origin", "pr1") in emitted
    repo.assert_untracked("pr1")
    assert repo.remote_subject("pr1") == "pr1 c2"
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_delete_remote_deletes_only_remote_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    client.sync(delete="remote")

    repo.assert_untracked("pr1")
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.git("branch", "--remotes", "--list", "origin/pr1") == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_delete_both_deletes_local_and_remote_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    client.sync(delete="both")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_after_local_squash_merge_pushed(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("push", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_after_local_fast_forward_merge_of_unpushed_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--ff-only", "pr1")
    repo.git("push", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


@pytest.mark.parametrize("delete_branch", [True, False], ids=["remote branch deleted", "remote branch kept"])
def test_sync_after_squash_merge_followed_by_edit_to_same_lines(
        repo: Repo, delete_branch: bool, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=delete_branch)
    repo.commit_on_forge("main", "a", "later\n", "main edits a again")

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_after_rebase_merge_followed_by_edit_to_same_lines(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("main", "d", "main\n", "main moves on")
    # Rebase-merge pr1, so main gets copies of its commits, then edit
    # the same line again.
    repo.git("switch", "--quiet", "-C", "pr1", "origin/pr1", cwd=repo.forge)
    repo.git("rebase", "--quiet", "main", cwd=repo.forge)
    repo.git("switch", "--quiet", "main", cwd=repo.forge)
    repo.git("merge", "--quiet", "--ff-only", "pr1", cwd=repo.forge)
    (repo.forge / "a").write_text("later\n")
    repo.git("commit", "--quiet", "-am", "main edits a again", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr1", cwd=repo.forge)

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_skips_merged_branch_with_commits_pushed_after_merge(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    repo.commit_on_forge("pr1", "d", "late\n", "pushed after merge")

    client.sync()

    assert events.MergedWithNewCommits("pr1", "main") in emitted
    assert events.ParentSkipped("pr2", "pr1") in emitted
    assert repo.config("pr2", "stackParent") == "pr1"


def test_sync_keeps_new_branch_without_commits(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    client.push()

    client.sync()

    assert repo.git("branch", "--list", "pr1") == "* pr1"
    assert repo.config("pr1", "stackParent") == "main"


def test_sync_after_two_levels_squash_merged(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("pull", "--quiet", cwd=repo.forge)
    # GitHub retargets pr2 onto main; merging it then squashes pr1's
    # original commits and pr2's together.
    repo.git("merge", "--quiet", "--squash", "origin/pr2", cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr2", cwd=repo.forge)
    repo.git("switch", "--quiet", "pr3")

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "main", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"


def test_sync_keeps_branch_deleted_without_merging(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.git("push", "--quiet", "origin", ":pr1", cwd=repo.forge)

    client.sync()

    assert events.UpstreamGone("pr1", "main") in emitted
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_sync_after_child_squash_merged_into_parent(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-c", "pr1", "origin/pr1", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "origin/pr2", cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "pr1", ":pr2", cwd=repo.forge)

    client.sync()

    repo.assert_untracked("pr2")
    assert repo.git("rev-parse", "pr1") == repo.git("rev-parse", "origin/pr1")
    repo.assert_stacked("pr3", "pr1", ["pr3 c1"])


def test_sync_fast_forwards_branch_to_commits_pushed_by_others(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")

    client.sync()

    assert repo.git("rev-parse", "pr1") == repo.git("rev-parse", "origin/pr1")
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_pulls_commits_pushed_by_others_into_diverged_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    client.sync()
    client.sync()

    assert repo.own_commits("pr1", "main") == ["collab", "pr1 c3", "pr1 c2", "pr1 c1"]
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    client.push()
    assert repo.remote_subject("pr1") == "collab"


def test_sync_pull_conflict_resolved_with_cherry_pick_continue(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "a", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    with pytest.raises(StackError, match="pulling upstream commits into pr1 stopped on a conflict"):
        client.sync()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("cherry-pick", "--continue")
    client.sync()

    assert repo.own_commits("pr1", "main") == ["collab", "pr1 c3", "pr1 c2", "pr1 c1"]
    assert (repo.path / "a").read_text() == "resolved\n"
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_sync_pull_aborted_then_committed_over_is_not_pulled(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "a", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    with pytest.raises(StackError, match="pulling upstream commits into pr1 stopped on a conflict"):
        client.sync()
    repo.git("cherry-pick", "--abort")
    repo.commit("b", "more\n", "pr1 c4")

    with pytest.raises(StackError, match="origin/pr1 has commits that pr1 doesn't"):
        client.push()

    assert repo.remote_subject("pr1") == "collab"


def test_sync_skips_branches_above_a_missing_parent(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("branch", "--quiet", "-D", "pr1")
    repo.git("switch", "--quiet", "pr3")

    client.sync()

    assert any(isinstance(event, events.BrokenBranch) and event.branch == "pr2"
               and "pr2's parent pr1 doesn't exist" in event.reason for event in emitted)
    assert events.ParentSkipped("pr3", "pr2") in emitted


def test_sync_leaves_other_stacks_alone(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("solo")
    repo.commit("c", "solo\n", "solo c1")
    client.push()
    repo.git("switch", "--quiet", "pr1")
    client.push()
    repo.squash_merge_on_forge("pr1")
    pr2 = repo.git("rev-parse", "pr2")
    repo.git("switch", "--quiet", "solo")

    client.sync()

    repo.assert_stacked("solo", "main", ["solo c1"])
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.git("rev-parse", "pr2") == pr2


def test_sync_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.sync()

    assert repo.git("rev-parse", "main") != repo.git("rev-parse", "main", cwd=repo.origin)


def test_sync_leaves_diverged_trunk_alone(
        repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.commit_on_forge("main", "d", "upstream\n", "upstream change")
    repo.commit("a", "local\n", "local change")
    client.branch("pr1")
    local_main = repo.git("rev-parse", "main")

    client.sync()

    assert events.TrunkDiverged("main", "origin/main") in emitted
    assert repo.git("rev-parse", "main") == local_main
