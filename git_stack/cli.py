import argparse
import os
import sys

import git_stack
from git_stack.commands import branch, clean, info, push, rename, restack, sync, track, untrack
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.output import print_event
from git_stack.stack import StackClient

# In the order `git stack -h` lists them.
COMMANDS = (branch, track, untrack, rename, info, restack, sync, clean, push)


def main(argv: list[str] | None = None, stack: StackClient | None = None) -> int:
    """Run the `git stack` command in argv (default: sys.argv) with stack (default:
    a StackClient working in the directory -C names, or the current one,
    printing what it reports). Returns the exit status."""
    parser = argparse.ArgumentParser(prog="git stack", description=git_stack.__doc__.splitlines()[0])
    parser.add_argument("-C", dest="directories", action="append", default=[], metavar="path",
                        help="run as if started in path instead of the current directory. Like "
                             "git's -C, each one after the first is relative to the one before")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.register(subparsers)

    args = parser.parse_args(argv)
    try:
        if stack is None:
            # os.path.join starts over at an absolute path, as git's -C does.
            directory = os.path.join(".", *args.directories)
            stack = StackClient(GitClient(directory), on_event=print_event)
        args.func(stack, args)
    except StackError as e:
        print(f"git stack: {e}", file=sys.stderr)
        return 1
    return 0
