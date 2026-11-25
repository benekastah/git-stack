"""Closing draft blockers is explicit and scoped to a GitHub stack rebuild."""

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from git_stack.adapters.github import GitHubAdapter, GitHubStack, OrphanedPullRequest
from git_stack.stack import StackClient
from tests import fake_gh
from tests.helpers import FakeGitHub, Repo


def after_closure_plan(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    plan = GitHubAdapter.plan_orphan_closures

    def plan_then_change(
        adapter: GitHubAdapter, stacks: Iterable[GitHubStack], allowed: set[int]
    ) -> list[OrphanedPullRequest]:
        candidates = plan(adapter, stacks, allowed)
        if candidates:
            change()
        return candidates

    monkeypatch.setattr(GitHubAdapter, "plan_orphan_closures", plan_then_change)


@pytest.fixture
def orphan(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.client.push()
    repo.client.prs()
    gh.enable_stacks([1, 2, 3])
    state = gh.load()
    state["prs"][2]["baseRefName"] = "pr1"
    gh.save(state)
    repo.client.untrack(["pr2"])
    repo.git("config", "branch.pr3.stackParent", "pr1")
    gh.clear_calls()


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_dry_run_previews_without_writes(repo: Repo, gh: FakeGitHub) -> None:
    result = repo.stack("prs", "--dry-run", "--force")

    assert "would close draft PR #2" in result.stdout
    assert "https://github.com/acme/widgets/pull/2" in result.stdout
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_rebuild_closes_eligible_draft_without_a_terminal(repo: Repo, gh: FakeGitHub) -> None:
    before = repo.git("rev-parse", "refs/remotes/origin/pr2")

    result = repo.stack_process("prs", "--force")

    assert "closed draft PR #2" in result.stdout
    assert "Enter" not in result.stderr
    assert gh.load()["prs"][1]["state"] == "CLOSED"
    assert gh.stacks() == [[1, 3]]
    assert repo.git("rev-parse", "refs/heads/pr2") == before
    assert repo.git("rev-parse", "refs/heads/pr2", cwd=repo.origin) == before
    assert "--delete-branch" not in gh.calls("pr", "close")[0]


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize(
    "updates",
    [{"author": "someone-else"}, {"isDraft": False}, {"isInMergeQueue": True}, {"reviewDecision": "APPROVED"}],
)
def test_close_orphaned_prs_refuses_ineligible_blockers(
    repo: Repo, gh: FakeGitHub, updates: dict[str, str | bool]
) -> None:
    state = gh.load()
    state["prs"][1].update(updates)
    gh.save(state)

    result = repo.stack("prs", "--force", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_refuses_if_any_open_pr_depends_on_it(repo: Repo, gh: FakeGitHub) -> None:
    gh.add_pr(
        number=20,
        state="OPEN",
        headRefName="external",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "refs/heads/pr2"),
        mergeCommit=None,
    )

    result = repo.stack("prs", "--force", expect_success=False)

    assert "open pull requests target its head pr2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_rebuild_rechecks_a_planned_blocker_before_closing(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def change() -> None:
        state = gh.load()
        state["prs"][1]["headRefOid"] = "f" * 40
        gh.save(state)

    after_closure_plan(monkeypatch, change)

    result = repo.stack("prs", "--force", expect_success=False)

    assert "changed before closure" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_push_closes_blocker_before_retarget_and_publication(repo: Repo, gh: FakeGitHub) -> None:
    state = gh.load()
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr3")
    repo.commit("c", "updated\n", "update pr3")

    repo.stack("push", "--prs", "--force")

    assert gh.load()["prs"][1]["state"] == "CLOSED"
    assert gh.load()["prs"][2]["baseRefName"] == "pr1"
    assert gh.stacks() == [[1, 3]]
    assert repo.remote_subject("pr3") == "update pr3"


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_keeps_a_draft_tracked_in_another_stack(repo: Repo, gh: FakeGitHub) -> None:
    repo.stack("track", "main", "pr2")

    result = repo.stack("prs", "--force", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_push_reports_draft_closure_when_real_push_fails(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr3")
    repo.commit("c", "updated locally\n", "update pr3")

    def change() -> None:
        repo.commit_on_forge("pr3", "c", "updated remotely\n", "upstream update")

    after_closure_plan(monkeypatch, change)

    result = repo.stack("push", "--prs", "--force", expect_success=False)

    assert "push failed, so nothing was pushed" in result.stderr
    assert "closed draft PR #2" in result.stderr
    assert "reopen with `gh pr reopen 2" in result.stderr
    assert gh.calls("pr", "close")
    assert gh.load()["prs"][1]["state"] == "CLOSED"
    assert repo.remote_subject("pr3") == "upstream update"


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_keeps_a_draft_tracked_under_remote_and_branch_aliases(repo: Repo, gh: FakeGitHub) -> None:
    repo.git("remote", "add", "mirror", repo.git("config", "--get", "remote.origin.url"))
    repo.git("fetch", "mirror")
    repo.git("branch", "--move", "pr2", "other-work")
    repo.git("branch", "--set-upstream-to=mirror/pr2", "other-work")
    repo.stack("track", "main", "other-work")

    result = repo.stack("prs", "--remote", "origin", "pr3", "--force", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize(("command", "needs_retarget"), [("push", True), ("prs", True), ("prs", False)])
def test_force_allows_selected_pull_requests_queued_after_planning(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, command: str, needs_retarget: bool
) -> None:
    if needs_retarget:
        state = gh.load()
        state["prs"][2]["baseRefName"] = "main"
        gh.save(state)

    def change() -> None:
        state = gh.load()
        state["prs"][2]["isInMergeQueue"] = True
        gh.save(state)

    after_closure_plan(monkeypatch, change)
    repo.stack(command, *(["--prs"] if command == "push" else []), "--force")
    assert gh.calls("pr", "close")
    assert gh.stacks() == [[1, 3]]


@pytest.mark.usefixtures("orphan")
def test_force_allows_queued_refs_outside_the_rebuilt_github_stack(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["stacks"][0]["prs"] = [2, 3]
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr1")
    repo.commit("a", "updated locally\n", "update pr1")
    before = repo.remote_subject("pr1")

    initial_protection = StackClient.protect_push

    def queue_after_protection(client: StackClient, *args: Any, **kwargs: Any) -> None:
        initial_protection(client, *args, **kwargs)
        state = gh.load()
        state["prs"][0]["isInMergeQueue"] = True
        gh.save(state)

    monkeypatch.setattr(StackClient, "protect_push", queue_after_protection)
    repo.stack("push", "--prs", "--force")
    assert gh.calls("pr", "close")
    assert repo.remote_subject("pr1") != before


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize("add_dependent", [False, True])
def test_multiple_closures_recheck_later_drafts(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, add_dependent: bool
) -> None:
    gh.add_pr(
        number=4,
        state="OPEN",
        isDraft=True,
        headRefName="orphan4",
        baseRefName="pr1",
        headRefOid=repo.git("rev-parse", "refs/heads/pr2"),
        mergeCommit=None,
    )
    state = gh.load()
    state["stacks"][0]["prs"].append(4)
    gh.save(state)
    respond = fake_gh.respond

    def close_then_change(args: list[str], state: dict[str, Any]) -> str | None:
        result = respond(args, state)
        if args[:3] == ["pr", "close", "2"]:
            state["stacks"][0]["prs"].remove(2)
            if add_dependent:
                state["prs"].append(dict(state["prs"][3], number=20, headRefName="external", baseRefName="orphan4"))
        return result

    monkeypatch.setattr(fake_gh, "respond", close_then_change)

    result = repo.stack("prs", "--force", expect_success=not add_dependent)

    assert [call[2] for call in gh.calls("pr", "close")] == (["2"] if add_dependent else ["2", "4"])
    assert "closed draft PR #2" in result.stdout
    if add_dependent:
        assert "open pull requests target its head orphan4" in result.stderr
        assert gh.load()["prs"][3]["state"] == "OPEN"
    else:
        assert gh.stacks() == [[1, 3]]


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize("command", ["push", "prs"])
def test_retarget_without_rebuild_reports_stack_base_and_draft(repo: Repo, gh: FakeGitHub, command: str) -> None:
    state = gh.load()
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)

    result = repo.stack(command, *(["--prs"] if command == "push" else []), expect_success=False)

    if command == "push":
        assert result.stderr.splitlines()[0] == "git stack: Nothing was pushed."
    assert "Retry with --force." in result.stderr
    assert "- https://github.com/acme/widgets/pull/2" in result.stderr
    assert "- Unstack GitHub stack 1000 (keep merged PRs linked)" in result.stderr
    assert "- Retarget #3 to pr1" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_unlinked_stack_warning_lists_each_draft_on_its_own_line(repo: Repo, gh: FakeGitHub) -> None:
    gh.add_pr(
        number=4,
        state="OPEN",
        isDraft=True,
        headRefName="orphan4",
        baseRefName="pr1",
        headRefOid=repo.git("rev-parse", "refs/heads/pr2"),
        mergeCommit=None,
    )
    state = gh.load()
    state["stacks"][0]["prs"].append(4)
    gh.save(state)

    result = repo.stack("prs", expect_success=False)

    assert "--force" in result.stderr
    assert "Unstack GitHub stack 1000" in result.stderr
    assert "Link #1, #3" in result.stderr
    assert [line.strip() for line in result.stderr.splitlines() if line.startswith("- https://")] == [
        "- https://github.com/acme/widgets/pull/2",
        "- https://github.com/acme/widgets/pull/4",
    ]
    (dependents,) = [call for call in gh.calls("api", "graphql") if any("base2=" in arg for arg in call)]
    assert "base4=orphan4" in dependents
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3, 4]]


@pytest.mark.usefixtures("orphan")
def test_unlinked_stack_warning_explains_ineligible_blocker(repo: Repo, gh: FakeGitHub) -> None:
    state = gh.load()
    state["prs"][1]["isDraft"] = False
    gh.save(state)

    result = repo.stack("prs", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert "must still be your own untracked draft" in result.stderr
    assert "Retry with --force" not in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_checks_draft_after_checking_other_prs(repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    respond = fake_gh.respond
    planned = False

    def change() -> None:
        nonlocal planned
        planned = True

    def mark_ready_during_protection(args: list[str], state: dict[str, Any]) -> str | None:
        query = next((arg for arg in args if arg.startswith("query=")), "")
        if planned and "p1: pullRequest" in query and "isInMergeQueue" in query:
            state["prs"][1]["isDraft"] = False
        return respond(args, state)

    monkeypatch.setattr(fake_gh, "respond", mark_ready_during_protection)
    after_closure_plan(monkeypatch, change)

    result = repo.stack("prs", "--force", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.load()["prs"][1]["isDraft"] is False
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_unlinked_stack_warning_distinguishes_eligible_and_ineligible_drafts(repo: Repo, gh: FakeGitHub) -> None:
    gh.add_pr(
        number=4,
        state="OPEN",
        isDraft=False,
        headRefName="ready4",
        baseRefName="pr1",
        headRefOid=repo.git("rev-parse", "refs/heads/pr2"),
        mergeCommit=None,
    )
    state = gh.load()
    state["stacks"][0]["prs"].append(4)
    gh.save(state)

    result = repo.stack("prs", expect_success=False)

    assert "can't close blocking PR #4" in result.stderr
    assert "No drafts will close." in result.stderr
    assert "- https://github.com/acme/widgets/pull/2" in result.stderr
    assert "Retry with --force" not in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_push_refuses_new_closure_candidates_after_protection(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["stacks"][0]["prs"] = [1, 3]
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    initial_protection = StackClient.protect_push

    def add_blocker(client: StackClient, *args: Any, **kwargs: Any) -> None:
        initial_protection(client, *args, **kwargs)
        state = gh.load()
        state["stacks"][0]["prs"].insert(1, 2)
        gh.save(state)

    monkeypatch.setattr(StackClient, "protect_push", add_blocker)

    result = repo.stack("push", "--prs", "--force", expect_success=False)

    assert "gained other open pull requests" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_push_refuses_new_blocker_after_publishing_parent(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["stacks"][0]["prs"] = [1, 3]
    gh.save(state)
    repo.git("switch", "pr1")
    repo.client.branch("new-parent")
    repo.commit("a", "new parent\n", "new parent")
    repo.git("switch", "pr3")
    repo.client.restack(onto="new-parent")
    respond = fake_gh.respond

    def add_blocker_at_retarget(args: list[str], state: dict[str, Any]) -> str | None:
        if args[:3] == ["pr", "edit", "3"] and "--base" in args:
            state["stacks"][0]["prs"].insert(1, 2)
        return respond(args, state)

    monkeypatch.setattr(fake_gh, "respond", add_blocker_at_retarget)

    result = repo.stack("push", "--prs", "--force", expect_success=False)

    assert "pushed, but updating pull requests failed" in result.stderr
    assert "gained other open pull requests" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.load()["prs"][2]["baseRefName"] == "pr1"
    assert repo.remote_subject("new-parent") == "new parent"


@pytest.mark.usefixtures("orphan")
def test_blocked_push_diagnoses_rebuild_before_checking_write_protection(repo: Repo, gh: FakeGitHub) -> None:
    repo.git("switch", "pr1")
    repo.client.branch("new-parent")
    repo.commit("a", "new parent\n", "new parent")
    repo.git("switch", "pr3")
    repo.client.restack(onto="new-parent")
    gh.clear_calls()

    result = repo.stack("push", "--prs", expect_success=False)

    assert "Retry with --force." in result.stderr
    assert "https://github.com/acme/widgets/pull/2" in result.stderr
    calls = gh.calls("api", "graphql")
    assert any("ref0:" in arg for call in calls for arg in call)
    assert any("viewer { login }" in arg for call in calls for arg in call)
    assert gh.calls("pr", "close") == []
    assert not repo.git("branch", "--list", "new-parent", cwd=repo.origin)
