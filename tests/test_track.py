"""git stack track."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_track_on_tracks_the_current_branch(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "feature")
    repo.commit("a", "feature\n", "feature")

    repo.stack("track", "--on", "main")

    repo.assert_stacked("feature", "main", ["feature"])


def test_track_on_tracks_one_named_branch(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("switch", "--quiet", "main")

    repo.stack("track", "--on", "main", "feature")

    repo.assert_stacked("feature", "main", ["feature"])
    assert repo.git("branch", "--show-current") == "main"


def test_track_on_multiple_bases_records_them_without_rewriting(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("feature")
    repo.commit("b", "feature\n", "feature")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.commit("c", "second\n", "second")
    repo.git("switch", "--quiet", "feature")
    before = repo.git("rev-parse", "HEAD")

    repo.stack("track", "--on", "first", "--on", "second")

    assert repo.git("rev-parse", "HEAD") == before
    assert repo.stack("parents").stdout == "first\nsecond\n"
    assert client.stacks().dependencies("feature") == ("first", "second")


def test_track_on_multiple_bases_rejects_a_cycle_without_changing_metadata(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.branch("child")
    repo.commit("b", "child\n", "child")
    repo.git("switch", "--quiet", "feature")
    before = repo.config("feature", "stackParent")

    result = repo.stack("track", "--on", "main", "--on", "child", expect_success=False)

    assert "cycle" in result.stderr
    assert repo.config("feature", "stackParent") == before


def test_track_on_multiple_bases_tracks_an_untracked_branch(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.git("switch", "--quiet", "first")
    repo.git("switch", "--quiet", "-c", "feature")
    repo.commit("a", "feature\n", "feature")

    repo.stack("track", "--on", "first", "--on", "second")

    assert client.stacks().dependencies("feature") == ("first", "second")
    assert repo.config("feature", "stackBase") == repo.git("rev-parse", "first")


def test_track_cannot_silently_remove_a_secondary_base(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    client.branch("feature")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.git("switch", "--quiet", "feature")
    repo.stack("track", "--on", "first", "--on", "second")

    result = repo.stack("track", "main", expect_success=False)

    assert "multiple-base" in result.stderr
    assert client.stacks().dependencies("feature") == ("first", "second")


def test_track_multiple_bases_inherits_shared_ownership(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    client.branch("feature")
    client.branch("child")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.stack("ownership", "shared")
    repo.git("switch", "--quiet", "feature")

    repo.stack("track", "--on", "first", "--on", "second")

    stacks = client.stacks()
    assert stacks.is_shared("feature")
    assert stacks.is_shared("child")


def test_restack_onto_a_secondary_base_cannot_discard_dependencies(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    client.branch("feature")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.git("switch", "--quiet", "feature")
    repo.stack("track", "--on", "first", "--on", "second")
    before = repo.git("rev-parse", "feature")

    result = repo.stack("restack", "--onto", "second", expect_success=False)

    assert "multiple bases" in result.stderr
    assert client.stacks().dependencies("feature") == ("first", "second")
    assert repo.git("rev-parse", "feature") == before


def test_track_on_refuses_more_than_one_target_with_argument_error(repo: Repo) -> None:
    result = repo.stack("track", "--on", "main", "a", "b", expect_success=False)
    assert result.returncode == 2


def test_track_without_parent_is_still_an_argument_error(repo: Repo) -> None:
    assert repo.stack("track", expect_success=False).returncode == 2


def test_track_rejects_cycle(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="would create a cycle"):
        client.track("pr3")

    assert repo.config("pr1", "stackParent") == "main"


def test_track_stacks_several_existing_branches(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "-c", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("switch", "--quiet", "-c", "pr3")
    repo.commit("c", "pr3\n", "pr3 c1")
    repo.git("switch", "--quiet", "main")

    client.track("main", ["pr1", "pr2", "pr3"])

    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "main"


def test_track_uses_branches_when_tags_have_the_same_names(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "-c", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("tag", "pr1", "refs/heads/main")
    repo.git("tag", "pr2", "refs/heads/main")

    client.track("pr1", ["pr2"])

    assert repo.config("pr2", "stackBase") == repo.git("rev-parse", "refs/heads/pr1")


def test_track_several_branches_writes_nothing_if_one_is_bad(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")

    with pytest.raises(StackError, match="missing isn't a local branch"):
        client.track("main", ["pr1", "missing"])

    repo.assert_untracked("pr1")


def test_track_several_branches_rejects_cycle(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="would create a cycle"):
        client.track("pr2", ["pr3", "pr1"])

    assert repo.config("pr3", "stackParent") == "pr2"
    assert repo.config("pr1", "stackParent") == "main"


def test_track_rejects_base_outside_branch_history(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="isn't in pr1's history"):
        client.track("main", base="main")

    assert not repo.config_exists("pr1", "stackParent")


def test_track_existing_branch_after_parent_was_squash_merged(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1 first\n", "pr1 c1")
    repo.commit("a", "pr1 second\n", "pr1 c2")
    old_pr1_tip = repo.git("rev-parse", "HEAD")
    repo.git("switch", "--quiet", "-c", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("branch", "--quiet", "-D", "pr1")
    repo.git("switch", "--quiet", "pr2")

    client.track("main", base=old_pr1_tip)
    client.restack()

    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_track_later_branch_uses_where_it_forked_from_rewritten_parent(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1 v1\n", "pr1 c1")
    old_pr1_tip = repo.git("rev-parse", "HEAD")
    repo.git("switch", "--quiet", "-c", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("switch", "--quiet", "pr1")
    repo.write("a", "pr1 v2\n")
    repo.git("commit", "--quiet", "--amend", "-am", "pr1 c1 (amended)")
    repo.git("switch", "--quiet", "pr2")

    tracked = client.track("main", ["pr1", "pr2"])
    client.restack()

    assert tracked[1].base == old_pr1_tip
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


@pytest.mark.parametrize("legacy", [False, True])
def test_track_isnt_undone_by_an_earlier_interrupted_restack(repo: Repo, client: StackClient, legacy: bool) -> None:
    repo.make_stack()
    pr1_tip = repo.git("rev-parse", "pr1")
    # As if a restack of pr2 onto pr1 stopped on a conflict and the user then
    # finished the rebase by hand; or one an older version started, which
    # recorded it in three settings.
    before = repo.git("rev-parse", "pr2")
    if legacy:
        repo.git("config", "branch.pr2.stackPendingParent", "pr1")
        repo.git("config", "branch.pr2.stackPendingBase", pr1_tip)
        repo.git("config", "branch.pr2.stackPendingRestackFrom", before)
    else:
        repo.git("config", "branch.pr2.stackPendingRestack", f"{before} {pr1_tip} pr1")
    # The rebase, finished, with the message edited: otherwise it would give
    # pr2 the commit it has (pr1 hasn't moved, and the tests' dates are fixed),
    # which leaves no reflog entry.
    rebased = repo.git("commit-tree", "-p", pr1_tip, "-m", "pr2 c1, edited", f"{before}^{{tree}}")
    repo.git("update-ref", "-m", f"rebase (finish): refs/heads/pr2 onto {pr1_tip}", "refs/heads/pr2", rebased)

    client.track("main", ["pr2"], base=pr1_tip)
    client.info()

    assert repo.config("pr2", "stackParent") == "main"
    assert repo.config("pr2", "stackBase") == pr1_tip


def test_track_refuses_while_a_rebase_is_in_progress(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "conflicting\n", "main touches a")
    repo.git("switch", "--quiet", "pr1")
    with pytest.raises(StackError, match="stopped on a conflict"):
        client.restack()

    with pytest.raises(StackError, match="in progress"):
        client.track("main", ["pr2"])


@pytest.mark.parametrize("parent", ["origin/main", "HEAD~1"])
def test_track_refuses_parent_that_isnt_a_local_branch(repo: Repo, client: StackClient, parent: str) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")

    with pytest.raises(StackError, match=f"{parent} isn't a local branch"):
        client.track(parent)

    repo.assert_untracked("pr1")
