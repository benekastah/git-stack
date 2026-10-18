from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.errors import StackError
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import stack_of


def register(subparsers):
    p = subparsers.add_parser("push", help="force-push (with lease) every branch in the current stack, "
                                           "refusing if that would drop commits on the remote")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    stack.ensure_no_operation_in_progress()
    stack.settle_all()
    parents = stack.tracked_branches()
    branch = stack.current_stacked_branch(parents, "push")
    adapter = make_adapter(stack, args.remote)
    branches = stack.skip_merged(stack_of(branch, parents), parents, adapter)
    if not branches:
        print(f"nothing to push: every branch in {branch}'s stack looks merged")
        return
    # A remote branch deleted since we pushed it was most likely deleted when
    # its pull request was merged, so pushing it again would recreate it.
    for b in branches:
        if git.upstream_gone(b):
            raise StackError(f"{b}'s remote branch was deleted; run `git stack sync` to stop tracking "
                             f"it if it was merged, or `git branch --unset-upstream {b}` to push it "
                             f"again")

    # Force-pushing is only safe if we've seen everything on the remote branch.
    # Refuse if we know of commits we haven't pulled, and pin each lease to the
    # remote-tracking ref we checked, so a push that raced with someone else's
    # fails instead of overwriting it.
    leases = []
    for b in branches:
        remote_tip = git.rev(f"refs/remotes/{args.remote}/{b}")
        if remote_tip and stack.unpulled_commits(b, parents[b], remote_tip):
            raise StackError(f"{args.remote}/{b} has commits that {b} doesn't; "
                             f"run `git stack sync` to pull them in first")
        leases.append(f"--force-with-lease={b}:{remote_tip or ''}")
    if not git.passthrough("push", *leases, "--set-upstream", args.remote, *branches):
        raise StackError("push failed; if the remote has new commits, run `git stack sync` and try again")
    try:
        adapter.after_push(branch, branches, parents)
    except StackError as e:
        raise StackError(f"pushed, but updating pull requests failed (set {ADAPTER_SETTING} to git "
                         f"to stop using GitHub): {e}")
