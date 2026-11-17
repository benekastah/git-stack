"""The selection and root protection that last for an entire stack operation."""

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from itertools import islice

from git_stack.stacks import Stacks, dependency_order


@dataclass(frozen=True)
class DependencyPlan:
    """Snapshot which branches and roots an operation covers.

    The operation may change edges as it goes. The order, scope and protected
    roots remain fixed so a merge or a skipped branch cannot narrow the plan.
    """

    order: tuple[str, ...]
    scope: frozenset[str]
    roots: frozenset[str]

    @classmethod
    def select(cls, stacks: Stacks, branches: Iterable[str]) -> DependencyPlan:
        parents = {branch: stacks.dependencies(branch) for branch in stacks}
        order = tuple(dependency_order(branches, parents))
        return cls(order, frozenset(order), dependency_roots(order, parents))

    def remaining(self, index: int, stacks: Stacks, skipped: AbstractSet[str]) -> Iterable[str]:
        """Queue after a replay, excluding dependents of earlier skips."""
        return (
            branch
            for branch in islice(self.order, index + 1, None)
            if not skipped or skipped.isdisjoint(stacks.dependency_ancestry(branch))
        )


def dependency_roots(branches: tuple[str, ...], parents: dict[str, tuple[str, ...]]) -> frozenset[str]:
    """Follow every dependency, even when a tracked source is outside the scope."""
    roots: set[str] = set()
    seen: set[str] = set()
    pending = list(branches)
    while pending:
        branch = pending.pop()
        if branch in seen:
            continue
        seen.add(branch)
        if branch in parents:
            pending.extend(parents[branch])
        else:
            roots.add(branch)
    return frozenset(roots)


def blocked_base(branch: str, stacks: Stacks, skipped: AbstractSet[str]) -> str | None:
    """Return the first dependency earlier in the plan that was skipped."""
    return next((base for base in stacks.dependencies(branch) if base in skipped), None)
