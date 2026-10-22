import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "base",
        help="print the commit a branch (default: the current one) was last rebased onto, where its own commits start",
    )
    p.add_argument("branch", nargs="?", help="branch whose base to print (default: the current branch)")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    print(stack.base(args.branch))
