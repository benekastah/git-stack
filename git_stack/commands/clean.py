import argparse

from git_stack.commands import Subparsers, add_delete_argument, add_ignore_revert_argument
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    delete: str | None
    ignore_reverts: list[str]


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, without restacking")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    add_ignore_revert_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    branches = stack.clean(remote=args.remote, delete=args.delete, ignore_reverts=args.ignore_reverts)
    if branches:
        print_tree(stack, branches)
