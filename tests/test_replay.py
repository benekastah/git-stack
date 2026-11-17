"""Ordered replay in real repositories."""

import pytest

from git_stack.errors import StackError
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


@pytest.mark.parametrize("resume", [True, False])
def test_replay_conflict_continue_or_abort(repo: Repo, client: StackClient, combined: None, resume: bool) -> None:
    repo.git("switch", "--quiet", "second")
    repo.commit("a", "conflicting second\n", "conflicting second")
    repo.git("switch", "--quiet", "combined")
    tip = repo.git("rev-parse", "combined")
    base = repo.config("combined", "stackBase")

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
