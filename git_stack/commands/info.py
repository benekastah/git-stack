import argparse
import enum
import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from git_stack.adapters.git import PullRequestState
from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.errors import StackError
from git_stack.json_fields import to_json
from git_stack.stack import BranchInfo, BranchStatus, StackClient

NOTES: dict[BranchStatus, str | None] = {
    BranchStatus.UP_TO_DATE: None,
    BranchStatus.NEEDS_RESTACK: "(needs restack)",
    BranchStatus.MERGED: "(merged)",
    BranchStatus.MERGED_WITH_NEW_COMMITS: "(merged, with new commits)",
    BranchStatus.REVERTED: "(merged, then reverted)",
    BranchStatus.UNKNOWN: "(status unavailable)",
    BranchStatus.STALE_BASE: "(stale recorded base)",
}
ORIENTATION_SETTING = "stack.orientation"


class Orientation(enum.Enum):
    """Which way up info draws each stack, as ORIENTATION_SETTING says. A
    root on top reads like a file tree; a root at the bottom like a stack of
    commits, as in `git log --graph`. Values are the setting's."""

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
    BranchStatus.UNKNOWN: "33",
    BranchStatus.STALE_BASE: "33",
    BranchStatus.PARENT_MISSING: "31",  # red
}

type Paint = Callable[[str, str], str]


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    list_json: bool
    offline: bool
    all: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("info", help="show the current stack (or the named ones)")
    add_stack_arguments(p, "show", optional_group=True)
    p.add_argument("-a", "--all", action="store_true", help="show every tracked stack")
    # Older than every other command's --json (see git_stack.commands), so it
    # keeps printing a single list.
    p.add_argument(
        "--json",
        dest="list_json",
        action="store_true",
        help="print a JSON list of selected branches, each stack's root first, parent before child",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="don't ask GitHub about pull requests: check for merges locally, and show no links",
    )
    p.set_defaults(run=run, show=show, json=False)


def use_color(stack: StackClient) -> bool:
    """Whether to color info's output: as git's color.stack (or color.ui)
    setting says, by default only on a terminal, and never with NO_COLOR set."""
    if os.environ.get("NO_COLOR"):
        return False
    tty = "true" if sys.stdout.isatty() else "false"
    return stack.git.run("config", "--get-colorbool", "color.stack", tty, check=False) == "true"


def orientation(stack: StackClient) -> Orientation:
    """Which way up to draw each stack, as ORIENTATION_SETTING says (default:
    root at the top)."""
    setting = stack.git.config_value(ORIENTATION_SETTING)
    if not setting:
        return Orientation.ROOT_AT_TOP
    try:
        return Orientation(setting)
    except ValueError:
        choices = " or ".join(o.value for o in Orientation)
        raise StackError(f"{ORIENTATION_SETTING} must be {choices}, not {setting}") from None


def painter(color: bool) -> Paint:
    """paint(text, code): text in the ANSI color code, if color is on."""
    return lambda text, code: f"\033[{code}m{text}\033[m" if color and text else text


@dataclass(frozen=True)
class GroupHeader:
    """The line info draws above the stacks in a group, on their root."""

    group: str
    root: str


def label(branch: BranchInfo | GroupHeader, paint: Paint, color: bool) -> str:
    if isinstance(branch, GroupHeader):
        return paint(f"group {branch.group}", ROOT)
    if branch.parent is None:
        parts = [paint(branch.name, ROOT)]
    elif branch.status is BranchStatus.NEEDS_RESTACK and color:
        parts = [paint(branch.name + (" *" if branch.current else ""), "1;33" if branch.current else "33"), "!"]
    elif branch.current:
        parts = [paint(f"{branch.name} *", CURRENT)]
    else:
        parts = [branch.name]
    if branch.status is BranchStatus.PARENT_MISSING:
        missing = "(a base is missing)" if branch.additional_parents else f"(parent {branch.parent} missing)"
        parts.append(paint(missing, STATUS_COLORS[branch.status]))
    elif branch.status is not None:
        note = NOTES[branch.status]
        if note and not (color and branch.status is BranchStatus.NEEDS_RESTACK):
            parts.append(paint(note, STATUS_COLORS[branch.status]))
    return " ".join(parts)


@dataclass(frozen=True)
class Info:
    """The stacks, as info() lists them."""

    stacks: list[BranchInfo]


def run(stack: StackClient, args: Args) -> Info:
    if args.all:
        if args.branches or args.groups:
            raise StackError("--all cannot be combined with branches or groups")
        return Info(stack.info(offline=args.offline))
    return Info(stack.info(args.branches, args.groups, offline=args.offline, current=True))


def show(stack: StackClient, args: Args, result: Info) -> None:
    if args.list_json:
        print(json.dumps(to_json(result.stacks), indent=2))
    elif not result.stacks:
        print("no tracked branches")
    else:
        print_tree(stack, result.stacks)


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


type TreeRow = tuple[str, BranchInfo | GroupHeader]


def tree_rows(branches: list[BranchInfo], flip: bool) -> list[TreeRow]:
    """Draw the primary-parent tree, with one row per branch."""
    children = tree_children(branches)
    rows: list[TreeRow] = []
    for root in branches:
        if root.parent is not None:
            continue
        drawn: list[TreeRow] = []
        pending: list[tuple[str, BranchInfo | GroupHeader, str]] = [("", root, "")]
        while pending:
            tree, row, prefix = pending.pop()
            drawn.append((tree, row))
            key = row if isinstance(row, GroupHeader) else row.name
            kids = children.get(key, ())
            for index in reversed(range(len(kids))):
                last = index == len(kids) - 1
                elbow = ("┌── " if flip else "└── ") if last else "├── "
                pending.append((prefix + elbow, kids[index], prefix + ("    " if last else "│   ")))
        rows.extend(reversed(drawn) if flip else drawn)
    return rows


def base_brackets(rows: list[TreeRow]) -> dict[int, str]:
    """Bracket each multi-base branch and its bases, with an arrow at the branch."""
    positions = {row.name: i for i, (_, row) in enumerate(rows) if isinstance(row, BranchInfo)}
    brackets: list[tuple[int, int, int, set[int]]] = []
    for i, (_, row) in enumerate(rows):
        if not isinstance(row, BranchInfo) or not row.additional_parents:
            continue
        bases = {positions[base] for base in (row.parent, *row.additional_parents) if base in positions}
        if bases:
            members = bases | {i}
            brackets.append((min(members), max(members), i, members))

    columns: list[int] = []
    lines: dict[int, list[str]] = {}
    for start, end, combined, members in sorted(brackets):
        column = next((n for n, last in enumerate(columns) if last < start), len(columns))
        if column == len(columns):
            columns.append(end)
        else:
            columns[column] = end
        for i in range(start, end + 1):
            fields = lines.setdefault(i, [])
            fields.extend(["   "] * (column + 1 - len(fields)))
            if i in members:
                end_piece = "┐" if i == start else "┘" if i == end else "┤"
                fields[column] = (" ←" if i == combined else " ─") + end_piece
            else:
                fields[column] = "  │"
    return {i: "".join(fields) for i, fields in lines.items()}


def print_tree(
    stack: StackClient,
    branches: list[BranchInfo],
    new_links: Mapping[str, str] = {},
) -> None:
    """Print branches, which isn't empty, as a tree for each stack, with each
    branch's pull request linked at the end of its line. new_links is
    {branch: text}, shown in NEW_LINK's color in place of its link, for pull
    requests that were just opened, or would be."""
    color = use_color(stack)
    paint = painter(color)
    flip = orientation(stack) is Orientation.ROOT_AT_BOTTOM
    rows = tree_rows(branches, flip)
    brackets = base_brackets(rows)
    plain = painter(False)
    widths = [len(tree) + len(label(row, plain, color)) for tree, row in rows]
    tree_width = max(widths, default=0)
    bracket_width = max(map(len, brackets.values()), default=0)
    links = [link_of(row, new_links) for _, row in rows]
    link_width = max((len(link[0]) for link in links if link), default=0)
    for i, ((tree, row), width, link) in enumerate(zip(rows, widths, links, strict=True)):
        line = paint(tree, TREE) + label(row, paint, color)
        if bracket := brackets.get(i):
            line += " " * (tree_width - width) + paint(bracket, TREE)
            width = tree_width + len(bracket)
        if link:
            text, link_color = link
            line += " " * (tree_width + bracket_width - width + 2) + paint(text.rjust(link_width), link_color)
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
