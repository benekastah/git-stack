import enum
from collections.abc import Iterable
from dataclasses import dataclass

from git_stack import events
from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.adapters.git import GitAdapter
from git_stack.adapters.merge_state import MergeState
from git_stack.core import (BASE_KEY, PARENT_KEY, Parents, StackCore, children_map, descendants,
                            drop_from_stack, ensure_no_cycle, stack_of, stack_root)
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import DEFAULT_REMOTE


class BranchStatus(enum.Enum):
    UP_TO_DATE = "up to date"
    NEEDS_RESTACK = "needs restack"
    PARENT_MISSING = "parent missing"
    MERGED = "merged"
    MERGED_WITH_NEW_COMMITS = "merged, with new commits"


@dataclass(frozen=True)
class BranchInfo:
    """A branch in info()'s listing. parent and status are None for a stack's
    root (e.g. main), which isn't tracked."""
    name: str
    parent: str | None
    status: BranchStatus | None
    current: bool


@dataclass(frozen=True)
class Tracked:
    branch: str
    parent: str
    base: str


@dataclass(frozen=True)
class Renamed:
    """upstream is the remote branch old was pushed to, if any. It isn't
    renamed, and new no longer tracks it."""
    old: str
    new: str
    children: tuple[str, ...]
    upstream: str | None


class StackClient(StackCore):
    """The operations behind each `git stack` command.

    Like StackCore's, they run git in the current directory, report progress
    and warnings through on_event, and raise StackError when they fail.
    """

    def branch(self, name: str) -> str:
        """Create name on top of the current branch and track it. Returns the parent."""
        git = self.git
        parent = git.current_branch()
        if parent is None:
            raise StackError("not on a branch")
        # Without --no-track, branch.autoSetupMerge=inherit (or always) would give
        # name its parent's upstream, which sync and push take as its own.
        git.run("switch", "--quiet", "--no-track", "--create", name)
        git.config_set(name, PARENT_KEY, parent)
        git.config_set(name, BASE_KEY, git.tip(name))
        return parent

    def track(self, parent: str, branches: Iterable[str] = (), base: str | None = None) -> list[Tracked]:
        """Track branches (default: the current branch) as a stack on parent, each
        on the one before it. base is where the first branch starts (default:
        where it forked from parent, see fork_point). Returns a Tracked for each
        branch.
        """
        git = self.git
        branches = list(branches)
        if not branches:
            branch = git.current_branch()
            if branch is None:
                raise StackError("not on a branch")
            branches = [branch]
        if git.rev(parent) is None:
            raise StackError(f"{parent} isn't a commit")
        for branch in branches:
            if not git.local_branch_exists(branch):
                raise StackError(f"{branch} isn't a local branch")
        if len(set(branches)) != len(branches) or parent in branches:
            raise StackError("each branch can only appear once")

        # Validate the whole chain before writing anything, so a bad argument
        # doesn't leave the stack half-tracked.
        parents = self.tracked_branches()
        plan = []
        base_arg = base
        for branch in branches:
            ensure_no_cycle(branch, parent, parents)
            base = git.rev(base_arg) if base_arg else self.fork_point(parent, branch)
            if base is None:
                raise StackError(f"{base_arg} isn't a commit" if base_arg
                                 else f"{branch} has no history in common with {parent}")
            if not git.is_ancestor(base, branch):
                raise StackError(f"{base_arg} isn't in {branch}'s history")
            parents[branch] = parent
            plan.append(Tracked(branch, parent, base))
            parent, base_arg = branch, None

        for tracked in plan:
            git.config_set(tracked.branch, PARENT_KEY, tracked.parent)
            git.config_set(tracked.branch, BASE_KEY, tracked.base)
        return plan

    def fork_point(self, parent: str, branch: str) -> str | None:
        """Where branch forked from parent, or None if they share no history.

        Like `git rebase`, uses parent's reflog, so if parent was rewritten after
        branch was created from it, this is the old parent commit branch was
        built on rather than their (older) merge-base.
        """
        git = self.git
        return (git.run("merge-base", "--fork-point", parent, branch, check=False)
                or git.run("merge-base", parent, branch, check=False) or None)

    def rename(self, new: str, old: str | None = None) -> Renamed:
        """Rename old (default: the current branch) to new, keeping the branches
        stacked on it. Returns a Renamed.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        old = old or git.current_branch()
        if old is None:
            raise StackError("not on a branch; name the branch to rename")
        if not git.local_branch_exists(old):
            raise StackError(f"{old} isn't a local branch")
        upstream = git.run("rev-parse", "--abbrev-ref", f"{old}@{{upstream}}", check=False) or None
        children = tuple(child for child, parent in self.tracked_branches().items() if parent == old)
        git.run("branch", "--move", old, new)
        for child in children:
            git.config_set(child, PARENT_KEY, new)
        if upstream:
            # Leaving the old upstream would make sync pull from it, and report it
            # as gone once it's deleted.
            git.run("branch", "--unset-upstream", new)
        return Renamed(old, new, children, upstream)

    def info(self) -> list[BranchInfo]:
        """Every tracked stack, as a list of BranchInfo: each stack's root, then
        its branches, parent-before-child."""
        git = self.git
        # Pick up the result of a rebase the user finished by hand, but leave the
        # pending state alone while it's still running.
        if not git.operation_in_progress():
            self.settle_all()
        parents = self.tracked_branches()
        current = git.current_branch()
        children = children_map(parents)
        adapter = make_adapter(self, DEFAULT_REMOTE)

        def status(branch: str) -> BranchStatus:
            parent = parents[branch]
            parent_tip = git.rev(parent)
            if parent_tip is None:
                return BranchStatus.PARENT_MISSING
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                return BranchStatus.MERGED
            if state is MergeState.MERGED_WITH_NEW_COMMITS:
                return BranchStatus.MERGED_WITH_NEW_COMMITS
            if git.config_get(branch, BASE_KEY) != parent_tip:
                return BranchStatus.NEEDS_RESTACK
            return BranchStatus.UP_TO_DATE

        result = []
        for root in sorted({stack_root(b, parents) for b in parents}):
            result.append(BranchInfo(root, None, None, root == current))
            result.extend(BranchInfo(b, parents[b], status(b), b == current)
                          for b in descendants(root, children))
        return result

    def skip_merged(self, branches: Iterable[str], parents: Parents, adapter: GitAdapter) -> list[str]:
        """Leave merged branches out of branches, moving their children down in parents."""
        remaining = []
        for branch in branches:
            parent = parents[branch]
            if adapter.merge_state(branch, parent) is MergeState.MERGED:
                self.emit(events.SkippedMerged(branch, parent))
                drop_from_stack(branch, parents)
            else:
                remaining.append(branch)
        return remaining

    def restack(self, onto: str | None = None, squash: bool = False) -> None:
        """Rebase every branch in the current stack onto its parent, bottom-up.

        onto first retargets the current branch; squash first squashes each
        branch into one commit.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        branch = self.current_stacked_branch(
            parents, "restack", hint="use `git stack track` first" if onto else None)
        if onto:
            if git.rev(onto) is None:
                raise StackError(f"{onto} isn't a commit")
            ensure_no_cycle(branch, onto, parents)
            parents[branch] = onto
        branches = self.skip_merged(stack_of(branch, parents), parents, make_adapter(self, DEFAULT_REMOTE))
        if squash:
            for b in branches:
                self.squash_branch(b, parents[b])
            if git.current_branch() != branch:
                git.switch(branch)
        self.restack_all(branches, parents)

    def sync(self, remote: str = DEFAULT_REMOTE, delete: str | None = None) -> list[str]:
        """Fetch, untrack the current stack's merged branches (deleting what
        delete, "local", "remote" or "both", says), restack it, and pull in
        commits others pushed.

        Returns the branches that now differ from their remote branch.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        # Where to leave the user afterwards: their branch, or wherever deleting
        # it left them.
        original = self.current_stacked_branch(parents, "sync")
        adapter = make_adapter(self, remote)
        git.fetch(remote)
        self.fast_forward_trunks([stack_root(original, parents)])

        # Parent-before-child, so by the time we reach a branch its parent is
        # final: merged branches have been removed (and their children reparented
        # in `parents`), and the rest have been restacked and pulled.
        order = stack_of(original, parents)
        skipped = set()
        unpushed = []
        for branch in order:
            parent = parents[branch]
            if parent in skipped:
                self.emit(events.ParentSkipped(branch, parent))
                skipped.add(branch)
                continue
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                if not self.untrack_merged(branch, parents, delete):
                    skipped.add(branch)
                elif branch == original:
                    original = self.current_checkout()
                continue
            if state is MergeState.MERGED_WITH_NEW_COMMITS:
                # Restacking would replay the merged commits onto parent.
                self.emit(events.MergedWithNewCommits(branch, parent))
                skipped.add(branch)
                continue
            if git.upstream_gone(branch):
                self.emit(events.UpstreamGone(branch, parent))
            try:
                self.restack_branch(branch, parent)
            except BrokenStackError as e:
                self.emit(events.BrokenBranch(branch, str(e)))
                skipped.add(branch)
                continue
            self.pull_upstream(branch, parent)
            upstream_tip = git.rev(f"{branch}@{{upstream}}")
            if upstream_tip and upstream_tip != git.rev(branch):
                unpushed.append(branch)

        self.restore_checkout(original)
        return unpushed

    def clean(self, remote: str = DEFAULT_REMOTE, delete: str | None = None) -> None:
        """Fetch and untrack merged branches in every stack, deleting what delete
        says, without restacking anything."""
        git = self.git
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        # Where to leave the user afterwards: their branch (or the commit they had
        # detached at), or wherever deleting their branch left them.
        original = self.current_checkout()
        adapter = make_adapter(self, remote)
        git.fetch(remote)
        parents = self.tracked_branches()
        roots = sorted({stack_root(b, parents) for b in parents})
        self.fast_forward_trunks(roots)

        # Parent-before-child, so a branch whose parent was merged is checked
        # against its new parent.
        order = [b for root in roots for b in descendants(root, children_map(parents))]
        for branch in order:
            parent = parents[branch]
            if git.rev(parent) is None:
                self.emit(events.ParentMissing(branch, parent))
                continue
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                if self.untrack_merged(branch, parents, delete) and branch == original:
                    original = self.current_checkout()
            elif state is MergeState.MERGED_WITH_NEW_COMMITS:
                self.emit(events.MergedWithNewCommits(branch, parent))
        self.restore_checkout(original)

    def push(self, branch: str | None = None, remote: str = DEFAULT_REMOTE, force: bool = False) -> list[str]:
        """Force-push (with lease) every unmerged branch in branch's stack (default:
        the current branch's), then let the adapter update their pull requests.
        Returns the pushed branches, which is empty if every branch looks merged.

        Refuses if a remote branch has commits the local one doesn't, unless
        force is set. Either way, the push fails if a remote branch changed
        since it was last fetched.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        if branch is None:
            branch = self.current_stacked_branch(parents, "push")
        elif branch not in parents:
            raise StackError(f"{branch} isn't in a stack")
        adapter = make_adapter(self, remote)
        branches = self.skip_merged(stack_of(branch, parents), parents, adapter)
        if not branches:
            return []
        # A remote branch deleted since we pushed it was most likely deleted when
        # its pull request was merged, so pushing it again would recreate it.
        for b in branches:
            if git.upstream_gone(b):
                raise StackError(f"{b}'s remote branch was deleted; run `git stack sync` to stop tracking "
                                 f"it if it was merged, or `git branch --unset-upstream {b}` to push it "
                                 f"again")

        # Force-pushing is only safe if we've seen everything on the remote branch.
        # Refuse if we know of commits we haven't pulled (unless told to drop
        # them), and pin each lease to the remote-tracking ref we checked, so a
        # push that raced with someone else's fails instead of overwriting it.
        leases = []
        for b in branches:
            remote_tip = git.rev(f"refs/remotes/{remote}/{b}")
            if remote_tip and not force and self.unpulled_commits(b, parents[b], remote_tip):
                raise StackError(f"{remote}/{b} has commits that {b} doesn't; run `git stack sync` "
                                 f"to pull them in first, or push with --force to drop them")
            leases.append(f"--force-with-lease={b}:{remote_tip or ''}")
        if not git.passthrough("push", *leases, "--set-upstream", remote, *branches):
            raise StackError("push failed; if the remote has new commits, run `git stack sync` and try again")
        try:
            adapter.after_push(branches, parents)
        except StackError as e:
            raise StackError(f"pushed, but updating pull requests failed (set {ADAPTER_SETTING} to git "
                             f"to stop using GitHub): {e}")
        return branches
