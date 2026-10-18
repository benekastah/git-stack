"""git stack restack."""

import pytest

from tests.helpers import Repo


def test_restack_moves_whole_stack_after_parent_changes(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")

    repo.stack("restack")

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"
    assert "needs restack" not in repo.stack("info").stdout


def test_restack_onto_retargets_after_local_squash_merge(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    repo.stack("restack", "--onto", "main")

    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_skips_branch_merged_locally(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")

    assert "pr1 (merged)" in repo.stack("info").stdout
    repo.git("switch", "--quiet", "pr2")
    result = repo.stack("restack")

    assert "pr1 looks merged into main" in result.stderr
    assert repo.git("branch", "--list", "pr1") == "pr1"
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_conflict_resolved_with_rebase_continue_records_new_base(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr1")

    # pr1 restacks cleanly; pr2 conflicts and pr3 is left for later.
    result = repo.stack("restack", expect_success=False)
    assert "restacking pr2 stopped on a conflict" in result.stderr

    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")
    assert "pr2 *\n" in repo.stack("info").stdout
    repo.stack("restack")

    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_aborted_restack_keeps_previous_base(repo: Repo) -> None:
    repo.make_stack()
    old_base = repo.config("pr1", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main change\n", "main touches a")
    repo.git("switch", "--quiet", "pr1")

    repo.stack("restack", expect_success=False)
    repo.git("rebase", "--abort")

    assert "pr1 * (needs restack)" in repo.stack("info").stdout
    assert repo.config("pr1", "stackBase") == old_base


def test_aborted_restack_onto_keeps_previous_parent(repo: Repo) -> None:
    repo.make_stack()
    old_base = repo.config("pr2", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr2")

    repo.stack("restack", "--onto", "main", expect_success=False)
    repo.git("rebase", "--abort")

    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.config("pr2", "stackBase") == old_base


@pytest.mark.parametrize("target", ["pr3", "pr1"])
def test_restack_onto_rejects_cycle(repo: Repo, target: str) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    result = repo.stack("restack", "--onto", target, expect_success=False)

    assert "would create a cycle" in result.stderr
    assert repo.config("pr1", "stackParent") == "main"


def test_restack_failure_without_conflict_is_not_reported_as_conflict(repo: Repo) -> None:
    repo.make_stack()
    repo.git("worktree", "add", "--quiet", str(repo.tmp / "wt"), "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    result = repo.stack("restack", expect_success=False)

    assert "restacking pr2 onto pr1 failed" in result.stderr
    assert "conflict" not in result.stderr
    assert not repo.config_exists("pr2", "stackPendingParent")


def test_restack_squash_squashes_each_branch_in_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "pr2 second\n", "pr2 c2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.git("switch", "--quiet", "pr2")

    repo.stack("restack", "--squash")

    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("log", "-1", "--format=%B", "pr1") == "pr1 c1\n\npr1 c2\n\npr1 c3"
    assert repo.git("log", "-1", "--format=%B", "pr2") == "pr2 c1\n\npr2 c2"
    assert [repo.git("show", f"pr3:{name}") for name in "abc"] == ["pr1 third", "pr2 second", "pr3"]
    assert repo.git("branch", "--show-current") == "pr2"


def test_restack_squash_resolves_a_line_edited_by_every_commit_once(repo: Repo) -> None:
    repo.stack("branch", "pr1")
    repo.commit("a", "function(A, B)\n", "pr1 c1")
    repo.stack("branch", "pr2")
    repo.commit("a", "function(A, B, D)\n", "pr2 c1")
    repo.commit("a", "function(A, B, E)\n", "pr2 c2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "function(A, F)\n", "pr1 c2")
    repo.git("switch", "--quiet", "pr2")

    result = repo.stack("restack", "--squash", expect_success=False)
    assert "restacking pr2 stopped on a conflict" in result.stderr
    repo.write("a", "function(A, F, E)\n")
    repo.git("add", "a")
    # The only conflict: continuing finishes the rebase.
    repo.git("rebase", "--continue")
    repo.stack("restack")

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    assert repo.git("show", "pr2:a") == "function(A, F, E)"


def test_restack_onto_suggests_tracking_untracked_branch(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "loose")

    result = repo.stack("restack", "--onto", "main", expect_success=False)

    assert "loose isn't in a stack; use `git stack track` first" in result.stderr


def test_restack_refuses_outside_a_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main\n", "main moved")

    result = repo.stack("restack", expect_success=False)

    assert "main isn't in a stack" in result.stderr
    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main~")
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]
