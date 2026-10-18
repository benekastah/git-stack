from git_stack.errors import StackError
from git_stack.stack import BASE_KEY, PARENT_KEY, ensure_no_cycle


def register(subparsers):
    p = subparsers.add_parser("track", help="track existing branches as a stack on PARENT, each on the "
                                            "one before it (default: the current branch)")
    p.add_argument("parent")
    p.add_argument("branches", nargs="*", metavar="branch",
                   help="branches to stack, bottom first (default: the current branch)")
    p.add_argument("--base", help="commit the first branch was built on (default: merge-base with "
                                  "PARENT). Needed when PARENT has already been squash-merged or "
                                  "rewritten")
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    branches = args.branches
    if not branches:
        branch = git.current_branch()
        if branch is None:
            raise StackError("not on a branch")
        branches = [branch]
    if git.rev(args.parent) is None:
        raise StackError(f"{args.parent} isn't a commit")
    for branch in branches:
        if not git.local_branch_exists(branch):
            raise StackError(f"{branch} isn't a local branch")
    if len(set(branches)) != len(branches) or args.parent in branches:
        raise StackError("each branch can only appear once")

    # Validate the whole chain before writing anything, so a bad argument
    # doesn't leave the stack half-tracked.
    parents = stack.tracked_branches()
    plan = []
    parent, base_arg = args.parent, args.base
    for branch in branches:
        ensure_no_cycle(branch, parent, parents)
        base = git.rev(base_arg) if base_arg else git.run("merge-base", parent, branch, check=False) or None
        if base is None:
            raise StackError(f"{base_arg} isn't a commit" if base_arg
                             else f"{branch} has no history in common with {parent}")
        if not git.is_ancestor(base, branch):
            raise StackError(f"{base_arg} isn't in {branch}'s history")
        parents[branch] = parent
        plan.append((branch, parent, base))
        parent, base_arg = branch, None

    for branch, parent, base in plan:
        git.config_set(branch, PARENT_KEY, parent)
        git.config_set(branch, BASE_KEY, base)
        print(f"tracking {branch} on top of {parent} (base {base[:12]})")
