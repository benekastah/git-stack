import sys

from git_stack.adapters import make_adapter
from git_stack.commands import add_delete_argument
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import MergeState, children_map, descendants, stack_root


def register(subparsers):
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, "
                                            "without restacking")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    stack.ensure_no_operation_in_progress()
    stack.ensure_clean_worktree()
    stack.settle_all()
    # Where to leave the user afterwards: their branch (or the commit they had
    # detached at), or wherever deleting their branch left them.
    original = git.current_branch() or git.rev("HEAD")
    adapter = make_adapter(stack, args.remote)
    git.fetch(args.remote)
    parents = stack.tracked_branches()
    roots = sorted({stack_root(b, parents) for b in parents})
    stack.fast_forward_trunks(roots)

    # Parent-before-child, so a branch whose parent was merged is checked
    # against its new parent.
    order = [b for root in roots for b in descendants(root, children_map(parents))]
    for branch in order:
        parent = parents[branch]
        if git.rev(parent) is None:
            print(f"warning: skipping {branch}: its parent {parent} doesn't exist", file=sys.stderr)
            continue
        state = adapter.merge_state(branch, parent)
        if state is MergeState.MERGED:
            if stack.untrack_merged(branch, parents, args.delete) and branch == original:
                original = git.current_branch() or git.rev("HEAD")
        elif state is MergeState.MERGED_WITH_NEW_COMMITS:
            print(f"warning: leaving {branch} alone: it was merged into {parent}, but it or its "
                  f"remote branch has commits that {parent} doesn't", file=sys.stderr)
    stack.restore_checkout(original)
