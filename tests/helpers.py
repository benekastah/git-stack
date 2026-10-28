"""Test repositories and a fake GitHub, which tests/conftest.py provides as fixtures."""

import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

import pytest

from git_stack import events
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, StackClient


@dataclass
class Repo:
    """A clone (path) of a bare repository (origin), plus a second clone of it
    (forge) standing in for GitHub's merge button.

    git, git stack and client run in path, with the isolated environment the
    repo fixture sets. emitted collects client's events.
    """

    path: pathlib.Path
    origin: pathlib.Path
    forge: pathlib.Path
    tmp: pathlib.Path
    emitted: list[events.Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = StackClient(GitClient(self.path), on_event=self.emitted.append)

    def git(self, *args: str, cwd: pathlib.Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.path, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            pytest.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def stack(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        """Run `git stack <args>` as a separate process, failing the test unless it
        succeeds (or, with expect_success=False, fails)."""
        result = subprocess.run(
            [sys.executable, "-m", "git_stack", "-C", str(self.path), *args],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if expect_success and result.returncode != 0:
            pytest.fail(f"git stack {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        if not expect_success and result.returncode == 0:
            pytest.fail(f"git stack {' '.join(args)} unexpectedly succeeded:\n{result.stdout}")
        return result

    def write(self, name: str, content: str) -> None:
        (self.path / name).write_text(content)

    def commit(self, name: str, content: str, message: str) -> None:
        self.write(name, content)
        self.git("commit", "--quiet", "-am", message)

    def make_stack(self) -> None:
        """main <- pr1 (two commits to a) <- pr2 (b) <- pr3 (c)."""
        self.client.branch("pr1")
        self.commit("a", "pr1 first\n", "pr1 c1")
        self.commit("a", "pr1 second\n", "pr1 c2")
        self.client.branch("pr2")
        self.commit("b", "pr2\n", "pr2 c1")
        self.client.branch("pr3")
        self.commit("c", "pr3\n", "pr3 c1")

    def squash_merge_on_forge(self, branch: str, delete_branch: bool = True) -> None:
        """Squash-merge origin/<branch> into origin/main and (by default) delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", f"origin/{branch}", cwd=self.forge)
        self.git("commit", "--quiet", "-m", f"{branch} (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", *([f":{branch}"] if delete_branch else []), cwd=self.forge)

    def merge_on_forge(self, branch: str) -> None:
        """Merge origin/<branch> into origin/main with a merge commit, and delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--no-ff", "--no-edit", f"origin/{branch}", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", f":{branch}", cwd=self.forge)

    def revert_on_forge(self, *args: str) -> None:
        """Run `git revert <args>` on origin/main, as a maintainer would."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("revert", "--no-edit", *args, cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", cwd=self.forge)

    def commit_on_forge(
        self,
        branch: str,
        name: str,
        content: str,
        message: str,
        author: str = "Collaborator <collaborator@example.com>",
    ) -> None:
        """Push a commit to origin/<branch>, as a collaborator (or author, from
        another machine) would."""
        self.git("fetch", "--quiet", cwd=self.forge)
        self.git("switch", "--quiet", "-C", branch, f"origin/{branch}", cwd=self.forge)
        (self.forge / name).write_text(content)
        self.git("add", name, cwd=self.forge)
        self.git("commit", "--quiet", f"--author={author}", "-m", message, cwd=self.forge)
        self.git("push", "--quiet", "origin", branch, cwd=self.forge)

    def push_someone_elses_branch(self, branch: str) -> None:
        """Push a branch called branch, with a commit "theirs" on main, as a
        coworker would, and fetch it."""
        self.git("fetch", "--quiet", cwd=self.forge)
        self.git("switch", "--quiet", "-C", branch, "origin/main", cwd=self.forge)
        (self.forge / "theirs").write_text("theirs\n")
        self.git("add", "theirs", cwd=self.forge)
        self.git("commit", "--quiet", "-m", "theirs", cwd=self.forge)
        self.git("push", "--quiet", "origin", branch, cwd=self.forge)
        self.git("fetch", "--quiet")

    def remote_subject(self, branch: str) -> str:
        """Subject of the commit at <branch> on the remote itself."""
        return self.git("log", "-1", "--format=%s", branch, cwd=self.origin)

    def cached_check(self, branch: str, key: str) -> str | None:
        """branch's key in the check cache (see git_stack.check_cache), as on disk."""
        try:
            entries: dict[str, dict[str, str]] = json.loads((self.path / ".git" / "git-stack-cache.json").read_text())
        except FileNotFoundError:
            return None
        return entries.get(branch, {}).get(key)

    def config(self, branch: str, key: str) -> str:
        return self.git("config", f"branch.{branch}.{key}")

    def config_exists(self, branch: str, key: str) -> bool:
        return (
            subprocess.run(
                ["git", "config", f"branch.{branch}.{key}"], cwd=self.path, capture_output=True, check=False
            ).returncode
            == 0
        )

    def own_commits(self, branch: str, parent: str) -> list[str]:
        return self.git("log", "--format=%s", f"{parent}..{branch}").splitlines()

    def branch_info(self, name: str) -> BranchInfo:
        """name's entry in client.info()."""
        return next(branch for branch in self.client.info() if branch.name == name)

    def assert_untracked(self, branch: str) -> None:
        """branch still exists but is no longer in a stack."""
        assert self.git("branch", "--list", branch)
        assert not self.config_exists(branch, "stackParent")
        assert not self.config_exists(branch, "stackBase")

    def assert_stacked(self, branch: str, parent: str, subjects: list[str]) -> None:
        """branch sits directly on parent's tip with exactly the given commits."""
        assert self.config(branch, "stackParent") == parent
        assert self.config(branch, "stackBase") == self.git("rev-parse", parent)
        assert self.own_commits(branch, parent) == subjects


@dataclass
class FakeGitHub:
    """The state of tests/fake_gh.py, which stands in for the gh CLI."""

    state_path: pathlib.Path

    def load(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads(self.state_path.read_text())
        return state

    def save(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state))

    def add_pr(self, **pr: Any) -> None:
        state = self.load()
        state["prs"].append({"isCrossRepository": False, **pr})
        self.save(state)

    def calls(self, *prefix: str) -> list[list[str]]:
        """The gh commands run so far that start with prefix."""
        return [call for call in self.load()["calls"] if call[: len(prefix)] == list(prefix)]

    def clear_calls(self) -> None:
        state = self.load()
        state["calls"] = []
        self.save(state)

    def fail(self, *commands: str) -> None:
        """Make gh fail for commands whose first argument is one of commands."""
        state = self.load()
        state["fail"] = list(commands)
        self.save(state)

    def fail_opening(self, *heads: str) -> None:
        """Make gh fail to open pull requests of the branches heads."""
        state = self.load()
        state["fail_heads"] = list(heads)
        self.save(state)
