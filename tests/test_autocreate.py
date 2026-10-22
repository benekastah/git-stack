"""git stack autocreate, with tests/fake_gh.py standing in for gh."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import AutocreateAction, Autocreated, BranchStatus, StackClient
from tests.helpers import FakeGitHub, Repo


def open_pr(repo: Repo, gh: FakeGitHub, number: int, branch: str, base: str, author: str = "me") -> None:
    """Push branch, with one commit on base, from another clone, and open
    pull request number of it into base."""
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-C", branch, f"origin/{base}", cwd=repo.forge)
    (repo.forge / branch).write_text(f"{branch}\n")
    repo.git("add", branch, cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", f"{branch} c1", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", branch, cwd=repo.forge)
    gh.add_pr(
        number=number,
        state="OPEN",
        author=author,
        headRefName=branch,
        baseRefName=base,
        headRefOid=repo.git("rev-parse", "HEAD", cwd=repo.forge),
        mergeCommit=None,
    )


def actions(steps: list[Autocreated]) -> list[tuple[str, AutocreateAction]]:
    return [(step.branch, step.action) for step in steps]


def test_autocreate_creates_and_tracks_branches_of_open_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")

    steps = client.autocreate()

    assert [(s.branch, s.parent, s.number, s.action) for s in steps] == [
        ("pr1", "main", 1, AutocreateAction.CREATE),
        ("pr2", "pr1", 2, AutocreateAction.CREATE),
    ]
    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    assert repo.git("rev-parse", "--abbrev-ref", "pr2@{upstream}") == "origin/pr2"
    assert [b.status for b in client.info()] == [None, BranchStatus.UP_TO_DATE, BranchStatus.UP_TO_DATE]


def test_autocreate_says_what_its_doing_before_the_slow_parts(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    open_pr(repo, gh, 1, "pr1", "main")

    client.autocreate()

    assert emitted[:2] == [events.LookingUpPullRequests(), events.Fetching("origin")]


def test_autocreate_dry_run_changes_nothing(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")

    steps = client.autocreate(dry_run=True)

    assert [s.action for s in steps] == [AutocreateAction.CREATE, AutocreateAction.CREATE]
    assert repo.git("branch", "--list", "pr1", "pr2") == ""
    assert client.tracked_branches() == {}


def test_autocreate_tracks_existing_local_branch_and_sets_its_upstream(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--no-track", "pr1", "origin/pr1")

    steps = client.autocreate()

    assert actions(steps) == [("pr1", AutocreateAction.TRACK)]
    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    assert repo.git("rev-parse", "--abbrev-ref", "pr1@{upstream}") == "origin/pr1"


def test_autocreate_fast_forwards_main_before_finding_bases(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.commit_on_forge("main", "d", "main\n", "main c1")
    open_pr(repo, gh, 1, "pr1", "main")  # built on the new origin/main, which main lacks

    client.autocreate()

    repo.assert_stacked("pr1", "main", ["pr1 c1"])


def test_autocreate_leaves_tracked_branches_alone(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--track", "pr1", "origin/pr1")
    repo.git("branch", "--quiet", "--track", "pr2", "origin/pr2")
    client.track("main", ["pr1"])
    client.track("main", ["pr2"])

    steps = client.autocreate()

    assert actions(steps) == [("pr1", AutocreateAction.ALREADY_TRACKED), ("pr2", AutocreateAction.PARENT_DIFFERS)]
    assert steps[1].tracked_parent == "main"
    assert client.tracked_branches() == {"pr1": "main", "pr2": "main"}


def test_autocreate_ignores_other_peoples_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "theirs", "main", author="someone")

    assert client.autocreate() == []
    assert repo.git("branch", "--list", "theirs") == ""


def test_autocreate_skips_pr_into_branch_that_isnt_local(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "theirs", "main", author="someone")
    open_pr(repo, gh, 2, "pr2", "theirs")
    open_pr(repo, gh, 3, "pr3", "pr2")

    steps = client.autocreate()

    assert actions(steps) == [("pr2", AutocreateAction.SKIP), ("pr3", AutocreateAction.SKIP)]
    assert "theirs, which it merges into, isn't a local branch" in (steps[0].reason or "")
    assert repo.git("branch", "--list", "pr2", "pr3") == ""


def test_autocreate_refuses_without_github(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    repo.git("config", "stack.adapter", "git")

    with pytest.raises(StackError, match="autocreate finds stacks from pull requests on GitHub"):
        client.autocreate()


def test_autocreate_refuses_when_lookup_fails(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    gh.fail("api")

    with pytest.raises(StackError, match="couldn't look up your pull requests"):
        client.autocreate()

    assert repo.git("branch", "--list", "pr1") == ""
