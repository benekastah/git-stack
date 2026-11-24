"""Closing draft blockers is explicit and scoped to a GitHub stack rebuild."""

import io
from typing import Any

import pytest

from git_stack import commands
from git_stack.adapters.github import GitHubStack, OrphanedPullRequest
from git_stack.git import GitClient
from tests import fake_gh
from tests.helpers import FakeGitHub, Repo


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
def test_close_orphaned_draft_and_rebuild(repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    def confirm(candidates: object) -> bool:
        assert "pr2" in repr(candidates) and "pr1" in repr(candidates)
        return True

    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm, raising=False)
    before = repo.git("rev-parse", "refs/remotes/origin/pr2")

    repo.stack("prs", "--pr-rebuild")

    assert gh.load()["prs"][1]["state"] == "CLOSED"
    assert gh.stacks() == [[1, 3]]
    assert repo.git("rev-parse", "refs/remotes/origin/pr2") == before
    assert repo.git("rev-parse", "refs/heads/pr2") == before
    assert "--delete-branch" not in gh.calls("pr", "close")[0]


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_dry_run_previews_without_prompt_or_writes(repo: Repo, gh: FakeGitHub) -> None:
    result = repo.stack("prs", "--dry-run", "--pr-rebuild")

    assert "would close draft PR #2" in result.stdout
    assert "pr2 → pr1" in result.stdout
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_refuses_without_terminal(repo: Repo, gh: FakeGitHub) -> None:
    result = repo.stack_process("prs", "--pr-rebuild", expect_success=False)

    assert "needs an interactive terminal" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


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

    result = repo.stack("prs", "--pr-rebuild", expect_success=False)

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

    result = repo.stack("prs", "--pr-rebuild", expect_success=False)

    assert "open pull requests target its head pr2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_closing_a_blocker_requires_a_fresh_confirmation(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def confirm(_candidates: object) -> bool:
        state = gh.load()
        state["prs"][1]["headRefOid"] = "f" * 40
        gh.save(state)
        return True

    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm)

    result = repo.stack("prs", "--pr-rebuild", expect_success=False)

    assert "changed during confirmation" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize("approve", [False, True])
def test_push_closes_confirmed_blocker_before_retarget_and_publication(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, approve: bool
) -> None:
    state = gh.load()
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr3")
    repo.commit("c", "updated\n", "update pr3")
    before = repo.remote_subject("pr3")
    monkeypatch.setattr(commands, "confirm_orphaned_prs", lambda _: approve)

    result = repo.stack("push", "--pr-rebuild", expect_success=approve)

    assert gh.load()["prs"][1]["state"] == ("CLOSED" if approve else "OPEN")
    assert repo.remote_subject("pr3") == ("update pr3" if approve else before)
    if not approve:
        assert "wasn't confirmed" in result.stderr
        assert gh.calls("pr", "edit") == []


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_keeps_a_draft_tracked_in_another_stack(repo: Repo, gh: FakeGitHub) -> None:
    repo.stack("track", "main", "pr2")

    result = repo.stack("prs", "--pr-rebuild", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_confirmation_precedes_push_lease_check(repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    state = gh.load()
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr3")
    repo.commit("c", "updated locally\n", "update pr3")

    def confirm(_candidates: object) -> bool:
        repo.commit_on_forge("pr3", "c", "updated remotely\n", "upstream update")
        return True

    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm)

    result = repo.stack("push", "--pr-rebuild", expect_success=False)

    assert "push would fail, so nothing was pushed or changed" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.calls("pr", "edit") == []
    assert repo.remote_subject("pr3") == "upstream update"


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("answer", "accepted"), [("#7 3\n", True), ("3\n", False), ("\n", False)])
def test_confirmation_lists_each_pr_and_accepts_only_the_full_set(
    monkeypatch: pytest.MonkeyPatch, answer: str, accepted: bool
) -> None:
    stack = GitHubStack(1000, ())
    terminal = Terminal()
    monkeypatch.setattr("sys.stdin", Terminal(answer))
    monkeypatch.setattr("sys.stderr", terminal)
    candidates = [
        OrphanedPullRequest(3, stack, "https://github.com/acme/widgets/pull/3", "first", "main", "first-tip"),
        OrphanedPullRequest(7, stack, "https://github.com/acme/widgets/pull/7", "second", "main", "second-tip"),
    ]

    assert commands.confirm_orphaned_prs(candidates) is accepted

    output = terminal.getvalue()
    assert "https://github.com/acme/widgets/pull/3 (first → main)" in output
    assert "https://github.com/acme/widgets/pull/7 (second → main)" in output


@pytest.mark.usefixtures("orphan")
def test_close_orphaned_prs_keeps_a_draft_tracked_under_remote_and_branch_aliases(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("remote", "add", "mirror", repo.git("config", "--get", "remote.origin.url"))
    repo.git("fetch", "mirror")
    repo.git("branch", "--move", "pr2", "other-work")
    repo.git("branch", "--set-upstream-to=mirror/pr2", "other-work")
    repo.stack("track", "main", "other-work")
    monkeypatch.setattr(commands, "confirm_orphaned_prs", lambda _: True)

    result = repo.stack("prs", "--remote", "origin", "pr3", "--pr-rebuild", expect_success=False)

    assert "can't close blocking PR #2" in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
@pytest.mark.parametrize(("command", "needs_retarget"), [("push", True), ("prs", True), ("prs", False)])
def test_selected_pull_requests_are_rechecked_after_confirmation(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, command: str, needs_retarget: bool
) -> None:
    if needs_retarget:
        state = gh.load()
        state["prs"][2]["baseRefName"] = "main"
        gh.save(state)

    def confirm(_candidates: object) -> bool:
        state = gh.load()
        state["prs"][2]["isInMergeQueue"] = True
        gh.save(state)
        return True

    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm)
    result = repo.stack(command, "--pr-rebuild", expect_success=False)

    assert "merge queue" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_push_rechecks_queue_for_changed_refs_outside_the_rebuilt_github_stack(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["stacks"][0]["prs"] = [2, 3]
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "pr1")
    repo.commit("a", "updated locally\n", "update pr1")
    before = repo.remote_subject("pr1")

    def confirm(_candidates: object) -> bool:
        state = gh.load()
        state["prs"][0]["isInMergeQueue"] = True
        gh.save(state)
        return True

    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm)
    result = repo.stack("push", "--pr-rebuild", expect_success=False)

    assert "merge queue" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.calls("pr", "edit") == []
    assert repo.remote_subject("pr1") == before


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
    monkeypatch.setattr(commands, "confirm_orphaned_prs", lambda _: True)

    result = repo.stack("prs", "--pr-rebuild", expect_success=not add_dependent)

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
    assert result.stderr.removeprefix("git stack: ").splitlines()[-7:] == [
        "Retry with --pr-rebuild.",
        "",
        "Drafts to close after confirmation (keep branches)",
        "- https://github.com/acme/widgets/pull/2",
        "",
        "- Unstack GitHub stack 1000 (keep merged PRs linked)",
        "- Retarget #3 to pr1",
    ]
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

    result = repo.stack("prs")

    assert "--pr-rebuild" in result.stderr
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

    result = repo.stack("prs")

    assert "can't close blocking PR #2" in result.stderr
    assert "must still be your own untracked draft" in result.stderr
    assert "Retry with --pr-rebuild" not in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_checks_draft_after_checking_other_prs(repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    respond = fake_gh.respond
    confirmed = False

    def confirm(_candidates: object) -> bool:
        nonlocal confirmed
        confirmed = True
        return True

    def mark_ready_during_protection(args: list[str], state: dict[str, Any]) -> str | None:
        query = next((arg for arg in args if arg.startswith("query=")), "")
        if confirmed and "p1: pullRequest" in query and "isInMergeQueue" in query:
            state["prs"][1]["isDraft"] = False
        return respond(args, state)

    monkeypatch.setattr(fake_gh, "respond", mark_ready_during_protection)
    monkeypatch.setattr(commands, "confirm_orphaned_prs", confirm)

    result = repo.stack("prs", "--pr-rebuild", expect_success=False)

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

    result = repo.stack("prs")

    assert "can't close blocking PR #4" in result.stderr
    assert "No drafts will close." in result.stderr
    assert "- https://github.com/acme/widgets/pull/2" in result.stderr
    assert "Retry with --pr-rebuild" not in result.stderr
    assert gh.calls("pr", "close") == []


@pytest.mark.usefixtures("orphan")
def test_push_refuses_new_closure_candidates_after_the_dry_push(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = gh.load()
    state["stacks"][0]["prs"] = [1, 3]
    state["prs"][2]["baseRefName"] = "main"
    gh.save(state)
    original = GitClient.push_would_fail

    def add_blocker(git: GitClient, *args: str) -> str | None:
        result = original(git, *args)
        state = gh.load()
        state["stacks"][0]["prs"].insert(1, 2)
        gh.save(state)
        return result

    monkeypatch.setattr(GitClient, "push_would_fail", add_blocker)
    monkeypatch.setattr(commands, "confirm_orphaned_prs", lambda _: pytest.fail("prompted after the dry push"))

    result = repo.stack("push", "--pr-rebuild", expect_success=False)

    assert "gained other open pull requests" in result.stderr
    assert gh.calls("pr", "close") == []
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("orphan")
def test_push_never_prompts_to_close_new_blocker_after_publishing_parent(
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
    monkeypatch.setattr(commands, "confirm_orphaned_prs", lambda _: pytest.fail("prompted after publishing new-parent"))

    result = repo.stack("push", "--pr-rebuild", expect_success=False)

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

    assert "Retry with --pr-rebuild." in result.stderr
    assert "https://github.com/acme/widgets/pull/2" in result.stderr
    calls = gh.calls("api", "graphql")
    assert not any("ref0:" in arg for call in calls for arg in call)
    assert any("viewer { login }" in arg for call in calls for arg in call)
    assert len(calls) == len(gh.calls("api")) == 4
    assert gh.calls("pr", "close") == []
    assert not repo.git("branch", "--list", "new-parent", cwd=repo.origin)
