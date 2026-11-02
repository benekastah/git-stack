import argparse

from git_stack.commands import Subparsers, add_json_argument
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import ImportAction, ImportResult, ImportStep, ImportStepWithoutPr, StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    prs: list[str]
    author: str | None
    remote: str
    dry_run: bool
    force: bool
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "import",
        aliases=["autocreate"],
        help="track open pull requests on GitHub (drafts included) as stacks, "
        "creating local branches for them where needed: yours, or another author's",
    )
    p.add_argument(
        "branches",
        nargs="*",
        metavar="branch",
        help="only the stack trees containing these branches, or the pull requests --pr names (default: all)",
    )
    p.add_argument(
        "--pr",
        action="append",
        default=[],
        dest="prs",
        metavar="number|url",
        help="a pull request, by number or URL, whose stack tree to import, whoever's it is (repeatable)",
    )
    p.add_argument("--author", metavar="login", help="the GitHub user whose pull requests to import (default: you)")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print what it would do with -f, without changing anything (besides fetching)",
    )
    mode.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="also import branches of someone else's pull requests, or with their commits",
    )
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


def needs_force(by: str, other_authors: list[str]) -> str:
    """What a dry run says of a branch with other_authors: by them is how
    they're involved (as the pull request's author, or of commits)."""
    return f", {by} {', '.join(other_authors)}, needs -f" if other_authors else ""


def describe(step: ImportStep, remote: str, dry_run: bool) -> str:
    pr = f"#{step.number}{needs_force('by', step.other_authors) if dry_run else ''}"
    match step.action:
        case ImportAction.CREATE:
            verb = "would create" if dry_run else "created"
            return f"{verb} {step.branch} from {remote}/{step.branch}, tracking it on {step.parent} ({pr})"
        case ImportAction.TRACK:
            return f"{'would track' if dry_run else 'tracking'} {step.branch} on {step.parent} ({pr})"
        case ImportAction.ALREADY_TRACKED:
            return f"{step.branch} is already tracked on {step.parent} ({pr})"
        case ImportAction.PARENT_DIFFERS:
            return (
                f"{step.branch} is tracked on {step.tracked_parent}, but {pr} merges it into {step.parent}; "
                f"leaving it (to follow GitHub, run `git stack track {step.parent} {step.branch}`)"
            )
        case ImportAction.SKIP:
            return f"skipping {step.branch} ({pr}): {step.reason}"


def describe_without_pr(step: ImportStepWithoutPr, remote: str) -> str:
    """What a dry run would do for a branch without a pull request that it
    creates. (One it skips is in the reason the pull requests built on it
    were skipped.)"""
    return (
        f"would create {step.branch} from {remote}/{step.branch}, tracking it on {step.parent} "
        f"(no pull request{needs_force('commits by', step.other_authors)})"
    )


def run(stack: StackClient, args: Args) -> ImportResult:
    return stack.import_stacks(
        remote=args.remote,
        dry_run=args.dry_run,
        force=args.force,
        author=args.author,
        branches=args.branches,
        prs=args.prs,
    )


def show(stack: StackClient, args: Args, result: ImportResult) -> None:
    if not result.steps:
        whose = f"{args.author} has" if args.author else "you have"
        print(f"{whose} no open pull requests from branches in this repository")
    # Without a dry run, the stacks show what was tracked, with the pull
    # requests of the branches it just tracked in green.
    if args.dry_run:
        for without_pr in result.without_prs:
            if without_pr.action is ImportAction.CREATE:
                print(describe_without_pr(without_pr, args.remote))
    for step in result.steps:
        if args.dry_run or step.action in (ImportAction.PARENT_DIFFERS, ImportAction.SKIP):
            print(describe(step, args.remote, args.dry_run))
    if result.branches:
        tracked = {s.branch for s in result.steps if s.action in (ImportAction.CREATE, ImportAction.TRACK)}
        new_links = {b.name: b.pull_request.url for b in result.branches if b.name in tracked and b.pull_request}
        print_tree(stack, result.branches, new_links)
