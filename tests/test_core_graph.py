"""How core selects and protects dependency graphs.

Multiple-base graphs are constructed in memory until storage and replay are
implemented; these tests don't claim that commands can create them yet.
"""

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


def test_core_does_not_partly_save_a_graph_with_multiple_bases(repo: Repo, client: StackClient) -> None:
    stacks = graph()
    stacks.set_ownership("first", Ownership.OWNED)
    stacks.set_ownership("combined", Ownership.SHARED)

    with pytest.raises(StackError, match="multiple bases.*not supported"):
        client.save(stacks)

    assert not repo.config_exists("first", "stackOwnership")
    assert not repo.config_exists("combined", "stackOwnership")
