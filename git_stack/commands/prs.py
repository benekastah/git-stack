import argparse

from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import PullRequestAction, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str
    dry_run: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "prs",
        aliases=["pr"],
        help="open a draft pull request on GitHub for each branch in the current "
        "stack (or the named ones) that has none, into its stack parent",
    )
    add_stack_arguments(p, "open pull requests for")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="print the pull requests it would open, without opening them"
    )
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    result = stack.prs(args.branches, args.groups, remote=args.remote, dry_run=args.dry_run)
    if not result.steps:
        print("nothing to open: every branch in the current stack looks merged")
    new_links = {}
    for step in result.steps:
        if step.action is PullRequestAction.NO_COMMITS:
            print(f"skipping {step.branch}: it has no commits of its own")
        elif step.action is PullRequestAction.OPEN:
            new_links[step.branch] = f"would open: {step.title}" if args.dry_run else step.url or ""
    if result.branches:
        print_tree(stack, result.branches, new_links)
