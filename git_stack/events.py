"""What stack operations report as they go.

StackCore and its subclasses pass each event to the on_event callback they
were created with; git_stack.output turns them into messages for the CLI.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Squashing:
    branch: str
    count: int


@dataclass(frozen=True)
class Restacking:
    branch: str
    parent: str


@dataclass(frozen=True)
class PullingUpstream:
    branch: str
    count: int


@dataclass(frozen=True)
class FastForwarded:
    branch: str
    upstream: str


@dataclass(frozen=True)
class TrunkDiverged:
    """branch wasn't fast-forwarded because it has commits upstream doesn't."""
    branch: str
    upstream: str


@dataclass(frozen=True)
class SkippedMerged:
    """branch looks merged, so it was left out and its children moved onto parent,
    without changing the stack's metadata."""
    branch: str
    parent: str


@dataclass(frozen=True)
class Untracked:
    """Merged branch was removed from its stack, its children moved onto parent.

    deleted_tip is the commit the branch pointed at if it was deleted, or None
    if it was kept.
    """
    branch: str
    parent: str
    children: Tuple[str, ...]
    deleted_tip: Optional[str]


@dataclass(frozen=True)
class RemoteBranchDeleted:
    upstream: str


@dataclass(frozen=True)
class RemoteBranchDeleteFailed:
    """upstream couldn't be deleted, so branch was left alone."""
    branch: str
    upstream: str


@dataclass(frozen=True)
class RemoteBranchKept:
    """A merged branch was untracked, but its remote branch still exists."""
    upstream: str
    remote: str
    name: str


@dataclass(frozen=True)
class ParentSkipped:
    branch: str
    parent: str


@dataclass(frozen=True)
class MergedWithNewCommits:
    """branch was merged into parent, but it or its remote branch has commits
    that weren't, so it was left alone."""
    branch: str
    parent: str


@dataclass(frozen=True)
class UpstreamGone:
    """branch's remote branch was deleted, but it wasn't merged into parent."""
    branch: str
    parent: str


@dataclass(frozen=True)
class BrokenBranch:
    """branch was skipped because its metadata doesn't match the repository."""
    branch: str
    reason: str


@dataclass(frozen=True)
class ParentMissing:
    branch: str
    parent: str


@dataclass(frozen=True)
class PullRequestLookupFailed:
    """GitHub couldn't be asked about pull requests, so merges are only
    detected locally."""
    error: str


@dataclass(frozen=True)
class PullRequestFound:
    branch: str
    number: int


@dataclass(frozen=True)
class PullRequestOpened:
    branch: str
    number: int
    url: str


@dataclass(frozen=True)
class PullRequestRetargeted:
    branch: str
    number: int
    base: str
