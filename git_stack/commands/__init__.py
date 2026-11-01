"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets func to
its run(stack, args).
"""

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, TypeAlias, TypeVar

from git_stack import events
from git_stack.errors import StackError
from git_stack.output import event_json, print_event
from git_stack.stack import StackClient

Result = TypeVar("Result")

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


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one JSON object for scripts, with what happened and the result, and the usual messages on stderr",
    )


def run_for_json(stack: StackClient, command: Callable[[], Result], result_json: Callable[[Result], Any]) -> Result:
    """Run command for --json: print {"events": [...], "result": ..., "error":
    ...} with each event it reports (see event_json), result_json of what it
    returns (null if it fails), and the error it fails with (null if it
    doesn't). stdout is kept for that, so its messages, and git's output, go
    to stderr. Failing, it still prints what happened before raising."""
    reported: list[events.Event] = []

    def on_event(event: events.Event) -> None:
        reported.append(event)
        print_event(event, out=sys.stderr)

    stack.on_event = on_event
    stack.git.passthrough_stdout = sys.stderr

    def output(result: Any, error: str | None) -> None:
        print(json.dumps({"events": [event_json(e) for e in reported], "result": result, "error": error}, indent=2))

    try:
        returned = command()
    except StackError as e:
        output(None, str(e))
        raise
    output(result_json(returned), None)
    return returned
