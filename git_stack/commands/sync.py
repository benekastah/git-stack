from git_stack.commands import (
    PRProtectionArgs,
    Subparsers,
    add_allow_shared_argument,
    add_delete_argument,
    add_ignore_revert_argument,
    add_latest_argument,
    add_pr_access_arguments,
    add_pr_protection_arguments,
    add_rebuild_stack_argument,
    add_stack_arguments,
    pr_protection,
)
from git_stack.commands.info import print_tree
from git_stack.core import Delete
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient, SyncResult


class Args(PRProtectionArgs):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str | None
    delete: str | None
    latest: bool
    ignore_reverts: list[str]
    allow_shared: bool
    pull_tracked_upstream: bool
    rewritten_upstream: bool
    rebuild_stack: bool
    no_verify: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "sync",
        help="fetch, untrack the current stack's (or the named ones') merged branches, restack "
        "it, pull in commits others pushed, and push the branches that already have a remote branch",
    )
    add_stack_arguments(p, "sync", optional_group=True)
    p.add_argument("--remote", help="only sync branches tracking this remote")
    p.add_argument("--no-verify", action="store_true", help="skip the pre-push hook")
    p.add_argument(
        "--force",
        action="store_true",
        help="allow syncing to the remote default branch or updating queued pull requests",
    )
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
    add_pr_protection_arguments(p)
    add_pr_access_arguments(p, writes=True)
    p.set_defaults(run=run, show=show)


def run(stack: StackClient, args: Args) -> SyncResult:
    return stack.sync(
        args.branches,
        args.groups,
        remote=args.remote or DEFAULT_REMOTE,
        only_remote=args.remote is not None,
        delete=Delete(args.delete) if args.delete else None,
        latest=args.latest,
        push=True,
        ignore_reverts=args.ignore_reverts,
        allow_shared=args.allow_shared,
        pull_tracked_upstream=args.pull_tracked_upstream,
        rewritten_upstream=args.rewritten_upstream,
        force=args.force,
        rebuild_stack=args.rebuild_stack,
        no_verify=args.no_verify,
        close_orphaned_prs=args.rebuild_stack,
        **pr_protection(args),
    )


def show(stack: StackClient, args: Args, result: SyncResult) -> None:
    if result.branches:
        print_tree(stack, result.branches)
    if result.unpushed:
        print("note: run `git stack push --with-prs` to update the remote branches and pull requests of:")
        for branch in result.unpushed:
            print(f"  {branch}")
