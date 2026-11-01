import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument, add_stack_arguments
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    branches: list[str]
    groups: list[str]
    move: bool
    old: str | None
    new: str
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("group", help="put stacks in a group, take them out, or rename a group")
    actions = p.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="put stacks in group NAME, so commands on one cover them all")
    add.add_argument("name", help="the group")
    add_stack_arguments(add, "group")
    add.add_argument("--move", action="store_true", help="move stacks that are in another group out of it")
    add_json_argument(add)
    add.set_defaults(run=run_add, show=show_add)

    remove = actions.add_parser("remove", help="take stacks out of their groups (also `git stack ungroup`)")
    add_stack_arguments(remove, "ungroup")
    add_json_argument(remove)
    remove.set_defaults(run=run_remove, show=show_remove)

    rename = actions.add_parser("rename", help="rename a group (default: the current stack's)")
    rename.add_argument("old", nargs="?", help="group to rename (default: the current stack's)")
    rename.add_argument("new", help="new name")
    add_json_argument(rename)
    rename.set_defaults(run=run_rename, show=show_rename)

    ungroup = subparsers.add_parser("ungroup", help="take stacks out of their groups (`git stack group remove`)")
    add_stack_arguments(ungroup, "ungroup")
    add_json_argument(ungroup)
    ungroup.set_defaults(run=run_remove, show=show_remove)


@dataclass(frozen=True)
class Added:
    """bottoms are the bottom branches of the stacks put in group."""

    group: str
    bottoms: list[str]


@dataclass(frozen=True)
class Ungrouped:
    """A stack, by its bottom branch, taken out of group."""

    bottom: str
    group: str


@dataclass(frozen=True)
class Removed:
    removed: list[Ungrouped]


@dataclass(frozen=True)
class GroupRenamed:
    """branches are the branches in the group."""

    old: str
    new: str
    branches: list[str]


def run_add(stack: StackClient, args: Args) -> Added:
    return Added(args.name, stack.group_add(args.name, args.branches, args.groups, move=args.move))


def show_add(stack: StackClient, args: Args, added: Added) -> None:
    print(f"put the stacks of {', '.join(added.bottoms)} in group {added.group}")


def run_remove(stack: StackClient, args: Args) -> Removed:
    return Removed([Ungrouped(bottom, group) for bottom, group in stack.group_remove(args.branches, args.groups)])


def show_remove(stack: StackClient, args: Args, result: Removed) -> None:
    if not result.removed:
        print("none of those stacks were in a group")
    for ungrouped in result.removed:
        print(f"took {ungrouped.bottom}'s stack out of group {ungrouped.group}")


def run_rename(stack: StackClient, args: Args) -> GroupRenamed:
    old, branches = stack.group_rename(args.new, old=args.old)
    return GroupRenamed(old, args.new, branches)


def show_rename(stack: StackClient, args: Args, renamed: GroupRenamed) -> None:
    print(f"renamed group {renamed.old} to {renamed.new}")
