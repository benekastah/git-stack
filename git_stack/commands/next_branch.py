import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("next", help="switch to the branch stacked on the current one")
    p.set_defaults(func=run)


def run(stack: StackClient, args: argparse.Namespace) -> None:
    print(f"switched to {stack.next()}")
