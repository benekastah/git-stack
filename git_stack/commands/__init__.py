"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets func to
its run(stack, args).
"""

import argparse
import dataclasses
import enum
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, TypeAlias, TypeVar

from git_stack.errors import StackError
from git_stack.output import event_json
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
        help="print what happens, then the result, for scripts: a JSON object per line",
    )


def steps_json(steps: Sequence[Any]) -> list[dict[str, Any]]:
    """steps, dataclasses whose action is an enum, as --json prints them: their
    fields, with action's value."""
    result = []
    for step in steps:
        action: enum.Enum = step.action
        result.append({**dataclasses.asdict(step), "action": action.value})
    return result


def run_for_json(
    stack: StackClient, command: Callable[[], Result], result_json: Callable[[Result], dict[str, Any]]
) -> Result:
    """Run command for --json, which prints a JSON object per line, as it goes:
    each event it reports (see event_json), then {"type": "result", ...} with
    result_json of what it returns, or if it fails, {"type": "error",
    "message": ...} before raising. stdout is kept for that, so git's output
    goes to stderr."""

    def emit(record: dict[str, Any]) -> None:
        print(json.dumps(record), flush=True)

    stack.on_event = lambda event: emit(event_json(event))
    stack.git.passthrough_stdout = sys.stderr
    try:
        returned = command()
    except StackError as e:
        emit({"type": "error", "message": str(e)})
        raise
    emit({"type": "result", **result_json(returned)})
    return returned
