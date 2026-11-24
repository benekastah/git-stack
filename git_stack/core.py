import enum
import fnmatch
import os
import shlex
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from git_stack import events
from git_stack.check_cache import CheckCache
from git_stack.errors import BrokenStackError, LockHeldByCaller, StackError
from git_stack.file_lock import file_lock
from git_stack.git import GitClient, is_object_id
from git_stack.stacks import Ownership, StackNode, Stacks

PARENT_KEY = "stackParent"
INTEGRATION_KEY = "stackIntegration"
BASE_KEY = "stackBase"
# The last upstream tip the branch caught up with, by pulling it or pushing it.
# Commits it contains aren't new to the branch, even if the branch has since
# rebased them and its reflog no longer has them.
PULLED_KEY = "stackPulled"
# Bind a recorded pulled tip to its upstream, so changing upstreams can't make
# a previous remote's history look already integrated.
PULLED_UPSTREAM_KEY = "stackPulledUpstream"
# Upstream tip seen when explicitly re-tracking a branch with --base. It may
# contain either old rewritten commits or work that still needs to be pulled.
TRACK_UPSTREAM_KEY = "stackTrackUpstream"
# Written before a rebase starts and promoted to PARENT_KEY/BASE_KEY once the
# rebase is known to have finished. This lets the user resolve conflicts with a
# plain `git rebase --continue` (or back out with `git rebase --abort`).
# A restack that's started, as "<the branch's tip before> <the parent's tip
# it's rebased onto> <the parent>", in one setting, so it's recorded with one
# git process, and all of it or none (see restack_settlement). Older versions
# don't read it, so a restack this version stopped is finished with this
# version or a later one.
PENDING_RESTACK_KEY = "stackPendingRestack"
# How older versions recorded it, in three settings, which a restack they
# started and didn't finish may have left.
LEGACY_PENDING_PARENT_KEY = "stackPendingParent"
LEGACY_PENDING_BASE_KEY = "stackPendingBase"
LEGACY_PENDING_RESTACK_FROM_KEY = "stackPendingRestackFrom"
# Same idea for pulling upstream commits with `git cherry-pick`: the branch tip
# before the pull, the upstream tip being pulled, and the identity (see
# GitClient.identity_counts) of each commit being cherry-picked, one per line.
PENDING_PULL_FROM_KEY = "stackPendingPullFrom"
PENDING_PULLED_KEY = "stackPendingPulled"
PENDING_PULL_UPSTREAM_KEY = "stackPendingPullUpstream"
LEGACY_PULL_IDENTITIES_KEY = "stackPendingPullIdentities"
PENDING_PULL_IDENTITIES_KEY = "stackPendingPullIdentityCounts"
# The last answers to whether the branch conflicts with its parent, whether its
# changes landed in its parent, and whether its parent then reverted them (see
# StackCore.remembered), kept in CHECK_CACHE_FILE. Each can take a second or
# more for a branch far behind main.
CHECK_CACHE_FILE = "git-stack-cache.json"
# Held (see StackCore.exclusive) by each command that changes stack metadata, so
# two never interleave their writes.
LOCK_FILE = "git-stack.lock"
# Set, to the holder's process ID, in the environment of what a process
# holding the lock starts, so a git-stack that one of git's hooks runs then
# knows not to wait for the lock (see StackCore.exclusive).
LOCK_HELD_ENV = "GIT_STACK_LOCK_HELD"
CONFLICT_CHECK_KEY = "stackConflictCheck"
# Renamed whenever what the check counts as landed changes, so answers worked
# out the old way aren't used.
LANDED_CHECK_KEY = "stackLandedCheck2"
REVERTED_CHECK_KEY = "stackRevertedCheck"
STALE_BASE_CHECK_KEY = "stackStaleBaseCheck"
LITERAL_HISTORY_ENV = {"GIT_NO_REPLACE_OBJECTS": "1", "GIT_GRAFT_FILE": "", "GIT_SHALLOW_FILE": ""}
# The group of stacks the branch's stack is in. When a branch stacked on a
# stack's root (e.g. main) merges, the branches stacked on it become the
# bottoms of stacks of their own, and get its group (or, if it had none, its
# name), so commands on one of them still cover the others, as they did before
# it merged.
GROUP_KEY = "stackGroup"
# An omitted group selector, distinct from an explicitly empty group name.
CURRENT_GROUP = "\0"
# Ownership is stored on every tracked branch so it survives stack splits.
OWNERSHIP_KEY = "stackOwnership"


@dataclass(frozen=True)
class TrunkUpstream:
    """The upstream of a trunk (e.g. main), by its name (e.g. origin/main)
    and its ref, with the trunk's commit and the upstream's."""

    name: str
    ref: str
    old: str
    new: str


@dataclass(frozen=True)
class Settlement:
    """What recording an interrupted restack or pull of branch writes (see
    StackCore.settle): parent, if set, as its parent, then each of sets, then
    each of unsets, only if it still has the value given (any, for None)."""

    branch: str
    parent: str | None = None
    sets: tuple[tuple[str, str], ...] = ()
    unsets: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class RemoteDeletion:
    """A merged branch's upstream and the tip a delete is allowed to remove."""

    remote: str
    ref: str
    tip: str
    children: tuple[tuple[str, str | None], ...] | None = None

    def with_dependents_from(self, earlier: RemoteDeletion) -> RemoteDeletion:
        """Preserve all dependents when aliases queue the same leased ref."""
        children = None
        if self.children is not None and earlier.children is not None:
            children = tuple(dict.fromkeys((*earlier.children, *self.children)))
        return RemoteDeletion(self.remote, self.ref, self.tip, children)

    @property
    def branch(self) -> str:
        return self.ref.removeprefix("refs/heads/")

    @property
    def upstream(self) -> str:
        return f"{self.remote}/{self.branch}"


class Delete(enum.Enum):
    """What sync and clean delete of a merged branch besides untracking it,
    as -d says. Values are what -d takes."""

    LOCAL = "local"
    REMOTE = "remote"
    BOTH = "both"

    @property
    def deletes_local(self) -> bool:
        return self in (Delete.LOCAL, Delete.BOTH)

    @property
    def deletes_remote(self) -> bool:
        return self in (Delete.REMOTE, Delete.BOTH)


class SyncCommand(enum.Enum):
    """Which command a sync is: sync, or pull, which syncs without pushing.
    Its messages say to rerun that one, since sync would push. Values are the
    commands' names."""

    SYNC = "sync"
    PULL = "pull"


def read_ownership(branch: str, value: str | None) -> Ownership | None:
    """branch's OWNERSHIP_KEY setting, value, as an Ownership, or None if it
    has none."""
    if not value:
        return None
    try:
        return Ownership(value)
    except ValueError:
        choices = " or ".join(o.value for o in Ownership)
        raise StackError(
            f"branch.{branch}.{OWNERSHIP_KEY} must be {choices}, not {value}; fix it with "
            f"`git config branch.{branch}.{OWNERSHIP_KEY} {Ownership.SHARED.value}` (or {Ownership.OWNED.value})"
        ) from None


def is_glob(name: str) -> bool:
    """Whether name is a glob rather than a branch or group name, which can't
    have these characters in them."""
    return any(c in name for c in "*?[")


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
        # See unpulled_commits.
        self._unpulled: dict[tuple[str, ...], tuple[str, ...]] = {}
        # How many exclusive blocks this client is in, so they can nest.
        self._exclusive_depth = 0

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold the repository's git-stack lock (see LOCK_FILE), waiting for
        another git-stack command to finish if it has it.

        Without it, a command could take a restack another one has just recorded
        as pending, but not started yet, for one that was aborted, and discard
        it (see settle). The lock is in the git directory every worktree
        shares, as the metadata is.

        Taken inside a snapshot (see GitClient.snapshot), as a command that
        only reads takes it to settle, it forgets what the snapshot and the
        branch config cache read before, which another command may have
        changed until then.

        While it's held, it sets LOCK_HELD_ENV (GIT_STACK_LOCK_HELD) in this
        process's environment (os.environ), so every process this one starts
        meanwhile has it. Started with it set (see lock_held_by_caller), a
        command that finds the lock taken raises LockHeldByCaller rather than
        wait for the process that started it, which waits for it. With the
        lock free, it takes it as ever, so a stale value costs nothing then.
        """
        if self._exclusive_depth:
            self._exclusive_depth += 1
            try:
                yield
            finally:
                self._exclusive_depth -= 1
            return

        def on_wait() -> None:
            if self.lock_held_by_caller():
                raise LockHeldByCaller(
                    "can't take git-stack's lock: the git stack command that started this one "
                    f"(process {os.environ[LOCK_HELD_ENV]}) holds it, and waits for this one, "
                    "e.g. through one of git's hooks; run this once that command has finished"
                )
            self.emit(events.WaitingForLock())

        with file_lock(self.git.common_dir() / LOCK_FILE, on_wait):
            self.git.forget_snapshot()
            self._exclusive_depth = 1
            held_before = os.environ.get(LOCK_HELD_ENV)
            os.environ[LOCK_HELD_ENV] = str(os.getpid())
            try:
                yield
            finally:
                if held_before is None:
                    del os.environ[LOCK_HELD_ENV]
                else:
                    os.environ[LOCK_HELD_ENV] = held_before
                self._exclusive_depth = 0

    @staticmethod
    def lock_held_by_caller() -> bool:
        """Whether this process's environment says a process that held the
        lock, other than this one, started it, e.g. through one of git's hooks
        (see LOCK_HELD_ENV), so waiting for the lock could wait forever. The
        value may be stale (the lock released since), so it's only a reason
        not to wait for a lock that's taken."""
        held_by = os.environ.get(LOCK_HELD_ENV)
        return bool(held_by) and held_by != str(os.getpid())

    @property
    def checks(self) -> CheckCache:
        """Where remembered keeps its answers."""
        if self._checks is None:
            self._checks = CheckCache(self.git.common_dir() / CHECK_CACHE_FILE)
        return self._checks

    def emit(self, event: events.Event) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def stacks(self) -> Stacks:
        """Every tracked branch that still exists, read from git config."""
        existing = self.git.local_branches()
        config = self.git.branch_config()
        return Stacks(
            StackNode(
                branch,
                parent,
                config.get((branch, GROUP_KEY.lower())) or None,
                read_ownership(branch, config.get((branch, OWNERSHIP_KEY.lower()))),
            )
            for (branch, key), parent in config.items()
            if key == PARENT_KEY.lower() and branch in existing
        )

    def save(self, stacks: Stacks) -> None:
        """Write what changed in stacks since it was read (or last saved) to
        git config: the settings that changed, and for a branch no longer
        tracked, none at all (see forget)."""
        git = self.git
        for branch, old, new in stacks.changes():
            if new is None:
                self.forget(branch)
                continue
            for key, before, after in (
                (OWNERSHIP_KEY, old and old.ownership and old.ownership.value, new.ownership and new.ownership.value),
                (GROUP_KEY, old and old.group, new.group),
                (PARENT_KEY, old and old.parent, new.parent),
            ):
                if after == before:
                    continue
                if after is None:
                    git.config_unset(branch, key)
                else:
                    git.config_set(branch, key, after)
        stacks.mark_saved()

    def set_parent(self, branch: str, parent: str) -> None:
        """Record parent as branch's parent (see Stacks.reparent)."""
        stacks = self.stacks()
        stacks.reparent(branch, parent)
        self.save(stacks)

    def matching(self, selector: str, stacks: Stacks) -> list[str]:
        """The tracked branches selector names, as a branch or a glob (as
        `git branch --list` would match it). Fails if it names none."""
        if not is_glob(selector):
            if selector not in stacks:
                raise StackError(f"{selector} isn't in a stack")
            return [selector]
        matches = [b for b in stacks if fnmatch.fnmatchcase(b, selector)]
        if not matches:
            raise StackError(f"{selector} matches no tracked branch")
        return matches

    def named(self, branches: Sequence[str], groups: Sequence[str], stacks: Stacks) -> list[str]:
        """The tracked branches that branches name (see matching), and the
        bottom branches of groups. Fails if any of them names none."""
        named = [b for selector in branches for b in self.matching(selector, stacks)]
        for group in groups:
            if group == CURRENT_GROUP:
                current = self.current_stacked_branch(stacks, "select")
                group = stacks.group(stacks.bottom(current)) or ""
                if not group:
                    named.append(current)
                    continue
            bottoms = stacks.bottoms_in(group)
            if not bottoms:
                raise StackError(f"there's no group {group}")
            named += bottoms
        return list(dict.fromkeys(named))

    def selected_stacks(
        self,
        branches: Sequence[str],
        groups: Sequence[str],
        stacks: Stacks,
        command: str,
    ) -> list[str]:
        """The branches in the stacks (see Stacks.stack_of) of the branches
        that branches and groups name (see named), or with neither, in the
        current stack. Each stack is parent-before-child."""
        if not branches and not groups:
            return stacks.stack_of(self.current_stacked_branch(stacks, command), groups=False)
        result: dict[str, None] = {}
        for branch in self.named(branches, groups, stacks):
            # Stacks never overlap, so one with branch in it is already all there.
            if branch not in result:
                result.update(dict.fromkeys(stacks.stack_of(branch, groups=False)))
        return list(result)

    def selected_bottoms(
        self, branches: Sequence[str], groups: Sequence[str], stacks: Stacks, command: str
    ) -> list[str]:
        """The bottom branches (on a stack's root) of the branches that branches
        and groups name (see named), or with neither, of the current branch.
        """
        if not branches and not groups:
            branches = [self.current_stacked_branch(stacks, command)]
        return list(dict.fromkeys(stacks.bottom(b) for b in self.named(branches, groups, stacks)))

    def current_stacked_branch(self, stacks: Stacks, command: str, hint: str | None = None) -> str:
        """Return the current branch, refusing unless it's in a stack.

        hint, if given, replaces the suggestion to switch to a stacked branch.
        """
        branch = self.git.current_branch()
        if branch is None:
            raise StackError("not on a branch")
        if branch not in stacks:
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
        if self.git.run("status", "--porcelain", "--untracked-files=no", "--no-renames"):
            raise StackError("you have uncommitted changes; commit or stash them first")

    def settle(self, branch: str, forget_unfinished: bool = True, restack_completed: bool = False) -> None:
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
        self.apply(self.restack_settlement(branch, forget_unfinished, restack_completed))
        self.apply(self.pull_settlement(branch, forget_unfinished))

    def apply(self, settlement: Settlement | None) -> None:
        """Write settlement, if there is one."""
        if settlement is None:
            return
        branch = settlement.branch
        if settlement.parent is not None:
            self.set_parent(branch, settlement.parent)
        for key, value in settlement.sets:
            self.git.config_set(branch, key, value)
        for key, expected in settlement.unsets:
            self.git.config_unset(branch, key, expected)

    def restack_settlement(self, branch: str, forget_unfinished: bool, completed: bool = False) -> Settlement | None:
        """What settle writes to record branch's restack, if anything,
        without writing it."""
        git = self.git
        before: str | None
        pending_base: str | None
        if (pending := git.config_get(branch, PENDING_RESTACK_KEY)) is not None:
            before, pending_base, pending_parent = pending.split(" ", 2)
            recorded = {PENDING_RESTACK_KEY: pending}
        else:
            legacy_parent = git.config_get(branch, LEGACY_PENDING_PARENT_KEY)
            if legacy_parent is None:
                return None
            pending_parent = legacy_parent
            pending_base = git.config_get(branch, LEGACY_PENDING_BASE_KEY)
            before = git.config_get(branch, LEGACY_PENDING_RESTACK_FROM_KEY)
            recorded = {
                key: value
                for key, value in (
                    (LEGACY_PENDING_PARENT_KEY, pending_parent),
                    (LEGACY_PENDING_BASE_KEY, pending_base),
                    (LEGACY_PENDING_RESTACK_FROM_KEY, before),
                )
                if value is not None
            }
        if (
            pending_base is not None
            and (completed or (before is not None and git.finished_rebase(branch, before, pending_base)))
            and git.is_ancestor(pending_base, f"refs/heads/{branch}")
        ):
            parent: str | None = pending_parent
            sets: tuple[tuple[str, str], ...] = ((BASE_KEY, pending_base),)
        elif not forget_unfinished or git.other_worktree_of(branch) is not None:
            # Another worktree may have stopped partway through it.
            return None
        else:
            parent, sets = None, ()
        # Only if it's unchanged, in case another process has started a new
        # restack since we read it.
        return Settlement(branch, parent, sets, tuple(recorded.items()))

    def pull_settlement(self, branch: str, forget_unfinished: bool) -> Settlement | None:
        """What settle writes to record a pull into branch, if anything,
        without writing it."""
        git = self.git
        pending_pulled = git.config_get(branch, PENDING_PULLED_KEY)
        if pending_pulled is None:
            return None
        sets: list[tuple[str, str]] = []
        pull_from = git.config_get(branch, PENDING_PULL_FROM_KEY)
        pending_upstream = git.config_get(branch, PENDING_PULL_UPSTREAM_KEY)
        identities = git.config_get(branch, PENDING_PULL_IDENTITIES_KEY)
        picked = Counter((identities or "").splitlines())
        # Cherry-picking keeps each commit's author, date and subject, even
        # when the user resolves a conflict.
        if (
            pull_from
            and picked
            and git.is_ancestor(pull_from, f"refs/heads/{branch}")
            and picked <= git.identity_counts(f"{pull_from}..refs/heads/{branch}")
        ):
            if pending_upstream is None or pending_upstream == self.upstream_identity(branch):
                sets.append((PULLED_KEY, pending_pulled))
                if pending_upstream is not None:
                    sets.append((PULLED_UPSTREAM_KEY, pending_upstream))
        elif not forget_unfinished or git.other_worktree_of(branch) is not None:
            return None
        unsets = (
            (PENDING_PULLED_KEY, pending_pulled),
            (PENDING_PULL_FROM_KEY, pull_from),
            (PENDING_PULL_UPSTREAM_KEY, pending_upstream),
            (PENDING_PULL_IDENTITIES_KEY, identities),
            (LEGACY_PULL_IDENTITIES_KEY, None),
        )
        return Settlement(branch, None, tuple(sets), unsets)

    def settle_all(self, forget_unfinished: bool = True) -> None:
        for branch in self.stacks():
            self.settle(branch, forget_unfinished)

    def settle_waiting(self) -> bool:
        """Whether settle_all(forget_unfinished=False) would record anything
        now, found without writing or taking the lock. Never while a rebase,
        cherry-pick, revert or merge is in progress (see settle)."""
        if self.git.operation_in_progress():
            return False
        return any(
            self.restack_settlement(branch, forget_unfinished=False)
            or self.pull_settlement(branch, forget_unfinished=False)
            for branch in self.stacks()
        )

    def recorded_base(self, branch: str, parent: str) -> str:
        """Return branch's base, checking that it is still in the branch's history."""
        base = self.git.config_get(branch, BASE_KEY)
        if base is None or not self.git.is_ancestor(base, f"refs/heads/{branch}"):
            raise BrokenStackError(
                f"{branch}'s recorded base isn't in its history (was it rebased by hand?); "
                f"fix it with `git stack track {parent} --base <sha>`"
            )
        return base

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
        if (
            base is None
            or latest
            or any(self.git.config_get(parent, key) is not None for key in (PARENT_KEY, INTEGRATION_KEY))
        ):
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
        self.remember_check_result(branch, key, inputs, str(result).lower())
        return result

    def remember_check_result(self, branch: str, key: str, inputs: list[str], result: str) -> None:
        tracked = {b for (b, k) in self.git.branch_config() if k == PARENT_KEY.lower()}
        self.checks.set(branch, key, " ".join([*inputs, result]), tracked)

    def restack_branch(self, branch: str, parent: str, latest: bool = False) -> bool:
        """Rebase branch's own commits onto parent's current tip, if it needs it
        (see needs_restack).

        parent may differ from the recorded one (restack --onto); it is recorded
        only once the rebase has finished.

        Returns False, changing nothing, if branch needs rebasing but is checked
        out in another worktree.
        """
        git = self.git
        parent_tip = git.rev(f"refs/heads/{parent}")
        if parent_tip is None:
            raise BrokenStackError(
                f"{branch}'s parent {parent} doesn't exist. "
                + events.move_off_missing_parent(branch, parent, git.config_get(branch, BASE_KEY))
            )
        base = self.recorded_base(branch, parent)
        if not self.needs_restack(branch, parent, base, parent_tip, latest):
            # There's no rebase to wait for.
            self.set_parent(branch, parent)
            return True
        branch_ref = f"refs/heads/{branch}"
        if git.run("rev-list", "--max-count=1", "--merges", f"{base}..{branch_ref}"):
            self.ensure_fresh_trunk_bases((branch,))
            raise StackError(
                f"{branch} contains merge commits; restack it manually while preserving merges, "
                f"then run `git stack track {parent} --base <sha>`"
            )
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotRestackedInOtherWorktree(branch, str(worktree)))
            return False
        self.emit(events.Restacking(branch, parent))
        git.config_set(branch, PENDING_RESTACK_KEY, f"{git.tip(branch)} {parent_tip} {parent}")
        if git.passthrough("rebase", "--no-update-refs", "--onto", parent_tip, base, branch):
            self.settle(branch, restack_completed=True)
            return True
        if git.operation_in_progress():
            raise self.stopped(f"restacking {branch}", "rebase", "rerun this command to finish the stack")
        git.config_unset(branch, PENDING_RESTACK_KEY)
        raise StackError(f"restacking {branch} onto {parent} failed; see git's output above")

    def stale_trunk_base(self, branch: str) -> tuple[str, str, str] | None:
        """Find a repair candidate when every merge in the replay range belongs to trunk."""
        git = self.git
        parent = git.config_get(branch, PARENT_KEY)
        base = git.config_get(branch, BASE_KEY)
        if (
            not parent
            or not base
            or any(git.config_get(parent, key) is not None for key in (PARENT_KEY, INTEGRATION_KEY))
        ):
            return None
        trunk = git.rev(f"refs/heads/{parent}")
        if trunk is None:
            return None
        upstream = git.rev(f"{parent}@{{upstream}}") if git.config_get(parent, "merge") else None
        if upstream and upstream != trunk and git.is_ancestor(trunk, upstream):
            trunk = upstream
        if base == trunk:
            return None
        branch_ref = f"refs/heads/{branch}"
        tip = git.rev(branch_ref)
        if tip is None:
            return None
        fork = self.stale_trunk_fork(branch, base, trunk, tip)
        return (parent, fork, trunk) if fork else None

    def stale_trunk_fork(self, branch: str, base: str, trunk: str, tip: str) -> str | None:
        """Return a repair point, caching only checks on a complete commit graph."""
        git = self.git
        inputs = [tip, base, trunk]
        cacheable = is_object_id(base) and git.cacheable_history()
        if cacheable:
            cached = (self.checks.get(branch, STALE_BASE_CHECK_KEY) or "").split()
            if cached[:-1] == inputs:
                if cached[-1] == "-":
                    return None
                if is_object_id(cached[-1]):
                    return cached[-1]

        # A concurrent replacement, graft or shallow fetch must not change
        # a result that will outlive this snapshot under ordinary commit IDs.
        env = LITERAL_HISTORY_ENV if cacheable else None
        try:
            forks = git.run("merge-base", "--all", trunk, tip, env=env).splitlines()
            fork = forks[0] if len(forks) == 1 else None
            is_candidate = fork is not None and fork not in (base, tip) and git.check_ancestry(base, fork, env=env)
        except StackError:
            # A missing object or a failed probe is not evidence of a clean base.
            return None
        if (
            not is_candidate
            or not git.run("rev-list", "--max-count=1", "--merges", f"{base}..{fork}", env=env)
            or git.run("rev-list", "--max-count=1", "--merges", f"{fork}..{tip}", env=env)
        ):
            fork = None
        if cacheable:
            self.remember_check_result(branch, STALE_BASE_CHECK_KEY, inputs, fork or "-")
        return fork

    def ensure_fresh_trunk_bases(
        self, branches: Iterable[str], *, skip: Callable[[str, str, str], bool] | None = None
    ) -> None:
        for branch in branches:
            stale = self.stale_trunk_base(branch)
            if stale is None:
                continue
            parent, fork, trunk = stale
            if skip is not None and skip(branch, parent, trunk):
                continue
            repair = shlex.join(("git", "stack", "track", parent, branch, "--base", fork))
            raise StackError(
                f"{branch}'s recorded base puts trunk merge commits in its restack range. "
                f"If {fork} is the intended fork, run `{repair}` and retry"
            )

    def restack_all(self, branches: Iterable[str], stacks: Stacks, latest: bool = False) -> list[str]:
        """Restack branches, parent-before-child, returning the ones blocked by another worktree."""
        original = self.git.current_branch()
        skipped: set[str] = set()
        for branch in branches:
            parent = stacks.parent(branch)
            if parent in skipped:
                self.emit(events.ParentSkipped(branch, parent))
                skipped.add(branch)
            elif not self.restack_branch(branch, parent, latest):
                skipped.add(branch)
        if original and self.git.current_branch() != original:
            self.git.switch(original)
        return sorted(skipped)

    def unpulled_commits(self, branch: str, parent: str, upstream_tip: str) -> list[str]:
        """Non-merge commits on upstream_tip that branch doesn't have, oldest first.

        A commit counts as already present if branch (or parent) contains it or an
        equivalent patch, if branch ever pointed at it or a descendant of it, or if
        an earlier pull or push already took it (so the old copies of commits we
        have since rebased don't count).

        upstream_tip must be a commit ID, not a ref, since the answer is
        remembered for the commits asked about.
        """
        git = self.git
        tip = git.tip(branch)
        if tip == upstream_tip:
            return []
        pulled = self.pulled_tip(branch)
        if pulled == upstream_tip and git.rev(upstream_tip) == upstream_tip:
            return []
        known = [tip, *git.reflog_tips(branch)]
        for ref in (f"refs/heads/{parent}", pulled):
            commit = git.rev(ref) if ref else None
            if commit:
                known.append(commit)
        # Commits don't change, so neither does the answer for the same ones,
        # which a command can ask for several times (e.g. before pulling, and
        # again before pushing).
        key = (upstream_tip, *known)
        if (cached := self._unpulled.get(key)) is not None:
            return list(cached)
        # Each commit, then its parents.
        lines = [line.split() for line in git.run("rev-list", "--parents", upstream_tip, "--not", *known).splitlines()]
        unseen = {commit for commit, *_ in lines}
        if any(len(parents) > 1 for _, *parents in lines):
            raise StackError(
                f"{branch}'s upstream contains unseen merge commits; inspect and integrate them manually "
                "before syncing or pushing"
            )
        commits: list[str] = []
        if unseen:
            new_patches = git.run(
                "rev-list",
                "--reverse",
                "--topo-order",
                "--no-merges",
                "--cherry-pick",
                "--right-only",
                f"{tip}...{upstream_tip}",
            ).split()
            commits = [commit for commit in new_patches if commit in unseen]
        self._unpulled[key] = tuple(commits)
        return commits

    def upstream_identity(self, branch: str) -> str | None:
        remote = self.git.config_get(branch, "remote")
        ref = self.git.config_get(branch, "merge")
        return f"{remote} {ref}" if remote and ref and remote != "." else None

    def pulled_tip(self, branch: str) -> str | None:
        """The recorded tip only if it belongs to the branch's current upstream."""
        git = self.git
        recorded = git.config_get(branch, PULLED_UPSTREAM_KEY)
        current = self.upstream_identity(branch)
        if recorded is not None:
            return git.config_get(branch, PULLED_KEY) if recorded == current else None
        # Older versions only recorded tips for same-named upstreams.
        remote = git.config_get(branch, "remote")
        return git.config_get(branch, PULLED_KEY) if remote and git.tracks_same_name(branch, remote) else None

    def ensure_upstream_not_rewritten(
        self, branch: str, parent: str, remote: str, allow: bool, command: SyncCommand
    ) -> None:
        """Refuse ambiguous upstream changes until the user explicitly chooses to pull them."""
        git = self.git
        if git.remote_branch(branch) is None:
            return
        upstream = git.rev(f"{branch}@{{upstream}}")
        if upstream is None or not self.unpulled_commits(branch, parent, upstream) or allow:
            return
        tip = git.tip(branch)
        if git.is_ancestor(tip, upstream):
            return
        previous = self.pulled_tip(branch)
        if previous and git.rev(previous) and git.is_ancestor(previous, upstream):
            return
        upstream_name = git.run("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        raise StackError(
            f"{branch}'s upstream has unseen commits after its history was rewritten or its previous tip is unknown; "
            f"inspect `git log --left-right --oneline {branch}...{upstream_name}`. "
            f"To pull them, rerun `git stack {command.value} --rewritten-upstream`; "
            "to replace the remote with local history, use `git stack push --force`"
        )

    def pull_upstream(
        self, branch: str, parent: str, remote: str, pull_tracked_upstream: bool, command: SyncCommand
    ) -> bool:
        """Cherry-pick commits that others pushed to branch's upstream onto branch,
        for command, which its messages say to rerun.

        Uses the branch's configured remote-tracking upstream, which can have
        a different name. Commits already in its stack parent are not replayed.

        Returns False, changing nothing, if there are commits to pull but branch
        is checked out in another worktree.
        """
        git = self.git
        if git.remote_branch(branch) is None:
            return True
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip is None:
            return True
        commits = self.unpulled_commits(branch, parent, upstream_tip)
        if not commits:
            return True
        tracked_upstream = git.config_get(branch, TRACK_UPSTREAM_KEY)
        if (
            tracked_upstream
            and not pull_tracked_upstream
            and any(git.is_ancestor(commit, tracked_upstream) for commit in commits)
        ):
            raise StackError(
                f"{branch}'s upstream has unseen commits from before it was re-tracked; "
                f"inspect them before proceeding. If they are wanted, rerun `git stack {command.value} --pull-tracked-upstream`; "
                "if they are obsolete copies of your own work, use `git stack push --force` to replace them"
            )
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotPulledInOtherWorktree(branch, str(worktree)))
            return False
        self.emit(events.PullingUpstream(branch, len(commits)))
        git.config_set(branch, PENDING_PULL_FROM_KEY, git.tip(branch))
        git.config_set(branch, PENDING_PULLED_KEY, upstream_tip)
        if (identity := self.upstream_identity(branch)) is not None:
            git.config_set(branch, PENDING_PULL_UPSTREAM_KEY, identity)
        git.config_set(
            branch,
            PENDING_PULL_IDENTITIES_KEY,
            "\n".join(sorted(git.identity_counts("--no-walk", *commits).elements())),
        )
        git.switch(branch)
        if git.passthrough("cherry-pick", "--ff", *commits):
            self.settle(branch)
            git.config_unset(branch, TRACK_UPSTREAM_KEY)
            return True
        if git.operation_in_progress():
            stopped = self.stopped(
                f"pulling upstream commits into {branch}", "cherry-pick", f"rerun `git stack {command.value}`"
            )
            raise StackError(
                f"{stopped}\nIf these are old copies of your own rewritten commits, "
                "run `git cherry-pick --abort` and inspect the remote history before using `git stack push --force`."
            ) from stopped
        git.config_unset(branch, PENDING_PULLED_KEY)
        git.config_unset(branch, PENDING_PULL_FROM_KEY)
        git.config_unset(branch, PENDING_PULL_UPSTREAM_KEY)
        git.config_unset(branch, PENDING_PULL_IDENTITIES_KEY)
        git.config_unset(branch, LEGACY_PULL_IDENTITIES_KEY)
        raise StackError(f"pulling upstream commits into {branch} failed; see git's output above")

    def take_upstream_rebase(self, branch: str, parent: str, remote: str, allow_rewritten: bool = False) -> bool:
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
        own = git.identity_counts(f"{base}..{tip}")
        # The upstream may be on commits the local parent doesn't have yet,
        # e.g. main's latest, when it hasn't been fetched and fast-forwarded.
        forks = (
            git.run("merge-base", ref, upstream_tip, check=False)
            for ref in (f"refs/heads/{parent}", f"{parent}@{{upstream}}")
        )
        new_base = next((f for f in forks if f and git.identity_counts(f"{f}..{upstream_tip}") == own), None)
        if not own or new_base is None:
            return False
        if git.run("rev-list", "--merges", f"{base}..{tip}") or git.run(
            "rev-list", "--merges", f"{new_base}..{upstream_tip}"
        ):
            raise StackError(
                f"{remote}/{branch} may be a rebased copy of {branch}, but contains merge commits; "
                "inspect both histories before deciding how to proceed"
            )
        if not git.is_rebased_copy(base, tip, new_base, upstream_tip):
            if allow_rewritten:
                return False
            raise StackError(
                f"{remote}/{branch} has commits matching {branch}'s authors, dates and subjects "
                "but different changes; inspect both histories. To replay the upstream changes, run "
                "`git stack pull --rewritten-upstream`; to replace the remote history, "
                "run `git stack push --force`"
            )
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
        if (identity := self.upstream_identity(branch)) is not None:
            git.config_set(branch, PULLED_UPSTREAM_KEY, identity)
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

    def trunk_upstream(self, root: str) -> TrunkUpstream | None:
        """An ordinary local root's upstream if it exists and is at another commit."""
        git = self.git
        if (
            git.config_get(root, INTEGRATION_KEY) is not None
            or not git.local_branch_exists(root)
            or git.upstream_is_at_tip(root)
        ):
            return None
        upstream = git.run("rev-parse", "--abbrev-ref", f"{root}@{{upstream}}", check=False)
        if not upstream:
            return None
        upstream_ref = git.run("rev-parse", "--symbolic-full-name", f"{root}@{{upstream}}", check=False)
        old, new = git.tip(root), git.rev(upstream_ref) if upstream_ref else None
        if new is None or old == new:
            return None
        return TrunkUpstream(upstream, upstream_ref, old, new)

    def fast_forward_target(self, root: str, upstream: TrunkUpstream | None) -> str | None:
        """The ref of root's upstream (as trunk_upstream gives it), if
        fast_forward_trunks would move root to it, or None if it would leave
        root where it is."""
        git = self.git
        if upstream is None or not git.is_ancestor(upstream.old, upstream.new):
            return None
        if root != git.current_branch() and git.worktree_of(root) is not None:
            return None
        return upstream.ref

    def fast_forward_trunks(self, roots: Iterable[str]) -> None:
        """Fast-forward untracked local parents (e.g. main) to their upstreams."""
        git = self.git
        current = git.current_branch()
        for root in roots:
            if (found := self.trunk_upstream(root)) is None:
                continue
            upstream, old, new = found.name, found.old, found.new
            if not git.is_ancestor(old, new):
                self.emit(events.TrunkDiverged(root, upstream))
                continue
            if root == current:
                git.run("merge", "--ff-only", "--quiet", new)
            elif (worktree := git.worktree_of(root)) is not None:
                # Moving the branch would leave that worktree's files behind.
                self.emit(events.TrunkInOtherWorktree(root, upstream, str(worktree)))
                continue
            else:
                git.run("update-ref", f"refs/heads/{root}", new, old)
            self.emit(events.FastForwarded(root, upstream))

    def forget(self, branch: str) -> None:
        """Remove all of branch's stack metadata: each of its settings that
        starts with "stack", as all of git-stack's do (and older versions'
        did)."""
        for name, key in list(self.git.branch_config()):
            if name == branch and key.startswith("stack"):
                self.git.config_unset(branch, key)
        self.checks.forget(branch)

    def remove_merged(self, branch: str, stacks: Stacks, delete: bool) -> None:
        """Stop tracking merged branch, moving its children onto its parent
        (see Stacks.remove_merged), in stacks and in git config.

        If delete is set, the branch itself is deleted too, unless it's checked
        out in another worktree. If it was checked out here, its first child (or
        its parent) that isn't checked out elsewhere is checked out instead, or
        failing that, the parent's commit, detached.
        """
        git = self.git
        new_parent = stacks.parent(branch)
        children = tuple(stacks.remove_merged(branch))
        if delete and (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotDeletedInOtherWorktree(branch, str(worktree)))
            delete = False
        if not delete:
            self.save(stacks)
            self.emit(events.Untracked(branch, new_parent, children, deleted_tip=None))
            return
        # Saved only once it's deleted, so if switching away from it or
        # deleting it fails, it's still tracked, and the next sync or clean
        # finds it merged again.
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
                git.run("switch", "--quiet", "--detach", f"refs/heads/{new_parent}")
        tip = git.tip(branch)
        git.run("branch", "--quiet", "--delete", "--force", branch)
        self.save(stacks)
        self.emit(events.Untracked(branch, new_parent, children, deleted_tip=tip))

    def untrack_merged(
        self,
        branch: str,
        stacks: Stacks,
        delete: Delete | None,
        allow_different_upstream: bool = False,
        confirmed: bool = True,
        no_verify: bool = False,
        defer: Callable[[RemoteDeletion], None] | None = None,
        protected_refs: frozenset[tuple[str, str]] = frozenset(),
        check_delete: Callable[[RemoteDeletion], None] | None = None,
    ) -> bool:
        """Stop tracking merged branch, deleting what delete says, and moving its children onto its parent (see remove_merged).
        Returns False, leaving branch alone, if its remote branch couldn't be
        deleted.

        Unless confirmed (see GitAdapter.merge_confirmed), nothing is deleted,
        since the changes in parent may not be branch's.
        """
        stacks.ensure_group_available_for_merge(branch)
        git = self.git
        if not confirmed:
            parent = stacks.parent(branch)
            self.remove_merged(branch, stacks, delete=False)
            if delete is not None:
                self.emit(events.NotDeletedUnconfirmedMerge(branch, parent))
            return True
        delete_local = delete is not None and delete.deletes_local
        remote = git.remote_branch(branch)
        if remote is None or (not allow_different_upstream and not git.tracks_same_name(branch, remote[0])):
            self.remove_merged(branch, stacks, delete_local)
            return True
        upstream = git.run("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        remote_name, ref, tip = remote
        root = stacks.root(branch)
        root_ref = f"refs/heads/{git.remote_branch_name(root, remote_name)}"
        if ref == root_ref or (remote_name, ref) in protected_refs:
            self.remove_merged(branch, stacks, delete_local)
            return True
        if delete is not None and delete.deletes_remote:
            defaults = git.push_default_branches(remote_name)
            if defaults is None:
                raise StackError(f"can't determine {remote_name}'s default branch; refusing to delete {upstream}")
            if ref.removeprefix("refs/heads/") in defaults:
                self.remove_merged(branch, stacks, delete_local)
                return True
            children = tuple((child, git.remote_branch_name(child, remote_name)) for child in stacks.children(branch))
            deletion = RemoteDeletion(remote_name, ref, tip, children)
            if check_delete is not None and defer is None:
                check_delete(deletion)
            if not self.delete_merged_upstream(branch, deletion, no_verify, defer):
                return False
            self.remove_merged(branch, stacks, delete_local)
        else:
            self.remove_merged(branch, stacks, delete_local)
            self.emit(events.RemoteBranchKept(upstream, remote_name, ref.removeprefix("refs/heads/")))
        return True

    def delete_merged_upstream(
        self, branch: str, deletion: RemoteDeletion, no_verify: bool, defer: Callable[[RemoteDeletion], None] | None
    ) -> bool:
        """Queue a deletion for an atomic sync push, or delete it now."""
        if defer is not None:
            defer(deletion)
        elif self.git.delete_remote_branch(deletion.remote, deletion.ref, deletion.tip, no_verify=no_verify):
            self.emit(events.RemoteBranchDeleted(deletion.upstream))
        else:
            self.emit(events.RemoteBranchDeleteFailed(branch, deletion.upstream))
            return False
        return True

    def restore_checkout(self, original: str) -> None:
        """Switch back to original, a branch or (if it was deleted) a commit."""
        git = self.git
        if git.local_branch_exists(original):
            if git.current_branch() != original:
                git.switch(original)
        elif git.rev("HEAD") != original or git.current_branch():
            git.run("switch", "--quiet", "--detach", original)
