"""The stack section that `git stack prs` keeps at the end of each pull
request's description: the stack as a tree from its root, with the pull
request's own branch marked.

A node is a pull request's number, or the name of a branch without one (or of
the root).
"""

import re
from typing import TypeAlias

Node: TypeAlias = int | str
Tree: TypeAlias = dict[Node, Node]  # {node: its parent}

START = "<!-- git-stack -->"
END = "<!-- /git-stack -->"
NOTE = "<!-- Kept up to date by `git stack prs`, which overwrites any edits to this section. -->"
SECTION = re.compile(f"{re.escape(START)}.*?{re.escape(END)}", re.DOTALL)
ACTIVE = " 👈"


def render(root: str, tree: Tree, active: Node) -> str:
    """The stack section for active's pull request."""
    children: dict[Node, list[Node]] = {}
    # Pull requests in the order they were opened, then branches without one.
    for node in sorted(tree, key=lambda n: (0, n, "") if isinstance(n, int) else (1, 0, n)):
        children.setdefault(tree[node], []).append(node)
    lines = [START, NOTE, "---", "**Stack**", f"- `{root}`"]

    def add(parent: Node, depth: int) -> None:
        for node in children.get(parent, []):
            label = f"#{node}" if isinstance(node, int) else f"`{node}`"
            lines.append(f"{'  ' * depth}- {label}{ACTIVE if node == active else ''}")
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
