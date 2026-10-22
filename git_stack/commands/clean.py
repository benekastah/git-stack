import argparse

from git_stack.commands import Subparsers, add_delete_argument
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    delete: str | None


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, without restacking")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    stack.clean(remote=args.remote, delete=args.delete)
