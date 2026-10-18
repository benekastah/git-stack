"""git stack sync."""

import pytest

from tests.helpers import Repo


def test_sync_after_parent_squash_merged(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr1")

    repo.stack("sync")

    repo.assert_untracked("pr1")
    assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr1"


def test_sync_lists_restacked_branches_to_push(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")

    result = repo.stack("sync")

    assert result.stdout.endswith("pull requests of:\n  pr2\n  pr3\n"), result.stdout


def test_sync_lists_nothing_to_push_when_up_to_date(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")

    result = repo.stack("sync")

    assert "git stack push" not in result.stdout


def test_sync_delete_local_deletes_merged_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr1")

    repo.stack("sync", "-d", "local")

    assert repo.git("branch", "--list", "pr1") == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    # We were on pr1, which no longer exists, so we land on its child.
    assert repo.git("branch", "--show-current") == "pr2"


def test_sync_delete_local_lands_on_parent_of_childless_branch(repo: Repo) -> None:
    repo.stack("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")

    repo.stack("sync", "-d", "local")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--show-current") == "main"


def test_sync_after_parent_squash_merged_with_remote_branch_kept(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    result = repo.stack("sync")

    assert "origin/pr1 still exists" in result.stdout
    repo.assert_untracked("pr1")
    assert repo.remote_subject("pr1") == "pr1 c2"
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_delete_remote_deletes_only_remote_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    repo.stack("sync", "--delete", "remote")

    repo.assert_untracked("pr1")
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.git("branch", "--remotes", "--list", "origin/pr1") == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_delete_both_deletes_local_and_remote_branches(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    repo.stack("sync", "-d", "both")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_after_local_squash_merge_pushed(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("push", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_after_local_fast_forward_merge_of_unpushed_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--ff-only", "pr1")
    repo.git("push", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


@pytest.mark.parametrize("delete_branch", [True, False], ids=["remote branch deleted", "remote branch kept"])
def test_sync_after_squash_merge_followed_by_edit_to_same_lines(repo: Repo, delete_branch: bool) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=delete_branch)
    repo.commit_on_forge("main", "a", "later\n", "main edits a again")

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_after_rebase_merge_followed_by_edit_to_same_lines(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
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

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_sync_skips_merged_branch_with_commits_pushed_after_merge(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    repo.commit_on_forge("pr1", "d", "late\n", "pushed after merge")

    result = repo.stack("sync")

    assert "skipping pr1: it was merged into main, but it or its remote branch has commits" in result.stderr
    assert "skipping pr2 because its parent pr1 was skipped" in result.stderr
    assert repo.config("pr2", "stackParent") == "pr1"


def test_sync_keeps_new_branch_without_commits(repo: Repo) -> None:
    repo.stack("branch", "pr1")
    repo.stack("push")

    repo.stack("sync")

    assert repo.git("branch", "--list", "pr1") == "* pr1"
    assert repo.config("pr1", "stackParent") == "main"


def test_sync_after_two_levels_squash_merged(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.git("pull", "--quiet", cwd=repo.forge)
    # GitHub retargets pr2 onto main; merging it then squashes pr1's
    # original commits and pr2's together.
    repo.git("merge", "--quiet", "--squash", "origin/pr2", cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr2", cwd=repo.forge)
    repo.git("switch", "--quiet", "pr3")

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "main", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"


def test_sync_keeps_branch_deleted_without_merging(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("push", "--quiet", "origin", ":pr1", cwd=repo.forge)

    result = repo.stack("sync")

    assert "pr1's remote branch is gone but its changes aren't in main" in result.stderr
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_sync_after_child_squash_merged_into_parent(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-c", "pr1", "origin/pr1", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "origin/pr2", cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "pr1", ":pr2", cwd=repo.forge)

    repo.stack("sync")

    repo.assert_untracked("pr2")
    assert repo.git("rev-parse", "pr1") == repo.git("rev-parse", "origin/pr1")
    repo.assert_stacked("pr3", "pr1", ["pr3 c1"])


def test_sync_fast_forwards_branch_to_commits_pushed_by_others(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")

    repo.stack("sync")

    assert repo.git("rev-parse", "pr1") == repo.git("rev-parse", "origin/pr1")
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_sync_pulls_commits_pushed_by_others_into_diverged_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    repo.stack("sync")
    repo.stack("sync")

    assert repo.own_commits("pr1", "main") == ["collab", "pr1 c3", "pr1 c2", "pr1 c1"]
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.stack("push")
    assert repo.remote_subject("pr1") == "collab"


def test_sync_pull_conflict_resolved_with_cherry_pick_continue(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.commit_on_forge("pr1", "a", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    result = repo.stack("sync", expect_success=False)
    assert "pulling upstream commits into pr1 stopped on a conflict" in result.stderr
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("cherry-pick", "--continue")
    repo.stack("sync")

    assert repo.own_commits("pr1", "main") == ["collab", "pr1 c3", "pr1 c2", "pr1 c1"]
    assert (repo.path / "a").read_text() == "resolved\n"
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_sync_skips_branches_above_a_missing_parent(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("branch", "--quiet", "-D", "pr1")
    repo.git("switch", "--quiet", "pr3")

    result = repo.stack("sync")

    assert "skipping pr2: pr2's parent pr1 doesn't exist" in result.stderr
    assert "skipping pr3 because its parent pr2 was skipped" in result.stderr


def test_sync_leaves_other_stacks_alone(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.stack("branch", "solo")
    repo.commit("c", "solo\n", "solo c1")
    repo.stack("push")
    repo.git("switch", "--quiet", "pr1")
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    pr2 = repo.git("rev-parse", "pr2")
    repo.git("switch", "--quiet", "solo")

    repo.stack("sync")

    repo.assert_stacked("solo", "main", ["solo c1"])
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.git("rev-parse", "pr2") == pr2


def test_sync_refuses_outside_a_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit_on_forge("main", "d", "upstream\n", "main moved")

    result = repo.stack("sync", expect_success=False)

    assert "main isn't in a stack" in result.stderr
    assert repo.git("rev-parse", "main") != repo.git("rev-parse", "main", cwd=repo.origin)


def test_sync_leaves_diverged_trunk_alone(repo: Repo) -> None:
    repo.commit_on_forge("main", "d", "upstream\n", "upstream change")
    repo.commit("a", "local\n", "local change")
    repo.stack("branch", "pr1")
    local_main = repo.git("rev-parse", "main")

    result = repo.stack("sync")

    assert "main has diverged from origin/main" in result.stderr
    assert repo.git("rev-parse", "main") == local_main
