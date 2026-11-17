"""Ordered replay in real repositories."""

import pytest

from git_stack import events, replay
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchStatus, StackClient
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

    with pytest.raises(StackError, match="cherry-pick --continue"):
        client.restack()
    assert repo.git("rev-parse", "combined") == tip
    assert repo.config("combined", "stackBase") == base

    if resume:
        repo.write("a", "resolved\n")
        repo.git("add", "a")
        repo.git("-c", "core.editor=true", "cherry-pick", "--continue")
    else:
        repo.git("cherry-pick", "--abort")
    client.restack()

    assert repo.git("branch", "--show-current") == "combined"
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


def test_info_tracks_secondary_source_and_descendant_status(repo: Repo, client: StackClient, combined: None) -> None:
    client.restack()
    client.branch("child")
    assert {b.name: b.status for b in client.info(offline=True)}["combined"] is BranchStatus.UP_TO_DATE
    repo.git("switch", "--quiet", "second")
    repo.commit("b", "more\n", "more")
    statuses = {b.name: b.status for b in client.info(offline=True)}
    assert statuses["combined"] is statuses["child"] is BranchStatus.NEEDS_RESTACK


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

    with pytest.raises(StackError, match="cherry-pick --continue"):
        client.restack()
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "cherry-pick", "--continue")

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
    repo.git("cherry-pick", "--abort")
    client.restack()
    client.restack(onto="first")
    assert repo.git("show", "combined:c") == "combined"


def test_replay_deduplicates_a_shared_source_in_a_nested_graph(repo: Repo, client: StackClient) -> None:
    client.branch("secondary")
    repo.commit("a", "secondary\n", "secondary")
    client.branch("first")
    repo.commit("a", "secondary amended\n", "amend secondary")
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

    assert repo.git("show", "final:a") == "secondary amended"
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


def test_replay_recovers_after_pick_before_journal_write(
    repo: Repo, client: StackClient, combined: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    save = replay.Replay.save

    def interrupt_after_pick(record: replay.Replay, git: GitClient) -> None:
        if not record.in_pick and record.head != record.tips[0]:
            monkeypatch.setattr(replay.Replay, "save", save)
            raise StackError("simulated interruption")
        save(record, git)

    monkeypatch.setattr(replay.Replay, "save", interrupt_after_pick)
    with pytest.raises(StackError, match="simulated interruption"):
        client.restack()

    client.restack()

    assert repo.git("log", "--reverse", "--format=%s", "first..combined") == "second\nown change"
