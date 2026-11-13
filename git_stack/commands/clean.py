import argparse
from dataclasses import dataclass

from git_stack.commands import (
    Subparsers,
    add_allow_shared_argument,
    add_delete_argument,
    add_ignore_revert_argument,
    add_json_argument,
)
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import BranchInfo, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    delete: str | None
    ignore_reverts: list[str]
    json: bool
    allow_shared: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, without restacking")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    add_ignore_revert_argument(p)
    add_json_argument(p)
    add_allow_shared_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Cleaned:
    """Every stack, as info() lists them."""

    stacks: list[BranchInfo]


def run(stack: StackClient, args: Args) -> Cleaned:
    return Cleaned(
        stack.clean(
            remote=args.remote, delete=args.delete, ignore_reverts=args.ignore_reverts, allow_shared=args.allow_shared
        )
    )


def show(stack: StackClient, args: Args, result: Cleaned) -> None:
    if result.stacks:
        print_tree(stack, result.stacks)
