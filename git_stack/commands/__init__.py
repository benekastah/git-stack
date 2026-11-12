"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets run and
show: run(stack, args) does the command and returns its result, a dataclass,
and show(stack, args, result) prints the result as text, returning the exit
status (or None for 0). With --json, the CLI prints the result as JSON (see
git_stack.json_fields) instead of calling show, so the result has to hold
everything the command reports.
"""

import argparse
from typing import TypeAlias

# argparse doesn't export the type of add_subparsers()'s result.
Subparsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"

DELETE_HELP = "besides untracking merged branches, delete their local branches, their remote branches, or both"


def add_latest_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--latest",
        action="store_true",
        help="also move branches stacked directly on the stack's root (e.g. main) onto "
        "its latest commit, even if they don't conflict with it",
    )


def add_allow_shared_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--allow-shared", action="store_true", help="allow rewriting a shared stack")


def add_stack_arguments(parser: argparse.ArgumentParser, verb: str, default: str = "the current stack") -> None:
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
        help=f"{verb} the stacks in GROUP too (can be given more than once)",
    )


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
    parser.add_argument("-d", "--delete", choices=("local", "remote", "both"), help=DELETE_HELP)


# parser can be a mutually exclusive group, whose type argparse doesn't export either.
def add_json_argument(parser: argparse.ArgumentParser | argparse._MutuallyExclusiveGroup) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="print what happens, then the result, for scripts: a JSON object per line",
    )
