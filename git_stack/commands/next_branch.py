import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("next", help="switch to the branch stacked on the current one")
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: argparse.Namespace) -> None:
    if args.json:
        run_for_json(stack, stack.next, lambda branch: {"branch": branch})
        return
    print(f"switched to {stack.next()}")
