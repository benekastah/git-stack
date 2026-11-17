import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("bases", help="print a branch's recorded base commits in order")
    p.add_argument("branch", nargs="?", help="branch whose bases to print (default: the current branch)")
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Result:
    bases: list[str]


def run(stack: StackClient, args: Args) -> Result:
    return Result(stack.bases(args.branch))


def show(stack: StackClient, args: Args, result: Result) -> None:
    for base in result.bases:
        print(base)
