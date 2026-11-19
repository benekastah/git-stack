"""The stack section that `git stack prs` keeps at the end of each pull
request's description: the stacks as trees from their roots, with the pull
request's own branch marked.

A node is a pull request's number, or the name of a branch without one (or of
the root). Merged pull requests are marked.
"""

import re
from collections.abc import Mapping

type Node = int | str
type Tree = dict[Node, Node]  # {node: its parent}

START = "<!-- git-stack -->"
END = "<!-- /git-stack -->"
NOTE = "<!-- Kept up to date by `git stack prs`, which overwrites any edits to this section. -->"
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.DOTALL)
ACTIVE = " 👈"
MERGED = " (merged)"


def render(trees: Mapping[str, Tree], merged: set[int], active: Node) -> str:
    """The stack section for active's pull request."""
    lines = [START, NOTE, "---", "**Stack**"]

    def add(children: dict[Node, list[Node]], parent: Node, depth: int) -> None:
        for node in children.get(parent, []):
            label = f"#{node}" if isinstance(node, int) else f"`{node}`"
            notes = (MERGED if node in merged else "") + (ACTIVE if node == active else "")
            lines.append(f"{'  ' * depth}- {label}{notes}")
            add(children, node, depth + 1)

    for root, tree in trees.items():
        children: dict[Node, list[Node]] = {}
        # Pull requests in the order they were opened, then branches without one.
        for node in sorted(tree, key=lambda n: (0, n, "") if isinstance(n, int) else (1, 0, n)):
            children.setdefault(tree[node], []).append(node)
        lines.append(f"- `{root}`")
        add(children, root, 1)
    lines.append(END)
    return "\n".join(lines)


def replace(body: str, section: str) -> str:
    """body with section at its end, in place of any stack section it had."""
    rest = remove(body)
    return f"{rest}\n\n{section}" if rest else section


def remove(body: str) -> str:
    """body without its stack section, if it had one."""
    return SECTION.sub("\n\n", body).strip() if SECTION.search(body) else body
