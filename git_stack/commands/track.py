import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
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
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class TrackResult:
    tracked: list[Tracked]


def run(stack: StackClient, args: Args) -> TrackResult:
    return TrackResult(stack.track(args.parent, args.branches, base=args.base))


def show(stack: StackClient, args: Args, result: TrackResult) -> None:
    for tracked in result.tracked:
        print(f"tracking {tracked.branch} on top of {tracked.parent} (base {tracked.base[:12]})")
