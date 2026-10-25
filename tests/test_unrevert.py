"""git stack unrevert."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import BranchStatus, PullRequestAction, StackClient
from tests.helpers import FakeGitHub, Repo
from tests.test_github import merge_pr1_on_forge_with_a_tweak


def sync_main(repo: Repo) -> None:
    """Bring the local main up to date with origin's, as sync does."""
    repo.git("switch", "--quiet", "main")
    repo.git("pull", "--quiet", "--prune")


def test_unrevert_rebuilds_reverted_branch_on_its_parent(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    sync_main(repo)
    revert = repo.git("rev-parse", "main")

    unreverted = client.unrevert("pr1")

    assert unreverted.reverts == (revert,)
    assert unreverted.children == ("pr2",)
    assert len(repo.own_commits("pr1", "main")) == 1
    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main")
    assert repo.git("show", "pr1:a") == "pr1 second"
    assert repo.git("branch", "--show-current") == "pr1"
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    # Its remote branch was deleted when it was merged, so push can push it again.
    assert not repo.config_exists("pr1", "merge")

    repo.git("switch", "--quiet", "pr2")
    client.sync()
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    client.push()
    assert repo.git("rev-parse", "origin/pr1") == repo.git("rev-parse", "pr1")


def test_unrevert_reverts_only_reverts_that_are_still_in_effect(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.commit("b", "pr1\n", "pr1 c2")
    client.push()
    repo.merge_on_forge("pr1")
    repo.revert_on_forge(repo.git("rev-parse", "pr1~1"))
    repo.revert_on_forge(repo.git("rev-parse", "pr1"))
    repo.revert_on_forge("HEAD")
    sync_main(repo)
    c1_revert = repo.git("rev-parse", "main~2")

    unreverted = client.unrevert("pr1")

    assert unreverted.reverts == (c1_revert,)
    assert repo.git("show", "pr1:a") == "pr1"
    assert repo.git("show", "pr1:b") == "pr1"


def test_unrevert_reverts_reverted_pull_request_merge(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.revert_on_forge("HEAD")
    sync_main(repo)

    client.unrevert("pr1")

    assert repo.git("show", "pr1:a") == "tweaked by the maintainer"
    # The merged pull request is no longer pr1's, so it doesn't make pr1 look
    # merged, and prs opens a new one.
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    client.restack()
    client.push()
    first = client.prs()[0]
    assert (first.branch, first.action) == ("pr1", PullRequestAction.OPEN)


def test_unrevert_refuses_branch_that_isnt_reverted(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    pr1 = repo.git("rev-parse", "pr1")

    with pytest.raises(StackError, match=r"pr1 doesn't look reverted in main \(it's not merged\)"):
        client.unrevert("pr1")

    assert repo.git("rev-parse", "pr1") == pr1


def conflicting_unrevert(repo: Repo, client: StackClient) -> str:
    """Revert pr1's merge, then change a on main, so unreverting pr1 stops on a
    conflict. Returns pr1's tip before it."""
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    sync_main(repo)
    repo.commit("a", "main again\n", "main edits a")
    old_tip = repo.git("rev-parse", "pr1")
    with pytest.raises(StackError, match="unreverting pr1 stopped"):
        client.unrevert("pr1")
    return old_tip


def test_unrevert_stopped_on_conflict_is_recorded_once_continued(repo: Repo, client: StackClient) -> None:
    conflicting_unrevert(repo, client)
    with pytest.raises(StackError, match="a rebase, cherry-pick, revert or merge is in progress"):
        client.restack()
    repo.write("a", "pr1 second\n")
    repo.git("add", "a")
    repo.git("revert", "--continue")

    client.restack()

    assert len(repo.own_commits("pr1", "main")) == 1
    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main")
    assert repo.git("show", "pr1:a") == "pr1 second"
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])


def test_unrevert_stopped_on_conflict_can_be_undone(repo: Repo, client: StackClient) -> None:
    old_tip = conflicting_unrevert(repo, client)
    old_base = repo.config("pr1", "stackBase")
    repo.git("revert", "--abort")
    repo.git("reset", "--quiet", "--hard", old_tip)

    client.settle_all()

    assert repo.config("pr1", "stackBase") == old_base
    assert repo.branch_info("pr1").status is BranchStatus.REVERTED
