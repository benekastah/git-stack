import argparse

from git_stack.commands import (
    Subparsers,
    add_delete_argument,
    add_ignore_revert_argument,
    add_json_argument,
    add_latest_argument,
    add_stack_arguments,
    run_for_json,
)
from git_stack.commands.info import print_tree, stacks_json
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient, SyncResult


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str
    delete: str | None
    latest: bool
    no_push: bool
    ignore_reverts: list[str]
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "sync",
        help="fetch, untrack the current stack's (or the named ones') merged branches, restack "
        "it, pull in commits others pushed, and push the branches that already have a remote branch",
    )
    add_stack_arguments(p, "sync")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    add_latest_argument(p)
    add_ignore_revert_argument(p)
    p.add_argument(
        "--no-push", action="store_true", help="don't push, only list the branches whose remote branch is out of date"
    )
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def sync() -> SyncResult:
        return stack.sync(
            args.branches,
            args.groups,
            remote=args.remote,
            delete=args.delete,
            latest=args.latest,
            push=not args.no_push,
            ignore_reverts=args.ignore_reverts,
        )

    if args.json:
        run_for_json(stack, sync, lambda result: {"stacks": stacks_json(result.branches), "unpushed": result.unpushed})
        return
    result = sync()
    if result.branches:
        print_tree(stack, result.branches)
    if result.unpushed:
        print("note: run `git stack push` to update the remote branches and pull requests of:")
        for branch in result.unpushed:
            print(f"  {branch}")
