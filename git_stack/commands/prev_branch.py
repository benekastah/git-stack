import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("prev", help="switch to the branch the current one is stacked on")
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: argparse.Namespace) -> None:
    if args.json:
        run_for_json(stack, stack.prev, lambda branch: {"branch": branch})
        return
    print(f"switched to {stack.prev()}")
