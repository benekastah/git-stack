"""Declaring, and checking, how many git processes and gh commands a test runs.

    @expect_operations(git=3)                   # what the whole test body runs
    def test_info(repo): ...

    with expect_operations(git=3, gh=1):        # or one block of it, e.g. per size in a loop
        client.info()

It counts the git processes git-stack starts (each launch of the batch `git cat-file`
once, as the `git_calls` fixture does; not the tests' own `Repo.git`), and the gh commands
it runs (against tests/fake_gh.py). The count has to be exact: a test that runs fewer
should have its number lowered, so an improvement is kept. A mismatch lists what ran.

Work a test needs before the block (its repositories and stacks) belongs in fixtures or
before the `with`, since what the block runs counts.
"""

import contextlib
import os
import shlex
import threading
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import pytest
from git_client import Arg, ReadOnlyGit

from tests import fake_gh

_LOCK = threading.Lock()
_ACTIVE: list[Operations] = []
"""The Operations being filled: one for each block that's running now."""

_SHOWN = 160
"""How many characters of a command a failure shows."""


@dataclass
class Operations:
    """The git processes (each's arguments) and gh commands that ran."""

    git: list[list[str]] = field(default_factory=list)
    gh: list[list[str]] = field(default_factory=list)


def _record(kind: str, args: list[str]) -> None:
    with _LOCK:
        for operations in _ACTIVE:
            getattr(operations, kind).append(args)


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record each git process and gh command the test runs, for the blocks that are active
    (see expect_operations). The autouse `count_operations` fixture does this for every test."""
    process, popen = ReadOnlyGit.process, ReadOnlyGit.popen
    gh_run = fake_gh.run

    def recording_process(self: ReadOnlyGit, *args: Arg, **kwargs: Any) -> Any:
        _record("git", [os.fsdecode(arg) for arg in args])
        return process(self, *args, **kwargs)

    def recording_popen(self: ReadOnlyGit, *args: Arg, **kwargs: Any) -> Any:
        _record("git", [os.fsdecode(arg) for arg in args])
        return popen(self, *args, **kwargs)

    def recording_gh(args: list[str], *rest: Any, **kwargs: Any) -> Any:
        _record("gh", list(args))
        return gh_run(args, *rest, **kwargs)

    monkeypatch.setattr(ReadOnlyGit, "process", recording_process)
    monkeypatch.setattr(ReadOnlyGit, "popen", recording_popen)
    monkeypatch.setattr(fake_gh, "run", recording_gh)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _listing(program: str, commands: list[list[str]]) -> str:
    lines = [f"  {program} {shlex.join(command)}" for command in commands]
    return "\n".join(line if len(line) <= _SHOWN else line[: _SHOWN - 1] + "…" for line in lines)


class expect_operations(contextlib.ContextDecorator):
    """Fail unless exactly `git` git processes and `gh` gh commands run, in a `with` block
    (which gives the Operations that ran) or in a decorated function (each call). Failing
    inside doesn't hide the error that did."""

    def __init__(self, *, git: int, gh: int = 0) -> None:
        self.git = git
        self.gh = gh
        self.operations = Operations()

    def _recreate_cm(self) -> Self:
        """A fresh one for each call of a decorated function."""
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
        if exc_type is not None:
            return
        problems: list[str] = []
        for program, expected, ran, singular, plural in (
            ("git", self.git, self.operations.git, "process", "processes"),
            ("gh", self.gh, self.operations.gh, "command", "commands"),
        ):
            if len(ran) != expected:
                problems.append(
                    f"expected {expected} {program} {_plural(expected, singular, plural)}, ran {len(ran)}:\n"
                    + (_listing(program, ran) or "  (none)")
                )
        if problems:
            raise AssertionError("\n".join(problems))
