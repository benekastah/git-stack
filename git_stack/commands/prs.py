import argparse

from git_stack.commands import Subparsers, add_json_argument, add_stack_arguments
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import BranchInfo, PrsResult, PullRequestAction, PullRequestStep, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str
    dry_run: bool
    json: bool


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
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> PrsResult:
    return stack.prs(args.branches, args.groups, remote=args.remote, dry_run=args.dry_run)


def show(stack: StackClient, args: Args, result: PrsResult) -> None:
    if not result.steps:
        print("nothing to open: every branch in the current stack looks merged")
    show_steps(stack, result.steps, result.branches, args.dry_run)


def show_steps(stack: StackClient, steps: list[PullRequestStep], branches: list[BranchInfo], dry_run: bool) -> None:
    """Print what opening pull requests did (or would do), then the stacks,
    with the pull requests it opened in green."""
    new_links = {}
    for step in steps:
        if step.action is PullRequestAction.NO_COMMITS:
            print(f"skipping {step.branch}: it has no commits of its own")
        elif step.action is PullRequestAction.OPEN:
            new_links[step.branch] = f"would open: {step.title}" if dry_run else step.url or ""
    if branches:
        print_tree(stack, branches, new_links)
