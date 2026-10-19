import argparse

from git_stack.commands import Subparsers
from git_stack.stack import StackClient


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("down", help="switch to the branch the current one is stacked on")
    p.set_defaults(func=run)


def run(stack: StackClient, args: argparse.Namespace) -> None:
    print(f"switched to {stack.down()}")
