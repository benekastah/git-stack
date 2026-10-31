import argparse
import os
import sys

import git_stack
from git_stack.commands import (
    base,
    branch,
    clean,
    group,
    import_stacks,
    info,
    next_branch,
    parent,
    prev_branch,
    prs,
    push,
    rename,
    restack,
    show_stack,
    sync,
    track,
    untrack,
)
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.output import JsonStream, print_event
from git_stack.stack import StackClient

# In the order `git stack -h` lists them.
COMMANDS = (
    branch,
    track,
    import_stacks,
    untrack,
    rename,
    group,
    info,
    next_branch,
    prev_branch,
    restack,
    sync,
    clean,
    push,
    prs,
    parent,
    base,
    show_stack,
)


def main(argv: list[str] | None = None, stack: StackClient | None = None) -> int:
    """Run the `git stack` command in argv (default: sys.argv) with stack (default:
    a StackClient working in the directory -C names, or the current one,
    printing what it reports). Returns the exit status."""
    parser = argparse.ArgumentParser(prog="git stack", description=git_stack.__doc__.splitlines()[0])
    parser.add_argument(
        "-C",
        dest="directories",
        action="append",
        default=[],
        metavar="path",
        help="run as if started in path instead of the current directory. Like "
        "git's -C, each one after the first is relative to the one before",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.register(subparsers)

    args = parser.parse_args(argv)
    if stack is None:
        # os.path.join starts over at an absolute path, as git's -C does.
        stack = StackClient(GitClient(os.path.join(".", *args.directories)))
    # With --json, stdout is for the JSON, so git's output goes to stderr.
    stream = JsonStream() if args.json else None
    stack.on_event = stream.event if stream else print_event
    if stream:
        stack.git.passthrough_stdout = sys.stderr
    try:
        result = args.run(stack, args)
        if stream:
            stream.result(result)
            return 0
        # A command's show prints its result, and returns its exit status, or
        # None for 0. It can fail too, e.g. on a bad setting for how to show it.
        status: int | None = args.show(stack, args, result)
    except StackError as e:
        if stream:
            stream.error(str(e))
        print(f"git stack: {e}", file=sys.stderr)
        return 1
    return status or 0
