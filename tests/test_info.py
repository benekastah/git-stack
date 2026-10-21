"""git stack info."""

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


def test_info_remembers_whether_a_branch_on_main_conflicts_with_it(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main\n", "main c1")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK
    cached = repo.git("config", "branch.pr1.stackConflictCheck")
    assert cached == f"{repo.git('rev-parse', 'pr1')} {repo.git('rev-parse', 'main')} true"

    # The remembered answer is used while neither tip moves...
    repo.git("config", "branch.pr1.stackConflictCheck", cached.replace("true", "false"))
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    # ...and checked again once one does.
    repo.commit("b", "main again\n", "main c2")
    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK


def test_info_remembers_whether_a_branch_landed_in_its_parent(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE
    cached = repo.git("config", "branch.pr1.stackLandedCheck")
    assert cached == " ".join([repo.git("rev-parse", "pr1"), repo.git("rev-parse", "main"),
                               repo.git("config", "branch.pr1.stackBase"), "false"])

    repo.git("config", "branch.pr1.stackLandedCheck", cached.replace("false", "true"))
    assert repo.branch_info("pr1").status is BranchStatus.MERGED
