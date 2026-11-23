import argparse
import enum
import json
import os
import shutil
import sys
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from git_stack.adapters.git import PullRequestState
from git_stack.commands import Subparsers, add_stack_arguments
from git_stack.errors import StackError
from git_stack.integrations import Integrations
from git_stack.json_fields import to_json
from git_stack.stack import BranchInfo, BranchStatus, StackClient

NOTES: dict[BranchStatus, str | None] = {
    BranchStatus.UP_TO_DATE: None,
    BranchStatus.NEEDS_RESTACK: "(needs restack)",
    BranchStatus.STALE_BASE: "(stale recorded base)",
    BranchStatus.MERGED: "(merged)",
    BranchStatus.MERGED_WITH_NEW_COMMITS: "(merged, with new commits)",
    BranchStatus.REVERTED: "(merged, then reverted)",
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
    BranchStatus.PARENT_MISSING: "31",  # red
    BranchStatus.STALE_BASE: "31",
}

type Paint = Callable[[str, str], str]


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    branches: list[str]
    groups: list[str]
    json: bool
    prs: bool
    all: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "info",
        help="show the current stack, the named stacks, or all stacks from an untracked branch",
        description="Show the current stack or the named stacks. On an ordinary untracked branch, show all stacks.",
    )
    add_stack_arguments(p, "show", optional_group=True)
    p.add_argument("-a", "--all", action="store_true", help="show every tracked stack")
    p.add_argument(
        "--prs",
        action="store_true",
        help="look up pull request links and merge status on GitHub",
    )
    # For compatibility, info prints a JSON list through its show function.
    p.set_defaults(run=run, show=show, json_list=True)


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


def display_width(text: str) -> int:
    """Estimate the terminal cells used by uncolored text."""
    if text.isascii():
        return len(text)
    return sum(
        2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        for char in text
        if unicodedata.category(char) not in {"Mn", "Me", "Cf"}
    )


def link_continuation(tree: str) -> str:
    """Carry the tree guides onto the link line and align it with the branch name."""
    guide = "│   " if tree.endswith(("├── ", "┌── ")) else "    "
    return tree[:-4] + guide if tree else ""


def compact_link(row: BranchInfo | GroupHeader, text: str) -> str:
    """Shorten a PR URL to its number while keeping other annotations in full."""
    if isinstance(row, BranchInfo) and (pr := row.pull_request) is not None and pr.url == text:
        return f"#{pr.number}"
    return text


@dataclass(frozen=True)
class GroupHeader:
    """The line info draws above the stacks in a group, on their root."""

    group: str
    root: str


def label(branch: BranchInfo | GroupHeader, paint: Paint, color: bool, sources_by_root: Mapping[str, str]) -> str:  # noqa: C901
    if isinstance(branch, GroupHeader):
        return paint(f"group {branch.group}", ROOT)
    if branch.parent is None:
        root_color = ("1;33" if branch.current else "33") if color and branch.integration_needs_restack else ROOT
        parts = [paint(branch.name, root_color)]
        if color and branch.integration_needs_restack:
            parts.append(paint("!", STATUS_COLORS[BranchStatus.NEEDS_RESTACK]))
        if branch.integration_sources is not None:
            sources = ", ".join(
                paint(name, STATUS_COLORS[BranchStatus.MERGED]) if landed else name
                for name, landed in branch.integration_sources
            )
            parts.append((f"(of {sources})" if sources else "(integration)") + (" *" if branch.current else ""))
        elif (sources := sources_by_root.get(branch.name)) is not None:
            parts.append(f"(of {sources})" + (" *" if branch.current else ""))
        if branch.integration_needs_restack and not color:
            parts.append("(needs restack)")
    elif branch.status is BranchStatus.NEEDS_RESTACK and color:
        parts = [paint(branch.name + (" *" if branch.current else ""), "1;33" if branch.current else "33"), "!"]
    elif branch.current:
        parts = [paint(f"{branch.name} *", CURRENT)]
    else:
        parts = [branch.name]
    if branch.status is BranchStatus.PARENT_MISSING:
        parts.append(paint(f"(parent {branch.parent} missing)", STATUS_COLORS[branch.status]))
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
        return Info(stack.info(offline=not args.prs))
    return Info(stack.info(args.branches, args.groups, offline=not args.prs, current=True))


def show(stack: StackClient, args: Args, result: Info) -> None:
    if args.json:
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


def print_tree(
    stack: StackClient,
    branches: list[BranchInfo],
    new_links: Mapping[str, str] = {},
) -> None:
    """Print branches, which isn't empty, as a tree for each stack, with each
    branch's pull request linked beside or below its label. new_links is
    {branch: text}, shown in NEW_LINK's color in place of its link, for pull
    requests that were just opened, or would be."""
    color = use_color(stack)
    paint = painter(color)
    flip = orientation(stack) is Orientation.ROOT_AT_BOTTOM
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
    # The link column is sized by the rows with links.
    integrations = Integrations(stack)
    sources_by_root = {
        b.name: ", ".join(source["name"] for source in integration["sources"])
        for b in branches
        if b.parent is None and (integration := integrations.read(b.name)) is not None
    }
    plain = painter(False)
    widths = [len(tree) + display_width(label(row, plain, color, sources_by_root)) for tree, row in rows]
    links = [link_of(row, new_links) for _, row in rows]
    link_widths = [display_width(link[0]) if link else 0 for link in links]
    label_width = max((width for width, link in zip(widths, links, strict=True) if link), default=0)
    link_width = max(link_widths, default=0)
    columns = shutil.get_terminal_size().columns if sys.stdout.isatty() else None
    for (tree, row), width, link, link_cells in zip(rows, widths, links, link_widths, strict=True):
        line = paint(tree, TREE) + label(row, paint, color, sources_by_root)
        if link:
            text, link_color = link
            if columns is None or label_width + 2 + link_width <= columns:
                line += " " * (label_width - width + 2) + paint(" " * (link_width - link_cells) + text, link_color)
            else:
                if len(tree) + link_cells > columns:
                    text = compact_link(row, text)
                line += "\n" + paint(link_continuation(tree), TREE) + paint(text, link_color)
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
