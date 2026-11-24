"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets run and
show: run(stack, args) does the command and returns its result, a dataclass,
and show(stack, args, result) prints the result as text, returning the exit
status (or None for 0). With --json, the CLI prints the result as JSON (see
git_stack.json_fields) instead of calling show, so the result has to hold
everything the command reports.
"""

import argparse
import re
import sys
from typing import TypedDict

from git_stack.adapters.github import ConfirmOrphanClosures, OrphanedPullRequest, PRProtection
from git_stack.core import CURRENT_GROUP, Delete
from git_stack.errors import StackError
from git_stack.stack import StackClient

# argparse doesn't export the type of add_subparsers()'s result.
type Subparsers = "argparse._SubParsersAction[argparse.ArgumentParser]"  # pyright: ignore[reportPrivateUsage]

DELETE_HELP = "besides untracking merged branches, delete their local branches, their remote branches, or both"


def add_latest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-l",
        "--latest",
        action="store_true",
        help="also move branches stacked directly on the stack's root (e.g. main) onto "
        "its latest commit, even if they don't conflict with it",
    )


def add_allow_shared_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allow-shared", action="store_true", help="allow rewriting a shared stack")


def add_rebuild_stack_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--github-rebuild",
        "--gh-rebuild",
        "--rebuild-stack",
        dest="rebuild_stack",
        action="store_true",
        help="rebuild GitHub PR stacks, asking before closing any eligible blocking drafts",
    )


def rebuild_confirmation(rebuild_stack: bool) -> ConfirmOrphanClosures | None:
    return confirm_orphaned_prs if rebuild_stack else None


def confirm_orphaned_prs(candidates: list[OrphanedPullRequest]) -> bool:
    """Require the listed PR numbers from an interactive terminal."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise StackError("--github-rebuild needs an interactive terminal to confirm closing drafts")
    print("The rebuild is blocked by these draft pull requests. Closing them keeps their branches.", file=sys.stderr)
    for candidate in candidates:
        print(f"- {candidate.description}", file=sys.stderr)
    print(
        "Enter all listed PR numbers to continue, separated by spaces, or press Enter to cancel: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    answer = sys.stdin.readline().split()
    return sorted(value.removeprefix("#") for value in answer) == sorted(str(pr.number) for pr in candidates)


class PRProtectionArgs(argparse.Namespace):
    force: bool
    protect_prs: bool
    allow_pr_update: list[int | str]


def pr_identifier(value: str) -> int | str:
    if re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", value):
        return value.casefold()
    raise argparse.ArgumentTypeError("expected a positive PR number or OWNER/REPO#NUMBER")


def add_github_arguments(parser: argparse.ArgumentParser, *, writes: bool = False) -> None:
    parser.add_argument("--github", "--gh", action="store_true", help="enable GitHub pull request lookups")
    if writes:
        parser.add_argument(
            "--git-only", action="store_true", help="allow GitHub ref changes without checking pull requests"
        )


def add_pr_protection_arguments(parser: argparse.ArgumentParser, *, add_force: bool = False) -> None:
    if add_force:
        parser.add_argument("--force", action="store_true", help="allow updates to queued pull requests")
    parser.add_argument(
        "--protect-prs", action="store_true", help="check GitHub and protect approved, ready, and queued pull requests"
    )
    parser.add_argument(
        "--allow-pr-update",
        metavar="PR",
        type=pr_identifier,
        action="append",
        default=[],
        help="allow updating this pull request with --protect-prs (repeatable; queued requests also need --force)",
    )


class PRProtectionOptions(TypedDict, total=False):
    protection: PRProtection


def pr_protection(args: PRProtectionArgs, *, separate_force: bool = False) -> PRProtectionOptions:
    if args.allow_pr_update and not args.protect_prs:
        raise StackError("--allow-pr-update needs --protect-prs")
    if args.protect_prs or (separate_force and args.force):
        return {"protection": PRProtection(args.force, args.protect_prs, frozenset(args.allow_pr_update))}
    return {}


def add_stack_arguments(
    parser: argparse.ArgumentParser, verb: str, default: str = "the current stack", optional_group: bool = False
) -> None:
    """Add the branches and --group arguments that name stacks, as
    StackCore.named takes them."""
    parser.add_argument(
        "branches",
        nargs="*",
        metavar="branch|glob",
        help=f"{verb} the stacks of these branches, or of the branches these globs match (quote "
        f"them, so the shell doesn't expand them), instead of {default}",
    )
    parser.add_argument(
        "-g",
        "--group",
        dest="groups",
        action="append",
        default=[],
        nargs="?" if optional_group else None,
        const=CURRENT_GROUP if optional_group else None,
        help=f"{verb} the stacks in GROUP too (can be given more than once)"
        + ("; omit GROUP for the current stack's group" if optional_group else ""),
    )


def named_stacks(stack: StackClient, branches: list[str], groups: list[str]) -> str:
    """Name the stacks that add_stack_arguments' arguments chose, for a message."""
    return "those stacks" if branches or groups else f"{stack.git.current_branch()}'s stack"


def add_ignore_revert_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ignore-revert",
        dest="ignore_reverts",
        action="append",
        default=[],
        metavar="BRANCH",
        help="treat BRANCH as merged even if its parent has since reverted some of it (can be given more than once)",
    )


def add_delete_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-d", "--delete", choices=[d.value for d in Delete], help=DELETE_HELP)
