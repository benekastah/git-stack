import argparse

from git_stack.commands import (
    Subparsers,
    add_allow_shared_argument,
    add_delete_argument,
    add_ignore_revert_argument,
    add_json_argument,
    add_latest_argument,
    add_rebuild_stack_argument,
    add_stack_arguments,
)
from git_stack.commands.info import print_tree
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient, SyncResult


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str | None
    delete: str | None
    latest: bool
    ignore_reverts: list[str]
    json: bool
    allow_shared: bool
    pull_tracked_upstream: bool
    rewritten_upstream: bool
    force: bool
    rebuild_stack: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "sync",
        help="fetch, untrack the current stack's (or the named ones') merged branches, restack "
        "it, pull in commits others pushed, and push the branches that already have a remote branch",
    )
    add_stack_arguments(p, "sync")
    p.add_argument("--remote", help="only sync branches tracking this remote")
    p.add_argument("--force", action="store_true", help="allow syncing a branch to the remote default branch")
    add_delete_argument(p)
    add_latest_argument(p)
    add_ignore_revert_argument(p)
    add_allow_shared_argument(p)
    add_rebuild_stack_argument(p)
    p.add_argument(
        "--pull-tracked-upstream",
        action="store_true",
        help="pull unseen upstream commits that existed before a branch was re-tracked with --base",
    )
    p.add_argument(
        "--rewritten-upstream", action="store_true", help="pull unseen commits from rewritten upstream history"
    )
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> SyncResult:
    return stack.sync(
        args.branches,
        args.groups,
        remote=args.remote or DEFAULT_REMOTE,
        only_remote=args.remote is not None,
        delete=args.delete,
        latest=args.latest,
        push=True,
        ignore_reverts=args.ignore_reverts,
        allow_shared=args.allow_shared,
        pull_tracked_upstream=args.pull_tracked_upstream,
        rewritten_upstream=args.rewritten_upstream,
        force=args.force,
        rebuild_stack=args.rebuild_stack,
    )


def show(stack: StackClient, args: Args, result: SyncResult) -> None:
    if result.branches:
        print_tree(stack, result.branches)
    if result.unpushed:
        print("note: run `git stack push` to update the remote branches and pull requests of:")
        for branch in result.unpushed:
            print(f"  {branch}")
