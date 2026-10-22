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


def add_delete_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-d", "--delete", choices=("local", "remote", "both"), help=DELETE_HELP)
