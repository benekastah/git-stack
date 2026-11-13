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


def test_shared_ownership_survives_joining_and_splitting_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership("owned")
    repo.git("switch", "--quiet", "main")
    client.branch("shared")
    repo.commit("a", "shared\n", "shared")
    client.set_ownership("shared")
    client.track("shared", ["pr1"])
    client.untrack(["pr1"])

    assert client.get_ownership(["pr2"])[0] == "shared"
    with pytest.raises(StackError, match="--allow-shared"):
        client.restack(["pr2"])


def test_clean_requires_override_to_delete_shared_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership("shared")
    client.push(allow_shared=True)
    repo.squash_merge_on_forge("pr1", delete_branch=False)

    with pytest.raises(StackError, match="clean --allow-shared"):
        client.clean(delete="both")
    assert client.git.local_branch_exists("pr1")
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin)

    client.clean(delete="both", allow_shared=True)
    assert not client.git.local_branch_exists("pr1")
    assert not repo.git("branch", "--list", "pr1", cwd=repo.origin)
