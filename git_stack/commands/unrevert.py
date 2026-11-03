import argparse

from git_stack.commands import Subparsers, add_json_argument
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient, Unreverted


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    reverts: list[str]
    from_branch: str | None
    stack: list[str]
    remote: str
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "unrevert",
        help="create a branch that reverts reverts, to bring back a merged branch they reverted, "
        "and move a stack onto it",
    )
    p.add_argument("name", help="the branch to create")
    p.add_argument("reverts", nargs="+", metavar="revert", help="the commits to revert, in any order")
    p.add_argument(
        "--from",
        dest="from_branch",
        metavar="BRANCH",
        help="create it on BRANCH (default: the root of the first --stack branch's stack, or else the "
        "remote's default branch)",
    )
    p.add_argument(
        "--stack",
        action="append",
        default=[],
        metavar="BRANCH",
        help="move BRANCH, and the branches stacked on it, onto the new branch (can be given more than once)",
    )
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> Unreverted:
    return stack.unrevert(args.name, args.reverts, args.from_branch, args.stack, remote=args.remote)


def show(stack: StackClient, args: Args, unreverted: Unreverted) -> None:
    count = len(unreverted.reverts)
    print(f"created {unreverted.branch} on {unreverted.parent}, reverting {count} revert{'s' if count != 1 else ''}")
    if unreverted.children:
        print(f"moved {', '.join(unreverted.children)} onto it")
    print(f"note: run `git stack push --prs` to open a pull request for {unreverted.branch}")
