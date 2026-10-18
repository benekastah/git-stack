from git_stack.errors import StackError
from git_stack.stack import PARENT_KEY, PR_KEY


def register(subparsers):
    p = subparsers.add_parser("rename", help="rename a branch (default: the current one), "
                                             "keeping branches stacked on it")
    p.add_argument("old", nargs="?", help="branch to rename (default: the current branch)")
    p.add_argument("name", metavar="new", help="new name")
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    stack.ensure_no_operation_in_progress()
    stack.settle_all()
    old = args.old or git.current_branch()
    if old is None:
        raise StackError("not on a branch; name the branch to rename")
    if not git.local_branch_exists(old):
        raise StackError(f"{old} isn't a local branch")
    upstream = git.run("rev-parse", "--abbrev-ref", f"{old}@{{upstream}}", check=False)
    children = [child for child, parent in stack.tracked_branches().items() if parent == old]
    git.run("branch", "--move", old, args.name)
    # Its pull request stays on the old remote branch, so the next push opens
    # a new one.
    git.config_unset(args.name, PR_KEY)
    for child in children:
        git.config_set(child, PARENT_KEY, args.name)
    print(f"renamed {old} to {args.name}" +
          (f" and moved {', '.join(children)} onto it" if children else ""))
    if upstream:
        # Leaving the old upstream would make sync pull from it, and report it
        # as gone once it's deleted.
        git.run("branch", "--unset-upstream", args.name)
        print(f"note: {upstream} wasn't renamed, and pull requests on it or based on it still point "
              f"there. `git stack push` will push {args.name} as a new remote branch")
