"""git stack unrevert."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def merge_and_revert_pr1(repo: Repo, client: StackClient) -> str:
    """Squash-merge pr1, revert it on main, and sync from pr2, which leaves
    pr1 (and pr2 on it) alone as reverted. Returns the revert."""
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    repo.git("switch", "--quiet", "pr2")
    client.sync()
    return repo.git("rev-parse", "main")


def test_unrevert_brings_back_a_branch_and_moves_the_stack_onto_it(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    revert = merge_and_revert_pr1(repo, client)

    unreverted = client.unrevert("pr1-unrevert", [revert], stack=["pr2"])

    assert unreverted.parent == "main"
    assert unreverted.reverts == (revert,)
    assert repo.config("pr1-unrevert", "stackParent") == "main"
    assert len(repo.own_commits("pr1-unrevert", "main")) == 1
    assert repo.git("show", "pr1-unrevert:a") == "pr1 second"
    repo.assert_stacked("pr2", "pr1-unrevert", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    # pr2 left it, and nothing else is stacked on it.
    repo.assert_untracked("pr1")
    assert events.Untracked("pr1", "main", (), deleted_tip=None) in emitted
    assert repo.git("branch", "--show-current") == "pr1-unrevert"


def test_unrevert_requires_override_for_a_shared_stack(repo: Repo, client: StackClient) -> None:
    revert = merge_and_revert_pr1(repo, client)
    client.set_ownership("shared")
    before = repo.git("rev-parse", "refs/heads/pr2")

    with pytest.raises(StackError, match="unrevert --allow-shared"):
        client.unrevert("restore", [revert], stack=["pr2"])
    assert not client.git.local_branch_exists("restore")
    assert repo.git("rev-parse", "refs/heads/pr2") == before

    client.unrevert("restore", [revert], stack=["pr2"], allow_shared=True)
    assert repo.git("rev-parse", "refs/heads/pr2") != before


def test_unrevert_after_sync_moved_the_stack_onto_main(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")
    client.sync()
    repo.revert_on_forge("HEAD")
    # The revert isn't noticed, since pr1 isn't tracked any more.
    client.sync()

    client.unrevert("pr1-unrevert", ["main"], stack=["pr2"])

    assert repo.git("show", "pr1-unrevert:a") == "pr1 second"
    repo.assert_stacked("pr2", "pr1-unrevert", ["pr2 c1"])
    # It took pr2's place at the bottom of the stack, in pr2's group.
    assert repo.config("pr1-unrevert", "stackGroup") == "pr1"
    assert not repo.config_exists("pr2", "stackGroup")


def test_unrevert_reverts_reverts_newest_first(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.merge_on_forge("pr1")
    # Each reverts one of pr1's commits, which both change a.
    repo.revert_on_forge(repo.git("rev-parse", "pr1"))
    repo.revert_on_forge(repo.git("rev-parse", "pr1~1"))
    client.clean()
    newer, older = repo.git("rev-parse", "main"), repo.git("rev-parse", "main~1")

    unreverted = client.unrevert("pr1-unrevert", [older, newer])

    assert unreverted.reverts == (newer, older)
    assert repo.git("show", "pr1-unrevert:a") == "pr1 second"


def test_unrevert_builds_on_the_remotes_default_branch_without_a_stack(repo: Repo, client: StackClient) -> None:
    revert = merge_and_revert_pr1(repo, client)
    repo.git("remote", "set-head", "origin", "main")

    assert client.unrevert("pr1-unrevert", [revert]).parent == "main"


def test_unrevert_needs_from_if_the_remotes_default_branch_isnt_known(repo: Repo, client: StackClient) -> None:
    revert = merge_and_revert_pr1(repo, client)
    repo.git("remote", "set-head", "origin", "--delete")

    with pytest.raises(StackError, match="origin's default branch isn't known; pass --from"):
        client.unrevert("pr1-unrevert", [revert])

    assert client.unrevert("pr1-unrevert", [revert], from_branch="main").parent == "main"


def test_unrevert_refuses_a_revert_that_isnt_on_the_branch_it_builds_on(repo: Repo, client: StackClient) -> None:
    merge_and_revert_pr1(repo, client)

    with pytest.raises(StackError, match="pr2 isn't in main's history"):
        client.unrevert("pr1-unrevert", ["pr2"], stack=["pr2"])

    assert not repo.git("branch", "--list", "pr1-unrevert")


def test_unrevert_refuses_a_name_thats_taken(repo: Repo, client: StackClient) -> None:
    revert = merge_and_revert_pr1(repo, client)

    with pytest.raises(StackError, match="pr1 already exists"):
        client.unrevert("pr1", [revert])


def test_unrevert_stopped_on_conflict_is_finished_by_restack(repo: Repo, client: StackClient) -> None:
    revert = merge_and_revert_pr1(repo, client)
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main again\n", "main edits a")

    with pytest.raises(StackError, match=r"unreverting stopped.*`git stack restack` to move pr2 onto pr1-unrevert"):
        client.unrevert("pr1-unrevert", [revert], stack=["pr2"])
    repo.write("a", "pr1 second\n")
    repo.git("add", "a")
    repo.git("revert", "--continue")
    client.restack()

    assert repo.git("show", "pr1-unrevert:a") == "pr1 second"
    repo.assert_stacked("pr2", "pr1-unrevert", ["pr2 c1"])
