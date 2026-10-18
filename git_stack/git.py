import os
import subprocess

from git_stack.errors import StackError

DEFAULT_REMOTE = "origin"


class GitClient:
    """Runs git in the current directory."""

    def run(self, *args, check=True, input=None):
        """Run git and return stripped stdout."""
        result = subprocess.run(["git", *args], capture_output=True, text=True, input=input)
        if check and result.returncode != 0:
            raise StackError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def ok(self, *args):
        """Run git and report whether it succeeded."""
        return subprocess.run(["git", *args], capture_output=True).returncode == 0

    def passthrough(self, *args):
        """Run git with output going straight to the terminal."""
        return subprocess.run(["git", *args]).returncode == 0

    def config_get(self, branch, key):
        value = self.run("config", "--get", f"branch.{branch}.{key}", check=False)
        return value or None

    def config_set(self, branch, key, value):
        self.run("config", f"branch.{branch}.{key}", value)

    def config_unset(self, branch, key):
        self.run("config", "--unset", f"branch.{branch}.{key}", check=False)

    def rev(self, ref):
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) or None

    def is_ancestor(self, ancestor, descendant):
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def current_branch(self):
        return self.run("branch", "--show-current") or None

    def local_branch_exists(self, name):
        return self.ok("show-ref", "--verify", "--quiet", f"refs/heads/{name}")

    def switch(self, branch):
        self.run("switch", "--quiet", branch)

    def operation_in_progress(self):
        """Whether a rebase, cherry-pick or merge has stopped and is waiting for the user."""
        return any(os.path.exists(self.run("rev-parse", "--git-path", name))
                   for name in ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "MERGE_HEAD"))

    def upstream_gone(self, branch):
        return self.run("for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch}") == "[gone]"

    def remote_branch(self, branch):
        """Return (remote, ref, tip) for branch's upstream on a remote, or None."""
        remote = self.config_get(branch, "remote")
        ref = self.config_get(branch, "merge")
        tip = self.rev(f"{branch}@{{upstream}}")
        if not remote or remote == "." or not ref or tip is None:
            return None
        return remote, ref, tip

    def delete_remote_branch(self, remote, ref, tip):
        """Delete ref on remote, failing if it no longer points at tip."""
        return self.passthrough("push", f"--force-with-lease={ref}:{tip}", remote, f":{ref}")

    def fetch(self, remote):
        if not self.passthrough("fetch", "--prune", remote):
            raise StackError(f"fetching {remote} failed")

    def reflog_tips(self, branch):
        """Every commit branch has pointed at, as far back as its reflog goes."""
        return self.run("log", "--walk-reflogs", "--format=%H", f"refs/heads/{branch}", check=False).split()

    def contained_in(self, branch, target):
        """Whether merging branch into target would change nothing.

        This is true after a squash merge even though none of branch's commits are
        in target's history.
        """
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", target, branch], capture_output=True, text=True)
        if result.returncode != 0:
            return False
        merged_tree = result.stdout.splitlines()[0]
        return merged_tree == self.run("rev-parse", f"{target}^{{tree}}")

    def patch_ids(self, *args):
        """The patch-ids of the commits or diff that `git <args>` prints."""
        command, *rest = args
        patches = self.run("--literal-pathspecs", command, "--no-color", "--no-ext-diff", *rest)
        return {line.split()[0]
                for line in self.run("patch-id", "--stable", input=patches + "\n").splitlines()}
