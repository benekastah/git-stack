import argparse
from dataclasses import dataclass

from git_stack.commands import (
    Subparsers,
    add_allow_shared_argument,
    add_latest_argument,
    add_stack_arguments,
)
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    onto: str | None
    latest: bool
    prs: bool
    force: bool
    allow_shared: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "restack",
        help="rebase every branch in the current stack (or the named ones) onto its parent, "
        "except that branches on the stack's root only move if "
        "they conflict with it",
    )
    add_stack_arguments(p, "restack", optional_group=True)
    p.add_argument(
        "--onto",
        help="first change the parent of the one branch named (default: the current branch) to ONTO",
    )
    p.add_argument("--force", action="store_true", help="replace the tip of an explicitly named integration root")
    p.add_argument("--prs", action="store_true", help="also check GitHub pull requests for merge evidence")
    add_latest_argument(p)
    add_allow_shared_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Restacked:
    """Nothing: what restack did is in its events."""


def run(stack: StackClient, args: Args) -> Restacked:
    stack.restack(
        args.branches,
        args.groups,
        onto=args.onto,
        latest=args.latest,
        allow_shared=args.allow_shared,
        force=args.force,
        prs=args.prs,
    )
    return Restacked()


def show(stack: StackClient, args: Args, result: Restacked) -> None:
    pass
