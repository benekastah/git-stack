"""git stack rename."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from tests.helpers import Repo


def test_rename_keeps_branch_and_children_in_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    old_base = repo.config("pr2", "stackBase")

    client.rename("middle")

    assert repo.git("branch", "--show-current") == "middle"
    assert repo.config("middle", "stackParent") == "pr1"
    assert repo.config("middle", "stackBase") == old_base
    assert repo.config("pr3", "stackParent") == "middle"
    assert client.info() == [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=False),
        BranchInfo("middle", "pr1", BranchStatus.UP_TO_DATE, current=True),
        BranchInfo("pr3", "middle", BranchStatus.UP_TO_DATE, current=False),
    ]


def test_rename_other_branch_by_name(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr3")

    client.rename("first", old="pr1")

    assert repo.git("branch", "--show-current") == "pr3"
    assert repo.config("first", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "first"


def test_rename_missing_branch_fails(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="nope isn't a local branch"):
        client.rename("other", old="nope")



def test_rename_pushed_branch_unsets_old_upstream(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr1")

    renamed = client.rename("first")

    assert renamed.upstream == "origin/pr1"
    assert not repo.config_exists("first", "merge")
    client.push()
    assert repo.git("rev-parse", "--abbrev-ref", "first@{upstream}") == "origin/first"


def test_rename_to_existing_branch_changes_nothing(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    with pytest.raises(StackError):
        client.rename("pr3")

    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.git("branch", "--show-current") == "pr1"
