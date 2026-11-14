import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument, add_stack_arguments
from git_stack.core import CURRENT_GROUP
from git_stack.errors import StackError
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    ownership: str
    branches: list[str]
    groups: list[str]
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("ownership", help="mark stacks as owned or shared")
    p.add_argument("ownership", nargs="?", default="show", choices=("show", "owned", "shared"))
    add_stack_arguments(p, "mark", optional_group=True)
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Ownership:
    ownership: str
    branches: list[str]


def run(stack: StackClient, args: Args) -> Ownership:
    if args.ownership == "show":
        ownership, branches = stack.get_ownership(args.branches, args.groups)
        return Ownership(ownership, branches)
    if CURRENT_GROUP in args.groups:
        raise StackError("--group requires a group name when changing ownership")
    return Ownership(args.ownership, stack.set_ownership(args.ownership, args.branches, args.groups))


def show(stack: StackClient, args: Args, result: Ownership) -> None:
    if args.ownership == "show":
        print(f"{result.ownership}: {', '.join(result.branches)}")
    else:
        print(f"marked {', '.join(result.branches)} as {result.ownership}")
