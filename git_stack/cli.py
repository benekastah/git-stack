import argparse
import pathlib
import sys

import git_stack
from git_stack.commands import (
    base,
    branch,
    clean,
    group,
    import_stacks,
    info,
    install_skills,
    next_branch,
    ownership,
    parent,
    prev_branch,
    prs,
    pull,
    push,
    rename,
    restack,
    show_stack,
    sync,
    track,
    unrevert,
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
    ownership,
    info,
    next_branch,
    prev_branch,
    restack,
    sync,
    pull,
    clean,
    push,
    prs,
    unrevert,
    parent,
    base,
    show_stack,
    install_skills,
)


def build_parser() -> argparse.ArgumentParser:
    """The `git stack` command line's parser, with every command registered."""
    parser = argparse.ArgumentParser(prog="git stack", description=(git_stack.__doc__ or "").partition("\n")[0])
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
    return parser


def main(argv: list[str] | None = None, stack: StackClient | None = None) -> int:
    """Run the `git stack` command in argv (default: sys.argv) with stack (default:
    a StackClient working in the directory -C names, or the current one,
    printing what it reports). Returns the exit status."""
    args = build_parser().parse_args(argv)
    # With --json, stdout is for the JSON, so git's output goes to stderr.
    stream = JsonStream() if args.json else None
    status: int | None = None
    try:
        if stack is None:
            # Joining starts over at an absolute path, as git's -C does. It
            # fails if there's no git to run.
            stack = StackClient(GitClient(pathlib.Path(".", *args.directories)))
        stack.on_event = stream.event if stream else print_event
        if stream:
            stack.git.passthrough_stdout = sys.stderr
        result = args.run(stack, args)
        # A command can fail after doing part of its work (e.g. push --prs,
        # which pushed, but couldn't open pull requests): then its error(result)
        # says why, after its result is shown.
        error: str | None = args.error(result) if getattr(args, "error", None) else None
        if stream:
            stream.result(result)
        else:
            # A command's show prints its result, and returns its exit status,
            # or None for 0. It can fail too, e.g. on a bad setting for how to
            # show it.
            status = args.show(stack, args, result)
    except StackError as e:
        if stream:
            stream.error(str(e))
        print(f"git stack: {e}", file=sys.stderr)
        return 1
    if error is not None:
        print(f"git stack: {error}", file=sys.stderr)
        return 1
    return 0 if stream else status or 0
