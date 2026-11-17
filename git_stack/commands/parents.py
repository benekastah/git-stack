import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("parents", help="print a branch's parents in order")
    p.add_argument("branch", nargs="?", help="branch whose parents to print (default: the current branch)")
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Result:
    parents: list[str]


def run(stack: StackClient, args: Args) -> Result:
    return Result(stack.parents(args.branch))


def show(stack: StackClient, args: Args, result: Result) -> None:
    for parent in result.parents:
        print(parent)
