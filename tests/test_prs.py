"""git stack prs, with tests/fake_gh.py standing in for gh."""

import pytest

from git_stack import events, stack_section
from git_stack.errors import StackError
from git_stack.stack import BranchStatus, PullRequestAction, PullRequestStep, StackClient
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


def stack(*lines: str) -> str:
    """The stack section showing lines, each a list item."""
    return "\n".join(["<!-- git-stack -->", stack_section.NOTE, "---", "**Stack**", *lines, "<!-- /git-stack -->"])


def body(gh: FakeGitHub, number: int) -> str:
    return str(next(pr for pr in gh.load()["prs"] if pr["number"] == number)["body"])


def opened(gh: FakeGitHub) -> list[tuple[str, str, str, str, bool]]:
    """(head, base, title, body, draft) of each pull request gh opened."""
    return [
        (pr["headRefName"], pr["baseRefName"], pr["title"], pr["body"], pr["isDraft"])
        for pr in gh.load()["prs"]
        if "title" in pr
    ]


def test_prs_opens_drafts_for_the_whole_stack_into_stack_parents(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    add_template(repo, ".github/PULL_REQUEST_TEMPLATE.md")
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr2")

    result = client.prs()

    assert result.steps == [
        PullRequestStep("pr1", "main", PullRequestAction.OPEN, "pr1 c1", 1, "https://github.com/acme/widgets/pull/1"),
        PullRequestStep("pr2", "pr1", PullRequestAction.OPEN, "pr2 c1", 2, "https://github.com/acme/widgets/pull/2"),
        PullRequestStep("pr3", "pr2", PullRequestAction.OPEN, "pr3 c1", 3, "https://github.com/acme/widgets/pull/3"),
    ]
    # The pull requests it just opened, without looking them up again.
    assert [(b.name, b.pull_request and b.pull_request.number) for b in result.branches] == [
        ("main", None),
        ("pr1", 1),
        ("pr2", 2),
        ("pr3", 3),
    ]
    assert [pr[:3] + pr[4:] for pr in opened(gh)] == [
        ("pr1", "main", "pr1 c1", True),
        ("pr2", "pr1", "pr2 c1", True),
        ("pr3", "pr2", "pr3 c1", True),
    ]
    assert body(gh, 2) == f"{TEMPLATE}\n\n" + stack("- `main`", "  - #1", "    - #2 👈", "      - #3")
    assert [body(gh, n).count("👈") for n in (1, 3)] == [1, 1]
    assert "- #1 👈" in body(gh, 1) and "- #3 👈" in body(gh, 3)


def test_prs_skips_branches_with_a_pull_request(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )

    steps = client.prs().steps

    assert [(s.branch, s.action, s.number) for s in steps] == [
        ("pr1", PullRequestAction.OPEN, 8),
        ("pr2", PullRequestAction.EXISTS, 7),
        ("pr3", PullRequestAction.OPEN, 9),
    ]
    assert [head for head, *_ in opened(gh)] == ["pr1", "pr3"]


def test_prs_retargets_existing_pull_requests(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    # Opened from the link GitHub prints on push, which targets the default branch.
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="main",
        mergeCommit=None,
    )

    client.prs()

    assert events.PullRequestRetargeted("pr2", 7, "pr1") in emitted
    assert gh.load()["prs"][0]["baseRefName"] == "pr1"


def test_prs_skips_branches_without_commits(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("empty")
    client.push()

    assert client.prs().steps == [PullRequestStep("empty", "main", PullRequestAction.NO_COMMITS)]
    assert opened(gh) == []


@pytest.mark.parametrize("path", ["pull_request_template.md", "docs/Pull_Request_Template.md"])
def test_prs_finds_the_template_where_github_does(repo: Repo, gh: FakeGitHub, client: StackClient, path: str) -> None:
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


def test_prs_takes_the_template_from_the_remote_default_branch(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    add_template(repo, ".github/pull_request_template.md")
    client.branch("pr1")
    repo.commit(".github/pull_request_template.md", "changed on pr1\n", "pr1 c1")
    client.push()

    client.prs()

    assert [body for *_, body, _ in opened(gh)] == [TEMPLATE]


def test_prs_refuses_unless_every_branch_matches_its_remote_branch(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
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

    steps = client.prs(dry_run=True).steps

    assert [(s.branch, s.action, s.title, s.number) for s in steps] == [
        ("pr1", PullRequestAction.OPEN, "pr1 c1", None),
        ("pr2", PullRequestAction.OPEN, "pr2 c1", None),
        ("pr3", PullRequestAction.OPEN, "pr3 c1", None),
    ]
    assert gh.calls("pr") == []


def test_prs_dry_run_retargets_nothing(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="main",
        mergeCommit=None,
    )

    client.prs(dry_run=True)

    assert gh.load()["prs"][0]["baseRefName"] == "main"


def test_prs_says_which_it_opened_when_one_fails(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.fail_opening("pr2")

    with pytest.raises(StackError, match=r"opening a pull request for pr2 failed \(after opening #1 for pr1\): "):
        client.prs()
    assert [head for head, *_ in opened(gh)] == ["pr1"]


def test_prs_adds_new_pull_requests_to_the_stack_of_existing_ones(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.push()
    client.prs()
    assert body(gh, 1) == ""  # a stack of one pull request gets no section
    client.branch("pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    client.push()

    client.prs()

    assert body(gh, 1) == stack("- `main`", "  - #1 👈", "    - #2")


def test_prs_keeps_merged_pull_requests_in_the_stack(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    repo.squash_merge_on_forge("pr1")
    state = gh.load()
    state["prs"][0] |= {
        "state": "MERGED",
        "headRefOid": repo.git("rev-parse", "pr1"),
        "mergeCommit": {"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    }
    gh.save(state)
    client.sync()
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")
    client.push()

    client.prs()

    assert body(gh, 2) == stack("- `main`", "  - #1 (merged)", "  - #2 👈", "    - #3", "      - #4")
    assert "#4" not in body(gh, 1)  # merged pull requests are left alone


def test_prs_shows_a_merged_branch_it_skipped(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    repo.squash_merge_on_forge("pr1")
    state = gh.load()
    state["prs"][0] |= {
        "state": "MERGED",
        "headRefOid": repo.git("rev-parse", "pr1"),
        "mergeCommit": {"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    }
    gh.save(state)
    repo.git("fetch", "--quiet")
    repo.git("branch", "--quiet", "--force", "main", "origin/main")

    branches = client.prs().branches

    assert [(b.name, b.status) for b in branches] == [
        ("main", None),
        ("pr1", BranchStatus.MERGED),
        ("pr2", BranchStatus.UP_TO_DATE),
        ("pr3", BranchStatus.UP_TO_DATE),
    ]


def test_prs_moves_the_stack_to_the_end_keeping_the_rest(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    state["prs"][0]["body"] = f"{body(gh, 1)}\n\nadded later"
    gh.save(state)
    gh.clear_calls()

    client.prs()

    assert body(gh, 1) == "added later\n\n" + stack("- `main`", "  - #1 👈", "    - #2", "      - #3")
    assert [call[2] for call in gh.calls("pr", "edit")] == ["1"]  # the rest were up to date


def test_prs_needs_github(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")

    with pytest.raises(StackError, match="prs opens pull requests on GitHub"):
        client.prs()


def test_prs_opens_pull_requests_for_the_stacks_named(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "main")
    client.branch("solo")
    repo.commit("c", "solo\n", "solo c1")
    client.push()
    repo.git("switch", "--quiet", "main")

    client.prs(["pr2"])

    assert [head for head, *_ in opened(gh)] == ["pr1", "pr2", "pr3"]


def test_prs_links_a_linear_stack_on_github(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()

    client.prs()

    assert gh.stacks() == [[1, 2, 3]]
    assert events.GitHubStackLinked((1, 2, 3)) in emitted
    # GitHub shows the stack on each pull request itself.
    assert [body(gh, n) for n in (1, 2, 3)] == ["", "", ""]


def test_prs_adds_new_pull_requests_to_the_top_of_the_stack_on_github(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")
    client.push()

    client.prs()

    assert gh.stacks() == [[1, 2, 3, 4]]


def test_prs_adds_to_a_stack_on_github_whose_bottom_merged(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    repo.squash_merge_on_forge("pr1")
    state = gh.load()
    state["prs"][0] |= {
        "state": "MERGED",
        "headRefOid": repo.git("rev-parse", "pr1"),
        "mergeCommit": {"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    }
    state["prs"][1]["baseRefName"] = "main"  # as GitHub does for the next pull request in a stack
    gh.save(state)
    client.sync()
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")
    client.push()

    client.prs()

    assert gh.stacks() == [[1, 2, 3, 4]]


def test_prs_removes_the_stack_section_once_the_stack_is_on_github(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    client.prs()  # without stacks on GitHub, so with sections
    assert stack_section.START in body(gh, 1)
    gh.enable_stacks()

    client.prs()

    assert [body(gh, n) for n in (1, 2, 3)] == ["", "", ""]


def test_prs_doesnt_link_a_branching_stack_on_github(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    gh.enable_stacks()
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.branch("pr2a")
    repo.commit("b", "pr2a\n", "pr2a c1")
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")
    repo.commit("c", "pr2b\n", "pr2b c1")
    client.push()

    client.prs()

    assert gh.stacks() == []
    assert body(gh, 1) == stack("- `main`", "  - #1 👈", "    - #2", "    - #3")


def test_prs_doesnt_link_on_github_when_turned_off(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    gh.enable_stacks()
    repo.git("config", "stack.githubStacks", "false")
    repo.make_stack()
    client.push()

    client.prs()

    assert gh.stacks() == []
    assert stack_section.START in body(gh, 1)


def test_prs_warns_when_a_stack_on_github_doesnt_match(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    gh.enable_stacks([2, 1])  # linked by hand, in the wrong order

    client.prs()

    assert gh.stacks() == [[2, 1]]
    assert events.GitHubStackMismatch((1, 2, 3), (1000,)) in emitted
    assert stack_section.START in body(gh, 3)


def test_prs_warns_when_a_stack_on_github_is_in_another_order(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    gh.enable_stacks([1, 3, 2])

    client.prs()

    assert events.GitHubStackMismatch((1, 2, 3), (1000,)) in emitted
    assert stack_section.START in body(gh, 1)


def test_prs_works_where_github_has_no_stacked_pull_requests(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    state = gh.load()
    state["no_stack_field"] = True
    gh.save(state)
    repo.make_stack()
    client.push()

    client.prs()

    assert stack_section.START in body(gh, 1)


def test_push_unstacks_on_github_to_put_a_branch_in_the_middle_then_prs_links_it_again(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr1b")
    repo.write("d", "pr1b\n")
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "pr1b c1")
    repo.git("switch", "--quiet", "pr2")
    client.restack(onto="pr1b")

    client.push()

    # GitHub doesn't change the base of a pull request in a stack.
    assert events.GitHubStackUnstacked(1000, 2) in emitted
    assert gh.stacks() == []
    assert next(pr for pr in gh.load()["prs"] if pr["number"] == 2)["baseRefName"] == "pr1b"

    client.prs()

    assert gh.stacks() == [[1, 4, 2, 3]]
