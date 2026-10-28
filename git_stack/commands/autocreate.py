import argparse

from git_stack.commands import Subparsers
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import AutocreateAction, Autocreated, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    dry_run: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "autocreate",
        help="track your open pull requests on GitHub (drafts included) as "
        "stacks, creating local branches for them where needed",
    )
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print what it would do, without changing anything (besides fetching)",
    )
    p.set_defaults(func=run)


def describe(step: Autocreated, remote: str, dry_run: bool) -> str:
    pr = f"#{step.number}"
    match step.action:
        case AutocreateAction.CREATE:
            verb = "would create" if dry_run else "created"
            return f"{verb} {step.branch} from {remote}/{step.branch}, tracking it on {step.parent} ({pr})"
        case AutocreateAction.TRACK:
            return f"{'would track' if dry_run else 'tracking'} {step.branch} on {step.parent} ({pr})"
        case AutocreateAction.ALREADY_TRACKED:
            return f"{step.branch} is already tracked on {step.parent} ({pr})"
        case AutocreateAction.PARENT_DIFFERS:
            return (
                f"{step.branch} is tracked on {step.tracked_parent}, but {pr} merges it into {step.parent}; "
                f"leaving it (to follow GitHub, run `git stack track {step.parent} {step.branch}`)"
            )
        case AutocreateAction.SKIP:
            return f"skipping {step.branch} ({pr}): {step.reason}"


def run(stack: StackClient, args: Args) -> None:
    result = stack.autocreate(remote=args.remote, dry_run=args.dry_run)
    if not result.steps:
        print("you have no open pull requests from branches in this repository")
    # Without a dry run, the stacks show what was tracked, with the pull
    # requests of the branches it just tracked in green.
    for step in result.steps:
        if args.dry_run or step.action in (AutocreateAction.PARENT_DIFFERS, AutocreateAction.SKIP):
            print(describe(step, args.remote, args.dry_run))
    if result.branches:
        tracked = {s.branch for s in result.steps if s.action in (AutocreateAction.CREATE, AutocreateAction.TRACK)}
        new_links = {b.name: b.pull_request.url for b in result.branches if b.name in tracked and b.pull_request}
        print_tree(stack, result.branches, new_links)
