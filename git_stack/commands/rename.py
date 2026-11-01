import argparse
import dataclasses

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import Renamed, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    old: str | None
    name: str
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "rename", help="rename a branch (default: the current one), keeping branches stacked on it"
    )
    p.add_argument("old", nargs="?", help="branch to rename (default: the current branch)")
    p.add_argument("name", metavar="new", help="new name")
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def rename() -> Renamed:
        return stack.rename(args.name, old=args.old)

    if args.json:
        run_for_json(stack, rename, dataclasses.asdict)
        return
    renamed = rename()
    print(
        f"renamed {renamed.old} to {renamed.new}"
        + (f" and moved {', '.join(renamed.children)} onto it" if renamed.children else "")
    )
    if renamed.upstream:
        print(
            f"note: {renamed.upstream} wasn't renamed, and pull requests on it or based on it still "
            f"point there. `git stack push` will push {renamed.new} as a new remote branch"
        )
