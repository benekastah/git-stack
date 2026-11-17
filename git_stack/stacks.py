"""The tracked branches, as trees, without touching the repository.

StackCore.stacks reads them from git config, and StackCore.save writes back
what changed, so a command can work out what to change in memory, with the
rules for groups and ownership below, then write only that.
"""

import enum
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace

from git_stack.errors import StackError


def dependency_order(branches: Iterable[str], parents: Mapping[str, Sequence[str]]) -> list[str]:
    """Order selected branches after their selected dependencies, refusing cycles."""
    selected = list(dict.fromkeys(branches))
    remaining = set(selected)
    visiting: set[str] = set()
    ordered: list[str] = []

    def visit(branch: str) -> None:
        if branch not in remaining:
            return
        if branch in visiting:
            raise StackError(f"cycle in stack metadata involving {branch}")
        visiting.add(branch)
        for parent in parents.get(branch, ()):
            visit(parent)
        visiting.remove(branch)
        remaining.remove(branch)
        ordered.append(branch)

    for branch in selected:
        visit(branch)
    return ordered


class Ownership(enum.Enum):
    """Whether a stack is the user's own, or shared with others, which
    commands that rewrite or delete its branches only change with
    --allow-shared. Values are what core.OWNERSHIP_KEY holds, and what
    `git stack ownership --json` prints."""

    OWNED = "owned"
    SHARED = "shared"


@dataclass(frozen=True)
class StackNode:
    """A tracked branch: the branch it's stacked on, the group of stacks its
    stack is in, if it's at the bottom of one (see core.GROUP_KEY), and its
    stack's ownership (see core.OWNERSHIP_KEY), if that's been recorded."""

    name: str
    parent: str
    group: str | None = None
    ownership: Ownership | None = None


class Stacks:
    """Every tracked branch, by name, each stacked on another tracked branch,
    or at the bottom of a stack, on its root: an untracked branch (usually
    main). A root's stacks form a tree, since several branches can be stacked
    on one.

    Methods take and return branch names, in the order the branches were
    read in, unless they say otherwise. Changes are only made here, until
    StackCore.save writes them (see changes).
    """

    def __init__(self, nodes: Iterable[StackNode] = ()) -> None:
        self._nodes = {node.name: node for node in nodes}
        # The nodes as they were when read, or last saved.
        self._saved = dict(self._nodes)
        # {branch: the branches stacked on it, sorted}, worked out when needed.
        self._children: dict[str, list[str]] | None = None

    def __contains__(self, branch: object) -> bool:
        return branch in self._nodes

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._nodes))

    def __len__(self) -> int:
        return len(self._nodes)

    def __getitem__(self, branch: str) -> StackNode:
        return self._nodes[branch]

    def parent(self, branch: str) -> str:
        return self._nodes[branch].parent

    def parents(self) -> dict[str, str]:
        """{branch: the branch it's stacked on}"""
        return {name: node.parent for name, node in self._nodes.items()}

    def group(self, branch: str) -> str | None:
        return self._nodes[branch].group

    def is_shared(self, branch: str) -> bool:
        return self._nodes[branch].ownership is Ownership.SHARED

    def groups(self) -> dict[str, str]:
        """{branch: its group}, for the branches that have one."""
        return {name: node.group for name, node in self._nodes.items() if node.group is not None}

    def children(self, branch: str) -> list[str]:
        """The branches stacked on branch, sorted."""
        if self._children is None:
            self._children = {}
            for name, node in sorted(self._nodes.items()):
                self._children.setdefault(node.parent, []).append(name)
        return list(self._children.get(branch, ()))

    def descendants(self, branch: str) -> list[str]:
        """The branches stacked on branch, on those, and so on,
        parent-before-child (branch excluded)."""
        order: list[str] = []
        for child in self.children(branch):
            order.append(child)
            order.extend(self.descendants(child))
        return order

    def ancestry(self, branch: str) -> list[str]:
        """[branch, its parent, ..., its stack's root]"""
        chain = [branch]
        while chain[-1] in self._nodes:
            parent = self._nodes[chain[-1]].parent
            if parent in chain:
                raise StackError(f"cycle in stack metadata involving {parent}")
            chain.append(parent)
        return chain

    def root(self, branch: str) -> str:
        """The root of branch's stack (usually main)."""
        return self.ancestry(branch)[-1]

    def bottom(self, branch: str) -> str:
        """The branch at the bottom of tracked branch's stack, on its root."""
        return self.ancestry(branch)[-2]

    def is_bottom(self, branch: str) -> bool:
        """Whether tracked branch is at the bottom of its stack."""
        return self._nodes[branch].parent not in self._nodes

    def roots(self) -> list[str]:
        """The roots of every stack, sorted."""
        return sorted({self.root(branch) for branch in self._nodes})

    def in_order(self) -> list[str]:
        """Every tracked branch, by root, parent-before-child."""
        return [branch for root in self.roots() for branch in self.descendants(root)]

    def bottoms_in(self, group: str) -> list[str]:
        """The branches at the bottom of a stack in group."""
        return [name for name, node in self._nodes.items() if node.group == group and self.is_bottom(name)]

    def group_bottoms(self, bottom: str) -> list[str]:
        """bottom, a branch at the bottom of a stack, followed by the others
        in its group, if it has one, sorted."""
        group = self._nodes[bottom].group
        if group is None:
            return [bottom]
        return [bottom, *sorted(b for b in self.bottoms_in(group) if b != bottom)]

    def stack_of(self, branch: str, groups: bool = True) -> list[str]:
        """The branches in tracked branch's stack, parent-before-child: its
        bottom branch and everything stacked on that. With groups, also the
        stacks of the others in the bottom branch's group (see group_bottoms),
        which may be on other roots."""
        bottom = self.bottom(branch)
        bottoms = self.group_bottoms(bottom) if groups else [bottom]
        return [b for each in bottoms for b in (each, *self.descendants(each))]

    def copy(self) -> Stacks:
        """A copy to change without changing this one, e.g. to check a plan."""
        return Stacks(self._nodes.values())

    def restricted(self, branches: Iterable[str]) -> Stacks:
        """A copy with only those of branches that are tracked, in which a
        branch whose parent is left out is at the bottom of a stack."""
        return Stacks(self._nodes[b] for b in dict.fromkeys(branches) if b in self._nodes)

    def ensure_no_cycle(self, branch: str, new_parent: str) -> None:
        """Refuse to stack branch on new_parent if new_parent is on branch."""
        if branch in self.ancestry(new_parent):
            raise StackError(f"{new_parent} is stacked on {branch}; that would create a cycle")

    def ensure_group_available_for_merge(self, branch: str) -> None:
        """Refuse to remove merged branch (see remove_merged) if that would
        put its children in a group named after it that already exists, which
        would join them to unrelated stacks."""
        if (
            self.is_bottom(branch)
            and self.children(branch)
            and self._nodes[branch].group is None
            and branch in self.groups().values()
        ):
            raise StackError(
                f"can't group {branch}'s children: group {branch} already exists; "
                "rename the existing group before continuing"
            )

    def _set(self, node: StackNode) -> None:
        old = self._nodes.get(node.name)
        if old is None or old.parent != node.parent:
            self._children = None
        self._nodes[node.name] = node

    def _remove(self, branch: str) -> None:
        del self._nodes[branch]
        self._children = None

    def reparent(self, branch: str, parent: str) -> None:
        """Stack branch on parent, tracking branch if it isn't.

        If parent is tracked, branch is no longer at the bottom of a stack, so
        it leaves its group, rather than rejoining it if it's ever at the
        bottom again. If parent is a stack's root and branch was higher up a
        stack (e.g. restack --onto main), branch joins that stack's group, so
        the two stay together. If either stack is shared, branch and the
        branches on it are shared from now on.
        """
        old = self._nodes.get(branch)
        node = old or StackNode(branch, parent)
        if old is None or old.parent != parent:
            connected = {m for member in (branch, parent) if member in self for m in self.stack_of(member, False)}
            if node.ownership is Ownership.SHARED or any(self.is_shared(m) for m in connected):
                node = replace(node, ownership=Ownership.SHARED)
                self.share_descendants(branch)
        if parent in self._nodes:
            node = replace(node, group=None)
        elif (
            old is not None
            and old.parent in self._nodes
            and (group := self._nodes[self.bottom(old.parent)].group) is not None
        ):
            node = replace(node, group=group)
        self._set(replace(node, parent=parent))

    def share(self, branch: str) -> None:
        """Mark tracked branch, and the branches stacked on it, shared."""
        self.set_ownership(branch, Ownership.SHARED)
        self.share_descendants(branch)

    def share_descendants(self, branch: str) -> None:
        """Mark the branches stacked on branch, and on those, shared."""
        for member in self.descendants(branch):
            self.set_ownership(member, Ownership.SHARED)

    def set_group(self, branch: str, group: str | None) -> None:
        self._set(replace(self._nodes[branch], group=group))

    def set_ownership(self, branch: str, ownership: Ownership | None) -> None:
        self._set(replace(self._nodes[branch], ownership=ownership))

    def drop(self, branch: str) -> list[str]:
        """Move the branches stacked on branch onto its parent, and remove
        branch, without the rules remove_merged follows. Returns the moved
        branches.

        For working out what's left once branch is treated as gone (e.g.
        merged) without changing anything: saving it would untrack branch.
        """
        new_parent = self._nodes[branch].parent
        children = self.children(branch)
        for child in children:
            self._set(replace(self._nodes[child], parent=new_parent))
        self._remove(branch)
        return children

    def remove_merged(self, branch: str) -> list[str]:
        """Move the branches stacked on merged branch onto its parent (see
        reparent), and, if that's a stack's root, put them in branch's group,
        or a group named after branch, so commands on one of them still cover
        the others, as they did before it merged. Then stop tracking branch.
        Returns the moved branches.

        Refuses, changing nothing, as ensure_group_available_for_merge does.
        """
        self.ensure_group_available_for_merge(branch)
        new_parent = self._nodes[branch].parent
        group = self._nodes[branch].group or branch
        children = self.children(branch)
        for child in children:
            self.reparent(child, new_parent)
            if new_parent not in self._nodes:
                self.set_group(child, group)
        self._remove(branch)
        return children

    def untrack(self, branches: Iterable[str]) -> None:
        """Stop tracking branches. The branches stacked on them stay on them,
        at the bottom of their stacks now, and are shared from now on if their
        stack was."""
        branches = list(branches)
        for branch in branches:
            if any(self.is_shared(b) for b in self.stack_of(branch, False)):
                self.share_descendants(branch)
        for branch in dict.fromkeys(branches):
            self._remove(branch)

    def changes(self) -> list[tuple[str, StackNode | None, StackNode | None]]:
        """(branch, its node as read or last saved, its node now) for each
        branch whose node changed since, None for a branch that wasn't, or
        isn't, tracked."""
        return [
            (name, self._saved.get(name), self._nodes.get(name))
            for name in dict.fromkeys([*self._saved, *self._nodes])
            if self._saved.get(name) != self._nodes.get(name)
        ]

    def mark_saved(self) -> None:
        """Note that the changes so far were saved."""
        self._saved = dict(self._nodes)
