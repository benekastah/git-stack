import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    onto: str | None
    squash: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("restack", help="rebase every branch in the current stack onto its parent")
    p.add_argument("--onto", help="first change the current branch's parent to ONTO")
    p.add_argument("--squash", action="store_true",
                   help="first squash each branch in the stack into a single commit")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    stack.restack(onto=args.onto, squash=args.squash)
