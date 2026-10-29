import fcntl
import fnmatch
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypeAlias

from git_stack import events
from git_stack.check_cache import CheckCache
from git_stack.errors import BrokenStackError, StackError
from git_stack.git import GitClient

PARENT_KEY = "stackParent"
BASE_KEY = "stackBase"
# The last upstream tip the branch caught up with, by pulling it or pushing it.
# Commits it contains aren't new to the branch, even if the branch has since
# rebased them and its reflog no longer has them.
PULLED_KEY = "stackPulled"
# Written before a rebase starts and promoted to PARENT_KEY/BASE_KEY once the
# rebase is known to have finished. This lets the user resolve conflicts with a
# plain `git rebase --continue` (or back out with `git rebase --abort`).
PENDING_PARENT_KEY = "stackPendingParent"
PENDING_BASE_KEY = "stackPendingBase"
# Same idea for pulling upstream commits with `git cherry-pick`: the branch tip
# before the pull, the upstream tip being pulled, and the identity (see
# GitClient.identities) of each commit being cherry-picked, one per line.
PENDING_PULL_FROM_KEY = "stackPendingPullFrom"
PENDING_PULLED_KEY = "stackPendingPulled"
PENDING_PULL_IDENTITIES_KEY = "stackPendingPullIdentities"
# The last answers to whether the branch conflicts with its parent, whether its
# changes landed in its parent, and whether its parent then reverted them (see
# StackCore.remembered), kept in CHECK_CACHE_FILE. Each can take a second or
# more for a branch far behind main.
CHECK_CACHE_FILE = "git-stack-cache.json"
# Held (see StackCore.exclusive) by each command that changes stack metadata, so
# two never interleave their writes.
LOCK_FILE = "git-stack.lock"
CONFLICT_CHECK_KEY = "stackConflictCheck"
LANDED_CHECK_KEY = "stackLandedCheck"
REVERTED_CHECK_KEY = "stackRevertedCheck"
# The branch this one was stacked on until it merged, as "<name> <the parent it
# merged into> <its base> <its tip> <its merge commits...>" (see MergedParent),
# so a later revert of it can be noticed, and undone with `git stack unrevert`,
# even once it's untracked or deleted. And the last answer to whether it was
# reverted.
MERGED_PARENT_KEY = "stackMergedParent"
MERGED_PARENT_REVERTED_CHECK_KEY = "stackMergedParentRevertedCheck"
# A ref to each tip a MERGED_PARENT_KEY records, named after it, so its commits
# aren't garbage-collected once nothing else has them (e.g. after its branch
# was deleted following a squash merge), for as long as a record names it.
MERGED_TIPS_REF_PREFIX = "refs/stack/merged/"
# The group of stacks the branch's stack is in. When a branch stacked on a
# stack's root (e.g. main) merges, the branches stacked on it become the
# bottoms of stacks of their own, and get its group (or, if it had none, its
# name), so commands on one of them still cover the others, as they did before
# it merged.
GROUP_KEY = "stackGroup"

# {branch: the branch it is stacked on}
Parents: TypeAlias = dict[str, str]
# {branch: its group}, for branches that have one
Groups: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class MergedParent:
    """A merged branch, as a branch stacked on it recorded it when it merged:
    the parent it merged into, the base it was built on, its tip, and the
    commits known to have landed it (see GitAdapter.landing_merges)."""

    name: str
    parent: str
    base: str
    tip: str
    merges: tuple[str, ...]

    def __str__(self) -> str:
        return " ".join([self.name, self.parent, self.base, self.tip, *self.merges])

    @classmethod
    def parse(cls, value: str) -> "MergedParent | None":
        parts = value.split()
        if len(parts) < 4:
            return None
        name, parent, base, tip, *merges = parts
        return cls(name, parent, base, tip, tuple(merges))


# The functions below work on a {branch: parent} map like the one
# StackCore.tracked_branches returns, without touching the repository.


def children_map(parents: Parents) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for branch, parent in sorted(parents.items()):
        children.setdefault(parent, []).append(branch)
    return children


def descendants(root: str, children: dict[str, list[str]]) -> list[str]:
    """Return root's descendants in parent-before-child order (root excluded)."""
    order = []
    for child in children.get(root, []):
        order.append(child)
        order.extend(descendants(child, children))
    return order


def ancestry(branch: str, parents: Parents) -> list[str]:
    """Return [branch, its parent, ..., the first untracked ancestor]."""
    chain = [branch]
    while chain[-1] in parents:
        parent = parents[chain[-1]]
        if parent in chain:
            raise StackError(f"cycle in stack metadata involving {parent}")
        chain.append(parent)
    return chain


def stack_root(branch: str, parents: Parents) -> str:
    """Walk up from branch to the first untracked ancestor (usually main)."""
    return ancestry(branch, parents)[-1]


def ensure_no_cycle(branch: str, new_parent: str, parents: Parents) -> None:
    if branch in ancestry(new_parent, parents):
        raise StackError(f"{new_parent} is stacked on {branch}; that would create a cycle")


def stack_of(branch: str, parents: Parents, groups: Groups | None = None) -> list[str]:
    """Tracked branches in the stack containing tracked branch, parent-before-child:
    everything stacked on its bottom-most tracked ancestor, and that ancestor.
    With groups, also the stacks of the other bottom branches in that
    ancestor's group (see group_bottoms), which may be on other roots.
    """
    chain = ancestry(branch, parents)
    bottoms = group_bottoms(chain[-2], parents, groups or {})
    children = children_map(parents)
    return [b for bottom in bottoms for b in (bottom, *descendants(bottom, children))]


def group_bottoms(bottom: str, parents: Parents, groups: Groups) -> list[str]:
    """bottom, a branch on a stack's root, followed by the other branches on a
    stack's root in its group, if it has one."""
    group = groups.get(bottom)
    if group is None:
        return [bottom]
    others = (b for b, g in groups.items() if g == group and b != bottom and b in parents and parents[b] not in parents)
    return [bottom, *sorted(others)]


def is_glob(name: str) -> bool:
    """Whether name is a glob rather than a branch or group name, which can't
    have these characters in them."""
    return any(c in name for c in "*?[")


def drop_from_stack(branch: str, parents: Parents) -> list[str]:
    """Move branch's children onto its parent in parents and remove branch.

    Returns the moved children. Only parents is changed, not the config.
    """
    new_parent = parents.pop(branch)
    children = [child for child, parent in parents.items() if parent == branch]
    for child in children:
        parents[child] = new_parent
    return children


class StackCore:
    """The building blocks of stack operations: reading and changing stack
    metadata in git config, and restacking, pulling and removing single branches.

    Progress and warnings are passed to on_event as git_stack.events objects
    rather than printed. Failures raise StackError.
    """

    def __init__(self, git: GitClient, on_event: Callable[[events.Event], None] | None = None) -> None:
        self.git = git
        self.on_event = on_event
        self._checks: CheckCache | None = None
        # How many exclusive blocks this client is in, so they can nest.
        self._exclusive_depth = 0

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold the repository's git-stack lock (see LOCK_FILE), waiting for
        another git-stack command to finish if it has it.

        Without it, a command could take a restack another one has just recorded
        as pending, but not started yet, for one that was aborted, and discard
        it (see settle). The lock is in the git directory every worktree
        shares, as the metadata is. The system releases it when the process
        ends, however it ends.
        """
        if self._exclusive_depth:
            self._exclusive_depth += 1
            try:
                yield
            finally:
                self._exclusive_depth -= 1
            return
        with open(os.path.join(self.git.common_dir(), LOCK_FILE), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.emit(events.WaitingForLock())
                fcntl.flock(lock, fcntl.LOCK_EX)
            self._exclusive_depth = 1
            try:
                yield
            finally:
                self._exclusive_depth = 0

    @property
    def checks(self) -> CheckCache:
        """Where remembered keeps its answers."""
        if self._checks is None:
            self._checks = CheckCache(os.path.join(self.git.common_dir(), CHECK_CACHE_FILE))
        return self._checks

    def emit(self, event: events.Event) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def tracked_branches(self) -> Parents:
        """Return {branch: parent} for every tracked branch that still exists."""
        existing = self.git.local_branches()
        return {
            branch: parent
            for (branch, key), parent in self.git.branch_config().items()
            if key == PARENT_KEY.lower() and branch in existing
        }

    def set_parent(self, branch: str, parent: str) -> None:
        """Record parent as branch's parent. If parent is tracked, branch is no
        longer at the bottom of a stack, so it leaves its group (see GROUP_KEY),
        rather than rejoining it if it's ever at the bottom again. If parent is
        a stack's root and branch was higher up a stack (e.g. restack --onto
        main), branch joins that stack's group, so the two stay together."""
        git = self.git
        old_parent = git.config_get(branch, PARENT_KEY)
        if git.config_get(parent, PARENT_KEY) is not None:
            git.config_unset(branch, GROUP_KEY)
        elif old_parent is not None and git.config_get(old_parent, PARENT_KEY) is not None:
            bottom = ancestry(old_parent, self.tracked_branches())[-2]
            if (group := git.config_get(bottom, GROUP_KEY)) is not None:
                git.config_set(branch, GROUP_KEY, group)
        git.config_set(branch, PARENT_KEY, parent)

    def groups(self, parents: Parents) -> Groups:
        """The group of each branch in parents that has one (see GROUP_KEY)."""
        return {b: group for b in parents if (group := self.git.config_get(b, GROUP_KEY))}

    def stack(self, branch: str, parents: Parents) -> list[str]:
        """stack_of, with the branches' groups."""
        return stack_of(branch, parents, self.groups(parents))

    def matching(self, selector: str, parents: Parents) -> list[str]:
        """The tracked branches selector names, as a branch or a glob (as
        `git branch --list` would match it). Fails if it names none."""
        if not is_glob(selector):
            if selector not in parents:
                raise StackError(f"{selector} isn't in a stack")
            return [selector]
        matches = [b for b in parents if fnmatch.fnmatchcase(b, selector)]
        if not matches:
            raise StackError(f"{selector} matches no tracked branch")
        return matches

    def named(self, branches: Sequence[str], groups: Sequence[str], parents: Parents) -> list[str]:
        """The tracked branches that branches name (see matching), and the
        bottom branches of groups. Fails if any of them names none."""
        named = [b for selector in branches for b in self.matching(selector, parents)]
        all_groups = self.groups(parents)
        for group in groups:
            bottoms = [b for b, g in all_groups.items() if g == group and parents[b] not in parents]
            if not bottoms:
                raise StackError(f"there's no group {group}")
            named += bottoms
        return list(dict.fromkeys(named))

    def selected_stacks(
        self, branches: Sequence[str], groups: Sequence[str], parents: Parents, command: str
    ) -> list[str]:
        """The branches in the stacks (see stack) of the branches that branches
        and groups name (see named), or with neither, in the current stack.
        Each stack is parent-before-child."""
        if not branches and not groups:
            return self.stack(self.current_stacked_branch(parents, command), parents)
        result: list[str] = []
        for branch in self.named(branches, groups, parents):
            # Stacks never overlap, so one with branch in it is already all there.
            if branch not in result:
                result.extend(self.stack(branch, parents))
        return result

    def selected_bottoms(
        self, branches: Sequence[str], groups: Sequence[str], parents: Parents, command: str
    ) -> list[str]:
        """The bottom branches (on a stack's root) of the branches that branches
        and groups name (see named), or with neither, of the current branch.
        Unlike selected_stacks, a branch's group doesn't add the group's other
        stacks."""
        if not branches and not groups:
            branches = [self.current_stacked_branch(parents, command)]
        return list(dict.fromkeys(ancestry(b, parents)[-2] for b in self.named(branches, groups, parents)))

    def current_stacked_branch(self, parents: Parents, command: str, hint: str | None = None) -> str:
        """Return the current branch, refusing unless it's in a stack.

        hint, if given, replaces the suggestion to switch to a stacked branch.
        """
        branch = self.git.current_branch()
        if branch is None:
            raise StackError("not on a branch")
        if branch not in parents:
            hint = hint or f"switch to a stacked branch to {command} its stack"
            raise StackError(f"{branch} isn't in a stack; {hint}")
        return branch

    def current_checkout(self) -> str:
        """The current branch, or the commit HEAD is detached at."""
        return self.git.current_branch() or self.git.run("rev-parse", "HEAD")

    def ensure_no_operation_in_progress(self) -> None:
        if self.git.operation_in_progress():
            raise StackError(
                "a rebase, cherry-pick, revert or merge is in progress; finish it with "
                "`--continue` (or `--abort`), then rerun this command"
            )

    def ensure_clean_worktree(self) -> None:
        if self.git.run("status", "--porcelain", "--untracked-files=no"):
            raise StackError("you have uncommitted changes; commit or stash them first")

    def settle(self, branch: str, forget_unfinished: bool = True) -> None:
        """Record the result of an interrupted restack or pull, if there was one.

        If the rebase finished, the pending parent commit is now in the branch's
        history and becomes the new base. If it was aborted, it isn't, and the
        previous parent and base are kept.

        Likewise, if every upstream commit being cherry-picked made it onto the
        branch, the upstream tip counts as pulled. If the cherry-pick was aborted,
        or some commits were skipped, it doesn't, and the next sync tries again.

        A restack or pull that hasn't finished may also not have started yet,
        in another git-stack process. Unless forget_unfinished is set, which
        only commands that restack or pull themselves should do, it's left
        pending for that process to record. It's also left pending while
        branch is checked out or being rebased in another worktree, where it
        may have stopped on a conflict.

        Must not be called while a rebase or cherry-pick is in progress, since the
        branch hasn't been updated yet.
        """
        self.settle_restack(branch, forget_unfinished)
        self.settle_pull(branch, forget_unfinished)

    def settle_restack(self, branch: str, forget_unfinished: bool) -> None:
        git = self.git
        pending_parent = git.config_get(branch, PENDING_PARENT_KEY)
        if pending_parent is None:
            return
        pending_base = git.config_get(branch, PENDING_BASE_KEY)
        if pending_base is not None and git.is_ancestor(pending_base, branch):
            self.set_parent(branch, pending_parent)
            git.config_set(branch, BASE_KEY, pending_base)
        elif not forget_unfinished or git.other_worktree_of(branch) is not None:
            # Another worktree may have stopped partway through it.
            return
        # Only if they're unchanged, in case another process has started a new
        # restack since we read them.
        git.config_unset(branch, PENDING_PARENT_KEY, pending_parent)
        git.config_unset(branch, PENDING_BASE_KEY, pending_base)

    def settle_pull(self, branch: str, forget_unfinished: bool) -> None:
        git = self.git
        pending_pulled = git.config_get(branch, PENDING_PULLED_KEY)
        if pending_pulled is None:
            return
        pull_from = git.config_get(branch, PENDING_PULL_FROM_KEY)
        identities = git.config_get(branch, PENDING_PULL_IDENTITIES_KEY)
        picked = set((identities or "").splitlines())
        # Cherry-picking keeps each commit's author, date and subject, even
        # when the user resolves a conflict.
        if (
            pull_from
            and picked
            and git.is_ancestor(pull_from, branch)
            and picked <= git.identities(f"{pull_from}..refs/heads/{branch}")
        ):
            git.config_set(branch, PULLED_KEY, pending_pulled)
        elif not forget_unfinished or git.other_worktree_of(branch) is not None:
            return
        git.config_unset(branch, PENDING_PULLED_KEY, pending_pulled)
        git.config_unset(branch, PENDING_PULL_FROM_KEY, pull_from)
        git.config_unset(branch, PENDING_PULL_IDENTITIES_KEY, identities)

    def settle_all(self, forget_unfinished: bool = True) -> None:
        for branch in self.tracked_branches():
            self.settle(branch, forget_unfinished)

    def recorded_base(self, branch: str, parent: str) -> str:
        """Return branch's base, checking that it is still in the branch's history."""
        base = self.git.config_get(branch, BASE_KEY)
        if base is None or not self.git.is_ancestor(base, branch):
            raise BrokenStackError(
                f"{branch}'s recorded base isn't in its history (was it rebased by hand?); "
                f"fix it with `git stack track {parent} --base <sha>`"
            )
        return base

    def squash_branch(self, branch: str, parent: str) -> None:
        """Squash branch's own commits into one, keeping its base.

        The commit gets every squashed commit's message, oldest first, and the
        oldest commit's author and date.
        """
        git = self.git
        base = self.recorded_base(branch, parent)
        commits = git.run("rev-list", "--reverse", f"{base}..refs/heads/{branch}").split()
        if len(commits) < 2:
            return
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotSquashedInOtherWorktree(branch, worktree))
            return
        self.emit(events.Squashing(branch, len(commits)))
        message = "\n\n".join(git.run("show", "--no-patch", "--format=%B", c) for c in commits)
        author, date = git.run("show", "--no-patch", "--format=%an <%ae>%x00%aI", commits[0]).split("\0")
        tip = git.tip(branch)
        git.switch(branch)
        git.run("reset", "--quiet", "--soft", base)
        # Committed like `git commit` would be, with the user's hooks, and their
        # terminal for anything that prompts (e.g. a GPG passphrase).
        with tempfile.TemporaryDirectory() as directory:
            message_file = os.path.join(directory, "message")
            with open(message_file, "w") as f:
                f.write(message)
            committed = git.passthrough(
                "commit",
                "--quiet",
                "--cleanup=whitespace",
                f"--author={author}",
                f"--date={date}",
                f"--file={message_file}",
            )
        if not committed:
            git.run("reset", "--quiet", "--soft", tip)
            raise StackError(f"squashing {branch} failed, so it was left as it was; see git's output above")

    def needs_restack(self, branch: str, parent: str, base: str | None, parent_tip: str, latest: bool) -> bool:
        """Whether branch, last rebased onto base, has to be rebased onto
        parent_tip, its parent's tip.

        It does if its parent moved since, except when the parent is a stack's
        root (e.g. main) and branch is still on the root's history: then it
        stays where it is unless it conflicts with the root's tip, or latest
        is set, so its pull request isn't rewritten (rerunning CI and maybe
        dismissing reviews) every time main moves.
        """
        if base == parent_tip:
            return False
        if base is None or latest or self.git.config_get(parent, PARENT_KEY) is not None:
            return True
        return not self.git.is_ancestor(base, parent_tip) or self.conflicts(branch, parent_tip)

    def conflicts(self, branch: str, parent_tip: str) -> bool:
        """Whether merging branch into parent_tip would conflict."""
        tip = self.git.tip(branch)
        return self.remembered(
            branch, CONFLICT_CHECK_KEY, [tip, parent_tip], lambda: self.git.conflicts(tip, parent_tip)
        )

    def remembered(self, branch: str, key: str, inputs: list[str], check: Callable[[], bool]) -> bool:
        """check(), which depends only on the commits inputs, remembered as
        branch's key in the check cache (see CheckCache) as
        "<inputs...> true|false", so it isn't run again until one of them
        changes. Failing to remember it only means checking again next time.
        """
        cached = (self.checks.get(branch, key) or "").split()
        if cached[:-1] == inputs and cached[-1:] in (["true"], ["false"]):
            return cached[-1] == "true"
        result = check()
        tracked = {b for (b, k) in self.git.branch_config() if k == PARENT_KEY.lower()}
        self.checks.set(branch, key, " ".join([*inputs, str(result).lower()]), tracked)
        return result

    def restack_branch(self, branch: str, parent: str, latest: bool = False) -> bool:
        """Rebase branch's own commits onto parent's current tip, if it needs it
        (see needs_restack).

        parent may differ from the recorded one (restack --onto); it is recorded
        only once the rebase has finished.

        Returns False, changing nothing, if branch needs rebasing but is checked
        out in another worktree.
        """
        git = self.git
        parent_tip = git.rev(parent)
        if parent_tip is None:
            raise BrokenStackError(
                f"{branch}'s parent {parent} doesn't exist; retarget it with `git stack track <parent> --base <sha>`"
            )
        base = self.recorded_base(branch, parent)
        if not self.needs_restack(branch, parent, base, parent_tip, latest):
            # There's no rebase to wait for.
            self.set_parent(branch, parent)
            return True
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotRestackedInOtherWorktree(branch, worktree))
            return False
        self.emit(events.Restacking(branch, parent))
        git.config_set(branch, PENDING_PARENT_KEY, parent)
        git.config_set(branch, PENDING_BASE_KEY, parent_tip)
        if git.passthrough("rebase", "--no-update-refs", "--onto", parent_tip, base, branch):
            self.settle(branch)
            return True
        if git.operation_in_progress():
            raise self.stopped(f"restacking {branch}", "rebase", "rerun this command to finish the stack")
        git.config_unset(branch, PENDING_PARENT_KEY)
        git.config_unset(branch, PENDING_BASE_KEY)
        raise StackError(f"restacking {branch} onto {parent} failed; see git's output above")

    def restack_all(self, branches: Iterable[str], parents: Parents, latest: bool = False) -> None:
        """Restack branches, parent-before-child, skipping those stacked on a
        branch that couldn't be restacked."""
        original = self.git.current_branch()
        skipped = set()
        for branch in branches:
            parent = parents[branch]
            if parent in skipped:
                self.emit(events.ParentSkipped(branch, parent))
                skipped.add(branch)
            elif not self.restack_branch(branch, parent, latest):
                skipped.add(branch)
        if original and self.git.current_branch() != original:
            self.git.switch(original)

    def unpulled_commits(self, branch: str, parent: str, upstream_tip: str) -> list[str]:
        """Non-merge commits on upstream_tip that branch doesn't have, oldest first.

        A commit counts as already present if branch (or parent) contains it or an
        equivalent patch, if branch ever pointed at it or a descendant of it, or if
        an earlier pull or push already took it (so the old copies of commits we
        have since rebased don't count).
        """
        git = self.git
        known = [f"refs/heads/{branch}", *git.reflog_tips(branch)]
        for ref in (parent, git.config_get(branch, PULLED_KEY)):
            commit = git.rev(ref) if ref else None
            if commit:
                known.append(commit)
        unknown = set(git.run("rev-list", "--no-merges", upstream_tip, "--not", *known).split())
        if not unknown:
            return []
        new_patches = git.run(
            "rev-list",
            "--reverse",
            "--topo-order",
            "--no-merges",
            "--cherry-pick",
            "--right-only",
            f"refs/heads/{branch}...{upstream_tip}",
        ).split()
        return [commit for commit in new_patches if commit in unknown]

    def pull_upstream(self, branch: str, parent: str, remote: str) -> bool:
        """Cherry-pick commits that others pushed to branch's upstream onto branch.

        Only an upstream that's the branch of the same name on remote, as
        `git stack push` sets it, is pulled from. Any other (e.g. origin/main,
        for a branch made with `git switch -c <branch> origin/main`) isn't
        branch's own remote branch, and its commits aren't branch's to take.

        Returns False, changing nothing, if there are commits to pull but branch
        is checked out in another worktree.
        """
        git = self.git
        if not git.tracks_same_name(branch, remote):
            return True
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip is None:
            return True
        commits = self.unpulled_commits(branch, parent, upstream_tip)
        if not commits:
            return True
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotPulledInOtherWorktree(branch, worktree))
            return False
        self.emit(events.PullingUpstream(branch, len(commits)))
        git.config_set(branch, PENDING_PULL_FROM_KEY, git.tip(branch))
        git.config_set(branch, PENDING_PULLED_KEY, upstream_tip)
        git.config_set(branch, PENDING_PULL_IDENTITIES_KEY, "\n".join(sorted(git.identities("--no-walk", *commits))))
        git.switch(branch)
        if git.passthrough("cherry-pick", "--ff", *commits):
            self.settle(branch)
            return True
        if git.operation_in_progress():
            raise self.stopped(f"pulling upstream commits into {branch}", "cherry-pick", "rerun `git stack sync`")
        git.config_unset(branch, PENDING_PULLED_KEY)
        git.config_unset(branch, PENDING_PULL_FROM_KEY)
        git.config_unset(branch, PENDING_PULL_IDENTITIES_KEY)
        raise StackError(f"pulling upstream commits into {branch} failed; see git's output above")

    def take_upstream_rebase(self, branch: str, parent: str, remote: str) -> bool:
        """Move branch to its upstream if that's branch's own commits rebased
        onto parent somewhere else (e.g. by GitHub's "Rebase stack" button),
        so they're neither pulled in again nor pushed over. Its base becomes
        where the upstream forks from parent. Returns whether it moved.

        Only if branch hasn't changed since it was last pushed or pulled, so
        no local work is lost, and the upstream has the same commits, by
        author, date and subject, with nobody else's added. Not if branch is
        checked out in another worktree.
        """
        git = self.git
        if not git.tracks_same_name(branch, remote):
            return False
        tip = git.tip(branch)
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        base = git.config_get(branch, BASE_KEY)
        if upstream_tip in (None, tip) or base is None or git.config_get(branch, PULLED_KEY) != tip:
            return False
        own = git.identities(f"{base}..{tip}")
        # The upstream may be on commits the local parent doesn't have yet,
        # e.g. main's latest, when it hasn't been fetched and fast-forwarded.
        forks = (git.run("merge-base", ref, upstream_tip, check=False) for ref in (parent, f"{parent}@{{upstream}}"))
        new_base = next((f for f in forks if f and git.identities(f"{f}..{upstream_tip}") == own), None)
        if not own or new_base is None:
            return False
        if branch == git.current_branch():
            # Like a fast-forward, it keeps uncommitted changes, or fails
            # without touching them.
            git.run("reset", "--quiet", "--keep", upstream_tip)
        elif git.other_worktree_of(branch) is not None:
            return False
        else:
            git.run("update-ref", f"refs/heads/{branch}", upstream_tip, tip)
        git.config_set(branch, BASE_KEY, new_base)
        git.config_set(branch, PULLED_KEY, upstream_tip)
        self.emit(events.TookUpstreamRebase(branch, f"{remote}/{branch}"))
        return True

    def stopped(self, what: str, command: str, then: str) -> StackError:
        """The error for a rebase or cherry-pick (command) that stopped partway
        through what, saying how to carry on."""
        if self.git.has_conflicts():
            return StackError(f"{what} stopped on a conflict. Resolve it, run `git {command} --continue`, then {then}")
        # `git rebase --continue` retries the commit that couldn't be applied,
        # but `git cherry-pick --continue` skips it, so a cherry-pick has to be
        # started over.
        carry_on = "`git rebase --continue`" if command == "rebase" else f"`git {command} --abort`"
        return StackError(
            f"{what} stopped without a conflict; see git's output above (e.g. an untracked "
            f"file is in the way: move or delete it). Fix that, run {carry_on}, then {then}"
        )

    def fetch(self, remote: str) -> None:
        """Fetch remote with --prune, saying so first, since in a busy
        repository it can take a while."""
        self.emit(events.Fetching(remote))
        self.git.fetch(remote)

    def fast_forward_trunks(self, roots: Iterable[str]) -> None:
        """Fast-forward untracked local parents (e.g. main) to their upstreams."""
        git = self.git
        current = git.current_branch()
        for root in roots:
            if not git.local_branch_exists(root):
                continue
            upstream = git.run("rev-parse", "--abbrev-ref", f"{root}@{{upstream}}", check=False)
            if not upstream:
                continue
            old, new = git.tip(root), git.rev(upstream)
            if new is None or old == new:
                continue
            if not git.is_ancestor(old, new):
                self.emit(events.TrunkDiverged(root, upstream))
                continue
            if root == current:
                git.run("merge", "--ff-only", "--quiet", upstream)
            elif (worktree := git.worktree_of(root)) is not None:
                # Moving the branch would leave that worktree's files behind.
                self.emit(events.TrunkInOtherWorktree(root, upstream, worktree))
                continue
            else:
                git.run("update-ref", f"refs/heads/{root}", new, old)
            self.emit(events.FastForwarded(root, upstream))

    def forget(self, branch: str) -> None:
        """Remove all of branch's stack metadata."""
        for key in (
            PARENT_KEY,
            BASE_KEY,
            PULLED_KEY,
            PENDING_PARENT_KEY,
            PENDING_BASE_KEY,
            PENDING_PULL_FROM_KEY,
            PENDING_PULLED_KEY,
            PENDING_PULL_IDENTITIES_KEY,
            MERGED_PARENT_KEY,
            GROUP_KEY,
            # Versions before the check cache kept these in git config too.
            CONFLICT_CHECK_KEY,
            LANDED_CHECK_KEY,
            REVERTED_CHECK_KEY,
            MERGED_PARENT_REVERTED_CHECK_KEY,
        ):
            self.git.config_unset(branch, key)
        self.checks.forget(branch)
        self.prune_merged_tips()

    def unrecord_merged_parent(self, branch: str) -> None:
        """Forget the branch that branch was stacked on until it merged."""
        self.git.config_unset(branch, MERGED_PARENT_KEY)
        self.checks.forget(branch, [MERGED_PARENT_REVERTED_CHECK_KEY])
        self.prune_merged_tips()

    def prune_merged_tips(self) -> None:
        """Delete the refs (see MERGED_TIPS_REF_PREFIX) to tips no branch's
        MergedParent records any more."""
        git = self.git
        recorded = {
            merged.tip
            for (_, key), value in git.branch_config().items()
            if key == MERGED_PARENT_KEY.lower() and (merged := MergedParent.parse(value)) is not None
        }
        for ref in git.run("for-each-ref", "--format=%(refname)", MERGED_TIPS_REF_PREFIX).split():
            if ref.removeprefix(MERGED_TIPS_REF_PREFIX) not in recorded:
                git.run("update-ref", "-d", ref)

    def merged_parent(self, branch: str) -> MergedParent | None:
        """The branch that branch was stacked on until it merged, if recorded."""
        value = self.git.config_get(branch, MERGED_PARENT_KEY)
        return MergedParent.parse(value) if value else None

    def remove_merged(self, branch: str, parents: Parents, delete: bool, merges: tuple[str, ...] = ()) -> None:
        """Move branch's children onto its parent, recording branch on each as
        their MergedParent (with merges, see GitAdapter.landing_merges), and,
        if that's a stack's root, putting them in branch's group (see
        GROUP_KEY). Then stop tracking branch.

        If delete is set, the branch itself is deleted too, unless it's checked
        out in another worktree. If it was checked out here, its first child (or
        its parent) that isn't checked out elsewhere is checked out instead, or
        failing that, the parent's commit, detached.
        """
        git = self.git
        new_parent = parents[branch]
        base = git.config_get(branch, BASE_KEY)
        merged = MergedParent(branch, new_parent, base, git.tip(branch), merges) if base else None
        if merged is not None:
            git.run("update-ref", f"{MERGED_TIPS_REF_PREFIX}{merged.tip}", merged.tip)
        group = git.config_get(branch, GROUP_KEY) or branch
        children = tuple(drop_from_stack(branch, parents))
        for child in children:
            self.set_parent(child, new_parent)
            if merged is not None:
                git.config_set(child, MERGED_PARENT_KEY, str(merged))
            if new_parent not in parents:
                git.config_set(child, GROUP_KEY, group)
        if delete and (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotDeletedInOtherWorktree(branch, worktree))
            delete = False
        if not delete:
            self.forget(branch)
            self.emit(events.Untracked(branch, new_parent, children, deleted_tip=None))
            return
        if git.current_branch() == branch:
            target = next(
                (
                    b
                    for b in (*sorted(children), new_parent)
                    if git.local_branch_exists(b) and git.worktree_of(b) is None
                ),
                None,
            )
            if target is not None:
                git.switch(target)
            else:
                git.run("switch", "--quiet", "--detach", new_parent)
        tip = git.tip(branch)
        git.run("branch", "--quiet", "--delete", "--force", branch)
        self.emit(events.Untracked(branch, new_parent, children, deleted_tip=tip))

    def untrack_merged(self, branch: str, parents: Parents, delete: str | None, merges: tuple[str, ...] = ()) -> bool:
        """Stop tracking merged branch, deleting what delete ("local", "remote" or
        "both") says, and recording it on its children (see remove_merged).
        Returns False, leaving branch alone, if its remote branch couldn't be
        deleted.
        """
        git = self.git
        delete_local = delete in ("local", "both")
        remote = git.remote_branch(branch)
        if remote is None:
            self.remove_merged(branch, parents, delete_local, merges)
            return True
        upstream = git.run("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        remote_name, ref, tip = remote
        if delete in ("remote", "both"):
            if not git.delete_remote_branch(remote_name, ref, tip):
                self.emit(events.RemoteBranchDeleteFailed(branch, upstream))
                return False
            self.emit(events.RemoteBranchDeleted(upstream))
            self.remove_merged(branch, parents, delete_local, merges)
        else:
            self.remove_merged(branch, parents, delete_local, merges)
            self.emit(events.RemoteBranchKept(upstream, remote_name, ref.removeprefix("refs/heads/")))
        return True

    def restore_checkout(self, original: str) -> None:
        """Switch back to original, a branch or (if it was deleted) a commit."""
        git = self.git
        if git.local_branch_exists(original):
            if git.current_branch() != original:
                git.switch(original)
        elif git.rev("HEAD") != original or git.current_branch():
            git.run("switch", "--quiet", "--detach", original)
