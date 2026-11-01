import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "base",
        help="print the commit a branch (default: the current one) was last rebased onto, where its own commits start",
    )
    p.add_argument("branch", nargs="?", help="branch whose base to print (default: the current branch)")
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def base() -> str:
        return stack.base(args.branch)

    if args.json:
        run_for_json(stack, base, lambda base: {"base": base})
        return
    print(base())
