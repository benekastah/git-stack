import argparse
import dataclasses

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient, Tracked


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    parent: str
    branches: list[str]
    base: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "track",
        help="track existing branches as a stack on PARENT, each on the one before it (default: the current branch)",
    )
    p.add_argument("parent")
    p.add_argument(
        "branches", nargs="*", metavar="branch", help="branches to stack, bottom first (default: the current branch)"
    )
    p.add_argument(
        "--base",
        help="commit the first branch was built on (default: where it forked "
        "from PARENT, found from PARENT's reflog like `git rebase` does). "
        "Needed when PARENT has already been squash-merged",
    )
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def track() -> list[Tracked]:
        return stack.track(args.parent, args.branches, base=args.base)

    if args.json:
        run_for_json(stack, track, lambda tracked: {"tracked": [dataclasses.asdict(t) for t in tracked]})
        return
    for tracked in track():
        print(f"tracking {tracked.branch} on top of {tracked.parent} (base {tracked.base[:12]})")
