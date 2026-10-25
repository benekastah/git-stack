import enum
from dataclasses import dataclass

from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, LANDED_CHECK_KEY, REVERTED_CHECK_KEY, Parents, StackCore


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

    def reverted(self, branch: str, parent: str, merges: tuple[str, ...] = ()) -> bool:
        """Whether parent, which has branch's changes, has since reverted some of
        them, and not reapplied them since.

        A revert counts if it undoes the whole branch, one of its commits, or
        one of merges, the commits that landed it in parent (e.g. its pull
        request's merge commit), as `git revert` does. Changes undone by hand,
        or a revert edited (e.g. to resolve a conflict), aren't recognized.
        """
        git = self.git
        parent_tip = git.rev(parent)
        base = git.config_get(branch, BASE_KEY)
        if parent_tip is None or base is None or not git.is_ancestor(base, branch):
            return False
        tip = git.tip(branch)
        return self.core.remembered(
            branch,
            REVERTED_CHECK_KEY,
            [tip, parent_tip, base, *merges],
            lambda: self.reverted_since(tip, base, parent_tip, merges),
        )

    def reverted_since(self, tip: str, base: str, parent_tip: str, merges: tuple[str, ...]) -> bool:
        """reverted, for a branch at tip built on base, and parent at parent_tip."""
        git = self.git
        files = self.changed_files(base, tip)
        if not files:
            return False
        # Without context lines, a revert of one of the branch's commits matches
        # even when it was reverted after later commits changed the lines
        # around it. Without prefixes, so -R doesn't swap "a/" and "b/".
        diff = ("-U0", "--no-prefix")
        ranges = [(base, tip), *((f"{m}^", m) for m in merges)]
        applied = git.patch_ids("log", "-p", "--no-merges", *diff, f"{base}..{tip}")
        reverted = git.patch_ids("log", "-p", "--no-merges", "-R", *diff, f"{base}..{tip}")
        for old, new in ranges:
            applied |= git.patch_ids("diff", *diff, old, new)
            reverted |= git.patch_ids("diff", "-R", *diff, old, new)
        # A patch that does one of the branch's changes and undoes another (e.g.
        # its own commits revert each other) doesn't say which happened.
        reverted -= applied
        # The newest commit that does or undoes one of them says which is current.
        for patch in git.patch_id_list(
            "log", "-p", "--no-merges", "--full-diff", *diff, parent_tip, "--not", tip, "--", *files
        ):
            if patch in reverted:
                return True
            if patch in applied:
                return False
        return False

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
