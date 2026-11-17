"""git stack info."""

import json

import pytest
from git_client.testing import GitCall

from git_stack.core import CURRENT_GROUP
from git_stack.errors import StackError
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from git_stack.stacks import StackNode, Stacks
from tests.helpers import Repo


def test_info_lists_a_multiple_base_branch_once_with_all_its_parents(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    stacks = Stacks(
        [
            StackNode("pr1", "main"),
            StackNode("pr2", "main"),
            StackNode("pr3", "pr1", additional_parents=("pr2",)),
        ]
    )
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    branches = client.info(offline=True)

    assert [branch.name for branch in branches] == ["main", "pr1", "pr2", "pr3"]
    combined = next(branch for branch in branches if branch.name == "pr3")
    assert combined.parent == "pr1"
    assert combined.additional_parents == ("pr2",)
    assert combined.status is BranchStatus.UNKNOWN


def test_info_does_not_guess_status_of_a_multiple_base_descendant(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    stacks = Stacks(
        [StackNode("pr1", "main"), StackNode("pr2", "pr1", additional_parents=("main",)), StackNode("pr3", "pr2")]
    )
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    statuses = {branch.name: branch.status for branch in client.info(offline=True)}

    assert statuses["pr2"] is BranchStatus.UNKNOWN
    assert statuses["pr3"] is BranchStatus.UNKNOWN


def test_info_lists_a_shared_dependent_once_across_roots(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    repo.git("branch", "release", "refs/heads/main")
    stacks = Stacks(
        [StackNode("pr1", "main"), StackNode("pr2", "release"), StackNode("pr3", "pr1", additional_parents=("pr2",))]
    )
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    branches = client.info(offline=True)

    assert [b.name for b in branches] == ["main", "pr1", "release", "pr2", "pr3"]


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
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=False),
        BranchInfo("pr2", "pr1", BranchStatus.NEEDS_RESTACK, current=False),
        BranchInfo("pr2b", "pr1", BranchStatus.NEEDS_RESTACK, current=False),
        BranchInfo("solo", "main", BranchStatus.UP_TO_DATE, current=True),
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

    current = json.loads(repo.stack("info", "--offline", "--json").stdout)
    all_stacks = json.loads(repo.stack("info", "--all", "--offline", "--json").stdout)
    assert {b["name"] for b in current} == {"main", "pr1", "pr2", "pr3"}
    assert {b["name"] for b in all_stacks} == {"main", "pr1", "pr2", "pr3", "other"}


def test_info_current_requires_a_tracked_branch(repo: Repo, client: StackClient) -> None:
    with pytest.raises(StackError, match="main isn't in a stack"):
        client.info(current=True, offline=True)


def test_bare_group_selects_all_current_group_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    client.group_add("feature", ["pr1", "other"])
    repo.git("switch", "--quiet", "pr2")

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

    assert repo.stack("info").stdout == "        ┌── pr3 *\n    ┌── pr2\n┌── pr1\nmain\n"

    assert [call[:2] for call in git_calls if call[0] == "config"] == [
        ["config", "--list"],
        ["config", "--get-colorbool"],
    ]
