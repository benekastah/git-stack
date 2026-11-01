import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient, Untracked


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "untrack", help="stop tracking branches (default: the current one), without deleting them"
    )
    p.add_argument(
        "branches",
        nargs="*",
        metavar="branch|glob",
        help="branches to untrack, or globs of them (quote them, so the shell doesn't expand them); "
        "default: the current branch",
    )
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class UntrackResult:
    untracked: list[Untracked]


def run(stack: StackClient, args: Args) -> UntrackResult:
    return UntrackResult(stack.untrack(args.branches))


def show(stack: StackClient, args: Args, result: UntrackResult) -> None:
    for untracked in result.untracked:
        print(f"stopped tracking {untracked.branch}")
        if untracked.children:
            print(
                f"note: {', '.join(untracked.children)} still stacked on {untracked.branch}, which is now "
                f"the bottom of their stack. Move them with `git stack restack --onto <parent>`"
            )
