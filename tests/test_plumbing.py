"""git stack parent, base and show-stack."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_parent_of_current_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    assert client.parent() == "pr2"


def test_parent_of_named_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    assert client.parent("pr1") == "main"


def test_parent_refuses_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.parent("main")


def test_base_is_where_branch_was_created(repo: Repo, client: StackClient) -> None:
    main = repo.git("rev-parse", "main")
    repo.make_stack()

    assert client.base("pr1") == main


def test_base_follows_restack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    client.restack()

    assert client.base("pr2") == repo.git("rev-parse", "pr1")


def test_base_picks_up_restack_finished_by_hand(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 conflicts with pr2\n", "pr1 c3")
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()
    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("-c", "core.editor=true", "rebase", "--continue")

    assert client.base("pr2") == repo.git("rev-parse", "pr1")


def test_show_stack_lists_whole_stack_bottom_first(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")

    assert client.show_stack() == ["pr1", "pr2", "pr3", "pr2b"]
    assert client.show_stack("pr3") == ["pr1", "pr2", "pr3", "pr2b"]


def test_show_stack_refuses_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.show_stack()
