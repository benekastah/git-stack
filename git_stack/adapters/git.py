import enum
from dataclasses import dataclass

from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, LANDED_CHECK_KEY, Parents, StackCore


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
        the merge make it MERGED_WITH_NEW_COMMITS, so they aren't dropped.
        """
        git = self.git
        if not self.landed(branch, parent):
            return MergeState.NOT_MERGED
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
        files = git.run("diff", "--name-only", "-z", base, f"refs/heads/{branch}").split("\0")
        files = [f for f in files if f]
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
