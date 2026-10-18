import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    name: str


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("branch", help="create a new branch stacked on the current one")
    p.add_argument("name")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    parent = stack.branch(args.name)
    print(f"created {args.name} on top of {parent}")
