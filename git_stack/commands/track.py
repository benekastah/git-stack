import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.errors import StackError
from git_stack.stack import StackClient, Tracked


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    parent: str | None
    branches: list[str]
    base: str | None
    json: bool
    on: list[str]


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "track",
        help="track existing branches as a stack on PARENT, each on the one before it (default: the current branch)",
    )
    p.add_argument("parent", nargs="?")
    p.add_argument(
        "branches", nargs="*", metavar="branch", help="branches to stack, bottom first (default: the current branch)"
    )
    p.add_argument(
        "--base",
        help="commit the first branch was built on (default: where it forked "
        "from PARENT, found from PARENT's reflog like `git rebase` does). "
        "Needed when PARENT has already been squash-merged",
    )
    p.add_argument("--on", action="append", default=[], metavar="PARENT", help="track one branch on PARENT")
    add_json_argument(p)
    p.set_defaults(run=run, show=show, validate=validate)


def validate(parser: argparse.ArgumentParser, args: Args) -> None:
    if args.on and args.branches:
        parser.error("track --on accepts at most one branch")
    if not args.on and args.parent is None:
        parser.error("track requires a parent or --on")


@dataclass(frozen=True)
class TrackResult:
    tracked: list[Tracked]


def run(stack: StackClient, args: Args) -> TrackResult:
    if args.on:
        if len(args.on) > 1:
            raise StackError("tracking multiple bases is not supported yet")
        return TrackResult(stack.track(args.on[0], [args.parent] if args.parent else [], base=args.base))
    if args.parent is None:
        raise StackError("track requires a parent or --on")
    return TrackResult(stack.track(args.parent, args.branches, base=args.base))


def show(stack: StackClient, args: Args, result: TrackResult) -> None:
    for tracked in result.tracked:
        print(f"tracking {tracked.branch} on top of {tracked.parent} (base {tracked.base[:12]})")
