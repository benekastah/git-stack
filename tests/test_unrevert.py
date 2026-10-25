"""git stack unrevert."""

import pytest

from git_stack import events
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


def merge_sync_revert(
    repo: Repo, client: StackClient, *, delete: str | None = None, merge_commit: bool = False
) -> None:
    """Merge pr1, sync from pr2 (untracking pr1 and moving pr2 onto main), then
    revert pr1's merge on main and sync again."""
    repo.make_stack()
    client.push()
    if merge_commit:
        repo.merge_on_forge("pr1")
    else:
        repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")
    client.sync(delete=delete)
    repo.revert_on_forge(*(("-m", "1") if merge_commit else ()), "HEAD")
    client.sync()


def test_sync_records_merged_parent_on_its_children(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    base, tip = repo.config("pr1", "stackBase"), repo.git("rev-parse", "pr1")
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")

    client.sync()

    assert repo.config("pr2", "stackMergedParent") == f"pr1 main {base} {tip}"
    assert not repo.config_exists("pr3", "stackMergedParent")


def test_sync_and_info_say_when_a_merged_parent_was_reverted(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    merge_sync_revert(repo, client)

    assert events.MergedParentReverted("pr2", "pr1", "main") in emitted
    assert repo.branch_info("pr2").reverted_parent == "pr1"
    assert repo.branch_info("pr3").reverted_parent is None
    # pr2 is still synced: only its pull request is missing pr1's changes.
    assert repo.config("pr2", "stackParent") == "main"


@pytest.mark.parametrize("delete", [None, "local"], ids=["kept", "deleted"])
def test_unrevert_recreates_untracked_branch_and_moves_its_children_back(
    repo: Repo, client: StackClient, delete: str | None
) -> None:
    merge_sync_revert(repo, client, delete=delete)
    assert bool(repo.git("branch", "--list", "pr1")) == (delete is None)

    unreverted = client.unrevert("pr1")

    assert unreverted.children == ("pr2",)
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main")
    assert repo.git("show", "pr1:a") == "pr1 second"
    assert repo.config("pr2", "stackParent") == "pr1"
    assert not repo.config_exists("pr2", "stackMergedParent")
    # pr2 is no longer at the bottom of a stack, so pr1 holds its group.
    assert repo.config("pr1", "stackGroup") == "pr1"
    assert not repo.config_exists("pr2", "stackGroup")
    client.restack()
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_unrevert_recreates_untracked_branch_merged_with_a_merge_commit(repo: Repo, client: StackClient) -> None:
    merge_sync_revert(repo, client, merge_commit=True)

    client.unrevert("pr1")

    assert repo.git("show", "pr1:a") == "pr1 second"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_unrevert_recreates_deleted_branch_whose_pull_request_merge_was_tweaked(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("switch", "--quiet", "pr2")
    client.sync(delete="local")
    repo.revert_on_forge("HEAD")
    client.sync()
    assert events.MergedParentReverted("pr2", "pr1", "main") in emitted

    client.unrevert("pr1")

    assert repo.git("show", "pr1:a") == "tweaked by the maintainer"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_unrevert_leaves_untracked_branch_alone_if_it_changed(repo: Repo, client: StackClient) -> None:
    merge_sync_revert(repo, client)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("c", "later\n", "pr1 later")
    pr1 = repo.git("rev-parse", "pr1")

    with pytest.raises(StackError, match="pr1 has changed since it merged"):
        client.unrevert("pr1")

    assert repo.git("rev-parse", "pr1") == pr1
    assert not repo.config_exists("pr1", "stackParent")
    assert repo.config("pr2", "stackParent") == "main"


def test_unrevert_refuses_untracked_branch_whose_commits_are_gone(repo: Repo, client: StackClient) -> None:
    merge_sync_revert(repo, client, delete="local")
    recorded = repo.config("pr2", "stackMergedParent").split()
    recorded[3] = "0123456789abcdef0123456789abcdef01234567"
    repo.git("config", "branch.pr2.stackMergedParent", " ".join(recorded))

    with pytest.raises(StackError, match="pr1's commits are no longer in the repository"):
        client.unrevert("pr1")

    assert not repo.git("branch", "--list", "pr1")


def test_unrevert_refuses_untracked_branch_that_isnt_reverted(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")
    client.sync()

    with pytest.raises(StackError, match="pr1 doesn't look reverted in main"):
        client.unrevert("pr1")

    assert not repo.config_exists("pr1", "stackParent")


def test_unrevert_refuses_branch_nothing_was_stacked_on(repo: Repo, client: StackClient) -> None:
    repo.git("branch", "loose")

    with pytest.raises(StackError, match="loose isn't in a stack, and no branch in one was stacked on it"):
        client.unrevert("loose")


def merged_tip_refs(repo: Repo) -> list[str]:
    return repo.git("for-each-ref", "--format=%(refname)", "refs/stack/merged/").split()


def test_recorded_tip_of_deleted_branch_survives_garbage_collection(repo: Repo, client: StackClient) -> None:
    merge_sync_revert(repo, client, delete="local")
    pr1 = repo.config("pr2", "stackMergedParent").split()[3]
    assert merged_tip_refs(repo) == [f"refs/stack/merged/{pr1}"]

    repo.git("reflog", "expire", "--expire=now", "--all")
    repo.git("gc", "--quiet", "--prune=now")

    assert repo.branch_info("pr2").reverted_parent == "pr1"
    client.unrevert("pr1")
    assert repo.git("show", "pr1:a") == "pr1 second"
    # No branch records pr1's old tip any more.
    assert merged_tip_refs(repo) == []


def test_recorded_tip_ref_goes_once_no_branch_records_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")
    client.sync(delete="local")
    assert len(merged_tip_refs(repo)) == 1

    client.untrack(["pr2"])

    assert merged_tip_refs(repo) == []
