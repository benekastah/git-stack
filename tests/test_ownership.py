"""Owned and shared stacks."""

import pytest

from git_stack.core import Delete
from git_stack.errors import StackError
from git_stack.stack import StackClient
from git_stack.stacks import Ownership
from tests.helpers import Repo


@pytest.mark.parametrize("selector", ["first", "second", "combined", "child"])
def test_ownership_changes_the_whole_connected_graph(repo: Repo, client: StackClient, selector: str) -> None:
    client.branch("first")
    client.branch("combined")
    client.branch("child")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.git("switch", "--quiet", "combined")
    repo.stack("track", "--on", "first", "--on", "second")

    assert set(client.set_ownership(Ownership.SHARED, [selector])) == {"first", "second", "combined", "child"}
    assert all(client.stacks().is_shared(branch) for branch in ("first", "second", "combined", "child"))
    client.set_ownership(Ownership.OWNED, [selector])
    assert all(not client.stacks().is_shared(branch) for branch in ("first", "second", "combined", "child"))


@pytest.mark.parametrize("command", ["restack", "sync", "push"])
def test_shared_stack_requires_override(repo: Repo, client: StackClient, command: str) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.SHARED)
    assert client.get_ownership()[0] is Ownership.SHARED
    before = repo.git("rev-parse", "HEAD")

    with pytest.raises(StackError, match=rf"git stack {command} --allow-shared"):
        getattr(client, command)()

    assert repo.git("rev-parse", "HEAD") == before


def test_shared_stack_can_be_marked_owned_and_restacked(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.SHARED)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "review\n", "review")
    repo.git("switch", "--quiet", "pr3")

    client.restack(allow_shared=True)
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    client.set_ownership(Ownership.OWNED)
    assert client.get_ownership()[0] is Ownership.OWNED
    client.restack()


def test_new_branch_inherits_shared_ownership(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.SHARED)
    client.branch("pr4")
    client.untrack(["pr3"])

    with pytest.raises(StackError, match="shared stack"):
        client.restack()


def test_shared_ownership_survives_joining_and_splitting_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.OWNED)
    repo.git("switch", "--quiet", "main")
    client.branch("shared")
    repo.commit("a", "shared\n", "shared")
    client.set_ownership(Ownership.SHARED)
    client.track("shared", ["pr1"])
    client.untrack(["pr1"])

    assert client.get_ownership(["pr2"])[0] is Ownership.SHARED
    with pytest.raises(StackError, match="--allow-shared"):
        client.restack(["pr2"])


def test_clean_requires_override_to_delete_shared_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.SHARED)
    client.push(allow_shared=True)
    repo.merge_on_forge("pr1", delete_branch=False)

    with pytest.raises(StackError, match="clean --allow-shared"):
        client.clean(delete=Delete.BOTH)
    assert client.git.local_branch_exists("pr1")
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin)

    client.clean(delete=Delete.BOTH, allow_shared=True)
    assert not client.git.local_branch_exists("pr1")
    assert not repo.git("branch", "--list", "pr1", cwd=repo.origin)


def test_an_unknown_ownership_setting_is_refused_with_how_to_fix_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("config", "branch.pr2.stackOwnership", "mine")

    with pytest.raises(StackError, match="branch.pr2.stackOwnership must be owned or shared, not mine"):
        client.info()
