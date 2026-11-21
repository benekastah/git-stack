"""Declaring, and checking, how many git processes and gh commands a test runs.

    @expect_operations(git=3)                   # what the whole test body runs
    def test_info(repo): ...

    with expect_operations(git=3, gh=1):        # or one block of it, e.g. per size in a loop
        client.info()

It counts the git processes git-stack starts (each launch of the batch `git cat-file`
once, as the `git_calls` fixture does; not the tests' own `Repo.git`), and the gh commands
it runs (against tests/fake_gh.py), in this process, or in a git-stack process the test
starts with `Repo.stack_process`: that one is counted from outside, with git's own trace of
the processes it starts (TRACE_VARIABLE, keeping those git-stack starts, not the ones git
starts for them, e.g. under `git rebase`) and the calls fake gh keeps, so it's counted
as a user's would run, with nothing cached in Python between runs.

The numbers are what the block is allotted: running more fails, listing what ran. Running
fewer is an improvement, and warns (UnderBudget) to lower the number to what ran, so it's kept
and the next one is looked for.

Work a test needs before the block (its repositories and stacks) belongs in fixtures or
before the `with`, since what the block runs counts.
"""

import contextlib
import json
import os
import pathlib
import shlex
import tempfile
import threading
import warnings
from collections.abc import Generator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import pytest
from git_client import Arg, ReadOnlyGit

from tests import fake_gh

TRACE_VARIABLE = "GIT_TRACE2_EVENT"
"""Names the file git writes a JSON event to for each process it starts, and each one they start."""
TRACE_PARENT_VARIABLE = "GIT_TRACE2_PARENT_SID"
"""Set in a process git starts, to its own session ID. The tests' git can inherit one (e.g. if they
were run by a git with tracing on), which would give every process under it a parent."""

# git-stack runs git from several threads (e.g. the workers that check each branch's status),
# and blocks can nest, so the blocks that are running are kept under a lock.
_LOCK = threading.Lock()
_ACTIVE: list[Operations] = []
"""The Operations being filled: one for each block that's running now."""

_SHOWN = 160
"""How many characters of a command a failure shows, so one long `git log` doesn't bury the rest."""


class UnderBudget(UserWarning):
    """A block ran fewer git processes or gh commands than it was allotted: its number can be
    lowered, to keep the improvement."""


# eq=False: blocks are told apart by identity, not by what they've recorded so far, or an inner one
# that ends first, with nothing yet (so equal to the one around it), would end the outer one's record.
@dataclass(eq=False)
class Operations:
    """The git processes and gh commands that ran. A git process is its whole command, from
    `-C <directory>` (how git-stack names the repository, which `subcommand` leaves out) on, so a
    test can check which command was run."""

    git: list[list[str]] = field(default_factory=list)
    gh: list[list[str]] = field(default_factory=list)


def subcommand(command: list[str]) -> list[str]:
    """A git command without the `-C <directory>` that names the repository it ran in."""
    return command[2:] if command[:1] == ["-C"] else command


def _running() -> list[Operations]:
    """The blocks that are running now, to record what just ran in each. A copy, so a block
    that finishes meanwhile in another thread doesn't change what's being looped over; appending
    to a block's own list is safe from any thread."""
    with _LOCK:
        return list(_ACTIVE)


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record each git process and gh command the test runs, for the blocks that are active
    (see expect_operations). The autouse `count_operations` fixture does this for every test.

    It wraps the places every command goes through, the ones the `git_calls` fixture wraps
    too, rather than use that fixture: a decorator can't ask for a fixture without changing
    the test's signature, and a `with` block inside a test has no way to reach one."""
    # Every git process git-stack starts is one of these two (`Git` inherits them from
    # ReadOnlyGit): `process` runs it to the end, `popen` leaves it running, as for the batch
    # `cat-file`, which counts once however many requests it answers.
    process, popen = ReadOnlyGit.process, ReadOnlyGit.popen
    # With the `gh` fixture, the GitHub adapter's `subprocess` is replaced by InProcessGh, which
    # answers each gh command by calling this.
    gh_run = fake_gh.run

    def recording_process(self: ReadOnlyGit, *args: Arg, **kwargs: Any) -> Any:
        command = ["-C", self.directory, *(os.fsdecode(arg) for arg in args)]
        for ran in _running():
            ran.git.append(command)
        return process(self, *args, **kwargs)

    def recording_popen(self: ReadOnlyGit, *args: Arg, **kwargs: Any) -> Any:
        command = ["-C", self.directory, *(os.fsdecode(arg) for arg in args)]
        for ran in _running():
            ran.git.append(command)
        return popen(self, *args, **kwargs)

    def recording_gh(args: list[str], *rest: Any, **kwargs: Any) -> Any:
        for ran in _running():
            ran.gh.append(list(args))
        return gh_run(args, *rest, **kwargs)

    monkeypatch.setattr(ReadOnlyGit, "process", recording_process)
    monkeypatch.setattr(ReadOnlyGit, "popen", recording_popen)
    monkeypatch.setattr(fake_gh, "run", recording_gh)


def _fake_gh_calls() -> list[list[str]]:
    """The calls fake gh has kept so far, if the test has one. They're in the file the `gh`
    fixture names in $FAKE_GH_STATE, which a git-stack process's own fake gh appends to."""
    state = os.environ.get("FAKE_GH_STATE")
    if state is None or not pathlib.Path(state).exists():
        return []
    calls: list[list[str]] = json.loads(pathlib.Path(state).read_text(encoding="utf-8"))["calls"]
    return calls


def _traced_git_processes(trace: pathlib.Path) -> list[list[str]]:
    """The whole command (see Operations) of each git process that the process traced in trace
    started itself.

    Git writes one JSON event per line, and a process's `start` event has its `argv` and a
    session ID, `sid`. A process git-stack starts has a plain one (`20261120T0415Z-H1ab-P1cd`);
    one that git starts for it, e.g. the `git notes copy` a `git rebase` runs, has its parent's
    first, after a `/`. So those with no `/` are git-stack's own."""
    commands: list[list[str]] = []
    for line in trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []:
        event = json.loads(line)
        if event["event"] == "start" and "/" not in event["sid"]:
            commands.append(event["argv"][1:])
    return commands


@contextlib.contextmanager
def traced_subprocess() -> Generator[dict[str, str] | None]:
    """Around a git-stack process a test starts: gives the environment to start it with, and
    afterwards counts the git processes it started and the gh commands it ran in the blocks
    that are active. Nothing is traced, and None given (the process is started as it would be),
    without one.

    The environment is for that one process, not `os.environ`: git processes started in this
    one would inherit it, write to the trace too, and be counted twice, here and by `install`.
    It has no trace session to inherit (see TRACE_PARENT_VARIABLE)."""
    if not _running():
        yield None
        return
    gh_before = len(_fake_gh_calls())
    # A file of its own for each process, so what one wrote isn't counted for the next.
    with tempfile.TemporaryDirectory() as directory:
        trace = pathlib.Path(directory) / "trace2.json"
        environment = {k: v for k, v in os.environ.items() if k != TRACE_PARENT_VARIABLE}
        yield {**environment, TRACE_VARIABLE: str(trace)}
        for command in _traced_git_processes(trace):
            for ran in _running():
                ran.git.append(command)
        for command in _fake_gh_calls()[gh_before:]:
            for ran in _running():
                ran.gh.append(command)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _listing(program: str, commands: list[list[str]]) -> str:
    # Without the directory, which is the same for all of them and a long path in each line.
    lines = [f"  {program} {shlex.join(subcommand(command))}" for command in commands]
    return "\n".join(line if len(line) <= _SHOWN else line[: _SHOWN - 1] + "…" for line in lines)


class expect_operations(contextlib.ContextDecorator):
    """Fail if more than `git` git processes or `gh` gh commands run, in a `with` block (which
    gives the Operations that ran) or in a decorated function (each call), and warn if fewer
    (see UnderBudget). Failing inside doesn't hide the error that did."""

    def __init__(self, *, git: int, gh: int = 0) -> None:
        self.git = git
        self.gh = gh
        self.operations = Operations()

    def _recreate_cm(self) -> Self:
        """A fresh one for each call of a decorated function. ContextDecorator would reuse this
        one, with the Operations the last call filled, so a parameterized test would count the
        runs before it."""
        return type(self)(git=self.git, gh=self.gh)

    def __enter__(self) -> Operations:
        with _LOCK:
            _ACTIVE.append(self.operations)
        return self.operations

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        with _LOCK:
            _ACTIVE.remove(self.operations)
        # An error in the block is the failure to show; the counts are likely off because of it.
        if exc_type is not None:
            return
        problems: list[str] = []
        under: list[str] = []
        for program, allotted, ran, singular, plural in (
            ("git", self.git, self.operations.git, "process", "processes"),
            ("gh", self.gh, self.operations.gh, "command", "commands"),
        ):
            count = len(ran)
            if count > allotted:
                problems.append(
                    f"ran {count} {program} {_plural(count, singular, plural)}, {allotted} allotted:\n"
                    + _listing(program, ran)
                )
            elif count < allotted:
                under.append(
                    f"ran {count} of the {allotted} {program} {_plural(allotted, singular, plural)} allotted: "
                    f"lower the number to {count}"
                )
        # A failure is the news; a warning could be hidden by it.
        if problems:
            raise AssertionError("\n".join(problems))
        for message in under:
            warnings.warn(message, UnderBudget, stacklevel=2)
