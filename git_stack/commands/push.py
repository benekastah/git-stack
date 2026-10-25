import argparse

from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str
    force: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "push",
        help="force-push (with lease) every branch in the current stack (or the ones "
        "named), refusing if that would drop commits on the remote",
    )
    add_stack_arguments(p, "push")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument(
        "--force",
        action="store_true",
        help="push even if that drops commits on a remote branch that the local one doesn't "
        "have. Still fails if a remote branch changed since the last fetch",
    )
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    if not stack.push(args.branches, args.groups, remote=args.remote, force=args.force):
        where = "those stacks" if args.branches or args.groups else f"{stack.git.current_branch()}'s stack"
        print(f"nothing to push: every branch in {where} looks merged")
