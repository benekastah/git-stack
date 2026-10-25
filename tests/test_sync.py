"""git stack sync."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchStatus, StackClient
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

    assert client.sync(push=False) == ["pr2", "pr3"]


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
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
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
    repo: Repo, delete_branch: bool, client: StackClient
) -> None:
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
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
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
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
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


def test_sync_records_pull_despite_info_run_before_it(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    # As an editor showing `git stack info` would, while sync has written the
    # pending pull but hasn't started cherry-picking yet.
    editor = StackClient(GitClient(repo.path))
    switch = client.git.switch

    def run_editor_first(branch: str) -> None:
        editor.info()
        switch(branch)

    monkeypatch.setattr(client.git, "switch", run_editor_first)

    client.sync()

    assert repo.config("pr1", "stackPulled") == repo.git("rev-parse", "origin/pr1")


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


def test_sync_doesnt_pull_from_upstream_of_another_name(repo: Repo, client: StackClient) -> None:
    # As `git switch -c pr2 origin/main` would set it.
    repo.make_stack()
    repo.commit_on_forge("main", "d", "main\n", "main c1")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--set-upstream-to=origin/main", "pr2")

    client.sync()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


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


def test_sync_pull_stopped_by_untracked_file_is_retried_after_cherry_pick_abort(
    repo: Repo, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.commit_on_forge("pr1", "e", "collab 2\n", "collab 2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.write("d", "untracked\n")

    with pytest.raises(StackError, match="pulling upstream commits into pr1 stopped without a conflict") as failure:
        client.sync()
    assert "git cherry-pick --abort" in str(failure.value)
    with pytest.raises(StackError, match="in progress"):
        client.sync()

    (repo.path / "d").unlink()
    repo.git("cherry-pick", "--abort")
    client.sync()
    assert repo.own_commits("pr1", "main") == ["collab 2", "collab", "pr1 c3", "pr1 c2", "pr1 c1"]
    assert repo.config("pr1", "stackPulled") == repo.git("rev-parse", "origin/pr1")


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


def test_sync_pull_aborted_then_committed_over_in_same_second_by_same_author_is_not_pulled(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    client.push()
    monkeypatch.setenv("GIT_AUTHOR_DATE", "1700000000 +0000")
    repo.commit_on_forge("pr1", "a", "other machine\n", "from my other machine", author="Test <test@example.com>")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    with pytest.raises(StackError, match="pulling upstream commits into pr1 stopped on a conflict"):
        client.sync()
    repo.git("cherry-pick", "--abort")
    repo.commit("b", "more\n", "pr1 c4")

    with pytest.raises(StackError, match="origin/pr1 has commits that pr1 doesn't"):
        client.push()

    assert repo.remote_subject("pr1") == "from my other machine"


def test_sync_skips_branches_above_a_missing_parent(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("branch", "--quiet", "-D", "pr1")
    repo.git("switch", "--quiet", "pr3")

    client.sync()

    assert any(
        isinstance(event, events.BrokenBranch)
        and event.branch == "pr2"
        and "pr2's parent pr1 doesn't exist" in event.reason
        for event in emitted
    )
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

    assert repo.own_commits("solo", "main") == ["solo c1"]
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.git("rev-parse", "pr2") == pr2


def test_sync_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.sync()

    assert repo.git("rev-parse", "main") != repo.git("rev-parse", "main", cwd=repo.origin)


def test_sync_leaves_diverged_trunk_alone(repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.commit_on_forge("main", "d", "upstream\n", "upstream change")
    repo.commit("a", "local\n", "local change")
    client.branch("pr1")
    local_main = repo.git("rev-parse", "main")

    client.sync()

    assert events.TrunkDiverged("main", "origin/main") in emitted
    assert repo.git("rev-parse", "main") == local_main


def test_sync_leaves_trunk_checked_out_in_another_worktree_alone(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    worktree = repo.tmp / "main-worktree"
    repo.git("worktree", "add", "--quiet", str(worktree), "main")
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")
    local_main = repo.git("rev-parse", "main")

    client.sync()

    assert any(isinstance(e, events.TrunkInOtherWorktree) and e.branch == "main" for e in emitted)
    assert repo.git("rev-parse", "main") == local_main
    assert repo.git("status", "--porcelain", cwd=worktree) == ""


def test_sync_ignores_old_copies_of_rebased_commits_missing_from_reflog(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr1")
    repo.git("reset", "--quiet", "--hard", "pr1~")
    repo.commit("a", "pr1 second, reworked\n", "pr1 c2")
    repo.git("reflog", "expire", "--expire=now", "--all")

    client.sync()

    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]
    assert (repo.path / "a").read_text() == "pr1 second, reworked\n"


def test_sync_leaves_branch_on_main_alone_when_main_moves_without_conflict(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    tips = {branch: repo.git("rev-parse", branch) for branch in ("pr1", "pr2", "pr3")}
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")

    unpushed = client.sync()

    assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")
    assert {branch: repo.git("rev-parse", branch) for branch in tips} == tips
    assert unpushed == []
    assert all(branch.status in (None, BranchStatus.UP_TO_DATE) for branch in client.info())


def test_sync_rebases_branch_on_main_that_conflicts_with_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("main", "a", "upstream\n", "main touches a")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--force", "main", "origin/main")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK

    with pytest.raises(StackError, match="restacking pr1 stopped on a conflict"):
        client.sync()


def test_sync_latest_moves_branch_on_main_onto_its_tip(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")

    client.sync(latest=True)

    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_pushes_the_branches_it_restacked(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")

    assert client.sync() == []

    assert repo.git("rev-parse", "origin/pr2") == repo.git("rev-parse", "pr2")
    assert repo.git("rev-parse", "origin/pr3") == repo.git("rev-parse", "pr3")


def test_sync_does_not_push_a_branch_without_an_upstream(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr3")
    client.branch("pr4")
    repo.commit("a", "pr4\n", "pr4 c1")
    repo.squash_merge_on_forge("pr1")

    client.sync()

    assert repo.git("rev-parse", "origin/pr3") == repo.git("rev-parse", "pr3")
    assert repo.git("branch", "--remotes", "--list", "origin/pr4") == ""


def test_sync_stopped_on_a_conflict_pushes_nothing_until_rerun(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    pushed = repo.git("rev-parse", "origin/pr2")
    repo.commit_on_forge("pr1", "a", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    with pytest.raises(StackError, match="stopped on a conflict"):
        client.sync()
    assert repo.git("rev-parse", "origin/pr2") == pushed
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("cherry-pick", "--continue")
    client.sync()

    assert repo.git("rev-parse", "origin/pr1") == repo.git("rev-parse", "pr1")
    assert repo.git("rev-parse", "origin/pr2") == repo.git("rev-parse", "pr2")


def test_sync_leaves_reverted_squash_merge_and_its_children_alone(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    pr2 = repo.git("rev-parse", "pr2")

    client.sync()

    assert events.Reverted("pr1", "main") in emitted
    assert events.ParentSkipped("pr2", "pr1") in emitted
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.git("rev-parse", "pr2") == pr2


def test_sync_leaves_reverted_merge_commit_alone(repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.merge_on_forge("pr1")
    repo.revert_on_forge("-m", "1", "HEAD")

    client.sync()

    assert events.Reverted("pr1", "main") in emitted
    assert repo.config("pr1", "stackParent") == "main"


def test_sync_leaves_branch_alone_when_one_of_its_commits_was_reverted(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.commit("b", "pr1\n", "pr1 c2")
    client.push()
    repo.merge_on_forge("pr1")
    # The other commit changes neither the lines it changed nor the lines around them.
    repo.revert_on_forge(repo.git("rev-parse", "pr1~1"))

    client.sync()

    assert events.Reverted("pr1", "main") in emitted
    assert repo.config("pr1", "stackParent") == "main"


def test_sync_untracks_branch_whose_revert_was_reverted(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    repo.revert_on_forge("HEAD")

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_ignore_revert_untracks_reverted_branch_as_merged(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    repo.git("switch", "--quiet", "pr2")

    client.sync(ignore_reverts=["pr1"])

    assert not any(isinstance(e, (events.Reverted, events.MergedParentReverted)) for e in emitted)
    repo.assert_untracked("pr1")
    assert repo.config("pr2", "stackParent") == "main"
    # So the next sync doesn't warn that pr2's merged parent was reverted.
    assert not repo.config_exists("pr2", "stackMergedParent")


def test_sync_ignore_revert_refuses_branch_outside_the_stacks_it_syncs(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("solo")
    repo.commit("c", "solo\n", "solo c1")

    # pr1 is tracked, but in another stack, so this sync would never see it.
    with pytest.raises(StackError, match="there's no revert of pr1 to ignore in the stacks being synced"):
        client.sync(ignore_reverts=["pr1"])
