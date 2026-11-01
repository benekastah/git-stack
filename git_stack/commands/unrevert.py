import argparse
import dataclasses

from git_stack.commands import Subparsers, add_json_argument, run_for_json
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
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def unrevert() -> Unreverted:
        return stack.unrevert(args.branch, remote=args.remote)

    if args.json:
        run_for_json(stack, unrevert, dataclasses.asdict)
        return
    unreverted = unrevert()
    count = len(unreverted.reverts)
    print(f"rebuilt {unreverted.branch} on {unreverted.parent}, reverting {count} revert{'s' if count != 1 else ''}")
    if unreverted.children:
        print(f"note: run `git stack restack` to move {', '.join(unreverted.children)} onto it")
    print("note: run `git stack push` and `git stack prs` to open a new pull request for it")
