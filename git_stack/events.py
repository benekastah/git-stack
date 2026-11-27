"""What stack operations report as they go.

StackCore and its subclasses pass each event to the on_event callback they
were created with; git_stack.output turns them into messages for the CLI.

Each event's event_type is its type in --json output. Scripts rely on it, and
on the event's field names, so they don't change once released.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class WaitingForLock:
    """Another git-stack command in the repository is running, so this one is
    waiting for it to finish."""

    event_type: ClassVar[str] = "waiting-for-lock"


@dataclass(frozen=True)
class Fetching:
    """remote is being fetched, with --prune."""

    event_type: ClassVar[str] = "fetching"

    remote: str


@dataclass(frozen=True)
class LookingUpPullRequests:
    """import is searching GitHub for the open pull requests of author (a
    GitHub login), or if None, the user's: one for each author it searches
    for, if any (there's none when it only has pull requests by deleted
    accounts to go on)."""

    event_type: ClassVar[str] = "looking-up-pull-requests"

    author: str | None = None


@dataclass(frozen=True)
class LookingUpNamedPullRequests:
    """import is looking up the pull requests numbers, named with --pr,
    before it searches for their authors'."""

    event_type: ClassVar[str] = "looking-up-named-pull-requests"

    numbers: tuple[int, ...]


@dataclass(frozen=True)
class Squashing:
    event_type: ClassVar[str] = "squashing"

    branch: str
    count: int


@dataclass(frozen=True)
class Restacking:
    event_type: ClassVar[str] = "restacking"

    branch: str
    parent: str


@dataclass(frozen=True)
class RebuildingIntegration:
    event_type: ClassVar[str] = "rebuilding-integration"

    branch: str


@dataclass(frozen=True)
class RemovingIntegrationWorktree:
    event_type: ClassVar[str] = "removing-integration-worktree"

    branch: str


@dataclass(frozen=True)
class CheckingPush:
    event_type: ClassVar[str] = "checking-push"

    remote: str


@dataclass(frozen=True)
class PullingUpstream:
    event_type: ClassVar[str] = "pulling-upstream"

    branch: str
    count: int


@dataclass(frozen=True)
class TookUpstreamRebase:
    """branch was moved to upstream, its own commits rebased elsewhere (e.g. by
    GitHub's "Rebase stack" button)."""

    event_type: ClassVar[str] = "took-upstream-rebase"

    branch: str
    upstream: str


@dataclass(frozen=True)
class FastForwarded:
    event_type: ClassVar[str] = "fast-forwarded"

    branch: str
    upstream: str


@dataclass(frozen=True)
class TrunkDiverged:
    """branch wasn't fast-forwarded because it has commits upstream doesn't."""

    event_type: ClassVar[str] = "trunk-diverged"

    branch: str
    upstream: str


@dataclass(frozen=True)
class TrunkInOtherWorktree:
    """branch wasn't fast-forwarded to upstream because it's checked out in the
    worktree at path."""

    event_type: ClassVar[str] = "trunk-in-other-worktree"

    branch: str
    upstream: str
    path: str


@dataclass(frozen=True)
class NotRestackedInOtherWorktree:
    """branch needed restacking, but was skipped (along with the branches stacked
    on it) because it's checked out in the worktree at path."""

    event_type: ClassVar[str] = "not-restacked-in-other-worktree"

    branch: str
    path: str


@dataclass(frozen=True)
class NotSquashedInOtherWorktree:
    event_type: ClassVar[str] = "not-squashed-in-other-worktree"

    branch: str
    path: str


@dataclass(frozen=True)
class NotPulledInOtherWorktree:
    """branch's upstream has new commits, but they weren't pulled because branch
    is checked out in the worktree at path."""

    event_type: ClassVar[str] = "not-pulled-in-other-worktree"

    branch: str
    path: str


@dataclass(frozen=True)
class NotDeletedInOtherWorktree:
    """branch was merged and untracked, but not deleted because it's checked out
    in the worktree at path."""

    event_type: ClassVar[str] = "not-deleted-in-other-worktree"

    branch: str
    path: str


@dataclass(frozen=True)
class SkippedMerged:
    """branch looks merged, so it was left out and its children moved onto parent,
    without changing the stack's metadata."""

    event_type: ClassVar[str] = "skipped-merged"

    branch: str
    parent: str


@dataclass(frozen=True)
class Untracked:
    """Merged branch was removed from its stack, its children moved onto parent.

    deleted_tip is the commit the branch pointed at if it was deleted, or None
    if it was kept.
    """

    event_type: ClassVar[str] = "untracked"

    branch: str
    parent: str
    children: tuple[str, ...]
    deleted_tip: str | None


@dataclass(frozen=True)
class RemoteBranchDeleted:
    event_type: ClassVar[str] = "remote-branch-deleted"

    upstream: str


@dataclass(frozen=True)
class RemoteBranchDeleteFailed:
    """upstream couldn't be deleted, so branch was left alone."""

    event_type: ClassVar[str] = "remote-branch-delete-failed"

    branch: str
    upstream: str


@dataclass(frozen=True)
class RetryGroupNotRecorded:
    """A sync or pull untracked group's work on integration and didn't publish
    it, but couldn't record that group should still select it the next time
    the group is synced."""

    event_type: ClassVar[str] = "retry-group-not-recorded"

    integration: str
    group: str
    error: str


@dataclass(frozen=True)
class RemoteBranchKept:
    """A merged branch was untracked, but its remote branch still exists."""

    event_type: ClassVar[str] = "remote-branch-kept"

    upstream: str
    remote: str
    name: str


@dataclass(frozen=True)
class NotDeletedUnconfirmedMerge:
    """branch was untracked, since parent has its changes, but neither it nor
    its remote branch was deleted, since nothing else shows that it was merged:
    the changes could be someone else's."""

    event_type: ClassVar[str] = "not-deleted-unconfirmed-merge"

    branch: str
    parent: str


@dataclass(frozen=True)
class ParentSkipped:
    event_type: ClassVar[str] = "parent-skipped"

    branch: str
    parent: str


@dataclass(frozen=True)
class MergedWithNewCommits:
    """branch was merged into parent, but it or its remote branch has commits
    that weren't, so it was left alone."""

    event_type: ClassVar[str] = "merged-with-new-commits"

    branch: str
    parent: str


@dataclass(frozen=True)
class Reverted:
    """branch was merged into parent, then parent reverted some of its
    changes with reverts (newest first), so it was left alone, along with
    children, the branches stacked on it."""

    event_type: ClassVar[str] = "reverted"

    branch: str
    parent: str
    reverts: tuple[str, ...]
    children: tuple[str, ...]


@dataclass(frozen=True)
class UpstreamGone:
    """branch's remote branch was deleted, but it wasn't merged into parent."""

    event_type: ClassVar[str] = "upstream-gone"

    branch: str
    parent: str


@dataclass(frozen=True)
class BrokenBranch:
    """branch was skipped because its metadata doesn't match the repository."""

    event_type: ClassVar[str] = "broken-branch"

    branch: str
    reason: str


@dataclass(frozen=True)
class ParentMissing:
    """branch was skipped because its parent, a stack's root, doesn't exist.
    base is branch's recorded base, which moving it with `track --base` keeps."""

    event_type: ClassVar[str] = "parent-missing"

    branch: str
    parent: str
    base: str | None = None


def move_off_missing_parent(branch: str, parent: str, base: str | None) -> str:
    """How to move branch off parent, which doesn't exist, keeping base: for
    ParentMissing's message, and the error restacking branch gives."""
    return (
        f"If {parent} was merged, move {branch} onto the branch it was merged into with "
        f"`git stack track <parent> {branch} --base {base[:12] if base else '<sha>'}`"
    )


@dataclass(frozen=True)
class MovedOffDeletedParent:
    """branch's parent, a stack's root, was deleted, locally and on the
    remote, as a merged branch is, so branch was moved onto new_parent, which
    its pull request merges into now, keeping its base."""

    event_type: ClassVar[str] = "moved-off-deleted-parent"

    branch: str
    parent: str
    new_parent: str


@dataclass(frozen=True)
class PullRequestLookupFailed:
    """GitHub couldn't be asked about pull requests, so merges are only
    detected locally."""

    event_type: ClassVar[str] = "pull-request-lookup-failed"

    error: str


@dataclass(frozen=True)
class NoPullRequests:
    """branches were pushed but have no pull request."""

    event_type: ClassVar[str] = "no-pull-requests"

    branches: tuple[str, ...]


@dataclass(frozen=True)
class GitHubStackLinked:
    """The pull requests numbers were put in a stack on GitHub, at its top."""

    event_type: ClassVar[str] = "github-stack-linked"

    numbers: tuple[int, ...]


@dataclass(frozen=True)
class GitHubStackUnstacked:
    """The open pull requests of stack stack on GitHub were taken out of it, so
    the base of pull request number could be changed."""

    event_type: ClassVar[str] = "github-stack-unstacked"

    stack: int
    number: int


@dataclass(frozen=True)
class OrphanedPullRequestClosure:
    """A blocking draft was closed or would be closed during a rebuild."""

    event_type: ClassVar[str] = "orphaned-pull-request-closure"
    number: int
    url: str
    head: str
    base: str
    closed: bool


@dataclass(frozen=True)
class GitHubStackRebuildPlanned:
    """GitHub stacks to unstack and pull requests to put in a new stack."""

    event_type: ClassVar[str] = "github-stack-rebuild-planned"
    github_stacks: tuple[int, ...]
    numbers: tuple[int, ...]


@dataclass(frozen=True)
class GitHubStackAddPlanned:
    """The pull requests numbers would be added to the top of stack stack on
    GitHub."""

    event_type: ClassVar[str] = "github-stack-add-planned"
    stack: int
    numbers: tuple[int, ...]


@dataclass(frozen=True)
class GitHubStackUnstackPlanned:
    """GitHub stacks that would be unstacked to retarget pull requests."""

    event_type: ClassVar[str] = "github-stack-unstack-planned"
    github_stacks: tuple[int, ...]
    numbers: tuple[int, ...]


@dataclass(frozen=True)
class GitHubStackMismatch:
    """The pull requests numbers, a chain bottom first, weren't linked into a
    stack on GitHub, since some are in stacks there (github_stacks, by their
    numbers) that don't fit them."""

    event_type: ClassVar[str] = "github-stack-mismatch"

    numbers: tuple[int, ...]
    github_stacks: tuple[int, ...]
    rebuild_hint: str = ""


@dataclass(frozen=True)
class PullRequestRetargeted:
    event_type: ClassVar[str] = "pull-request-retargeted"

    branch: str
    number: int
    base: str


# Every event, so code that handles them can check it handles each one.
type Event = (
    WaitingForLock
    | Fetching
    | LookingUpPullRequests
    | LookingUpNamedPullRequests
    | Squashing
    | Restacking
    | RebuildingIntegration
    | RemovingIntegrationWorktree
    | CheckingPush
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
    | RetryGroupNotRecorded
    | RemoteBranchKept
    | NotDeletedUnconfirmedMerge
    | ParentSkipped
    | MergedWithNewCommits
    | Reverted
    | UpstreamGone
    | BrokenBranch
    | ParentMissing
    | MovedOffDeletedParent
    | PullRequestLookupFailed
    | NoPullRequests
    | PullRequestRetargeted
    | GitHubStackLinked
    | GitHubStackMismatch
    | GitHubStackUnstacked
    | OrphanedPullRequestClosure
    | GitHubStackRebuildPlanned
    | GitHubStackAddPlanned
    | GitHubStackUnstackPlanned
)
