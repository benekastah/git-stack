import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    branch: str | None


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("untrack", help="stop tracking a branch (default: the current one), "
                                              "without deleting it")
    p.add_argument("branch", nargs="?", help="branch to untrack (default: the current branch)")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    untracked = stack.untrack(args.branch)
    print(f"stopped tracking {untracked.branch}")
    if untracked.children:
        print(f"note: {', '.join(untracked.children)} still stacked on {untracked.branch}, which is now "
              f"the bottom of their stack. Move them with `git stack restack --onto <parent>`")
