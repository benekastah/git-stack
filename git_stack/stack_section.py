"""The stack section that `git stack prs` keeps at the end of each pull
request's description: the stack as a tree from its root, with the pull
request's own branch marked.

A node is a pull request's number, or the name of a branch without one (or of
the root). Merged pull requests stay in the tree, since sync stops tracking
their branches, so they're carried over from the section being replaced.
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
    with the merged pull requests put back where old had them.

    A branch goes back under a merged pull request it was stacked on in old,
    unless it has since moved elsewhere. A merged pull request whose parent
    isn't in the tree goes on root.
    """

    def unmerged_ancestor(node: Node) -> Node:
        seen = set()
        while node in merged and node not in seen:
            seen.add(node)
            node = old.get(node, root)
        return node if node in current else root

    tree = dict(current)
    for number in merged:
        tree[number] = old.get(number, root)
    for node, parent in current.items():
        old_parent = old.get(node)
        if old_parent in merged and unmerged_ancestor(old_parent) == parent:
            tree[node] = old_parent
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
    rest = SECTION.sub("\n\n", body).strip()
    return f"{rest}\n\n{section}" if rest else section
