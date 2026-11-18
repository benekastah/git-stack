"""The tracked branches and their dependencies, without touching the repository.

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

    for first in selected:
        pending = [(first, False)]
        while pending:
            branch, visited = pending.pop()
            if branch not in remaining:
                continue
            if visited:
                visiting.remove(branch)
                remaining.remove(branch)
                ordered.append(branch)
            else:
                if branch in visiting:
                    raise StackError(f"cycle in stack metadata involving {branch}")
                visiting.add(branch)
                pending.append((branch, True))
                pending.extend((parent, False) for parent in reversed(parents.get(branch, ())))
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
    stack's ownership (see core.OWNERSHIP_KEY), if that's been recorded.
    additional_parents are its other bases, in replay order."""

    name: str
    parent: str
    group: str | None = None
    ownership: Ownership | None = None
    additional_parents: tuple[str, ...] = ()


class Stacks:
    """Tracked branches and their dependency graph. Untracked dependencies
    are roots (usually main). The primary parent determines the existing
    single-parent interfaces, such as ancestry and group membership.

    Methods take and return branch names, in the order the branches were
    read in, unless they say otherwise. Changes are only made here, until
    StackCore.save writes them (see changes).
    """

    def __init__(self, nodes: Iterable[StackNode] = ()) -> None:
        self._nodes = {node.name: node for node in nodes}
        # The nodes as they were when read, or last saved.
        self._saved = dict(self._nodes)
        # {branch: the branches stacked on it, sorted}, worked out when needed.
        self._children: dict[str, set[str]] | None = None
        self._multiple_base: set[str] | None = None
        self._primary: dict[str, tuple[str, str]] = {}
        self._bottoms_by_group: dict[str, list[str]] | None = None

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

    def dependencies(self, branch: str) -> tuple[str, ...]:
        """The branch's primary parent followed by its other bases."""
        node = self._nodes[branch]
        return (node.parent, *node.additional_parents)

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
            for name in self._nodes:
                for parent in self.dependencies(name):
                    self._children.setdefault(parent, set()).add(name)
        return sorted(self._children.get(branch, ()))

    def descendants(self, branch: str) -> list[str]:
        """The branches stacked on branch, on those, and so on,
        parent-before-child (branch excluded)."""
        order = [branch] if branch in self._nodes else []
        self._walk_descendants(branch, {branch}, order)
        ordered = dependency_order(order, {b: self.dependencies(b) for b in order})
        return [b for b in ordered if b != branch]

    def _walk_descendants(self, branch: str, seen: set[str], order: list[str]) -> None:
        pending = list(reversed(self.children(branch)))
        while pending:
            child = pending.pop()
            if child not in seen:
                seen.add(child)
                order.append(child)
                pending.extend(reversed(self.children(child)))

    def dependent_branches(self, branches: Iterable[str]) -> set[str]:
        """The given branches and every branch that depends on them."""
        seeds = set(branches)
        seen = set(seeds)
        order: list[str] = []
        for branch in seeds:
            self._walk_descendants(branch, seen, order)
        return seen

    def ancestry(self, branch: str) -> list[str]:
        """[branch, its primary parent, ..., its stack's primary root]"""
        chain = [branch]
        seen = {branch}
        while chain[-1] in self._nodes:
            parent = self._nodes[chain[-1]].parent
            if parent in seen:
                raise StackError(f"cycle in stack metadata involving {parent}")
            seen.add(parent)
            chain.append(parent)
        return chain

    def dependency_ancestry(self, branch: str) -> list[str]:
        """All of branch's bases and branch, each after its own bases."""
        selected = self.dependencies_of((branch,))
        return dependency_order(selected, {b: self.dependencies(b) for b in selected if b in self._nodes})

    def dependencies_of(self, branches: Iterable[str]) -> list[str]:
        """The branches and everything they depend on, each visited once."""
        pending = list(branches)
        selected: list[str] = []
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            selected.append(current)
            if current in self._nodes:
                pending.extend(self.dependencies(current))
        return selected

    def has_multiple_base_ancestor(self, branch: str) -> bool:
        """Whether branch or any tracked dependency has several bases."""
        if branch not in self._nodes:
            return False
        if self._multiple_base is None:
            parents = {b: self.dependencies(b) for b in self._nodes}
            affected: set[str] = set()
            for b in dependency_order(self._nodes, parents):
                if len(parents[b]) > 1 or parents[b][0] in affected:
                    affected.add(b)
            self._multiple_base = affected
        return branch in self._multiple_base

    def _primary_endpoints(self, branch: str) -> tuple[str, str]:
        path: set[str] = set()
        bottom = branch
        while branch in self._nodes and branch not in self._primary:
            if branch in path:
                raise StackError(f"cycle in stack metadata involving {branch}")
            path.add(branch)
            bottom, branch = branch, self.parent(branch)
        endpoints = self._primary.get(branch, (bottom, branch))
        self._primary.update(dict.fromkeys(path, endpoints))
        return endpoints

    def root(self, branch: str) -> str:
        """The root of branch's stack (usually main)."""
        return self._primary_endpoints(branch)[1]

    def bottom(self, branch: str) -> str:
        """The branch at the bottom of tracked branch's stack, on its root."""
        return self._primary_endpoints(branch)[0]

    def is_bottom(self, branch: str) -> bool:
        """Whether tracked branch is at the bottom of its stack."""
        return self._nodes[branch].parent not in self._nodes

    def roots(self) -> list[str]:
        """The untracked dependencies of the graph, sorted."""
        dependency_order(self._nodes, {b: self.dependencies(b) for b in self._nodes})
        return sorted({parent for b in self._nodes for parent in self.dependencies(b) if parent not in self._nodes})

    def in_order(self) -> list[str]:
        """Every tracked branch, by root, parent-before-child."""
        branches: list[str] = []
        seen: set[str] = set()
        for root in self.roots():
            found: list[str] = []
            self._walk_descendants(root, seen, found)
            branches.extend(dependency_order(found, {b: self.dependencies(b) for b in found}))
        return dependency_order(branches, {b: self.dependencies(b) for b in self._nodes})

    def bottoms_in(self, group: str) -> list[str]:
        """The branches at the bottom of a stack in group."""
        if self._bottoms_by_group is None:
            self._bottoms_by_group = {}
            for node in self._nodes.values():
                if node.group is not None and self.is_bottom(node.name):
                    self._bottoms_by_group.setdefault(node.group, []).append(node.name)
        return list(self._bottoms_by_group.get(group, ()))

    def group_bottoms(self, bottom: str) -> list[str]:
        """bottom, a branch at the bottom of a stack, followed by the others
        in its group, if it has one, sorted."""
        group = self._nodes[bottom].group
        if group is None:
            return [bottom]
        return [bottom, *sorted(b for b in self.bottoms_in(group) if b != bottom)]

    def stack_of(self, branch: str, groups: bool = True) -> list[str]:
        """The connected tracked branches, each after its dependencies.

        With groups, include the other stacks of each connected bottom branch's
        group (see group_bottoms), which may be on other roots.
        """
        bottom = self.bottom(branch)
        selected: list[str] = []
        seen: set[str] = set()

        expanded: set[str] = set()

        def add_bottom(base: str) -> None:
            if base not in expanded:
                bottoms = self.group_bottoms(base) if groups else [base]
                expanded.update(bottoms)
                for member in bottoms:
                    if member not in seen:
                        seen.add(member)
                        selected.append(member)
                        found: list[str] = []
                        self._walk_descendants(member, seen, found)
                        selected.extend(dependency_order(found, {b: self.dependencies(b) for b in found}))

        add_bottom(bottom)
        index = 0
        while index < len(selected):
            current = selected[index]
            to_visit = self.unvisited_bottoms(current, seen)
            for base in to_visit:
                add_bottom(base)
            index += 1
        return dependency_order(selected, {b: self.dependencies(b) for b in selected})

    def stack_union(self, branches: Iterable[str], groups: bool = True) -> list[str]:
        """Expand each selected connected stack once, keeping selection order."""
        selected: list[str] = []
        seen: set[str] = set()
        for branch in branches:
            if branch not in seen:
                stack = self.stack_of(branch, groups)
                selected.extend(b for b in stack if b not in seen)
                seen.update(stack)
        return selected

    def unvisited_bottoms(self, branch: str, seen: set[str]) -> list[str]:
        bottoms = [branch] if self.is_bottom(branch) else []
        bottoms.extend(
            self.bottom(parent) for parent in self.dependencies(branch) if parent in self and parent not in seen
        )
        return bottoms

    def copy(self) -> Stacks:
        """A copy to change without changing this one, e.g. to check a plan."""
        return Stacks(self._nodes.values())

    def restricted(self, branches: Iterable[str]) -> Stacks:
        """A copy with only those of branches that are tracked, in which a
        branch whose parent is left out is at the bottom of a stack."""
        return Stacks(self._nodes[b] for b in dict.fromkeys(branches) if b in self._nodes)

    def ensure_no_cycle(self, branch: str, new_parent: str) -> None:
        """Refuse to stack branch on new_parent if new_parent is on branch."""
        if branch in self.dependency_ancestry(new_parent):
            raise StackError(f"{new_parent} is stacked on {branch}; that would create a cycle")

    def ensure_group_available_for_merge(self, branch: str) -> None:
        """Refuse to remove merged branch (see remove_merged) if that would
        put its children in a group named after it that already exists, which
        would join them to unrelated stacks."""
        if (
            self.is_bottom(branch)
            and any(self.parent(child) == branch for child in self.children(branch))
            and self._nodes[branch].group is None
            and branch in self.groups().values()
        ):
            raise StackError(
                f"can't group {branch}'s children: group {branch} already exists; "
                "rename the existing group before continuing"
            )

    def _set(self, node: StackNode) -> None:
        old = self._nodes.get(node.name)
        before = self.dependencies(node.name) if old is not None else ()
        after = (node.parent, *node.additional_parents)
        if before != after:
            self._update_edges(node.name, before, after)
        elif old is not None and old.group != node.group:
            self._bottoms_by_group = None
        self._nodes[node.name] = node

    def _remove(self, branch: str) -> None:
        self._update_edges(branch, self.dependencies(branch), ())
        del self._nodes[branch]

    def _update_edges(self, branch: str, before: Sequence[str], after: Sequence[str]) -> None:
        if self._children is not None:
            for parent in set(before).difference(after):
                children = self._children[parent]
                children.remove(branch)
                if not children:
                    del self._children[parent]
            for parent in set(after).difference(before):
                self._children.setdefault(parent, set()).add(branch)
        self._multiple_base = None
        self._primary.clear()
        self._bottoms_by_group = None

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
        self._set(
            replace(node, parent=parent, additional_parents=tuple(p for p in node.additional_parents if p != parent))
        )

    def set_additional_parents(self, branch: str, parents: Sequence[str]) -> None:
        """Record the remaining ordered dependencies of a tracked branch."""
        self._set(replace(self._nodes[branch], additional_parents=tuple(parents)))

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

    def rename_dependency(self, old: str, new: str) -> list[str]:
        """Replace references to a renamed base, keeping their positions."""
        children = self.children(old)
        for child in children:
            parents = tuple(dict.fromkeys(new if p == old else p for p in self.dependencies(child)))
            if self.parent(child) == old:
                self.reparent(child, parents[0])
            self._set(replace(self._nodes[child], additional_parents=parents[1:]))
        return children

    def drop(self, branch: str) -> list[str]:
        """Move the branches stacked on branch onto its parent, and remove
        branch, without the rules remove_merged follows. Returns the moved
        branches.

        For working out what's left once branch is treated as gone (e.g.
        merged) without changing anything: saving it would untrack branch.
        """
        children = self.children(branch)
        for child in children:
            parents = self._without_dependency(child, branch)
            self._set(replace(self._nodes[child], parent=parents[0], additional_parents=parents[1:]))
        self._remove(branch)
        return children

    def _without_dependency(self, branch: str, removed: str) -> tuple[str, ...]:
        """Replace a dependency with its own bases, preserving their order."""
        return tuple(
            dict.fromkeys(
                base
                for parent in self.dependencies(branch)
                for base in (self.dependencies(removed) if parent == removed else (parent,))
            )
        )

    def remove_merged(self, branch: str) -> list[str]:
        """Move the branches stacked on merged branch onto its parent (see
        reparent), and, if that's a stack's root, put them in branch's group,
        or a group named after branch, so commands on one of them still cover
        the others, as they did before it merged. Then stop tracking branch.
        Returns the moved branches.

        Refuses, changing nothing, as ensure_group_available_for_merge does.
        """
        self.ensure_group_available_for_merge(branch)
        group = self._nodes[branch].group or branch
        children = self.children(branch)
        for child in children:
            primary_removed = self.parent(child) == branch
            parents = self._without_dependency(child, branch)
            if primary_removed:
                self.reparent(child, parents[0])
                if parents[0] not in self._nodes:
                    self.set_group(child, group)
            self._set(replace(self._nodes[child], additional_parents=parents[1:]))
        self._remove(branch)
        return children

    def untrack(self, branches: Iterable[str]) -> None:
        """Stop tracking branches. The branches stacked on them stay on them,
        at the bottom of their stacks now, and are shared from now on if their
        stack was."""
        branches = list(dict.fromkeys(branches))
        classified: set[str] = set()
        shared: set[str] = set()
        for branch in branches:
            if branch not in classified:
                connected = self.stack_of(branch, False)
                classified.update(connected)
                if any(self.is_shared(b) for b in connected):
                    shared.update(connected)
        for member in self.dependent_branches(b for b in branches if b in shared):
            self.set_ownership(member, Ownership.SHARED)
        for branch in branches:
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
