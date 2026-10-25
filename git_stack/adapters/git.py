import enum
from dataclasses import dataclass

from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    LANDED_CHECK_KEY,
    MERGED_PARENT_REVERTED_CHECK_KEY,
    REVERTED_CHECK_KEY,
    MergedParent,
    Parents,
    StackCore,
)


class PullRequestState(enum.Enum):
    """Values are what `git stack info --json` prints."""

    OPEN = "open"
    DRAFT = "draft"
    MERGED = "merged"


@dataclass(frozen=True)
class PullRequestLink:
    """A branch's pull request, as info shows it."""

    number: int
    url: str
    state: PullRequestState


class GitAdapter:
    """Works out whether branches were merged from the repository alone."""

    def __init__(self, core: StackCore) -> None:
        self.core = core
        self.git = core.git

    def merge_state(self, branch: str, parent: str) -> MergeState:
        """Whether branch has landed in parent, so dropping it loses nothing.

        Its changes must be in parent. Unless its remote branch was deleted
        (which is all the evidence of a merge we need), the branch must also
        change something, so a new branch with no commits isn't mistaken for a
        merged one, and if it has a remote branch, commits pushed there after
        the merge make it MERGED_WITH_NEW_COMMITS, so they aren't dropped. If
        parent has since reverted any of its changes, it's REVERTED.
        """
        git = self.git
        if not self.landed(branch, parent):
            return MergeState.NOT_MERGED
        if self.reverted(branch, parent):
            return MergeState.REVERTED
        if git.upstream_gone(branch):
            return MergeState.MERGED
        if not self.changes_something(branch):
            return MergeState.NOT_MERGED
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if (
            upstream_tip
            and not git.contained_in(upstream_tip, parent)
            and self.core.unpulled_commits(branch, parent, upstream_tip)
        ):
            return MergeState.MERGED_WITH_NEW_COMMITS
        return MergeState.MERGED

    def landed(self, branch: str, parent: str) -> bool:
        """Whether parent has branch's changes, as a merge or as patches."""
        git = self.git
        parent_tip = git.rev(parent)
        if parent_tip is None:
            return False
        tip = git.tip(branch)
        return self.core.remembered(
            branch,
            LANDED_CHECK_KEY,
            [tip, parent_tip, git.config_get(branch, BASE_KEY) or "-"],
            lambda: git.contained_in(tip, parent_tip) or self.landed_as_patches(branch, parent_tip),
        )

    def changes_something(self, branch: str) -> bool:
        """Whether branch's tree differs from its recorded base's.

        False for a branch with no commits of its own yet, and when the base is
        missing or not in its history.
        """
        base = self.git.config_get(branch, BASE_KEY)
        if base is None or not self.git.is_ancestor(base, branch):
            return False
        return not self.git.ok("diff", "--quiet", base, f"refs/heads/{branch}")

    def landed_as_patches(self, branch: str, parent: str) -> bool:
        """Whether parent has branch's changes as one squashed commit, or as
        commits with the same changes as each of branch's.

        Unlike GitClient.contained_in, this still holds after parent edits the
        same lines again.
        """
        git = self.git
        base = git.config_get(branch, BASE_KEY)
        if base is None or not git.is_ancestor(base, branch) or git.rev(parent) is None:
            return False
        files = self.changed_files(base, f"refs/heads/{branch}")
        if not files:
            return False
        # Only parent's commits that touch the same files can match.
        landed = git.patch_ids(
            "log", "-p", "--no-merges", "--full-diff", parent, "--not", f"refs/heads/{branch}", "--", *files
        )
        if git.patch_ids("diff", base, f"refs/heads/{branch}") & landed:
            return True
        own = git.patch_ids("log", "-p", "--no-merges", f"{base}..refs/heads/{branch}")
        return bool(own) and own <= landed

    def reverted(self, branch: str, parent: str) -> bool:
        """Whether parent, which has branch's changes, has since reverted some of
        them (see reverts)."""
        git = self.git
        parent_tip = git.rev(parent)
        base = git.config_get(branch, BASE_KEY)
        if parent_tip is None or base is None or not git.is_ancestor(base, branch):
            return False
        tip = git.tip(branch)
        merges = self.landing_merges(branch, parent)
        return self.core.remembered(
            branch,
            REVERTED_CHECK_KEY,
            [tip, parent_tip, base, *merges],
            lambda: bool(self.reverts_since(tip, base, parent_tip, merges)),
        )

    def reverts(self, branch: str, parent: str) -> list[str]:
        """The commits on parent, newest first, that revert some of branch's
        changes, and that nothing on parent has since reapplied.

        A revert counts if it undoes the whole branch, one of its commits, or
        one of the commits that landed it in parent (see landing_merges), as
        `git revert` does. Changes undone by hand, or a revert edited (e.g. to
        resolve a conflict), aren't recognized.
        """
        git = self.git
        parent_tip = git.rev(parent)
        base = git.config_get(branch, BASE_KEY)
        if parent_tip is None or base is None or not git.is_ancestor(base, branch):
            return []
        return self.reverts_since(git.tip(branch), base, parent_tip, self.landing_merges(branch, parent))

    def reverted_merged_parent(self, branch: str) -> MergedParent | None:
        """The branch that branch was stacked on until it merged (see
        StackCore.merged_parent), if the parent it merged into has since
        reverted some of its changes."""
        merged = self.core.merged_parent(branch)
        if merged is None:
            return None
        parent_tip = self.git.rev(merged.parent)
        # Its commits may be gone, e.g. garbage-collected after it was deleted.
        if parent_tip is None or self.git.rev(merged.tip) is None or self.git.rev(merged.base) is None:
            return None
        reverted = self.core.remembered(
            branch,
            MERGED_PARENT_REVERTED_CHECK_KEY,
            [merged.tip, parent_tip, merged.base, *merged.merges],
            lambda: bool(self.merged_parent_reverts(merged)),
        )
        return merged if reverted else None

    def merged_parent_reverts(self, merged: MergedParent) -> list[str]:
        """reverts, for the merged branch merged."""
        parent_tip = self.git.rev(merged.parent)
        if parent_tip is None:
            return []
        return self.reverts_since(merged.tip, merged.base, parent_tip, merged.merges)

    def landing_merges(self, branch: str, parent: str) -> tuple[str, ...]:
        """Commits known to have landed branch in parent, whose reverts count as
        reverting branch."""
        return ()

    def reverts_since(self, tip: str, base: str, parent_tip: str, merges: tuple[str, ...]) -> list[str]:
        """reverts, for a branch at tip built on base, and parent at parent_tip."""
        git = self.git
        files = self.changed_files(base, tip)
        if not files:
            return []
        # Without context lines, a revert of one of the branch's commits matches
        # even when it was reverted after later commits changed the lines
        # around it. Without prefixes, so -R doesn't swap "a/" and "b/".
        diff = ("-U0", "--no-prefix")
        # Each change the branch made, as (the patch-id of doing it, of undoing it).
        own = f"{base}..{tip}"
        changes = [
            (done, undone)
            for (done, _), (undone, _) in zip(
                git.commit_patch_ids("log", "-p", "--no-merges", *diff, own),
                git.commit_patch_ids("log", "-p", "--no-merges", "-R", *diff, own),
                strict=True,
            )
        ]
        for old, new in [(base, tip), *((f"{m}^", m) for m in merges)]:
            forward, backward = git.patch_ids("diff", *diff, old, new), git.patch_ids("diff", "-R", *diff, old, new)
            # Empty for a merge that changed nothing.
            if forward and backward:
                changes.append((forward.pop(), backward.pop()))
        doing: dict[str, set[int]] = {}
        undoing: dict[str, set[int]] = {}
        for i, (done, undone) in enumerate(changes):
            doing.setdefault(done, set()).add(i)
            undoing.setdefault(undone, set()).add(i)
        # A patch that does one of the branch's changes and undoes another (e.g.
        # its own commits revert each other) doesn't say which happened.
        for patch in doing:
            undoing.pop(patch, None)
        # The newest commit that does or undoes a change says whether it's undone.
        decided: set[int] = set()
        reverts = []
        log = ("log", "-p", "--no-merges", "--full-diff", *diff, parent_tip, "--not", tip, "--", *files)
        for patch, commit in git.commit_patch_ids(*log):
            if undecided := undoing.get(patch, set()) - decided:
                reverts.append(commit)
            decided |= undecided | doing.get(patch, set())
        return reverts

    def changed_files(self, base: str, rev: str) -> list[str]:
        """The files rev changes since base."""
        files = self.git.run("diff", "--name-only", "-z", base, rev).split("\0")
        return [f for f in files if f]

    def prefetch(self) -> None:
        """Look up whatever merge_state asks a remote service about, so it's
        ready when needed. Runs alongside other work, so it must be thread-safe."""

    def pull_request_link(self, branch: str) -> PullRequestLink | None:
        """branch's pull request, open or merged, if the adapter knows of one."""
        return None

    def ensure_available(self) -> None:
        """Fail now if whatever the adapter asks about merges can't answer,
        rather than falling back to the repository alone."""

    def before_push(self, to_push: list[str], parents: Parents) -> None:
        """Called before to_push, a stack's unmerged branches, are pushed."""

    def after_push(self, pushed: list[str], parents: Parents) -> None:
        """Called once pushed, a stack's unmerged branches, were pushed."""
