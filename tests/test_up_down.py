"""git stack up and down."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_up_switches_to_child(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    assert client.up() == "pr2"

    assert repo.git("branch", "--show-current") == "pr2"


def test_up_from_stack_root(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    assert client.up() == "pr1"


def test_up_refuses_to_choose_between_children(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="several branches are stacked on pr1: pr2, pr2b"):
        client.up()

    assert repo.git("branch", "--show-current") == "pr1"


def test_up_refuses_at_top_of_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="no branch is stacked on pr3"):
        client.up()


def test_down_switches_to_parent(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    assert client.down() == "pr2"

    assert repo.git("branch", "--show-current") == "pr2"


def test_down_from_bottom_switches_to_root(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    assert client.down() == "main"


def test_down_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    with pytest.raises(StackError, match="main isn't in a stack; there's no branch below it"):
        client.down()


def test_switch_failure_is_reported(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.write("c", "uncommitted, conflicts with pr2\n")

    with pytest.raises(StackError, match="switching to pr2 failed"):
        client.down()

    assert repo.git("branch", "--show-current") == "pr3"
