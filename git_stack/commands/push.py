from git_stack.commands import (
    PRProtectionArgs,
    Subparsers,
    add_allow_shared_argument,
    add_pr_access_arguments,
    add_pr_protection_arguments,
    add_rebuild_stack_argument,
    add_stack_arguments,
    named_stacks,
    pr_protection,
)
from git_stack.commands.info import print_tree
from git_stack.commands.prs import show_steps
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import PushResult, StackClient


class Args(PRProtectionArgs):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    remote: str | None
    prs: bool
    allow_shared: bool
    rebuild_stack: bool
    no_verify: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "push",
        help="force-push (with lease) every branch in the current stack (or the ones "
        "named), refusing if that would drop commits on the remote",
    )
    add_stack_arguments(p, "push", optional_group=True)
    p.add_argument("--remote", help="only push branches for this remote")
    p.add_argument("--no-verify", action="store_true", help="skip the pre-push hook")
    p.add_argument(
        "--force",
        action="store_true",
        help="push even if that drops commits on a remote branch that the local one doesn't "
        "have, or update queued pull requests. Still checks the lease",
    )
    p.add_argument(
        "--prs",
        "--pr",
        action="store_true",
        help="then open pull requests for them, as `git stack prs` does (implied by --pr-rebuild)",
    )
    add_allow_shared_argument(p)
    add_rebuild_stack_argument(p)
    add_pr_protection_arguments(p)
    add_pr_access_arguments(p, writes=True)
    p.set_defaults(run=run, show=show, error=lambda result: result.error)


def run(stack: StackClient, args: Args) -> PushResult:
    return stack.push(
        args.branches,
        args.groups,
        remote=args.remote or DEFAULT_REMOTE,
        only_remote=args.remote is not None,
        force=args.force,
        prs=args.prs or args.rebuild_stack,
        allow_shared=args.allow_shared,
        rebuild_stack=args.rebuild_stack,
        no_verify=args.no_verify,
        close_orphaned_prs=args.rebuild_stack,
        **pr_protection(args),
    )


def show(stack: StackClient, args: Args, result: PushResult) -> None:
    if result.steps is not None:
        show_steps(stack, result.steps, result.branches, dry_run=False)
    elif result.branches:
        print_tree(stack, result.branches)
    if not result.pushed:
        print(f"nothing to push: every branch in {named_stacks(stack, args.branches, args.groups)} looks merged")
