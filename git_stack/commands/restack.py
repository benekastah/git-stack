from git_stack.adapters import make_adapter
from git_stack.errors import StackError
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import ensure_no_cycle, stack_of


def register(subparsers):
    p = subparsers.add_parser("restack", help="rebase every branch in the current stack onto its parent")
    p.add_argument("--onto", help="first change the current branch's parent to ONTO")
    p.add_argument("--squash", action="store_true",
                   help="first squash each branch in the stack into a single commit")
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    stack.ensure_no_operation_in_progress()
    stack.ensure_clean_worktree()
    stack.settle_all()
    parents = stack.tracked_branches()
    branch = stack.current_stacked_branch(
        parents, "restack", hint="use `git stack track` first" if args.onto else None)
    if args.onto:
        if git.rev(args.onto) is None:
            raise StackError(f"{args.onto} isn't a commit")
        ensure_no_cycle(branch, args.onto, parents)
        parents[branch] = args.onto
    branches = stack.skip_merged(stack_of(branch, parents), parents, make_adapter(stack, DEFAULT_REMOTE))
    if args.squash:
        original = git.current_branch()
        for b in branches:
            stack.squash_branch(b, parents[b])
        if git.current_branch() != original:
            git.switch(original)
    stack.restack_all(branches, parents)
