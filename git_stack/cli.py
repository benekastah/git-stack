import argparse
import sys

import git_stack
from git_stack.commands import branch, clean, info, push, rename, restack, sync, track
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.output import print_event
from git_stack.stack import StackClient

# In the order `git stack -h` lists them.
COMMANDS = (branch, track, rename, info, restack, sync, clean, push)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="git stack", description=git_stack.__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        command.register(subparsers)

    args = parser.parse_args(argv)
    try:
        args.func(StackClient(GitClient(), on_event=print_event), args)
    except StackError as e:
        print(f"git stack: {e}", file=sys.stderr)
        return 1
    return 0
