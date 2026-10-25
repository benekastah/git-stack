import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    group: str | None


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("branch", help="create a new branch stacked on the current one")
    p.add_argument("name")
    p.add_argument(
        "--group",
        help="also put its stack in group GROUP (see `git stack group`), failing without "
        "creating it if the stack is in another group already",
    )
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    parent = stack.branch(args.name, group=args.group)
    print(f"created {args.name} on top of {parent}" + (f", in group {args.group}" if args.group else ""))
