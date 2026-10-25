import argparse

from git_stack.commands import Subparsers, add_latest_argument, add_stack_arguments
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    onto: str | None
    squash: bool
    latest: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "restack",
        help="rebase every branch in the current stack (or the named ones) onto its parent, "
        "except that branches on the stack's root only move if "
        "they conflict with it",
    )
    add_stack_arguments(p, "restack")
    p.add_argument("--onto", help="first change the current branch's parent to ONTO")
    p.add_argument("--squash", action="store_true", help="first squash each branch in the stack into a single commit")
    add_latest_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    stack.restack(args.branches, args.groups, onto=args.onto, squash=args.squash, latest=args.latest)
