import argparse

from git_stack.commands import Subparsers
from git_stack.stack import BranchInfo, BranchStatus, StackClient

NOTES: dict[BranchStatus, str | None] = {
    BranchStatus.UP_TO_DATE: None,
    BranchStatus.NEEDS_RESTACK: "(needs restack)",
    BranchStatus.MERGED: "(merged)",
    BranchStatus.MERGED_WITH_NEW_COMMITS: "(merged, with new commits)",
}


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("info", help="show all tracked stacks")
    p.set_defaults(func=run)


def label(branch: BranchInfo) -> str:
    notes = []
    if branch.current:
        notes.append("*")
    if branch.status is BranchStatus.PARENT_MISSING:
        notes.append(f"(parent {branch.parent} missing)")
    elif branch.status is not None:
        note = NOTES[branch.status]
        if note:
            notes.append(note)
    return " ".join([branch.name, *notes])


def run(stack: StackClient, args: argparse.Namespace) -> None:
    branches = stack.info()
    if not branches:
        print("no tracked branches")
        return
    children: dict[str, list[BranchInfo]] = {}
    for branch in branches:
        if branch.parent is not None:
            children.setdefault(branch.parent, []).append(branch)

    def show(branch: BranchInfo, prefix: str) -> None:
        kids = children.get(branch.name, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            print(f"{prefix}{'└── ' if last else '├── '}{label(kid)}")
            show(kid, prefix + ("    " if last else "│   "))

    for branch in branches:
        if branch.parent is None:
            print(label(branch))
            show(branch, "")
