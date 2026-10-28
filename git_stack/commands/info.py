import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from git_stack.adapters.git import PullRequestState
from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.errors import StackError
from git_stack.stack import BranchInfo, BranchStatus, StackClient

NOTES: dict[BranchStatus, str | None] = {
    BranchStatus.UP_TO_DATE: None,
    BranchStatus.NEEDS_RESTACK: "(needs restack)",
    BranchStatus.MERGED: "(merged)",
    BranchStatus.MERGED_WITH_NEW_COMMITS: "(merged, with new commits)",
    BranchStatus.REVERTED: "(merged, then reverted)",
}
# Which way up info draws each stack. A root on top reads like a file tree;
# a root at the bottom like a stack of commits, as in `git log --graph`.
ORIENTATION_SETTING = "stack.orientation"
ROOT_AT_TOP = "root-at-top"
ROOT_AT_BOTTOM = "root-at-bottom"
# ANSI SGR codes for each part of a line, when color is on.
TREE = "2"  # dim
ROOT = "1"  # bold
CURRENT = "1;32"  # bold green, like `git branch`
# Like GitHub's, except that open pull requests are blue rather than green.
LINK_COLORS = {
    PullRequestState.OPEN: "34",  # blue
    PullRequestState.DRAFT: "90",  # grey
    PullRequestState.MERGED: "35",  # magenta
}
# For a pull request prs just opened, or would open.
NEW_LINK = "32"  # green
STATUS_COLORS = {
    BranchStatus.NEEDS_RESTACK: "33",  # yellow
    BranchStatus.MERGED: "36",  # cyan
    BranchStatus.MERGED_WITH_NEW_COMMITS: "33",
    BranchStatus.REVERTED: "33",
    BranchStatus.PARENT_MISSING: "31",  # red
}

Paint: TypeAlias = Callable[[str, str], str]


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    json: bool
    offline: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("info", help="show all tracked stacks (or the named ones)")
    add_stack_arguments(p, "show", default="every stack")
    p.add_argument(
        "--json",
        action="store_true",
        help="print a JSON list of every branch, each stack's root first, parent before child",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="don't ask GitHub about pull requests: check for merges locally, and show no links",
    )
    p.set_defaults(func=run)


def use_color(stack: StackClient) -> bool:
    """Whether to color info's output: as git's color.stack (or color.ui)
    setting says, by default only on a terminal, and never with NO_COLOR set."""
    if os.environ.get("NO_COLOR"):
        return False
    tty = "true" if sys.stdout.isatty() else "false"
    return stack.git.run("config", "--get-colorbool", "color.stack", tty, check=False) == "true"


def root_at_bottom(stack: StackClient) -> bool:
    """Whether to draw each stack with its root at the bottom, as
    ORIENTATION_SETTING says (default: ROOT_AT_TOP)."""
    setting = stack.git.run("config", "--get", ORIENTATION_SETTING, check=False) or ROOT_AT_TOP
    if setting not in (ROOT_AT_TOP, ROOT_AT_BOTTOM):
        raise StackError(f"{ORIENTATION_SETTING} must be {ROOT_AT_TOP} or {ROOT_AT_BOTTOM}, not {setting}")
    return setting == ROOT_AT_BOTTOM


def painter(color: bool) -> Paint:
    """paint(text, code): text in the ANSI color code, if color is on."""
    return lambda text, code: f"\033[{code}m{text}\033[m" if color and text else text


@dataclass(frozen=True)
class GroupHeader:
    """The line info draws above the stacks in a group, on their root."""

    group: str
    root: str


def label(branch: BranchInfo | GroupHeader, paint: Paint) -> str:
    if isinstance(branch, GroupHeader):
        return paint(f"group {branch.group}", ROOT)
    if branch.parent is None:
        parts = [paint(branch.name, ROOT)]
    elif branch.current:
        parts = [paint(f"{branch.name} *", CURRENT)]
    else:
        parts = [branch.name]
    if branch.status is BranchStatus.PARENT_MISSING:
        parts.append(paint(f"(parent {branch.parent} missing)", STATUS_COLORS[branch.status]))
    elif branch.status is not None:
        note = NOTES[branch.status]
        if note:
            parts.append(paint(note, STATUS_COLORS[branch.status]))
    if branch.reverted_parent:
        parts.append(paint(f"({branch.reverted_parent} was reverted)", STATUS_COLORS[BranchStatus.REVERTED]))
    return " ".join(parts)


def run(stack: StackClient, args: Args) -> None:
    branches = stack.info(args.branches, args.groups, offline=args.offline)
    if args.json:
        print_json(branches)
    elif not branches:
        print("no tracked branches")
    else:
        print_tree(stack, branches)


def print_json(branches: list[BranchInfo]) -> None:
    print(
        json.dumps(
            [
                {
                    "name": b.name,
                    "parent": b.parent,
                    "status": b.status.value if b.status else None,
                    "current": b.current,
                    "reverted_parent": b.reverted_parent,
                    "group": b.group,
                    "pr": {
                        "number": b.pull_request.number,
                        "url": b.pull_request.url,
                        "state": b.pull_request.state.value,
                    }
                    if b.pull_request
                    else None,
                }
                for b in branches
            ],
            indent=2,
        )
    )


def tree_children(branches: list[BranchInfo]) -> dict[str | GroupHeader, list[BranchInfo | GroupHeader]]:
    """What print_tree draws under each branch (by name) and group header."""
    children: dict[str | GroupHeader, list[BranchInfo | GroupHeader]] = {}
    for branch in branches:
        if branch.parent is None:
            continue
        # The stacks in a group go under a header for it, where the first of
        # them would be.
        if branch.group is not None:
            header = GroupHeader(branch.group, branch.parent)
            if header not in children:
                children.setdefault(branch.parent, []).append(header)
            children.setdefault(header, []).append(branch)
        else:
            children.setdefault(branch.parent, []).append(branch)
    return children


def print_tree(stack: StackClient, branches: list[BranchInfo], new_links: Mapping[str, str] = {}) -> None:
    """Print branches, which isn't empty, as a tree for each stack, with each
    branch's pull request linked at the end of its line. new_links is
    {branch: text}, shown in NEW_LINK's color in place of its link, for pull
    requests that were just opened, or would be."""
    paint = painter(use_color(stack))
    flip = root_at_bottom(stack)
    children = tree_children(branches)

    # (the tree drawn before branch, branch), in the order they're printed.
    rows: list[tuple[str, BranchInfo | GroupHeader]] = []

    def add(branch: BranchInfo | GroupHeader, prefix: str, drawn: list[tuple[str, BranchInfo | GroupHeader]]) -> None:
        kids = children.get(branch if isinstance(branch, GroupHeader) else branch.name, [])
        for i, kid in enumerate(kids):
            last = i == len(kids) - 1
            drawn.append((f"{prefix}{('┌── ' if flip else '└── ') if last else '├── '}", kid))
            add(kid, prefix + ("    " if last else "│   "), drawn)

    for branch in branches:
        if branch.parent is None:
            drawn: list[tuple[str, BranchInfo | GroupHeader]] = [("", branch)]
            add(branch, "", drawn)
            # Upside down, the tree reads the same, mirrored.
            rows.extend(reversed(drawn) if flip else drawn)
    # Pull request links go in a right-aligned column after the longest line.
    widths = [len(tree) + len(label(row, painter(False))) for tree, row in rows]
    links = [link_of(row, new_links) for _, row in rows]
    link_width = max(len(link[0]) if link else 0 for link in links)
    for (tree, row), width, link in zip(rows, widths, links, strict=True):
        line = paint(tree, TREE) + label(row, paint)
        if link:
            text, color = link
            line += " " * (max(widths) - width + 2) + paint(text.rjust(link_width), color)
        print(line)


def link_of(row: BranchInfo | GroupHeader, new_links: Mapping[str, str]) -> tuple[str, str] | None:
    """(text, color) at the end of row's line in print_tree, if anything."""
    if isinstance(row, GroupHeader):
        return None
    if row.name in new_links:
        return new_links[row.name], NEW_LINK
    if row.pull_request:
        return row.pull_request.url, LINK_COLORS[row.pull_request.state]
    return None
