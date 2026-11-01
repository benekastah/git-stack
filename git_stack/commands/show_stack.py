import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
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
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> int:
    if args.json:
        run_for_json(stack, lambda: stack.show_stack(args.branch), lambda branches: {"branches": branches})
        return 0
    try:
        branches = stack.show_stack(args.branch)
    except StackError:
        if args.quiet:
            return 1
        raise
    if not args.quiet:
        for branch in branches:
            print(branch)
    return 0
