import argparse

from git_stack.commands import Subparsers, add_json_argument
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient, Unreverted


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branch: str | None
    remote: str
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "unrevert",
        help="rebuild a branch (default: the current one) whose merge was reverted, "
        "as commits on its parent that revert the reverts",
    )
    p.add_argument("branch", nargs="?", help="branch to unrevert (default: the current branch)")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> Unreverted:
    return stack.unrevert(args.branch, remote=args.remote)


def show(stack: StackClient, args: Args, unreverted: Unreverted) -> None:
    count = len(unreverted.reverts)
    print(f"rebuilt {unreverted.branch} on {unreverted.parent}, reverting {count} revert{'s' if count != 1 else ''}")
    if unreverted.children:
        print(f"note: run `git stack restack` to move {', '.join(unreverted.children)} onto it")
    print("note: run `git stack push` and `git stack prs` to open a new pull request for it")
