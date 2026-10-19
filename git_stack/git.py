import os
import subprocess

from git_stack.errors import StackError

DEFAULT_REMOTE = "origin"


class GitClient:
    """Runs git in directory, as `git -C <directory>` does."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = os.fspath(directory)

    def _command(self, *args: str) -> list[str]:
        return ["git", "-C", self.directory, *args]

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        """Run git and return stripped stdout."""
        result = subprocess.run(self._command(*args), capture_output=True, text=True, input=input)
        if check and result.returncode != 0:
            raise StackError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded."""
        return subprocess.run(self._command(*args), capture_output=True).returncode == 0

    def passthrough(self, *args: str) -> bool:
        """Run git with output going straight to the terminal."""
        return subprocess.run(self._command(*args)).returncode == 0

    def config_get(self, branch: str, key: str) -> str | None:
        value = self.run("config", "--get", f"branch.{branch}.{key}", check=False)
        return value or None

    def config_set(self, branch: str, key: str, value: str) -> None:
        self.run("config", f"branch.{branch}.{key}", value)

    def config_unset(self, branch: str, key: str, value: str | None = None) -> None:
        """Unset branch's key, or, if value is given, only if it's still value."""
        self.run("config", "--unset", *(("--fixed-value",) if value is not None else ()),
                 f"branch.{branch}.{key}", *((value,) if value is not None else ()), check=False)

    def rev(self, ref: str) -> str | None:
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) or None

    def tip(self, branch: str) -> str:
        """The commit local branch points at. Fails if branch doesn't exist."""
        return self.run("rev-parse", "--verify", f"refs/heads/{branch}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def current_branch(self) -> str | None:
        return self.run("branch", "--show-current") or None

    def local_branch_exists(self, name: str) -> bool:
        return self.ok("show-ref", "--verify", "--quiet", f"refs/heads/{name}")

    def switch(self, branch: str) -> None:
        self.run("switch", "--quiet", branch)

    def operation_in_progress(self) -> bool:
        """Whether a rebase, cherry-pick or merge has stopped and is waiting for the user.

        A cherry-pick of several commits that stopped before changing anything
        (e.g. because of an untracked file in the way) leaves only its
        sequencer state behind, not CHERRY_PICK_HEAD.
        """
        return any(os.path.exists(self.run("rev-parse", "--path-format=absolute", "--git-path", name))
                   for name in ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "sequencer", "MERGE_HEAD"))

    def has_conflicts(self) -> bool:
        """Whether the index has unmerged paths, i.e. a merge stopped on a conflict."""
        return bool(self.run("diff", "--name-only", "--diff-filter=U"))

    def worktree_of(self, branch: str) -> str | None:
        """The path of the worktree branch is checked out in, if any, or that a
        rebase of branch is in progress in. The rebase detaches that worktree's
        HEAD, so `git worktree list` doesn't show the branch."""
        path = None
        for line in self.run("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif line == f"branch refs/heads/{branch}":
                return path
            elif line == "detached" and path is not None and self.rebasing_in(path) == branch:
                return path
        return None

    def rebasing_in(self, worktree: str) -> str | None:
        """The branch a rebase in progress in worktree is rebasing, if any."""
        git_dir = GitClient(worktree).run("rev-parse", "--absolute-git-dir", check=False)
        if not git_dir:  # e.g. the worktree was deleted without `git worktree remove`
            return None
        for name in ("rebase-merge", "rebase-apply"):
            try:
                with open(os.path.join(git_dir, name, "head-name")) as f:
                    head = f.read().strip()
            except OSError:
                continue
            return head.removeprefix("refs/heads/") if head.startswith("refs/heads/") else None
        return None

    def other_worktree_of(self, branch: str) -> str | None:
        """The path of the worktree branch is checked out (or being rebased) in,
        if that isn't this one. git won't check out, rebase or delete a branch
        that's checked out or being rebased elsewhere."""
        path = self.worktree_of(branch)
        if path is None or os.path.realpath(path) == os.path.realpath(self.run("rev-parse", "--show-toplevel")):
            return None
        return path

    def upstream_gone(self, branch: str) -> bool:
        return self.run("for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch}") == "[gone]"

    def remotes(self) -> list[str]:
        return self.run("remote").split()

    def tracks_same_name(self, branch: str, remote: str) -> bool:
        """Whether branch's upstream is the branch of the same name on remote,
        which is how `git stack push` (or `git push -u`) sets it up."""
        return (self.config_get(branch, "remote") == remote
                and self.config_get(branch, "merge") == f"refs/heads/{branch}")

    def remote_branch(self, branch: str) -> tuple[str, str, str] | None:
        """Return (remote, ref, tip) for branch's upstream on a remote, or None."""
        remote = self.config_get(branch, "remote")
        ref = self.config_get(branch, "merge")
        tip = self.rev(f"{branch}@{{upstream}}")
        if not remote or remote == "." or not ref or tip is None:
            return None
        return remote, ref, tip

    def delete_remote_branch(self, remote: str, ref: str, tip: str) -> bool:
        """Delete ref on remote, failing if it no longer points at tip."""
        return self.passthrough("push", f"--force-with-lease={ref}:{tip}", remote, f":{ref}")

    def fetch(self, remote: str) -> None:
        if not self.passthrough("fetch", "--prune", remote):
            raise StackError(f"fetching {remote} failed")

    def reflog_tips(self, branch: str) -> list[str]:
        """Every commit branch has pointed at, as far back as its reflog goes."""
        return self.run("log", "--walk-reflogs", "--format=%H", f"refs/heads/{branch}", check=False).split()

    def conflicts(self, branch: str, target: str) -> bool:
        """Whether merging branch into target would conflict."""
        result = subprocess.run(
            self._command("merge-tree", "--write-tree", "--name-only", target, branch),
            capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise StackError(f"git merge-tree {target} {branch} failed:\n{result.stderr.strip()}")
        return result.returncode == 1

    def contained_in(self, branch: str, target: str) -> bool:
        """Whether merging branch into target would change nothing.

        This is true after a squash merge even though none of branch's commits are
        in target's history.
        """
        result = subprocess.run(
            self._command("merge-tree", "--write-tree", target, branch), capture_output=True, text=True)
        if result.returncode != 0:
            return False
        merged_tree = result.stdout.splitlines()[0]
        return merged_tree == self.run("rev-parse", f"{target}^{{tree}}")

    def patch_ids(self, *args: str) -> set[str]:
        """The patch-ids of the commits or diff that `git <args>` prints."""
        command, *rest = args
        patches = self.run("--literal-pathspecs", command, "--no-color", "--no-ext-diff", *rest)
        return {line.split()[0]
                for line in self.run("patch-id", "--stable", input=patches + "\n").splitlines()}

    def identities(self, *args: str) -> set[str]:
        """The author, author date and subject of each commit `git log <args>`
        lists, e.g. "Jane Doe <jane@example.com> 1700000000 +0100 Fix the parser"."""
        return set(self.run("log", "--format=%an <%ae> %ad %s", "--date=raw", *args).splitlines())
