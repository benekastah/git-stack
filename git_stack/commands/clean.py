from dataclasses import dataclass

from git_stack.commands import (
    PRProtectionArgs,
    Subparsers,
    add_allow_shared_argument,
    add_delete_argument,
    add_ignore_revert_argument,
    add_pr_access_arguments,
    add_pr_protection_arguments,
    pr_protection,
)
from git_stack.commands.info import print_tree
from git_stack.core import Delete
from git_stack.stack import BranchInfo, StackClient


class Args(PRProtectionArgs):
    """The arguments register's parser produces."""

    remote: str | None
    delete: str | None
    ignore_reverts: list[str]
    allow_shared: bool
    no_verify: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, without restacking")
    p.add_argument("--remote")
    p.add_argument("--no-verify", action="store_true", help="skip the pre-push hook")
    add_delete_argument(p)
    add_ignore_revert_argument(p)
    add_allow_shared_argument(p)
    add_pr_protection_arguments(p)
    add_pr_access_arguments(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Cleaned:
    """Every stack, as info() lists them."""

    stacks: list[BranchInfo]


def run(stack: StackClient, args: Args) -> Cleaned:
    return Cleaned(
        stack.clean(
            remote=args.remote,
            delete=Delete(args.delete) if args.delete else None,
            ignore_reverts=args.ignore_reverts,
            allow_shared=args.allow_shared,
            no_verify=args.no_verify,
            **pr_protection(args),
        )
    )


def show(stack: StackClient, args: Args, result: Cleaned) -> None:
    if result.stacks:
        print_tree(stack, result.stacks)
