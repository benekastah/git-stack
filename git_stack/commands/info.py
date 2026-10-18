from git_stack.adapters import make_adapter
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import BASE_KEY, MergeState, children_map, stack_root


def register(subparsers):
    p = subparsers.add_parser("info", help="show all tracked stacks")
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    # Pick up the result of a rebase the user finished by hand, but leave the
    # pending state alone while it's still running.
    if not git.operation_in_progress():
        stack.settle_all()
    parents = stack.tracked_branches()
    if not parents:
        print("no tracked branches")
        return
    current = git.current_branch()
    children = children_map(parents)
    roots = sorted({stack_root(b, parents) for b in parents})
    adapter = make_adapter(stack, DEFAULT_REMOTE)

    def label(branch):
        notes = []
        if branch == current:
            notes.append("*")
        if branch in parents:
            parent = parents[branch]
            parent_tip = git.rev(parent)
            state = parent_tip and adapter.merge_state(branch, parent)
            if parent_tip is None:
                notes.append(f"(parent {parent} missing)")
            elif state is not MergeState.NOT_MERGED:
                notes.append(f"({state.value})")
            elif git.config_get(branch, BASE_KEY) != parent_tip:
                notes.append("(needs restack)")
        return " ".join([branch, *notes])

    def show(branch, prefix):
        kids = children.get(branch, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            print(f"{prefix}{'└── ' if last else '├── '}{label(kid)}")
            show(kid, prefix + ("    " if last else "│   "))

    for root in roots:
        print(label(root))
        show(root, "")
