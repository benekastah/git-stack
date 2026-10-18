"""git stack track."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


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


def test_track_isnt_undone_by_an_earlier_interrupted_restack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    pr1_tip = repo.git("rev-parse", "pr1")
    # As if a restack of pr2 onto pr1 stopped on a conflict and the user then
    # finished the rebase by hand.
    repo.git("config", "branch.pr2.stackPendingParent", "pr1")
    repo.git("config", "branch.pr2.stackPendingBase", pr1_tip)

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
