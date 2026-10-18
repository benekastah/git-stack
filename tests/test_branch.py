"""git stack branch."""

from git_stack.stack import StackClient
from tests.helpers import Repo


def test_branch_stacks_new_branch_on_current_one(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")

    assert repo.git("branch", "--show-current") == "pr1"
    repo.assert_stacked("pr1", "main", [])


def test_branch_does_not_inherit_parents_upstream(repo: Repo, client: StackClient) -> None:
    repo.git("config", "branch.autoSetupMerge", "inherit")
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    client.push()

    client.branch("pr2")

    assert not repo.config_exists("pr2", "remote")
    assert not repo.config_exists("pr2", "merge")
