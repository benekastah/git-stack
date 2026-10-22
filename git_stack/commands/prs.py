import argparse

from git_stack.commands import Subparsers
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import PullRequestAction, PullRequestStep, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    dry_run: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "prs",
        aliases=["pr"],
        help="open a draft pull request on GitHub for each branch in the current "
        "stack that has none, into its stack parent",
    )
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument(
        "-n", "--dry-run", action="store_true", help="print the pull requests it would open, without opening them"
    )
    p.set_defaults(func=run)


def describe(step: PullRequestStep, dry_run: bool) -> str:
    match step.action:
        case PullRequestAction.OPEN if dry_run:
            return f"would open a draft pull request for {step.branch} into {step.parent}: {step.title}"
        case PullRequestAction.OPEN:
            return f"opened draft pull request #{step.number} for {step.branch} into {step.parent}: {step.url}"
        case PullRequestAction.EXISTS:
            return f"{step.branch} already has pull request #{step.number}"
        case PullRequestAction.NO_COMMITS:
            return f"skipping {step.branch}: it has no commits of its own"


def run(stack: StackClient, args: Args) -> None:
    steps = stack.prs(remote=args.remote, dry_run=args.dry_run)
    if not steps:
        print("nothing to open: every branch in the current stack looks merged")
    for step in steps:
        print(describe(step, args.dry_run))
