"""What stack operations report as they go.

StackCore and its subclasses pass each event to the on_event callback they
were created with; git_stack.output turns them into messages for the CLI.
"""

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class WaitingForLock:
    """Another git-stack command in the repository is running, so this one is
    waiting for it to finish."""


@dataclass(frozen=True)
class Fetching:
    """remote is being fetched, with --prune."""

    remote: str


@dataclass(frozen=True)
class LookingUpPullRequests:
    """autocreate is searching GitHub for the user's open pull requests."""


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
class TookUpstreamRebase:
    """branch was moved to upstream, its own commits rebased elsewhere (e.g. by
    GitHub's "Rebase stack" button)."""

    branch: str
    upstream: str


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
class TrunkInOtherWorktree:
    """branch wasn't fast-forwarded to upstream because it's checked out in the
    worktree at path."""

    branch: str
    upstream: str
    path: str


@dataclass(frozen=True)
class NotRestackedInOtherWorktree:
    """branch needed restacking, but was skipped (along with the branches stacked
    on it) because it's checked out in the worktree at path."""

    branch: str
    path: str


@dataclass(frozen=True)
class NotSquashedInOtherWorktree:
    branch: str
    path: str


@dataclass(frozen=True)
class NotPulledInOtherWorktree:
    """branch's upstream has new commits, but they weren't pulled because branch
    is checked out in the worktree at path."""

    branch: str
    path: str


@dataclass(frozen=True)
class NotDeletedInOtherWorktree:
    """branch was merged and untracked, but not deleted because it's checked out
    in the worktree at path."""

    branch: str
    path: str


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
    children: tuple[str, ...]
    deleted_tip: str | None


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
class Reverted:
    """branch was merged into parent, then parent reverted some of its
    changes, so it was left alone."""

    branch: str
    parent: str


@dataclass(frozen=True)
class MergedParentReverted:
    """branch was stacked on merged until it merged into parent, and parent
    has since reverted some of merged's changes."""

    branch: str
    merged: str
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
class NoPullRequests:
    """branches were pushed but have no pull request."""

    branches: tuple[str, ...]


@dataclass(frozen=True)
class GitHubStackLinked:
    """The pull requests numbers were put in a stack on GitHub, at its top."""

    numbers: tuple[int, ...]


@dataclass(frozen=True)
class GitHubStackUnstacked:
    """The open pull requests of stack number on GitHub were taken out of it,
    so pull request pull_request's base could be changed."""

    number: int
    pull_request: int


@dataclass(frozen=True)
class GitHubStackMismatch:
    """The pull requests numbers, a chain bottom first, weren't linked into a
    stack on GitHub, since some are in stacks (their numbers) there that
    don't fit them."""

    numbers: tuple[int, ...]
    stacks: tuple[int, ...]


@dataclass(frozen=True)
class PullRequestRetargeted:
    branch: str
    number: int
    base: str


# Every event, so code that handles them can check it handles each one.
Event: TypeAlias = (
    WaitingForLock
    | Fetching
    | LookingUpPullRequests
    | Squashing
    | Restacking
    | PullingUpstream
    | TookUpstreamRebase
    | FastForwarded
    | TrunkDiverged
    | TrunkInOtherWorktree
    | NotRestackedInOtherWorktree
    | NotSquashedInOtherWorktree
    | NotPulledInOtherWorktree
    | NotDeletedInOtherWorktree
    | SkippedMerged
    | Untracked
    | RemoteBranchDeleted
    | RemoteBranchDeleteFailed
    | RemoteBranchKept
    | ParentSkipped
    | MergedWithNewCommits
    | Reverted
    | MergedParentReverted
    | UpstreamGone
    | BrokenBranch
    | ParentMissing
    | PullRequestLookupFailed
    | NoPullRequests
    | PullRequestRetargeted
    | GitHubStackLinked
    | GitHubStackMismatch
    | GitHubStackUnstacked
)
