"""Commands running at once: the git-stack lock, and git config's."""

import os
import subprocess
import sys
import threading

import pytest

from git_stack import git
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchStatus, StackClient
from tests.helpers import Repo


def main_moves_on(repo: Repo) -> None:
    """Commit a new file to main, so the stack needs restacking (with --latest)."""
    repo.git("switch", "--quiet", "main")
    repo.write("d", "main\n")
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "main c2")
    repo.git("switch", "--quiet", "pr1")


def test_command_waits_for_another_holding_the_lock(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    main_moves_on(repo)
    with client.exclusive():
        restack = subprocess.Popen(
            [sys.executable, "-m", "git_stack", "-C", str(repo.path), "restack", "--latest"],
            cwd=repo.tmp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            # Unbuffered, so it prints that it's waiting as it starts to.
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        stdout = restack.stdout
        assert stdout is not None
        # Read in a thread, to give up if it never says it's waiting.
        first_line: list[str] = []
        reader = threading.Thread(target=lambda: first_line.append(stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=60)
        assert first_line == ["waiting for another git stack command in this repository to finish\n"]
        assert restack.poll() is None
    out, err = restack.communicate(timeout=60)

    assert restack.returncode == 0, err
    assert "restacking pr1 onto main" in out
    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])


def test_lock_is_shared_by_every_worktree(repo: Repo) -> None:
    repo.git("worktree", "add", "--quiet", "-b", "elsewhere", str(repo.tmp / "elsewhere"))

    assert GitClient(repo.tmp / "elsewhere").common_dir() == GitClient(repo.path).common_dir()


def test_config_write_waits_while_another_process_has_config_locked(repo: Repo, client: StackClient) -> None:
    client.branch("pr1")
    # Newer versions of git wait for the lock themselves; older ones, and this
    # one with this setting, fail at once.
    repo.git("config", "core.configLockTimeout", "0")
    config_lock = repo.path / ".git" / "config.lock"
    config_lock.touch()
    unlocked = threading.Event()

    def unlock() -> None:
        # Set first: a write that lands as soon as the lock goes still finds it set.
        unlocked.set()
        config_lock.unlink()

    threading.Timer(0.1, unlock).start()

    client.git.config_set("pr1", "stackBase", "x")

    # It waited for the lock, rather than failing while it was held.
    assert unlocked.is_set()
    assert repo.config("pr1", "stackBase") == "x"


def test_config_write_gives_up_once_config_stays_locked(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As a StackError, which the command line reports as its own. The runner retries while the config lock is held."""
    monkeypatch.setattr(git, "CONFIG_LOCK_TIMEOUT", 0)
    repo.git("config", "core.configLockTimeout", "0")
    (repo.path / ".git" / "config.lock").touch()

    with pytest.raises(StackError, match="could not lock config file"):
        client.branch("pr1")


def test_info_writes_nothing_to_git_config(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main\n", "main c1")
    config = (repo.path / ".git" / "config").read_text()
    (repo.path / ".git" / "config.lock").touch()

    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK

    assert (repo.path / ".git" / "config").read_text() == config
    assert repo.cached_check("pr1", "stackConflictCheck") is not None
