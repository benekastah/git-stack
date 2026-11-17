import enum
import functools
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Concatenate

from git_stack import events
from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.adapters.git import GitAdapter, PullRequestLink
from git_stack.adapters.github import GitHubAdapter, GitHubState, OpenPullRequest, check_github_login
from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    PULLED_KEY,
    PULLED_UPSTREAM_KEY,
    TRACK_UPSTREAM_KEY,
    Delete,
    StackCore,
    SyncCommand,
)
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import DEFAULT_REMOTE, SWITCH
from git_stack.json_fields import json_name
from git_stack.stacks import Ownership, Stacks


def exclusive[Client: StackCore, **Params, Result](
    method: Callable[Concatenate[Client, Params], Result],
) -> Callable[Concatenate[Client, Params], Result]:
    """Run method holding the repository's git-stack lock (see
    StackCore.exclusive), as every command that changes stack metadata does,
    and with a snapshot of its refs (see GitClient.snapshot), taken once it
    has the lock."""

    @functools.wraps(method)
    def locked(client: Client, /, *args: Params.args, **kwargs: Params.kwargs) -> Result:
        with client.exclusive(), client.git.snapshot():
            return method(client, *args, **kwargs)

    return locked


def snapshot[Client: StackCore, **Params, Result](
    method: Callable[Concatenate[Client, Params], Result],
) -> Callable[Concatenate[Client, Params], Result]:
    """Run method, a command that only reads, with a snapshot of the
    repository's refs (see GitClient.snapshot)."""

    @functools.wraps(method)
    def snapshotted(client: Client, /, *args: Params.args, **kwargs: Params.kwargs) -> Result:
        with client.git.snapshot():
            return method(client, *args, **kwargs)

    return snapshotted


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
    for a branch without a pull request the adapter knows of. group is the
    group of a branch on a stack's root, if another branch on a stack's root (this one or
    another) is in it too (see GROUP_KEY)."""

    name: str
    parent: str | None
    status: BranchStatus | None
    current: bool
    pull_request: PullRequestLink | None = field(default=None, metadata=json_name("pr"))
    group: str | None = None


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


@dataclass(frozen=True)
class Unreverted:
    """What unrevert did: created branch on parent, with commits reverting
    reverts (newest first), and moved children, the branches it was given to
    stack on it, onto it."""

    branch: str
    parent: str
    reverts: tuple[str, ...]
    children: tuple[str, ...]


def not_github(remote: str) -> str:
    """Why prs can't open pull requests, without the GitHub adapter."""
    return f"{remote} isn't a GitHub repository, gh isn't installed, or {ADAPTER_SETTING} is git"


def ensure_github(adapter: GitAdapter, remote: str) -> GitHubAdapter:
    """adapter, failing unless it's the GitHub adapter, which prs needs."""
    if not isinstance(adapter, GitHubAdapter):
        raise StackError(f"prs opens pull requests on GitHub, but {not_github(remote)}")
    return adapter


def lookup_failed(error: StackError) -> StackError:
    """The error for import when GitHub couldn't be asked for pull requests."""
    return StackError(
        f"couldn't look up pull requests on GitHub; check `gh auth status` (and, for "
        f"an organization with SSO, that gh's token is authorized for it): {error}"
    )


class ImportAction(enum.Enum):
    CREATE = "create"  # create the local branch from its remote branch, and track it
    CREATE_ROOT = "create-root"  # create a shared trunk without tracking it in a stack
    TRACK = "track"
    ALREADY_TRACKED = "already-tracked"
    PARENT_DIFFERS = "parent-differs"  # already tracked, on another parent
    SKIP = "skip"


@dataclass(frozen=True)
class ImportStep:
    """What import did, or in a dry run would do, for the pull request
    number of branch into parent.

    base is the commit branch is tracked as built on (CREATE and TRACK),
    tracked_parent the parent it's already tracked on instead
    (PARENT_DIFFERS), and reason why it was skipped (SKIP). other_authors is
    the pull request's author, if it isn't the gh user's (CREATE and TRACK,
    which then need force)."""

    branch: str
    parent: str
    number: int
    action: ImportAction
    base: str | None = None
    tracked_parent: str | None = None
    reason: str | None = None
    other_authors: list[str] = field(default_factory=list)


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


@dataclass(frozen=True)
class PrsResult:
    """What prs did: a PullRequestStep for each branch it looked at, and the
    stacks it looked at, as info() lists them, with the pull requests it
    opened."""

    steps: list[PullRequestStep]
    branches: list[BranchInfo] = field(metadata=json_name("stacks"))


@dataclass(frozen=True)
class ImportStepWithoutPr:
    """What import did, or in a dry run would do, for branch: a branch on
    the remote, without a pull request or a local branch, that one of the
    user's pull requests is built on. Its parent is found from history (see
    ImportPlanner.find_parent).

    action is CREATE or SKIP. base is the commit branch is tracked as built
    on (CREATE), and reason why it was skipped (SKIP). other_authors are the
    email addresses of its commits' authors besides the user (CREATE, which
    then needs force)."""

    branch: str
    parent: str | None
    action: ImportAction
    base: str | None = None
    reason: str | None = None
    other_authors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImportResult:
    """What import did: an ImportStep for each pull request, and the
    stacks of the branches it tracked or found tracked, as info() lists them
    (none in a dry run, which tracks nothing). without_prs has what it did for
    the branches without a pull request that pull requests are built on,
    parents before children. remote is the remote it imported from."""

    steps: list[ImportStep]
    branches: list[BranchInfo] = field(metadata=json_name("stacks"))
    without_prs: list[ImportStepWithoutPr] = field(default_factory=list)
    remote: str = DEFAULT_REMOTE


@dataclass(frozen=True)
class SyncResult:
    """What sync did: the branches that now differ from their remote branch,
    and the stacks it synced, as info() lists them."""

    unpushed: list[str]
    branches: list[BranchInfo] = field(metadata=json_name("stacks"))


@dataclass(frozen=True)
class PushResult:
    """What push did: the branches it pushed, and the stacks it pushed, as
    info() lists them. With prs, steps is what opening pull requests did, as
    for PrsResult, or None without it, or if they couldn't be opened: then
    error says why, and the command fails, though it pushed (see cli.main)."""

    pushed: list[str]
    branches: list[BranchInfo] = field(metadata=json_name("stacks"))
    steps: list[PullRequestStep] | None = None
    error: str | None = None


@dataclass
class SyncState:
    """A sync's options, and how far it got, as it goes through the stack.

    original is where to leave the user afterwards: their branch, or wherever
    deleting it left them. stacks is the tracked branches, without the merged
    ones untracked so far. skipped are the branches left alone, whose children
    are left alone too. blocked are those that couldn't be restacked or pulled
    into because of another worktree (or are stacked on one), which a push
    can't include. pushable are the branches to push, and unpushed those that
    differ from their remote branch but won't be pushed. ignore_reverts are
    the branches whose reverts to ignore (see StackClient.sync). command is
    sync or pull, for messages saying what to rerun.
    """

    original: str
    stacks: Stacks
    remote: str
    delete: Delete | None
    latest: bool
    push: bool
    ignore_reverts: frozenset[str] = frozenset()
    pull_tracked_upstream: bool = False
    rewritten_upstream: bool = False
    scope: frozenset[str] = frozenset()
    protected: frozenset[str] = frozenset()
    command: SyncCommand = SyncCommand.SYNC
    skipped: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)
    pushable: list[str] = field(default_factory=list)
    unpushed: list[str] = field(default_factory=list)


class StackClient(StackCore):
    """The operations behind each `git stack` command.

    Like StackCore's, they run git in the current directory, report progress
    and warnings through on_event, and raise StackError when they fail.
    """

    @snapshot
    def get_ownership(self, branches: Sequence[str] = (), groups: Sequence[str] = ()) -> tuple[Ownership, list[str]]:
        """Return the selected branches' ownership, shared if any belongs to a
        shared stack, and the selected branches."""
        stacks = self.stacks()
        selected = self.selected_stacks(branches, groups, stacks, "inspect")
        ownership = Ownership.SHARED if any(stacks.is_shared(b) for b in selected) else Ownership.OWNED
        return ownership, selected

    @exclusive
    def set_ownership(
        self, ownership: Ownership, branches: Sequence[str] = (), groups: Sequence[str] = ()
    ) -> list[str]:
        """Mark the selected stacks as owned or shared."""
        self.ensure_no_operation_in_progress()
        stacks = self.stacks()
        selected = self.selected_stacks(branches, groups, stacks, "mark")
        for branch in selected:
            stacks.set_ownership(branch, ownership)
        self.save(stacks)
        return selected

    def ensure_owned(self, branches: Iterable[str], stacks: Stacks, allow_shared: bool, command: str) -> None:
        """Refuse an operation on tracked branches in a shared stack without an
        explicit override."""
        if not allow_shared:
            shared = [b for b in branches if stacks.is_shared(b)]
            if shared:
                raise StackError(
                    f"{', '.join(shared)} belong to a shared stack; "
                    f"rerun `git stack {command} --allow-shared` with the same arguments to proceed"
                )

    @exclusive
    def branch(self, name: str, group: str | None = None) -> str:
        """Create name on top of the current branch and track it, putting its
        stack in group, if given. Returns the parent.

        Refuses, creating nothing, if the stack is in another group already.
        """
        git = self.git
        parent = git.current_branch()
        if parent is None:
            raise StackError("not on a branch")
        if not git.local_branch_exists(parent):
            # git would switch to name without creating it, leaving it
            # tracked on parent with nothing to stack on.
            raise StackError(f"{parent} has no commits yet, so there's nothing to build {name} on; commit to it first")
        self.ensure_name_free_on_remotes(name)
        # The branch at the bottom of name's stack, which is what's in a group,
        # or None if that's name itself.
        bottom = None
        stacks = self.stacks()
        if group is not None:
            self.ensure_group_name(group)
            if parent in stacks:
                bottom = stacks.bottom(parent)
                current = stacks.group(bottom)
                if current is not None and current != group:
                    raise StackError(
                        f"{parent}'s stack is in group {current} already; run `git stack group add --move {group}` "
                        f"from it to move it, or `git stack group rename {group}` to rename the group"
                    )
        # Without --no-track, branch.autoSetupMerge=inherit (or always) would give
        # name its parent's upstream, which sync and push take as its own.
        git.run(*SWITCH, "--no-track", "--create", name)
        stacks.reparent(name, parent)
        if group is not None:
            stacks.set_group(bottom or name, group)
        self.save(stacks)
        git.config_set(name, BASE_KEY, git.tip(name))
        return parent

    @exclusive
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

        # Work out the whole chain before writing anything, so a bad argument
        # doesn't leave the stack half-tracked.
        stacks = self.stacks()
        plan: list[Tracked] = []
        explicit_base = base is not None
        base_arg = base
        for branch in branches:
            stacks.ensure_no_cycle(branch, parent)
            base = git.rev(base_arg) if base_arg else self.fork_point(parent, branch)
            if base is None:
                raise StackError(
                    f"{base_arg} isn't a commit" if base_arg else f"{branch} has no history in common with {parent}"
                )
            if not git.is_ancestor(base, f"refs/heads/{branch}"):
                raise StackError(f"{base_arg} isn't in {branch}'s history")
            stacks.reparent(branch, parent)
            plan.append(Tracked(branch, parent, base))
            parent, base_arg = branch, None

        self.save(stacks)
        for tracked in plan:
            git.config_set(tracked.branch, BASE_KEY, tracked.base)
        self.remember_tracked_upstream(plan[0].branch, explicit_base)
        return plan

    def remember_tracked_upstream(self, branch: str, explicit_base: bool) -> None:
        """Remember the same-named upstream present at an explicit re-track."""
        if not explicit_base:
            return
        git = self.git
        remote = git.remote_branch(branch)
        if remote and git.tracks_same_name(branch, remote[0]):
            git.config_set(branch, TRACK_UPSTREAM_KEY, remote[2])
        else:
            git.config_unset(branch, TRACK_UPSTREAM_KEY)

    def fork_point(self, parent: str, branch: str) -> str | None:
        """Where branch forked from parent, or None if they share no history.

        Like `git rebase`, uses parent's reflog, so if parent was rewritten after
        branch was created from it, this is the old parent commit branch was
        built on rather than their (older) merge-base.
        """
        git = self.git
        parent_ref = parent if parent.startswith("refs/") else f"refs/heads/{parent}"
        branch_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        return (
            git.run("merge-base", "--fork-point", parent_ref, branch_ref, check=False)
            or git.run("merge-base", parent_ref, branch_ref, check=False)
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

    @exclusive
    def import_stacks(
        self,
        remote: str | None = None,
        dry_run: bool = False,
        force: bool = False,
        author: str | None = None,
        branches: Sequence[str] = (),
        prs: Sequence[str] = (),
    ) -> ImportResult:
        """Track open pull requests on GitHub, drafts included, as stacks: each
        one's branch on the branch it merges into, creating local branches
        from remote for those that don't have one. Branches that are already
        tracked are left alone. The untracked branches the ones it creates or
        tracks go on (e.g. main) are fast-forwarded to their upstreams.

        The pull requests are author's (a GitHub login), or the gh user's.
        branches and prs (pull request numbers or URLs, whoever's they are)
        narrow them to the stack trees containing those. The pull requests of
        anyone but the gh user are only tracked with force.

        A pull request may merge into a branch that's only on remote, without a
        pull request: that branch is created and tracked too, on the branch
        its history says it's built on. If it has commits by anyone but the
        user, only with force.

        With dry_run, nothing is changed (besides fetching remote), and the
        steps are what force would do.
        """
        if author is not None:
            check_github_login(author)
        self.ensure_no_operation_in_progress()
        self.settle_all()
        remote = remote or self.fallback_remote()
        adapter = make_adapter(self, remote, fetch=True)
        if not isinstance(adapter, GitHubAdapter):
            raise StackError(
                f"import finds stacks from pull requests on GitHub, but {remote} isn't a "
                f"GitHub repository, gh isn't installed, or {ADAPTER_SETTING} is git"
            )
        numbers = sorted({adapter.pull_request_number(pr) for pr in prs})
        # The user's own pull requests, unless only pull requests were named.
        mine = bool(branches) or not numbers
        open_prs = self.look_up_open_pull_requests(adapter, author, numbers, mine)
        self.fetch(remote)
        planner = ImportPlanner(
            self, open_prs, remote, adapter.default_branch, adapter.viewer, force or dry_run, adapter
        )
        plan, without_prs = planner.plan_all(), list(planner.without_prs.values())
        if branches or numbers:
            plan, without_prs = planner.select(branches, numbers, plan, without_prs)
        if dry_run:
            return ImportResult(plan, [], without_prs, remote)
        stacks = self.stacks()
        self.fast_forward_import_trunks(plan, without_prs, stacks)
        previous_ownership = {b: stacks[b].ownership for b in stacks}
        self.apply_import(plan, without_prs, remote, stacks)
        self.classify_imported_stacks(plan, without_prs, open_prs, adapter.viewer, previous_ownership, stacks)
        shown = [s.branch for s in plan] + [s.branch for s in without_prs]
        return ImportResult(plan, self.stack_infos(shown, adapter), without_prs, remote)

    def classify_imported_stacks(
        self,
        plan: list[ImportStep],
        without_prs: list[ImportStepWithoutPr],
        prs: list[OpenPullRequest],
        viewer: str | None,
        previous_ownership: dict[str, Ownership | None],
        stacks: Stacks,
    ) -> None:
        """Mark newly imported stacks shared when their contributors aren't solely the user."""
        git = self.git
        email = (git.author_email() or "").lower()
        authors = {pr.number: pr.author for pr in prs}
        all_steps: list[ImportStep | ImportStepWithoutPr] = [*plan, *without_prs]
        candidates = [
            step
            for step in all_steps
            if step.action
            in (ImportAction.CREATE, ImportAction.TRACK, ImportAction.ALREADY_TRACKED, ImportAction.PARENT_DIFFERS)
        ]
        shared: set[str] = set()
        imported: set[str] = set()
        for step in candidates:
            members = stacks.stack_of(step.branch, groups=False)
            imported.update(members)
            if any(previous_ownership.get(b) is Ownership.SHARED for b in members):
                shared.update(members)
                continue
            # An explicit choice takes precedence over automatic classification.
            if any(previous_ownership.get(b) is Ownership.OWNED for b in members):
                continue
            base = step.base or git.config_get(step.branch, BASE_KEY)
            commit_authors = (
                set(git.run("log", "--format=%ae", f"{base}..refs/heads/{step.branch}").lower().splitlines())
                if base
                else set[str]()
            )
            pr_is_other = isinstance(step, ImportStep) and (not viewer or authors.get(step.number) != viewer)
            if not email or not commit_authors or commit_authors != {email} or pr_is_other:
                shared.update(members)
        for branch in imported:
            if branch in shared:
                stacks.set_ownership(branch, Ownership.SHARED)
            else:
                stacks.set_ownership(branch, previous_ownership.get(branch))
        self.save(stacks)

    def look_up_open_pull_requests(
        self, adapter: GitHubAdapter, author: str | None, numbers: Sequence[int], mine: bool
    ) -> list[OpenPullRequest]:
        """The open pull requests numbers, those of their authors and of
        author, and (if author is None and mine) of the gh user, saying whose
        it looks up as it goes. Those from forks are left out."""
        if numbers:
            self.emit(events.LookingUpNamedPullRequests(tuple(numbers)))
        try:
            named = {number: adapter.open_pull_request_by_number(number) for number in numbers}
        except StackError as e:
            raise lookup_failed(e) from e
        if closed := [number for number, pr in named.items() if pr is None]:
            raise StackError(f"#{closed[0]} isn't an open pull request")
        # A deleted account's pull requests can't be searched for, so those
        # named are kept as they were looked up.
        prs = {pr.number: pr for pr in named.values() if pr is not None and not pr.fork}
        authors = {author} if author else ({"@me"} if mine else set[str]())
        # The lookups said who the gh user is, whose are searched for as @me.
        authors |= {
            "@me" if pr.author == adapter.viewer else pr.author for pr in named.values() if pr is not None and pr.author
        }
        try:
            for login in sorted(authors):
                self.emit(events.LookingUpPullRequests(None if login == "@me" else login))
                prs |= {pr.number: pr for pr in adapter.open_pull_requests(login)}
        except StackError as e:
            raise lookup_failed(e) from e
        return list(prs.values())

    def fast_forward_import_trunks(
        self, plan: list[ImportStep], without_prs: list[ImportStepWithoutPr], stacks: Stacks
    ) -> None:
        """Fast-forward the untracked branches (e.g. main) that the branches
        import creates or tracks go on, as their bases were worked out for
        (see ImportPlanner.trunk_ref), and no others."""
        imported = [s for s in plan if s.action in (ImportAction.CREATE, ImportAction.TRACK)]
        imported += [s for s in without_prs if s.action is ImportAction.CREATE]
        heads = {s.branch for s in plan} | {s.branch for s in without_prs}
        trunks = {s.parent for s in imported if s.parent is not None} - heads - set(stacks)
        self.fast_forward_trunks(sorted(trunks))

    def apply_import(
        self, plan: list[ImportStep], without_prs: list[ImportStepWithoutPr], remote: str, stacks: Stacks
    ) -> None:
        """Create and track the branches import planned to, in stacks and git
        config, the branches without a pull request first. Each one it tracks
        is shared, with the branches on it."""
        git = self.git
        self.apply_import_without_prs(without_prs, remote, stacks)
        for step in plan:
            if step.action is ImportAction.CREATE:
                git.run("branch", "--quiet", "--track", step.branch, f"refs/remotes/{remote}/{step.branch}")
            elif (
                step.action is ImportAction.TRACK
                and git.config_get(step.branch, "remote") is None
                and git.rev(f"refs/remotes/{remote}/{step.branch}") is not None
            ):
                git.run("branch", "--quiet", f"--set-upstream-to=refs/remotes/{remote}/{step.branch}", step.branch)
            if step.base is not None:
                stacks.reparent(step.branch, step.parent)
                if stacks[step.branch].ownership is None:
                    stacks.share(step.branch)
            elif step.action is not ImportAction.SKIP and stacks[step.branch].ownership is None:
                # Already tracked: its stack is left as it is.
                stacks.set_ownership(step.branch, Ownership.SHARED)
            self.save(stacks)
            if step.base is not None:
                git.config_set(step.branch, BASE_KEY, step.base)

    def apply_import_without_prs(self, without_prs: list[ImportStepWithoutPr], remote: str, stacks: Stacks) -> None:
        """Create and track the branches without a pull request import planned to."""
        git = self.git
        for without_pr in without_prs:
            if without_pr.action is ImportAction.CREATE_ROOT:
                git.run("branch", "--quiet", "--track", without_pr.branch, f"refs/remotes/{remote}/{without_pr.branch}")
            if without_pr.action is ImportAction.CREATE and without_pr.parent and without_pr.base:
                branch = without_pr.branch
                git.run("branch", "--quiet", "--track", branch, f"refs/remotes/{remote}/{branch}")
                stacks.reparent(branch, without_pr.parent)
                stacks.share(branch)
                self.save(stacks)
                git.config_set(branch, BASE_KEY, without_pr.base)

    def ensure_local_branch_parent(self, parent: str) -> None:
        """Refuse a parent that isn't a local branch: sync fast-forwards a stack's
        root, and pull requests are based on the parent, which both need a
        branch."""
        if not self.git.local_branch_exists(parent):
            raise StackError(
                f"{parent} isn't a local branch; branches can only be stacked on local "
                f"branches (e.g. branch, not origin/branch)"
            )

    @exclusive
    def group_add(
        self, name: str, branches: Sequence[str] = (), groups: Sequence[str] = (), move: bool = False
    ) -> list[str]:
        """Put the stacks of the branches that branches and groups name (see
        named; default: the current branch), each by its bottom branch, in the
        group name. Returns those bottom branches.

        Refuses, changing nothing, if any of them is in another group, unless
        move is set, in which case they leave it. A glob can easily match a
        stack in another group, and moving it silently splits that group.
        """
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        self.ensure_group_name(name)
        bottoms = self.selected_bottoms(branches, groups, stacks, "group")
        if not move:
            moves = [
                f"{bottom}'s stack, from group {group}"
                for bottom in bottoms
                if (group := stacks.group(bottom)) not in (None, name)
            ]
            if moves:
                raise StackError(
                    "some of those stacks are in another group already; pass --move to move them into "
                    f"{name}:\n" + "\n".join(f"  {m}" for m in moves)
                )
        for bottom in bottoms:
            stacks.set_group(bottom, name)
        self.save(stacks)
        return bottoms

    @exclusive
    def group_remove(self, branches: Sequence[str] = (), groups: Sequence[str] = ()) -> list[tuple[str, str]]:
        """Take the stacks of the branches that branches and groups name (see
        named; default: the current branch), each by its bottom branch, out of
        their groups. Returns (bottom branch, its old group) for those that
        were in one."""
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        removed: list[tuple[str, str]] = []
        for bottom in self.selected_bottoms(branches, groups, stacks, "ungroup"):
            if (group := stacks.group(bottom)) is not None:
                stacks.set_group(bottom, None)
                removed.append((bottom, group))
        self.save(stacks)
        return removed

    @exclusive
    def group_rename(self, new: str, old: str | None = None) -> tuple[str, list[str]]:
        """Rename the group old (default: the current branch's stack's) to new.
        Returns old and the branches that were in it. Refuses if a branch is in
        new already, so groups aren't merged by mistake."""
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        if old is None:
            current = self.current_stacked_branch(stacks, "rename", hint="name the group to rename")
            bottom = stacks.bottom(current)
            old = stacks.group(bottom)
            if old is None:
                raise StackError(f"{bottom}'s stack isn't in a group; name the group to rename")
        self.ensure_group_name(new)
        groups = stacks.groups()
        if new in groups.values():
            raise StackError(
                f"group {new} already exists; to merge {old} into it, run `git stack group add --move {new} -g {old}`"
            )
        members = [b for b, g in groups.items() if g == old]
        if not members:
            raise StackError(f"there's no group {old}")
        for branch in members:
            stacks.set_group(branch, new)
        self.save(stacks)
        return old, members

    def ensure_group_name(self, name: str) -> None:
        """Refuse a group name that's empty or has spaces in it."""
        if not name or any(c.isspace() for c in name):
            raise StackError(f"{name!r} can't be a group name: it can't be empty, or have spaces in it")

    @exclusive
    def untrack(self, branches: Iterable[str] = ()) -> list[Untracked]:
        """Stop tracking branches (default: the current branch), each a branch
        or a glob (see matching), leaving the branches themselves alone.
        Branches stacked on them stay on them. Returns an Untracked for each
        branch.

        Nothing is untracked unless every branch is tracked, so a bad argument
        doesn't leave some of them untracked.
        """
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        selectors = list(branches) or [
            self.current_stacked_branch(stacks, "untrack", hint="name the branches to untrack")
        ]
        branches = [branch for selector in selectors for branch in self.matching(selector, stacks)]
        # Children untracked along with their parent aren't still stacked on it.
        untracked = [
            Untracked(branch, tuple(child for child in stacks.children(branch) if child not in branches))
            for branch in dict.fromkeys(branches)
        ]
        stacks.untrack(branches)
        self.save(stacks)
        return untracked

    @exclusive
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
        git.run("branch", "--move", old, new)
        # The move took old's settings, and so its place in its stack, with
        # it, but the branches stacked on it still name old.
        stacks = self.stacks()
        children = tuple(stacks.children(old))
        for child in children:
            stacks.reparent(child, new)
        self.save(stacks)
        if upstream:
            # Leaving the old upstream would make sync pull from it, and report it
            # as gone once it's deleted.
            git.run("branch", "--unset-upstream", new)
        return Renamed(old, new, children, upstream)

    @exclusive
    def unrevert(
        self,
        name: str,
        reverts: Sequence[str],
        from_branch: str | None = None,
        stack: Sequence[str] = (),
        remote: str | None = None,
        allow_shared: bool = False,
    ) -> Unreverted:
        """Create name on from_branch (default: the root of stack's first
        branch, or else remote's default branch), track it, and revert reverts
        there, newest first, bringing back a merged branch they reverted. Move
        the branches in stack onto it, keeping their bases, and restack them
        onto it. A merged, reverted branch they leave with nothing stacked on
        it is untracked. Returns an Unreverted.

        The branches are moved before the reverts are, so if one stops on a
        conflict, resolving it with `git revert --continue` and running
        `git stack restack` finishes the job.
        """
        git = self.git
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        stacks = self.stacks()
        if git.local_branch_exists(name):
            raise StackError(f"{name} already exists; pick another name")
        self.ensure_name_free_on_remotes(name)
        if len(set(stack)) != len(stack):
            raise StackError("each branch can only appear once")
        for branch in stack:
            if branch not in stacks:
                raise StackError(f"{branch} isn't in a stack")
        remote = remote or self.default_remote(stack, stacks)
        parent = from_branch or (stacks.root(stack[0]) if stack else self.default_branch(remote))
        affected = (b for branch in (*stack, parent) if stack and branch in stacks for b in stacks.stack_of(branch))
        self.ensure_owned(affected, stacks, allow_shared, "unrevert")
        self.ensure_local_branch_parent(parent)
        planned = stacks.copy()
        planned.reparent(name, parent)
        for branch in stack:
            planned.ensure_no_cycle(branch, name)
        commits = self.reverts_to_revert(reverts, parent)
        # The merged branches the stack is leaving, to untrack once nothing is
        # stacked on them, which they'd otherwise be left as forever.
        adapter = make_adapter(self, remote, fetch=False)
        left = [
            old
            for old in dict.fromkeys(stacks.parent(b) for b in stack)
            if old in stacks
            and all(b in stack for b in stacks.children(old))
            and adapter.merge_state(old, stacks.parent(old)) is MergeState.REVERTED
        ]
        # So name, if it's at the bottom of a stack, stays in the group the
        # stack was in, with the other stacks in it.
        bottoms = [] if parent in stacks else [stacks.bottom(b) for b in stack]
        group = next((g for b in bottoms if (g := stacks.group(b))), None)

        git.run("branch", "--quiet", "--no-track", name, f"refs/heads/{parent}")
        stacks.reparent(name, parent)
        if group is not None:
            stacks.set_group(name, group)
        for branch in stack:
            stacks.reparent(branch, name)
        self.save(stacks)
        git.config_set(name, BASE_KEY, git.tip(name))
        for old in left:
            self.remove_merged(old, stacks, delete=False)
        git.switch(name)
        if not git.passthrough("revert", "--no-edit", *commits):
            moved = f" to move {', '.join(stack)} onto {name}" if stack else ""
            raise StackError(
                f"unreverting stopped; see git's output above. Resolve any conflicts and run "
                f"`git revert --continue`, then `git stack restack`{moved}"
            )
        self.restack_all(stacks.descendants(name), stacks)
        return Unreverted(name, parent, tuple(commits), tuple(stack))

    def default_branch(self, remote: str) -> str:
        """remote's default branch, as its HEAD names it (e.g. main)."""
        head = self.git.run("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
        if not head:
            raise StackError(
                f"{remote}'s default branch isn't known; pass --from, or run `git remote set-head {remote} --auto`"
            )
        return head.removeprefix(f"{remote}/")

    def reverts_to_revert(self, reverts: Sequence[str], parent: str) -> list[str]:
        """The commits reverts names, newest first, so undoing them one after
        another doesn't conflict where a later one changed what an earlier one
        did. Fails unless each is in parent's history."""
        git = self.git
        commits = []
        for revert in reverts:
            commit = git.rev(f"{revert}^{{commit}}")
            if commit is None:
                raise StackError(f"{revert} isn't a commit")
            if not git.is_ancestor(commit, f"refs/heads/{parent}"):
                raise StackError(f"{revert} isn't in {parent}'s history")
            commits.append(commit)

        def newest_first(a: str, b: str) -> int:
            return -1 if git.is_ancestor(b, a) else 1 if git.is_ancestor(a, b) else 0

        return sorted(dict.fromkeys(commits), key=functools.cmp_to_key(newest_first))

    def settled_stacks(self) -> Stacks:
        """Like stacks, but first picks up the result of a restack or
        pull the user finished by hand. One that hasn't finished is left alone,
        even if it looks aborted, since it may be about to start in another
        git-stack process, so this is safe to call at any time."""
        if not self.git.operation_in_progress():
            self.settle_all(forget_unfinished=False)
        return self.stacks()

    def stacked_branch(self, branch: str | None, stacks: Stacks) -> str:
        """Return branch, or the current branch if it's None, refusing unless it's
        in a stack."""
        if branch is None:
            return self.current_stacked_branch(stacks, "", hint="name a stacked branch, or switch to one")
        if branch not in stacks:
            raise StackError(f"{branch} isn't in a stack")
        return branch

    @snapshot
    def parent(self, branch: str | None = None) -> str:
        """The branch that branch (default: the current branch) is stacked on."""
        stacks = self.settled_stacks()
        return stacks.parent(self.stacked_branch(branch, stacks))

    @snapshot
    def base(self, branch: str | None = None) -> str:
        """The commit branch (default: the current branch) was last rebased onto."""
        stacks = self.settled_stacks()
        branch = self.stacked_branch(branch, stacks)
        return self.recorded_base(branch, stacks.parent(branch))

    @snapshot
    def show_stack(self, branch: str | None = None) -> list[str]:
        """The branches in branch's (default: the current branch's) stack,
        parent-before-child."""
        stacks = self.settled_stacks()
        return stacks.stack_of(self.stacked_branch(branch, stacks))

    @snapshot
    def next(self) -> str:
        """Switch to the branch stacked on the current one, and return it."""
        current = self.git.current_branch()
        if current is None:
            raise StackError("not on a branch")
        children = self.settled_stacks().children(current)
        if not children:
            raise StackError(f"no branch is stacked on {current}")
        if len(children) > 1:
            raise StackError(
                f"several branches are stacked on {current}: {', '.join(children)}; switch to one with `git switch`"
            )
        self.switch_to(children[0])
        return children[0]

    @snapshot
    def prev(self) -> str:
        """Switch to the current branch's parent, and return it."""
        stacks = self.settled_stacks()
        parent = stacks.parent(self.current_stacked_branch(stacks, "", hint="there's no previous branch"))
        if not self.git.local_branch_exists(parent):
            raise StackError(f"{parent} doesn't exist")
        self.switch_to(parent)
        return parent

    def switch_to(self, branch: str) -> None:
        """Switch to branch, showing git's output (e.g. why it couldn't)."""
        if not self.git.passthrough(*SWITCH, branch):
            raise StackError(f"switching to {branch} failed; see git's output above")

    @snapshot
    def info(
        self, branches: Sequence[str] = (), groups: Sequence[str] = (), offline: bool = False, current: bool = False
    ) -> list[BranchInfo]:
        """Every tracked stack (or the stacks of the branches that branches and
        groups name, see named), as a list of BranchInfo: each stack's root, then its
        branches, parent-before-child. offline checks for merges like the git
        adapter does, without asking GitHub about pull requests."""
        stacks = self.settled_stacks()
        if current or branches or groups:
            stacks = stacks.restricted(self.selected_stacks(branches, groups, stacks, "show"))
        adapter = GitAdapter(self) if offline else make_adapter(self, self.default_remote(stacks, stacks), fetch=False)
        return self.branch_infos(stacks, adapter)

    def branch_infos(self, stacks: Stacks, adapter: GitAdapter) -> list[BranchInfo]:
        """The branches in stacks, as info() lists them, checked with adapter,
        so a command that already has one (and its pull requests) can show
        them without looking them up again."""
        git = self.git
        current = git.current_branch()

        def status(branch: str) -> BranchStatus:
            parent = stacks.parent(branch)
            parent_tip = git.rev(f"refs/heads/{parent}")
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
            branches = list(stacks)
            statuses = dict(zip(branches, pool.map(status, branches), strict=True))
            prefetched.result()
        shown_groups = {
            b: g for b, g in stacks.groups().items() if stacks.is_bottom(b) and len(stacks.group_bottoms(b)) > 1
        }
        result: list[BranchInfo] = []
        for root in stacks.roots():
            result.append(BranchInfo(root, None, None, root == current))
            result.extend(
                BranchInfo(
                    b,
                    stacks.parent(b),
                    statuses[b],
                    b == current,
                    adapter.pull_request_link(b),
                    shown_groups.get(b),
                )
                for b in stacks.descendants(root)
            )
        return result

    def stack_infos(self, branches: Iterable[str], adapter: GitAdapter) -> list[BranchInfo]:
        """The stacks of those of branches that are still tracked, and of the
        other stacks in their groups, as info() lists them (see branch_infos),
        for a command to show what it left them as."""
        stacks = self.stacks()
        return self.branch_infos(
            stacks.restricted(s for b in branches if b in stacks for s in stacks.stack_of(b)), adapter
        )

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

    def skip_merged(self, branches: Iterable[str], stacks: Stacks, adapter: GitAdapter) -> list[str]:
        """Leave merged branches out of branches (parent-before-child), moving
        their children down in stacks (see Stacks.drop). Leave reverted ones
        out too, with the branches stacked on them, as sync does."""
        remaining: list[str] = []
        skipped: set[str] = set()
        for branch in branches:
            parent = stacks.parent(branch)
            if parent in skipped:
                self.emit(events.ParentSkipped(branch, parent))
                skipped.add(branch)
                continue
            state = adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                self.emit(events.SkippedMerged(branch, parent))
                stacks.drop(branch)
            elif state is MergeState.REVERTED:
                # Restacking it could drop its commits, as already in parent.
                self.emit(self.reverted(branch, stacks, adapter))
                skipped.add(branch)
            else:
                remaining.append(branch)
        return remaining

    @exclusive
    def restack(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        onto: str | None = None,
        latest: bool = False,
        allow_shared: bool = False,
    ) -> None:
        """Rebase every branch that needs it onto its parent, bottom-up (see
        needs_restack), in the current stack, or in the stacks of the branches
        that branches and groups name (see named).

        onto first retargets the current branch, so it can't be combined with
        branches or groups; latest
        moves branches on a stack's root onto its tip even if they don't
        conflict with it.
        """
        if onto and (branches or groups):
            raise StackError("--onto moves the current branch, so it can't be combined with branches to restack")
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        stacks = self.stacks()
        original = self.current_checkout()
        if onto:
            branch = self.current_stacked_branch(stacks, "restack", hint="use `git stack track` first")
            self.ensure_local_branch_parent(onto)
            stacks.ensure_no_cycle(branch, onto)
            self.ensure_owned(stacks.stack_of(branch), stacks, allow_shared, "restack")
            if onto in stacks:
                self.ensure_owned(stacks.stack_of(onto), stacks, allow_shared, "restack")
            selected = [branch, *stacks.descendants(branch)]
            # Only in stacks, which isn't saved: restack_branch records the
            # new parent once the rebase has finished.
            stacks.reparent(branch, onto)
        else:
            selected = self.selected_stacks(branches, groups, stacks, "restack")
            self.ensure_owned(selected, stacks, allow_shared, "restack")
        adapter = make_adapter(self, self.default_remote(selected, stacks), fetch=False)
        to_restack = self.skip_merged(selected, stacks, adapter)
        self.restack_all(to_restack, stacks, latest)
        self.restore_checkout(original)

    def sync_selection(
        self,
        branches: Sequence[str],
        groups: Sequence[str],
        stacks: Stacks,
        remote: str,
        only_remote: bool,
        command: SyncCommand,
    ) -> tuple[list[str], set[str]]:
        """Select one remote's stack branches before changing repository state,
        for command."""
        stack = self.selected_stacks(branches, groups, stacks, command.value)
        if only_remote:
            stack = [b for b in stack if self.git.config_get(b, "remote") == remote]
            if not stack:
                raise StackError(f"none of the selected branches track {remote}")
        remotes = self.upstream_remotes(stack)
        if len(remotes) > 1:
            raise StackError(f"can't {command.value} branches on multiple remotes atomically; select one with --remote")
        return stack, remotes

    def upstream_remotes(self, branches: Iterable[str]) -> set[str]:
        """The remotes branches' upstreams are on."""
        return {name for b in branches if (name := self.git.config_get(b, "remote")) and name != "."}

    def stack_remote(self, upstream_remotes: set[str], root_remotes: set[str]) -> str:
        """The remote for stacks without --remote: the one their branches'
        upstreams are on (upstream_remotes), if there's one, or else the one
        their roots' are on (root_remotes), if there's one, or else
        fallback_remote. So a clone without origin works."""
        for remotes in (upstream_remotes, root_remotes):
            if len(remotes) == 1:
                return next(iter(remotes))
        return self.fallback_remote()

    def fallback_remote(self) -> str:
        """The remote for a command without --remote, when nothing it works on
        says which: origin, or the repository's only remote if it has no
        origin."""
        remotes = self.git.remotes()
        return remotes[0] if len(remotes) == 1 else DEFAULT_REMOTE

    def default_remote(self, branches: Iterable[str], stacks: Stacks) -> str:
        """stack_remote for branches, tracked branches of stacks."""
        branches = list(branches)
        return self.stack_remote(
            self.upstream_remotes(branches), self.upstream_remotes({stacks.root(b) for b in branches})
        )

    def protected_sync_destinations(self, branches: list[str], roots: set[str], remote: str) -> set[str]:
        """Those of branches whose upstream is remote's default branch, or the
        remote branch of one of roots, which only --force pushes to. Fails if
        any of them tracks remote and the server can't say what its default
        branch is."""
        git = self.git
        if not any(git.config_get(b, "remote") == remote for b in branches):
            return set()
        default = git.remote_default_branch(remote)
        if default is None:
            raise StackError(
                f"{remote}'s default branch isn't known (couldn't ask {remote}, or its HEAD isn't a branch); "
                "use --force to allow the configured upstream"
            )
        protected = {default} | {git.remote_branch_name(root, remote) for root in roots}
        return {
            b
            for b in branches
            if git.config_get(b, "remote") == remote and git.remote_branch_name(b, remote) in protected
        }

    def ensure_sync_destination(
        self, branches: list[str], roots: set[str], remote: str, force: bool, command: str = "sync"
    ) -> None:
        if force:
            return
        blocked = sorted(self.protected_sync_destinations(branches, roots, remote))
        if blocked:
            raise StackError(
                f"{', '.join(blocked)} track {remote}'s default branch, or a stack root's remote branch; "
                f"rerun `git stack {command} --force` to allow pushing to it"
            )

    @exclusive
    def sync(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str = DEFAULT_REMOTE,
        delete: Delete | None = None,
        latest: bool = False,
        push: bool = True,
        ignore_reverts: Sequence[str] = (),
        allow_shared: bool = False,
        pull_tracked_upstream: bool = False,
        rewritten_upstream: bool = False,
        force: bool = False,
        only_remote: bool = False,
        rebuild_stack: bool = False,
    ) -> SyncResult:
        """Fetch, untrack the merged branches in the current stack, or in the
        stacks of the branches that branches and groups name (see named), deleting what
        delete says, restack them (see restack
        for latest), and pull in commits others pushed. Then, if push is set, push
        the branches that differ from the upstream they already have on remote.
        Branches with no such upstream are never pushed.

        A branch in ignore_reverts counts as merged even if its parent has since
        reverted it.

        Returns the branches that now differ from their remote branch, which
        with push are only those it didn't push, and the stacks it synced.

        Raises StackError, after pushing the rest, if push is set and a branch
        couldn't be restacked or pulled into because it's checked out in
        another worktree, or is stacked on one that couldn't.
        """
        # Without push, it's `git stack pull`, which its messages must suggest
        # rerunning rather than sync, which would push.
        command = SyncCommand.SYNC if push else SyncCommand.PULL
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        initial_stack, _ = self.sync_selection(branches, groups, self.stacks(), remote, only_remote, command)
        if only_remote:
            for branch in initial_stack:
                if self.git.config_get(branch, "remote") == remote:
                    self.settle(branch)
        else:
            self.settle_all()
        stacks = self.stacks()
        self.ensure_owned(
            self.selected_stacks(branches, groups, stacks, command.value), stacks, allow_shared, command.value
        )
        stack, upstream_remotes = self.sync_selection(branches, groups, stacks, remote, only_remote, command)
        self.ensure_ignorable_reverts(ignore_reverts, stack, "the stacks being synced")
        roots = {stacks.root(b) for b in stack}
        roots_to_update = {root for root in roots if not only_remote or self.git.config_get(root, "remote") == remote}
        root_remotes = self.upstream_remotes(roots_to_update)
        push_remote = remote if only_remote else self.stack_remote(upstream_remotes, root_remotes)
        protected = self.protected_sync_destinations(stack, roots, push_remote) if push and not force else set[str]()
        original = self.current_checkout()
        adapter = self.checked_adapter(push_remote)
        remotes = {push_remote} | upstream_remotes | root_remotes | ({remote} if only_remote else set[str]())
        for name in sorted(remotes, key=lambda name: (name != remote, name)):
            self.fetch(name)
        if self.move_off_deleted_parents(stack, stacks, adapter, push_remote):
            roots = {stacks.root(b) for b in stack}
            roots_to_update = {r for r in roots if not only_remote or self.git.config_get(r, "remote") == remote}
            for name in sorted(self.upstream_remotes(roots_to_update) - remotes):
                self.fetch(name)
            if push and not force:
                protected = self.protected_sync_destinations(stack, roots, push_remote)
        # A group's stacks can be on different roots.
        self.fast_forward_trunks(sorted(roots_to_update))

        state = SyncState(
            original,
            stacks,
            push_remote,
            delete,
            latest,
            push,
            frozenset(ignore_reverts),
            pull_tracked_upstream,
            rewritten_upstream,
            frozenset(stack),
            frozenset(protected),
            command,
        )
        # Parent-before-child, so by the time we reach a branch its parent is
        # final: merged branches have been removed (and their children reparented
        # in stacks), and the rest have been restacked and pulled.
        for branch in stack:
            self.sync_branch(branch, adapter, state)

        self.restore_checkout(state.original)
        self.push_branches(
            state.pushable, stacks, adapter, push_remote, rebuild_stack=rebuild_stack, scope=stack, use_upstreams=True
        )
        if push and state.blocked:
            raise StackError(
                f"didn't push {', '.join(state.blocked)}: checked out in another worktree, or stacked on a branch "
                f"that is"
            )
        shown = (
            self.branch_infos(stacks.restricted(stack), adapter) if only_remote else self.stack_infos(stack, adapter)
        )
        return SyncResult(state.unpushed, shown)

    def move_off_deleted_parents(
        self, branches: Sequence[str], stacks: Stacks, adapter: GitAdapter, remote: str
    ) -> bool:
        """Move each of branches stacked on a root that's gone, locally and on
        remote, onto the branch the root was merged into, if the adapter can
        tell (see GitAdapter.deleted_parent_merged_into), and say whether any
        moved.

        That's what a merged branch deleted outside git-stack (e.g. by
        `gh pr merge --delete-branch`) leaves: deleting it forgot it was
        tracked, so nothing local says where it went, but GitHub retargets the
        pull requests on it to the branch it was merged into. Each keeps its
        base, the deleted branch's commit it was built on, so restacking it
        replays only its own commits. The rest are left on the missing root,
        for sync and clean to say how to move them.
        """
        git = self.git
        moved = False
        for branch in branches:
            parent = stacks.parent(branch)
            if (
                parent in stacks
                or git.local_branch_exists(parent)
                or git.rev(f"refs/remotes/{remote}/{parent}") is not None
            ):
                continue
            new_parent = adapter.deleted_parent_merged_into(branch, parent)
            if new_parent is None:
                continue
            try:
                stacks.ensure_no_cycle(branch, new_parent)
            except StackError:
                continue
            stacks.reparent(branch, new_parent)
            self.save(stacks)
            self.emit(events.MovedOffDeletedParent(branch, parent, new_parent))
            moved = True
        return moved

    def sync_branch(self, branch: str, adapter: GitAdapter, state: SyncState) -> None:
        """Untrack branch if it was merged, and otherwise bring it up to date (see
        sync_unmerged), unless its parent was skipped."""
        parent = state.stacks.parent(branch)
        if parent in state.skipped:
            self.emit(events.ParentSkipped(branch, parent))
            state.skipped.add(branch)
            if parent in state.blocked:
                state.blocked.append(branch)
            return
        merge_state = adapter.merge_state(branch, parent, ignore_revert=branch in state.ignore_reverts)
        if merge_state is MergeState.MERGED:
            outside = [b for b in state.stacks.children(branch) if b not in state.scope]
            if outside:
                raise StackError(
                    f"can't untrack {branch}: it has children outside the selected remote: {', '.join(outside)}"
                )
            confirmed = adapter.merge_confirmed(branch, parent)
            if not self.untrack_merged(
                branch, state.stacks, state.delete, allow_different_upstream=True, confirmed=confirmed
            ):
                state.skipped.add(branch)
            elif branch == state.original:
                state.original = self.current_checkout()
        elif merge_state is MergeState.MERGED_WITH_NEW_COMMITS:
            # Restacking would replay the merged commits onto parent.
            self.emit(events.MergedWithNewCommits(branch, parent))
            state.skipped.add(branch)
        elif merge_state is MergeState.REVERTED:
            self.emit(self.reverted(branch, state.stacks, adapter))
            state.skipped.add(branch)
        else:
            self.sync_unmerged(branch, parent, state)

    def reverted(self, branch: str, stacks: Stacks, adapter: GitAdapter) -> events.Reverted:
        """The warning that branch, which stacks says is stacked on its parent,
        was merged and then reverted, with what unrevert needs to bring it back."""
        parent = stacks.parent(branch)
        children = tuple(stacks.children(branch))
        return events.Reverted(branch, parent, tuple(adapter.reverts(branch, parent)), children)

    def ensure_ignorable_reverts(self, ignore_reverts: Iterable[str], scope: Sequence[str], where: str) -> None:
        """Refuse a branch to ignore the reverts of (see sync) that isn't in
        scope, the branches the command will look at, since the command would
        never come across a revert of it. where says what scope is, for the
        error."""
        for branch in ignore_reverts:
            if branch not in scope:
                raise StackError(f"there's no revert of {branch} to ignore in {where}: it isn't one of their branches")

    def sync_unmerged(self, branch: str, parent: str, state: SyncState) -> None:
        """Take a rebase of branch from its upstream (see take_upstream_rebase),
        restack branch, which isn't merged, onto parent and pull in commits
        others pushed to it, then note whether it needs pushing."""
        git = self.git
        if branch in state.protected:
            raise StackError(
                f"{branch} tracks {state.remote}'s default branch, or a stack root's remote branch; "
                "rerun `git stack sync --force` to allow pushing to it"
            )
        if git.upstream_gone(branch):
            self.emit(events.UpstreamGone(branch, parent))
        self.take_upstream_rebase(branch, parent, state.remote, allow_rewritten=state.rewritten_upstream)
        self.ensure_upstream_not_rewritten(branch, parent, state.remote, state.rewritten_upstream, state.command)
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
        if not self.pull_upstream(branch, parent, state.remote, state.pull_tracked_upstream, state.command):
            state.blocked.append(branch)
            return
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip and upstream_tip != git.rev(f"refs/heads/{branch}"):
            if state.push and git.remote_branch(branch) is not None:
                state.pushable.append(branch)
            else:
                state.unpushed.append(branch)

    @exclusive
    def clean(
        self,
        remote: str | None = None,
        delete: Delete | None = None,
        ignore_reverts: Sequence[str] = (),
        allow_shared: bool = False,
    ) -> list[BranchInfo]:
        """Fetch and untrack merged branches in every stack, deleting what delete
        says, without restacking anything. ignore_reverts is as for sync.
        Returns every stack, as info() lists them."""
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        remote = remote or self.default_remote(stacks, stacks)
        if delete is not None:
            self.ensure_owned(stacks, stacks, allow_shared, "clean")
        self.ensure_clean_worktree_for_clean(stacks, delete)
        self.ensure_ignorable_reverts(ignore_reverts, list(stacks), "any stack")
        # Where to leave the user afterwards: their branch (or the commit they had
        # detached at), or wherever deleting their branch left them.
        original = self.current_checkout()
        adapter = self.checked_adapter(remote)
        self.fetch(remote)
        if self.move_off_deleted_parents(list(stacks), stacks, adapter, remote):
            # The current branch may be the root a branch moved onto. The move
            # is saved already, which is right whether or not this refuses.
            self.ensure_clean_worktree_for_clean(stacks, delete)
        self.fast_forward_trunks(stacks.roots())

        # Parent-before-child, so a branch whose parent was merged is checked
        # against its new parent.
        order = stacks.in_order()
        for branch in order:
            parent = stacks.parent(branch)
            if git.rev(f"refs/heads/{parent}") is None:
                self.emit(events.ParentMissing(branch, parent, git.config_get(branch, BASE_KEY)))
                continue
            state = adapter.merge_state(branch, parent, ignore_revert=branch in ignore_reverts)
            if state is MergeState.MERGED:
                confirmed = adapter.merge_confirmed(branch, parent)
                if self.untrack_merged(branch, stacks, delete, confirmed=confirmed) and branch == original:
                    original = self.current_checkout()
            elif state is MergeState.MERGED_WITH_NEW_COMMITS:
                self.emit(events.MergedWithNewCommits(branch, parent))
            elif state is MergeState.REVERTED:
                self.emit(self.reverted(branch, stacks, adapter))
        self.restore_checkout(original)
        return self.stack_infos(order, adapter)

    def ensure_clean_worktree_for_clean(self, stacks: Stacks, delete: Delete | None) -> None:
        """Refuse uncommitted changes that clean could get in the way of: only
        fast-forwarding the current branch, if it's a stack's root, or deleting
        it (and so switching away) touches the worktree, and either could fail
        partway."""
        current = self.git.current_branch()
        if current in stacks.roots() or (delete is not None and delete.deletes_local and current in stacks):
            self.ensure_clean_worktree()

    @exclusive
    def push(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str = DEFAULT_REMOTE,
        force: bool = False,
        prs: bool = False,
        allow_shared: bool = False,
        rebuild_stack: bool = False,
        only_remote: bool = False,
    ) -> PushResult:
        """Force-push (with lease) every unmerged branch in the current stack, or
        in the stacks of the branches that branches and groups name (see named), then let
        the adapter update their pull requests. Returns the pushed branches,
        which is empty if every branch looks merged, and the stacks. With prs,
        then opens their pull requests as prs does; without the GitHub adapter,
        it pushes anyway, and the result's error says it opened none.

        Unless force is set, a branch whose remote branch has its commits rebased
        (see take_upstream_rebase) is moved there first, so there's nothing
        to push for it. Refuses if a remote branch has commits the local one doesn't, unless
        force is set. Either way, the push fails if a remote branch changed
        since it was last fetched.
        """
        self.ensure_no_operation_in_progress()
        stacks = self.stacks()
        selected = self.selected_stacks(branches, groups, stacks, "push")
        self.ensure_owned(selected, stacks, allow_shared, "push")
        stack = [b for b in selected if not only_remote or self.git.config_get(b, "remote") in (None, remote)]
        if not stack:
            raise StackError(f"none of the selected branches can be pushed to {remote}")
        remotes = self.upstream_remotes(stack)
        if len(remotes) > 1:
            raise StackError("can't push branches on multiple remotes atomically; select one with --remote")
        roots = {stacks.root(b) for b in stack}
        push_remote = remote if only_remote else self.stack_remote(remotes, self.upstream_remotes(roots))
        self.ensure_sync_destination(stack, roots, push_remote, force, "push")
        if only_remote:
            for branch in stack:
                self.settle(branch)
        else:
            self.settle_all()
        stacks = self.stacks()
        selected = self.selected_stacks(branches, groups, stacks, "push")
        self.ensure_owned(selected, stacks, allow_shared, "push")
        stack = [b for b in selected if not only_remote or self.git.config_get(b, "remote") in (None, remote)]
        remotes = self.upstream_remotes(stack)
        if not stack or len(remotes) > 1 or (remotes and push_remote not in remotes):
            raise StackError("the selected upstreams changed while settling; retry the push")
        adapter = self.checked_adapter(push_remote)
        github = adapter if prs and isinstance(adapter, GitHubAdapter) else None
        branches = self.skip_merged(stack, stacks, adapter)
        if not force:
            for b in branches:
                self.take_upstream_rebase(b, stacks.parent(b), push_remote)
        pushed = self.push_branches(
            branches,
            stacks,
            adapter,
            push_remote,
            force,
            opening=github is not None,
            rebuild_stack=rebuild_stack,
            use_upstreams=True,
        )
        steps = (
            self.open_pull_requests(pushed, stacks, github, push_remote, rebuild_stack=rebuild_stack)
            if github and pushed
            else None
        )
        error = None
        if prs and github is None and pushed:
            error = f"pushed, but didn't open or update any pull requests: {not_github(push_remote)}"
        shown = (
            self.branch_infos(stacks.restricted(stack), adapter) if only_remote else self.stack_infos(stack, adapter)
        )
        return PushResult(pushed, shown, steps, error)

    def push_refspecs(
        self, branches: list[str], stacks: Stacks, remote: str, force: bool, use_upstreams: bool
    ) -> tuple[list[str], list[str]]:
        """Validate destinations and pin leases to the remote tips we checked."""
        git = self.git
        leases: list[str] = []
        refspecs: list[str] = []
        destinations: set[str] = set()
        for b in branches:
            target = git.remote_branch_name(b, remote) if use_upstreams else b
            if target in destinations:
                raise StackError(f"multiple local branches track {remote}/{target}; refusing to push")
            destinations.add(target)
            remote_tip = git.rev(f"refs/remotes/{remote}/{target}")
            if use_upstreams and git.config_get(b, "remote") == remote and remote_tip is None:
                raise StackError(f"{b}'s upstream {remote}/{target} is unavailable; refusing to push")
            if remote_tip and not force and self.unpulled_commits(b, stacks.parent(b), remote_tip):
                raise StackError(
                    f"{remote}/{target} has commits that {b} doesn't; run `git stack sync` "
                    f"to pull them in first, or push with --force to drop them"
                )
            leases.append(f"--force-with-lease=refs/heads/{target}:{remote_tip or ''}")
            refspecs.append(f"refs/heads/{b}:refs/heads/{target}" if use_upstreams else b)
        return leases, refspecs

    def push_branches(
        self,
        branches: list[str],
        stacks: Stacks,
        adapter: GitAdapter,
        remote: str,
        force: bool = False,
        opening: bool = False,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
        use_upstreams: bool = False,
    ) -> list[str]:
        """Force-push (with lease) branches, which must be unmerged, in one atomic
        push, then let the adapter update their pull requests (see
        GitAdapter.after_push for opening). Returns branches.

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
        leases, refspecs = self.push_refspecs(branches, stacks, remote, force, use_upstreams)
        # Before the adapter changes anything (e.g. retargets pull requests),
        # so a push that's bound to fail, e.g. because a remote branch moved
        # since it was fetched, leaves nothing changed. The remote can still
        # refuse the real push (e.g. a protected branch), which a dry run
        # doesn't ask it about.
        if (why := git.push_would_fail("--atomic", *leases, remote, *refspecs)) is not None:
            raise StackError(
                f"push would fail, so nothing was pushed or changed; if the remote has new commits, run "
                f"`git stack sync` and try again:\n{why}"
            )
        try:
            adapter.before_push(branches, stacks, rebuild_stack, scope, opening)
        except StackError as e:
            raise StackError(
                f"updating pull requests failed, so nothing was pushed (set {ADAPTER_SETTING} "
                f"to git to stop using GitHub): {e}{self.changes_left(adapter)}"
            ) from e
        # Atomic, so a branch that can't be pushed doesn't leave the rest of the
        # stack pushed without it, and its pull requests not retargeted.
        if not git.passthrough("push", "--atomic", *leases, "--set-upstream", remote, *refspecs):
            raise StackError(
                "push failed, so nothing was pushed; if the remote has new commits, run `git stack sync` and try "
                f"again{self.changes_left(adapter)}"
            )
        for b in branches:
            git.config_set(b, PULLED_KEY, git.tip(b))
            if (identity := self.upstream_identity(b)) is not None:
                git.config_set(b, PULLED_UPSTREAM_KEY, identity)
        try:
            adapter.after_push(branches, stacks, opening, rebuild_stack, scope)
        except StackError as e:
            raise StackError(
                f"pushed, but updating pull requests failed (set {ADAPTER_SETTING} to git to stop using GitHub): {e}"
            ) from e
        return branches

    def changes_left(self, adapter: GitAdapter) -> str:
        """What adapter changed before a push failed, for its error, or ""."""
        if not adapter.changes:
            return ""
        changes = "".join(f"\n  {change}" for change in adapter.changes)
        return (
            f"\nBefore that, these were changed on GitHub, and were left as they are:{changes}\n"
            "They're what the push needs, so push again once it can succeed, or undo them as shown."
        )

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
            configured = git.config_get(b, "remote")
            if configured and configured != remote:
                raise StackError(f"{b} tracks {configured}, not {remote}; select its upstream remote")
            # Otherwise the push would go to a remote branch named after b,
            # which nothing says is b's.
            if configured == remote and not git.has_branch_upstream(b, remote):
                raise StackError(
                    f"{b}'s upstream on {remote} isn't a branch (branch.{b}.merge is "
                    f"{git.config_get(b, 'merge')!r}); fix it with `git branch --set-upstream-to "
                    f"{remote}/<branch> {b}`, or `git branch --unset-upstream {b}`"
                )
            target = git.remote_branch_name(b, remote)
            if git.rev(f"refs/remotes/{remote}/{target}") is not None and configured != remote:
                raise StackError(
                    f"{remote}/{b} exists but isn't {b}'s upstream, so it's probably someone "
                    f"else's branch; rename yours with `git stack rename {b} <new>`. If "
                    f"{remote}/{b} is yours, run `git branch --set-upstream-to {remote}/{b} {b}` "
                    f"and `git stack sync` first"
                )

    @exclusive
    def prs(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str | None = None,
        dry_run: bool = False,
        rebuild_stack: bool = False,
    ) -> PrsResult:
        """Open a draft pull request into its stack parent for each unmerged
        branch in the current stack (or in the stacks of the branches that
        branches and groups name, see named) that has none, bottom-up. Its title is the
        branch's oldest commit's subject, and its body the repository's pull
        request template. Points the other open ones at their stack parents, as
        push does, in case one was opened or retargeted elsewhere since. Then
        rewrites the stack section in each open pull request's description
        that's out of date (see GitHubAdapter.update_stack_sections).

        Refuses unless every unmerged branch in the stack was pushed with `git
        stack push` and matches its remote branch, so each pull request shows
        what's local. Returns a PullRequestStep for each branch, and the stacks
        as info() lists them. With dry_run, nothing is opened, retargeted or
        rewritten.
        """
        self.ensure_no_operation_in_progress()
        self.settle_all()
        stacks = self.stacks()
        stack = self.selected_stacks(branches, groups, stacks, "open pull requests for")
        remote = remote or self.default_remote(stack, stacks)
        adapter = ensure_github(make_adapter(self, remote, fetch=True), remote)
        adapter.ensure_available()
        branches = self.skip_merged(stack, stacks, adapter)
        steps = self.open_pull_requests(branches, stacks, adapter, remote, dry_run, rebuild_stack)
        return PrsResult(steps, self.stack_infos(stack, adapter))

    def open_pull_requests(
        self,
        branches: list[str],
        stacks: Stacks,
        adapter: GitHubAdapter,
        remote: str,
        dry_run: bool = False,
        rebuild_stack: bool = False,
    ) -> list[PullRequestStep]:
        """Do what prs does for branches, which must be unmerged,
        parent-before-child. Returns a PullRequestStep for each."""
        git = self.git
        self.ensure_on_remote(branches, sorted({stacks.root(b) for b in branches}), remote)
        prs = adapter.pull_requests()
        allowed = {prs[b].number for b in branches if b in prs and prs[b].state is GitHubState.OPEN}
        template = adapter.pull_request_template() or ""
        steps: list[PullRequestStep] = []
        for b in branches:
            parent = stacks.parent(b)
            pr = prs.get(b)
            if pr is not None:
                if not dry_run:
                    adapter.retarget(b, parent, rebuild_stack, allowed)
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
            allowed.add(number)
        if branches and (not dry_run or rebuild_stack):
            self.update_stack_sections(branches, stacks, adapter, rebuild_stack, dry_run)
        return steps

    def update_stack_sections(
        self,
        branches: list[str],
        stacks: Stacks,
        adapter: GitHubAdapter,
        rebuild_stack: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Update the stack sections of branches' pull requests (see
        GitHubAdapter.update_stack_sections), one for each root, since a
        group's stacks can be on several."""
        try:
            for root in sorted({stacks.root(b) for b in branches}):
                adapter.update_stack_sections(
                    [b for b in branches if stacks.root(b) == root], stacks, rebuild_stack, dry_run
                )
        except StackError as e:
            raise StackError(f"updating the stack in pull request descriptions failed: {e}") from e

    def ensure_on_remote(self, branches: list[str], roots: list[str], remote: str) -> None:
        """Refuse unless each of branches was pushed with `git stack push` and
        matches its remote branch, and their stacks' roots are on remote, so
        pull requests can be opened for them that show what's local."""
        git = self.git
        unpushed = [
            b
            for b in branches
            if git.config_get(b, "remote") != remote
            or git.rev(f"refs/remotes/{remote}/{git.remote_branch_name(b, remote)}") != git.tip(b)
        ]
        if unpushed:
            raise StackError(
                f"these branches aren't pushed, or differ from their remote branches: "
                f"{', '.join(unpushed)}; run `git stack push` first"
            )
        for root in roots:
            if git.rev(f"refs/remotes/{remote}/{git.remote_branch_name(root, remote)}") is None:
                raise StackError(f"{root} isn't on {remote}, so pull requests can't merge into it")


class ImportPlanner:
    """Works out what import should do for prs, without changing anything.

    viewer is the gh user's login. Branches of anyone else's pull requests,
    or with commits by anyone else, are only planned to be tracked with
    allow_others."""

    def __init__(
        self,
        stack: StackClient,
        prs: list[OpenPullRequest],
        remote: str,
        default_branch: str | None,
        viewer: str | None,
        allow_others: bool,
        adapter: GitHubAdapter,
    ) -> None:
        self.stack = stack
        self.git = stack.git
        self.prs = prs
        self.remote = remote
        self.default_branch = default_branch
        self.viewer = viewer
        self.allow_others = allow_others
        self.adapter = adapter
        # The tracked branches, and the ones planned to be tracked so far.
        self.stacks = stack.stacks()
        self.by_head = {pr.head: pr for pr in prs}
        self.planned: dict[str, ImportStep] = {}
        # The plans for branches without a pull request, parents first.
        self.without_prs: dict[str, ImportStepWithoutPr] = {}
        # Branches whose plan is being worked out, to catch a cycle.
        self.visiting: set[str] = set()

    def plan_all(self) -> list[ImportStep]:
        """An ImportStep for each pull request, parents before children."""
        for pr in self.prs:
            self.plan(pr)
        return list(self.planned.values())

    def plan(self, pr: OpenPullRequest) -> ImportStep:
        """pr's plan, working it out (after its parent's) if it hasn't been yet."""
        if pr.head not in self.planned:
            self.visiting.add(pr.head)
            self.planned[pr.head] = self.plan_one(pr)
            self.visiting.discard(pr.head)
        return self.planned[pr.head]

    def plan_one(self, pr: OpenPullRequest) -> ImportStep:
        branch, parent = pr.head, pr.base
        if branch not in self.stacks:
            return self.plan_untracked(pr)
        if self.stacks.parent(branch) == parent:
            return ImportStep(branch, parent, pr.number, ImportAction.ALREADY_TRACKED)
        return ImportStep(
            branch, parent, pr.number, ImportAction.PARENT_DIFFERS, tracked_parent=self.stacks.parent(branch)
        )

    def plan_untracked(self, pr: OpenPullRequest) -> ImportStep:
        """The plan for pr, whose branch isn't tracked: track it, creating it
        first if there's no local branch, unless it has to be skipped."""
        git, remote = self.git, self.remote
        branch, parent = pr.head, pr.base
        remote_ref = f"refs/remotes/{remote}/{branch}"
        local = git.local_branch_exists(branch)
        if not local and git.rev(remote_ref) is None:
            return self.skip(pr, f"{remote}/{branch} doesn't exist")
        try:
            others = self.other_author(pr, local)
        except StackError as e:
            return self.skip(pr, str(e))
        try:
            parent_ref = self.merges_into_ref(parent, branch if local else remote_ref)
            self.stacks.ensure_no_cycle(branch, parent)
        except StackError as e:
            return self.skip(pr, str(e))
        base = self.stack.fork_point(parent_ref, branch if local else remote_ref)
        if base is None:
            return self.skip(pr, f"it has no history in common with {parent}")
        self.stacks.reparent(branch, parent)
        action = ImportAction.TRACK if local else ImportAction.CREATE
        return ImportStep(branch, parent, pr.number, action, base=base, other_authors=others)

    def merges_into_ref(self, parent: str, built: str) -> str:
        """The ref of parent, which a pull request of built (a ref) merges
        into, after planning it if it's to be created. Raises StackError if the
        pull request has to be skipped for it."""
        git, remote = self.git, self.remote
        if parent in self.by_head:
            if parent in self.visiting:
                raise StackError("the pull requests it's based on are based on it")
            if self.plan(self.by_head[parent]).action is ImportAction.SKIP:
                raise StackError(f"{parent}, which it merges into, was skipped")
            return f"refs/heads/{parent}" if git.local_branch_exists(parent) else f"refs/remotes/{remote}/{parent}"
        if git.local_branch_exists(parent):
            return self.trunk_ref(parent, built)
        if (without_pr := self.plan_without_pr(parent)).action in (ImportAction.CREATE, ImportAction.CREATE_ROOT):
            return f"refs/remotes/{remote}/{parent}"
        raise StackError(f"{parent}, which it merges into, isn't a local branch ({without_pr.reason})")

    def other_author(self, pr: OpenPullRequest, local: bool) -> list[str]:
        """pr's author, unless it's the gh user. Raises StackError if that means
        pr's branch can't be tracked: without allow_others, or if local, the
        user's local branch of the same name isn't the one on the remote."""
        if pr.author == self.viewer:
            return []
        author = pr.author or "a deleted account"
        if not self.allow_others:
            raise StackError(f"it's {author}'s pull request; import it with -f")
        if local and not self.git.tracks_same_name(pr.head, self.remote):
            # Most likely the user's own branch that happens to share its name.
            raise StackError(f"your local {pr.head} isn't {self.remote}/{pr.head}'s branch")
        return [author]

    def skip(self, pr: OpenPullRequest, reason: str) -> ImportStep:
        return ImportStep(pr.head, pr.base, pr.number, ImportAction.SKIP, reason=reason)

    def plan_without_pr(self, branch: str) -> ImportStepWithoutPr:
        """The plan for branch, which has no pull request or local branch,
        working it out (after its parent's) if it hasn't been yet."""
        if branch not in self.without_prs:
            self.visiting.add(branch)
            self.without_prs[branch] = self.plan_one_without_pr(branch)
            self.visiting.discard(branch)
        return self.without_prs[branch]

    def plan_one_without_pr(self, branch: str) -> ImportStepWithoutPr:
        """Create and track branch from its remote branch, on the parent its
        history says it's built on, if all of its own commits are the user's."""
        remote_ref = f"refs/remotes/{self.remote}/{branch}"
        if self.git.rev(remote_ref) is None:
            reason = f"{self.remote}/{branch} doesn't exist"
            return ImportStepWithoutPr(branch, None, ImportAction.SKIP, reason=reason)
        try:
            if self.adapter.has_merged_pull_requests_into(branch):
                return ImportStepWithoutPr(branch, None, ImportAction.CREATE_ROOT)
        except StackError as e:
            return ImportStepWithoutPr(
                branch, None, ImportAction.SKIP, reason=f"couldn't check its merged pull requests: {e}"
            )
        try:
            parent = self.find_parent(branch)
        except StackError as e:
            return ImportStepWithoutPr(branch, None, ImportAction.SKIP, reason=str(e))
        try:
            parent_ref = self.built_on_ref(parent, remote_ref)
            others = self.other_authors(parent, parent_ref, remote_ref)
            if others and not self.allow_others:
                raise StackError(f"it has commits by {', '.join(others)}; import it with -f")
            self.stacks.ensure_no_cycle(branch, parent)
            base = self.stack.fork_point(parent_ref, remote_ref)
            if base is None:
                raise StackError(f"it has no history in common with {parent}")
        except StackError as e:
            return ImportStepWithoutPr(branch, parent, ImportAction.SKIP, reason=str(e))
        self.stacks.reparent(branch, parent)
        return ImportStepWithoutPr(branch, parent, ImportAction.CREATE, base=base, other_authors=others)

    def built_on_ref(self, parent: str, built: str) -> str:
        """The ref of parent, which built, the ref of a branch without a pull
        request, is built on, after planning it if it's to be created. Raises
        StackError if it won't be a local branch, or see trunk_ref."""
        git, remote = self.git, self.remote
        if parent in self.visiting:
            raise StackError(f"{parent}, which it's built on, is built on it")
        if parent in self.by_head:
            if self.plan(self.by_head[parent]).action is ImportAction.SKIP:
                raise StackError(f"{parent}, which it's built on, was skipped")
            return f"refs/heads/{parent}" if git.local_branch_exists(parent) else f"refs/remotes/{remote}/{parent}"
        if parent == self.default_branch:
            if not git.local_branch_exists(parent):
                raise StackError(f"{parent}, which it's built on, isn't a local branch")
            return self.trunk_ref(parent, built)
        if git.local_branch_exists(parent):
            return self.trunk_ref(parent, built)
        if self.plan_without_pr(parent).action is ImportAction.SKIP:
            raise StackError(f"{parent}, which it's built on, was skipped")
        return f"refs/remotes/{remote}/{parent}"

    def trunk_ref(self, branch: str, built: str) -> str:
        """The ref of local branch, which built (a ref) is built on, as import
        leaves it: if it's untracked (a trunk, e.g. main), import fast-forwards
        it to its upstream when it tracks a branch on it, so a stale trunk
        doesn't make bases too old.

        Raises StackError if built has commits of the upstream that the trunk
        lacks, and the trunk can't be fast-forwarded (it's checked out in
        another worktree, or diverged): tracked on it, built would count them
        as its own, or later be restacked without them.
        """
        git, local = self.git, f"refs/heads/{branch}"
        if branch in self.stacks or (upstream := self.stack.trunk_upstream(branch)) is None:
            return local
        if (target := self.stack.fast_forward_target(branch)) is not None:
            return target
        built_on = git.run("merge-base", upstream.new, built, check=False)
        if not built_on or git.is_ancestor(built_on, local):
            return local
        if not git.is_ancestor(upstream.old, upstream.new):
            raise StackError(
                f"it's built on {upstream.name}, which {branch} has diverged from; bring {branch} in line with "
                "it, then import again"
            )
        path = git.worktree_of(branch)
        raise StackError(
            f"{branch} is behind {upstream.name}, which it's built on, but checked out in {path}; run "
            f"`git -C {path} merge --ff-only`, then import again"
        )

    def other_authors(self, parent: str, parent_ref: str, remote_ref: str) -> list[str]:
        """The email addresses of the authors besides the user of the commits
        at remote_ref that parent doesn't have. Raises StackError if there are
        no such commits."""
        git = self.git
        email = (git.author_email() or "").lower()
        authors = set(git.run("log", "--format=%ae", f"{parent_ref}..{remote_ref}").lower().splitlines())
        if not authors:
            raise StackError(f"it has no commits that {parent} doesn't")
        return sorted(authors - {email})

    def select(
        self,
        branches: Sequence[str],
        numbers: Sequence[int],
        plan: list[ImportStep],
        without_prs: list[ImportStepWithoutPr],
    ) -> tuple[list[ImportStep], list[ImportStepWithoutPr]]:
        """The steps of plan and without_prs in the stack trees containing
        branches and the pull requests numbers. Raises StackError for one that
        isn't in any."""
        parents = {s.branch: s.parent for s in plan} | {s.branch: s.parent for s in without_prs if s.parent}
        by_number = {s.number: s.branch for s in plan}

        def bottom(branch: str) -> str:
            seen = {branch}
            while (parent := parents.get(branch)) in parents and parent not in seen:
                branch = parent
                seen.add(branch)
            return branch

        bottoms: set[str] = set()
        for number in numbers:
            if number not in by_number:
                # It's open, so its author's pull requests were looked up.
                raise StackError(f"#{number} isn't from a branch in the repository (e.g. it's from a fork)")
            bottoms.add(bottom(by_number[number]))
        for branch in branches:
            if branch not in parents:
                raise StackError(
                    f"{branch} isn't the branch of an open pull request it looked up, or a branch one is built on"
                )
            bottoms.add(bottom(branch))
        return (
            [s for s in plan if bottom(s.branch) in bottoms],
            [s for s in without_prs if bottom(s.branch) in bottoms],
        )

    def find_parent(self, branch: str) -> str:
        """The branch that remote branch is most likely built on: of the
        remote's branches it contains that have commits the default branch
        doesn't, the closest to it, or else the default branch. Raises
        StackError if there's no one closest.

        Only finds it while branch is built on the parent's current tip: if
        the parent was rewritten since, its old commits are in branch, so it
        looks built on what's below the parent."""
        git, remote = self.git, self.remote
        if self.default_branch is None:
            raise StackError(f"{remote} has no default branch to look for its parent on")
        prefix = f"refs/remotes/{remote}/"
        tip = git.rev(prefix + branch)
        below: dict[str, list[str]] = {}
        listing = git.run(
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            f"--merged={prefix}{branch}",
            f"--no-merged={prefix}{self.default_branch}",
            prefix,
        )
        for line in listing.splitlines():
            oid, ref = line.split(" ", 1)
            if oid != tip:
                below.setdefault(oid, []).append(ref.removeprefix(prefix))
        if not below:
            return self.default_branch
        nearest = git.run("merge-base", "--independent", *below).split()
        candidates = sorted(name for oid in nearest for name in below[oid])
        if len(candidates) > 1:
            raise StackError(f"it could be built on any of {', '.join(candidates)}")
        return candidates[0]
