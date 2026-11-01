import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("parent", help="print the branch a branch (default: the current one) is stacked on")
    p.add_argument("branch", nargs="?", help="branch whose parent to print (default: the current branch)")
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Result:
    parent: str


def run(stack: StackClient, args: Args) -> Result:
    return Result(stack.parent(args.branch))


def show(stack: StackClient, args: Args, result: Result) -> None:
    print(result.parent)
