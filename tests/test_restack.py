"""git stack restack."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from git_stack.stacks import Ownership, StackNode, Stacks
from tests.helpers import Repo


def test_restack_moves_whole_stack_after_parent_changes(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"
    assert all(branch.status is not BranchStatus.NEEDS_RESTACK for branch in client.info())


def test_restack_all_orders_parents_before_children(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")

    client.restack_all(["pr3", "pr2"], client.stacks())

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_multiple_onto_values_refuses_before_changing_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    before = {branch: repo.git("rev-parse", f"refs/heads/{branch}") for branch in ("pr1", "pr2", "pr3")}

    result = repo.stack("restack", "--onto", "pr1", "--onto", "pr2", expect_success=False)

    assert "multiple bases" in result.stderr
    assert {branch: repo.git("rev-parse", f"refs/heads/{branch}") for branch in before} == before
    assert repo.config("pr3", "stackParent") == "pr2"


def test_restack_when_secondary_base_is_already_in_the_primary_history(repo: Repo, client: StackClient) -> None:
    client.branch("second")
    repo.commit("a", "second\n", "second")
    client.branch("first")
    repo.commit("b", "first\n", "first")
    client.branch("combined")
    repo.commit("c", "combined\n", "combined")
    repo.stack("track", "--on", "first", "--on", "second")
    repo.git("switch", "--quiet", "first")
    repo.commit("a", "more\n", "more")
    first = repo.git("rev-parse", "first")
    second = repo.git("rev-parse", "second")
    repo.git("switch", "--quiet", "combined")

    client.restack()

    assert repo.git("rev-parse", "first") == first
    assert repo.git("rev-parse", "second") == second
    assert client.stacks().dependencies("combined") == ("first", "second")
    assert repo.git("log", "--format=%s", "first..combined") == "combined"
    assert repo.git("merge-base", "--is-ancestor", "second", "combined") == ""


def test_restack_refuses_an_unincorporated_secondary_base_before_rewriting(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("combined")
    repo.commit("c", "combined\n", "combined")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.commit("a", "second\n", "second")
    repo.git("switch", "--quiet", "combined")
    repo.stack("track", "--on", "first", "--on", "second")
    before = {b: repo.git("rev-parse", b) for b in ("first", "second", "combined")}

    with pytest.raises(StackError, match="multiple bases.*not supported"):
        client.restack()

    assert {b: repo.git("rev-parse", b) for b in before} == before


def test_restack_refuses_multiple_bases_before_changing_any_branch(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stacks = Stacks(
        [
            StackNode("first", "main"),
            StackNode("second", "main"),
            StackNode("combined", "first", additional_parents=("second",)),
        ]
    )
    attempted: list[str] = []

    def restack_branch(branch: str, parent: str, latest: bool = False) -> bool:
        attempted.append(branch)
        return True

    monkeypatch.setattr(client, "restack_branch", restack_branch)
    with pytest.raises(StackError, match="multiple bases.*not supported"):
        client.restack_all(["combined", "first", "second"], stacks)

    assert attempted == []


def test_restack_refuses_to_drop_local_merge_changes(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1")
    repo.git("switch", "--quiet", "-c", "side")
    repo.commit("b", "side\n", "side")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("c", "other\n", "other")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    before = repo.git("rev-parse", "pr1")
    repo.git("switch", "--quiet", "main")
    repo.write("d", "main\n")
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "main")
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="contains merge commits"):
        client.restack(latest=True)
    assert repo.git("rev-parse", "pr1") == before


def test_info_uses_parent_branch_when_a_tag_has_the_same_name(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("tag", "pr1", "refs/heads/main")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "review fix\n", "review fix")

    statuses = {branch.name: branch.status for branch in client.info(offline=True)}

    assert statuses["pr2"] is BranchStatus.NEEDS_RESTACK


def test_restack_uses_branches_when_tags_have_the_same_names(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("tag", "pr1", "refs/heads/main")
    repo.git("tag", "pr2", "refs/heads/main")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "review fix\n", "review fix")
    repo.git("switch", "--quiet", "pr3")

    client.restack()

    assert repo.config("pr2", "stackBase") == repo.git("rev-parse", "refs/heads/pr1")


def test_restack_survives_scripting_command_run_before_each_rebase(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")
    # As a shell prompt would, while restack has written a branch's pending
    # parent and base but its rebase hasn't started yet.
    prompt = StackClient(GitClient(repo.path))
    passthrough = client.git.passthrough

    def run_prompt_first(*args: str) -> bool:
        if args[0] == "rebase":
            prompt.parent(args[-1])
        return passthrough(*args)

    monkeypatch.setattr(client.git, "passthrough", run_prompt_first)

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_onto_retargets_after_local_squash_merge(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    client.restack(onto="main")

    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_onto_does_not_rewrite_destination(repo: Repo, client: StackClient) -> None:
    client.branch("target")
    repo.commit("b", "target\n", "target")
    before = repo.git("rev-parse", "target")
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main moved\n", "main")
    client.branch("moving")
    repo.commit("a", "moving\n", "moving")
    client.restack(onto="target", latest=True)
    assert repo.git("rev-parse", "target") == before
    assert repo.config("moving", "stackParent") == "target"


def test_restack_onto_requires_override_for_shared_source_group(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.OWNED)
    repo.git("switch", "--quiet", "main")
    client.branch("shared")
    client.set_ownership(Ownership.SHARED)
    client.group_add("feature", ["pr1", "shared"])
    repo.git("switch", "--quiet", "main")
    client.branch("target")
    repo.git("switch", "--quiet", "pr1")
    before = repo.git("rev-parse", "pr1")
    with pytest.raises(StackError, match="--allow-shared"):
        client.restack(onto="target")
    assert repo.git("rev-parse", "pr1") == before
    assert repo.config("pr1", "stackParent") == "main"


def test_restack_checks_ownership_before_skipping_merged_branches(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("config", "branch.pr1.stackOwnership", "shared")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "merge pr1")
    repo.git("switch", "--quiet", "pr2")
    before = repo.git("rev-parse", "pr2")

    with pytest.raises(StackError, match="--allow-shared"):
        client.restack()
    assert repo.git("rev-parse", "pr2") == before


def test_restack_skips_branch_merged_locally(repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")

    assert repo.branch_info("pr1").status is BranchStatus.MERGED
    repo.git("switch", "--quiet", "pr2")
    client.restack()

    assert events.SkippedMerged("pr1", "main") in emitted
    assert repo.git("branch", "--list", "pr1") == "pr1"
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_restack_leaves_reverted_branch_and_its_children_alone(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.git("merge", "--quiet", "--no-ff", "--no-edit", "pr2")
    repo.git("revert", "--no-edit", "-m", "1", "HEAD")
    pr2 = repo.git("rev-parse", "pr2")
    repo.git("switch", "--quiet", "pr3")

    client.restack()

    assert events.Reverted("pr2", "pr1", (repo.git("rev-parse", "pr1"),), ("pr3",)) in emitted
    assert events.ParentSkipped("pr3", "pr2") in emitted
    # Rebasing pr2 onto pr1 would drop its commit, which pr1 already has.
    assert repo.git("rev-parse", "pr2") == pr2
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_conflict_resolved_with_rebase_continue_records_new_base(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 change\n", "pr1 touches b")

    # pr2 conflicts and pr3 is left for later.
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()

    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")
    assert repo.branch_info("pr2") == BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=True)
    client.restack()

    repo.assert_stacked("pr1", "main", ["pr1 touches b", "pr1 c2", "pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_conflict_from_an_older_version_is_finished_by_this_one(repo: Repo, client: StackClient) -> None:
    """Older versions recorded a restack in three settings, which one that
    stopped on a conflict before upgrading leaves behind."""
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 change\n", "pr1 touches b")
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()
    before, base, parent = repo.config("pr2", "stackPendingRestack").split(" ")
    repo.git("config", "--unset", "branch.pr2.stackPendingRestack")
    for key, value in (("RestackFrom", before), ("Base", base), ("Parent", parent)):
        repo.git("config", f"branch.pr2.stackPending{key}", value)

    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")
    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.config("pr2", "stackBase") == repo.git("rev-parse", "pr1")
    assert not any(
        key.startswith("branch.pr2.stackpending") for key in repo.git("config", "--list", "--name-only").split()
    )


def test_conflict_from_an_older_version_that_was_aborted_is_forgotten(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    old_base = repo.config("pr2", "stackBase")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 change\n", "pr1 touches b")
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()
    before, base, parent = repo.config("pr2", "stackPendingRestack").split(" ")
    repo.git("config", "--unset", "branch.pr2.stackPendingRestack")
    for key, value in (("RestackFrom", before), ("Base", base), ("Parent", parent)):
        repo.git("config", f"branch.pr2.stackPending{key}", value)
    repo.git("rebase", "--abort")
    # So restacking doesn't stop on the same conflict again.
    repo.git("switch", "--quiet", "pr1")
    repo.git("reset", "--quiet", "--hard", "HEAD~1")

    client.restack()

    assert repo.config("pr2", "stackBase") == old_base
    assert not any(
        key.startswith("branch.pr2.stackpending") for key in repo.git("config", "--list", "--name-only").split()
    )


def test_restack_stopped_by_untracked_file_is_not_reported_as_conflict(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    repo.write("new", "pr2\n")
    repo.git("add", "new")
    repo.git("commit", "--quiet", "-m", "pr2 adds new")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.write("new", "untracked\n")

    with pytest.raises(StackError, match="restacking pr2 stopped without a conflict") as failure:
        client.restack()

    assert "untracked" in str(failure.value)
    (repo.path / "new").unlink()
    repo.git("rebase", "--continue")
    client.restack()
    repo.assert_stacked("pr2", "pr1", ["pr2 adds new", "pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_aborted_restack_keeps_previous_base(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    old_base = repo.config("pr1", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main change\n", "main touches a")
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError):
        client.restack()
    repo.git("rebase", "--abort")

    assert repo.branch_info("pr1") == BranchInfo("pr1", "main", BranchStatus.NEEDS_RESTACK, current=True)
    assert repo.config("pr1", "stackBase") == old_base


@pytest.mark.parametrize("commit_after_abort", [False, True])
def test_aborted_restack_onto_ancestor_keeps_parent_and_base(
    repo: Repo, client: StackClient, commit_after_abort: bool
) -> None:
    client.branch("parent")
    repo.commit("a", "parent\n", "parent")
    client.branch("child")
    repo.commit("a", "child\n", "child")
    old_base = repo.config("child", "stackBase")

    with pytest.raises(StackError, match="stopped on a conflict"):
        client.restack(onto="main")
    repo.git("rebase", "--abort")
    if commit_after_abort:
        repo.commit("b", "more work\n", "more work")
    client.info(offline=True)

    assert repo.config("child", "stackParent") == "parent"
    assert repo.config("child", "stackBase") == old_base


def test_manually_completed_restack_onto_ancestor_records_parent(repo: Repo, client: StackClient) -> None:
    client.branch("parent")
    repo.commit("a", "parent\n", "parent")
    client.branch("child")
    repo.commit("a", "child\n", "child")

    with pytest.raises(StackError, match="stopped on a conflict"):
        client.restack(onto="main")
    repo.write("a", "child\n")
    repo.git("add", "a")
    repo.git("rebase", "--continue")
    client.info(offline=True)

    assert repo.config("child", "stackParent") == "main"
    assert repo.config("child", "stackBase") == repo.git("rev-parse", "refs/heads/main")


def test_aborted_restack_onto_keeps_previous_parent(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    old_base = repo.config("pr2", "stackBase")
    repo.git("switch", "--quiet", "main")
    repo.commit("b", "main change\n", "main touches b")
    repo.git("switch", "--quiet", "pr2")

    with pytest.raises(StackError):
        client.restack(onto="main")
    repo.git("rebase", "--abort")

    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.config("pr2", "stackBase") == old_base


def test_restack_onto_refuses_parent_that_isnt_a_local_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    with pytest.raises(StackError, match="origin/main isn't a local branch"):
        client.restack(onto="origin/main")

    assert repo.config("pr2", "stackParent") == "pr1"


@pytest.mark.parametrize("target", ["pr3", "pr1"])
def test_restack_onto_rejects_cycle(repo: Repo, target: str, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError, match="would create a cycle"):
        client.restack(onto=target)

    assert repo.config("pr1", "stackParent") == "main"


def test_restack_failure_without_conflict_is_not_reported_as_conflict(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.add_hook(repo.path, "pre-rebase", "#!/bin/sh\nexit 1\n")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    with pytest.raises(StackError, match="restacking pr2 onto pr1 failed") as failure:
        client.restack()

    assert "conflict" not in str(failure.value)
    assert not repo.config_exists("pr2", "stackPendingRestack")


def test_restack_onto_suggests_tracking_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "loose")

    with pytest.raises(StackError, match="loose isn't in a stack; use `git stack track` first"):
        client.restack(onto="main")


def test_restack_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main\n", "main moved")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.restack()

    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main~")
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]
