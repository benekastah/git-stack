import enum
import fnmatch
import functools
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from itertools import takewhile
from typing import Concatenate

from git_stack import events
from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.adapters.git import GitAdapter, PullRequestLink
from git_stack.adapters.github import (
    GitHubAdapter,
    GitHubState,
    OpenPullRequest,
    PRProtection,
    PullRequest,
    check_github_login,
    github_repo,
    github_repo_from_url,
)
from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    CURRENT_GROUP,
    INTEGRATION_KEY,
    PULLED_KEY,
    PULLED_UPSTREAM_KEY,
    TRACK_UPSTREAM_KEY,
    Delete,
    RemoteDeletion,
    StackCore,
    SyncCommand,
    TrunkUpstream,
    is_glob,
)
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import DEFAULT_REMOTE, SWITCH
from git_stack.integrations import Integration, Integrations, RetiredSource, Source
from git_stack.json_fields import json_name
from git_stack.performance import active_timeline
from git_stack.stacks import Ownership, Stacks

DEFAULT_PR_PROTECTION = PRProtection()


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
    STALE_BASE = "stale-base"
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
    integration_sources: tuple[tuple[str, bool], ...] | None = None
    integration_needs_restack: bool = False


@dataclass(frozen=True)
class Integrated:
    """The integration branch and its tip, or None after aborting a build."""

    name: str
    tip: str | None


@dataclass
class RefreshSelection:
    """Selected work branches and the integration roots they need."""

    branches: list[str]
    integrations: dict[str, Integration]
    explicit: tuple[str, ...] = ()
    recovered_sources: frozenset[str] = frozenset()
    selected_groups: frozenset[str] = frozenset()
    recovery_remotes: dict[str, set[str]] = field(default_factory=dict)

    restack_inputs: dict[str, Integration] = field(default_factory=dict)

    def ordered(self, stacks: Stacks) -> tuple[list[str], list[str]]:
        """Process selected source stacks before refreshing integration roots."""
        selected_bottoms = {stacks.bottom(branch) for branch in self.branches}
        source_bottoms = {
            stacks.bottom(source)
            for integration in self.integrations.values()
            for source in Integrations.source_names(integration, stacks)
            if source in stacks
        } & selected_bottoms
        source_bottoms.update(
            stacks.bottom(branch)
            for branch in self.recovered_sources
            if branch in stacks and stacks.root(branch) not in self.integrations
        )
        sources: list[str] = []
        remaining: list[str] = []
        for branch in self.branches:
            (sources if stacks.bottom(branch) in source_bottoms else remaining).append(branch)
        return sources, remaining


@dataclass(frozen=True)
class IntegrationPush:
    """The observed local inputs and leased remote destination of a rebuild."""

    name: str
    target: str
    remote_tip: str | None
    refs: dict[str, str | None]


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
class QueuedRemoteDeletion:
    deletion: RemoteDeletion
    journals: set[str] = field(default_factory=set)

    def add(self, journal: str, deletion: RemoteDeletion) -> None:
        self.journals.add(journal)
        self.deletion = deletion.with_dependents_from(self.deletion)


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
    no_verify: bool = False
    skipped: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    confirmed_merged: dict[str, str | None] = field(default_factory=dict)
    defer_delete: Callable[[RemoteDeletion], None] | None = None
    check_delete: Callable[[RemoteDeletion], None] | None = None
    protected_deletes: frozenset[tuple[str, str]] = frozenset()
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
    def branch(self, name: str, group: str | None = None, from_branch: str | None = None) -> str:
        """Create name on top of from_branch (default: the current branch) and
        track it, switching to it and putting its stack in group, if given.
        Returns the parent.

        Refuses, creating nothing, if the stack is in another group already.
        """
        git = self.git
        parent = from_branch or git.current_branch()
        if parent is None:
            raise StackError("not on a branch")
        if from_branch and not git.local_branch_exists(from_branch):
            raise StackError(f"{from_branch} isn't a local branch")
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
        git.run(*SWITCH, "--no-track", "--create", name, *([f"refs/heads/{from_branch}"] if from_branch else []))
        stacks.reparent(name, parent)
        if group is not None:
            if not stacks.bottoms_in(group):
                Integrations(self).replace_group(group)
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

        self.validate_integrations(stacks)
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
        self.validate_import_integrations(stacks, plan, without_prs)
        self.fast_forward_import_trunks(plan, without_prs, stacks)
        previous_ownership = {b: stacks[b].ownership for b in stacks}
        self.apply_import(plan, without_prs, remote, stacks)
        self.classify_imported_stacks(plan, without_prs, open_prs, adapter.viewer, previous_ownership, stacks)
        shown = [s.branch for s in plan] + [s.branch for s in without_prs]
        return ImportResult(plan, self.stack_infos(shown, adapter), without_prs, remote)

    def validate_import_integrations(
        self, stacks: Stacks, plan: list[ImportStep], without_prs: list[ImportStepWithoutPr]
    ) -> None:
        if Integrations(self).names():
            proposed = stacks.copy()
            for step in [*without_prs, *plan]:
                if step.base is not None and step.parent is not None:
                    proposed.reparent(step.branch, step.parent)
            self.validate_integrations(proposed)

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

    def remove_merged(self, branch: str, stacks: Stacks, delete: bool) -> None:
        """Clear stale recovery selectors when a merge creates a new group."""
        creates_group = stacks.is_bottom(branch) and stacks.group(branch) is None and bool(stacks.children(branch))
        super().remove_merged(branch, stacks, delete)
        if creates_group:
            Integrations(self).replace_group(branch)

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
        if not stacks.bottoms_in(name):
            Integrations(self).replace_group(name)
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
        Integrations(self).replace_group(new, old)
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
        integrations = Integrations(self)
        updated_inputs = integrations.renamed_inputs(old, new)
        self.ensure_name_free_on_remotes(new)
        upstream = git.run("rev-parse", "--abbrev-ref", f"{old}@{{upstream}}", check=False) or None
        if integrations.read(old) is not None and (integrations.path(old) / ".git").is_file():
            integrations.remove_worktree(old, force=False)
        git.run("branch", "--move", old, new)
        for name, config in updated_inputs.items():
            integrations.save(name, config)
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
        affected = (
            b
            for branch in (*stack, parent)
            if stack and branch in stacks
            for b in stacks.stack_of(branch, groups=False)
        )
        self.ensure_owned(affected, stacks, allow_shared, "unrevert")
        self.ensure_local_branch_parent(parent)
        planned = stacks.copy()
        planned.reparent(name, parent)
        for branch in stack:
            planned.ensure_no_cycle(branch, name)
            planned.reparent(branch, name)
        self.validate_integrations(planned)
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
        stacks = planned
        if group is not None:
            stacks.set_group(name, group)
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

    def default_branch(self, remote: str, *, from_option: bool = True) -> str:
        """remote's default branch, as its HEAD names it (e.g. main)."""
        head = self.git.run("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
        if not head:
            override = "pass --from, or " if from_option else ""
            raise StackError(
                f"{remote}'s default branch isn't known; {override}run `git remote set-head {remote} --auto`"
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
        branches, parent-before-child. current selects the checkout's stack when
        it has one. offline checks for merges locally, without asking GitHub."""
        stacks = self.settled_stacks()
        integration_roots = Integrations(self).names() & self.git.local_branches()
        if current and not (branches or groups):
            checkout = self.git.current_branch()
            current = checkout is None or checkout in stacks or checkout in integration_roots
        if current or branches or groups:
            selected = self.refresh_selection(branches, groups, stacks, "show")
            stacks = stacks.restricted(selected.branches)
            integration_roots &= selected.integrations.keys()
        adapter = (
            GitAdapter(self)
            if offline
            else make_adapter(self, self.default_remote(stacks, stacks), fetch=False, lookup_branches=stacks)
        )
        return self.branch_infos(stacks, adapter, integration_roots, show_single_groups=True, overlap_root_info=True)

    def branch_infos(
        self,
        stacks: Stacks,
        adapter: GitAdapter,
        extra_roots: Iterable[str] = (),
        *,
        show_single_groups: bool = False,
        overlap_root_info: bool = False,
    ) -> list[BranchInfo]:
        """The branches in stacks, as info() lists them, checked with adapter,
        so a command that already has one (and its pull requests) can show
        them without looking them up again. show_single_groups keeps names
        visible even when the displayed selection has only one stack in a group."""
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
            if self.stale_trunk_base(branch):
                return BranchStatus.STALE_BASE
            return BranchStatus.NEEDS_RESTACK if needs_restack else BranchStatus.UP_TO_DATE

        roots = sorted(set(stacks.roots()) | set(extra_roots))

        def root_infos() -> tuple[list[BranchInfo], list[events.PullRequestLookupFailed]]:
            integrations = Integrations(self)
            warnings: list[events.PullRequestLookupFailed] = []
            rows = [self.integration_root_info(root, current, integrations, adapter, warnings) for root in roots]
            return rows, warnings

        # Each status can take a few git calls, and checking whether a branch on
        # main conflicts with it can take a second or more in a big repository
        # if the branch is far behind, so they're worked out in parallel, while
        # the adapter looks up pull requests (about a second on GitHub).
        with ThreadPoolExecutor(initializer=active_timeline.set, initargs=(active_timeline.get(),)) as pool:
            prefetched = pool.submit(adapter.prefetch)
            root_results = pool.submit(root_infos) if overlap_root_info else None
            branches = list(stacks)
            statuses = dict(zip(branches, pool.map(status, branches), strict=True))
            prefetched.result()
        shown_groups = {
            b: g
            for b, g in stacks.groups().items()
            if stacks.is_bottom(b) and (show_single_groups or len(stacks.group_bottoms(b)) > 1)
        }
        result: list[BranchInfo] = []
        root_rows, warnings = root_results.result() if root_results else root_infos()
        for warning in warnings:
            self.emit(warning)
        for root, root_info in zip(roots, root_rows, strict=True):
            result.append(root_info)
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

    def integration_root_info(
        self,
        root: str,
        current: str | None,
        integrations: Integrations,
        adapter: GitAdapter,
        warnings: list[events.PullRequestLookupFailed],
    ) -> BranchInfo:
        git = self.git
        config = integrations.read(root)
        integration_sources = None
        root_status = None
        if config is not None:
            root_tip = git.rev(f"refs/heads/{config['root']}")
            selected = integrations.selected_sources(config, self.stacks()) if "group" in config else config["sources"]
            sources = [source.copy() for source in selected]
            if root_tip:
                unresolved = [source for source in sources if not integrations.local_landing(source, root_tip)]
                if isinstance(adapter, GitHubAdapter) and unresolved:
                    try:
                        landed, _ = adapter.source_landings({(s["head"], s["tip"]) for s in unresolved}, root_tip)
                    except StackError as error:
                        warnings.append(events.PullRequestLookupFailed(str(error)))
                    else:
                        for source in unresolved:
                            source["landing"] = landed.get((source["head"], source["tip"]))
            integration_sources = tuple((source["name"], bool(source["landing"])) for source in sources)
            changed = (
                root_tip != config["root_tip"]
                or [source["name"] for source in sources] != [source["name"] for source in config["sources"]]
                or any(
                    git.rev(f"refs/heads/{source['name']}") not in (None, source["tip"])
                    or bool(source["landing"]) != bool(original["landing"])
                    for source, original in zip(sources, config["sources"], strict=True)
                )
            )
            if changed:
                root_status = BranchStatus.NEEDS_RESTACK
        return BranchInfo(
            root,
            None,
            None,
            root == current,
            integration_sources=integration_sources,
            integration_needs_restack=root_status is BranchStatus.NEEDS_RESTACK,
        )

    def stack_infos(self, branches: Iterable[str], adapter: GitAdapter) -> list[BranchInfo]:
        """The stacks of those of branches that are still tracked, as info() lists them (see branch_infos),
        for a command to show what it left them as."""
        stacks = self.stacks()
        return self.branch_infos(
            stacks.restricted(s for b in branches if b in stacks for s in stacks.stack_of(b, groups=False)), adapter
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

    @staticmethod
    def configure_pr_protection(adapter: GitAdapter, protection: PRProtection, force: bool) -> PRProtection:
        if force and not protection.force:
            protection = replace(protection, force=True)
        if isinstance(adapter, GitHubAdapter):
            adapter.protection = protection
        return protection

    def skip_merged(
        self,
        branches: Iterable[str],
        stacks: Stacks,
        adapter: GitAdapter,
        *,
        branch_adapters: dict[str, GitAdapter] | None = None,
    ) -> list[str]:
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
            branch_adapter = branch_adapters.get(branch, adapter) if branch_adapters is not None else adapter
            state = branch_adapter.merge_state(branch, parent)
            if state is MergeState.MERGED:
                self.emit(events.SkippedMerged(branch, parent))
                stacks.drop(branch)
            elif state is MergeState.REVERTED:
                # Restacking it could drop its commits, as already in parent.
                self.emit(self.reverted(branch, stacks, branch_adapter))
                skipped.add(branch)
            else:
                remaining.append(branch)
        return remaining

    @exclusive
    def integrate(
        self,
        name: str,
        sources: Sequence[str],
        *,
        root: str | None = None,
        group: bool = False,
        adopt: bool = False,
        abort: bool = False,
    ) -> Integrated:
        """Create, replace or adopt an integration root, or abort its pending build."""
        git = self.git
        integrations = Integrations(self)
        if not git.ok("check-ref-format", "--branch", name) or not git.ok("check-ref-format", f"refs/heads/{name}"):
            raise StackError(f"{name} isn't a valid integration branch name")
        integrations.restore_pending_checkout()
        if abort:
            if sources or root is not None or group:
                raise StackError("--abort takes only the integration name")
            integrations.abort(name)
            return Integrated(name, None)
        integrations.finish_interrupted(name)
        previous = integrations.read(name)
        expected = git.rev(f"refs/heads/{name}")
        if previous:
            if adopt:
                raise StackError(f"{name} is already a registered integration")
            integrations.preflight(name)
        elif adopt != (expected is not None):
            raise StackError(f"{name} already exists; use --adopt" if expected else f"{name} isn't a local branch")
        stacks = self.stacks()
        root, sources, source_group = self.choose_integration_inputs(sources, root, group, previous, stacks)
        original_stacks = stacks
        if adopt and name in stacks:
            stacks = stacks.copy()
            stacks.untrack([name])
        integrations.validate(
            name, root, sources, stacks, previous["tip"] if previous and previous["root_tip"] else None
        )
        integrations.ensure_independent(name, stacks, original_stacks)
        captured = [integrations.source(source, git.tip(source)) for source in sources]
        if adopt:
            assert expected is not None
            self.save(stacks)
            config = Integration(root=root, sources=captured, root_tip=None, tip=expected)
            if source_group is not None:
                config["group"] = source_group
            integrations.save(name, config)
        else:
            remote = self.stack_remote(set(), self.upstream_remotes([root]))
            desired, observed = integrations.inputs(
                name, root, captured, remote, fetch=False, previous=previous, group=source_group
            )
            integrations.build(name, desired, observed, previous, expected)
        return Integrated(name, git.tip(name))

    def choose_integration_inputs(
        self,
        sources: Sequence[str],
        root: str | None,
        group: bool,
        previous: Integration | None,
        stacks: Stacks,
    ) -> tuple[str, Sequence[str], str | None]:
        if group:
            if sources or root is not None:
                raise StackError("--group supplies both the root and the sources")
            current = self.current_stacked_branch(stacks, "integrate", hint="switch to a stack in a group")
            group_name = stacks.group(stacks.bottom(current))
            if group_name is None:
                raise StackError(f"{current}'s stack isn't in a group")
            remote = self.stack_remote(set(), self.upstream_remotes([stacks.root(current)]))
            root = self.default_branch(remote, from_option=False)
            leaves = (
                Integrations.source_names(previous, stacks)
                if previous and previous["root"] == root and previous.get("group") == group_name
                else stacks.group_leaves(group_name, root)
            )
            if not leaves:
                raise StackError(f"group {group_name} has no stacks on {root}")
            return root, leaves, group_name
        minimum = 1 if previous else 2
        if len(sources) < minimum or len(set(sources)) != len(sources):
            raise StackError(f"an integration needs at least {minimum} distinct local sources")
        return root if root is not None else previous["root"] if previous else "main", sources, None

    def refresh_selection(
        self, branches: Sequence[str], groups: Sequence[str], stacks: Stacks, command: str
    ) -> RefreshSelection:
        """Use ordinary selection, also allowing an integration root as a selector."""
        integrations = Integrations(self)
        names = integrations.names()
        explicit = [name for name in branches if name in names]
        current = self.git.current_branch()
        if not branches and not groups and current in names:
            assert current is not None
            explicit = [current]
        ordinary = [name for name in branches if name not in names]
        retired, replaced = self.retired_source_selectors(ordinary, stacks, integrations, names)
        recovery_remotes: dict[str, set[str]] = {}
        retired_groups = {
            entry["group"]
            for entries in retired.values()
            for _, entry in entries
            if "integration_groups" not in entry and entry["group"] is not None
        }
        selected_groups = set(groups)
        if CURRENT_GROUP in selected_groups and current is not None and current in stacks:
            selected_groups.add(stacks.group(stacks.bottom(current)) or "")
        if groups:
            for name in names:
                config = integrations.read(name)
                assert config is not None
                for entry in config.get("retired_sources", {}).values():
                    recovery_groups = entry.get("integration_groups", [])
                    matching_groups = selected_groups.intersection(recovery_groups)
                    if matching_groups:
                        remotes = recovery_remotes.setdefault(name, set())
                        if "refresh_remote" in entry:
                            remotes.add(entry["refresh_remote"])
                        retired_groups.update(matching_groups)
        ordinary = [name for name in ordinary if name not in replaced]
        groups = [group for group in groups if group not in retired_groups or stacks.bottoms_in(group)]
        selected = (
            self.selected_stacks(ordinary, groups, stacks, command)
            if ordinary or groups or not (explicit or replaced or retired or recovery_remotes)
            else []
        )
        expanded = dict.fromkeys(selected)
        for root in dict.fromkeys(explicit):
            expanded.update(dict.fromkeys(stacks.descendants(root)))
        recovered = frozenset[str]()
        if retired:
            roots = set(explicit) | recovery_remotes.keys() | {stacks.root(b) for b in expanded}
            recovered = self.resume_retired_stacks(retired, replaced, roots, expanded, stacks, recovery_remotes)
        roots = dict.fromkeys([*explicit, *sorted(recovery_remotes), *(stacks.root(b) for b in expanded)])
        return RefreshSelection(
            list(expanded),
            {root: integration for root in roots if (integration := integrations.read(root)) is not None},
            tuple(explicit),
            recovered,
            frozenset(selected_groups),
            recovery_remotes,
        )

    def retired_source_selectors(
        self, ordinary: Sequence[str], stacks: Stacks, integrations: Integrations, names: set[str]
    ) -> tuple[dict[str, list[tuple[str, RetiredSource]]], set[str]]:
        """Allow selectors that matched sources removed by an earlier sync."""
        missing = {name for name in ordinary if name not in stacks}
        if not missing:
            return {}, set()
        owners: dict[str, list[tuple[str, RetiredSource]]] = {}
        for name in names:
            config = integrations.read(name)
            assert config is not None
            for branch, entry in config.get("retired_sources", {}).items():
                owners.setdefault(branch, []).append((name, entry))
        retired = {branch: owners[branch] for branch in missing.intersection(owners)}
        replaced = set(retired)
        for selector in filter(is_glob, missing):
            matched = [
                owner
                for branch, entries in owners.items()
                if fnmatch.fnmatchcase(branch, selector)
                for owner in entries
            ]
            if matched:
                retired[selector] = matched
                if not any(fnmatch.fnmatchcase(branch, selector) for branch in stacks):
                    replaced.add(selector)
        return retired, replaced

    def resume_retired_stacks(
        self,
        retired: dict[str, list[tuple[str, RetiredSource]]],
        replaced: set[str],
        roots: set[str],
        expanded: dict[str, None],
        stacks: Stacks,
        recovery_remotes: dict[str, set[str]],
    ) -> frozenset[str]:
        """Keep surviving parts of a selected source stack in the retry's scope."""
        recovered: set[str] = set()
        for selector, owners in retired.items():
            matched = False
            for name, entry in owners:
                branches = (
                    self.surviving_source_members(entry["members"], entry["parent"], stacks)
                    if "members" in entry
                    else [
                        branch
                        for bottom in stacks.continuation_bottoms(entry["parent"], entry["group"])
                        for branch in stacks.stack_of(bottom, groups=False)
                    ]
                )
                if roots and name not in roots and all(stacks.root(branch) != name for branch in branches):
                    continue
                matched = True
                remotes = recovery_remotes.setdefault(name, set())
                if "refresh_remote" in entry:
                    remotes.add(entry["refresh_remote"])
                recovered.update(branches)
            if not matched and selector in replaced:
                raise StackError(f"{selector} is no longer tracked. Name the work stack to resume its integration.")
        if recovered:
            expanded.update(dict.fromkeys(branch for branch in stacks.in_order() if branch in recovered))
        return frozenset(recovered)

    def surviving_source_members(self, recorded: Sequence[str], bottom: str | None, stacks: Stacks) -> list[str]:
        """Recover only recorded branches still in a stack from the original scope."""
        bottoms = {*recorded, bottom}
        return [branch for branch in recorded if branch in stacks and stacks.bottom(branch) in bottoms]

    def validate_integrations(self, stacks: Stacks) -> None:
        """Reject stack changes that would make an integration depend on another."""
        integrations = Integrations(self)
        names = integrations.names()
        if not names:
            return
        previous = self.stacks()
        for name in names:
            config = integrations.read(name)
            assert config is not None
            root = config["root"]
            for branch in (name, root):
                if branch in stacks and branch not in previous:
                    raise StackError(f"{branch} must remain an untracked root for integration {name}")
            for branch in integrations.source_dependencies(config, previous):
                root_after = stacks.root(branch)
                if root_after in names and root_after != previous.root(branch):
                    raise StackError(f"{branch} is an integration or depends on one")

    def preflight_integrations(
        self, selection: RefreshSelection, stacks: Stacks, force: str | None = None
    ) -> dict[str, str | None]:
        """Check each integration before any selected branch can be updated."""
        integrations = Integrations(self)
        expected: dict[str, str | None] = {}
        names = integrations.names()
        for name in selection.integrations:
            config = integrations.preflight(name, force=name == force)
            tip = self.git.tip(name)
            if tip != config["tip"] and name != force:
                raise StackError(f"integration {name} moved during preflight; retry")
            integrations.validate(
                name,
                config["root"],
                integrations.source_names(config, stacks),
                stacks,
                config["tip"] if config["root_tip"] else None,
                names=names,
            )
            selection.integrations[name] = config
            expected[name] = tip
        return expected

    def preflight_integration_worktrees(
        self,
        selection: RefreshSelection,
        expected: dict[str, str | None],
        update_roots: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        """Catch known blocked rebuilds before changing the trunk or source refs."""
        git = self.git
        integrations = Integrations(self)
        for name, config in selection.integrations.items():
            if (path := git.other_worktree_of(name)) is None:
                continue
            root = config["root"]
            upstream = self.trunk_upstream(root) if root in update_roots else None
            root_tip = self.fast_forward_target(root, upstream) or f"refs/heads/{root}"
            if (
                expected[name] != config["tip"]
                or git.rev(root_tip) != config["root_tip"]
                or (
                    integrations.group_inputs_changed(config, self.stacks())
                    if "group" in config
                    else any(
                        (tip := git.rev(f"refs/heads/{source['name']}")) != source["tip"]
                        and (tip is not None or not source["landing"])
                        for source in config["sources"]
                    )
                )
            ):
                raise StackError(f"can't move integration {name}: checked out in another worktree at {path}")

    def observe_integration_inputs(
        self,
        integrations: Integrations,
        name: str,
        config: Integration,
        sources: list[Source],
        remote: str,
        *,
        fetch: bool,
        prior: Integration | None = None,
    ) -> tuple[Integration, dict[str, str | None]]:
        root = config["root"]
        root_remote = self.git.config_get(root, "remote")
        evidence_remote = root_remote if root_remote and root_remote != "." else remote
        if prior is not None:
            evidence = {source["name"]: source for source in prior["sources"]}
            sources = [evidence.get(source["name"], source) for source in sources]
        return integrations.inputs(
            name, root, sources, evidence_remote, fetch, previous=prior or config, group=config.get("group")
        )

    def select_restack_dependencies(
        self, selection: RefreshSelection, stacks: Stacks, remote: str
    ) -> dict[str, list[str]]:
        """Plan source restacks while the original tips can still prove landing."""
        integrations = Integrations(self)
        for name, config in selection.integrations.items():
            if not any(source in stacks for source in integrations.source_names(config, stacks)):
                continue
            sources = integrations.selected_sources(config, stacks)
            inputs, _ = self.observe_integration_inputs(integrations, name, config, sources, remote, fetch=False)
            selection.restack_inputs[name] = inputs
        # A landed input can also be an ancestor of an active input. Its
        # exact tip must stay put to preserve the integration's landing proof.
        landed = {
            source["name"]
            for inputs in selection.restack_inputs.values()
            for source in inputs["sources"]
            if source["landing"]
        }
        dependencies = {
            name: list(
                dict.fromkeys(
                    branch
                    for source in inputs["sources"]
                    for branch in reversed(
                        list(
                            takewhile(
                                lambda ancestor: ancestor in stacks and ancestor not in landed,
                                stacks.ancestry(source["name"]),
                            )
                        )
                    )
                )
            )
            for name, inputs in selection.restack_inputs.items()
        }
        selection.branches = list(
            dict.fromkeys([*(branch for branches in dependencies.values() for branch in branches), *selection.branches])
        )
        return dependencies

    def preflight_restack_dependencies(
        self, dependencies: dict[str, list[str]], unmerged: set[str], stacks: Stacks, latest: bool
    ) -> None:
        """Don't move a source when its integration can't accept changed inputs."""
        integrations = Integrations(self)
        for name, branches in dependencies.items():
            path = self.git.other_worktree_of(name)
            if path is None and integrations.pending(name) is None:
                continue
            if not any(self.source_needs_restack(branch, stacks, latest) for branch in branches if branch in unmerged):
                continue
            if path is not None:
                raise StackError(f"can't move integration {name}: checked out in another worktree at {path}")
            raise StackError(
                f"integration {name} has a stopped build that needs its current source tips. "
                f"Run `git stack integrate --abort {name}` before restacking its sources."
            )

    def ensure_restack_preserves_landings(
        self, selection: RefreshSelection, unmerged: set[str], stacks: Stacks, latest: bool
    ) -> None:
        """An explicitly selected branch can still contain a proven landed input."""
        for name, inputs in selection.restack_inputs.items():
            for source in inputs["sources"]:
                branch = source["name"]
                if (
                    source["landing"]
                    and branch in unmerged
                    and any(
                        self.source_needs_restack(ancestor, stacks, latest)
                        for ancestor in stacks.ancestry(branch)
                        if ancestor in unmerged
                    )
                ):
                    raise StackError(
                        f"can't restack {branch}: its exact tip is recorded as landed for integration {name}"
                    )

    def source_needs_restack(self, branch: str, stacks: Stacks, latest: bool) -> bool:
        parent = stacks.parent(branch)
        parent_tip = self.git.rev(f"refs/heads/{parent}")
        return parent_tip is not None and self.needs_restack(
            branch, parent, self.recorded_base(branch, parent), parent_tip, latest
        )

    def implicit_source_adapters(
        self, branches: Iterable[str], stacks: Stacks, original: GitAdapter, remote: str
    ) -> dict[str, GitAdapter]:
        """Resolve merge evidence on the implicit sources' remotes."""
        git = self.git
        adapters = {remote: original}
        result: dict[str, GitAdapter] = {}
        for branch in branches:
            source_remote = git.config_get(branch, "remote") or git.config_get(stacks.root(branch), "remote") or remote
            if source_remote not in adapters:
                if isinstance(original, GitHubAdapter) and github_repo(git, source_remote) is None:
                    adapters[source_remote] = GitAdapter(self)
                else:
                    adapters[source_remote] = make_adapter(self, source_remote, fetch=False)
            result[branch] = adapters[source_remote]
        return result

    def refresh_integrations(
        self,
        selection: RefreshSelection,
        remote: str,
        expected: dict[str, str | None],
        *,
        fetch: bool,
    ) -> dict[str, dict[str, str | None]]:
        integrations = Integrations(self)
        verified: dict[str, dict[str, str | None]] = {}
        for name in selection.integrations:
            # Source processing may have updated the stored retry selectors.
            config = integrations.read(name)
            assert config is not None
            selection.integrations[name] = config
            root = config["root"]
            sources = config["sources"]
            if "group" in config:
                stacks = self.stacks()
                sources = integrations.selected_sources(config, stacks)
                generated_tip = config["tip"] if config["root_tip"] else None
                integrations.validate(name, root, [source["name"] for source in sources], stacks, generated_tip)
            desired, observed = self.observe_integration_inputs(
                integrations, name, config, sources, remote, fetch=fetch, prior=selection.restack_inputs.get(name)
            )
            verified[name] = integrations.build(name, desired, observed, config, expected[name])
        return verified

    def validate_restack_selection(self, branches: Sequence[str], groups: Sequence[str], onto: str | None) -> None:
        """Reject invalid selectors before settling interrupted work in other stacks."""
        initial = self.stacks()
        if onto:
            if branches and branches[0] not in initial:
                raise StackError(f"{branches[0]} isn't in a stack; use `git stack track` first")
            if not branches:
                self.current_stacked_branch(initial, "restack", hint="use `git stack track` first")
        else:
            self.refresh_selection(branches, groups, initial, "restack")

    @exclusive
    def restack(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        onto: str | None = None,
        latest: bool = False,
        allow_shared: bool = False,
        force: bool = False,
    ) -> None:
        """Rebase every branch that needs it onto its parent, bottom-up (see
        needs_restack), in the current stack, or in the stacks of the branches
        that branches and groups name (see named). If a selected stack uses
        an integration, first restack the integration's tracked sources and
        their ancestors, then rebuild it.

        onto first retargets one branch: the one branches names, or the
        current one if it names none. It can't be combined with groups or
        more branches. latest moves branches on a stack's root onto its tip
        even if they don't conflict with it.
        """
        if onto and (groups or len(branches) > 1):
            raise StackError(
                "--onto moves one branch: name just that branch (or none, for the current one), without --group"
            )
        force_name = None
        if force:
            named_integrations = set(branches) & Integrations(self).names()
            if len(named_integrations) != 1 or onto:
                raise StackError("--force requires one explicitly named integration branch")
            force_name = named_integrations.pop()
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        Integrations(self).restore_pending_checkout()
        self.validate_restack_selection(branches, groups, onto)
        self.settle_all()
        stacks = self.stacks()
        original = self.current_checkout()
        if onto:
            if branches:
                branch = branches[0]
                if branch not in stacks:
                    raise StackError(f"{branch} isn't in a stack; use `git stack track` first")
            else:
                branch = self.current_stacked_branch(stacks, "restack", hint="use `git stack track` first")
            self.ensure_local_branch_parent(onto)
            stacks.ensure_no_cycle(branch, onto)
            self.ensure_owned(stacks.stack_of(branch, groups=False), stacks, allow_shared, "restack")
            if onto in stacks:
                self.ensure_owned(stacks.stack_of(onto, groups=False), stacks, allow_shared, "restack")
            selected = [branch, *stacks.descendants(branch)]
            # Only in stacks, which isn't saved: restack_branch records the
            # new parent once the rebase has finished.
            was_integrated = self.git.config_get(stacks.root(branch), INTEGRATION_KEY) is not None
            stacks.reparent(branch, onto)
            self.validate_integrations(stacks)
            root = stacks.root(branch)
            config = Integrations(self).read(root)
            selection = RefreshSelection(selected, {root: config} if config else {})
            latest = latest or was_integrated
        else:
            selection = self.refresh_selection(branches, groups, stacks, "restack")
        remote = self.default_remote(selection.branches, stacks)
        adapter = make_adapter(self, remote, fetch=False)
        explicitly_selected = set(selection.branches)
        expected = self.preflight_integrations(selection, stacks, force_name)
        dependencies = self.select_restack_dependencies(selection, stacks, remote)
        self.ensure_owned(selection.branches, stacks, allow_shared, "restack")
        branch_adapters = self.implicit_source_adapters(
            (branch for branch in selection.branches if branch not in explicitly_selected), stacks, adapter, remote
        )
        self.ensure_fresh_trunk_bases(
            selection.branches,
            skip=lambda branch, _parent, _trunk: (
                branch_adapters.get(branch, adapter).merge_state(branch, stacks.parent(branch))
                in {MergeState.MERGED, MergeState.REVERTED}
            ),
        )
        self.preflight_integration_worktrees(selection, expected)
        sources, remaining = selection.ordered(stacks)
        unmerged = self.skip_merged(sources, stacks, adapter, branch_adapters=branch_adapters)
        unmerged_names = set(unmerged)
        self.ensure_restack_preserves_landings(selection, unmerged_names, stacks, latest)
        self.preflight_restack_dependencies(dependencies, unmerged_names, stacks, latest)
        if unmerged:
            blocked = self.restack_all(unmerged, stacks, latest)
            if blocked:
                raise StackError(f"couldn't update selected integration source stacks: {', '.join(blocked)}")
        self.refresh_integrations(selection, remote, expected, fetch=False)
        self.restack_all(self.skip_merged(remaining, stacks, adapter), stacks, latest)
        self.restore_checkout(original)

    def sync_selection(
        self,
        selection: RefreshSelection,
        stacks: Stacks,
        remote: str,
        only_remote: bool,
        command: SyncCommand,
    ) -> tuple[RefreshSelection, set[str]]:
        """Filter a selection to one remote's stacks and their integration roots."""

        def recovered_remotes(name: str) -> set[str]:
            recorded = selection.recovery_remotes[name]
            upstream = self.git.config_get(name, "remote")
            return {upstream} if upstream else recorded

        if only_remote:
            selection.branches = [b for b in selection.branches if self.git.config_get(b, "remote") == remote]
            needed = {stacks.root(b) for b in selection.branches}
            selection.integrations = {
                name: config
                for name, config in selection.integrations.items()
                if name in needed
                or (name in selection.explicit and self.git.config_get(name, "remote") in (None, remote))
                or (
                    name in selection.recovery_remotes
                    and (not (recorded := recovered_remotes(name)) or remote in recorded)
                )
            }
            if not selection.branches and not selection.integrations:
                raise StackError(f"none of the selected branches track {remote}")
        remotes = self.upstream_remotes([*selection.branches, *selection.integrations])
        if not only_remote:
            for name in selection.recovery_remotes.keys() & selection.integrations.keys():
                remotes.update(recovered_remotes(name) - {"."})
        if len(remotes) > 1 or (only_remote and remotes - {remote}):
            raise StackError(f"can't {command.value} branches on multiple remotes atomically; select one with --remote")
        return selection, remotes

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

    def protected_sync_destinations(
        self, branches: list[str], roots: set[str], remote: str, *, known_default: str | None = None
    ) -> set[str]:
        """Those of branches whose upstream is remote's default branch, or the
        remote branch of one of roots, which only --force pushes to. Fails if
        any of them tracks remote and the server can't say what its default
        branch is."""
        git = self.git
        if not any(git.config_get(b, "remote") == remote for b in branches):
            return set()
        defaults = git.push_default_branches(remote, known=known_default)
        if defaults is None:
            raise StackError(
                f"{remote}'s default branch isn't known (couldn't ask {remote}, or its HEAD isn't a branch); "
                "use --force to allow the configured upstream"
            )
        protected = defaults | {git.remote_branch_name(root, remote) for root in roots}
        return {
            b
            for b in branches
            if git.config_get(b, "remote") == remote and git.remote_branch_name(b, remote) in protected
        }

    def ensure_sync_destination(
        self,
        branches: list[str],
        roots: set[str],
        remote: str,
        force: bool,
        command: str = "sync",
        *,
        known_default: str | None = None,
    ) -> None:
        if force:
            return
        blocked = sorted(self.protected_sync_destinations(branches, roots, remote, known_default=known_default))
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
        no_verify: bool = False,
        protection: PRProtection = DEFAULT_PR_PROTECTION,
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

        With push, raises StackError if another worktree blocks a branch or its parent.
        For an ordinary stack, pushes the remaining eligible branches first.
        A blocked integration dependent prevents the atomic push.
        """
        # Without push, it's `git stack pull`, which its messages must suggest
        # rerunning rather than sync, which would push.
        command = SyncCommand.SYNC if push else SyncCommand.PULL
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        Integrations(self).restore_pending_checkout()
        stacks = self.stacks()
        if only_remote:
            initial = self.refresh_selection(branches, groups, stacks, command.value)
            initial, _ = self.sync_selection(initial, stacks, remote, only_remote, command)
            for branch in initial.branches:
                self.settle(branch)
        else:
            self.settle_all()
        stacks = self.stacks()
        selection = self.refresh_selection(branches, groups, stacks, command.value)
        self.ensure_owned(selection.branches, stacks, allow_shared, command.value)
        selection, upstream_remotes = self.sync_selection(selection, stacks, remote, only_remote, command)
        expected = self.preflight_integrations(selection, stacks)
        stack = selection.branches
        retired = {
            branch
            for config in selection.integrations.values()
            for branch in config.get("retired_sources", {})
            if branch not in stacks
        }
        self.ensure_ignorable_reverts(ignore_reverts, [*stack, *retired], "the stacks being synced")
        roots = {stacks.root(b) for b in stack} | selection.integrations.keys()
        ordinary_roots = {config["root"] for config in selection.integrations.values()}
        roots_to_update = ordinary_roots | {
            root
            for root in roots - selection.integrations.keys()
            if not only_remote or self.git.config_get(root, "remote") == remote
        }
        root_remotes = self.upstream_remotes(roots_to_update)
        push_remote = remote if only_remote else self.stack_remote(upstream_remotes, root_remotes)
        adapter = self.checked_adapter(push_remote)
        protection = self.configure_pr_protection(adapter, protection, force)
        known_default = (
            adapter.default_branch_for_push() if push and not force and isinstance(adapter, GitHubAdapter) else None
        )
        destinations = [*stack, *selection.integrations]
        protected_roots = roots | ordinary_roots
        protected = (
            self.protected_sync_destinations(destinations, protected_roots, push_remote, known_default=known_default)
            if push and not force
            else set[str]()
        )
        original = self.current_checkout()
        remotes = {push_remote} | upstream_remotes | root_remotes
        for name in sorted(remotes, key=lambda name: (name != remote, name)):
            self.fetch(name)
        # Fetch may reveal a newer fork before the local trunk moves.
        self.check_sync_trunk_bases(stack, adapter, roots_to_update)
        if self.move_off_deleted_parents(stack, stacks, adapter, push_remote, selection, expected):
            roots = {stacks.root(b) for b in stack} | selection.integrations.keys()
            ordinary_roots = {config["root"] for config in selection.integrations.values()}
            protected_roots = roots | ordinary_roots
            destinations = [*stack, *selection.integrations]
            roots_to_update = ordinary_roots | {
                r
                for r in roots - selection.integrations.keys()
                if not only_remote or self.git.config_get(r, "remote") == remote
            }
            for name in sorted(self.upstream_remotes(roots_to_update) - remotes):
                self.fetch(name)
            if push and not force:
                protected = self.protected_sync_destinations(
                    destinations, protected_roots, push_remote, known_default=known_default
                )
        integration_leases = self.integration_leases(selection, push_remote, force, protected_roots) if push else {}
        # A group's stacks can be on different roots.
        self.preflight_integration_worktrees(selection, expected, roots_to_update)
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
            no_verify,
        )
        state.protected_deletes = self.integration_deletion_refs(delete, roots, push_remote)
        state.check_delete = lambda deletion: self.protect_deletion(deletion, adapter, protection, push_remote)
        self.refresh_and_push(selection, expected, adapter, state, integration_leases, rebuild_stack, protection)
        if push and state.blocked:
            raise StackError(
                f"didn't push {', '.join(state.blocked)}: checked out in another worktree, or stacked on a branch "
                f"that is"
            )
        if selection.integrations:
            shown = self.branch_infos(stacks.restricted(stack), adapter, selection.integrations)
        else:
            shown = self.branch_infos(stacks.restricted(stack), adapter)
        return SyncResult(state.unpushed, shown)

    def integration_deletion_refs(
        self, delete: Delete | None, roots: Iterable[str], fallback_remote: str
    ) -> frozenset[tuple[str, str]]:
        """Keep the remote branches used as an integration or its configured root."""
        if not delete or not delete.deletes_remote:
            return frozenset()
        integrations = Integrations(self)
        names = integrations.names()
        if not names:
            return frozenset()
        configured = {config["root"] for name in names if (config := integrations.read(name)) is not None}
        protected: set[tuple[str, str]] = set()
        for name in {*roots, *names, *configured}:
            upstream = self.git.config_get(name, "remote")
            remote = upstream if upstream and upstream != "." else fallback_remote
            protected.add((remote, f"refs/heads/{self.git.remote_branch_name(name, remote)}"))
        return frozenset(protected)

    def check_sync_trunk_bases(self, stack: list[str], adapter: GitAdapter, roots_to_update: set[str]) -> None:
        """Reject stale bases only when sync would need to replay the branch."""

        def skip_replay(branch: str, parent: str, trunk: str) -> bool:
            if parent in roots_to_update and trunk != self.git.rev(f"refs/heads/{parent}"):
                target = self.fast_forward_target(parent, self.trunk_upstream(parent))
                if target is not None:
                    return self.lands_on_updated_trunk(adapter, branch, parent, trunk)
            return adapter.merge_state(branch, parent) is not MergeState.NOT_MERGED

        self.ensure_fresh_trunk_bases(stack, skip=skip_replay)

    def lands_on_updated_trunk(self, adapter: GitAdapter, branch: str, parent: str, trunk: str) -> bool:
        """Recognize a landing against the commit sync will install as the local trunk."""
        git = self.git
        if isinstance(adapter, GitHubAdapter) and (pr := adapter.pull_requests().get(branch)):
            if (
                pr.state is GitHubState.OPEN
                and git.remote_branch_name(parent, adapter.remote) == adapter.default_branch
            ):
                return False
            if (
                pr.state is GitHubState.MERGED
                and pr.merge_oid
                and git.rev(pr.head_oid)
                and git.is_ancestor(pr.merge_oid, trunk)
            ):
                return True
        if not (git.upstream_gone(branch) or adapter.changes_something(branch)):
            return False
        return git.contained_in(f"refs/heads/{branch}", trunk) or adapter.landed_as_patches(branch, trunk)

    def refresh_and_push(
        self,
        selection: RefreshSelection,
        expected: dict[str, str | None],
        adapter: GitAdapter,
        state: SyncState,
        leases: dict[str, tuple[str, str | None]],
        rebuild_stack: bool,
        protection: PRProtection,
    ) -> None:
        integrations = Integrations(self)
        pending: dict[str, QueuedRemoteDeletion] = {}
        if selection.integrations and state.delete and state.delete.deletes_remote:
            pending, known = self.retry_deletions(selection, state, integrations)
            journal = next(iter(selection.integrations))

            def defer(deletion: RemoteDeletion) -> None:
                entry = known.setdefault(deletion.ref, QueuedRemoteDeletion(deletion))
                entry.add(journal, deletion)
                integrations.remember_deletion(journal, entry.deletion)
                pending[deletion.ref] = entry

            state.defer_delete = defer
        try:
            verified = self.sync_selected(selection, expected, adapter, state)
            self.restore_checkout(state.original)
            failed = [b for b in dict.fromkeys([*state.blocked, *state.broken]) if state.stacks.root(b) in leases]
            if failed:
                raise StackError(f"didn't push: couldn't update integration work on {', '.join(failed)}")
            integration_pushes = [
                IntegrationPush(name, target, remote_tip, verified[name])
                for name, (target, remote_tip) in leases.items()
            ]
            deletions = self.ready_remote_deletions(pending, state, adapter)
            self.push_branches(
                state.pushable,
                state.stacks,
                adapter,
                state.remote,
                rebuild_stack=rebuild_stack,
                scope=selection.branches,
                use_upstreams=True,
                no_verify=state.no_verify,
                integration_pushes=integration_pushes,
                deletions=deletions,
                protection=protection,
            )
            if not (state.blocked or state.broken) and len(deletions) == len(pending):
                integrations.clear_retired_sources(leases, state.stacks, state.scope)
        finally:
            for entry in pending.values():
                deletion = entry.deletion
                if self.git.rev(f"refs/remotes/{deletion.upstream}") is None:
                    for journal in entry.journals:
                        integrations.forget_deletion(journal, deletion)
                else:
                    self.emit(events.RemoteBranchKept(deletion.upstream, deletion.remote, deletion.branch))

    def retry_deletions(
        self, selection: RefreshSelection, state: SyncState, integrations: Integrations
    ) -> tuple[dict[str, QueuedRemoteDeletion], dict[str, QueuedRemoteDeletion]]:
        """Retry only the exact remote tips a previous sync was authorized to delete."""
        known: dict[str, QueuedRemoteDeletion] = {}
        pending: dict[str, QueuedRemoteDeletion] = {}
        recorded: list[tuple[str, RemoteDeletion]] = []
        for journal, deletion in integrations.pending_deletions(integrations.names(), state.remote):
            known.setdefault(deletion.ref, QueuedRemoteDeletion(deletion)).add(journal, deletion)
            if journal in selection.integrations:
                recorded.append((journal, deletion))
        if not recorded:
            return pending, known
        defaults = self.git.push_default_branches(state.remote)
        if defaults is None:
            raise StackError(f"can't determine {state.remote}'s default branch; refusing queued deletions")
        for journal, deletion in recorded:
            current = self.git.rev(f"refs/remotes/{deletion.upstream}")
            protected = (deletion.remote, deletion.ref) in state.protected_deletes
            if current == deletion.tip and deletion.branch not in defaults and not protected:
                entry = known[deletion.ref]
                entry.add(journal, deletion)
                pending[deletion.ref] = entry
            elif current is None:
                for owner in known[deletion.ref].journals:
                    integrations.forget_deletion(owner, deletion)
            else:
                integrations.forget_deletion(journal, deletion)
                self.emit(events.RemoteBranchKept(deletion.upstream, deletion.remote, deletion.branch))
        return pending, known

    def ready_remote_deletions(
        self, pending: dict[str, QueuedRemoteDeletion], state: SyncState, adapter: GitAdapter
    ) -> list[RemoteDeletion]:
        """Keep a remote base until its surviving children can be published and retargeted."""
        ready: list[RemoteDeletion] = []
        pushable = set(state.pushable)
        unavailable = state.skipped | set(state.blocked)
        pending_refs = frozenset(pending)
        prs = adapter.pull_requests() if pending and isinstance(adapter, GitHubAdapter) else {}
        for entry in pending.values():
            deletion = entry.deletion
            children = deletion.children
            if children is None:
                # Older records can only be retried with all potential children on this remote selected.
                children = tuple(
                    (b, self.git.remote_branch_name(b, state.remote))
                    for b in state.stacks
                    if self.git.config_get(b, "remote") in (None, state.remote)
                )
            if any(
                not self.deletion_child_ready(child, deletion, state, unavailable, prs, pending_refs)
                for child in children
            ):
                continue
            ready.append(deletion)
            for branch, _ in children:
                if (
                    branch in state.stacks
                    and branch not in pushable
                    and self.git.has_branch_upstream(branch, state.remote)
                ):
                    # Even an unchanged child may need its open PR retargeted.
                    state.pushable.append(branch)
                    pushable.add(branch)
        return ready

    def deletion_child_ready(
        self,
        child: tuple[str, str | None],
        deletion: RemoteDeletion,
        state: SyncState,
        unavailable: set[str],
        prs: dict[str, PullRequest],
        pending_refs: frozenset[str],
    ) -> bool:
        branch, old_upstream = child
        if branch in state.confirmed_merged and state.confirmed_merged[branch] == old_upstream:
            return True
        current_upstream = (
            self.git.remote_branch_name(branch, state.remote)
            if self.git.has_branch_upstream(branch, state.remote)
            else None
        )
        if (
            old_upstream
            and (branch not in state.stacks or current_upstream != old_upstream)
            and self.git.rev(f"refs/remotes/{state.remote}/{old_upstream}") is not None
        ):
            return False
        if branch not in state.stacks:
            return True
        if branch not in state.scope or branch in unavailable:
            return False
        pr = prs.get(branch)
        if pr is None or pr.state is not GitHubState.OPEN or pr.base != deletion.branch:
            return True
        parent = self.git.remote_branch_name(state.stacks.parent(branch), state.remote)
        return (
            f"refs/heads/{parent}" not in pending_refs
            and self.git.rev(f"refs/remotes/{state.remote}/{parent}") is not None
        )

    def sync_selected(
        self, selection: RefreshSelection, expected: dict[str, str | None], adapter: GitAdapter, state: SyncState
    ) -> dict[str, dict[str, str | None]]:
        """Sync selected sources first, then refresh roots before touching their work branches."""
        sources, remaining = selection.ordered(state.stacks)
        preserve_source = self.source_retirement(selection, sources, state.stacks, state.remote)
        for branch in sources:
            self.sync_branch(branch, adapter, state, preserve_source)
        if selection.integrations:
            self.restore_checkout(state.original)
        failed = dict.fromkeys([*state.blocked, *state.broken])
        if failed:
            raise StackError(f"couldn't update selected integration source stacks: {', '.join(failed)}")
        verified = self.refresh_integrations(selection, state.remote, expected, fetch=True)
        for branch in remaining:
            self.sync_branch(branch, adapter, state)
        return verified

    def source_retirement(
        self, selection: RefreshSelection, branches: Sequence[str], stacks: Stacks, remote: str
    ) -> Callable[[str], None]:
        """Capture merged sources and the selectors for their surviving stacks."""
        integrations = Integrations(self)
        # A selected, merged source may be deleted while syncing. Preserve the
        # live input and its upstream identity so exact landing proof is still required.
        selected_sources = set(branches)
        members_by_bottom: dict[str, list[str]] = {}
        groups_by_root: dict[str, set[str]] = {}
        for branch in selection.branches:
            bottom = stacks.bottom(branch)
            members_by_bottom.setdefault(bottom, []).append(branch)
            group = stacks.group(bottom)
            if group is not None and group in selection.selected_groups:
                groups_by_root.setdefault(stacks.root(bottom), set()).add(group)
        inputs: dict[str, list[tuple[str, Source]]] = {}
        for name, config in selection.integrations.items():
            for source in integrations.selected_sources(config, stacks):
                if source["name"] in selected_sources:
                    inputs.setdefault(source["name"], []).append((name, source))

        source_members = {branch: members_by_bottom[stacks.bottom(branch)] for branch in inputs}

        def preserve_source(branch: str) -> None:
            entries = inputs.get(branch)
            if not entries:
                return
            tip = self.git.tip(branch)
            parent = stacks.parent(branch)
            bottom = stacks.bottom(branch)
            group = stacks.group(bottom)
            if parent not in stacks and not group and stacks.children(branch):
                # remove_merged gives the children a group named after their old parent.
                group = branch
            for name, source in entries:
                config = integrations.read(name)
                assert config is not None
                refresh_groups = groups_by_root.get(name, set()) | (
                    selection.selected_groups & {config.get("group", "")}
                )
                config.setdefault("retired_sources", {})[branch] = RetiredSource(
                    source=integrations.source(branch, tip, source),
                    parent=bottom if parent in stacks else None,
                    group=group,
                    members=source_members[branch],
                    integration_groups=sorted(refresh_groups),
                    refresh_remote=remote,
                )
                integrations.save(name, config)

        return preserve_source

    def integration_leases(
        self, selection: RefreshSelection, remote: str, force: bool, roots: set[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Pin the integration destinations before rebuilding their local refs."""
        git = self.git
        self.recover_first_pushes(selection.integrations, remote)
        self.ensure_remote_branches_ours(list(selection.integrations), remote)
        protected = {git.remote_branch_name(root, remote) for root in roots - selection.integrations.keys()}
        if selection.integrations and not force:
            defaults = git.push_default_branches(remote)
            if defaults is None:
                raise StackError(f"{remote}'s default branch isn't known; rerun `git stack sync --force`")
            protected.update(defaults)
        leases: dict[str, tuple[str, str | None]] = {}
        for name, config in selection.integrations.items():
            target = git.remote_branch_name(name, remote)
            if not force and target in protected:
                raise StackError(f"{name} targets a protected remote branch; rerun `git stack sync --force`")
            remote_tip = git.rev(f"refs/remotes/{remote}/{target}")
            known = self.pulled_tip(name)
            safe_tip = (
                remote_tip in (known, config["tip"])
                if known
                else (not remote_tip or git.is_ancestor(remote_tip, config["tip"]))
            )
            if remote_tip and not force and not safe_tip:
                raise StackError(
                    f"{remote}/{target} has unexpected commits; rerun `git stack sync --force` to replace it"
                )
            leases[name] = target, remote_tip
        return leases

    def recover_first_pushes(self, names: Iterable[str], remote: str) -> None:
        """Finish bookkeeping when the server accepted the exact planned publication."""
        integrations = Integrations(self)
        git = self.git
        for name in names:
            intent = integrations.first_push(name)
            if not intent:
                continue
            chosen_remote, target, tip = intent
            if chosen_remote != remote or git.rev(f"refs/remotes/{remote}/{target}") != tip:
                continue
            configured = git.config_get(name, "remote")
            if configured is None or (configured == remote and git.remote_branch_name(name, remote) == target):
                self.record_published_branch(integrations, name, remote, target, tip)

    def move_off_deleted_parents(
        self,
        branches: Sequence[str],
        stacks: Stacks,
        adapter: GitAdapter,
        remote: str,
        selection: RefreshSelection | None = None,
        expected: dict[str, str | None] | None = None,
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
            self.check_recovered_integration(new_parent, stacks, remote, selection, expected)
            self.save(stacks)
            self.emit(events.MovedOffDeletedParent(branch, parent, new_parent))
            moved = True
        return moved

    def check_recovered_integration(
        self,
        parent: str,
        stacks: Stacks,
        remote: str,
        selection: RefreshSelection | None,
        expected: dict[str, str | None] | None,
    ) -> None:
        """Check a managed root found during recovery before saving the new parent."""
        root = stacks.root(parent)
        config = Integrations(self).read(root)
        if config is None:
            return
        self.validate_integrations(stacks)
        if selection is None or expected is None or root in selection.integrations:
            return
        if self.upstream_remotes([root]) - {remote}:
            raise StackError(f"recovered integration {root} tracks another remote; select its upstream remote")
        added = RefreshSelection([], {root: config})
        expected.update(self.preflight_integrations(added, stacks))
        selection.integrations.update(added.integrations)

    def sync_branch(
        self, branch: str, adapter: GitAdapter, state: SyncState, preserve_source: Callable[[str], None] | None = None
    ) -> None:
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
            self.sync_merged(branch, parent, adapter, state, preserve_source)
        elif merge_state is MergeState.MERGED_WITH_NEW_COMMITS:
            # Restacking would replay the merged commits onto parent.
            self.emit(events.MergedWithNewCommits(branch, parent))
            state.skipped.add(branch)
        elif merge_state is MergeState.REVERTED:
            self.emit(self.reverted(branch, state.stacks, adapter))
            state.skipped.add(branch)
        else:
            self.sync_unmerged(branch, parent, state)

    def sync_merged(
        self,
        branch: str,
        parent: str,
        adapter: GitAdapter,
        state: SyncState,
        preserve_source: Callable[[str], None] | None,
    ) -> None:
        outside = [b for b in state.stacks.children(branch) if b not in state.scope]
        if outside:
            raise StackError(
                f"can't untrack {branch}: it has children outside the selected remote: {', '.join(outside)}"
            )
        confirmed = adapter.merge_confirmed(branch, parent)
        merged_upstream = (
            self.git.remote_branch_name(branch, state.remote)
            if confirmed and state.defer_delete is not None and self.git.has_branch_upstream(branch, state.remote)
            else None
        )
        if preserve_source is not None:
            preserve_source(branch)
        if not self.untrack_merged(
            branch,
            state.stacks,
            state.delete,
            allow_different_upstream=True,
            confirmed=confirmed,
            no_verify=state.no_verify,
            defer=state.defer_delete,
            check_delete=state.check_delete,
            protected_refs=state.protected_deletes,
        ):
            state.skipped.add(branch)
            return
        if confirmed:
            state.confirmed_merged[branch] = merged_upstream
        if branch == state.original:
            state.original = self.current_checkout()

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
            state.broken.append(branch)
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
        no_verify: bool = False,
        protection: PRProtection = DEFAULT_PR_PROTECTION,
    ) -> list[BranchInfo]:
        """Fetch and untrack merged branches in every stack, deleting what delete
        says, without restacking anything. ignore_reverts is as for sync; no_verify
        skips the pre-push hook when deleting remote branches.
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
        protected_deletes = self.integration_deletion_refs(delete, stacks.roots(), remote)

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
                if (
                    self.untrack_merged(
                        branch,
                        stacks,
                        delete,
                        confirmed=confirmed,
                        no_verify=no_verify,
                        check_delete=lambda deletion: self.protect_deletion(deletion, adapter, protection, remote),
                        protected_refs=protected_deletes,
                    )
                    and branch == original
                ):
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
        no_verify: bool = False,
        protection: PRProtection = DEFAULT_PR_PROTECTION,
    ) -> PushResult:
        """Force-push (with lease) every unmerged branch in the current stack, or
        in the stacks of the branches that branches and groups name (see named), then let
        the adapter update their pull requests. Returns the pushed branches,
        which is empty if every branch looks merged, and the stacks. With prs,
        then opens their pull requests as prs does; without the GitHub adapter,
        it pushes anyway, and the result's error says it opened none.

        If the selected work uses an integration, rebuild it and restack that
        work before pushing both together. Unless force is set, a branch whose
        remote branch has its commits rebased (see take_upstream_rebase) is moved
        there first. Refuses if a remote branch has commits the local one doesn't,
        unless force is set. Either way, the push fails if a remote branch changed
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
        protection = self.configure_pr_protection(adapter, protection, force)
        github = adapter if prs and isinstance(adapter, GitHubAdapter) else None
        roots = {stacks.root(branch) for branch in stack}
        known_default = adapter.default_branch_for_push() if not force and isinstance(adapter, GitHubAdapter) else None
        self.ensure_sync_destination(stack, roots, push_remote, force, "push", known_default=known_default)
        integrations = Integrations(self)
        selection = RefreshSelection(
            stack, {root: config for root in sorted(roots) if (config := integrations.read(root)) is not None}
        )
        branches, integration_pushes = self.prepare_push(selection, stacks, adapter, push_remote, force)
        pushed = self.push_branches(
            branches,
            stacks,
            adapter,
            push_remote,
            force,
            opening=github is not None,
            rebuild_stack=rebuild_stack,
            use_upstreams=True,
            no_verify=no_verify,
            protection=protection,
            integration_pushes=integration_pushes,
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
        return PushResult([*(push.name for push in integration_pushes), *pushed], shown, steps, error)

    def prepare_push(
        self, selection: RefreshSelection, stacks: Stacks, adapter: GitAdapter, remote: str, force: bool
    ) -> tuple[list[str], list[IntegrationPush]]:
        """Prepare selected work and any generated bases for a single push."""
        if not selection.integrations:
            return self.take_branches_for_push(selection.branches, stacks, adapter, remote, force), []
        self.ensure_clean_worktree()
        expected = self.preflight_integrations(selection, stacks)
        self.preflight_integration_worktrees(selection, expected)
        roots = {stacks.root(branch) for branch in selection.branches}
        protected = roots | {config["root"] for config in selection.integrations.values()}
        self.ensure_sync_destination(selection.branches, protected, remote, force, "push")
        self.fetch_missing_integration_upstreams(selection.integrations, remote)
        leases = self.integration_leases(selection, remote, force, protected)
        sources, remaining = selection.ordered(stacks)
        source_branches = self.take_branches_for_push(sources, stacks, adapter, remote, force)
        verified = self.refresh_integrations(selection, remote, expected, fetch=False)
        work_branches = self.take_branches_for_push(remaining, stacks, adapter, remote, force)
        integrated_work = [b for b in work_branches if stacks.root(b) in selection.integrations]
        self.restack_work_for_push(integrated_work, stacks)
        return [*source_branches, *work_branches], [
            IntegrationPush(name, target, tip, verified[name]) for name, (target, tip) in leases.items()
        ]

    def take_branches_for_push(
        self, branches: list[str], stacks: Stacks, adapter: GitAdapter, remote: str, force: bool
    ) -> list[str]:
        remaining = self.skip_merged(branches, stacks, adapter)
        if not force:
            for branch in remaining:
                self.take_upstream_rebase(branch, stacks.parent(branch), remote)
        return remaining

    def restack_work_for_push(self, branches: list[str], stacks: Stacks) -> None:
        """Ensure every selected work branch includes its newly generated base."""
        if blocked := self.restack_all(branches, stacks):
            raise StackError(f"couldn't restack {', '.join(blocked)}; nothing was pushed")
        git = self.git
        for branch in branches:
            root = stacks.root(branch)
            if not git.is_ancestor(git.tip(root), git.tip(branch)):
                raise StackError(f"{branch} isn't based on the latest {root}; restack its full stack before pushing")

    def fetch_missing_integration_upstreams(self, names: Iterable[str], remote: str) -> None:
        """Restore missing tracking refs for published integrations without fetching work branches."""
        git = self.git
        targets = [git.remote_branch_name(name, remote) for name in names if git.config_get(name, "remote") == remote]
        missing = {f"refs/heads/{target}": f"refs/remotes/{remote}/{target}" for target in targets}
        missing = {ref: tracking for ref, tracking in missing.items() if git.rev(tracking) is None}
        if missing:
            found = git.run("ls-remote", "--heads", "--", remote, *missing).splitlines()
            refspecs = [f"{ref}:{missing[ref]}" for line in found if (ref := line.partition("\t")[2]) in missing]
            if refspecs:
                git.run("fetch", "--quiet", "--no-write-fetch-head", remote, *refspecs)

    def push_refspecs(
        self,
        branches: list[str],
        stacks: Stacks,
        remote: str,
        force: bool,
        use_upstreams: bool,
        work_tips: dict[str, str] | None = None,
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
            source = work_tips[b] if work_tips is not None else f"refs/heads/{b}"
            refspecs.append(f"{source}:refs/heads/{target}" if use_upstreams else b)
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
        no_verify: bool = False,
        integration_pushes: Sequence[IntegrationPush] = (),
        deletions: list[RemoteDeletion] | None = None,
        protection: PRProtection = DEFAULT_PR_PROTECTION,
    ) -> list[str]:
        """Force-push (with lease) branches, which must be unmerged, in one atomic
        push, then let the adapter update their pull requests (see
        GitAdapter.after_push for opening). Returns branches.

        Checks and fails as push does.
        """
        git = self.git
        if not branches and not integration_pushes and not deletions:
            return []
        if integration_pushes:
            self.recover_first_pushes(branches, remote)
        self.ensure_remote_branches_ours(branches, remote)

        # Force-pushing is only safe if we've seen everything on the remote branch.
        # Refuse if we know of commits we haven't pulled (unless told to drop
        # them), and pin each lease to the remote-tracking ref we checked, so a
        # push that raced with someone else's fails instead of overwriting it.
        work_tips = {branch: git.tip(branch) for branch in branches}
        pinned = work_tips if integration_pushes else None
        leases, refspecs = self.push_refspecs(branches, stacks, remote, force, use_upstreams, pinned)
        self.add_integration_refspecs(integration_pushes, deletions or (), leases, refspecs, remote)
        integrations = Integrations(self)
        for integration in integration_pushes:
            integrations.verify(integration.refs)
        push_urls = git.push_urls(remote)
        multiple_destinations = len(push_urls) > 1
        self.protect_push(leases, refspecs, remote, adapter, protection, push_urls)
        if opening and isinstance(adapter, GitHubAdapter):
            self.protect_pr_opening(branches, stacks, adapter)
        self.retarget_before_push(
            branches,
            stacks,
            adapter,
            remote,
            leases,
            refspecs,
            rebuild_stack,
            scope,
            opening,
            bool(integration_pushes),
            multiple_destinations,
        )
        # Atomic, so a branch that can't be pushed doesn't leave the rest of the
        # stack pushed without it, and its pull requests not retargeted.
        verify_options = ["--no-verify"] if no_verify else []
        for integration in integration_pushes:
            integrations.verify(integration.refs)
        self.remember_atomic_push(integrations, integration_pushes, remote, pinned)
        if not git.passthrough("push", "--atomic", *verify_options, *leases, "--set-upstream", remote, *refspecs):
            detail = "some destinations may have been updated" if multiple_destinations else "nothing was pushed"
            raise StackError(
                f"push failed, so {detail}; if the remote has new commits, run `git stack sync` and try "
                f"again{self.changes_left(adapter)}"
            )
        self.record_pushed(branches, integration_pushes, deletions, remote, work_tips)
        try:
            adapter.after_push(branches, stacks, opening, rebuild_stack, scope)
        except StackError as e:
            raise StackError(
                f"pushed, but updating pull requests failed (set {ADAPTER_SETTING} to git to stop using GitHub): {e}"
            ) from e
        return branches

    def protect_pr_opening(self, branches: list[str], stacks: Stacks, adapter: GitHubAdapter) -> None:
        prs = adapter.pull_requests()
        opening = (b for b in branches if b not in prs and self.git.tip(b) != self.recorded_base(b, stacks.parent(b)))
        adapter.protect_pr_opening(branches, opening, stacks)

    def protect_push(
        self,
        leases: list[str],
        refspecs: list[str],
        remote: str,
        adapter: GitAdapter,
        protection: PRProtection,
        urls: tuple[str, ...],
    ) -> None:
        if protection.force and not protection.protect:
            return
        changed = set[str]()
        for lease, refspec in zip(leases, refspecs, strict=True):
            _, _, old = lease.rpartition(":")
            source, separator, target = refspec.partition(":")
            if not separator:
                source = target = f"refs/heads/{source}"
            # An empty lease can only create a branch. A no-op under a
            # nonempty lease can't change the ref in any destination.
            if old and old != (self.git.rev(source) if source else None):
                changed.add(target.removeprefix("refs/heads/"))
        self.protect_remote_heads(remote, adapter, protection, changed, urls)

    def protect_remote_heads(
        self,
        remote: str,
        adapter: GitAdapter,
        protection: PRProtection,
        heads: set[str],
        urls: tuple[str, ...],
        *,
        primary_remote: bool = True,
    ) -> None:
        if not heads or (protection.force and not protection.protect):
            return
        repositories = self.github_push_adapters(remote, adapter, urls)
        hosts = {github.host for github in repositories}
        if isinstance(adapter, GitHubAdapter):
            hosts.add(adapter.host)
            number_repo = adapter.repo.casefold()
        else:
            number_repo = repositories[0].repo if len(repositories) == 1 else None
        if protection.allowed and len(hosts) > 1:
            raise StackError("--allow-pr-update requires one GitHub host; PR identifiers can refer to different PRs")
        for github in repositories:
            # Bare PR numbers refer to the selected repository, even if Git
            # pushes to a different repository or clean deletes other refs.
            github.protection = protection
            try:
                github.protect_ref_updates(heads, allow_number=primary_remote and github.repo == number_repo)
            except FileNotFoundError as error:
                raise StackError(f"checking the pull requests on {github.repo} requires gh on PATH") from error

    def github_push_adapters(self, remote: str, adapter: GitAdapter, urls: tuple[str, ...]) -> list[GitHubAdapter]:
        """Find GitHub repositories behind the push URLs, which can differ from fetch."""
        hosts = {"github.com"}
        if isinstance(adapter, GitHubAdapter):
            hosts.add(adapter.host)
        repositories = self.github_push_repositories(urls, hosts)
        if not repositories:
            repositories = self.github_push_repositories(self.git.configured_push_urls(remote), hosts)
        return [GitHubAdapter(self, remote, *repo, fetch=False) for repo in sorted(repositories)]

    @staticmethod
    def github_push_repositories(urls: Iterable[str], hosts: set[str]) -> set[tuple[str, str, str]]:
        repositories: set[tuple[str, str, str]] = set()
        for url in urls:
            if (repo := github_repo_from_url(url)) is not None and repo[0] in hosts:
                host, owner, name = repo
                repositories.add((host, owner.casefold(), name.casefold()))
        return repositories

    def protect_deletion(
        self, deletion: RemoteDeletion, adapter: GitAdapter, protection: PRProtection, primary_remote: str
    ) -> None:
        self.protect_remote_heads(
            deletion.remote,
            adapter,
            protection,
            {deletion.ref.removeprefix("refs/heads/")},
            self.git.push_urls(deletion.remote),
            primary_remote=deletion.remote == primary_remote,
        )

    def retarget_before_push(
        self,
        branches: list[str],
        stacks: Stacks,
        adapter: GitAdapter,
        remote: str,
        leases: list[str],
        refspecs: list[str],
        rebuild_stack: bool,
        scope: list[str] | None,
        opening: bool,
        integration: bool,
        multiple_destinations: bool,
    ) -> None:
        """Check leases before editing PRs or pushing to several destinations."""

        def update_error(error: StackError) -> StackError:
            return StackError(
                f"updating pull requests failed, so nothing was pushed (set {ADAPTER_SETTING} "
                f"to git to stop using GitHub): {error}{self.changes_left(adapter)}"
            )

        try:
            retarget = adapter.prepare_push_retargets(branches, stacks, rebuild_stack, scope, opening)
        except StackError as e:
            raise update_error(e) from e
        if retarget is None and not multiple_destinations:
            return
        if integration:
            self.emit(events.CheckingPush(remote))
        # Leave PR bases and remote refs unchanged on a stale lease. A real
        # push is atomic at each destination, and can still be refused.
        if (why := self.git.push_would_fail("--atomic", *leases, remote, *refspecs)) is not None:
            raise StackError(
                f"push would fail, so nothing was pushed or changed; if the remote has new commits, run "
                f"`git stack sync` and try again:\n{why}"
            )
        try:
            if retarget is not None:
                retarget()
        except StackError as e:
            raise update_error(e) from e

    def add_integration_refspecs(
        self,
        integrations: Sequence[IntegrationPush],
        deletions: Sequence[RemoteDeletion],
        leases: list[str],
        refspecs: list[str],
        remote: str,
    ) -> None:
        """Add integration updates to the same atomic push as the work."""
        destinations = {lease.rsplit(":", 1)[0] for lease in leases}
        for integration in integrations:
            tip = integration.refs[integration.name]
            assert tip is not None
            self.add_leased_refspec(
                leases, refspecs, destinations, remote, f"refs/heads/{integration.target}", integration.remote_tip, tip
            )

        for deletion in deletions:
            assert deletion.remote == remote
            self.add_leased_refspec(leases, refspecs, destinations, remote, deletion.ref, deletion.tip, "")

    @staticmethod
    def add_leased_refspec(
        leases: list[str],
        refspecs: list[str],
        destinations: set[str],
        remote: str,
        target: str,
        remote_tip: str | None,
        tip: str,
    ) -> None:
        lease = f"--force-with-lease={target}"
        if lease in destinations:
            raise StackError(f"multiple updates target {remote}/{target.removeprefix('refs/heads/')}; refusing to push")
        destinations.add(lease)
        leases.append(f"{lease}:{remote_tip or ''}")
        refspecs.append(f"{tip}:{target}")

    def remember_atomic_push(
        self,
        integrations: Integrations,
        pushes: Sequence[IntegrationPush],
        remote: str,
        work_tips: dict[str, str] | None,
    ) -> None:
        for push in pushes:
            if self.git.config_get(push.name, "remote") is None:
                tip = push.refs[push.name]
                assert tip is not None
                integrations.remember_first_push(push.name, remote, push.target, tip)

        for branch, tip in (work_tips or {}).items():
            if self.git.config_get(branch, "remote") is None:
                integrations.remember_first_push(branch, remote, branch, tip)

    def record_published_branch(
        self, integrations: Integrations, name: str, remote: str, target: str, tip: str
    ) -> None:
        if self.git.config_get(name, "remote") is None:
            self.git.run("branch", f"--set-upstream-to=refs/remotes/{remote}/{target}", "--", name)
        self.record_pulled_tips([(name, tip)])
        integrations.forget_first_push(name)

    def record_pulled_tips(self, tips: Iterable[tuple[str, str]]) -> None:
        """Plan metadata writes before making any, so they share one config read."""
        git = self.git
        updates: list[tuple[str, str, str]] = []
        for branch, tip in tips:
            if git.config_get(branch, PULLED_KEY) != tip:
                updates.append((branch, PULLED_KEY, tip))
            identity = self.upstream_identity(branch)
            if identity is not None and git.config_get(branch, PULLED_UPSTREAM_KEY) != identity:
                updates.append((branch, PULLED_UPSTREAM_KEY, identity))
        for branch, key, value in updates:
            git.config_set(branch, key, value)

    def record_pushed(
        self,
        branches: list[str],
        pushes: Sequence[IntegrationPush],
        deletions: list[RemoteDeletion] | None,
        remote: str,
        work_tips: dict[str, str],
    ) -> None:
        """Remember exactly the pushed tips and configure a newly published integration's upstream."""
        for deletion in deletions or ():
            self.emit(events.RemoteBranchDeleted(deletion.upstream))
        integrations = Integrations(self)
        for push in pushes:
            tip = push.refs[push.name]
            assert tip is not None
            self.record_published_branch(integrations, push.name, remote, push.target, tip)
        if not pushes:
            # Hooks can move local refs while publishing, and a concurrent
            # fetch can replace the tracking ref. Only record matching tips.
            self.record_pulled_tips(
                (name, work_tips[name])
                for name in branches
                if (upstream := self.git.remote_branch(name)) is not None
                and upstream[0] == remote
                and upstream[2] == work_tips[name]
            )
            return
        for name in branches:
            self.record_published_branch(
                integrations, name, remote, self.git.remote_branch_name(name, remote), work_tips[name]
            )

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
        protection: PRProtection = DEFAULT_PR_PROTECTION,
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
        adapter.protection = protection
        branches = self.skip_merged(stack, stacks, adapter)
        self.ensure_published_pr_roots({stacks.root(b) for b in branches}, remote)
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
        if not dry_run:
            adapter.protect_retargets(branches, stacks)
            self.protect_pr_opening(branches, stacks, adapter)
        template: str | None = None
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
            if template is None:
                template = adapter.pull_request_template() or ""
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
        GitHubAdapter.update_stack_sections)."""
        try:
            adapter.update_stack_sections(branches, stacks, rebuild_stack, dry_run)
        except StackError as e:
            raise StackError(f"updating the stack in pull request descriptions failed: {e}") from e

    def ensure_published_pr_roots(self, roots: set[str], remote: str) -> None:
        """Check integration bases on the server before opening PRs."""
        git = self.git
        refs = {
            root: f"refs/heads/{git.remote_branch_name(root, remote)}"
            for root in sorted(roots)
            if git.config_get(root, INTEGRATION_KEY)
        }
        if not refs:
            return
        # A cached remote-tracking ref can be stale or missing.
        found = {
            line.partition("\t")[2]
            for line in git.run("ls-remote", "--heads", "--", remote, *refs.values()).splitlines()
        }
        for root, ref in refs.items():
            if ref not in found:
                upstream = f"{remote}/{ref.removeprefix('refs/heads/')}"
                hint = (
                    f" If its upstream was deleted, restore it or unset it with "
                    f"`git branch --unset-upstream {root}` before publishing under the local name."
                    if git.config_get(root, "remote") == remote
                    else ""
                )
                raise StackError(
                    f"integration base {upstream} is missing; publish it with `git stack push --prs` from a work branch.{hint}"
                )

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
            # Push publishes integration bases itself. Standalone prs checks
            # those bases on the server before it gets here.
            if git.config_get(root, INTEGRATION_KEY):
                continue
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
        # {trunk: its upstream, and where import fast-forwards it to}, as
        # trunk_ref works them out.
        self.trunks: dict[str, tuple[TrunkUpstream | None, str | None]] = {}

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
        if self.git.config_get(branch, INTEGRATION_KEY):
            return self.skip(pr, "it's an integration root")
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
            parent_ref = self.merges_into_ref(parent, f"refs/heads/{branch}" if local else remote_ref)
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
        """The ref of parent, which a pull request of built (a full ref, e.g.
        refs/heads/pr1, never a bare name a tag could have too) merges
        into, after planning it if it's to be created. Raises StackError if the
        pull request has to be skipped for it."""
        git, remote = self.git, self.remote
        if git.config_get(parent, INTEGRATION_KEY) and git.local_branch_exists(parent):
            return f"refs/heads/{parent}"
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
        """The ref of local branch, which built (a full ref, as for
        merges_into_ref) is built on, as import leaves it: if it's untracked
        (a trunk, e.g. main), import fast-forwards it to its upstream when it
        tracks a branch on it, so a stale trunk doesn't make bases too old.

        Raises StackError if built has commits of the upstream that the trunk
        lacks, and the trunk can't be fast-forwarded (it's checked out in
        another worktree, or diverged): tracked on it, built would count them
        as its own, or later be restacked without them.
        """
        git, local = self.git, f"refs/heads/{branch}"
        if branch in self.stacks:
            return local
        if branch not in self.trunks:
            found = self.stack.trunk_upstream(branch)
            self.trunks[branch] = (found, self.stack.fast_forward_target(branch, found))
        upstream, target = self.trunks[branch]
        if upstream is None:
            return local
        if target is not None:
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
