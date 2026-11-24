"""git stack prs, with tests/fake_gh.py standing in for gh."""

from collections.abc import Callable
from typing import Any

import pytest

from git_stack import events, stack_section
from git_stack.adapters import github
from git_stack.adapters.github import PRProtection
from git_stack.errors import StackError
from git_stack.stack import BranchStatus, PullRequestAction, PullRequestStep, StackClient
from tests import fake_gh
from tests.git_support import GitCall
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


def test_prs_works_without_an_origin_remote(repo: Repo, gh: FakeGitHub) -> None:
    repo.git("remote", "rename", "origin", "upstream")
    repo.make_stack()

    repo.stack("push")
    repo.stack("prs")

    assert [pr[:2] for pr in opened(gh)] == [("pr1", "main"), ("pr2", "pr1"), ("pr3", "pr2")]


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


def test_prs_dry_run_opens_nothing(repo: Repo, gh: FakeGitHub, client: StackClient, git_calls: list[GitCall]) -> None:
    repo.make_stack()
    client.push()

    steps = client.prs(dry_run=True).steps

    assert [(s.branch, s.action, s.title, s.number) for s in steps] == [
        ("pr1", PullRequestAction.OPEN, "pr1 c1", None),
        ("pr2", PullRequestAction.OPEN, "pr2 c1", None),
        ("pr3", PullRequestAction.OPEN, "pr3 c1", None),
    ]
    assert gh.calls("pr") == []
    assert not any(call[0] == "ls-tree" for call in git_calls)


def test_prs_skips_template_lookup_when_all_pull_requests_exist(
    repo: Repo, gh: FakeGitHub, client: StackClient, git_calls: list[GitCall]
) -> None:
    repo.make_stack()
    client.push(prs=True)
    created = opened(gh)
    gh.clear_calls()
    git_calls.clear()

    assert all(step.action is PullRequestAction.EXISTS for step in client.prs().steps)
    assert opened(gh) == created
    assert not any(call[0] == "ls-tree" for call in git_calls)


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

    with pytest.raises(StackError, match=r"opening a pull request for pr2 failed \(opened #1 for pr1, #2 for pr3\): "):
        client.prs()
    assert [head for head, *_ in opened(gh)] == ["pr1", "pr3"]

    gh.fail_opening()
    client.prs()

    assert sorted(head for head, *_ in opened(gh)) == ["pr1", "pr2", "pr3"]


def test_push_prs_takes_template_on_first_publication(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    add_template(repo, ".github/PULL_REQUEST_TEMPLATE.md")
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")

    client.push(prs=True)

    assert opened(gh) == [("feature", "main", "feature", TEMPLATE, True)]


def test_pr_title_uses_gits_first_subject_when_it_spans_lines(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("feature")
    repo.commit("a", "first\n", "first line\nsecond line\n\nDetails.")
    repo.commit("a", "second\n", "Later commit")

    client.push(prs=True)

    assert opened(gh) == [("feature", "main", "first line second line", "", True)]


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


def merge(
    repo: Repo,
    gh: FakeGitHub,
    client: StackClient,
    branch: str,
    number: int,
    delete_branch: bool = True,
    *,
    sync: bool = True,
) -> None:
    """Squash-merge branch, pull request number, on the forge, and optionally sync.
    Deleting the branch, GitHub points the pull requests on it at main
    itself; otherwise sync's push does."""
    repo.squash_merge_on_forge(branch, delete_branch)
    state = gh.load()
    for pr in state["prs"]:
        if pr["number"] == number:
            pr |= {
                "state": "MERGED",
                "headRefOid": repo.git("rev-parse", branch),
                "mergeCommit": {"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
            }
        elif delete_branch and pr["baseRefName"] == branch:
            pr["baseRefName"] = "main"
            pr.setdefault("baseChanges", []).append({"oldBase": branch, "createdAt": ""})
    gh.save(state)
    if sync:
        client.sync()


def test_prs_shows_the_stack_on_the_merged_pull_request_it_was_on(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    merge(repo, gh, client, "pr1", 1)
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")
    client.push()

    client.prs()

    assert body(gh, 2) == stack("- `main`", "  - #1 (merged)", "    - #2 👈", "      - #3", "        - #4")
    assert "#4" not in body(gh, 1)  # merged pull requests are left alone


def test_prs_shows_only_the_last_merged_pull_request(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    merge(repo, gh, client, "pr1", 1)
    client.prs()
    merge(repo, gh, client, "pr2", 2, delete_branch=False)

    client.prs()

    # #2's own description still shows it on #1.
    assert body(gh, 3) == stack("- `main`", "  - #2 (merged)", "    - #3 👈")


def test_prs_shows_a_merged_pull_request_that_merged_before_the_base_changed(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    merge(repo, gh, client, "pr1", 1)
    # A later pull request from a new branch named pr1, merged after #2 moved
    # off pr1, isn't the one #2 was on.
    gh.add_pr(number=9, state="MERGED", headRefName="pr1", baseRefName="main", mergedAt="2026-12-31T00:00:00Z")

    client.prs()

    assert body(gh, 2) == stack("- `main`", "  - #1 (merged)", "    - #2 👈", "      - #3")


@pytest.mark.parametrize(("push", "merged_at"), [(False, ""), (True, ""), (True, "2099-01-01T00:00:00Z")])
def test_prs_shows_no_merged_pull_request_for_a_branch_moved_off_an_open_one(
    repo: Repo, gh: FakeGitHub, client: StackClient, push: bool, merged_at: str
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    # An earlier pull request from a branch named pr1, merged before.
    gh.add_pr(
        number=9,
        state="MERGED",
        headRefName="pr1",
        headRefOid=repo.git("rev-parse", "main"),
        baseRefName="main",
        mergeCommit=None,
        mergedAt=merged_at,
    )
    repo.git("switch", "--quiet", "pr2")
    client.restack(onto="main")
    client.push()

    if push:
        gh.clear_calls()
        client.push(prs=True)
        assert any("b0=pr1" in call for call in gh.calls("api", "graphql")) == (not merged_at)
    else:
        client.prs()

    assert body(gh, 2) == stack("- `main`", "  - #2 👈", "    - #3")


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


def test_prs_batches_changed_descriptions(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()

    client.prs()

    assert len(gh.calls("api", "graphql", "--hostname", "github.com", "--input", "-")) == 1
    assert not gh.calls("pr", "edit")
    assert body(gh, 2) == stack("- `main`", "  - #1", "    - #2 👈", "      - #3")


def test_prs_rechecks_protection_for_later_description_batches(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    client.push(prs=True)
    state = gh.load()
    for pr in state["prs"]:
        pr["body"] = "new description"
    gh.save(state)
    update_pull_requests = fake_gh.update_pull_requests

    def approve_after_first_update(request: dict[str, Any], state: dict[str, Any]) -> str:
        response = update_pull_requests(request, state)
        if "p1" in request["variables"]:
            state["prs"][1]["reviewDecision"] = "APPROVED"
        return response

    monkeypatch.setattr(github, "PR_UPDATE_BATCH_SIZE", 1)
    monkeypatch.setattr(fake_gh, "update_pull_requests", approve_after_first_update)

    with pytest.raises(StackError, match=r"can't edit the description of PR #2: it is approved"):
        client.prs(protection=PRProtection(protect=True))

    assert stack_section.START in body(gh, 1)
    assert body(gh, 2) == "new description"


def test_prs_reports_the_failed_description_from_a_partial_batch(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push(prs=True)
    state = gh.load()
    for pr in state["prs"]:
        pr["body"] = "new description"
    state["fail_edits"] = [2]
    gh.save(state)

    with pytest.raises(StackError, match=r"couldn't update descriptions for #2(?s:.*)fake gh: failing as asked"):
        client.prs()

    assert stack_section.START in body(gh, 1)
    assert body(gh, 2) == "new description"
    assert stack_section.START in body(gh, 3)


@pytest.mark.parametrize(
    "payload",
    [
        b"caf\xe9",
        b"[]",
        b'{"data":{"p1":{"pullRequest":{"number":1}},"p2":null,"p3":{"pullRequest":{"number":3}}},"errors":[null]}',
    ],
)
def test_description_batch_keeps_gh_error_with_an_invalid_response(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    repo.make_stack()
    client.push()
    run_gh = fake_gh.run

    def gh_response(args: list[str], read_input: Callable[[], bytes]) -> tuple[int, bytes, bytes]:
        if args[:2] == ["api", "graphql"] and "--input" in args:
            return 1, payload, b"gh: upstream proxy failed"
        return run_gh(args, read_input)

    monkeypatch.setattr(fake_gh, "run", gh_response)

    with pytest.raises(StackError, match="gh: upstream proxy failed") as failure:
        client.prs()
    if b'"p2":null' in payload:
        assert "couldn't update descriptions for #2;" in str(failure.value)


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


def test_push_with_prs_pushes_then_opens_pull_requests(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()

    result = client.push(prs=True)

    assert result.pushed == ["pr1", "pr2", "pr3"]
    assert [(s.branch, s.action, s.number) for s in result.steps or []] == [
        ("pr1", PullRequestAction.OPEN, 1),
        ("pr2", PullRequestAction.OPEN, 2),
        ("pr3", PullRequestAction.OPEN, 3),
    ]
    assert [(head, base) for head, base, *_ in opened(gh)] == [("pr1", "main"), ("pr2", "pr1"), ("pr3", "pr2")]
    assert [b.pull_request and b.pull_request.number for b in result.branches] == [None, 1, 2, 3]
    # It doesn't say to open them with `git stack prs`.
    assert not any(isinstance(e, events.NoPullRequests) for e in emitted)


def test_push_with_prs_pushes_without_github(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")

    result = client.push(prs=True)

    assert result.pushed == ["pr1"]
    assert repo.git("ls-remote", "origin", "pr1")
    assert result.steps is None
    assert result.error is not None and result.error.startswith(
        "pushed, but didn't open or update any pull requests: origin isn't a GitHub repository"
    )


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


@pytest.mark.parametrize("beside", [False, True], ids=["chain", "tree"])
def test_rebuild_stack_adds_to_the_top_of_a_github_stack_that_fits(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event], beside: bool
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    if beside:
        repo.git("switch", "--quiet", "pr2")
        client.branch("pr3b")
        repo.commit("b", "pr3b\n", "pr3b c1")
    repo.git("switch", "--quiet", "pr3")
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")

    client.push(prs=True, rebuild_stack=True)

    # The same stack, not a new one in its place.
    assert gh.load()["stacks"] == [{"number": 1000, "prs": [1, 2, 3, 4]}]
    assert not any(isinstance(e, events.GitHubStackUnstacked) for e in emitted)


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


def test_prs_falls_back_when_github_stack_ends_in_a_merged_pr(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    gh.enable_stacks([1, 2])
    state = gh.load()
    state["prs"][1]["state"] = "MERGED"
    gh.save(state)
    client.untrack(["pr2"])
    repo.git("config", "branch.pr3.stackParent", "pr1")

    client.prs()

    assert gh.stacks() == [[1, 2]]
    assert any(
        isinstance(event, events.GitHubStackMismatch) and event.numbers == (1, 3) and event.github_stacks == (1000,)
        for event in emitted
    )
    assert stack_section.START in body(gh, 3)

    before = gh.load()
    client.prs(rebuild_stack=True, dry_run=True)
    after = gh.load()
    assert after["prs"] == before["prs"]
    assert after["stacks"] == before["stacks"]
    assert events.GitHubStackRebuildPlanned((1000,), (1, 3)) in emitted

    client.prs(rebuild_stack=True)

    assert gh.stacks() == [[2], [1, 3]]
    assert stack_section.START not in body(gh, 3)


@pytest.mark.parametrize("dry_run", [False, True])
def test_prs_rebuild_refuses_to_unstack_unrelated_pull_requests(
    repo: Repo, gh: FakeGitHub, client: StackClient, dry_run: bool
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    gh.enable_stacks([1, 2, 3])
    state = gh.load()
    state["prs"][2]["baseRefName"] = "pr1"
    gh.save(state)
    client.untrack(["pr2"])
    repo.git("config", "branch.pr3.stackParent", "pr1")

    with pytest.raises(StackError, match="other open pull requests: #2"):
        client.prs(rebuild_stack=True, dry_run=dry_run)

    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.parametrize("dry_run", [False, True])
def test_prs_rebuild_allows_unrelated_closed_pull_requests(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event], dry_run: bool
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    gh.enable_stacks([1, 2])
    state = gh.load()
    other = dict(state["prs"][0])
    other.update(number=99, state="CLOSED", headRefName="unrelated")
    state["prs"].append(other)
    state["stacks"][0]["prs"].append(99)
    gh.save(state)

    client.prs(rebuild_stack=True, dry_run=dry_run)

    assert gh.stacks() == ([[1, 2, 99]] if dry_run else [[1, 2, 3]])
    if dry_run:
        assert events.GitHubStackRebuildPlanned((1000,), (1, 2, 3)) in emitted


def test_prs_rebuild_dry_run_does_not_create_a_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    client.prs()
    gh.enable_stacks()

    client.prs(rebuild_stack=True, dry_run=True)

    assert gh.stacks() == []
    assert events.GitHubStackRebuildPlanned((), (1, 2, 3)) in emitted


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


def test_push_keeps_a_github_stack_that_is_still_a_path_through_a_branching_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr3b")
    repo.commit("b", "pr3b\n", "pr3b c1")

    client.push(prs=True, rebuild_stack=True)

    assert gh.stacks() == [[1, 2, 3]]
    # GitHub shows the stack on the pull requests in it; the others show the
    # whole tree.
    assert [body(gh, n) for n in (1, 2, 3)] == ["", "", ""]
    assert body(gh, 4) == stack("- `main`", "  - #1", "    - #2", "      - #3", "      - #4 👈")


def test_prs_adds_only_children_to_the_top_of_a_github_stack_in_a_branching_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")  # #7, off the GitHub stack
    repo.commit("b", "pr2b\n", "pr2b c1")
    repo.git("switch", "--quiet", "pr3")
    client.branch("pr4")  # #4, pr3's only child
    repo.commit("c", "pr4\n", "pr4 c1")
    client.branch("pr5a")  # #5 and #6, a fork
    repo.commit("c", "pr5a\n", "pr5a c1")
    repo.git("switch", "--quiet", "pr4")
    client.branch("pr5b")
    repo.commit("c", "pr5b\n", "pr5b c1")
    client.push()

    client.prs()

    assert gh.stacks() == [[1, 2, 3, 4]]
    assert [body(gh, n) for n in (1, 2, 3, 4)] == ["", "", "", ""]
    assert all(stack_section.START in body(gh, n) for n in (5, 6, 7))


def test_prs_keeps_a_github_stack_when_linking_is_turned_off(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("config", "stack.githubStacks", "false")
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")

    client.push(prs=True)

    assert gh.stacks() == [[1, 2, 3]]
    # GitHub still shows its stack on those in it.
    assert [body(gh, n) for n in (1, 2, 3)] == ["", "", ""]
    assert body(gh, 4) == stack("- `main`", "  - #1", "    - #2", "      - #3", "        - #4 👈")


def test_prs_keeps_a_github_stack_in_a_branching_stack_when_linking_is_turned_off(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("config", "stack.githubStacks", "false")
    client.branch("pr4")  # #4, which would join the stack's top
    repo.commit("c", "pr4\n", "pr4 c1")
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr3b")  # #5
    repo.commit("b", "pr3b\n", "pr3b c1")

    client.push(prs=True)

    assert gh.stacks() == [[1, 2, 3]]
    # GitHub still shows its stack on those in it.
    assert [body(gh, n) for n in (1, 2, 3)] == ["", "", ""]
    assert all(stack_section.START in body(gh, n) for n in (4, 5))


def test_prs_keeps_a_github_stack_whose_bottom_merged_in_a_branching_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    merge(repo, gh, client, "pr1", 1)
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr3b")
    repo.commit("b", "pr3b\n", "pr3b c1")
    client.push()

    client.prs()

    assert gh.stacks() == [[1, 2, 3]]
    assert [body(gh, n) for n in (2, 3)] == ["", ""]
    assert stack_section.START in body(gh, 4)


def test_prs_keeps_the_github_stack_of_each_stack_in_a_group(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("x1", group="g")
    repo.commit("a", "x1\n", "x1 c1")
    client.branch("x2")
    repo.commit("a", "x2\n", "x2 c1")
    repo.git("switch", "--quiet", "main")
    client.branch("y1", group="g")
    repo.commit("b", "y1\n", "y1 c1")
    client.branch("y2")
    repo.commit("b", "y2\n", "y2 c1")
    client.push(groups=["g"], prs=True)
    gh.enable_stacks([1, 2], [3, 4])

    client.prs(groups=["g"])

    assert gh.stacks() == [[1, 2], [3, 4]]
    group = ["  - #1", "    - #2", "  - #3", "    - #4"]
    for number in (1, 2, 3, 4):
        assert body(gh, number) == stack(
            "- `main`", *(line + (" 👈" if line.endswith(f"#{number}") else "") for line in group)
        )


@pytest.mark.parametrize("push", [False, True])
def test_prs_shows_the_group_across_roots_in_the_selected_pr(
    repo: Repo, gh: FakeGitHub, client: StackClient, push: bool
) -> None:
    repo.git("branch", "release", "main")
    repo.git("push", "--quiet", "--set-upstream", "origin", "release")
    client.branch("a", group="g")
    repo.commit("a", "a\n", "a c1")
    client.push(prs=True)
    client.branch("b", group="g", from_branch="release")
    repo.commit("b", "b\n", "b c1")
    client.push(prs=True)
    assert body(gh, 1) == ""
    assert body(gh, 2) == stack("- `main`", "  - #1", "- `release`", "  - #2 👈")

    gh.clear_calls()
    if push:
        client.push(["a"], prs=True)
    else:
        client.prs(["a"])

    assert body(gh, 1) == stack("- `main`", "  - #1 👈", "- `release`", "  - #2")
    assert [call[2] for call in gh.calls("pr", "edit")] == ["1"]


def test_prs_batches_descriptions_across_roots(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.git("branch", "release", "main")
    repo.git("push", "--quiet", "--set-upstream", "origin", "release")
    client.branch("a", group="g")
    repo.commit("a", "a\n", "a")
    client.branch("b", group="g", from_branch="release")
    repo.commit("b", "b\n", "b")
    client.push(groups=["g"])
    gh.clear_calls()

    client.prs(groups=["g"])

    (mutation,) = [call for call in gh.calls("api", "graphql") if "--input" in call]
    assert mutation[-2:] == ["--input", "-"]
    assert gh.calls("pr", "edit") == []
    assert body(gh, 1) == stack("- `main`", "  - #1 👈", "- `release`", "  - #2")
    assert body(gh, 2) == stack("- `main`", "  - #1", "- `release`", "  - #2 👈")


@pytest.mark.parametrize("sync", [False, True])
def test_prs_keeps_group_and_merge_history_when_a_grouped_bottom_merges(
    repo: Repo, gh: FakeGitHub, client: StackClient, sync: bool
) -> None:
    repo.git("branch", "release", "main")
    repo.git("push", "--quiet", "--set-upstream", "origin", "release")
    client.branch("a", group="g")
    repo.commit("a", "a\n", "a")
    client.branch("b")
    repo.commit("b", "b\n", "b")
    client.branch("other", group="g", from_branch="release")
    repo.commit("c", "c\n", "c")
    client.push(groups=["g"], prs=True)
    repo.git("switch", "--quiet", "b")
    merge(repo, gh, client, "a", 1, sync=sync)
    if not sync:
        repo.git("fetch", "--quiet", "origin", "main:main")

    if sync:
        client.prs(["other"])
        assert body(gh, 3) == stack("- `main`", "  - #1 (merged)", "    - #2", "- `release`", "  - #3 👈")
    else:
        client.prs(["b"])
        assert body(gh, 2) == stack("- `main`", "  - #1 (merged)", "    - #2 👈", "- `release`", "  - #3")


def test_prs_shows_a_merged_parent_on_each_root_where_it_had_children(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.git("branch", "release", "main")
    repo.git("push", "--quiet", "--set-upstream", "origin", "release")
    client.branch("a", group="g")
    repo.commit("a", "a\n", "a")
    client.branch("b")
    repo.commit("b", "b\n", "b")
    client.branch("c", from_branch="a")
    repo.commit("c", "c\n", "c")
    client.push(prs=True)
    merge(repo, gh, client, "a", 1)
    client.restack(onto="release")
    client.push()

    client.prs(["b"])

    assert body(gh, 2) == stack(
        "- `main`", "  - #1 (merged)", "    - #2 👈", "- `release`", "  - #1 (merged)", "    - #3"
    )


def test_push_unstacks_a_github_stack_that_no_longer_fits_a_branching_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr3b")
    repo.commit("b", "pr3b\n", "pr3b c1")
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr2b")  # put in the middle of the GitHub stack, under pr3
    repo.commit("b", "pr2b\n", "pr2b c1")
    repo.git("switch", "--quiet", "pr3")
    client.restack(onto="pr2b")

    with pytest.raises(StackError, match="--pr-rebuild"):
        client.push(prs=True)
    assert gh.stacks() == [[1, 2, 3]]

    client.push(prs=True, rebuild_stack=True)

    # Which path of the tree to link is up to the user (see docs/roadmap.md).
    assert gh.stacks() == []
    assert all(stack_section.START in body(gh, n) for n in (1, 2, 3, 4, 5))


def test_prs_rebuild_dry_run_changes_nothing_in_a_branching_stack(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push(prs=True)
    repo.git("switch", "--quiet", "pr2")
    client.branch("pr3b")
    repo.commit("b", "pr3b\n", "pr3b c1")
    repo.git("switch", "--quiet", "pr3")
    client.branch("pr4")
    repo.commit("c", "pr4\n", "pr4 c1")
    repo.git("config", "stack.githubStacks", "false")
    client.push(prs=True)
    repo.git("config", "--unset", "stack.githubStacks")
    before = gh.load()

    client.prs(rebuild_stack=True, dry_run=True)

    after = gh.load()
    assert (after["prs"], after["stacks"]) == (before["prs"], before["stacks"])
    assert events.GitHubStackAddPlanned(1000, (4,)) in emitted


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
    assert any(
        isinstance(event, events.GitHubStackMismatch) and event.numbers == (1, 2, 3) and event.github_stacks == (1000,)
        for event in emitted
    )
    assert stack_section.START in body(gh, 3)


def test_prs_warns_when_a_stack_on_github_is_in_another_order(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    gh.enable_stacks([1, 3, 2])

    client.prs()

    assert any(
        isinstance(event, events.GitHubStackMismatch) and event.numbers == (1, 2, 3) and event.github_stacks == (1000,)
        for event in emitted
    )
    assert stack_section.START in body(gh, 1)


def test_prs_works_where_github_has_no_stacked_pull_requests(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    state = gh.load()
    state["no_stack_field"] = True
    gh.save(state)
    repo.make_stack()
    client.push()

    client.prs()

    assert stack_section.START in body(gh, 1)


def test_push_requires_rebuild_stack_to_retarget_a_stacked_pull_request(
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

    before = repo.git("rev-parse", "refs/remotes/origin/pr2")
    with pytest.raises(StackError, match="--pr-rebuild"):
        client.push()

    assert gh.stacks() == [[1, 2, 3]]
    assert repo.git("rev-parse", "refs/remotes/origin/pr2") == before
    client.push(rebuild_stack=True)

    # GitHub doesn't change the base of a pull request in a stack.
    assert events.GitHubStackUnstacked(1000, 2) in emitted
    assert gh.stacks() == []
    assert next(pr for pr in gh.load()["prs"] if pr["number"] == 2)["baseRefName"] == "pr1b"

    client.prs(rebuild_stack=True)

    assert gh.stacks() == [[1, 4, 2, 3]]


def test_prs_requires_rebuild_stack_to_retarget_a_stacked_pull_request(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    state["prs"][1]["baseRefName"] = "main"
    gh.save(state)

    with pytest.raises(StackError, match="--pr-rebuild"):
        client.prs()
    assert gh.stacks() == [[1, 2, 3]]
    assert gh.load()["prs"][1]["baseRefName"] == "main"

    before = gh.load()
    emitted.clear()
    client.prs(rebuild_stack=True, dry_run=True)
    after = gh.load()
    assert after["prs"] == before["prs"]
    assert after["stacks"] == before["stacks"]
    assert events.GitHubStackRebuildPlanned((1000,), (1, 2, 3)) in emitted

    client.prs(rebuild_stack=True)
    assert gh.stacks() == [[1, 2, 3]]
    assert gh.load()["prs"][1]["baseRefName"] == "pr1"


@pytest.mark.parametrize("dry_run", [False, True])
def test_prs_refuses_to_unstack_unrelated_prs_during_retarget(
    repo: Repo, gh: FakeGitHub, client: StackClient, dry_run: bool
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    other = dict(state["prs"][0])
    other.update(number=99, headRefName="unrelated", baseRefName="pr3")
    state["prs"].append(other)
    state["stacks"][0]["prs"].append(99)
    state["prs"][1]["baseRefName"] = "main"
    gh.save(state)

    with pytest.raises(StackError, match="other open pull requests: #99"):
        client.prs(rebuild_stack=True, dry_run=dry_run)

    assert gh.stacks() == [[1, 2, 3, 99]]
    assert gh.load()["prs"][1]["baseRefName"] == "main"


def test_prs_dry_run_reports_unstack_when_linking_is_disabled(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    state["prs"][1]["baseRefName"] = "main"
    gh.save(state)
    repo.git("config", "stack.githubStacks", "false")
    emitted.clear()

    client.prs(rebuild_stack=True, dry_run=True)

    assert events.GitHubStackUnstackPlanned((1000,), (2,)) in emitted
    assert gh.stacks() == [[1, 2, 3]]


def test_sync_requires_rebuild_stack_before_pushing_a_retargeted_pull_request(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    state["prs"][1]["baseRefName"] = "main"
    gh.save(state)
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "updated\n", "pr2 update")
    before = repo.git("rev-parse", "refs/remotes/origin/pr2")

    with pytest.raises(StackError, match="--pr-rebuild"):
        client.sync()
    assert gh.stacks() == [[1, 2, 3]]
    assert repo.git("rev-parse", "refs/remotes/origin/pr2") == before

    client.sync(rebuild_stack=True)
    assert gh.stacks() == []
    assert gh.load()["prs"][1]["baseRefName"] == "pr1"


def test_push_checks_unrelated_prs_before_pushing_a_new_parent(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    other = dict(state["prs"][0])
    other.update(number=99, headRefName="unrelated", baseRefName="pr3")
    state["prs"].append(other)
    state["stacks"][0]["prs"].append(99)
    gh.save(state)
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr1b")
    repo.commit("a", "new parent\n", "new parent")
    repo.git("switch", "--quiet", "pr2")
    client.restack(onto="pr1b")
    before = repo.git("rev-parse", "refs/remotes/origin/pr2")

    with pytest.raises(StackError, match="other open pull requests: #99"):
        client.push(rebuild_stack=True)

    assert gh.stacks() == [[1, 2, 3, 99]]
    assert repo.git("rev-parse", "refs/remotes/origin/pr2") == before
    assert not repo.git("branch", "--list", "pr1b", cwd=repo.origin)


def test_push_rebuild_allows_unrelated_closed_pr_before_pushing_new_parent(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    gh.enable_stacks()
    repo.make_stack()
    client.push()
    client.prs()
    state = gh.load()
    other = dict(state["prs"][0])
    other.update(number=99, state="CLOSED", headRefName="unrelated", baseRefName="pr3")
    state["prs"].append(other)
    state["stacks"][0]["prs"].append(99)
    gh.save(state)
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr1b")
    repo.commit("a", "new parent\n", "new parent")
    repo.git("switch", "--quiet", "pr2")
    client.restack(onto="pr1b")

    client.push(rebuild_stack=True)

    assert repo.git("rev-parse", "refs/remotes/origin/pr2") == repo.git("rev-parse", "pr2")
    assert gh.load()["prs"][1]["baseRefName"] == "pr1b"


def test_push_checks_protected_stack_member_before_publishing_new_parent(repo: Repo, gh: FakeGitHub) -> None:
    gh.enable_stacks()
    repo.make_stack()
    repo.stack("push", "--prs")
    state = gh.load()
    state["prs"][0]["isInMergeQueue"] = True
    gh.save(state)
    repo.git("switch", "--quiet", "pr1")
    repo.client.branch("pr1b")
    repo.commit("a", "new parent\n", "new parent")
    repo.git("switch", "--quiet", "pr2")
    repo.client.restack(onto="pr1b")

    result = repo.stack("push", "--pr-rebuild", expect_success=False)

    assert "can't unstack PR #1: it is in the merge queue" in result.stderr
    assert gh.stacks() == [[1, 2, 3]]
    assert not repo.git("branch", "--list", "pr1b", cwd=repo.origin)
