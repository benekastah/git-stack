import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    branches: list[str]


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("untrack", help="stop tracking branches (default: the current one), "
                                              "without deleting them")
    p.add_argument("branches", nargs="*", metavar="branch",
                   help="branches to untrack (default: the current branch)")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    for untracked in stack.untrack(args.branches):
        print(f"stopped tracking {untracked.branch}")
        if untracked.children:
            print(f"note: {', '.join(untracked.children)} still stacked on {untracked.branch}, which is now "
                  f"the bottom of their stack. Move them with `git stack restack --onto <parent>`")
