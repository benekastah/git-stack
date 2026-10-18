import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    old: str | None
    name: str


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("rename", help="rename a branch (default: the current one), "
                                             "keeping branches stacked on it")
    p.add_argument("old", nargs="?", help="branch to rename (default: the current branch)")
    p.add_argument("name", metavar="new", help="new name")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    renamed = stack.rename(args.name, old=args.old)
    print(f"renamed {renamed.old} to {renamed.new}" +
          (f" and moved {', '.join(renamed.children)} onto it" if renamed.children else ""))
    if renamed.upstream:
        print(f"note: {renamed.upstream} wasn't renamed, and pull requests on it or based on it still "
              f"point there. `git stack push` will push {renamed.new} as a new remote branch")
