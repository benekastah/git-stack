"""git stack info."""

import json
import re
from contextlib import AbstractContextManager
from threading import Event, get_ident

import pytest

from git_stack import events, git
from git_stack.adapters.github import GitHubAdapter
from git_stack.commands import info
from git_stack.core import CURRENT_GROUP
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from tests.git_support import GitCall
from tests.helpers import FakeGitHub, Repo


def test_info_checks_native_graph_safety_once_per_snapshot(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.make_stack()
    measured = git.measure_native
    calls: list[str] = []

    def record(name: str) -> AbstractContextManager[None]:
        calls.append(name)
        return measured(name)

    monkeypatch.setattr(git, "measure_native", record)
    repo.stack("info", "--json")
    assert calls.count("graph") == 1
    repo.stack("info", "--json")
    assert calls.count("graph") == 2


@pytest.mark.parametrize("native", [True, False])
def test_info_counts_each_branch_from_its_recorded_base(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch, native: bool, git_calls: list[GitCall]
) -> None:
    client.branch("first")
    repo.commit("a", "one\n" + "two\n" * 11, "first")
    client.branch("second")
    repo.commit("a", "one\nthree\n", "second")
    repo.git("switch", "--quiet", "first")
    repo.commit("b", "later\n", "parent moves")

    if not native:
        monkeypatch.setattr(GitClient, "_graph_repository", lambda _git: None)

    rows = json.loads(repo.stack("info", "--json").stdout)
    branches = {row["name"]: row for row in rows}
    assert (branches["first"]["additions"], branches["first"]["deletions"]) == (13, 2)
    assert (branches["second"]["additions"], branches["second"]["deletions"]) == (1, 11)
    assert branches["main"]["additions"] is branches["main"]["deletions"] is None
    _, first, second = repo.stack("info").stdout.splitlines()
    assert first.endswith("+13/-2")
    assert second.endswith("+1/-11")
    assert first.rindex("/") == second.rindex("/")
    stats_calls = [call for call in git_calls if "--numstat" in call]
    assert len(stats_calls) == (0 if native else 2)
    assert all(call[:2] == ["diff-tree", "-r"] for call in stats_calls)


@pytest.mark.parametrize("native", [True, False])
def test_info_counts_only_edits_to_renamed_files(repo: Repo, monkeypatch: pytest.MonkeyPatch, native: bool) -> None:
    original = "".join(f"line {i} contains unchanged text\n" for i in range(20))
    repo.commit("a", original, "add source")
    repo.client.branch("rename")
    repo.git("mv", "a", "new name")
    repo.commit("new name", original.replace("line 10 contains unchanged text", "an edited line"), "rename source")

    if not native:
        monkeypatch.setattr(GitClient, "_graph_repository", lambda _git: None)

    branch = repo.branch_info("rename")
    assert (branch.additions, branch.deletions) == (1, 1)


def test_info_counts_net_text_changes_and_handles_missing_base(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    repo.commit("a", "temporary\n", "temporary work")
    repo.git("revert", "--no-edit", "HEAD")
    (repo.path / "binary").write_bytes(b"\x00\xff\n")
    repo.git("add", "binary")
    repo.git("commit", "--quiet", "-m", "add binary")

    branch = repo.branch_info("feature")
    assert (branch.additions, branch.deletions) == (0, 0)
    assert "feature * +0/-0" in repo.stack("info").stdout

    repo.git("config", "--unset", "branch.feature.stackBase")
    branch = repo.branch_info("feature")
    assert branch.additions is branch.deletions is None
    assert "+0/-0" not in repo.stack("info").stdout


def test_info_skips_patch_comparison_when_branch_already_contains_parent(
    repo: Repo, client: StackClient, git_calls: list[GitCall]
) -> None:
    repo.make_stack()
    git_calls.clear()

    assert [branch.status for branch in client.info(offline=True)] == [None, *([BranchStatus.UP_TO_DATE] * 3)]
    assert not any(call[0] == "patch-id" for call in git_calls)


def test_info_lists_every_stack_parent_before_child(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.branch("pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("switch", "--quiet", "pr1")
    client.branch("pr2b")
    repo.commit("c", "pr2b\n", "pr2b c1")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 more\n", "pr1 c2")
    repo.git("switch", "--quiet", "main")
    client.branch("solo")

    assert client.info() == [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=False, additions=1, deletions=1),
        BranchInfo("pr2", "pr1", BranchStatus.NEEDS_RESTACK, current=False, additions=1, deletions=1),
        BranchInfo("pr2b", "pr1", BranchStatus.NEEDS_RESTACK, current=False, additions=1, deletions=1),
        BranchInfo("solo", "main", BranchStatus.UP_TO_DATE, current=True, additions=0, deletions=0),
    ]


def test_info_current_selects_only_the_current_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    repo.git("switch", "--quiet", "pr2")

    assert [branch.name for branch in client.info(current=True, offline=True)] == ["main", "pr1", "pr2", "pr3"]
    assert [branch.name for branch in client.info(groups=[CURRENT_GROUP], offline=True)] == [
        "main",
        "pr1",
        "pr2",
        "pr3",
    ]
    with pytest.raises(StackError, match="there's no group"):
        client.info(groups=[""], offline=True)


def test_info_defaults_to_current_and_all_shows_other_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    repo.git("switch", "--quiet", "pr2")

    current = json.loads(repo.stack("info", "--json").stdout)
    all_stacks = json.loads(repo.stack("info", "--all", "--json").stdout)
    assert {b["name"] for b in current} == {"main", "pr1", "pr2", "pr3"}
    assert {b["name"] for b in all_stacks} == {"main", "pr1", "pr2", "pr3", "other"}


def test_info_only_queries_prs_for_the_selected_stack(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push(prs=True)
    client.branch("unrelated", from_branch="main")
    repo.commit("c", "unrelated\n", "unrelated")
    client.push(prs=True)
    gh.clear_calls()

    shown = client.info(["pr2"])

    assert [b.name for b in shown] == ["main", "pr1", "pr2", "pr3"]
    (call,) = gh.calls("api", "graphql")
    assert {arg.split("=", 1)[1] for arg in call if re.fullmatch(r"b\d+=.*", arg)} == {"pr1", "pr2", "pr3"}


@pytest.mark.parametrize("json_output", [False, True])
def test_info_only_shows_pull_requests_on_request(repo: Repo, gh: FakeGitHub, json_output: bool) -> None:
    repo.make_stack()
    repo.client.push(prs=True)
    gh.clear_calls()
    args = ("--json", "info") if json_output else ("info",)

    local = repo.stack(*args).stdout
    assert gh.calls() == []
    with_prs = repo.stack(*args, "--prs").stdout
    assert len(gh.calls("api", "graphql")) == 1

    if json_output:
        assert all(row["pr"] is None for row in json.loads(local))
        assert [row["pr"]["number"] for row in json.loads(with_prs) if row["pr"]] == [1, 2, 3]
    else:
        assert "/pull/" not in local
        assert "/pull/1" in with_prs


@pytest.mark.usefixtures("gh")
def test_info_uses_open_pull_request_as_merge_evidence_only_with_prs(repo: Repo) -> None:
    repo.make_stack()
    repo.client.push(prs=True)
    repo.git("branch", "-f", "main", "pr1")

    local = json.loads(repo.stack("info", "--json").stdout)
    with_prs = json.loads(repo.stack("info", "--json", "--prs").stdout)

    assert next(row for row in local if row["name"] == "pr1")["status"] == "merged"
    branch = next(row for row in with_prs if row["name"] == "pr1")
    assert branch["pr"]["state"] == "draft"
    assert branch["status"] == "up-to-date"


def test_info_overlaps_pr_and_integration_source_queries(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for branch, path in (("one", "a"), ("two", "b")):
        client.branch(branch, from_branch="main")
        repo.commit(path, f"{branch}\n", branch)
        client.push()
    client.integrate("combined", ["one", "two"])
    client.branch("work", from_branch="combined")
    repo.commit("c", "work\n", "work")
    client.push(prs=True)
    gh.clear_calls()
    pulls, sources = Event(), Event()
    gh_call = GitHubAdapter.gh

    def observe(adapter: GitHubAdapter, *args: str, input: str | None = None) -> str:
        if args[:2] == ("api", "graphql"):
            if any(arg.startswith("b0=") for arg in args):
                pulls.set()
                assert sources.wait(5), "integration query did not overlap the PR lookup"
            if any(arg.startswith("h0=") for arg in args):
                sources.set()
                assert pulls.wait(5), "PR lookup did not overlap the integration query"
        return gh_call(adapter, *args, input=input)

    monkeypatch.setattr(GitHubAdapter, "gh", observe)
    assert [branch.name for branch in client.info(["work"])] == ["combined", "work"]
    assert pulls.is_set() and sources.is_set()


def test_info_reports_integration_lookup_warning_on_the_calling_thread(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for branch, path in (("one", "a"), ("two", "b")):
        client.branch(branch, from_branch="main")
        repo.commit(path, f"{branch}\n", branch)
        client.push()
    client.integrate("combined", ["one", "two"])
    client.branch("work", from_branch="combined")
    repo.commit("c", "work\n", "work")
    client.push(prs=True)
    warnings: list[tuple[str, int]] = []

    def fail(adapter: GitHubAdapter, *args: str, input: str | None = None) -> str:
        raise StackError(next(arg.split("=")[0] for arg in args if arg.startswith(("b0=", "h0="))))

    def observe(event: events.Event) -> None:
        if isinstance(event, events.PullRequestLookupFailed):
            warnings.append((event.error, get_ident()))

    monkeypatch.setattr(GitHubAdapter, "gh", fail)
    client.on_event = observe
    assert [branch.name for branch in client.info(["work"])] == ["combined", "work"]
    assert [name for name, _ in warnings] == ["b0", "h0"]
    assert warnings[-1] == ("h0", get_ident())


@pytest.mark.parametrize("checkout", ["main", "untracked"])
@pytest.mark.parametrize("output", [(), ("--json",)])
def test_info_from_an_untracked_branch_matches_all(repo: Repo, checkout: str, output: tuple[str, ...]) -> None:
    repo.make_stack()
    repo.stack("create", "other", "--from", "main")
    if checkout == "untracked":
        repo.git("switch", "-qc", checkout, "main")
    else:
        repo.git("switch", "--quiet", checkout)

    explicit = repo.stack("info", "-a", *output)
    implicit = repo.stack("info", *output)

    assert implicit.stdout == explicit.stdout
    assert implicit.stderr == explicit.stderr == ""


def test_info_group_selector_still_requires_a_current_stack(repo: Repo) -> None:
    result = repo.stack("info", "-g", expect_success=False)
    assert "main isn't in a stack" in result.stderr


def test_bare_group_selects_all_current_group_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    client.group_add("feature", ["pr1", "other"])
    repo.git("switch", "--quiet", "pr2")

    current = repo.stack("info").stdout
    assert "group feature" in current
    assert "other" not in current
    assert "other" in repo.stack("info", "-g").stdout
    data = json.loads(repo.stack("info", "--json").stdout)
    assert {b["name"]: b["group"] for b in data} == {"main": None, "pr1": "feature", "pr2": None, "pr3": None}

    assert set(client.selected_bottoms([], [CURRENT_GROUP], client.stacks(), "show")) == {"pr1", "other"}
    assert {b.name for b in client.info(groups=[CURRENT_GROUP], offline=True)} == {"main", "pr1", "pr2", "pr3", "other"}

    with pytest.raises(StackError, match="there's no group"):
        client.group_remove(groups=[""])
    assert repo.config("pr1", "stackGroup") == "feature"
    assert repo.config("other", "stackGroup") == "feature"


def test_info_remembers_whether_a_branch_on_main_conflicts_with_it(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main\n", "main c1")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK
    cached = repo.cached_check("pr1", "stackConflictCheck")
    assert cached is not None
    assert cached == f"{repo.git('rev-parse', 'pr1')} {repo.git('rev-parse', 'main')} true"
    assert not repo.config_exists("pr1", "stackConflictCheck")

    # The remembered answer is used while neither tip moves...
    client.checks.set("pr1", "stackConflictCheck", cached.replace("true", "false"), tracked=["pr1"])
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    # ...and checked again once one does.
    repo.commit("b", "main again\n", "main c2")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK


def test_info_remembers_whether_a_branch_landed_in_its_parent(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    cached = repo.cached_check("pr1", "stackLandedCheck2")
    assert cached is not None
    assert cached == " ".join(
        [
            repo.git("rev-parse", "pr1"),
            repo.git("rev-parse", "main"),
            repo.git("config", "branch.pr1.stackBase"),
            "false",
        ]
    )

    client.checks.set("pr1", "stackLandedCheck2", cached.replace("false", "true"), tracked=["pr1"])
    assert repo.branch_info("pr1").status is BranchStatus.MERGED


def test_info_shows_reverted_branch(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("revert", "--no-edit", "HEAD")

    assert repo.branch_info("pr1").status is BranchStatus.REVERTED


def test_info_reads_its_settings_with_the_config(repo: Repo, git_calls: list[GitCall]) -> None:
    """stack.adapter, stack.orientation and the remote's URL come from the one
    `git config --list` that reads the branches' settings, not a `git config
    --get` each."""
    repo.make_stack()
    repo.git("config", "stack.orientation", "root-at-bottom")
    git_calls.clear()

    assert repo.stack("info").stdout == "      ┌─ pr3 * +1/-1\n   ┌─ pr2      +1/-1\n┌─ pr1         +1/-1\nmain\n"

    assert [call[:2] for call in git_calls if call[0] == "config"] == [
        ["config", "--list"],
        ["config", "--get-colorbool"],
    ]


def test_info_overlaps_color_lookup_with_collecting_branches(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.make_stack()
    monkeypatch.delenv("NO_COLOR", raising=False)
    collecting, checking_color = Event(), Event()
    original = StackClient.info

    def color(_stack: StackClient) -> bool:
        checking_color.set()
        assert collecting.wait(timeout=3)
        return True

    def collect(
        stack: StackClient, branches: list[str], groups: list[str], *, offline: bool, current: bool
    ) -> list[BranchInfo]:
        collecting.set()
        assert checking_color.wait(timeout=3)
        return original(stack, branches, groups, offline=offline, current=current)

    monkeypatch.setattr(info, "use_color", color)
    monkeypatch.setattr(StackClient, "info", collect)
    assert "\033[" in repo.stack("info").stdout


@pytest.mark.parametrize("no_color", [None, "1"])
def test_info_colors_unless_no_color_is_set(
    repo: Repo, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch, no_color: str | None
) -> None:
    """With NO_COLOR set, info neither asks git whether to color nor colors, even when
    color.stack says always."""
    repo.make_stack()
    repo.git("config", "color.stack", "always")
    if no_color is not None:
        monkeypatch.setenv("NO_COLOR", no_color)
    git_calls.clear()

    output = repo.stack("info").stdout

    asked = any(call[:2] == ["config", "--get-colorbool"] for call in git_calls)
    assert asked is (no_color is None)
    assert ("\033[" in output) is (no_color is None)
    assert all(branch in output for branch in ("pr1", "pr2", "pr3"))
