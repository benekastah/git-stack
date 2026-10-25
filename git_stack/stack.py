import enum
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from git_stack import events
from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.adapters.git import GitAdapter, PullRequestLink
from git_stack.adapters.github import GitHubAdapter, OpenPullRequest
from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    PARENT_KEY,
    PULLED_KEY,
    Parents,
    StackCore,
    children_map,
    descendants,
    drop_from_stack,
    ensure_no_cycle,
    stack_of,
    stack_root,
)
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import DEFAULT_REMOTE


class BranchStatus(enum.Enum):
    """Values are what `git stack info --json` prints."""

    UP_TO_DATE = "up-to-date"
    NEEDS_RESTACK = "needs-restack"
    PARENT_MISSING = "parent-missing"
    MERGED = "merged"
    MERGED_WITH_NEW_COMMITS = "merged-with-new-commits"
    REVERTED = "reverted"


@dataclass(frozen=True)
class BranchInfo:
    """A branch in info()'s listing. parent and status are None for a stack's
    root (e.g. main), which isn't tracked. pull_request is None for a root and
    for a branch without a pull request the adapter knows of."""

    name: str
    parent: str | None
    status: BranchStatus | None
    current: bool
    pull_request: PullRequestLink | None = None


@dataclass(frozen=True)
class Tracked:
    branch: str
    parent: str
    base: str


@dataclass(frozen=True)
class Untracked:
    """children are the branches still stacked on branch, which is now the
    bottom of their stack."""

    branch: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class Renamed:
    """upstream is the remote branch old was pushed to, if any. It isn't
    renamed, and new no longer tracks it."""

    old: str
    new: str
    children: tuple[str, ...]
    upstream: str | None


class AutocreateAction(enum.Enum):
    CREATE = "create"  # create the local branch from its remote branch, and track it
    TRACK = "track"
    ALREADY_TRACKED = "already-tracked"
    PARENT_DIFFERS = "parent-differs"  # already tracked, on another parent
    SKIP = "skip"


@dataclass(frozen=True)
class Autocreated:
    """What autocreate did, or in a dry run would do, for the pull request
    number of branch into parent.

    base is the commit branch is tracked as built on (CREATE and TRACK),
    tracked_parent the parent it's already tracked on instead
    (PARENT_DIFFERS), and reason why it was skipped (SKIP)."""

    branch: str
    parent: str
    number: int
    action: AutocreateAction
    base: str | None = None
    tracked_parent: str | None = None
    reason: str | None = None


class PullRequestAction(enum.Enum):
    OPEN = "open"
    EXISTS = "exists"  # it already has a pull request, open or merged
    NO_COMMITS = "no-commits"  # GitHub won't open a pull request without changes


@dataclass(frozen=True)
class PullRequestStep:
    """What prs did, or in a dry run would do, for branch, stacked on parent.

    title is the pull request's title (OPEN). number is its number, and url
    its URL, for one that was opened (OPEN, unless a dry run) or already
    existed (EXISTS, number only)."""

    branch: str
    parent: str
    action: PullRequestAction
    title: str | None = None
    number: int | None = None
    url: str | None = None


@dataclass
class SyncState:
    """A sync's options, and how far it got, as it goes through the stack.

    original is where to leave the user afterwards: their branch, or wherever
    deleting it left them. parents is the tracked branches, without the merged
    ones untracked so far. skipped are the branches left alone, whose children
    are left alone too. blocked are those that couldn't be restacked or pulled
    into because of another worktree (or are stacked on one), which a push
    can't include. pushable are the branches to push, and unpushed those that
    differ from their remote branch but won't be pushed.
    """

    original: str
    parents: Parents
    remote: str
    delete: str | None
    latest: bool
    push: bool
    skipped: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)
    pushable: list[str] = field(default_factory=list)
    unpushed: list[str] = field(default_factory=list)


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
        self.ensure_name_free_on_remotes(name)
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
        # Settle first, so a restack the user finished by hand can't overwrite
        # the parent and base written here later.
        self.ensure_no_operation_in_progress()
        self.settle_all()
        branches = list(branches)
        if not branches:
            branch = git.current_branch()
            if branch is None:
                raise StackError("not on a branch")
            branches = [branch]
        self.ensure_local_branch_parent(parent)
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
                raise StackError(
                    f"{base_arg} isn't a commit" if base_arg else f"{branch} has no history in common with {parent}"
                )
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
        return (
            git.run("merge-base", "--fork-point", parent, branch, check=False)
            or git.run("merge-base", parent, branch, check=False)
            or None
        )

    def ensure_name_free_on_remotes(self, name: str) -> None:
        """Refuse a new branch name that a remote already has a branch of, which
        is most likely someone else's: pushing ours would overwrite it."""
        for remote in self.git.remotes():
            if self.git.rev(f"refs/remotes/{remote}/{name}") is not None:
                raise StackError(
                    f"{remote}/{name} already exists, so {name} is probably someone else's "
                    f"branch; pick another name. To work on {remote}/{name} itself, "
                    f"`git switch {name}` and track it with `git stack track <parent>`"
                )

    def autocreate(self, remote: str = DEFAULT_REMOTE, dry_run: bool = False) -> list[Autocreated]:
        """Track the gh user's open pull requests on GitHub, drafts included, as
        stacks: each one's branch on the branch it merges into, creating local
        branches from remote for those that don't have one. Branches that are
        already tracked are left alone. The untracked branches they merge into
        (e.g. main) are fast-forwarded to their upstreams first.

        Returns an Autocreated for each pull request, parents before children.
        With dry_run, nothing is changed (besides fetching remote).
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        adapter = make_adapter(self, remote, fetch=True)
        if not isinstance(adapter, GitHubAdapter):
            raise StackError(
                f"autocreate finds stacks from pull requests on GitHub, but {remote} isn't a "
                f"GitHub repository, gh isn't installed, or {ADAPTER_SETTING} is git"
            )
        self.emit(events.LookingUpPullRequests())
        try:
            prs = adapter.my_open_pull_requests()
        except StackError as e:
            raise StackError(
                f"couldn't look up your pull requests on GitHub; check `gh auth status` (and, for "
                f"an organization with SSO, that gh's token is authorized for it): {e}"
            ) from e
        self.fetch(remote)
        if not dry_run:
            # Bases are worked out from the local parents, so a stale main would
            # make them too old for branches built on a newer origin/main.
            heads, parents = {pr.head for pr in prs}, self.tracked_branches()
            self.fast_forward_trunks(sorted({pr.base for pr in prs if pr.base not in heads | parents.keys()}))
        plan = AutocreatePlanner(self, prs, remote).plan_all()
        if not dry_run:
            for step in plan:
                if step.action is AutocreateAction.CREATE:
                    git.run("branch", "--quiet", "--track", step.branch, f"refs/remotes/{remote}/{step.branch}")
                elif (
                    step.action is AutocreateAction.TRACK
                    and git.config_get(step.branch, "remote") is None
                    and git.rev(f"refs/remotes/{remote}/{step.branch}") is not None
                ):
                    git.run("branch", "--quiet", f"--set-upstream-to=refs/remotes/{remote}/{step.branch}", step.branch)
                if step.base is not None:
                    git.config_set(step.branch, PARENT_KEY, step.parent)
                    git.config_set(step.branch, BASE_KEY, step.base)
        return plan

    def ensure_local_branch_parent(self, parent: str) -> None:
        """Refuse a parent that isn't a local branch: sync fast-forwards a stack's
        root, and pull requests are based on the parent, which both need a
        branch."""
        if not self.git.local_branch_exists(parent):
            raise StackError(
                f"{parent} isn't a local branch; branches can only be stacked on local "
                f"branches (e.g. branch, not origin/branch)"
            )

    def untrack(self, branches: Iterable[str] = ()) -> list[Untracked]:
        """Stop tracking branches (default: the current branch), leaving the
        branches themselves alone. Branches stacked on them stay on them.
        Returns an Untracked for each branch.

        Nothing is untracked unless every branch is tracked, so a bad argument
        doesn't leave some of them untracked.
        """
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        branches = list(branches) or [
            self.current_stacked_branch(parents, "untrack", hint="name the branches to untrack")
        ]
        for branch in branches:
            if branch not in parents:
                raise StackError(f"{branch} isn't in a stack")
        for branch in branches:
            self.forget(branch)
        # Children untracked along with their parent aren't still stacked on it.
        return [
            Untracked(
                branch,
                tuple(child for child, parent in sorted(parents.items()) if parent == branch and child not in branches),
            )
            for branch in dict.fromkeys(branches)
        ]

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
        self.ensure_name_free_on_remotes(new)
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

    def settled_branches(self) -> Parents:
        """Like tracked_branches, but first picks up the result of a restack or
        pull the user finished by hand. One that hasn't finished is left alone,
        even if it looks aborted, since it may be about to start in another
        git-stack process, so this is safe to call at any time."""
        if not self.git.operation_in_progress():
            self.settle_all(forget_unfinished=False)
        return self.tracked_branches()

    def stacked_branch(self, branch: str | None, parents: Parents) -> str:
        """Return branch, or the current branch if it's None, refusing unless it's
        in a stack."""
        if branch is None:
            return self.current_stacked_branch(parents, "", hint="name a stacked branch, or switch to one")
        if branch not in parents:
            raise StackError(f"{branch} isn't in a stack")
        return branch

    def parent(self, branch: str | None = None) -> str:
        """The branch that branch (default: the current branch) is stacked on."""
        parents = self.settled_branches()
        return parents[self.stacked_branch(branch, parents)]

    def base(self, branch: str | None = None) -> str:
        """The commit branch (default: the current branch) was last rebased onto."""
        parents = self.settled_branches()
        branch = self.stacked_branch(branch, parents)
        return self.recorded_base(branch, parents[branch])

    def show_stack(self, branch: str | None = None) -> list[str]:
        """The branches in branch's (default: the current branch's) stack,
        parent-before-child."""
        parents = self.settled_branches()
        return stack_of(self.stacked_branch(branch, parents), parents)

    def next(self) -> str:
        """Switch to the branch stacked on the current one, and return it."""
        current = self.git.current_branch()
        if current is None:
            raise StackError("not on a branch")
        children = children_map(self.settled_branches()).get(current, [])
        if not children:
            raise StackError(f"no branch is stacked on {current}")
        if len(children) > 1:
            raise StackError(
                f"several branches are stacked on {current}: {', '.join(children)}; switch to one with `git switch`"
            )
        self.switch_to(children[0])
        return children[0]

    def prev(self) -> str:
        """Switch to the current branch's parent, and return it."""
        parents = self.settled_branches()
        parent = parents[self.current_stacked_branch(parents, "", hint="there's no previous branch")]
        if not self.git.local_branch_exists(parent):
            raise StackError(f"{parent} doesn't exist")
        self.switch_to(parent)
        return parent

    def switch_to(self, branch: str) -> None:
        """Switch to branch, showing git's output (e.g. why it couldn't)."""
        if not self.git.passthrough("switch", "--quiet", branch):
            raise StackError(f"switching to {branch} failed; see git's output above")

    def info(self, offline: bool = False) -> list[BranchInfo]:
        """Every tracked stack, as a list of BranchInfo: each stack's root, then
        its branches, parent-before-child. offline checks for merges like the
        git adapter does, without asking GitHub about pull requests."""
        git = self.git
        parents = self.settled_branches()
        current = git.current_branch()
        children = children_map(parents)
        adapter = GitAdapter(self) if offline else make_adapter(self, DEFAULT_REMOTE, fetch=False)

        def status(branch: str) -> BranchStatus:
            parent = parents[branch]
            parent_tip = git.rev(parent)
            if parent_tip is None:
                return BranchStatus.PARENT_MISSING
            # Worked out before merge_state, which may wait for the adapter's
            # lookup, so the two overlap. It's wasted on merged branches.
            needs_restack = self.needs_restack(
                branch, parent, git.config_get(branch, BASE_KEY), parent_tip, latest=False
            )
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                return BranchStatus.MERGED
            if state is MergeState.MERGED_WITH_NEW_COMMITS:
                return BranchStatus.MERGED_WITH_NEW_COMMITS
            if state is MergeState.REVERTED:
                return BranchStatus.REVERTED
            return BranchStatus.NEEDS_RESTACK if needs_restack else BranchStatus.UP_TO_DATE

        # Each status can take a few git calls, and checking whether a branch on
        # main conflicts with it can take a second or more in a big repository
        # if the branch is far behind, so they're worked out in parallel, while
        # the adapter looks up pull requests (about a second on GitHub).
        with ThreadPoolExecutor() as pool:
            prefetched = pool.submit(adapter.prefetch)
            statuses = dict(zip(parents, pool.map(status, parents), strict=True))
            prefetched.result()
        result = []
        for root in sorted({stack_root(b, parents) for b in parents}):
            result.append(BranchInfo(root, None, None, root == current))
            result.extend(
                BranchInfo(b, parents[b], statuses[b], b == current, adapter.pull_request_link(b))
                for b in descendants(root, children)
            )
        return result

    def checked_adapter(self, remote: str) -> GitAdapter:
        """The adapter for sync, clean and push, which fetch and change
        branches. Fails unless it can say which branches were merged, since
        checking locally instead could untrack (or delete) a branch whose pull
        request is open, or restack one that was merged."""
        adapter = make_adapter(self, remote, fetch=True)
        try:
            adapter.ensure_available()
        except StackError as e:
            raise StackError(
                f"{e}\nNothing was changed. To only check for merges locally, run "
                f"`git -c {ADAPTER_SETTING}=git stack ...`"
            ) from e
        return adapter

    def skip_merged(self, branches: Iterable[str], parents: Parents, adapter: GitAdapter) -> list[str]:
        """Leave merged branches out of branches (parent-before-child), moving
        their children down in parents. Leave reverted ones out too, with the
        branches stacked on them, as sync does."""
        remaining = []
        skipped = set()
        for branch in branches:
            parent = parents[branch]
            if parent in skipped:
                self.emit(events.ParentSkipped(branch, parent))
                skipped.add(branch)
                continue
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                self.emit(events.SkippedMerged(branch, parent))
                drop_from_stack(branch, parents)
            elif state is MergeState.REVERTED:
                # Restacking it could drop its commits, as already in parent.
                self.emit(events.Reverted(branch, parent))
                skipped.add(branch)
            else:
                remaining.append(branch)
        return remaining

    def restack(self, onto: str | None = None, squash: bool = False, latest: bool = False) -> None:
        """Rebase every branch in the current stack that needs it onto its
        parent, bottom-up (see needs_restack).

        onto first retargets the current branch; squash first squashes each
        branch into one commit; latest moves branches on the stack's root onto
        its tip even if they don't conflict with it.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        branch = self.current_stacked_branch(parents, "restack", hint="use `git stack track` first" if onto else None)
        if onto:
            self.ensure_local_branch_parent(onto)
            ensure_no_cycle(branch, onto, parents)
            parents[branch] = onto
        adapter = make_adapter(self, DEFAULT_REMOTE, fetch=False)
        branches = self.skip_merged(stack_of(branch, parents), parents, adapter)
        if squash:
            for b in branches:
                self.squash_branch(b, parents[b])
            if git.current_branch() != branch:
                git.switch(branch)
        self.restack_all(branches, parents, latest)

    def sync(
        self, remote: str = DEFAULT_REMOTE, delete: str | None = None, latest: bool = False, push: bool = True
    ) -> list[str]:
        """Fetch, untrack the current stack's merged branches (deleting what
        delete, "local", "remote" or "both", says), restack it (see restack for
        latest), and pull in commits others pushed. Then, if push is set, push
        the branches that differ from the upstream they already have on remote.
        Branches with no such upstream are never pushed.

        Returns the branches that now differ from their remote branch, which
        with push are only those it didn't push.

        Raises StackError, after pushing the rest, if push is set and a branch
        couldn't be restacked or pulled into because it's checked out in
        another worktree, or is stacked on one that couldn't.
        """
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        original = self.current_stacked_branch(parents, "sync")
        adapter = self.checked_adapter(remote)
        self.fetch(remote)
        self.fast_forward_trunks([stack_root(original, parents)])

        state = SyncState(original, parents, remote, delete, latest, push)
        # Parent-before-child, so by the time we reach a branch its parent is
        # final: merged branches have been removed (and their children reparented
        # in `parents`), and the rest have been restacked and pulled.
        for branch in stack_of(original, parents):
            self.sync_branch(branch, adapter, state)

        self.restore_checkout(state.original)
        self.push_branches(state.pushable, parents, adapter, remote)
        if push and state.blocked:
            raise StackError(
                f"didn't push {', '.join(state.blocked)}: checked out in another worktree, or stacked on a branch "
                f"that is"
            )
        return state.unpushed

    def sync_branch(self, branch: str, adapter: GitAdapter, state: SyncState) -> None:
        """Untrack branch if it was merged, and otherwise bring it up to date (see
        sync_unmerged), unless its parent was skipped."""
        parent = state.parents[branch]
        if parent in state.skipped:
            self.emit(events.ParentSkipped(branch, parent))
            state.skipped.add(branch)
            if parent in state.blocked:
                state.blocked.append(branch)
            return
        merge_state = adapter.merge_state(branch, parent)
        if merge_state is MergeState.MERGED:
            if not self.untrack_merged(branch, state.parents, state.delete):
                state.skipped.add(branch)
            elif branch == state.original:
                state.original = self.current_checkout()
        elif merge_state is MergeState.MERGED_WITH_NEW_COMMITS:
            # Restacking would replay the merged commits onto parent.
            self.emit(events.MergedWithNewCommits(branch, parent))
            state.skipped.add(branch)
        elif merge_state is MergeState.REVERTED:
            self.emit(events.Reverted(branch, parent))
            state.skipped.add(branch)
        else:
            self.sync_unmerged(branch, parent, state)

    def sync_unmerged(self, branch: str, parent: str, state: SyncState) -> None:
        """Restack branch, which isn't merged, onto parent and pull in commits
        others pushed to it, then note whether it needs pushing."""
        git = self.git
        if git.upstream_gone(branch):
            self.emit(events.UpstreamGone(branch, parent))
        try:
            restacked = self.restack_branch(branch, parent, state.latest)
        except BrokenStackError as e:
            self.emit(events.BrokenBranch(branch, str(e)))
            restacked = False
        if not restacked:
            state.skipped.add(branch)
            # Restacking only fails without an error for another worktree.
            if git.other_worktree_of(branch) is not None:
                state.blocked.append(branch)
            return
        if not self.pull_upstream(branch, parent, state.remote):
            state.blocked.append(branch)
            return
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip and upstream_tip != git.rev(branch):
            if state.push and git.tracks_same_name(branch, state.remote):
                state.pushable.append(branch)
            else:
                state.unpushed.append(branch)

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
        adapter = self.checked_adapter(remote)
        self.fetch(remote)
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
            elif state is MergeState.REVERTED:
                self.emit(events.Reverted(branch, parent))
        self.restore_checkout(original)

    def push(self, branch: str | None = None, remote: str = DEFAULT_REMOTE, force: bool = False) -> list[str]:
        """Force-push (with lease) every unmerged branch in branch's stack (default:
        the current branch's), then let the adapter update their pull requests.
        Returns the pushed branches, which is empty if every branch looks merged.

        Refuses if a remote branch has commits the local one doesn't, unless
        force is set. Either way, the push fails if a remote branch changed
        since it was last fetched.
        """
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        if branch is None:
            branch = self.current_stacked_branch(parents, "push")
        elif branch not in parents:
            raise StackError(f"{branch} isn't in a stack")
        adapter = self.checked_adapter(remote)
        branches = self.skip_merged(stack_of(branch, parents), parents, adapter)
        return self.push_branches(branches, parents, adapter, remote, force)

    def push_branches(
        self, branches: list[str], parents: Parents, adapter: GitAdapter, remote: str, force: bool = False
    ) -> list[str]:
        """Force-push (with lease) branches, which must be unmerged, in one atomic
        push, then let the adapter update their pull requests. Returns branches.

        Checks and fails as push does.
        """
        git = self.git
        if not branches:
            return []
        self.ensure_remote_branches_ours(branches, remote)

        # Force-pushing is only safe if we've seen everything on the remote branch.
        # Refuse if we know of commits we haven't pulled (unless told to drop
        # them), and pin each lease to the remote-tracking ref we checked, so a
        # push that raced with someone else's fails instead of overwriting it.
        leases = []
        for b in branches:
            remote_tip = git.rev(f"refs/remotes/{remote}/{b}")
            if remote_tip and not force and self.unpulled_commits(b, parents[b], remote_tip):
                raise StackError(
                    f"{remote}/{b} has commits that {b} doesn't; run `git stack sync` "
                    f"to pull them in first, or push with --force to drop them"
                )
            leases.append(f"--force-with-lease={b}:{remote_tip or ''}")
        try:
            adapter.before_push(branches, parents)
        except StackError as e:
            raise StackError(
                f"updating pull requests failed, so nothing was pushed (set {ADAPTER_SETTING} "
                f"to git to stop using GitHub): {e}"
            ) from e
        # Atomic, so a branch that can't be pushed doesn't leave the rest of the
        # stack pushed without it, and its pull requests not retargeted.
        if not git.passthrough("push", "--atomic", *leases, "--set-upstream", remote, *branches):
            raise StackError(
                "push failed, so nothing was pushed; if the remote has new commits, run `git stack sync` and try again"
            )
        for b in branches:
            git.config_set(b, PULLED_KEY, git.tip(b))
        try:
            adapter.after_push(branches, parents)
        except StackError as e:
            raise StackError(
                f"pushed, but updating pull requests failed (set {ADAPTER_SETTING} to git to stop using GitHub): {e}"
            ) from e
        return branches

    def ensure_remote_branches_ours(self, branches: list[str], remote: str) -> None:
        """Refuse to push a branch whose remote branch was deleted, or is someone
        else's branch of the same name."""
        git = self.git
        for b in branches:
            # A remote branch deleted since we pushed it was most likely deleted
            # when its pull request was merged, so pushing it again would recreate it.
            if git.upstream_gone(b):
                raise StackError(
                    f"{b}'s remote branch was deleted; run `git stack sync` to stop tracking "
                    f"it if it was merged, or `git branch --unset-upstream {b}` to push it "
                    f"again"
                )
            # Someone else's branch of the same name. Overwriting it isn't what
            # --force is for, so it doesn't override this.
            if git.rev(f"refs/remotes/{remote}/{b}") is not None and not git.tracks_same_name(b, remote):
                raise StackError(
                    f"{remote}/{b} exists but isn't {b}'s upstream, so it's probably someone "
                    f"else's branch; rename yours with `git stack rename {b} <new>`. If "
                    f"{remote}/{b} is yours, run `git branch --set-upstream-to {remote}/{b} {b}` "
                    f"and `git stack sync` first"
                )

    def prs(self, remote: str = DEFAULT_REMOTE, dry_run: bool = False) -> list[PullRequestStep]:
        """Open a draft pull request into its stack parent for each unmerged
        branch in the current stack that has none, bottom-up. Its title is the
        branch's oldest commit's subject, and its body the repository's pull
        request template. Points the other open ones at their stack parents, as
        push does, in case one was opened or retargeted elsewhere since. Then
        rewrites the stack section in each open pull request's description
        that's out of date (see GitHubAdapter.update_stack_sections).

        Refuses unless every unmerged branch in the stack was pushed with `git
        stack push` and matches its remote branch, so each pull request shows
        what's local. Returns a PullRequestStep for each branch. With dry_run, nothing
        is opened, retargeted or rewritten.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        branch = self.current_stacked_branch(parents, "open pull requests for")
        adapter = make_adapter(self, remote, fetch=True)
        if not isinstance(adapter, GitHubAdapter):
            raise StackError(
                f"prs opens pull requests on GitHub, but {remote} isn't a GitHub repository, gh "
                f"isn't installed, or {ADAPTER_SETTING} is git"
            )
        adapter.ensure_available()
        branches = self.skip_merged(stack_of(branch, parents), parents, adapter)
        self.ensure_on_remote(branches, stack_root(branch, parents), remote)

        prs = adapter.pull_requests()
        template = adapter.pull_request_template() or ""
        steps: list[PullRequestStep] = []
        for b in branches:
            parent = parents[b]
            pr = prs.get(b)
            if pr is not None:
                if not dry_run:
                    adapter.retarget(b, parent)
                steps.append(PullRequestStep(b, parent, PullRequestAction.EXISTS, number=pr.number))
                continue
            base = self.recorded_base(b, parent)
            subjects = git.run("log", "--reverse", "--format=%s", f"{base}..refs/heads/{b}").splitlines()
            if not subjects:
                steps.append(PullRequestStep(b, parent, PullRequestAction.NO_COMMITS))
                continue
            title = subjects[0]
            if dry_run:
                steps.append(PullRequestStep(b, parent, PullRequestAction.OPEN, title=title))
                continue
            try:
                number, url = adapter.open_pull_request(b, parent, title, template)
            except StackError as e:
                opened = ", ".join(f"#{s.number} for {s.branch}" for s in steps if s.action is PullRequestAction.OPEN)
                raise StackError(
                    f"opening a pull request for {b} failed{f' (after opening {opened})' if opened else ''}: {e}"
                ) from e
            steps.append(PullRequestStep(b, parent, PullRequestAction.OPEN, title=title, number=number, url=url))
        if branches and not dry_run:
            try:
                adapter.update_stack_sections(branches, parents)
            except StackError as e:
                raise StackError(f"updating the stack in pull request descriptions failed: {e}") from e
        return steps

    def ensure_on_remote(self, branches: list[str], root: str, remote: str) -> None:
        """Refuse unless each of branches was pushed with `git stack push` and
        matches its remote branch, and their stack's root is on remote, so pull
        requests can be opened for them that show what's local."""
        git = self.git
        unpushed = [
            b
            for b in branches
            if not git.tracks_same_name(b, remote) or git.rev(f"refs/remotes/{remote}/{b}") != git.tip(b)
        ]
        if unpushed:
            raise StackError(
                f"these branches aren't pushed, or differ from their remote branches: "
                f"{', '.join(unpushed)}; run `git stack push` first"
            )
        if git.rev(f"refs/remotes/{remote}/{root}") is None:
            raise StackError(f"{root} isn't on {remote}, so pull requests can't merge into it")


class AutocreatePlanner:
    """Works out what autocreate should do for prs, without changing anything."""

    def __init__(self, stack: StackClient, prs: list[OpenPullRequest], remote: str) -> None:
        self.stack = stack
        self.git = stack.git
        self.prs = prs
        self.remote = remote
        # The tracked branches, and the ones planned to be tracked so far.
        self.parents = stack.tracked_branches()
        self.by_head = {pr.head: pr for pr in prs}
        self.planned: dict[str, Autocreated] = {}
        # Branches whose plan is being worked out, to catch a cycle of pull requests.
        self.visiting: set[str] = set()

    def plan_all(self) -> list[Autocreated]:
        """An Autocreated for each pull request, parents before children."""
        for pr in self.prs:
            self.plan(pr)
        return list(self.planned.values())

    def plan(self, pr: OpenPullRequest) -> Autocreated:
        """pr's plan, working it out (after its parent's) if it hasn't been yet."""
        if pr.head not in self.planned:
            self.visiting.add(pr.head)
            self.planned[pr.head] = self.plan_one(pr)
            self.visiting.discard(pr.head)
        return self.planned[pr.head]

    def plan_one(self, pr: OpenPullRequest) -> Autocreated:
        branch, parent = pr.head, pr.base
        if branch not in self.parents:
            return self.plan_untracked(pr)
        if self.parents[branch] == parent:
            return Autocreated(branch, parent, pr.number, AutocreateAction.ALREADY_TRACKED)
        return Autocreated(
            branch, parent, pr.number, AutocreateAction.PARENT_DIFFERS, tracked_parent=self.parents[branch]
        )

    def plan_untracked(self, pr: OpenPullRequest) -> Autocreated:
        """The plan for pr, whose branch isn't tracked: track it, creating it
        first if there's no local branch, unless it has to be skipped."""
        git, remote = self.git, self.remote
        branch, parent = pr.head, pr.base
        remote_ref = f"refs/remotes/{remote}/{branch}"
        local = git.local_branch_exists(branch)
        if not local and git.rev(remote_ref) is None:
            return self.skip(pr, f"{remote}/{branch} doesn't exist")
        if parent in self.by_head:
            if parent in self.visiting:
                return self.skip(pr, "the pull requests it's based on are based on it")
            if self.plan(self.by_head[parent]).action is AutocreateAction.SKIP:
                return self.skip(pr, f"{parent}, which it merges into, was skipped")
            parent_ref = parent if git.local_branch_exists(parent) else f"refs/remotes/{remote}/{parent}"
        elif git.local_branch_exists(parent):
            parent_ref = parent
        else:
            return self.skip(
                pr,
                f"{parent}, which it merges into, isn't a local branch; create it with `git switch {parent}` and rerun",
            )
        try:
            ensure_no_cycle(branch, parent, self.parents)
        except StackError as e:
            return self.skip(pr, str(e))
        base = self.stack.fork_point(parent_ref, branch if local else remote_ref)
        if base is None:
            return self.skip(pr, f"it has no history in common with {parent}")
        self.parents[branch] = parent
        action = AutocreateAction.TRACK if local else AutocreateAction.CREATE
        return Autocreated(branch, parent, pr.number, action, base=base)

    def skip(self, pr: OpenPullRequest, reason: str) -> Autocreated:
        return Autocreated(pr.head, pr.base, pr.number, AutocreateAction.SKIP, reason=reason)
