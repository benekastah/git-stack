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
