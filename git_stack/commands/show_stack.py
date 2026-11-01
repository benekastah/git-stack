import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.errors import StackError
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None
    quiet: bool
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "show-stack", help="print the branches in a branch's (default: the current one's) stack, bottom first"
    )
    p.add_argument("branch", nargs="?", help="branch whose stack to print (default: the current branch)")
    output = p.add_mutually_exclusive_group()
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="print nothing; exit with status 0 if the branch is in a stack, and 1 if it isn't",
    )
    add_json_argument(output)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Shown:
    """The branches in the stack, bottom first, or None if --quiet found the
    branch isn't in one."""

    branches: list[str] | None


def run(stack: StackClient, args: Args) -> Shown:
    try:
        return Shown(stack.show_stack(args.branch))
    except StackError:
        if args.quiet:
            return Shown(None)
        raise


def show(stack: StackClient, args: Args, result: Shown) -> int:
    if result.branches is None:
        return 1
    if not args.quiet:
        for branch in result.branches:
            print(branch)
    return 0
