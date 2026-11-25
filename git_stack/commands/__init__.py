"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets run and
show: run(stack, args) does the command and returns its result, a dataclass,
and show(stack, args, result) prints the result as text, returning the exit
status (or None for 0). With --json, the CLI prints the result as JSON (see
git_stack.json_fields) instead of calling show, so the result has to hold
everything the command reports.
"""

import argparse
from typing import TypedDict

from git_stack.adapters.github import PRProtection
from git_stack.core import CURRENT_GROUP, Delete
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


class PRProtectionArgs(argparse.Namespace):
    force: bool


def add_pr_access_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prs", action="store_true", help="enable pull request integration for this command")


def add_pr_protection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="allow updates to protected pull requests")


class PRProtectionOptions(TypedDict, total=False):
    protection: PRProtection


def pr_protection(args: PRProtectionArgs) -> PRProtectionOptions:
    return {"protection": PRProtection(force=True)} if args.force else {}


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
