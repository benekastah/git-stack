import enum
import functools
import re
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Concatenate, ParamSpec, TypeVar

from git_stack import events
from git_stack.adapters import ADAPTER_SETTING, make_adapter
from git_stack.adapters.git import GitAdapter, PullRequestLink
from git_stack.adapters.github import GitHubAdapter, OpenPullRequest
from git_stack.adapters.merge_state import MergeState
from git_stack.core import (
    BASE_KEY,
    GROUP_KEY,
    PULLED_KEY,
    Parents,
    StackCore,
    ancestry,
    children_map,
    descendants,
    drop_from_stack,
    ensure_no_cycle,
    group_bottoms,
    stack_of,
    stack_root,
)
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import DEFAULT_REMOTE
from git_stack.json_fields import json_name

Client = TypeVar("Client", bound=StackCore)
Params = ParamSpec("Params")
Result = TypeVar("Result")


def exclusive(
    method: Callable[Concatenate[Client, Params], Result],
) -> Callable[Concatenate[Client, Params], Result]:
    """Run method holding the repository's git-stack lock (see
    StackCore.exclusive), as every command that changes stack metadata does."""

    @functools.wraps(method)
    def locked(client: Client, /, *args: Params.args, **kwargs: Params.kwargs) -> Result:
        with client.exclusive():
            return method(client, *args, **kwargs)

    return locked


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


# What GitHub allows in a username, so one can't add to a search query.
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


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
    parents before children."""

    steps: list[ImportStep]
    branches: list[BranchInfo] = field(metadata=json_name("stacks"))
    without_prs: list[ImportStepWithoutPr] = field(default_factory=list)


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
    deleting it left them. parents is the tracked branches, without the merged
    ones untracked so far. skipped are the branches left alone, whose children
    are left alone too. blocked are those that couldn't be restacked or pulled
    into because of another worktree (or are stacked on one), which a push
    can't include. pushable are the branches to push, and unpushed those that
    differ from their remote branch but won't be pushed. ignore_reverts are
    the branches whose reverts to ignore (see StackClient.sync).
    """

    original: str
    parents: Parents
    remote: str
    delete: str | None
    latest: bool
    push: bool
    ignore_reverts: frozenset[str] = frozenset()
    skipped: set[str] = field(default_factory=set)
    blocked: list[str] = field(default_factory=list)
    pushable: list[str] = field(default_factory=list)
    unpushed: list[str] = field(default_factory=list)


class StackClient(StackCore):
    """The operations behind each `git stack` command.

    Like StackCore's, they run git in the current directory, report progress
    and warnings through on_event, and raise StackError when they fail.
    """

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
        self.ensure_name_free_on_remotes(name)
        # The branch at the bottom of name's stack, which is what's in a group,
        # or None if that's name itself.
        bottom = None
        if group is not None:
            parents = self.tracked_branches()
            self.ensure_group_name(group)
            if parent in parents:
                bottom = ancestry(parent, parents)[-2]
                current = git.config_get(bottom, GROUP_KEY)
                if current is not None and current != group:
                    raise StackError(
                        f"{parent}'s stack is in group {current} already; run `git stack group add --move {group}` "
                        f"from it to move it, or `git stack group rename {group}` to rename the group"
                    )
        # Without --no-track, branch.autoSetupMerge=inherit (or always) would give
        # name its parent's upstream, which sync and push take as its own.
        git.run("switch", "--quiet", "--no-track", "--create", name)
        self.set_parent(name, parent)
        git.config_set(name, BASE_KEY, git.tip(name))
        if group is not None:
            git.config_set(bottom or name, GROUP_KEY, group)
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
            if not git.is_ancestor(base, f"refs/heads/{branch}"):
                raise StackError(f"{base_arg} isn't in {branch}'s history")
            parents[branch] = parent
            plan.append(Tracked(branch, parent, base))
            parent, base_arg = branch, None

        for tracked in plan:
            self.set_parent(tracked.branch, tracked.parent)
            git.config_set(tracked.branch, BASE_KEY, tracked.base)
        return plan

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
        remote: str = DEFAULT_REMOTE,
        dry_run: bool = False,
        force: bool = False,
        author: str | None = None,
        branches: Sequence[str] = (),
        prs: Sequence[str] = (),
    ) -> ImportResult:
        """Track open pull requests on GitHub, drafts included, as stacks: each
        one's branch on the branch it merges into, creating local branches
        from remote for those that don't have one. Branches that are already
        tracked are left alone. The untracked branches they merge into (e.g.
        main) are fast-forwarded to their upstreams first.

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
        if author is not None and not GITHUB_LOGIN.fullmatch(author):
            raise StackError(f"{author} isn't a GitHub username")
        self.ensure_no_operation_in_progress()
        self.settle_all()
        adapter = make_adapter(self, remote, fetch=True)
        if not isinstance(adapter, GitHubAdapter):
            raise StackError(
                f"import finds stacks from pull requests on GitHub, but {remote} isn't a "
                f"GitHub repository, gh isn't installed, or {ADAPTER_SETTING} is git"
            )
        numbers = sorted({adapter.pull_request_number(pr) for pr in prs})
        self.emit(events.LookingUpPullRequests(author))
        # The user's own pull requests, unless only pull requests were named.
        mine = bool(branches) or not numbers
        open_prs = self.look_up_open_pull_requests(adapter, author, numbers, mine)
        self.fetch(remote)
        if not dry_run:
            self.fast_forward_import_trunks(open_prs, adapter.default_branch)
        planner = ImportPlanner(self, open_prs, remote, adapter.default_branch, adapter.viewer, force or dry_run)
        plan, without_prs = planner.plan_all(), list(planner.without_prs.values())
        if branches or numbers:
            plan, without_prs = planner.select(branches, numbers, plan, without_prs)
        if dry_run:
            return ImportResult(plan, [], without_prs)
        self.apply_import(plan, without_prs, remote)
        shown = [s.branch for s in plan] + [s.branch for s in without_prs]
        return ImportResult(plan, self.stack_infos(shown, adapter), without_prs)

    def look_up_open_pull_requests(
        self, adapter: GitHubAdapter, author: str | None, numbers: Sequence[int], mine: bool
    ) -> list[OpenPullRequest]:
        """The open pull requests of author, of the authors of the pull
        requests numbers, and (if author is None and mine) of the gh user."""
        try:
            logins = {number: adapter.open_pull_request_author(number) for number in numbers}
        except StackError as e:
            raise lookup_failed(e) from e
        if closed := [number for number, login in logins.items() if login is None]:
            raise StackError(f"#{closed[0]} isn't an open pull request")
        authors = {author} if author else ({"@me"} if mine else set())
        authors |= {login for login in logins.values() if login}
        prs: dict[int, OpenPullRequest] = {}
        try:
            for login in sorted(authors):
                prs |= {pr.number: pr for pr in adapter.open_pull_requests(login)}
        except StackError as e:
            raise lookup_failed(e) from e
        return list(prs.values())

    def fast_forward_import_trunks(self, prs: list[OpenPullRequest], default_branch: str | None) -> None:
        """Fast-forward the untracked branches prs merge into (e.g. main), and
        the default branch if a branch without a pull request may turn out to
        be built on it."""
        # Bases are worked out from the local parents, so a stale main would
        # make them too old for branches built on a newer origin/main.
        heads, parents = {pr.head for pr in prs}, self.tracked_branches()
        trunks = {pr.base for pr in prs if pr.base not in heads | parents.keys()}
        if default_branch is not None and not all(self.git.local_branch_exists(t) for t in trunks):
            trunks.add(default_branch)
        self.fast_forward_trunks(sorted(trunks))

    def apply_import(self, plan: list[ImportStep], without_prs: list[ImportStepWithoutPr], remote: str) -> None:
        """Create and track the branches import planned to."""
        git = self.git
        for without_pr in without_prs:
            if without_pr.action is ImportAction.CREATE and without_pr.parent and without_pr.base:
                branch = without_pr.branch
                git.run("branch", "--quiet", "--track", branch, f"refs/remotes/{remote}/{branch}")
                self.set_parent(branch, without_pr.parent)
                git.config_set(branch, BASE_KEY, without_pr.base)
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
                self.set_parent(step.branch, step.parent)
                git.config_set(step.branch, BASE_KEY, step.base)

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
        parents = self.tracked_branches()
        self.ensure_group_name(name)
        bottoms = self.selected_bottoms(branches, groups, parents, "group")
        if not move:
            moves = [
                f"{bottom}'s stack, from group {group}"
                for bottom in bottoms
                if (group := self.git.config_get(bottom, GROUP_KEY)) not in (None, name)
            ]
            if moves:
                raise StackError(
                    "some of those stacks are in another group already; pass --move to move them into "
                    f"{name}:\n" + "\n".join(f"  {m}" for m in moves)
                )
        for bottom in bottoms:
            self.git.config_set(bottom, GROUP_KEY, name)
        return bottoms

    @exclusive
    def group_remove(self, branches: Sequence[str] = (), groups: Sequence[str] = ()) -> list[tuple[str, str]]:
        """Take the stacks of the branches that branches and groups name (see
        named; default: the current branch), each by its bottom branch, out of
        their groups. Returns (bottom branch, its old group) for those that
        were in one."""
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        removed = []
        for bottom in self.selected_bottoms(branches, groups, parents, "ungroup"):
            if (group := self.git.config_get(bottom, GROUP_KEY)) is not None:
                self.git.config_unset(bottom, GROUP_KEY)
                removed.append((bottom, group))
        return removed

    @exclusive
    def group_rename(self, new: str, old: str | None = None) -> tuple[str, list[str]]:
        """Rename the group old (default: the current branch's stack's) to new.
        Returns old and the branches that were in it. Refuses if a branch is in
        new already, so groups aren't merged by mistake."""
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        if old is None:
            current = self.current_stacked_branch(parents, "rename", hint="name the group to rename")
            bottom = ancestry(current, parents)[-2]
            old = self.git.config_get(bottom, GROUP_KEY)
            if old is None:
                raise StackError(f"{bottom}'s stack isn't in a group; name the group to rename")
        self.ensure_group_name(new)
        groups = self.groups(parents)
        if new in groups.values():
            raise StackError(
                f"group {new} already exists; to merge {old} into it, run `git stack group add --move {new} -g {old}`"
            )
        members = [b for b, g in groups.items() if g == old]
        if not members:
            raise StackError(f"there's no group {old}")
        for branch in members:
            self.git.config_set(branch, GROUP_KEY, new)
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
        parents = self.tracked_branches()
        selectors = list(branches) or [
            self.current_stacked_branch(parents, "untrack", hint="name the branches to untrack")
        ]
        branches = [branch for selector in selectors for branch in self.matching(selector, parents)]
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
        children = tuple(child for child, parent in self.tracked_branches().items() if parent == old)
        git.run("branch", "--move", old, new)
        for child in children:
            self.set_parent(child, new)
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
        remote: str = DEFAULT_REMOTE,
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
        parents = self.tracked_branches()
        if git.local_branch_exists(name):
            raise StackError(f"{name} already exists; pick another name")
        self.ensure_name_free_on_remotes(name)
        if len(set(stack)) != len(stack):
            raise StackError("each branch can only appear once")
        for branch in stack:
            if branch not in parents:
                raise StackError(f"{branch} isn't in a stack")
        parent = from_branch or (stack_root(stack[0], parents) if stack else self.default_branch(remote))
        self.ensure_local_branch_parent(parent)
        for branch in stack:
            ensure_no_cycle(branch, name, {**parents, name: parent})
        commits = self.reverts_to_revert(reverts, parent)
        # The merged branches the stack is leaving, to untrack once nothing is
        # stacked on them, which they'd otherwise be left as forever.
        adapter = make_adapter(self, remote, fetch=False)
        left = [
            old
            for old in dict.fromkeys(parents[b] for b in stack)
            if old in parents
            and all(b in stack for b, p in parents.items() if p == old)
            and adapter.merge_state(old, parents[old]) is MergeState.REVERTED
        ]
        # So name, if it's at the bottom of a stack, stays in the group the
        # stack was in, with the other stacks in it.
        bottoms = [] if parent in parents else [ancestry(b, parents)[-2] for b in stack]
        group = next((g for b in bottoms if (g := git.config_get(b, GROUP_KEY))), None)

        git.run("branch", "--quiet", "--no-track", name, f"refs/heads/{parent}")
        self.set_parent(name, parent)
        git.config_set(name, BASE_KEY, git.tip(name))
        if group is not None:
            git.config_set(name, GROUP_KEY, group)
        parents[name] = parent
        for branch in stack:
            self.set_parent(branch, name)
            parents[branch] = name
        for old in left:
            self.remove_merged(old, parents, delete=False)
        git.switch(name)
        if not git.passthrough("revert", "--no-edit", *commits):
            moved = f" to move {', '.join(stack)} onto {name}" if stack else ""
            raise StackError(
                f"unreverting stopped; see git's output above. Resolve any conflicts and run "
                f"`git revert --continue`, then `git stack restack`{moved}"
            )
        self.restack_all(descendants(name, children_map(parents)), parents)
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
        return self.stack(self.stacked_branch(branch, parents), parents)

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

    def info(self, branches: Sequence[str] = (), groups: Sequence[str] = (), offline: bool = False) -> list[BranchInfo]:
        """Every tracked stack (or the stacks of the branches that branches and
        groups name, see named), as a list of BranchInfo: each stack's root, then its
        branches, parent-before-child. offline checks for merges like the git
        adapter does, without asking GitHub about pull requests."""
        parents = self.settled_branches()
        if branches or groups:
            parents = {b: parents[b] for b in self.selected_stacks(branches, groups, parents, "show")}
        adapter = GitAdapter(self) if offline else make_adapter(self, DEFAULT_REMOTE, fetch=False)
        return self.branch_infos(parents, adapter)

    def branch_infos(self, parents: Parents, adapter: GitAdapter) -> list[BranchInfo]:
        """The branches in parents, as info() lists them, checked with adapter,
        so a command that already has one (and its pull requests) can show
        them without looking them up again."""
        git = self.git
        current = git.current_branch()
        children = children_map(parents)

        def status(branch: str) -> BranchStatus:
            parent = parents[branch]
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
            statuses = dict(zip(parents, pool.map(status, parents), strict=True))
            prefetched.result()
        all_groups = self.groups(parents)
        shown_groups = {
            b: g
            for b, g in all_groups.items()
            if parents[b] not in parents and len(group_bottoms(b, parents, all_groups)) > 1
        }
        result = []
        for root in sorted({stack_root(b, parents) for b in parents}):
            result.append(BranchInfo(root, None, None, root == current))
            result.extend(
                BranchInfo(
                    b,
                    parents[b],
                    statuses[b],
                    b == current,
                    adapter.pull_request_link(b),
                    shown_groups.get(b),
                )
                for b in descendants(root, children)
            )
        return result

    def stack_infos(self, branches: Iterable[str], adapter: GitAdapter) -> list[BranchInfo]:
        """The stacks of those of branches that are still tracked, and of the
        other stacks in their groups, as info() lists them (see branch_infos),
        for a command to show what it left them as."""
        parents = self.tracked_branches()
        groups = self.groups(parents)
        shown = {s: parents[s] for b in branches if b in parents for s in stack_of(b, parents, groups)}
        return self.branch_infos(shown, adapter)

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
                self.emit(self.reverted(branch, parents, adapter))
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
        squash: bool = False,
        latest: bool = False,
    ) -> None:
        """Rebase every branch that needs it onto its parent, bottom-up (see
        needs_restack), in the current stack, or in the stacks of the branches
        that branches and groups name (see named).

        onto first retargets the current branch, so it can't be combined with
        branches or groups; squash first squashes each branch into one commit; latest
        moves branches on a stack's root onto its tip even if they don't
        conflict with it.
        """
        if onto and (branches or groups):
            raise StackError("--onto moves the current branch, so it can't be combined with branches to restack")
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        original = self.current_checkout()
        if onto:
            branch = self.current_stacked_branch(parents, "restack", hint="use `git stack track` first")
            self.ensure_local_branch_parent(onto)
            ensure_no_cycle(branch, onto, parents)
            parents[branch] = onto
        adapter = make_adapter(self, DEFAULT_REMOTE, fetch=False)
        to_restack = self.skip_merged(self.selected_stacks(branches, groups, parents, "restack"), parents, adapter)
        if squash:
            for b in to_restack:
                self.squash_branch(b, parents[b])
            self.restore_checkout(original)
        self.restack_all(to_restack, parents, latest)
        self.restore_checkout(original)

    @exclusive
    def sync(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str = DEFAULT_REMOTE,
        delete: str | None = None,
        latest: bool = False,
        push: bool = True,
        ignore_reverts: Sequence[str] = (),
    ) -> SyncResult:
        """Fetch, untrack the merged branches in the current stack, or in the
        stacks of the branches that branches and groups name (see named), deleting what
        delete, "local", "remote" or "both", says, restack them (see restack
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
        self.ensure_no_operation_in_progress()
        self.ensure_clean_worktree()
        self.settle_all()
        parents = self.tracked_branches()
        stack = self.selected_stacks(branches, groups, parents, "sync")
        self.ensure_ignorable_reverts(ignore_reverts, stack, "the stacks being synced")
        original = self.current_checkout()
        adapter = self.checked_adapter(remote)
        self.fetch(remote)
        # A group's stacks can be on different roots.
        self.fast_forward_trunks(sorted({stack_root(b, parents) for b in stack}))

        state = SyncState(original, parents, remote, delete, latest, push, frozenset(ignore_reverts))
        # Parent-before-child, so by the time we reach a branch its parent is
        # final: merged branches have been removed (and their children reparented
        # in `parents`), and the rest have been restacked and pulled.
        for branch in stack:
            self.sync_branch(branch, adapter, state)

        self.restore_checkout(state.original)
        self.push_branches(state.pushable, parents, adapter, remote)
        if push and state.blocked:
            raise StackError(
                f"didn't push {', '.join(state.blocked)}: checked out in another worktree, or stacked on a branch "
                f"that is"
            )
        return SyncResult(state.unpushed, self.stack_infos(stack, adapter))

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
        merge_state = adapter.merge_state(branch, parent, ignore_revert=branch in state.ignore_reverts)
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
            self.emit(self.reverted(branch, state.parents, adapter))
            state.skipped.add(branch)
        else:
            self.sync_unmerged(branch, parent, state)

    def reverted(self, branch: str, parents: Parents, adapter: GitAdapter) -> events.Reverted:
        """The warning that branch, which parents says is stacked on its parent,
        was merged and then reverted, with what unrevert needs to bring it back."""
        parent = parents[branch]
        children = tuple(children_map(parents).get(branch, ()))
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
        if git.upstream_gone(branch):
            self.emit(events.UpstreamGone(branch, parent))
        self.take_upstream_rebase(branch, parent, state.remote)
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
        if upstream_tip and upstream_tip != git.rev(f"refs/heads/{branch}"):
            if state.push and git.tracks_same_name(branch, state.remote):
                state.pushable.append(branch)
            else:
                state.unpushed.append(branch)

    @exclusive
    def clean(
        self, remote: str = DEFAULT_REMOTE, delete: str | None = None, ignore_reverts: Sequence[str] = ()
    ) -> list[BranchInfo]:
        """Fetch and untrack merged branches in every stack, deleting what delete
        says, without restacking anything. ignore_reverts is as for sync.
        Returns every stack, as info() lists them."""
        git = self.git
        self.ensure_no_operation_in_progress()
        self.settle_all()
        parents = self.tracked_branches()
        roots = sorted({stack_root(b, parents) for b in parents})
        # Only fast-forwarding the current branch, or deleting it (and so
        # switching away), touches the worktree, and either could fail partway
        # with uncommitted changes in the way.
        current = git.current_branch()
        if current in roots or (delete in ("local", "both") and current in parents):
            self.ensure_clean_worktree()
        self.ensure_ignorable_reverts(ignore_reverts, list(parents), "any stack")
        # Where to leave the user afterwards: their branch (or the commit they had
        # detached at), or wherever deleting their branch left them.
        original = self.current_checkout()
        adapter = self.checked_adapter(remote)
        self.fetch(remote)
        self.fast_forward_trunks(roots)

        # Parent-before-child, so a branch whose parent was merged is checked
        # against its new parent.
        order = [b for root in roots for b in descendants(root, children_map(parents))]
        for branch in order:
            parent = parents[branch]
            if git.rev(f"refs/heads/{parent}") is None:
                self.emit(events.ParentMissing(branch, parent))
                continue
            state = adapter.merge_state(branch, parent, ignore_revert=branch in ignore_reverts)
            if state is MergeState.MERGED:
                if self.untrack_merged(branch, parents, delete) and branch == original:
                    original = self.current_checkout()
            elif state is MergeState.MERGED_WITH_NEW_COMMITS:
                self.emit(events.MergedWithNewCommits(branch, parent))
            elif state is MergeState.REVERTED:
                self.emit(self.reverted(branch, parents, adapter))
        self.restore_checkout(original)
        return self.stack_infos(order, adapter)

    @exclusive
    def push(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str = DEFAULT_REMOTE,
        force: bool = False,
        prs: bool = False,
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
        self.settle_all()
        parents = self.tracked_branches()
        stack = self.selected_stacks(branches, groups, parents, "push")
        adapter = self.checked_adapter(remote)
        github = adapter if prs and isinstance(adapter, GitHubAdapter) else None
        branches = self.skip_merged(stack, parents, adapter)
        if not force:
            for b in branches:
                self.take_upstream_rebase(b, parents[b], remote)
        pushed = self.push_branches(branches, parents, adapter, remote, force, opening=github is not None)
        steps = self.open_pull_requests(pushed, parents, github, remote) if github and pushed else None
        error = None
        if prs and github is None and pushed:
            error = f"pushed, but didn't open or update any pull requests: {not_github(remote)}"
        return PushResult(pushed, self.stack_infos(stack, adapter), steps, error)

    def push_branches(
        self,
        branches: list[str],
        parents: Parents,
        adapter: GitAdapter,
        remote: str,
        force: bool = False,
        opening: bool = False,
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
            adapter.after_push(branches, parents, opening)
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

    @exclusive
    def prs(
        self,
        branches: Sequence[str] = (),
        groups: Sequence[str] = (),
        remote: str = DEFAULT_REMOTE,
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
        parents = self.tracked_branches()
        stack = self.selected_stacks(branches, groups, parents, "open pull requests for")
        adapter = ensure_github(make_adapter(self, remote, fetch=True), remote)
        adapter.ensure_available()
        branches = self.skip_merged(stack, parents, adapter)
        steps = self.open_pull_requests(branches, parents, adapter, remote, dry_run, rebuild_stack)
        return PrsResult(steps, self.stack_infos(stack, adapter))

    def open_pull_requests(
        self,
        branches: list[str],
        parents: Parents,
        adapter: GitHubAdapter,
        remote: str,
        dry_run: bool = False,
        rebuild_stack: bool = False,
    ) -> list[PullRequestStep]:
        """Do what prs does for branches, which must be unmerged,
        parent-before-child. Returns a PullRequestStep for each."""
        git = self.git
        self.ensure_on_remote(branches, sorted({stack_root(b, parents) for b in branches}), remote)
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
        if branches and (not dry_run or rebuild_stack):
            self.update_stack_sections(branches, parents, adapter, rebuild_stack, dry_run)
        return steps

    def update_stack_sections(
        self,
        branches: list[str],
        parents: Parents,
        adapter: GitHubAdapter,
        rebuild_stack: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Update the stack sections of branches' pull requests (see
        GitHubAdapter.update_stack_sections), one for each root, since a
        group's stacks can be on several."""
        try:
            for root in sorted({stack_root(b, parents) for b in branches}):
                adapter.update_stack_sections(
                    [b for b in branches if stack_root(b, parents) == root], parents, rebuild_stack, dry_run
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
            if not git.tracks_same_name(b, remote) or git.rev(f"refs/remotes/{remote}/{b}") != git.tip(b)
        ]
        if unpushed:
            raise StackError(
                f"these branches aren't pushed, or differ from their remote branches: "
                f"{', '.join(unpushed)}; run `git stack push` first"
            )
        for root in roots:
            if git.rev(f"refs/remotes/{remote}/{root}") is None:
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
    ) -> None:
        self.stack = stack
        self.git = stack.git
        self.prs = prs
        self.remote = remote
        self.default_branch = default_branch
        self.viewer = viewer
        self.allow_others = allow_others
        # The tracked branches, and the ones planned to be tracked so far.
        self.parents = stack.tracked_branches()
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
        if branch not in self.parents:
            return self.plan_untracked(pr)
        if self.parents[branch] == parent:
            return ImportStep(branch, parent, pr.number, ImportAction.ALREADY_TRACKED)
        return ImportStep(branch, parent, pr.number, ImportAction.PARENT_DIFFERS, tracked_parent=self.parents[branch])

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
        if parent in self.by_head:
            if parent in self.visiting:
                return self.skip(pr, "the pull requests it's based on are based on it")
            if self.plan(self.by_head[parent]).action is ImportAction.SKIP:
                return self.skip(pr, f"{parent}, which it merges into, was skipped")
            parent_ref = (
                f"refs/heads/{parent}" if git.local_branch_exists(parent) else f"refs/remotes/{remote}/{parent}"
            )
        elif git.local_branch_exists(parent):
            parent_ref = f"refs/heads/{parent}"
        elif (without_pr := self.plan_without_pr(parent)).action is ImportAction.CREATE:
            parent_ref = f"refs/remotes/{remote}/{parent}"
        else:
            return self.skip(pr, f"{parent}, which it merges into, isn't a local branch ({without_pr.reason})")
        try:
            ensure_no_cycle(branch, parent, self.parents)
        except StackError as e:
            return self.skip(pr, str(e))
        base = self.stack.fork_point(parent_ref, branch if local else remote_ref)
        if base is None:
            return self.skip(pr, f"it has no history in common with {parent}")
        self.parents[branch] = parent
        action = ImportAction.TRACK if local else ImportAction.CREATE
        return ImportStep(branch, parent, pr.number, action, base=base, other_authors=others)

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
            parent = self.find_parent(branch)
        except StackError as e:
            return ImportStepWithoutPr(branch, None, ImportAction.SKIP, reason=str(e))
        try:
            parent_ref = self.built_on_ref(parent)
            others = self.other_authors(parent, parent_ref, remote_ref)
            if others and not self.allow_others:
                raise StackError(f"it has commits by {', '.join(others)}; import it with -f")
            ensure_no_cycle(branch, parent, self.parents)
            base = self.stack.fork_point(parent_ref, remote_ref)
            if base is None:
                raise StackError(f"it has no history in common with {parent}")
        except StackError as e:
            return ImportStepWithoutPr(branch, parent, ImportAction.SKIP, reason=str(e))
        self.parents[branch] = parent
        return ImportStepWithoutPr(branch, parent, ImportAction.CREATE, base=base, other_authors=others)

    def built_on_ref(self, parent: str) -> str:
        """The ref of parent, which a branch without a pull request is built
        on, after planning it if it's to be created. Raises StackError if it
        won't be a local branch."""
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
            # Where import fast-forwards it to (which a dry run doesn't),
            # so main's newer commits don't look like the branch's own.
            return f"refs/remotes/{remote}/{parent}"
        if git.local_branch_exists(parent):
            return f"refs/heads/{parent}"
        if self.plan_without_pr(parent).action is ImportAction.SKIP:
            raise StackError(f"{parent}, which it's built on, was skipped")
        return f"refs/remotes/{remote}/{parent}"

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

        bottoms = set()
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
