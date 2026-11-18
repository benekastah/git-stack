"""The selection and root protection that last for an entire stack operation."""

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, replace
from itertools import islice

from git_stack.errors import StackError
from git_stack.stacks import Stacks, dependency_order

type PlannedBranch = tuple[str, tuple[str, ...]]


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

    def remaining(self, index: int, stacks: Stacks, skipped: AbstractSet[str]) -> Iterable[PlannedBranch]:
        """Queue after a replay, excluding dependents of earlier skips."""
        blocked: set[str] = stacks.dependent_branches(skipped) if skipped else set()
        for branch in islice(self.order, index + 1, None):
            if branch not in blocked:
                yield branch, stacks.dependencies(branch)


def project_remaining(stacks: Stacks, remaining: tuple[PlannedBranch, ...]) -> Stacks:
    """Restore the in-memory parent choices made before a conflict."""
    parents = dict(remaining)
    if missing := parents.keys() - set(stacks):
        raise StackError(f"cannot resume restack: these branches are no longer tracked: {', '.join(sorted(missing))}")
    return Stacks(
        replace(stacks[b], parent=parents[b][0], additional_parents=parents[b][1:]) if b in parents else stacks[b]
        for b in stacks
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
