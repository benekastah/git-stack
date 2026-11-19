import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.stack import StackClient
from git_stack.stacks import Ownership


class Args(argparse.Namespace):
    ownership: str
    branches: list[str]
    groups: list[str]


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("ownership", help="mark stacks as owned or shared")
    p.add_argument("ownership", nargs="?", default="show", choices=("show", *(o.value for o in Ownership)))
    add_stack_arguments(p, "mark", optional_group=True)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Result:
    ownership: Ownership
    branches: list[str]


def run(stack: StackClient, args: Args) -> Result:
    if args.ownership == "show":
        ownership, branches = stack.get_ownership(args.branches, args.groups)
        return Result(ownership, branches)
    ownership = Ownership(args.ownership)
    return Result(ownership, stack.set_ownership(ownership, args.branches, args.groups))


def show(stack: StackClient, args: Args, result: Result) -> None:
    if args.ownership == "show":
        print(f"{result.ownership.value}: {', '.join(result.branches)}")
    else:
        print(f"marked {', '.join(result.branches)} as {result.ownership.value}")
