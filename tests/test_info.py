"""git stack info."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from tests.helpers import Repo


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
    assert [branch.name for branch in client.info(groups=[""], offline=True)] == ["main", "pr1", "pr2", "pr3"]


def test_info_current_requires_a_tracked_branch(repo: Repo, client: StackClient) -> None:
    with pytest.raises(StackError, match="main isn't in a stack"):
        client.info(current=True, offline=True)


def test_bare_group_selects_all_current_group_stacks(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    client.group_add("feature", ["pr1", "other"])
    repo.git("switch", "--quiet", "pr2")

    assert set(client.selected_bottoms([], [""], client.tracked_branches(), "show")) == {"pr1", "other"}
    assert {b.name for b in client.info(groups=[""], offline=True)} == {"main", "pr1", "pr2", "pr3", "other"}


def test_info_remembers_whether_a_branch_on_main_conflicts_with_it(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main\n", "main c1")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK
    cached = repo.cached_check("pr1", "stackConflictCheck")
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
    cached = repo.cached_check("pr1", "stackLandedCheck")
    assert cached is not None
    assert cached == " ".join(
        [
            repo.git("rev-parse", "pr1"),
            repo.git("rev-parse", "main"),
            repo.git("config", "branch.pr1.stackBase"),
            "false",
        ]
    )

    client.checks.set("pr1", "stackLandedCheck", cached.replace("false", "true"), tracked=["pr1"])
    assert repo.branch_info("pr1").status is BranchStatus.MERGED


def test_info_shows_reverted_branch(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("revert", "--no-edit", "HEAD")

    assert repo.branch_info("pr1").status is BranchStatus.REVERTED
