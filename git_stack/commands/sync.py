import argparse

from git_stack.commands import Subparsers, add_delete_argument, add_latest_argument
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    delete: str | None
    latest: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "sync", help="fetch, untrack the current stack's merged branches, restack it, and pull in commits others pushed"
    )
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    add_latest_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    unpushed = stack.sync(remote=args.remote, delete=args.delete, latest=args.latest)
    if unpushed:
        print("note: run `git stack push` to update the remote branches and pull requests of:")
        for branch in unpushed:
            print(f"  {branch}")
