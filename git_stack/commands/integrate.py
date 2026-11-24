import argparse

from git_stack.commands import Subparsers, add_pr_access_arguments
from git_stack.stack import Integrated, StackClient


class Args(argparse.Namespace):
    name: str
    sources: list[str]
    root: str | None
    group: bool
    adopt: bool
    abort: bool


def register(subparsers: Subparsers) -> None:
    parser = subparsers.add_parser("integrate", help="build an integration root from local source branches")
    parser.add_argument("name", help="integration branch")
    parser.add_argument("sources", nargs="*", metavar="source", help="local source branches, in application order")
    parser.add_argument(
        "-g",
        "--group",
        action="store_true",
        help="create a group integration from the current group's leaves on the default branch",
    )
    parser.add_argument("--root", help="start the build here (default: main)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--adopt", action="store_true", help="register an existing snapshot and rebuild it on refresh")
    mode.add_argument("--abort", action="store_true", help="discard a stalled build or release its cached worktree")
    add_pr_access_arguments(parser)
    parser.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> Integrated:
    return stack.integrate(
        args.name, args.sources, root=args.root, group=args.group, adopt=args.adopt, abort=args.abort
    )


def show(stack: StackClient, args: Args, result: Integrated) -> None:
    if result.tip is not None:
        print(f"{result.name} (integration)")
