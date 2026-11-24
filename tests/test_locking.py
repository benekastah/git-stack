"""Commands running at once: the git-stack lock, and git config's."""

import json
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager

import pytest

from git_stack import core, events, git
from git_stack.errors import LockHeldByCaller, StackError
from git_stack.file_lock import file_lock
from git_stack.git import GitClient
from git_stack.stack import BranchStatus, StackClient
from tests.helpers import Repo

# The commands that only read, each run on pr2 of make_stack's stack (see
# settled_stacks).
READING_COMMANDS: dict[str, Callable[[StackClient], object]] = {
    "parent": lambda client: client.parent(),
    "base": lambda client: client.base(),
    "show-stack": lambda client: client.show_stack(),
    "next": lambda client: client.next(),
    "prev": lambda client: client.prev(),
    "info": lambda client: client.info(offline=True),
}


@pytest.fixture
def locks_taken(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path]:
    """The git-stack locks (see StackCore.exclusive) taken from now on."""
    taken: list[pathlib.Path] = []
    file_lock = core.file_lock

    @contextmanager
    def recording(path: pathlib.Path, on_wait: Callable[[], None]) -> Generator[None]:
        taken.append(path)
        with file_lock(path, on_wait):
            yield

    monkeypatch.setattr(core, "file_lock", recording)
    return taken


def restack_finished_by_hand(repo: Repo) -> str:
    """Make a restack of pr2 stop on a conflict, then finish it with git, as
    the user would, leaving it to be recorded, and return the commit pr2 is
    now on (its new base)."""
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("b", "pr1 conflicts with pr2\n", "pr1 c3")
    with pytest.raises(StackError, match="restacking pr2 stopped on a conflict"):
        repo.client.restack()
    repo.write("b", "resolved\n")
    repo.git("add", "b")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    assert repo.config_exists("pr2", core.PENDING_RESTACK_KEY)
    return repo.git("rev-parse", "pr1")


@pytest.mark.parametrize("command", READING_COMMANDS.values(), ids=READING_COMMANDS.keys())
def test_reading_command_with_nothing_to_settle_takes_no_lock_and_writes_nothing(
    repo: Repo, client: StackClient, locks_taken: list[pathlib.Path], command: Callable[[StackClient], object]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    config = (repo.path / ".git" / "config").read_bytes()
    locks_taken.clear()

    command(client)

    assert locks_taken == []
    assert (repo.path / ".git" / "config").read_bytes() == config


@pytest.mark.parametrize("command", READING_COMMANDS.values(), ids=READING_COMMANDS.keys())
def test_reading_command_settles_under_the_lock(
    repo: Repo, client: StackClient, locks_taken: list[pathlib.Path], command: Callable[[StackClient], object]
) -> None:
    new_base = restack_finished_by_hand(repo)
    locks_taken.clear()

    command(client)

    assert locks_taken == [client.git.common_dir() / core.LOCK_FILE]
    assert not repo.config_exists("pr2", core.PENDING_RESTACK_KEY)
    assert repo.config("pr2", core.BASE_KEY) == new_base


def test_base_gives_the_base_it_settles(repo: Repo, client: StackClient) -> None:
    new_base = restack_finished_by_hand(repo)

    assert client.base("pr2") == new_base


def test_processes_started_under_the_lock_are_told_it_is_held(repo: Repo, client: StackClient) -> None:
    """They inherit our environment, so git's hooks see it."""
    assert core.LOCK_HELD_ENV not in os.environ
    with client.exclusive():
        assert os.environ[core.LOCK_HELD_ENV] == str(os.getpid())
    assert core.LOCK_HELD_ENV not in os.environ


def test_reading_command_under_another_holding_the_lock_records_nothing(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rather than wait for a lock it may never get: e.g. run by a hook of a
    git command that a git-stack command holding the lock started."""
    restack_finished_by_hand(repo)
    old_base = repo.git("rev-parse", "pr1~1")
    pending = repo.config("pr2", core.PENDING_RESTACK_KEY)
    monkeypatch.setenv(core.LOCK_HELD_ENV, str(os.getpid() + 1))

    with lock_held_by_another_command(repo):
        assert client.base("pr2") == old_base

    assert not any(isinstance(event, events.WaitingForLock) for event in repo.emitted)
    assert repo.config("pr2", core.PENDING_RESTACK_KEY) == pending


def test_reading_command_told_the_lock_is_held_settles_if_its_free(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable may be stale, e.g. in a process its holder started that
    outlived the lock, or foreign, e.g. inherited from another thread of a
    host process using git-stack."""
    new_base = restack_finished_by_hand(repo)
    monkeypatch.setenv(core.LOCK_HELD_ENV, str(os.getpid() + 1))

    assert client.base("pr2") == new_base

    assert not repo.config_exists("pr2", core.PENDING_RESTACK_KEY)


def test_reading_command_started_by_one_holding_the_lock_doesnt_wait_for_it(repo: Repo, client: StackClient) -> None:
    restack_finished_by_hand(repo)
    old_base = repo.git("rev-parse", "pr1~1")

    with client.exclusive():
        # It would wait for this process, its parent here, forever.
        base = repo.stack_process("base")

    assert base.stdout == f"{old_base}\n"


def test_writing_command_started_by_one_holding_the_lock_fails_at_once(repo: Repo, client: StackClient) -> None:
    """E.g. `git stack track` in a post-checkout hook of `git stack branch`:
    waiting for this process, its parent here, would wait forever."""
    repo.git("branch", "side")
    repo.git("switch", "--quiet", "side")

    with client.exclusive():
        track = repo.stack_process("track", "main", expect_success=False)

    assert track.returncode == 1
    assert f"the git stack command that started this one (process {os.getpid()}) holds" in track.stderr
    assert "waiting" not in track.stderr
    assert not repo.config_exists("side", core.PARENT_KEY)


def test_writing_command_told_the_lock_is_held_waits_for_nothing(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Told so by its caller, a command that finds the lock taken gives up
    rather than wait, without saying it's waiting."""
    monkeypatch.setenv(core.LOCK_HELD_ENV, str(os.getpid() + 1))

    with lock_held_by_another_command(repo), pytest.raises(LockHeldByCaller, match=f"process {os.getpid() + 1}"):
        client.branch("pr1")

    assert not any(isinstance(event, events.WaitingForLock) for event in repo.emitted)
    assert not repo.config_exists("pr1", core.PARENT_KEY)


def test_writing_command_told_the_lock_is_held_takes_it_if_its_free(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable may be stale, e.g. in a process its holder started that
    outlived the lock."""
    monkeypatch.setenv(core.LOCK_HELD_ENV, str(os.getpid() + 1))

    client.branch("pr1")

    assert repo.config("pr1", core.PARENT_KEY) == "main"


def start_waiting_for_lock(
    repo: Repo, command: Callable[[StackClient], object]
) -> tuple[threading.Thread, list[object]]:
    """Start command in a thread, with a client of its own, once the caller
    holds the lock, and return the thread, once the command is waiting for the
    lock, and a list it puts what the command returns in."""
    waiting = threading.Event()

    def on_event(event: events.Event) -> None:
        if isinstance(event, events.WaitingForLock):
            waiting.set()

    reader = StackClient(GitClient(repo.path), on_event=on_event)
    result: list[object] = []
    thread = threading.Thread(target=lambda: result.append(command(reader)), daemon=True)
    thread.start()
    assert waiting.wait(timeout=60)
    return thread, result


def test_reading_command_waits_for_the_lock_to_settle(repo: Repo, client: StackClient) -> None:
    new_base = restack_finished_by_hand(repo)

    with client.exclusive():
        thread, result = start_waiting_for_lock(repo, lambda reader: reader.base("pr2"))
        # Nothing's recorded while another command has the lock.
        assert repo.config_exists("pr2", core.PENDING_RESTACK_KEY)
        assert thread.is_alive()
    thread.join(timeout=60)

    assert result == [new_base]
    assert not repo.config_exists("pr2", core.PENDING_RESTACK_KEY)


def test_reading_command_finds_under_the_lock_what_another_settled(repo: Repo, client: StackClient) -> None:
    new_base = restack_finished_by_hand(repo)

    with client.exclusive():
        thread, result = start_waiting_for_lock(repo, lambda reader: reader.base("pr2"))
        # The command holding the lock records it meanwhile.
        client.settle_all()
        config = (repo.path / ".git" / "config").read_bytes()
    thread.join(timeout=60)

    assert result == [new_base]
    assert (repo.path / ".git" / "config").read_bytes() == config


def test_reading_command_reads_refs_again_under_the_lock(repo: Repo, client: StackClient) -> None:
    """The refs it read before waiting may have moved meanwhile."""
    restack_finished_by_hand(repo)
    old_base = repo.git("rev-parse", "pr1~1")
    pending = repo.config("pr2", core.PENDING_RESTACK_KEY)

    with client.exclusive():
        thread, result = start_waiting_for_lock(repo, lambda reader: reader.base("pr2"))
        # Another command moves pr2 to its old base, so its reflog still says
        # the restack finished, but what it was restacked onto isn't in its
        # history any more.
        repo.git("update-ref", "refs/heads/pr2", old_base)
    thread.join(timeout=60)

    assert result == [old_base]
    assert repo.config("pr2", core.BASE_KEY) == old_base
    assert repo.config("pr2", core.PENDING_RESTACK_KEY) == pending


def test_reading_command_records_nothing_once_an_operation_stops(repo: Repo, client: StackClient) -> None:
    """Another command may have stopped on a conflict while it waited."""
    restack_finished_by_hand(repo)
    old_base = repo.git("rev-parse", "pr1~1")
    pending = repo.config("pr2", core.PENDING_RESTACK_KEY)
    repo.git("switch", "--quiet", "-c", "side", "pr2~1")
    repo.commit("b", "side\n", "side c1")
    repo.git("switch", "--quiet", "pr2")

    with client.exclusive():
        thread, result = start_waiting_for_lock(repo, lambda reader: reader.base("pr2"))
        assert not client.git.ok("cherry-pick", "refs/heads/side")
        assert client.git.operation_in_progress()
    thread.join(timeout=60)

    assert result == [old_base]
    assert repo.config("pr2", core.PENDING_RESTACK_KEY) == pending


def test_next_goes_on_from_the_branch_checked_out_once_it_has_the_lock(repo: Repo, client: StackClient) -> None:
    restack_finished_by_hand(repo)

    with client.exclusive():
        thread, result = start_waiting_for_lock(repo, lambda reader: reader.next())
        # The command holding the lock switches branches meanwhile.
        repo.git("switch", "--quiet", "pr1")
    thread.join(timeout=60)

    assert result == ["pr2"]


def main_moves_on(repo: Repo) -> None:
    """Commit a new file to main, so the stack needs restacking (with --latest)."""
    repo.git("switch", "--quiet", "main")
    repo.write("d", "main\n")
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "main c2")
    repo.git("switch", "--quiet", "pr1")


@contextmanager
def lock_held_by_another_command(repo: Repo) -> Generator[None]:
    """Hold git-stack's lock, as another git-stack process would."""
    with file_lock(repo.path / ".git" / core.LOCK_FILE, lambda: None):
        yield


def start_waiting_command(repo: Repo, *args: str) -> subprocess.Popen[str]:
    """Start `git stack <args>` as a process, while the caller holds the lock,
    and return it once it says on stderr that it's waiting for the lock."""
    command = subprocess.Popen(
        [sys.executable, "-m", "tests.stack_entry", "-C", str(repo.path), *args],
        cwd=repo.tmp,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # Unbuffered, so it prints that it's waiting as it starts to.
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    stderr = command.stderr
    assert stderr is not None
    # Read in a thread, to give up if it never says it's waiting.
    first_line: list[str] = []
    reader = threading.Thread(target=lambda: first_line.append(stderr.readline()), daemon=True)
    reader.start()
    reader.join(timeout=60)
    assert first_line == ["waiting for another git stack command in this repository to finish\n"]
    assert command.poll() is None
    return command


def test_command_waits_for_another_holding_the_lock(repo: Repo) -> None:
    repo.make_stack()
    main_moves_on(repo)
    with lock_held_by_another_command(repo):
        restack = start_waiting_command(repo, "restack", "--latest")
    out, err = restack.communicate(timeout=60)

    assert restack.returncode == 0, err
    assert "restacking pr1 onto main" in out
    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])


def test_parent_prints_only_the_parent_after_waiting_for_the_lock(repo: Repo) -> None:
    """Saying it's waiting goes to stderr, so `$(git stack parent)` still
    gets the branch alone."""
    restack_finished_by_hand(repo)
    with lock_held_by_another_command(repo):
        parent = start_waiting_command(repo, "parent")
    out, err = parent.communicate(timeout=60)

    assert parent.returncode == 0, err
    assert out == "pr1\n"


def test_info_json_prints_only_its_list_after_waiting_for_the_lock(repo: Repo) -> None:
    restack_finished_by_hand(repo)
    with lock_held_by_another_command(repo):
        info = start_waiting_command(repo, "info", "--json")
    out, err = info.communicate(timeout=60)

    assert info.returncode == 0, err
    assert [branch["name"] for branch in json.loads(out)] == ["main", "pr1", "pr2", "pr3"]


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
