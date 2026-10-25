"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets func to
its run(stack, args).
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
        help="treat BRANCH as merged even if its parent has since reverted some of it, and stop "
        "warning that the branch a stacked branch was on until it merged was reverted (can be "
        "given more than once)",
    )


def add_delete_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-d", "--delete", choices=("local", "remote", "both"), help=DELETE_HELP)
