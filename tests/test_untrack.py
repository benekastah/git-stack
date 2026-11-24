"""git stack untrack."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient, Untracked
from tests.helpers import Repo


def test_untrack_current_branch_keeps_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    assert client.untrack() == [Untracked("pr3", children=())]

    repo.assert_untracked("pr3")
    assert repo.config("pr2", "stackParent") == "pr1"


def test_untrack_leaves_children_stacked_on_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("config", "branch.pr2.stackPulled", repo.git("rev-parse", "pr2"))

    assert client.untrack(["pr2"]) == [Untracked("pr2", children=("pr3",))]

    repo.assert_untracked("pr2")
    assert not repo.config_exists("pr2", "stackPulled")
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    client.restack()
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_untrack_refuses_branch_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.untrack(["main"])


def test_untrack_several_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    assert client.untrack(["pr1", "pr2"]) == [Untracked("pr1", children=()), Untracked("pr2", children=("pr3",))]

    repo.assert_untracked("pr1")
    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_untrack_changes_nothing_if_a_branch_isnt_tracked(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.untrack(["pr1", "main"])

    assert repo.config("pr1", "stackParent") == "main"
