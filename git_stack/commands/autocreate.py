import argparse

from git_stack.commands import Subparsers, add_json_argument
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import AutocreateAction, Autocreated, AutocreatedWithoutPr, AutocreateResult, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    remote: str
    dry_run: bool
    json: bool


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
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


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


def describe_without_pr(step: AutocreatedWithoutPr, remote: str) -> str:
    """What a dry run would do for a branch without a pull request that it
    creates. (One it skips is in the reason the pull requests built on it
    were skipped.)"""
    return f"would create {step.branch} from {remote}/{step.branch}, tracking it on {step.parent} (no pull request)"


def run(stack: StackClient, args: Args) -> AutocreateResult:
    return stack.autocreate(remote=args.remote, dry_run=args.dry_run)


def show(stack: StackClient, args: Args, result: AutocreateResult) -> None:
    if not result.steps:
        print("you have no open pull requests from branches in this repository")
    # Without a dry run, the stacks show what was tracked, with the pull
    # requests of the branches it just tracked in green.
    if args.dry_run:
        for without_pr in result.without_prs:
            if without_pr.action is AutocreateAction.CREATE:
                print(describe_without_pr(without_pr, args.remote))
    for step in result.steps:
        if args.dry_run or step.action in (AutocreateAction.PARENT_DIFFERS, AutocreateAction.SKIP):
            print(describe(step, args.remote, args.dry_run))
    if result.branches:
        tracked = {s.branch for s in result.steps if s.action in (AutocreateAction.CREATE, AutocreateAction.TRACK)}
        new_links = {b.name: b.pull_request.url for b in result.branches if b.name in tracked and b.pull_request}
        print_tree(stack, result.branches, new_links)
