"""git stack restack."""

import subprocess

import pytest
from git_client.testing import GitCall, real_git

from git_stack import events
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from git_stack.stacks import Ownership
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


@pytest.mark.parametrize("command", ["restack", "pull", "sync"])
def test_restack_suggests_recording_the_fork_when_only_trunk_has_merges(repo: Repo, command: str) -> None:
    old_base = repo.git("rev-parse", "main")
    repo.git("switch", "-qc", "trunk-side")
    repo.commit("b", "trunk change\n", "trunk change")
    repo.git("switch", "-q", "main")
    repo.git("merge", "--no-ff", "--no-edit", "trunk-side")
    fork = repo.git("rev-parse", "main")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "own change")
    tip = repo.git("rev-parse", "feature")
    repo.git("config", "branch.feature.stackBase", old_base)
    repo.git("push", "-q", "origin", "main")
    repo.commit_on_forge("main", "c", "new trunk change\n", "new trunk change")
    new_trunk = repo.git("rev-parse", "main", cwd=repo.origin)

    assert "stale recorded base" in repo.stack("info", "--offline").stdout

    result = repo.stack(command, "--latest", expect_success=False)

    assert f"git stack track main feature --base {fork}" in result.stderr
    assert repo.git("rev-parse", "feature") == tip
    assert repo.git("rev-parse", "main") == fork
    assert repo.git("rev-parse", "origin/main") == (fork if command == "restack" else new_trunk)
    assert repo.config("feature", "stackBase") == old_base

    repo.stack("track", "main", "feature", "--base", fork)
    repo.stack(command, "--latest")
    repo.assert_stacked("feature", "main", ["own change"])


@pytest.mark.parametrize(("landed", "fork_known"), [(False, False), (True, False), (True, True)])
def test_pull_checks_a_stale_fork_before_moving_trunk(repo: Repo, landed: bool, fork_known: bool) -> None:
    old = repo.git("rev-parse", "main")
    repo.git("switch", "-qc", "side")
    repo.commit("b", "trunk change\n", "trunk change")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    fork = repo.git("rev-parse", "main")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("config", "branch.feature.stackBase", old)
    repo.git("push", "-q", "origin", f"{fork}:refs/heads/main")
    if not fork_known:
        repo.git("update-ref", "refs/heads/main", old)
        repo.git("update-ref", "refs/remotes/origin/main", old)

    if landed:
        repo.git("push", "-q", "origin", "feature")
        repo.squash_merge_on_forge("feature", delete_branch=False)

    result = repo.stack("pull", "--latest", expect_success=landed)

    if landed:
        assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")
        assert repo.git("show", "main:a") == "feature"
        assert repo.client.git.config_get("feature", "stackParent") is None
    else:
        assert f"git stack track main feature --base {fork}" in result.stderr
        assert repo.git("rev-parse", "main") == old
        assert repo.git("rev-parse", "origin/main") == fork


def test_info_reuses_stale_base_check_until_trunk_moves(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("switch", "main")
    repo.commit("b", "trunk\n", "trunk")
    git_calls.clear()

    first = repo.stack("info", "feature", "--offline").stdout
    assert any(call[:2] == ["merge-base", "--all"] for call in git_calls)
    git_calls.clear()

    assert repo.stack("info", "feature", "--offline").stdout == first
    assert not any(call[:2] == ["merge-base", "--all"] for call in git_calls)

    repo.git("replace", "main", "main^")
    git_calls.clear()
    repo.stack("info", "feature", "--offline")
    assert any(call[:2] == ["merge-base", "--all"] for call in git_calls)
    repo.git("replace", "-d", "main")

    repo.commit("b", "new trunk\n", "new trunk")
    git_calls.clear()
    repo.stack("info", "feature", "--offline")
    assert any(call[:2] == ["merge-base", "--all"] for call in git_calls)


def test_failed_stale_base_probe_does_not_cache_a_negative(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    old = repo.git("rev-parse", "main")
    repo.git("switch", "-qc", "side")
    repo.commit("b", "trunk change\n", "trunk change")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    fork = repo.git("rev-parse", "main")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("config", "branch.feature.stackBase", old)

    def unavailable(*_args: object, **_kwargs: object) -> bool:
        raise StackError("Git could not read the base object")

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "check_ancestry", unavailable)
        assert repo.client.stale_trunk_base("feature") is None
        repo.stack("info", "feature", "--offline")

    result = repo.stack("restack", "feature", expect_success=False)

    assert f"git stack track main feature --base {fork}" in result.stderr


def test_stale_base_cache_uses_literal_history_if_replace_changes_during_check(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "-qc", "side")
    repo.commit("b", "side change\n", "side")
    side = repo.git("rev-parse", "side")
    repo.git("switch", "-qc", "alternate", "main")
    repo.commit("c", "different base\n", "alternate base")
    base = repo.git("rev-parse", "alternate")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    fork = repo.git("rev-parse", "main")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("config", "branch.feature.stackBase", base)
    repo.git("switch", "main")
    repo.commit("c", "later trunk change\n", "later trunk change")

    with client.git.snapshot():
        assert client.git.cacheable_history()
        repo.git("replace", "--graft", fork, base, side)
        assert client.stale_trunk_base("feature") is None
    repo.git("replace", "-d", fork)

    fresh = StackClient(GitClient(repo.path))
    with fresh.git.snapshot():
        assert fresh.stale_trunk_base("feature") is None


def test_pull_checks_landing_against_the_projected_trunk(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    repo.git("switch", "-qc", "trunk-side")
    repo.commit("b", "trunk change\n", "trunk change")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "trunk-side")
    fork = repo.git("rev-parse", "main")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("config", "branch.feature.stackBase", old)
    repo.git("switch", "-qc", "another-side", "main")
    repo.commit("c", "other change\n", "other change")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-commit", "another-side")
    repo.write("a", "feature\n")
    repo.git("commit", "-am", "merge takes the feature's change")
    local = repo.git("rev-parse", "main")
    repo.git("push", "-q", "origin", "main")
    repo.commit_on_forge("main", "a", "overlapping change\n", "overlapping change")
    repo.git("switch", "feature")
    assert "(merged)" in repo.stack("info", "--offline").stdout

    result = repo.stack("pull", "--latest", expect_success=False)

    assert f"git stack track main feature --base {fork}" in result.stderr
    assert repo.git("rev-parse", "main") == local


def test_pull_does_not_treat_a_branch_with_no_delta_from_its_stale_base_as_landed(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    original = repo.git("show", "main:a") + "\n"
    repo.git("switch", "-qc", "side")
    repo.commit("a", "trunk edit\n", "trunk edit")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    fork = repo.git("rev-parse", "main")
    repo.git("push", "-q", "origin", "main")
    repo.stack("create", "feature")
    repo.commit("a", original, "feature restores original")
    repo.git("config", "branch.feature.stackBase", old)
    repo.commit_on_forge("main", "a", original, "trunk restores original")

    result = repo.stack("pull", "--latest", expect_success=False)

    assert f"git stack track main feature --base {fork}" in result.stderr
    assert repo.git("rev-parse", "main") == fork


def test_restack_allows_a_squash_landed_branch_with_a_stale_base(repo: Repo) -> None:
    old = repo.git("rev-parse", "main")
    repo.git("switch", "-qc", "side")
    repo.commit("b", "trunk change\n", "trunk change")
    repo.git("switch", "main")
    repo.git("merge", "--no-ff", "--no-edit", "side")
    repo.stack("create", "feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("config", "branch.feature.stackBase", old)
    repo.git("switch", "main")
    repo.git("merge", "--squash", "feature")
    repo.git("commit", "-m", "land feature")
    repo.git("switch", "feature")

    assert "(merged)" in repo.stack("info", "--offline").stdout
    repo.stack("restack", "--latest")


def test_invalid_restack_selection_leaves_other_stacks_pending(repo: Repo, client: StackClient) -> None:
    client.branch("tracked")
    repo.commit("a", "tracked\n", "tracked")
    tip = repo.git("rev-parse", "tracked")
    base = repo.git("rev-parse", "main")
    pending = f"{tip} {base} main"
    repo.git("config", "branch.tracked.stackPendingRestack", pending)
    repo.git("switch", "-qc", "untracked", "main")

    with pytest.raises(StackError, match="untracked isn't in a stack"):
        client.restack()

    assert repo.config("tracked", "stackPendingRestack") == pending


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


def test_restack_onto_ignores_shared_group_peer(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.set_ownership(Ownership.OWNED)
    repo.git("switch", "--quiet", "main")
    client.branch("shared")
    client.set_ownership(Ownership.SHARED)
    client.group_add("feature", ["pr1", "shared"])
    repo.git("switch", "--quiet", "main")
    client.branch("target")
    repo.git("switch", "--quiet", "pr1")
    client.restack(onto="target")
    assert repo.config("pr1", "stackParent") == "target"
    assert repo.config("shared", "stackParent") == "main"


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

    # pr2 conflicts, in the one rebase that goes on to replay pr3 too.
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()

    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")
    # The rebase of the chain finished on its last branch, pr3.
    assert repo.branch_info("pr2") == BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=False)
    client.restack()

    repo.assert_stacked("pr1", "main", ["pr1 touches b", "pr1 c2", "pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_conflict_from_an_older_version_is_finished_by_this_one(repo: Repo, client: StackClient) -> None:
    """Older versions recorded a restack in three settings, which one that
    stopped on a conflict before upgrading leaves behind."""
    repo.make_stack()
    # Only pr2 is restacked, which is the one-branch restack older versions recorded this way (a chain
    # of branches is one rebase, recorded another way: see test_a_conflict_in_a_chain_...).
    client.untrack(["pr3"])
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
    assert repo.config("pr2", "stackBase") == repo.git("rev-parse", "pr1")
    assert not any(
        key.startswith("branch.pr2.stackpending") for key in repo.git("config", "--list", "--name-only").split()
    )


def test_conflict_from_an_older_version_that_was_aborted_is_forgotten(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    # Only pr2 is restacked, which is the one-branch restack older versions recorded this way (a chain
    # of branches is one rebase, recorded another way: see test_a_conflict_in_a_chain_...).
    client.untrack(["pr3"])
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

    # The rebase of pr1 and the branches on it started on the last of them, where an abort returns to.
    assert repo.branch_info("pr1") == BranchInfo("pr1", "main", BranchStatus.NEEDS_RESTACK, current=False)
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
    # The chain of pr2 and pr3 recorded where each was, and takes it back.
    assert not repo.config_exists("pr2", "stackPendingRestackBatch")
    assert not repo.config_exists("pr3", "stackPendingRestackBatch")


def test_restack_onto_suggests_tracking_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "loose")

    with pytest.raises(StackError, match="loose isn't in a stack; use `git stack track` first"):
        client.restack(onto="main")


def test_restack_onto_moves_the_named_branch_without_switching_to_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    client.restack(["pr2"], onto="main")

    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "main"


def test_restack_onto_a_named_branch_suggests_tracking_it_if_untracked(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "--quiet", "-c", "loose")
    repo.git("switch", "--quiet", "main")

    with pytest.raises(StackError, match="loose isn't in a stack; use `git stack track` first"):
        client.restack(["loose"], onto="main")


@pytest.mark.parametrize(("branches", "groups"), [(["pr2", "pr3"], []), ([], ["feature"]), (["pr2"], ["feature"])])
def test_restack_onto_moves_one_branch(repo: Repo, client: StackClient, branches: list[str], groups: list[str]) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="--onto moves one branch"):
        client.restack(branches, groups, onto="main")


def test_restack_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main\n", "main moved")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.restack()

    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "main~")
    assert repo.own_commits("pr1", "main") == ["pr1 c2", "pr1 c1"]


def rebase_commands(git_calls: list[GitCall]) -> list[GitCall]:
    return [call for call in git_calls if call[0] == "rebase"]


def continue_rebase(repo: Repo) -> bool:
    """`git rebase --continue`, which stops again, with an exit status, on the next conflict."""
    result = subprocess.run(
        [real_git(), "-C", str(repo.path), "rebase", "--continue"], check=False, capture_output=True
    )
    return result.returncode == 0


def test_restack_replays_a_chain_of_branches_in_one_rebase(
    repo: Repo, client: StackClient, git_calls: list[GitCall]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    repo.git("switch", "--quiet", "pr3")
    git_calls.clear()

    client.restack()

    (rebase,) = rebase_commands(git_calls)
    assert "--update-refs" in rebase
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr3"


def test_restack_of_a_chain_says_it_is_restacking_each_branch(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    client.restack()

    restacking = [e for e in emitted if isinstance(e, events.Restacking)]
    assert restacking == [events.Restacking("pr2", "pr1"), events.Restacking("pr3", "pr2")]


def test_a_conflict_in_a_chain_moves_no_branch_until_it_is_resolved_and_continued(
    repo: Repo, client: StackClient
) -> None:
    repo.make_stack()
    before = {branch: repo.git("rev-parse", branch) for branch in ("pr2", "pr3")}
    repo.git("switch", "--quiet", "pr1")
    repo.commit("c", "pr1 change\n", "pr1 touches c")

    # pr2 replays cleanly and pr3, which also changes c, conflicts: the one rebase stops there.
    with pytest.raises(StackError, match="restacking pr3 stopped on a conflict"):
        client.restack()
    assert {branch: repo.git("rev-parse", branch) for branch in ("pr2", "pr3")} == before

    repo.write("c", "resolved\n")
    repo.git("add", "c")
    assert continue_rebase(repo)
    assert repo.branch_info("pr2") == BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=False)
    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.config("pr3", "stackBase") == repo.git("rev-parse", "pr2")


def test_a_chain_finished_by_hand_records_each_base_even_if_a_branch_moved_on_before_git_stack_ran(
    repo: Repo, client: StackClient
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("c", "pr1 change\n", "pr1 touches c")
    with pytest.raises(StackError, match="restacking pr3 stopped on a conflict"):
        client.restack()
    repo.write("c", "resolved\n")
    repo.git("add", "c")
    assert continue_rebase(repo)
    rebased_pr2 = repo.git("rev-parse", "pr2")

    # A commit on pr2 before git-stack has seen the finished rebase: pr2's tip is no longer
    # where pr3 was replayed onto.
    repo.git("switch", "--quiet", "pr2")
    repo.commit("a", "pr2 afterwards\n", "pr2 afterwards")

    assert client.base("pr3") == rebased_pr2


def test_a_branch_pointing_into_a_chain_is_left_where_it_was(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    backup = repo.git("rev-parse", "pr2")
    repo.git("branch", "pr2-backup", "pr2")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    client.restack()

    # `git rebase --update-refs` would have moved it, which is no business of ours.
    assert repo.git("rev-parse", "pr2-backup") == backup
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_a_chain_whose_first_branch_has_no_commits_of_its_own_is_restacked(repo: Repo, client: StackClient) -> None:
    client.branch("empty")
    client.branch("middle")
    repo.commit("b", "middle\n", "middle c1")
    client.branch("top")
    repo.commit("c", "top\n", "top c1")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main moved\n", "main moves")
    repo.git("switch", "--quiet", "top")

    client.restack(latest=True)

    repo.git("merge-base", "--is-ancestor", "main", "top")  # fails the test if main isn't in top's history
    repo.assert_stacked("middle", "empty", ["middle c1"])
    repo.assert_stacked("top", "middle", ["top c1"])


def test_every_conflict_of_a_chain_is_resolved_in_the_one_rebase(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.write("b", "pr1 change\n")
    repo.write("c", "pr1 change\n")
    repo.git("commit", "--quiet", "-am", "pr1 touches b and c")

    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        client.restack()
    repo.write("b", "resolved b\n")
    repo.git("add", "b")
    assert not continue_rebase(repo)
    assert "pr3" in repo.git("log", "-1", "--format=%s", "REBASE_HEAD")
    repo.write("c", "resolved c\n")
    repo.git("add", "c")
    assert continue_rebase(repo)
    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])


def test_a_chain_rebase_that_was_aborted_changes_nothing_and_is_forgotten(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    before = {branch: (repo.git("rev-parse", branch), repo.config(branch, "stackBase")) for branch in ("pr2", "pr3")}
    repo.git("switch", "--quiet", "pr1")
    repo.commit("c", "pr1 change\n", "pr1 touches c")
    with pytest.raises(StackError, match="restacking pr3 stopped on a conflict"):
        client.restack()
    repo.git("rebase", "--abort")
    assert {branch: (repo.git("rev-parse", branch), repo.config(branch, "stackBase")) for branch in before} == before
    # So restacking doesn't stop on the same conflict again.
    repo.git("switch", "--quiet", "pr1")
    repo.git("reset", "--quiet", "--hard", "HEAD~1")

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    assert not any(
        key.startswith("branch.pr2.stackpending") for key in repo.git("config", "--list", "--name-only").split()
    )


def test_a_branch_stacked_beside_a_chain_is_restacked_after_it(
    repo: Repo, client: StackClient, git_calls: list[GitCall]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")
    repo.write("pr2b.txt", "pr2b\n")
    repo.git("add", "pr2b.txt")
    repo.git("commit", "--quiet", "-m", "pr2b c1")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    git_calls.clear()

    client.restack()

    # pr2 and pr3 in one rebase, and pr2b, which is on pr1 beside pr2, by itself.
    assert len(rebase_commands(git_calls)) == 2
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    repo.assert_stacked("pr2b", "pr1", ["pr2b c1"])


def test_a_chain_is_restacked_from_a_branch_in_the_middle_of_it(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")
    # git doesn't move a branch that's checked out, so one in the chain that's where we are
    # has to be left first, or its rebase is skipped and the ones above it are rebased without it.
    repo.git("switch", "--quiet", "pr2")

    client.restack()

    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert repo.git("branch", "--show-current") == "pr2"
