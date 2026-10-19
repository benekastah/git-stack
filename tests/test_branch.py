"""git stack branch."""

import pytest

from git_stack.errors import StackError
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


def test_branch_refuses_name_taken_on_remote(repo: Repo, client: StackClient) -> None:
    repo.push_someone_elses_branch("fix")

    with pytest.raises(StackError, match="origin/fix already exists"):
        client.branch("fix")

    assert repo.git("branch", "--show-current") == "main"
    assert repo.git("branch", "--list", "fix") == ""
