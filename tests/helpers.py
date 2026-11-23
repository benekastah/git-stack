"""Test repositories and a fake GitHub, which tests/conftest.py provides as fixtures."""

import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from git_stack import cli, events
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, StackClient
from tests import fake_gh, operations
from tests.git_support import CLOSE_FDS, real_git


@contextmanager
def captured_output() -> Generator[list[str]]:
    """Capture what this process, and the processes it starts, write to stdout
    and stderr, as [stdout, stderr], filled in on leaving. Their file
    descriptors are pointed at files for the duration, and sys.stdout and
    sys.stderr write to them as UTF-8, as git_stack.cli.main makes them."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_streams = sys.stdout, sys.stderr
    saved_fds = os.dup(1), os.dup(2)
    output: list[str] = []
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            with (
                open(1, "w", encoding="utf-8", closefd=False) as stdout,
                open(2, "w", encoding="utf-8", closefd=False) as stderr,
            ):
                sys.stdout, sys.stderr = stdout, stderr
                try:
                    yield output
                finally:
                    sys.stdout, sys.stderr = saved_streams
        finally:
            for fd, saved in zip((1, 2), saved_fds, strict=True):
                os.dup2(saved, fd)
                os.close(saved)
        for f in (out, err):
            f.seek(0)
            # Text mode, as subprocess.run's, so Windows' \r\n reads as \n.
            text = io.TextIOWrapper(f, encoding="utf-8")
            output.append(text.read())
            # Leave f to the with block to close.
            text.detach()


def install_command(bin_dir: pathlib.Path, name: str, module: str) -> None:
    """Make name, run from bin_dir (on PATH), run module's main() with this
    Python, as git-stack runs gh or ssh. module must be importable, as the
    repo fixture's PYTHONPATH makes the tests' modules."""
    if sys.platform == "win32":
        # Windows only runs .exe files found on PATH, so wrap it in distlib's
        # launcher, which pip makes console scripts with.
        import distlib.scripts  # pyright: ignore[reportMissingTypeStubs]

        # Any: distlib has no types, and what's inferred from its source is too narrow.
        maker: Any = distlib.scripts.ScriptMaker(None, str(bin_dir))
        maker.executable = sys.executable
        maker.variants = {""}
        maker.make(f"{name} = {module}:main")
    else:
        command = bin_dir / name
        command.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m {module} "$@"\n', encoding="utf-8")
        command.chmod(0o755)


@dataclass
class Repo:
    """A clone (path) of a bare repository (origin), plus a second clone of it
    (forge) standing in for GitHub's merge button.

    git, git stack and client run in path, with the isolated environment the
    repo fixture sets. emitted collects client's events. The tests' own git
    commands run directly, outside the git_calls recorder.
    """

    path: pathlib.Path
    origin: pathlib.Path
    forge: pathlib.Path
    tmp: pathlib.Path
    emitted: list[events.Event] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.client = StackClient(GitClient(self.path), on_event=self.emitted.append)

    def git(self, *args: str, cwd: pathlib.Path | None = None) -> str:
        # With -C rather than cwd, which would keep Python from using posix_spawn.
        result = subprocess.run(
            [real_git(), "-C", str(cwd or self.path), *args],
            capture_output=True,
            encoding="utf-8",
            close_fds=CLOSE_FDS,
            check=False,
        )
        if result.returncode != 0:
            pytest.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def stack(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        """Run `git stack <args>` as the git-stack command does, through
        git_stack.cli.main, but in this process, which is much quicker than
        starting another, failing the test unless it succeeds (or, with
        expect_success=False, fails). What it, and the git commands it runs,
        print is captured as a separate process's would be."""
        argv = ["-C", str(self.path), *args]
        with captured_output() as output:
            try:
                status = cli.main(argv)
            except SystemExit as e:  # argparse's, for arguments it doesn't understand
                status = 0 if e.code is None else e.code if isinstance(e.code, int) else 1
        return self._check_result(args, subprocess.CompletedProcess(argv, status, *output), expect_success)

    def stack_process(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        """stack, but as a separate process, the way users run it: nothing Python caches in this
        process (what a GitClient has learned, say) carries over from one run to the next.

        In an `expect_operations` block, the git and gh commands it runs are counted from outside
        (see tests/operations.py), by tracing git in the new process only; without one, the
        process gets this process's environment as it is."""
        with operations.traced_subprocess() as environment:
            result = subprocess.run(
                [sys.executable, "-m", "tests.stack_entry", "-C", str(self.path), *args],
                cwd=self.tmp,
                capture_output=True,
                encoding="utf-8",
                env=environment,
                timeout=60,
                check=False,
            )
        return self._check_result(args, result, expect_success)

    def _check_result(
        self, args: tuple[str, ...], result: subprocess.CompletedProcess[str], expect_success: bool
    ) -> subprocess.CompletedProcess[str]:
        if expect_success and result.returncode != 0:
            pytest.fail(f"git stack {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        if not expect_success and result.returncode == 0:
            pytest.fail(f"git stack {' '.join(args)} unexpectedly succeeded:\n{result.stdout}")
        return result

    def write(self, name: str, content: str) -> None:
        (self.path / name).write_text(content, encoding="utf-8", newline="")

    def add_hook(self, repository: pathlib.Path, name: str, script: str) -> None:
        """Make script, a shell script, repository's hook name (e.g. pre-push)."""
        hook = repository / ("hooks" if repository == self.origin else ".git/hooks") / name
        # With \n line endings, which sh needs, even on Windows.
        hook.write_text(script, encoding="utf-8", newline="\n")
        hook.chmod(0o755)

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

    def make_chain(self, size: int) -> list[str]:
        """main <- chain1 <- chain2 <- ... <- chain<size>, each with a commit of its own file, and the
        last checked out. Returns the branches, bottom first.

        Each branch's commit is a new file of its own, so restacking one onto a changed parent
        never conflicts, which a performance test's count shouldn't depend on. (`commit` changes
        a file that's already there, which is what makes a conflict.)"""
        names = [f"chain{i}" for i in range(1, size + 1)]
        for name in names:
            # In this process, as the test's setup: it isn't what the test counts.
            self.client.branch(name)
            self.write(f"{name}.txt", f"{name}\n")
            self.git("add", f"{name}.txt")
            self.git("commit", "--quiet", "-m", f"{name} c1")
        return names

    def squash_merge_on_forge(self, branch: str, delete_branch: bool = True) -> None:
        """Squash-merge origin/<branch> into origin/main and (by default) delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", f"origin/{branch}", cwd=self.forge)
        self.git("commit", "--quiet", "-m", f"{branch} (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", *([f":{branch}"] if delete_branch else []), cwd=self.forge)

    def merge_on_forge(self, branch: str, delete_branch: bool = True) -> None:
        """Merge origin/<branch> into origin/main with a merge commit, and (by default) delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--no-ff", "--no-edit", f"origin/{branch}", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", *([f":{branch}"] if delete_branch else []), cwd=self.forge)

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
        (self.forge / name).write_text(content, encoding="utf-8", newline="")
        self.git("add", name, cwd=self.forge)
        self.git("commit", "--quiet", f"--author={author}", "-m", message, cwd=self.forge)
        self.git("push", "--quiet", "origin", branch, cwd=self.forge)

    def merge_with_an_edit(self, other: str, name: str, content: str, cwd: pathlib.Path | None = None) -> None:
        """Merge other into the current branch (in cwd, default: path), writing
        content to name in the merge commit, so the merge changes something
        neither of its parents does, as resolving a conflict can."""
        cwd = cwd or self.path
        self.git("merge", "--quiet", "--no-ff", "--no-commit", other, cwd=cwd)
        (cwd / name).write_text(content, encoding="utf-8", newline="")
        self.git("add", name, cwd=cwd)
        self.git("commit", "--quiet", "--no-edit", cwd=cwd)

    def rebase_stack_on_forge(self, *branches: str) -> None:
        """Rebase origin/<branch> for each of branches, a stack bottom first,
        onto origin/main's tip or the rebased branch before it, and force-push
        them, as GitHub's "Rebase stack" button would. Then fetch."""
        self.git("fetch", "--quiet", cwd=self.forge)
        old_parent = self.git("merge-base", "origin/main", f"origin/{branches[0]}", cwd=self.forge)
        new_parent = "origin/main"
        for branch in branches:
            old_tip = self.git("rev-parse", f"origin/{branch}", cwd=self.forge)
            self.git("switch", "--quiet", "-C", branch, old_tip, cwd=self.forge)
            self.git("rebase", "--quiet", "--onto", new_parent, old_parent, cwd=self.forge)
            old_parent, new_parent = old_tip, branch
        self.git("push", "--quiet", "--force", "origin", *branches, cwd=self.forge)
        self.git("fetch", "--quiet")

    def push_someone_elses_branch(self, branch: str) -> None:
        """Push a branch called branch, with a commit "theirs" on main, as a
        coworker would, and fetch it."""
        self.git("fetch", "--quiet", cwd=self.forge)
        self.git("switch", "--quiet", "-C", branch, "origin/main", cwd=self.forge)
        (self.forge / "theirs").write_text("theirs\n", encoding="utf-8", newline="")
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
                [real_git(), "-C", str(self.path), "config", f"branch.{branch}.{key}"],
                capture_output=True,
                close_fds=CLOSE_FDS,
                check=False,
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


class InProcessGh:
    """The subprocess module, but for running gh, which tests/fake_gh.py's
    run answers in this process, as the gh command it stands in for would."""

    _lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    @staticmethod
    def run(
        args: list[str],
        *,
        input: bytes | str | None = None,
        encoding: str | None = None,
        check: bool = False,
        **kwargs: Any,
    ) -> Any:
        if args[0] != "gh":
            return subprocess.run(args, input=input, encoding=encoding, check=check, **kwargs)
        # Only the ways git-stack runs gh: its output captured, with or without input.
        assert kwargs == {"capture_output": True}, kwargs
        stdin = input.encode(encoding or "utf-8") if isinstance(input, str) else input or b""
        with InProcessGh._lock:
            status, out, err = fake_gh.run(args[1:], lambda: stdin)
        result: subprocess.CompletedProcess[Any] = (
            subprocess.CompletedProcess(args, status, out, err)
            if encoding is None
            else subprocess.CompletedProcess(args, status, out.decode(encoding), err.decode(encoding))
        )
        if check:
            result.check_returncode()
        return result


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

    def enable_stacks(self, *stacks: list[int]) -> None:
        """Give the repository GitHub's stacked pull requests, with stacks, each
        its pull requests' numbers, bottom first."""
        state = self.load()
        state["stacks"] = [{"number": 1000 + i, "prs": list(prs)} for i, prs in enumerate(stacks)]
        state["next_stack"] = 1000 + len(stacks)
        self.save(state)

    def stacks(self) -> list[list[int]]:
        """The pull requests of each stack on GitHub, bottom first."""
        return [stack["prs"] for stack in self.load().get("stacks", [])]

    def fail_editing(self, *numbers: int) -> None:
        """Make gh fail to edit the pull requests numbers."""
        state = self.load()
        state["fail_edits"] = list(numbers)
        self.save(state)

    def fail_opening(self, *heads: str) -> None:
        """Make gh fail to open pull requests of the branches heads."""
        state = self.load()
        state["fail_heads"] = list(heads)
        self.save(state)


def mentions(output: str, branch: str) -> bool:
    """Whether output names branch, as a whole name: `chain1` isn't in a line about `chain10`."""
    return re.search(rf"(?<![\w-]){re.escape(branch)}(?![\w-])", output) is not None


def chain_tips(repo: Repo, chain: list[str]) -> dict[str, str]:
    """The commit each branch of a chain (see Repo.make_chain) is at."""
    return {branch: repo.git("rev-parse", branch) for branch in chain}


def assert_chain_stacked(repo: Repo, chain: list[str]) -> None:
    """Each branch of a chain is recorded on the one before it (the first on main), which is in its
    history. Performance tests check this, which is what a command is for, because running fewer
    operations than allotted only warns, so one that stopped doing its work would otherwise pass.
    (`Repo.assert_stacked` is for one branch and its own commits.)"""
    for branch, parent in zip(chain, ["main", *chain], strict=False):
        # Fails the test if it isn't: `merge-base --is-ancestor` exits with 1.
        repo.git("merge-base", "--is-ancestor", parent, branch)
        assert repo.config(branch, "stackParent") == parent
