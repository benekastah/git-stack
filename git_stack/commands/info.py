from git_stack.stack import BranchStatus

NOTES = {
    BranchStatus.UP_TO_DATE: None,
    BranchStatus.NEEDS_RESTACK: "(needs restack)",
    BranchStatus.MERGED: "(merged)",
    BranchStatus.MERGED_WITH_NEW_COMMITS: "(merged, with new commits)",
}


def register(subparsers):
    p = subparsers.add_parser("info", help="show all tracked stacks")
    p.set_defaults(func=run)


def label(branch):
    notes = []
    if branch.current:
        notes.append("*")
    if branch.status is BranchStatus.PARENT_MISSING:
        notes.append(f"(parent {branch.parent} missing)")
    elif branch.status is not None and NOTES[branch.status]:
        notes.append(NOTES[branch.status])
    return " ".join([branch.name, *notes])


def run(stack, args):
    branches = stack.info()
    if not branches:
        print("no tracked branches")
        return
    children = {}
    for branch in branches:
        if branch.parent is not None:
            children.setdefault(branch.parent, []).append(branch)

    def show(branch, prefix):
        kids = children.get(branch.name, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            print(f"{prefix}{'└── ' if last else '├── '}{label(kid)}")
            show(kid, prefix + ("    " if last else "│   "))

    for branch in branches:
        if branch.parent is None:
            print(label(branch))
            show(branch, "")
