"""Owned and shared stacks."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


@pytest.mark.parametrize("command", ["restack", "sync", "push"])
def test_shared_stack_requires_override(repo: Repo, client: StackClient, command: str) -> None:
    repo.make_stack()
    client.set_ownership("shared")
    assert client.get_ownership()[0] == "shared"
    before = repo.git("rev-parse", "HEAD")

    with pytest.raises(StackError, match=rf"git stack {command} --allow-shared"):
        getattr(client, command)()

    assert repo.git("rev-parse", "HEAD") == before


def test_shared_stack_can_be_marked_owned_and_restacked(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership("shared")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "review\n", "review")
    repo.git("switch", "--quiet", "pr3")

    client.restack(allow_shared=True)
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    client.set_ownership("owned")
    assert client.get_ownership()[0] == "owned"
    client.restack()


def test_new_branch_inherits_shared_ownership(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership("shared")
    client.branch("pr4")
    client.untrack(["pr3"])

    with pytest.raises(StackError, match="shared stack"):
        client.restack()
