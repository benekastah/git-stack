"""Stacks, the tracked branches as trees, which commands change in memory and
StackCore.save writes back."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from git_stack.stacks import Ownership, StackNode, Stacks
from tests.helpers import Repo


def tree() -> Stacks:
    """main <- a <- b <- c, a <- d, and release <- r."""
    return Stacks(
        [
            StackNode("a", "main"),
            StackNode("b", "a"),
            StackNode("c", "b"),
            StackNode("d", "a"),
            StackNode("r", "release"),
        ]
    )


def test_walks_the_tree() -> None:
    stacks = tree()

    assert stacks.ancestry("c") == ["c", "b", "a", "main"]
    assert stacks.root("c") == "main"
    assert stacks.bottom("c") == "a"
    assert stacks.children("a") == ["b", "d"]
    assert stacks.descendants("a") == ["b", "c", "d"]
    assert stacks.descendants("main") == ["a", "b", "c", "d"]
    assert stacks.roots() == ["main", "release"]
    assert stacks.in_order() == ["a", "b", "c", "d", "r"]
    assert stacks.stack_of("d") == ["a", "b", "c", "d"]
    assert stacks.is_bottom("a")
    assert not stacks.is_bottom("b")


def test_refuses_a_cycle() -> None:
    stacks = tree()

    with pytest.raises(StackError, match="c is stacked on a"):
        stacks.ensure_no_cycle("a", "c")
    stacks.ensure_no_cycle("d", "c")
    with pytest.raises(StackError, match="cycle in stack metadata"):
        Stacks([StackNode("x", "y"), StackNode("y", "x")]).ancestry("x")


def test_a_group_joins_stacks_on_different_roots() -> None:
    stacks = Stacks([StackNode("a", "main", group="g"), StackNode("b", "a"), StackNode("r", "release", group="g")])

    assert stacks.stack_of("b") == ["a", "b", "r"]
    assert stacks.stack_of("r") == ["r", "a", "b"]
    assert stacks.stack_of("b", groups=False) == ["a", "b"]
    assert stacks.bottoms_in("g") == ["a", "r"]


def test_restricted_leaves_out_the_rest() -> None:
    stacks = tree().restricted(["c", "b", "nope"])

    assert list(stacks) == ["c", "b"]
    assert stacks.roots() == ["a"]
    assert stacks.is_bottom("b")


def test_reparent_tracks_a_new_branch() -> None:
    stacks = tree()

    stacks.reparent("e", "c")

    assert stacks.ancestry("e") == ["e", "c", "b", "a", "main"]


def test_reparent_onto_a_tracked_branch_leaves_the_group() -> None:
    stacks = Stacks([StackNode("a", "main", group="g"), StackNode("x", "main")])

    stacks.reparent("a", "x")

    assert stacks.group("a") is None


def test_reparent_onto_the_root_joins_the_group_it_was_in() -> None:
    stacks = Stacks([StackNode("a", "main", group="g"), StackNode("b", "a")])

    stacks.reparent("b", "main")

    assert stacks.group("b") == "g"


def test_reparent_shares_the_branch_and_its_children_with_a_shared_stack() -> None:
    stacks = Stacks(
        [
            StackNode("a", "main", ownership=Ownership.SHARED),
            StackNode("x", "main"),
            StackNode("y", "x", ownership=Ownership.OWNED),
        ]
    )

    stacks.reparent("x", "a")

    assert stacks.is_shared("x")
    assert stacks.is_shared("y")


def test_drop_moves_the_children_down_without_changing_anything_else() -> None:
    stacks = Stacks([StackNode("a", "main"), StackNode("b", "a"), StackNode("c", "a")])

    assert stacks.drop("a") == ["b", "c"]

    assert stacks.parents() == {"b": "main", "c": "main"}
    assert stacks.groups() == {}


def test_remove_merged_groups_the_children_left_on_the_root() -> None:
    stacks = Stacks([StackNode("a", "main"), StackNode("b", "a"), StackNode("c", "a")])

    assert stacks.remove_merged("a") == ["b", "c"]

    assert stacks.parents() == {"b": "main", "c": "main"}
    assert stacks.groups() == {"b": "a", "c": "a"}


def test_remove_merged_keeps_a_group_the_branch_was_in() -> None:
    stacks = Stacks([StackNode("a", "main", group="g"), StackNode("b", "a")])

    stacks.remove_merged("a")

    assert stacks.groups() == {"b": "g"}


def test_remove_merged_leaves_children_on_a_tracked_parent_ungrouped() -> None:
    stacks = Stacks([StackNode("a", "main"), StackNode("b", "a"), StackNode("c", "b")])

    stacks.remove_merged("b")

    assert stacks.parents() == {"a": "main", "c": "a"}
    assert stacks.groups() == {}


def test_remove_merged_refuses_to_join_an_existing_group_of_its_name() -> None:
    stacks = Stacks([StackNode("a", "main"), StackNode("b", "a"), StackNode("x", "main", group="a")])

    with pytest.raises(StackError, match="group a already exists"):
        stacks.remove_merged("a")

    assert "a" in stacks


def test_untrack_leaves_the_children_on_the_branch_and_shares_them_if_their_stack_was() -> None:
    stacks = Stacks([StackNode("a", "main", ownership=Ownership.SHARED), StackNode("b", "a"), StackNode("c", "b")])

    stacks.untrack(["b"])

    assert stacks.parents() == {"a": "main", "c": "b"}
    assert stacks.is_bottom("c")
    assert stacks.is_shared("c")


def test_changes_are_what_changed_since_reading_or_saving() -> None:
    stacks = tree()

    stacks.reparent("e", "c")
    stacks.set_group("a", "g")
    stacks.untrack(["r"])

    assert stacks.changes() == [
        ("a", StackNode("a", "main"), StackNode("a", "main", group="g")),
        ("r", StackNode("r", "release"), None),
        ("e", None, StackNode("e", "c")),
    ]
    stacks.mark_saved()
    assert stacks.changes() == []


def test_save_writes_what_changed(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    stacks = client.stacks()

    stacks.set_group("pr1", "g")
    stacks.set_ownership("pr2", Ownership.SHARED)
    stacks.untrack(["pr3"])
    client.save(stacks)

    assert client.stacks().parents() == {"pr1": "main", "pr2": "pr1"}
    assert repo.git("config", "branch.pr1.stackGroup") == "g"
    assert repo.git("config", "branch.pr2.stackOwnership") == "shared"
    assert not [key for branch, key in client.git.branch_config() if branch == "pr3" and key.startswith("stack")]
