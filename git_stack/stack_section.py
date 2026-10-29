"""The stack section that `git stack prs` keeps at the end of each pull
request's description: the stack as a tree from its root, with the pull
request's own branch marked.

A node is a pull request's number, or the name of a branch without one (or of
the root). Merged pull requests stay in the tree, as a history, since sync
stops tracking their branches, so they're carried over from the section being
replaced.
"""

import re
from typing import TypeAlias

Node: TypeAlias = int | str
Tree: TypeAlias = dict[Node, Node]  # {node: its parent}

START = "<!-- git-stack -->"
END = "<!-- /git-stack -->"
NOTE = "<!-- Kept up to date by `git stack prs`, which overwrites any edits to this section. -->"
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.DOTALL)
LINE = re.compile(r"^( *)- (?:#(\d+)|`([^`]+)`)")
ACTIVE = " 👈"
MERGED = " (merged)"


def find(body: str) -> Tree:
    """The tree in body's stack section, without its root: {} if it has none."""
    section = SECTION.search(body)
    tree: Tree = {}
    # (indent, node) of the last node seen at each depth above the current line.
    ancestors: list[tuple[int, Node]] = []
    for line in section.group().splitlines() if section else ():
        match = LINE.match(line)
        if not match:
            continue
        indent, number, name = match.groups()
        node: Node = int(number) if number else name
        while ancestors and ancestors[-1][0] >= len(indent):
            ancestors.pop()
        if ancestors:
            tree[node] = ancestors[-1][1]
        ancestors.append((len(indent), node))
    return tree


def lay_out(root: str, current: Tree, old: Tree, merged: set[int]) -> Tree:
    """The tree to show: current (the stack's branches, without merged ones),
    with the merged pull requests put back on the parents old had them on.

    Branches stay on their current parents, not under a merged pull request
    they were stacked on, so the tree shows what each pull request is built on
    now. A merged pull request whose parent isn't in the tree goes on root.
    """
    tree = dict(current)
    for number in merged:
        tree[number] = old.get(number, root)
    return {node: parent if parent in tree else root for node, parent in tree.items()}


def render(root: str, tree: Tree, merged: set[int], active: Node) -> str:
    """The stack section for active's pull request."""
    children: dict[Node, list[Node]] = {}
    # Pull requests in the order they were opened, then branches without one.
    for node in sorted(tree, key=lambda n: (0, n, "") if isinstance(n, int) else (1, 0, n)):
        children.setdefault(tree[node], []).append(node)
    lines = [START, NOTE, "---", "**Stack**", f"- `{root}`"]

    def add(parent: Node, depth: int) -> None:
        for node in children.get(parent, []):
            label = f"#{node}" if isinstance(node, int) else f"`{node}`"
            notes = (MERGED if node in merged else "") + (ACTIVE if node == active else "")
            lines.append(f"{'  ' * depth}- {label}{notes}")
            add(node, depth + 1)

    add(root, 1)
    lines.append(END)
    return "\n".join(lines)


def replace(body: str, section: str) -> str:
    """body with section at its end, in place of any stack section it had."""
    rest = remove(body)
    return f"{rest}\n\n{section}" if rest else section


def remove(body: str) -> str:
    """body without its stack section, if it had one."""
    return SECTION.sub("\n\n", body).strip() if SECTION.search(body) else body
