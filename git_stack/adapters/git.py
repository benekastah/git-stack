import enum
from dataclasses import dataclass

from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    LANDED_CHECK_KEY,
    REVERTED_CHECK_KEY,
    StackCore,
)
from git_stack.stacks import Stacks


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
        # What the adapter has changed outside the repository (e.g. a pull
        # request's base on GitHub), each saying how to undo it, so a push
        # that then fails can say what it left changed.
        self.changes: list[str] = []

    def merge_state(self, branch: str, parent: str, ignore_revert: bool = False) -> MergeState:
        """Whether branch has landed in parent, so dropping it loses nothing.

        Its changes must be in parent. Unless its remote branch was deleted
        (which is all the evidence of a merge we need), the branch must also
        change something, so a new branch with no commits isn't mistaken for a
        merged one, and if it has a remote branch, commits pushed there after
        the merge make it MERGED_WITH_NEW_COMMITS, so they aren't dropped. If
        parent has since reverted any of its changes, it's REVERTED, unless
        ignore_revert is set.
        """
        git = self.git
        if not self.landed(branch, parent):
            return MergeState.NOT_MERGED
        if not ignore_revert and self.reverted(branch, parent):
            return MergeState.REVERTED
        if git.upstream_gone(branch):
            return MergeState.MERGED
        if not self.changes_something(branch):
            return MergeState.NOT_MERGED
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if (
            upstream_tip
            and not git.contained_in(upstream_tip, f"refs/heads/{parent}")
            and self.core.unpulled_commits(branch, parent, upstream_tip)
        ):
            return MergeState.MERGED_WITH_NEW_COMMITS
        return MergeState.MERGED

    def merge_confirmed(self, branch: str, parent: str) -> bool:
        """Whether branch, which merge_state says is MERGED into parent, was
        merged as more than changes parent has too, which could be someone
        else's (e.g. the same dependency bump): its commits are in parent's
        history, or its remote branch was deleted (as when GitHub deletes a
        merged pull request's branch). Only then is it safe to delete."""
        git = self.git
        return git.upstream_gone(branch) or git.is_ancestor(f"refs/heads/{branch}", f"refs/heads/{parent}")

    def landed(self, branch: str, parent: str) -> bool:
        """Whether parent has branch's changes, as a merge or as patches."""
        git = self.git
        parent_tip = git.rev(f"refs/heads/{parent}")
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
        missing or not in its history. Every file counts, even with
        diff.relative set.
        """
        base = self.git.config_get(branch, BASE_KEY)
        if base is None or not self.git.is_ancestor(base, f"refs/heads/{branch}"):
            return False
        return not self.git.ok("diff", "--no-relative", "--quiet", base, f"refs/heads/{branch}")

    def landed_as_patches(self, branch: str, parent_tip: str) -> bool:
        """Whether parent_tip, the commit branch's parent is at, has branch's
        changes as one squashed commit, or as commits with the same changes as
        each of branch's.

        Unlike GitClient.contained_in, this still holds after the parent edits the
        same lines again.
        """
        git = self.git
        base = git.config_get(branch, BASE_KEY)
        if base is None or not git.is_ancestor(base, f"refs/heads/{branch}") or git.rev(parent_tip) is None:
            return False
        files = self.changed_files(base, f"refs/heads/{branch}")
        if not files:
            return False
        landed, squashed, own = (
            {patch_id for patch_id, _ in patches}
            for patches in git.commit_patch_ids(
                # Only the parent's commits that touch the same files can match.
                ("log", "-p", "--no-merges", "--full-diff", parent_tip, "--not", f"refs/heads/{branch}", "--", *files),
                ("diff", base, f"refs/heads/{branch}"),
                ("log", "-p", "--no-merges", f"{base}..refs/heads/{branch}"),
            )
        )
        if squashed & landed:
            return True
        # A merge's own changes aren't in any of the commits compared.
        return (
            bool(own)
            and own <= landed
            and not git.merges_with_own_changes(f"refs/heads/{branch}", "--not", base, parent_tip)
        )

    def reverted(self, branch: str, parent: str) -> bool:
        """Whether parent, which has branch's changes, has since reverted some of
        them (see reverts)."""
        git = self.git
        parent_tip = git.rev(f"refs/heads/{parent}")
        base = git.config_get(branch, BASE_KEY)
        if parent_tip is None or base is None or not git.is_ancestor(base, f"refs/heads/{branch}"):
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
        parent_tip = git.rev(f"refs/heads/{parent}")
        base = git.config_get(branch, BASE_KEY)
        if parent_tip is None or base is None or not git.is_ancestor(base, f"refs/heads/{branch}"):
            return []
        return self.reverts_since(git.tip(branch), base, parent_tip, self.landing_merges(branch, parent))

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
        own = f"{base}..{tip}"
        # The whole branch, and each merge that landed it, as (before, after).
        landings = [(base, tip), *((f"{m}^", m) for m in merges)]
        own_done, own_undone, *landing_patches, since = git.commit_patch_ids(
            ("log", "-p", "--no-merges", *diff, own),
            ("log", "-p", "--no-merges", "-R", *diff, own),
            *(("diff", *reverse, *diff, old, new) for old, new in landings for reverse in ((), ("-R",))),
            # The parent's commits since, which may do or undo those changes.
            ("log", "-p", "--no-merges", "--full-diff", *diff, parent_tip, "--not", tip, "--", *files),
        )
        # Each change the branch made, as (the patch-id of doing it, of undoing it).
        changes = [(done, undone) for (done, _), (undone, _) in zip(own_done, own_undone, strict=True)]
        for forward, backward in zip(landing_patches[::2], landing_patches[1::2], strict=True):
            # Empty for a merge that changed nothing.
            if forward and backward:
                changes.append((forward[0][0], backward[0][0]))
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
        reverts: list[str] = []
        for patch, commit in since:
            if undecided := undoing.get(patch, set()) - decided:
                reverts.append(commit)
            decided |= undecided | doing.get(patch, set())
        return reverts

    def changed_files(self, base: str, rev: str) -> list[str]:
        """The files rev changes since base, from the top of the worktree, as
        commit_patch_ids takes them, even with diff.relative set."""
        files = self.git.run("diff", "--no-relative", "--name-only", "-z", base, rev).split("\0")
        return [f for f in files if f]

    def prefetch(self) -> None:
        """Look up whatever merge_state asks a remote service about, so it's
        ready when needed. Runs alongside other work, so it must be thread-safe."""

    def pull_request_link(self, branch: str) -> PullRequestLink | None:
        """branch's pull request, open or merged, if the adapter knows of one."""
        return None

    def pull_request_base(self, branch: str) -> str | None:
        """The branch that branch's pull request (see pull_request_link)
        merges into, if the adapter knows of one."""
        return None

    def ensure_available(self) -> None:
        """Fail now if whatever the adapter asks about merges can't answer,
        rather than falling back to the repository alone."""

    def before_push(
        self,
        to_push: list[str],
        stacks: Stacks,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
        opening: bool = False,
    ) -> None:
        """Called before to_push, a stack's unmerged branches, are pushed."""

    def after_push(
        self,
        pushed: list[str],
        stacks: Stacks,
        opening: bool = False,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
    ) -> None:
        """Called once pushed, a stack's unmerged branches, were pushed. opening
        says pull requests are opened for them next."""
