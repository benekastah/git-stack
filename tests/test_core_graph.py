"""How core selects and protects dependency graphs."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from git_stack.stacks import Ownership, StackNode, Stacks
from tests.helpers import Repo


def graph() -> Stacks:
    return Stacks(
        [
            StackNode("first", "main"),
            StackNode("second", "release", ownership=Ownership.SHARED),
            StackNode("combined", "first", additional_parents=("second",)),
            StackNode("child", "combined"),
        ]
    )


def test_core_selects_all_dependencies_once_for_multiple_selectors(client: StackClient) -> None:
    selected = client.selected_stacks(["combined", "second", "child"], [], graph(), "restack")

    assert selected == ["first", "second", "combined", "child"]


def test_core_checks_shared_ownership_on_every_selected_base(client: StackClient) -> None:
    stacks = graph()
    selected = client.selected_stacks(["child"], [], stacks, "restack")

    with pytest.raises(StackError, match="second.*--allow-shared"):
        client.ensure_owned(selected, stacks, allow_shared=False, command="restack")

    client.ensure_owned(selected, stacks, allow_shared=True, command="restack")


def test_core_reads_legacy_and_multiple_base_metadata(repo: Repo, client: StackClient) -> None:
    repo.git("branch", "first")
    repo.git("branch", "second")
    repo.git("branch", "combined")
    stacks = client.stacks()
    stacks.reparent("first", "main")
    stacks.reparent("second", "main")
    stacks.reparent("combined", "first")
    stacks.set_additional_parents("combined", ("second",))
    client.save(stacks)

    assert repo.git("config", "branch.first.stackParent") == "main"
    assert client.stacks().dependencies("combined") == ("first", "second")


@pytest.mark.parametrize("value", ['git-stack-multi:v1:["first", "first"]', 'git-stack-multi:v2:["first", "second"]'])
def test_core_refuses_invalid_multiple_base_metadata(repo: Repo, client: StackClient, value: str) -> None:
    repo.git("branch", "combined")
    repo.git("config", "branch.combined.stackParent", value)

    with pytest.raises(StackError, match="multiple-base metadata is invalid"):
        client.stacks()
