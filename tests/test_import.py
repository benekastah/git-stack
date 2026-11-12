"""git stack import, with tests/fake_gh.py standing in for gh."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import BranchStatus, ImportAction, ImportStep, StackClient
from tests.helpers import FakeGitHub, Repo

SOMEONE = "Someone <someone@example.com>"


def push_branch(repo: Repo, branch: str, base: str, author: str | None = None) -> None:
    """Push branch, with one commit on base (by author, or else the user),
    from another clone."""
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-C", branch, f"origin/{base}", cwd=repo.forge)
    (repo.forge / branch).write_text(f"{branch}\n")
    repo.git("add", branch, cwd=repo.forge)
    repo.git("commit", "--quiet", *([f"--author={author}"] if author else []), "-m", f"{branch} c1", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", branch, cwd=repo.forge)


def open_pr(repo: Repo, gh: FakeGitHub, number: int, branch: str, base: str, author: str = "me") -> None:
    """Push branch, with one commit on base (by the user, or by someone else
    for another author), and open pull request number of it into base."""
    push_branch(repo, branch, base, author=None if author == "me" else SOMEONE)
    gh.add_pr(
        number=number,
        state="OPEN",
        author=author,
        headRefName=branch,
        baseRefName=base,
        headRefOid=repo.git("rev-parse", "HEAD", cwd=repo.forge),
        mergeCommit=None,
    )


def actions(steps: list[ImportStep]) -> list[tuple[str, ImportAction]]:
    return [(step.branch, step.action) for step in steps]


def test_import_creates_and_tracks_branches_of_open_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")

    result = client.import_stacks()

    assert [(s.branch, s.parent, s.number, s.action) for s in result.steps] == [
        ("pr1", "main", 1, ImportAction.CREATE),
        ("pr2", "pr1", 2, ImportAction.CREATE),
    ]
    assert [(b.name, b.pull_request and b.pull_request.number) for b in result.branches] == [
        ("main", None),
        ("pr1", 1),
        ("pr2", 2),
    ]
    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    repo.assert_stacked("pr2", "pr1", ["pr2 c1"])
    assert repo.git("rev-parse", "--abbrev-ref", "pr2@{upstream}") == "origin/pr2"
    assert [b.status for b in client.info()] == [None, BranchStatus.UP_TO_DATE, BranchStatus.UP_TO_DATE]
    assert not repo.config_exists("pr1", "stackOwnership")
    assert not repo.config_exists("pr2", "stackOwnership")


def test_import_marks_a_stack_with_another_contributor_shared(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main", author="alice")
    open_pr(repo, gh, 2, "pr2", "pr1")

    client.import_stacks(force=True)
    repo.git("switch", "--quiet", "pr2")

    assert repo.config("pr1", "stackOwnership") == "shared"
    assert repo.config("pr2", "stackOwnership") == "shared"
    with pytest.raises(StackError, match="--allow-shared"):
        client.restack()


def test_import_marks_someone_elses_stack_shared(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main", author="alice")

    client.import_stacks(force=True, author="alice")

    assert repo.config("pr1", "stackOwnership") == "shared"


def test_import_keeps_a_branch_targeted_by_merged_prs_as_a_root(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    push_branch(repo, "release", "main")
    gh.add_pr(
        number=1,
        state="MERGED",
        author="me",
        headRefName="old",
        baseRefName="release",
        headRefOid=repo.git("rev-parse", "HEAD", cwd=repo.forge),
        mergeCommit=None,
    )
    open_pr(repo, gh, 2, "feature", "release")

    preview = client.import_stacks(dry_run=True)
    assert preview.without_prs[0].action is ImportAction.CREATE_ROOT
    assert not client.git.local_branch_exists("release")
    result = client.import_stacks()

    assert result.without_prs[0].action is ImportAction.CREATE_ROOT
    assert repo.git("rev-parse", "refs/heads/release") == repo.git("rev-parse", "refs/remotes/origin/release")
    assert "release" not in client.tracked_branches()
    assert client.tracked_branches()["feature"] == "release"


def test_import_says_what_its_doing_before_the_slow_parts(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    open_pr(repo, gh, 1, "pr1", "main")

    client.import_stacks()

    assert emitted[:2] == [events.LookingUpPullRequests(), events.Fetching("origin")]


def test_import_dry_run_changes_nothing(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")

    steps = client.import_stacks(dry_run=True).steps

    assert [s.action for s in steps] == [ImportAction.CREATE, ImportAction.CREATE]
    assert repo.git("branch", "--list", "pr1", "pr2") == ""
    assert client.tracked_branches() == {}


def test_import_tracks_existing_local_branch_and_sets_its_upstream(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--no-track", "pr1", "origin/pr1")

    steps = client.import_stacks().steps

    assert actions(steps) == [("pr1", ImportAction.TRACK)]
    repo.assert_stacked("pr1", "main", ["pr1 c1"])
    assert repo.git("rev-parse", "--abbrev-ref", "pr1@{upstream}") == "origin/pr1"


def test_import_fast_forwards_main_before_finding_bases(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.commit_on_forge("main", "d", "main\n", "main c1")
    open_pr(repo, gh, 1, "pr1", "main")  # built on the new origin/main, which main lacks

    client.import_stacks()

    repo.assert_stacked("pr1", "main", ["pr1 c1"])


def test_import_fast_forwards_main_for_a_branch_without_pr_on_it(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.commit_on_forge("main", "d", "main\n", "main c1")
    push_branch(repo, "a", "main")  # built on the new origin/main, which main lacks
    open_pr(repo, gh, 1, "pr1", "a")

    client.import_stacks()

    repo.assert_stacked("a", "main", ["a c1"])


def test_import_leaves_tracked_branches_alone(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    open_pr(repo, gh, 2, "pr2", "pr1")
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--track", "pr1", "origin/pr1")
    repo.git("branch", "--quiet", "--track", "pr2", "origin/pr2")
    client.track("main", ["pr1"])
    client.track("main", ["pr2"])

    steps = client.import_stacks().steps

    assert actions(steps) == [("pr1", ImportAction.ALREADY_TRACKED), ("pr2", ImportAction.PARENT_DIFFERS)]
    assert steps[1].tracked_parent == "main"
    assert client.tracked_branches() == {"pr1": "main", "pr2": "main"}


def test_import_ignores_other_peoples_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "theirs", "main", author="someone")

    assert client.import_stacks().steps == []
    assert repo.git("branch", "--list", "theirs") == ""


def test_import_skips_pr_into_branch_that_isnt_local(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "theirs", "main", author="someone")
    open_pr(repo, gh, 2, "pr2", "theirs")
    open_pr(repo, gh, 3, "pr3", "pr2")

    steps = client.import_stacks().steps

    assert actions(steps) == [("pr2", ImportAction.SKIP), ("pr3", ImportAction.SKIP)]
    assert "theirs, which it merges into, isn't a local branch" in (steps[0].reason or "")
    assert repo.git("branch", "--list", "pr2", "pr3") == ""


def test_import_skips_pr_into_someone_elses_branch_without_a_pr(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    push_branch(repo, "theirs", "main", author=SOMEONE)
    open_pr(repo, gh, 1, "pr1", "theirs")

    result = client.import_stacks()

    assert actions(result.steps) == [("pr1", ImportAction.SKIP)]
    assert [(s.branch, s.parent, s.action) for s in result.without_prs] == [("theirs", "main", ImportAction.SKIP)]
    assert "commits by someone@example.com; import it with -f" in (result.steps[0].reason or "")
    assert repo.git("branch", "--list", "theirs", "pr1") == ""


def test_import_tracks_my_branches_without_prs_that_prs_are_built_on(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    push_branch(repo, "a", "main")
    push_branch(repo, "b", "a")
    open_pr(repo, gh, 1, "pr1", "b")

    result = client.import_stacks()

    assert [(s.branch, s.parent, s.action) for s in result.without_prs] == [
        ("a", "main", ImportAction.CREATE),
        ("b", "a", ImportAction.CREATE),
    ]
    assert actions(result.steps) == [("pr1", ImportAction.CREATE)]
    assert [b.name for b in result.branches] == ["main", "a", "b", "pr1"]
    repo.assert_stacked("a", "main", ["a c1"])
    repo.assert_stacked("b", "a", ["b c1"])
    repo.assert_stacked("pr1", "b", ["pr1 c1"])
    assert repo.git("rev-parse", "--abbrev-ref", "a@{upstream}") == "origin/a"


def test_import_finds_branch_without_pr_built_on_a_pr(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    push_branch(repo, "middle", "pr1")
    open_pr(repo, gh, 2, "pr2", "middle")

    result = client.import_stacks()

    assert [(s.branch, s.parent, s.action) for s in result.without_prs] == [("middle", "pr1", ImportAction.CREATE)]
    assert actions(result.steps) == [("pr1", ImportAction.CREATE), ("pr2", ImportAction.CREATE)]
    repo.assert_stacked("middle", "pr1", ["middle c1"])
    repo.assert_stacked("pr2", "middle", ["pr2 c1"])


def test_import_skips_branch_without_pr_on_one_of_several_branches(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    push_branch(repo, "a", "main")
    repo.git("push", "--quiet", "origin", "a:refs/heads/a-copy", cwd=repo.forge)
    push_branch(repo, "b", "a")
    open_pr(repo, gh, 1, "pr1", "b")

    result = client.import_stacks()

    assert [(s.branch, s.action, s.reason) for s in result.without_prs] == [
        ("b", ImportAction.SKIP, "it could be built on any of a, a-copy")
    ]
    assert actions(result.steps) == [("pr1", ImportAction.SKIP)]
    assert repo.git("branch", "--list", "a", "b", "pr1") == ""


def test_import_dry_run_creates_no_branches_without_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.commit_on_forge("main", "d", "main\n", "main c1")  # by someone else; main lacks it
    push_branch(repo, "a", "main")
    open_pr(repo, gh, 1, "pr1", "a")

    result = client.import_stacks(dry_run=True)

    assert [(s.branch, s.parent, s.action) for s in result.without_prs] == [("a", "main", ImportAction.CREATE)]
    assert repo.git("branch", "--list", "a", "pr1") == ""


def test_import_needs_force_for_another_authors_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "api", "main", author="someone")

    steps = client.import_stacks(author="someone").steps

    assert [(s.branch, s.action, s.reason) for s in steps] == [
        ("api", ImportAction.SKIP, "it's someone's pull request; import it with -f")
    ]
    assert repo.git("branch", "--list", "api") == ""


def test_import_with_force_tracks_another_authors_stack(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "api", "main", author="someone")
    open_pr(repo, gh, 2, "use-api", "api", author="someone")

    result = client.import_stacks(author="someone", force=True)

    assert [(s.branch, s.action, s.other_authors) for s in result.steps] == [
        ("api", ImportAction.CREATE, ["someone"]),
        ("use-api", ImportAction.CREATE, ["someone"]),
    ]
    repo.assert_stacked("api", "main", ["api c1"])
    repo.assert_stacked("use-api", "api", ["use-api c1"])
    assert repo.emitted[0] == events.LookingUpPullRequests("someone")


def test_import_dry_run_shows_what_force_would_do(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "api", "main", author="someone")

    steps = client.import_stacks(author="someone", dry_run=True).steps

    assert [(s.branch, s.action, s.other_authors) for s in steps] == [("api", ImportAction.CREATE, ["someone"])]
    assert repo.git("branch", "--list", "api") == ""


def test_import_with_force_tracks_someone_elses_branch_without_a_pr(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    push_branch(repo, "theirs", "main", author=SOMEONE)
    open_pr(repo, gh, 1, "pr1", "theirs")

    result = client.import_stacks(force=True)

    assert [(s.branch, s.action, s.other_authors) for s in result.without_prs] == [
        ("theirs", ImportAction.CREATE, ["someone@example.com"])
    ]
    repo.assert_stacked("theirs", "main", ["theirs c1"])
    repo.assert_stacked("pr1", "theirs", ["pr1 c1"])


def test_import_skips_another_authors_pr_named_like_my_own_branch(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.git("switch", "--quiet", "-c", "api")
    repo.commit("a", "mine\n", "my api")
    open_pr(repo, gh, 1, "api", "main", author="someone")

    steps = client.import_stacks(author="someone", force=True).steps

    assert [(s.branch, s.action, s.reason) for s in steps] == [
        ("api", ImportAction.SKIP, "your local api isn't origin/api's branch")
    ]
    assert not repo.config_exists("api", "stackParent")


@pytest.mark.parametrize(
    ("branches", "prs"),
    [
        (["use-api"], []),
        ([], ["2"]),
        ([], ["https://github.com/acme/widgets/pull/1/files"]),
        ([], ["https://github.com/ACME/Widgets/pull/2"]),
    ],
)
def test_import_only_the_named_stack_trees(
    repo: Repo, gh: FakeGitHub, client: StackClient, branches: list[str], prs: list[str]
) -> None:
    open_pr(repo, gh, 1, "api", "main")
    open_pr(repo, gh, 2, "use-api", "api")
    open_pr(repo, gh, 3, "other", "main")

    result = client.import_stacks(branches=branches, prs=prs)

    assert actions(result.steps) == [("api", ImportAction.CREATE), ("use-api", ImportAction.CREATE)]
    assert repo.git("branch", "--list", "other") == ""


def test_import_finds_the_author_of_a_named_pr(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "mine", "main")
    open_pr(repo, gh, 2, "api", "main", author="someone")

    result = client.import_stacks(prs=["2"], force=True)

    assert [(s.branch, s.action, s.other_authors) for s in result.steps] == [("api", ImportAction.CREATE, ["someone"])]
    assert repo.git("branch", "--list", "mine") == ""


def test_import_takes_a_branch_named_like_a_number_as_a_branch(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "24", "main")
    open_pr(repo, gh, 2, "other", "main")

    result = client.import_stacks(branches=["24"])

    assert actions(result.steps) == [("24", ImportAction.CREATE)]


def test_import_refuses_a_branch_in_no_stack(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "api", "main")

    with pytest.raises(StackError, match="nope isn't the branch of an open pull request it looked up"):
        client.import_stacks(branches=["nope"])

    assert repo.git("branch", "--list", "api") == ""


def test_import_refuses_a_pr_that_isnt_open(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    with pytest.raises(StackError, match="^#7 isn't an open pull request$"):
        client.import_stacks(prs=["7"])


@pytest.mark.parametrize(
    "pr", ["#7", "https://github.com/acme/other/pull/7", "https://example.com/acme/widgets/pull/7", "pull/7"]
)
def test_import_refuses_what_isnt_a_pr_number_or_url_of_the_repository(
    repo: Repo, gh: FakeGitHub, client: StackClient, pr: str
) -> None:
    with pytest.raises(StackError, match="isn't a pull request number, or the URL of a pull request in"):
        client.import_stacks(prs=[pr])


def test_import_refuses_an_author_that_isnt_a_github_username(client: StackClient) -> None:
    with pytest.raises(StackError, match="isn't a GitHub username"):
        client.import_stacks(author="someone is:closed")


def test_import_refuses_without_github(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    repo.git("config", "stack.adapter", "git")

    with pytest.raises(StackError, match="import finds stacks from pull requests on GitHub"):
        client.import_stacks()


def test_import_refuses_when_lookup_fails(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    open_pr(repo, gh, 1, "pr1", "main")
    gh.fail("api")

    with pytest.raises(StackError, match="couldn't look up pull requests"):
        client.import_stacks()

    assert repo.git("branch", "--list", "pr1") == ""
