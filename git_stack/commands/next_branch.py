import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("next", help="switch to the branch stacked on the current one")
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Switched:
    """The branch it switched to."""

    branch: str


def run(stack: StackClient, args: argparse.Namespace) -> Switched:
    return Switched(stack.next())


def show(stack: StackClient, args: argparse.Namespace, result: Switched) -> None:
    print(f"switched to {result.branch}")
