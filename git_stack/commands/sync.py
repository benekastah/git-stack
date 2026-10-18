import sys

from git_stack.adapters import make_adapter
from git_stack.commands import add_delete_argument
from git_stack.errors import BrokenStackError
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import MergeState, stack_of, stack_root


def register(subparsers):
    p = subparsers.add_parser("sync", help="fetch, untrack the current stack's merged branches, "
                                           "restack it, and pull in commits others pushed")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    stack.ensure_no_operation_in_progress()
    stack.ensure_clean_worktree()
    stack.settle_all()
    parents = stack.tracked_branches()
    # Where to leave the user afterwards: their branch, or wherever deleting
    # it left them.
    original = stack.current_stacked_branch(parents, "sync")
    adapter = make_adapter(stack, args.remote)
    git.fetch(args.remote)
    stack.fast_forward_trunks([stack_root(original, parents)])

    # Parent-before-child, so by the time we reach a branch its parent is
    # final: merged branches have been removed (and their children reparented
    # in `parents`), and the rest have been restacked and pulled.
    order = stack_of(original, parents)
    skipped = set()
    # Branches that differ from their remote branch once restacked and pulled.
    unpushed = []
    for branch in order:
        parent = parents[branch]
        if parent in skipped:
            print(f"warning: skipping {branch} because its parent {parent} was skipped", file=sys.stderr)
            skipped.add(branch)
            continue
        state = adapter.merge_state(branch, parent)
        if state is MergeState.MERGED:
            if not stack.untrack_merged(branch, parents, args.delete):
                skipped.add(branch)
            elif branch == original:
                original = git.current_branch() or git.rev("HEAD")
            continue
        if state is MergeState.MERGED_WITH_NEW_COMMITS:
            # Restacking would replay the merged commits onto parent.
            print(f"warning: skipping {branch}: it was merged into {parent}, but it or its remote "
                  f"branch has commits that {parent} doesn't", file=sys.stderr)
            skipped.add(branch)
            continue
        if git.upstream_gone(branch):
            print(f"warning: {branch}'s remote branch is gone but its changes aren't in {parent}; "
                  f"leaving it alone", file=sys.stderr)
        try:
            stack.restack_branch(branch, parent)
        except BrokenStackError as e:
            print(f"warning: skipping {branch}: {e}", file=sys.stderr)
            skipped.add(branch)
            continue
        stack.pull_upstream(branch, parent)
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip and upstream_tip != git.rev(branch):
            unpushed.append(branch)

    stack.restore_checkout(original)
    if unpushed:
        print("note: run `git stack push` to update the remote branches and pull requests of:")
        for branch in unpushed:
            print(f"  {branch}")
