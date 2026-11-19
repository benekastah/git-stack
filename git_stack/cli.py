import argparse
import pathlib
import sys
from contextlib import nullcontext
from typing import Any

import git_stack
from git_stack.commands import (
    base,
    branch,
    clean,
    group,
    import_stacks,
    info,
    install_skills,
    integrate,
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
from git_stack.performance import Timeline, append_performance_log, performance_log_limit
from git_stack.stack import StackClient

# In the order `git stack -h` lists them.
COMMANDS = (
    branch,
    integrate,
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


def register_global_flags(parser: argparse.ArgumentParser) -> None:
    """Add the flags shared by the main parser and every subparser."""
    # An absent flag must preserve the value set by an ancestor parser.
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print JSON for scripts: a list for info, otherwise one object per line",
    )


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        register_global_flags(self)


def build_parser() -> argparse.ArgumentParser:
    """The `git stack` command line's parser, with every command registered."""
    parser = _ArgumentParser(prog="git stack", description=(git_stack.__doc__ or "").partition("\n")[0])
    parser.set_defaults(json=False)
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
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    # A global flag and a command's flag may have been read by different parsers.
    if args.json and args.command == "show-stack" and args.quiet:
        parser.error("argument -q/--quiet: not allowed with argument --json")
    return _run(args, stack, argv)


def _run(args: argparse.Namespace, stack: StackClient | None, argv: list[str]) -> int:
    # With --json, stdout is for the JSON, so git's output goes to stderr.
    stream = JsonStream() if args.json and not getattr(args, "json_list", False) else None
    timeline: Timeline | None = None
    max_bytes: int | None = None
    try:
        if stack is None:
            # Joining starts over at an absolute path, as git's -C does. It
            # fails if there's no git to run.
            stack = StackClient(GitClient(pathlib.Path(".", *args.directories)))
        max_bytes = performance_log_limit(stack.git)
        timeline = Timeline() if max_bytes is not None else None
        with timeline.activate() if timeline is not None else nullcontext():
            return _run_command(args, stack, stream)
    except StackError as e:
        if stream:
            stream.error(str(e))
        print(f"git stack: {e}", file=sys.stderr)
        return 1
    finally:
        if timeline is not None and max_bytes is not None:
            try:
                append_performance_log(argv, timeline.render(), max_bytes=max_bytes)
            except (OSError, RuntimeError) as error:
                print(f"git stack: couldn't write the performance log: {error}", file=sys.stderr)


def _run_command(args: argparse.Namespace, stack: StackClient, stream: JsonStream | None) -> int:
    stack.on_event = stream.event if stream else print_event
    if stream:
        stack.git.passthrough_stdout = sys.stderr
    result = args.run(stack, args)
    # A command can fail after doing part of its work (e.g. push --prs,
    # which pushed, but couldn't open pull requests): then its error(result)
    # says why, after its result is shown.
    error: str | None = args.error(result) if getattr(args, "error", None) else None
    status: int | None = None
    if stream:
        stream.result(result)
    else:
        # A command's show prints its result, and returns its exit status,
        # or None for 0. It can fail too, e.g. on a bad setting for how to
        # show it.
        status = args.show(stack, args, result)
    if error is not None:
        print(f"git stack: {error}", file=sys.stderr)
        return 1
    return 0 if stream else status or 0
