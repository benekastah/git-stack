import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient

SELECTORS_HELP = (
    "the stacks of these branches, of the branches in these groups, or of the branches "
    "these globs match (quote them, so the shell doesn't expand them); default: the current stack"
)


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    selectors: list[str]
    old: str | None
    new: str


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("group", help="put stacks in a group, take them out, or rename a group")
    actions = p.add_subparsers(dest="action", required=True)

    add = actions.add_parser("add", help="put stacks in group NAME, so commands on one cover them all")
    add.add_argument("name", help="the group")
    add.add_argument("selectors", nargs="*", metavar="branch|group|glob", help=SELECTORS_HELP)
    add.set_defaults(func=run_add)

    remove = actions.add_parser("remove", help="take stacks out of their groups")
    remove.add_argument("selectors", nargs="*", metavar="branch|group|glob", help=SELECTORS_HELP)
    remove.set_defaults(func=run_remove)

    rename = actions.add_parser("rename", help="rename a group (default: the current stack's)")
    rename.add_argument("old", nargs="?", help="group to rename (default: the current stack's)")
    rename.add_argument("new", help="new name")
    rename.set_defaults(func=run_rename)


def run_add(stack: StackClient, args: Args) -> None:
    bottoms = stack.group_add(args.name, args.selectors)
    print(f"put the stacks of {', '.join(bottoms)} in group {args.name}")


def run_remove(stack: StackClient, args: Args) -> None:
    removed = stack.group_remove(args.selectors)
    if not removed:
        print("none of those stacks were in a group")
    for bottom, group in removed:
        print(f"took {bottom}'s stack out of group {group}")


def run_rename(stack: StackClient, args: Args) -> None:
    old = stack.group_rename(args.new, old=args.old)[0]
    print(f"renamed group {old} to {args.new}")
