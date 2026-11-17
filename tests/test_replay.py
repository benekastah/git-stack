"""Ordered replay in real repositories."""

import pytest

from git_stack import events, replay
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import StackClient
from tests.helpers import Repo


@pytest.fixture
def combined(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("combined")
    repo.commit("c", "combined\n", "own change")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.commit("b", "second\n", "second")
    repo.git("switch", "--quiet", "combined")
    repo.stack("track", "--on", "first", "--on", "second")


def test_replay_rebuilds_from_rewritten_base(repo: Repo, client: StackClient, combined: None) -> None:
    first = repo.git("rev-parse", "first")
    second = repo.git("rev-parse", "second")
    client.restack()
    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "second\nown change"
    tip = repo.git("rev-parse", "combined")
    client.restack()
    assert repo.git("rev-parse", "combined") == tip
    assert repo.git("rev-parse", "second") == second

    repo.git("switch", "--quiet", "second")
    repo.write("b", "rewritten\n")
    repo.git("commit", "--quiet", "--amend", "-am", "new second")
    repo.git("switch", "--quiet", "combined")
    client.restack()

    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "new second\nown change"
    assert repo.git("rev-parse", "first") == first
    assert repo.git("branch", "--show-current") == "combined"


@pytest.mark.parametrize("shape", ["one-branch", "linear-stack", "multiple-bases"])
def test_replay_excludes_main_history_from_a_newer_secondary_base(repo: Repo, client: StackClient, shape: str) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    first = repo.git("rev-parse", "first")
    client.branch("combined")
    repo.commit("c", "combined\n", "own change")
    repo.git("switch", "-q", "main")
    repo.write("trunk-only", "from main\n")
    repo.git("add", "trunk-only")
    repo.git("commit", "-qm", "unrelated main update")
    client.branch("second")
    repo.commit("b", "second\n", "secondary change")
    secondary = "second"
    if shape == "linear-stack":
        client.branch("top")
        repo.write("extra", "from top\n")
        repo.git("add", "extra")
        repo.git("commit", "-qm", "extra change")
        secondary = "top"
    elif shape == "multiple-bases":
        repo.git("switch", "-q", "main")
        client.branch("side")
        repo.write("extra", "from side\n")
        repo.git("add", "extra")
        repo.git("commit", "-qm", "extra change")
        repo.git("switch", "-q", "second")
        client.restack(onto=("main", "side"))
    source_tip = repo.git("rev-parse", secondary)
    repo.git("switch", "-q", "combined")

    client.restack(onto=("first", secondary))

    expected = {
        "one-branch": "secondary change\nown change",
        "linear-stack": "secondary change\nextra change\nown change",
        "multiple-bases": "extra change\nsecondary change\nown change",
    }
    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == expected[shape]
    assert repo.git("rev-parse", "first") == first
    assert repo.git("rev-parse", secondary) == source_tip


@pytest.mark.parametrize(("resume", "reflog"), [(True, True), (False, True), (True, False)])
def test_replay_conflict_continue_or_abort(
    repo: Repo, client: StackClient, combined: None, resume: bool, reflog: bool
) -> None:
    repo.git("switch", "--quiet", "second")
    repo.commit("a", "conflicting second\n", "conflicting second")
    repo.git("switch", "--quiet", "combined")
    tip = repo.git("rev-parse", "combined")
    base = repo.config("combined", "stackBase")

    if not reflog:
        repo.git("config", "core.logAllRefUpdates", "false")
        (repo.path / ".git" / "logs" / "HEAD").unlink()

    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    assert repo.git("rev-parse", "combined") == tip
    assert repo.config("combined", "stackBase") == base

    if resume:
        repo.write("a", "resolved\n")
        repo.git("add", "a")
        repo.git("-c", "core.editor=true", "rebase", "--continue")
    else:
        repo.git("rebase", "--abort")
    client.restack()

    assert repo.git("branch", "--show-current") == "combined"
    assert not repo.git("branch", "--list", "git-stack-replay/*")
    if resume:
        assert repo.git("show", "combined:a") == "resolved"
        assert repo.git("log", "-1", "--format=%s", "combined") == "own change"
    else:
        assert repo.git("rev-parse", "combined") == tip
        assert repo.config("combined", "stackBase") == base


def test_track_changes_base_order_without_reclassifying_old_replay_as_my_work(
    repo: Repo, client: StackClient, combined: None
) -> None:
    client.restack()
    old = repo.git("rev-parse", "combined")

    repo.stack("track", "--on", "second", "--on", "first")
    assert repo.git("rev-parse", "combined") == old
    assert client.stacks().dependencies("combined") == ("second", "first")
    client.restack()

    assert repo.git("log", "--reverse", "--format=%s", "second..combined") == "first\nown change"


def test_replay_recovers_between_publication_and_metadata(
    repo: Repo, client: StackClient, combined: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_config_set = client.git.config_set

    def fail_boundary_once(branch: str, key: str, value: str) -> None:
        if branch == "combined" and key == "stackBase":
            monkeypatch.setattr(client.git, "config_set", original_config_set)
            raise StackError("simulated interruption")
        original_config_set(branch, key, value)

    monkeypatch.setattr(client.git, "config_set", fail_boundary_once)
    with pytest.raises(StackError, match="simulated interruption"):
        client.restack()
    published = repo.git("rev-parse", "combined")
    assert repo.git("show", "combined:b") == "second"

    client.restack()

    assert repo.git("rev-parse", "combined") == published
    assert repo.git("branch", "--show-current") == "combined"
    assert repo.config("combined", "stackBase") == repo.git("rev-parse", "combined^")


def test_replay_resumes_descendants_after_a_conflict(repo: Repo, client: StackClient, combined: None) -> None:
    client.branch("child")
    repo.commit("c", "child\n", "child")
    repo.git("switch", "--quiet", "second")
    repo.commit("a", "conflicting second\n", "conflicting second")
    repo.git("switch", "--quiet", "child")

    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")

    client.restack()

    assert repo.git("merge-base", "--is-ancestor", "combined", "child") == ""
    assert repo.git("show", "child:a") == "resolved"
    assert repo.git("branch", "--show-current") == "child"


def test_replay_preserves_own_patch_when_a_replaceable_base_has_it(
    repo: Repo, client: StackClient, combined: None
) -> None:
    original = repo.git("rev-parse", "combined")
    repo.git("switch", "--quiet", "second")
    repo.git("cherry-pick", original)
    repo.git("switch", "--quiet", "combined")

    with pytest.raises(StackError, match="own commit"):
        client.restack()

    assert repo.git("rev-parse", "combined") == original
    repo.git("rebase", "--abort")
    client.restack()
    client.restack(onto="first")
    assert repo.git("show", "combined:c") == "combined"


@pytest.mark.parametrize(
    ("untrack_source", "diff_config"),
    [(False, None), (True, None), (False, ("diff.noprefix", "true")), (False, ("diff.context", "0"))],
)
def test_replay_deduplicates_a_shared_source_in_a_nested_graph(
    repo: Repo, client: StackClient, untrack_source: bool, diff_config: tuple[str, str] | None
) -> None:
    if diff_config:
        repo.git("config", *diff_config)
    repo.commit("a", "one\ntwo\ncommon\nfour\nfive\n", "add context")
    client.branch("secondary")
    repo.commit("a", "one\ntwo\nsecondary\nfour\nfive\n", "secondary")
    client.branch("first")
    resolved = "one\ntwo\nsecondary amended\nfour\nfive"
    repo.commit("a", resolved + "\n", "amend secondary")
    client.branch("final")
    repo.commit("c", "final\n", "final")
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    repo.commit("b", "other\n", "other")
    client.branch("mixed")
    repo.stack("track", "--on", "other", "--on", "secondary")
    repo.git("switch", "--quiet", "final")
    repo.stack("track", "--on", "first", "--on", "mixed")

    client.restack()

    if untrack_source:
        client.untrack(["mixed"])
        repo.git("switch", "-q", "first")
        repo.write("d", "unrelated\n")
        repo.git("add", "d")
        repo.git("commit", "-qm", "unrelated")
        repo.git("switch", "-q", "final")
        client.restack()

    assert repo.git("show", "final:a") == resolved
    assert repo.git("show", "final:b") == "other"
    assert repo.git("log", "--reverse", "--format=%s", "first..final") == "other\nfinal"


def test_replay_uses_updated_primary_when_validating_secondary_changes(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("combined")
    repo.commit("c", "own\n", "own")
    repo.git("switch", "--quiet", "main")
    repo.git("switch", "--quiet", "-c", "trunk-side")
    repo.write("d", "trunk side\n")
    repo.git("add", "d")
    repo.git("commit", "-qm", "trunk side")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--no-ff", "--no-edit", "trunk-side")
    client.branch("second")
    repo.commit("b", "second\n", "second")
    repo.git("switch", "--quiet", "combined")
    repo.stack("track", "--on", "first", "--on", "second")

    client.restack(latest=True)

    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "second\nown"


def test_pull_keeps_own_boundary_when_primary_already_contains_the_graph_branch(
    repo: Repo, client: StackClient, combined: None
) -> None:
    client.restack()
    old_base = repo.config("combined", "stackBase")
    repo.git("switch", "--quiet", "first")
    repo.git("merge", "--ff-only", "combined")
    repo.git("switch", "--quiet", "combined")

    client.sync(push=False)

    assert repo.config("combined", "stackBase") == old_base
    client.clean()
    repo.assert_untracked("combined")


def test_revert_advice_names_the_base_that_reverted_the_branch(repo: Repo, client: StackClient, combined: None) -> None:
    original = repo.git("rev-parse", "combined")
    repo.git("switch", "--quiet", "second")
    repo.git("cherry-pick", original)
    repo.git("revert", "--no-edit", "HEAD")
    revert = repo.git("rev-parse", "HEAD")
    repo.git("switch", "--quiet", "combined")
    client.branch("child")

    result = repo.stack("restack")

    assert "then second reverted" in result.stderr
    assert f"{revert[:12]} --from second" in result.stderr
    assert "--stack child" not in result.stderr
    assert repo.git("rev-parse", "combined") == original


def test_track_repairs_boundary_after_manual_rebase(repo: Repo, client: StackClient, combined: None) -> None:
    client.restack()
    boundary = repo.config("combined", "stackBase")
    primary = repo.git("rev-parse", "first")
    repo.git("rebase", "--onto", primary, boundary)

    result = repo.stack("restack", expect_success=False)
    assert "track --on first --on second --base" in result.stderr
    repo.stack("track", "--on", "first", "--on", "second", "--base", primary)
    client.restack()

    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "second\nown change"


def test_replay_does_not_publish_after_a_source_ref_changes(
    repo: Repo, client: StackClient, combined: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish = replay.publish
    before = repo.git("rev-parse", "combined")
    boundary = repo.config("combined", "stackBase")

    def move_source_then_publish(git: GitClient, job: replay.Replay) -> None:
        repo.git("update-ref", "refs/heads/second", "refs/heads/main")
        publish(git, job)

    monkeypatch.setattr(replay, "publish", move_source_then_publish)
    with pytest.raises(StackError, match="update-ref"):
        client.restack()

    assert repo.git("rev-parse", "combined") == before
    assert repo.config("combined", "stackBase") == boundary
    assert repo.git("branch", "--show-current") == "combined"
    assert replay.pending(client.git) is None


def test_replay_skips_combined_branch_checked_out_in_another_worktree(
    repo: Repo, client: StackClient, combined: None
) -> None:
    before = repo.git("rev-parse", "combined")
    repo.git("switch", "--quiet", "first")
    worktree = repo.tmp / "combined-worktree"
    repo.git("worktree", "add", str(worktree), "combined")

    client.restack(["combined"])

    assert repo.git("rev-parse", "combined") == before
    assert repo.git("rev-parse", "HEAD", cwd=worktree) == before
    assert isinstance(repo.emitted[-1], events.NotRestackedInOtherWorktree)


def test_replay_keeps_secondary_patch_reverted_in_primary_history(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "-qc", "second")
    repo.commit("b", "live secondary\n", "secondary")
    source = repo.git("rev-parse", "second")
    repo.git("switch", "-qc", "first", "main")
    repo.git("cherry-pick", source)
    repo.git("commit", "--amend", "-qm", "earlier equivalent change")
    repo.git("revert", "--no-edit", "HEAD")
    client.branch("combined")
    repo.commit("c", "combined\n", "own change")
    repo.stack("track", "--on", "first", "--on", "second")

    client.restack()

    assert repo.git("show", "combined:b") == "live secondary"
    assert repo.git("show", "combined:c") == "combined"


def test_replay_recovers_after_rebase_before_journal_write(
    repo: Repo, client: StackClient, combined: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = replay.Replay.save

    def interrupt_after_rebase(record: replay.Replay, git: GitClient) -> None:
        if not record.in_stage and record.head != record.tips[0]:
            monkeypatch.setattr(replay.Replay, "save", save)
            raise StackError("simulated interruption")
        save(record, git)

    monkeypatch.setattr(replay.Replay, "save", interrupt_after_rebase)
    with pytest.raises(StackError, match="simulated interruption"):
        client.restack()

    client.restack()

    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "second\nown change"


def test_replay_resume_preserves_an_earlier_worktree_skip(repo: Repo, client: StackClient, combined: None) -> None:
    repo.git("switch", "--quiet", "main")
    client.branch("blocked")
    repo.commit("c", "blocked\n", "blocked")
    client.branch("after")
    repo.stack("track", "--on", "blocked", "--on", "combined")
    after = repo.git("rev-parse", "after")
    repo.git("worktree", "add", str(repo.tmp / "blocked-worktree"), "blocked")

    repo.git("switch", "--quiet", "main")
    repo.write("d", "upstream\n")
    repo.git("add", "d")
    repo.git("commit", "-qm", "upstream")
    repo.git("switch", "--quiet", "second")
    repo.commit("a", "conflicting second\n", "conflicting second")
    repo.git("switch", "--quiet", "after")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack(latest=True)
    assert any(isinstance(event, events.NotRestackedInOtherWorktree) for event in repo.emitted)
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")

    client.restack()

    assert repo.git("show", "combined:a") == "resolved"
    assert repo.git("rev-parse", "after") == after


def test_restack_onto_does_not_treat_a_prospective_base_as_a_merge(
    repo: Repo, client: StackClient, combined: None
) -> None:
    client.restack()
    before = repo.git("rev-parse", "combined")
    repo.git("branch", "newfirst", before)
    repo.git("branch", "newsecond", "main")

    result = repo.stack("restack", "--onto", "newfirst", "--onto", "newsecond", expect_success=False)

    assert "own commit" in result.stderr
    assert client.stacks().dependencies("combined") == ("first", "second")
    assert repo.git("rev-parse", "combined") == before


def test_replay_refuses_a_symbolic_target_ref(repo: Repo, client: StackClient, combined: None) -> None:
    before = repo.git("rev-parse", "combined")
    repo.git("branch", "unrelated", "combined")
    repo.git("switch", "--quiet", "first")
    repo.git("symbolic-ref", "refs/heads/combined", "refs/heads/unrelated")

    with pytest.raises(StackError, match="symbolic ref"):
        client.restack(["combined"])

    assert repo.git("rev-parse", "unrelated") == before
    assert repo.git("symbolic-ref", "refs/heads/combined") == "refs/heads/unrelated"


def test_replay_resumes_projected_merge_in_another_group_member(
    repo: Repo, client: StackClient, combined: None
) -> None:
    repo.git("switch", "--quiet", "main")
    client.branch("z-merged")
    repo.write("d", "merged\n")
    repo.git("add", "d")
    repo.git("commit", "-qm", "merged")
    client.branch("z-child")
    repo.commit("c", "z-child\n", "z-child")
    client.group_add("together", ["combined", "z-merged"])
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--squash", "z-merged")
    repo.git("commit", "-qm", "squashed z-merged")
    repo.git("switch", "--quiet", "second")
    repo.commit("a", "conflicting secondary\n", "conflicting secondary")
    repo.git("switch", "--quiet", "combined")

    with pytest.raises(StackError, match="rebase --continue"):
        client.restack(groups=["together"], latest=True)
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")

    client.restack()

    assert client.stacks().dependencies("z-child") == ("main",)
    assert repo.git("log", "--format=%s", "main..z-child") == "z-child"


@pytest.mark.parametrize("shared_destination", [True, False])
def test_replay_retries_shared_ownership_after_metadata_failure(
    repo: Repo, client: StackClient, combined: None, monkeypatch: pytest.MonkeyPatch, shared_destination: bool
) -> None:
    client.branch("child")
    repo.git("switch", "--quiet", "main")
    client.branch("shared-source")
    repo.commit("b", "shared\n", "shared")
    if shared_destination:
        repo.stack("ownership", "shared")
        parents = ("first", "shared-source")
    else:
        repo.git("config", "branch.second.stackOwnership", "shared")
        parents = ("shared-source", "first")
    repo.git("switch", "--quiet", "combined")

    original_config_set = client.git.config_set

    def fail_child_ownership_once(branch: str, key: str, value: str) -> None:
        if branch == "child" and key == "stackOwnership":
            monkeypatch.setattr(client.git, "config_set", original_config_set)
            raise StackError("simulated interruption")
        original_config_set(branch, key, value)

    monkeypatch.setattr(client.git, "config_set", fail_child_ownership_once)
    with pytest.raises(StackError, match="simulated interruption"):
        client.restack(onto=parents, allow_shared=True)
    assert client.stacks().dependencies("combined") == parents

    client.restack()

    assert client.stacks().is_shared("child")


def test_replay_recovers_after_aborting_and_switching_back(repo: Repo, client: StackClient, combined: None) -> None:
    repo.git("switch", "-q", "second")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")
    before = repo.git("rev-parse", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()

    repo.git("rebase", "--abort")
    repo.git("switch", "-q", "combined")
    client.restack()

    assert replay.pending(client.git) is None
    assert repo.git("rev-parse", "combined") == before
    assert repo.git("branch", "--show-current") == "combined"


def test_replay_refuses_manually_emptied_own_commit(repo: Repo, client: StackClient, combined: None) -> None:
    before = repo.git("rev-parse", "combined")
    repo.git("switch", "-q", "second")
    repo.git("cherry-pick", before)
    repo.git("switch", "-q", "combined")
    with pytest.raises(StackError, match="own commit"):
        client.restack()

    repo.git("commit", "--allow-empty", "-C", before)
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    with pytest.raises(StackError, match="own commit.*empty"):
        client.restack()

    assert replay.pending(client.git) is None
    assert repo.git("rev-parse", "combined") == before
    assert repo.git("branch", "--show-current") == "combined"
    client.restack(onto="first")
    assert repo.git("show", "combined:c") == "combined"


@pytest.mark.parametrize("legacy_copy", [False, True])
def test_replay_does_not_deduplicate_a_conflict_resolution_that_changed_indentation(
    repo: Repo, client: StackClient, legacy_copy: bool
) -> None:
    repo.commit("a", "x=1\n", "baseline")
    repo.git("switch", "-qc", "first")
    repo.commit("a", " x=1\n", "indent first")
    repo.git("switch", "-qc", "second", "main")
    repo.commit("a", "x=3\n", "change second")
    repo.git("switch", "-qc", "mixed", "first")
    repo.stack("track", "--on", "first", "--on", "second")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "    x=3\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    client.restack()

    if legacy_copy:
        copy, source = (repo.git("rev-parse", ref) for ref in ("mixed", "second"))
        repo.git("config", "branch.mixed.stackReplayCopies", f"{copy} {source}")
    client.branch("final")
    repo.stack("track", "--on", "mixed", "--on", "second")
    before = repo.git("rev-parse", "final")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()

    assert repo.git("rev-parse", "final") == before


@pytest.mark.parametrize("command", ["restack", "pull", "sync"])
def test_replay_conflict_names_the_command_to_resume(
    repo: Repo, client: StackClient, combined: None, command: str
) -> None:
    repo.git("switch", "-q", "second")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")

    result = repo.stack(command, expect_success=False)

    assert "`git rebase --continue`" in result.stderr
    assert "`git rebase --abort`" in result.stderr
    assert f"`git stack {command}`" in result.stderr
    assert "before switching branches" in result.stderr


def test_replay_recovers_a_resolved_stage_after_switching_back(repo: Repo, client: StackClient, combined: None) -> None:
    repo.git("switch", "-q", "second")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    repo.git("switch", "-q", "combined")

    client.restack()
    assert repo.git("show", "combined:a") == "resolved"
    assert repo.git("branch", "--show-current") == "combined"


def test_replay_retries_a_stage_vetoed_by_a_pre_rebase_hook(repo: Repo, client: StackClient, combined: None) -> None:
    before = repo.git("rev-parse", "combined")
    repo.git("switch", "-q", "second")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    resolved = repo.git("rev-parse", "HEAD")

    repo.add_hook(repo.path, "pre-rebase", "#!/bin/sh\nexit 1\n")
    with pytest.raises(StackError, match="could not rebase"):
        client.restack()
    assert repo.git("rev-parse", "combined") == before
    pinned = repo.git("for-each-ref", "--format=%(objectname)", replay.PIN_PREFIX).splitlines()
    (repo.path / ".git" / "hooks" / "pre-rebase").unlink()
    client.restack()

    assert repo.git("rev-parse", "combined^") == resolved
    assert resolved in pinned
    assert repo.git("show", "combined:a") == "resolved"
    assert repo.git("show", "combined:c") == "combined"


def test_replay_keeps_completed_resolution_when_final_checkout_is_blocked(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("combined")
    repo.git("switch", "-q", "main")
    client.branch("second")
    repo.write("new-file", "from second\n")
    repo.git("add", "new-file")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")
    repo.stack("track", "--on", "first", "--on", "second")
    before = repo.git("rev-parse", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    repo.git("switch", "-q", "combined")
    repo.write("new-file", "local\n")

    with pytest.raises(StackError, match="untracked working tree files"):
        client.restack()
    assert repo.git("rev-parse", "combined") == before
    (repo.path / "new-file").rename(repo.path / "saved-local")
    client.restack()

    assert repo.git("show", "combined:a") == "resolved"
    assert repo.git("show", "combined:new-file") == "from second"
    assert (repo.path / "saved-local").read_text(encoding="utf-8") == "local\n"


def test_replay_prints_how_to_continue_when_an_untracked_file_blocks_an_own_commit(
    repo: Repo, client: StackClient, combined: None
) -> None:
    repo.write("new-file", "own change\n")
    repo.git("add", "new-file")
    repo.git("commit", "-qm", "add file")
    repo.git("switch", "-q", "second")
    repo.commit("a", "conflicting second\n", "conflict")
    repo.git("switch", "-q", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    repo.write("new-file", "local\n")

    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    (repo.path / "new-file").rename(repo.path / "saved-local")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    client.restack()

    assert repo.git("show", "combined:a") == "resolved"
    assert repo.git("show", "combined:new-file") == "own change"


@pytest.mark.parametrize("resume", [True, False])
def test_own_stage_recovery_accepts_an_edited_resolution_message(
    repo: Repo, client: StackClient, combined: None, resume: bool
) -> None:
    original = repo.git("rev-parse", "combined")
    repo.git("switch", "-q", "first")
    repo.commit("c", "conflicting primary\n", "primary edit")
    repo.git("switch", "-q", "combined")
    with pytest.raises(StackError, match="rebase --continue"):
        client.restack()
    assert repo.git("rev-parse", "combined") == original

    if resume:
        repo.write("c", "resolved own change\n")
        repo.git("add", "c")
        repo.git("commit", "-m", "updated resolution message")
        repo.git("-c", "core.editor=true", "rebase", "--continue")
    else:
        repo.git("rebase", "--abort")
    client.restack()

    assert repo.git("branch", "--show-current") == "combined"
    if resume:
        assert repo.git("log", "-1", "--format=%s", "combined") == "updated resolution message"
        assert repo.git("show", "combined:b") == "second"
    else:
        assert repo.git("rev-parse", "combined") == original
