"""git stack prs, with tests/fake_gh.py standing in for gh."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import PullRequestAction, PullRequestStep, StackClient
from tests.helpers import FakeGitHub, Repo

# Without a trailing newline, which git-stack strips like the rest of git's output.
TEMPLATE = "## What\n\n## Why"


def add_template(repo: Repo, path: str, content: str = TEMPLATE) -> None:
    """Commit a pull request template at path to main, and push it."""
    (repo.path / path).parent.mkdir(parents=True, exist_ok=True)
    repo.write(path, f"{content}\n")
    repo.git("add", path)
    repo.git("commit", "--quiet", "-m", f"add {path}")
    repo.git("push", "--quiet")


def opened(gh: FakeGitHub) -> list[tuple[str, str, str, str, bool]]:
    """(head, base, title, body, draft) of each pull request gh opened."""
    return [(pr["headRefName"], pr["baseRefName"], pr["title"], pr["body"], pr["draft"])
            for pr in gh.load()["prs"] if "title" in pr]


def test_prs_opens_drafts_for_the_whole_stack_into_stack_parents(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    add_template(repo, ".github/PULL_REQUEST_TEMPLATE.md")
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr2")

    steps = client.prs()

    assert steps == [
        PullRequestStep("pr1", "main", PullRequestAction.OPEN, "pr1 c1", 1, "https://github.com/acme/widgets/pull/1"),
        PullRequestStep("pr2", "pr1", PullRequestAction.OPEN, "pr2 c1", 2, "https://github.com/acme/widgets/pull/2"),
        PullRequestStep("pr3", "pr2", PullRequestAction.OPEN, "pr3 c1", 3, "https://github.com/acme/widgets/pull/3")]
    assert opened(gh) == [("pr1", "main", "pr1 c1", TEMPLATE, True),
                          ("pr2", "pr1", "pr2 c1", TEMPLATE, True),
                          ("pr3", "pr2", "pr3 c1", TEMPLATE, True)]


def test_prs_skips_branches_with_a_pull_request(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(number=7, state="OPEN", headRefName="pr2", headRefOid=repo.git("rev-parse", "pr2"),
              baseRefName="pr1", mergeCommit=None)

    steps = client.prs()

    assert [(s.branch, s.action, s.number) for s in steps] == [
        ("pr1", PullRequestAction.OPEN, 8), ("pr2", PullRequestAction.EXISTS, 7), ("pr3", PullRequestAction.OPEN, 9)]
    assert [head for head, *_ in opened(gh)] == ["pr1", "pr3"]


def test_prs_skips_branches_without_commits(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("empty")
    client.push()

    assert client.prs() == [PullRequestStep("empty", "main", PullRequestAction.NO_COMMITS)]
    assert opened(gh) == []


@pytest.mark.parametrize("path", ["pull_request_template.md", "docs/Pull_Request_Template.md"])
def test_prs_finds_the_template_where_github_does(
        repo: Repo, gh: FakeGitHub, client: StackClient, path: str) -> None:
    add_template(repo, path)
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.push()

    client.prs()

    assert [body for *_, body, _ in opened(gh)] == [TEMPLATE]


def test_prs_ignores_named_templates(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    add_template(repo, ".github/PULL_REQUEST_TEMPLATE/team.md")
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.push()

    client.prs()

    assert [body for *_, body, _ in opened(gh)] == [""]


def test_prs_takes_the_template_from_the_remote_default_branch(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    add_template(repo, ".github/pull_request_template.md")
    client.branch("pr1")
    repo.commit(".github/pull_request_template.md", "changed on pr1\n", "pr1 c1")
    client.push()

    client.prs()

    assert [body for *_, body, _ in opened(gh)] == [TEMPLATE]


def test_prs_refuses_unless_every_branch_matches_its_remote_branch(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit("c", "pr3 again\n", "pr3 c2")
    repo.git("switch", "--quiet", "pr1")
    client.branch("unpushed")
    repo.commit("b", "unpushed\n", "unpushed c1")

    with pytest.raises(StackError, match="differ from their remote branches: pr3, unpushed; run `git stack push`"):
        client.prs()
    assert opened(gh) == []


def test_prs_dry_run_opens_nothing(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()

    steps = client.prs(dry_run=True)

    assert [(s.branch, s.action, s.title, s.number) for s in steps] == [
        ("pr1", PullRequestAction.OPEN, "pr1 c1", None), ("pr2", PullRequestAction.OPEN, "pr2 c1", None),
        ("pr3", PullRequestAction.OPEN, "pr3 c1", None)]
    assert gh.calls("pr") == []


def test_prs_says_which_it_opened_when_one_fails(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.fail_opening("pr2")

    with pytest.raises(StackError, match=r"opening a pull request for pr2 failed \(after opening #1 for pr1\): "):
        client.prs()
    assert [head for head, *_ in opened(gh)] == ["pr1"]


def test_prs_needs_github(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")

    with pytest.raises(StackError, match="prs opens pull requests on GitHub"):
        client.prs()
