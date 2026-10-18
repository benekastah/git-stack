from git_stack import events
from git_stack.errors import BrokenStackError, StackError

PARENT_KEY = "stackParent"
BASE_KEY = "stackBase"
PULLED_KEY = "stackPulled"
PR_KEY = "stackPr"
# Written before a rebase starts and promoted to PARENT_KEY/BASE_KEY once the
# rebase is known to have finished. This lets the user resolve conflicts with a
# plain `git rebase --continue` (or back out with `git rebase --abort`).
PENDING_PARENT_KEY = "stackPendingParent"
PENDING_BASE_KEY = "stackPendingBase"
# Same idea for pulling upstream commits with `git cherry-pick`: the branch tip
# before the pull, and the upstream tip being pulled.
PENDING_PULL_FROM_KEY = "stackPendingPullFrom"
PENDING_PULLED_KEY = "stackPendingPulled"


# The functions below work on a {branch: parent} map like the one
# StackCore.tracked_branches returns, without touching the repository.

def children_map(parents):
    children = {}
    for branch, parent in sorted(parents.items()):
        children.setdefault(parent, []).append(branch)
    return children


def descendants(root, children):
    """Return root's descendants in parent-before-child order (root excluded)."""
    order = []
    for child in children.get(root, []):
        order.append(child)
        order.extend(descendants(child, children))
    return order


def ancestry(branch, parents):
    """Return [branch, its parent, ..., the first untracked ancestor]."""
    chain = [branch]
    while chain[-1] in parents:
        parent = parents[chain[-1]]
        if parent in chain:
            raise StackError(f"cycle in stack metadata involving {parent}")
        chain.append(parent)
    return chain


def stack_root(branch, parents):
    """Walk up from branch to the first untracked ancestor (usually main)."""
    return ancestry(branch, parents)[-1]


def ensure_no_cycle(branch, new_parent, parents):
    if branch in ancestry(new_parent, parents):
        raise StackError(f"{new_parent} is stacked on {branch}; that would create a cycle")


def stack_of(branch, parents):
    """Tracked branches in the stack containing tracked branch, parent-before-child:
    everything stacked on its bottom-most tracked ancestor, and that ancestor.
    """
    bottom = ancestry(branch, parents)[-2]
    return [bottom, *descendants(bottom, children_map(parents))]


def drop_from_stack(branch, parents):
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

    def __init__(self, git, on_event=None):
        self.git = git
        self.on_event = on_event

    def emit(self, event):
        if self.on_event is not None:
            self.on_event(event)

    def tracked_branches(self):
        """Return {branch: parent} for every tracked branch that still exists."""
        out = self.git.run("config", "--null", "--get-regexp", rf"^branch\..*\.{PARENT_KEY.lower()}$",
                           check=False)
        suffix = f".{PARENT_KEY.lower()}"
        parents = {}
        for entry in filter(None, out.split("\0")):
            key, _, value = entry.partition("\n")
            branch = key[len("branch."):-len(suffix)]
            if self.git.local_branch_exists(branch):
                parents[branch] = value
        return parents

    def current_stacked_branch(self, parents, command, hint=None):
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

    def ensure_no_operation_in_progress(self):
        if self.git.operation_in_progress():
            raise StackError("a rebase, cherry-pick or merge is in progress; finish it with "
                             "`--continue` (or `--abort`), then rerun this command")

    def ensure_clean_worktree(self):
        if self.git.run("status", "--porcelain", "--untracked-files=no"):
            raise StackError("you have uncommitted changes; commit or stash them first")

    def settle(self, branch):
        """Record the result of an interrupted restack or pull, if there was one.

        If the rebase finished, the pending parent commit is now in the branch's
        history and becomes the new base. If it was aborted, it isn't, and the
        previous parent and base are kept.

        Likewise, if the cherry-pick of upstream commits moved the branch forward,
        the upstream tip counts as pulled. If it was aborted, the branch is back
        where it started and the next sync tries again.

        Must not be called while a rebase or cherry-pick is in progress, since the
        branch hasn't been updated yet.
        """
        git = self.git
        pending_parent = git.config_get(branch, PENDING_PARENT_KEY)
        if pending_parent is not None:
            pending_base = git.config_get(branch, PENDING_BASE_KEY)
            if pending_base and git.is_ancestor(pending_base, branch):
                git.config_set(branch, PARENT_KEY, pending_parent)
                git.config_set(branch, BASE_KEY, pending_base)
            git.config_unset(branch, PENDING_PARENT_KEY)
            git.config_unset(branch, PENDING_BASE_KEY)

        pending_pulled = git.config_get(branch, PENDING_PULLED_KEY)
        if pending_pulled is not None:
            pull_from = git.config_get(branch, PENDING_PULL_FROM_KEY)
            if pull_from and git.rev(branch) != pull_from and git.is_ancestor(pull_from, branch):
                git.config_set(branch, PULLED_KEY, pending_pulled)
            git.config_unset(branch, PENDING_PULLED_KEY)
            git.config_unset(branch, PENDING_PULL_FROM_KEY)

    def settle_all(self):
        for branch in self.tracked_branches():
            self.settle(branch)

    def recorded_base(self, branch, parent):
        """Return branch's base, checking that it is still in the branch's history."""
        base = self.git.config_get(branch, BASE_KEY)
        if base is None or not self.git.is_ancestor(base, branch):
            raise BrokenStackError(f"{branch}'s recorded base isn't in its history (was it rebased by hand?); "
                                   f"fix it with `git stack track {parent} --base <sha>`")
        return base

    def squash_branch(self, branch, parent):
        """Squash branch's own commits into one, keeping its base.

        The commit gets every squashed commit's message, oldest first, and the
        oldest commit's author and date.
        """
        git = self.git
        base = self.recorded_base(branch, parent)
        commits = git.run("rev-list", "--reverse", f"{base}..refs/heads/{branch}").split()
        if len(commits) < 2:
            return
        self.emit(events.Squashing(branch, len(commits)))
        message = "\n\n".join(git.run("show", "--no-patch", "--format=%B", c) for c in commits)
        author, date = git.run("show", "--no-patch", "--format=%an <%ae>%x00%aI", commits[0]).split("\0")
        git.switch(branch)
        git.run("reset", "--quiet", "--soft", base)
        git.run("commit", "--quiet", "--no-verify", "--cleanup=whitespace",
                f"--author={author}", f"--date={date}", "--file=-", input=message)

    def restack_branch(self, branch, parent):
        """Rebase branch's own commits onto parent's current tip.

        parent may differ from the recorded one (restack --onto); it is recorded
        only once the rebase has finished.
        """
        git = self.git
        parent_tip = git.rev(parent)
        if parent_tip is None:
            raise BrokenStackError(f"{branch}'s parent {parent} doesn't exist; "
                                   f"retarget it with `git stack track <parent> --base <sha>`")
        base = self.recorded_base(branch, parent)
        if base == parent_tip:
            # Nothing to replay, so there's no rebase to wait for.
            git.config_set(branch, PARENT_KEY, parent)
            return
        self.emit(events.Restacking(branch, parent))
        git.config_set(branch, PENDING_PARENT_KEY, parent)
        git.config_set(branch, PENDING_BASE_KEY, parent_tip)
        if git.passthrough("rebase", "--no-update-refs", "--onto", parent_tip, base, branch):
            self.settle(branch)
        elif git.operation_in_progress():
            raise StackError(f"restacking {branch} stopped on a conflict. Resolve it, run "
                             f"`git rebase --continue`, then rerun this command to finish the stack")
        else:
            git.config_unset(branch, PENDING_PARENT_KEY)
            git.config_unset(branch, PENDING_BASE_KEY)
            raise StackError(f"restacking {branch} onto {parent} failed; see git's output above")

    def restack_all(self, branches, parents):
        original = self.git.current_branch()
        for branch in branches:
            self.restack_branch(branch, parents[branch])
        if original and self.git.current_branch() != original:
            self.git.switch(original)

    def unpulled_commits(self, branch, parent, upstream_tip):
        """Non-merge commits on upstream_tip that branch doesn't have, oldest first.

        A commit counts as already present if branch (or parent) contains it or an
        equivalent patch, if branch ever pointed at it or a descendant of it (so the
        old copies of commits we have since rebased don't count), or if an earlier
        pull already took it.
        """
        git = self.git
        known = [f"refs/heads/{branch}", *git.reflog_tips(branch)]
        for ref in (parent, git.config_get(branch, PULLED_KEY)):
            if ref and git.rev(ref):
                known.append(git.rev(ref))
        unknown = set(git.run("rev-list", "--no-merges", upstream_tip, "--not", *known).split())
        if not unknown:
            return []
        new_patches = git.run("rev-list", "--reverse", "--topo-order", "--no-merges", "--cherry-pick",
                              "--right-only", f"refs/heads/{branch}...{upstream_tip}").split()
        return [commit for commit in new_patches if commit in unknown]

    def pull_upstream(self, branch, parent):
        """Cherry-pick commits that others pushed to branch's upstream onto branch."""
        git = self.git
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if upstream_tip is None:
            return
        commits = self.unpulled_commits(branch, parent, upstream_tip)
        if not commits:
            return
        self.emit(events.PullingUpstream(branch, len(commits)))
        git.config_set(branch, PENDING_PULL_FROM_KEY, git.rev(branch))
        git.config_set(branch, PENDING_PULLED_KEY, upstream_tip)
        git.switch(branch)
        if not git.passthrough("cherry-pick", "--ff", *commits):
            raise StackError(f"pulling upstream commits into {branch} stopped on a conflict. Resolve it, "
                             f"run `git cherry-pick --continue`, then rerun `git stack sync`")
        self.settle(branch)

    def fast_forward_trunks(self, roots):
        """Fast-forward untracked local parents (e.g. main) to their upstreams."""
        git = self.git
        current = git.current_branch()
        for root in roots:
            if not git.local_branch_exists(root):
                continue
            upstream = git.run("rev-parse", "--abbrev-ref", f"{root}@{{upstream}}", check=False)
            if not upstream:
                continue
            old, new = git.rev(root), git.rev(upstream)
            if old == new:
                continue
            if not git.is_ancestor(old, new):
                self.emit(events.TrunkDiverged(root, upstream))
                continue
            if root == current:
                git.run("merge", "--ff-only", "--quiet", upstream)
            else:
                git.run("update-ref", f"refs/heads/{root}", new, old)
            self.emit(events.FastForwarded(root, upstream))

    def untrack(self, branch):
        for key in (PARENT_KEY, BASE_KEY, PULLED_KEY, PR_KEY, PENDING_PARENT_KEY, PENDING_BASE_KEY,
                    PENDING_PULL_FROM_KEY, PENDING_PULLED_KEY):
            self.git.config_unset(branch, key)

    def remove_merged(self, branch, parents, delete):
        """Move branch's children onto its parent, then stop tracking branch.

        If delete is set, the branch itself is deleted too, and if it was checked
        out, its first child (or, without children, its parent) is checked out
        instead.
        """
        git = self.git
        new_parent = parents[branch]
        children = tuple(drop_from_stack(branch, parents))
        for child in children:
            git.config_set(child, PARENT_KEY, new_parent)
        if not delete:
            self.untrack(branch)
            self.emit(events.Untracked(branch, new_parent, children, deleted_tip=None))
            return
        if git.current_branch() == branch:
            if children:
                git.switch(sorted(children)[0])
            elif git.local_branch_exists(new_parent):
                git.switch(new_parent)
            else:
                git.run("switch", "--quiet", "--detach", new_parent)
        tip = git.rev(branch)
        git.run("branch", "--quiet", "--delete", "--force", branch)
        self.emit(events.Untracked(branch, new_parent, children, deleted_tip=tip))

    def untrack_merged(self, branch, parents, delete):
        """Stop tracking merged branch, deleting what delete ("local", "remote" or
        "both") says. Returns False, leaving branch alone, if its remote branch
        couldn't be deleted.
        """
        git = self.git
        remote = git.remote_branch(branch)
        upstream = git.run("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") if remote else None
        delete_remote = delete in ("remote", "both")
        if remote and delete_remote:
            if not git.delete_remote_branch(*remote):
                self.emit(events.RemoteBranchDeleteFailed(branch, upstream))
                return False
            self.emit(events.RemoteBranchDeleted(upstream))
        self.remove_merged(branch, parents, delete in ("local", "both"))
        if remote and not delete_remote:
            remote_name, ref, _ = remote
            self.emit(events.RemoteBranchKept(upstream, remote_name, ref.removeprefix("refs/heads/")))
        return True

    def restore_checkout(self, original):
        """Switch back to original, a branch or (if it was deleted) a commit."""
        git = self.git
        if git.local_branch_exists(original):
            if git.current_branch() != original:
                git.switch(original)
        elif git.rev("HEAD") != original or git.current_branch():
            git.run("switch", "--quiet", "--detach", original)
