import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("parent", help="print the branch a branch (default: the current one) is stacked on")
    p.add_argument("branch", nargs="?", help="branch whose parent to print (default: the current branch)")
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def parent() -> str:
        return stack.parent(args.branch)

    if args.json:
        run_for_json(stack, parent, lambda parent: {"parent": parent})
        return
    print(parent())
