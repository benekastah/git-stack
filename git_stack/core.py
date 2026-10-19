import os
import tempfile
from collections.abc import Callable, Iterable
from typing import TypeAlias

from git_stack import events
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

# {branch: the branch it is stacked on}
Parents: TypeAlias = dict[str, str]


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


def stack_of(branch: str, parents: Parents) -> list[str]:
    """Tracked branches in the stack containing tracked branch, parent-before-child:
    everything stacked on its bottom-most tracked ancestor, and that ancestor.
    """
    bottom = ancestry(branch, parents)[-2]
    return [bottom, *descendants(bottom, children_map(parents))]


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

    def emit(self, event: events.Event) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def tracked_branches(self) -> Parents:
        """Return {branch: parent} for every tracked branch that still exists."""
        existing = self.git.local_branches()
        return {branch: parent for (branch, key), parent in self.git.branch_config().items()
                if key == PARENT_KEY.lower() and branch in existing}

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
            raise StackError("a rebase, cherry-pick or merge is in progress; finish it with "
                             "`--continue` (or `--abort`), then rerun this command")

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
            git.config_set(branch, PARENT_KEY, pending_parent)
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
        if (pull_from and picked and git.is_ancestor(pull_from, branch)
                and picked <= git.identities(f"{pull_from}..refs/heads/{branch}")):
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
            raise BrokenStackError(f"{branch}'s recorded base isn't in its history (was it rebased by hand?); "
                                   f"fix it with `git stack track {parent} --base <sha>`")
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
            committed = git.passthrough("commit", "--quiet", "--cleanup=whitespace", f"--author={author}",
                                        f"--date={date}", f"--file={message_file}")
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
        return (not self.git.is_ancestor(base, parent_tip)
                or self.git.conflicts(f"refs/heads/{branch}", parent_tip))

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
            raise BrokenStackError(f"{branch}'s parent {parent} doesn't exist; "
                                   f"retarget it with `git stack track <parent> --base <sha>`")
        base = self.recorded_base(branch, parent)
        if not self.needs_restack(branch, parent, base, parent_tip, latest):
            # There's no rebase to wait for.
            git.config_set(branch, PARENT_KEY, parent)
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
        new_patches = git.run("rev-list", "--reverse", "--topo-order", "--no-merges", "--cherry-pick",
                              "--right-only", f"refs/heads/{branch}...{upstream_tip}").split()
        return [commit for commit in new_patches if commit in unknown]

    def pull_upstream(self, branch: str, parent: str, remote: str) -> None:
        """Cherry-pick commits that others pushed to branch's upstream onto branch.

        Only an upstream that's the branch of the same name on remote, as
        `git stack push` sets it, is pulled from. Any other (e.g. origin/main,
        for a branch made with `git switch -c <branch> origin/main`) isn't
        branch's own remote branch, and its commits aren't branch's to take.
        """
        git = self.git
        if not git.tracks_same_name(branch, remote):
            return
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip is None:
            return
        commits = self.unpulled_commits(branch, parent, upstream_tip)
        if not commits:
            return
        if (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotPulledInOtherWorktree(branch, worktree))
            return
        self.emit(events.PullingUpstream(branch, len(commits)))
        git.config_set(branch, PENDING_PULL_FROM_KEY, git.tip(branch))
        git.config_set(branch, PENDING_PULLED_KEY, upstream_tip)
        git.config_set(branch, PENDING_PULL_IDENTITIES_KEY,
                       "\n".join(sorted(git.identities("--no-walk", *commits))))
        git.switch(branch)
        if git.passthrough("cherry-pick", "--ff", *commits):
            self.settle(branch)
            return
        if git.operation_in_progress():
            raise self.stopped(f"pulling upstream commits into {branch}", "cherry-pick", "rerun `git stack sync`")
        git.config_unset(branch, PENDING_PULLED_KEY)
        git.config_unset(branch, PENDING_PULL_FROM_KEY)
        git.config_unset(branch, PENDING_PULL_IDENTITIES_KEY)
        raise StackError(f"pulling upstream commits into {branch} failed; see git's output above")

    def stopped(self, what: str, command: str, then: str) -> StackError:
        """The error for a rebase or cherry-pick (command) that stopped partway
        through what, saying how to carry on."""
        if self.git.has_conflicts():
            return StackError(f"{what} stopped on a conflict. Resolve it, run `git {command} --continue`, "
                              f"then {then}")
        # `git rebase --continue` retries the commit that couldn't be applied,
        # but `git cherry-pick --continue` skips it, so a cherry-pick has to be
        # started over.
        carry_on = "`git rebase --continue`" if command == "rebase" else f"`git {command} --abort`"
        return StackError(f"{what} stopped without a conflict; see git's output above (e.g. an untracked "
                          f"file is in the way: move or delete it). Fix that, run {carry_on}, then {then}")

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
        for key in (PARENT_KEY, BASE_KEY, PULLED_KEY, PENDING_PARENT_KEY, PENDING_BASE_KEY,
                    PENDING_PULL_FROM_KEY, PENDING_PULLED_KEY, PENDING_PULL_IDENTITIES_KEY):
            self.git.config_unset(branch, key)

    def remove_merged(self, branch: str, parents: Parents, delete: bool) -> None:
        """Move branch's children onto its parent, then stop tracking branch.

        If delete is set, the branch itself is deleted too, unless it's checked
        out in another worktree. If it was checked out here, its first child (or
        its parent) that isn't checked out elsewhere is checked out instead, or
        failing that, the parent's commit, detached.
        """
        git = self.git
        new_parent = parents[branch]
        children = tuple(drop_from_stack(branch, parents))
        for child in children:
            git.config_set(child, PARENT_KEY, new_parent)
        if delete and (worktree := git.other_worktree_of(branch)) is not None:
            self.emit(events.NotDeletedInOtherWorktree(branch, worktree))
            delete = False
        if not delete:
            self.forget(branch)
            self.emit(events.Untracked(branch, new_parent, children, deleted_tip=None))
            return
        if git.current_branch() == branch:
            target = next((b for b in (*sorted(children), new_parent)
                           if git.local_branch_exists(b) and git.worktree_of(b) is None), None)
            if target is not None:
                git.switch(target)
            else:
                git.run("switch", "--quiet", "--detach", new_parent)
        tip = git.tip(branch)
        git.run("branch", "--quiet", "--delete", "--force", branch)
        self.emit(events.Untracked(branch, new_parent, children, deleted_tip=tip))

    def untrack_merged(self, branch: str, parents: Parents, delete: str | None) -> bool:
        """Stop tracking merged branch, deleting what delete ("local", "remote" or
        "both") says. Returns False, leaving branch alone, if its remote branch
        couldn't be deleted.
        """
        git = self.git
        delete_local = delete in ("local", "both")
        remote = git.remote_branch(branch)
        if remote is None:
            self.remove_merged(branch, parents, delete_local)
            return True
        upstream = git.run("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        remote_name, ref, tip = remote
        if delete in ("remote", "both"):
            if not git.delete_remote_branch(remote_name, ref, tip):
                self.emit(events.RemoteBranchDeleteFailed(branch, upstream))
                return False
            self.emit(events.RemoteBranchDeleted(upstream))
            self.remove_merged(branch, parents, delete_local)
        else:
            self.remove_merged(branch, parents, delete_local)
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
