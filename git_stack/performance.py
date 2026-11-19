"""The CLI's opt-in wall-clock timeline of Git and GitHub calls."""

import os
import shlex
import subprocess
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import IO, Literal, override

from git_client import CLIENT_TIMEOUT, Arg, CatFile, EnvValue, Git, Timeout

Tool = Literal["git", "gh"]


@dataclass
class _Call:
    tool: Tool
    args: tuple[str, ...]
    start: float
    end: float


def _occupied_time(calls: list[_Call]) -> float:
    """Length of the union of the calls' intervals, in start order."""
    elapsed = end = 0.0
    for call in calls:
        elapsed += max(0.0, call.end - max(end, call.start))
        end = max(end, call.end)
    return elapsed


class Timeline:
    """Record calls from all workers in one command without holding a lock while they run."""

    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._lock = Lock()
        self._calls: list[_Call] = []

    @contextmanager
    def activate(self) -> Generator[None]:
        token = active_timeline.set(self)
        try:
            yield
        finally:
            active_timeline.reset(token)

    @contextmanager
    def record(self, tool: Tool, *args: str) -> Generator[None]:
        if tool == "gh" and args[:2] == ("api", "graphql"):
            args = tuple("query=<omitted>" if arg.startswith("query=") else arg for arg in args)
        with self._lock:
            start = self._clock() - self._start
            call = _Call(tool, args, start, start)
            self._calls.append(call)
        try:
            yield
        finally:
            with self._lock:
                call.end = self._clock() - self._start

    def render(self) -> str:
        """Format the timeline after the command and its workers have finished."""
        total = self._clock() - self._start
        lines = ["Performance timeline"]
        for call in self._calls:
            command = shlex.join((call.tool, *call.args)).replace("\r", r"\r").replace("\n", r"\n")
            lines.append(f"  +{call.start:.3f}s  {call.end - call.start:6.3f}s  {command}")

        git = _occupied_time([call for call in self._calls if call.tool == "git"])
        gh = _occupied_time([call for call in self._calls if call.tool == "gh"])
        busy = _occupied_time(self._calls)

        def percent(seconds: float) -> str:
            return f"{(seconds / total if total else 0):.1%}"

        lines.append(f"  {'Total':<10} {total:7.3f}s")
        for name, seconds in (("Git", git), ("gh", gh), ("Other", max(0.0, total - busy))):
            lines.append(f"  {name:<10} {seconds:7.3f}s ({percent(seconds)})")
        overlap = git + gh - busy
        # Ignore roundoff when the tool intervals are disjoint.
        if overlap > 1e-9:
            lines.append(f"  {'Git and gh':<10} {overlap:7.3f}s ({percent(overlap)} overlap)")
        return "\n".join(lines)


active_timeline: ContextVar[Timeline | None] = ContextVar("active_timeline", default=None)


def measure_gh(args: tuple[str, ...]) -> AbstractContextManager[None]:
    timeline = active_timeline.get()
    return timeline.record("gh", *args) if timeline is not None else nullcontext()


class TimedGit(Git):
    """Capture every ordinary py-git-client process, including its batched helpers."""

    @override
    def process(
        self,
        *args: Arg,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | IO[bytes] | IO[str] | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
    ) -> subprocess.CompletedProcess[bytes]:
        timeline = active_timeline.get()
        with timeline.record("git", *map(os.fsdecode, args)) if timeline is not None else nullcontext():
            return super().process(
                *args, capture_output=capture_output, input=input, stdout=stdout, env=env, timeout=timeout
            )


class TimedCatFile(CatFile):
    """Measure each batch exchange and process shutdown with the batch lock already held."""

    # These hooks in the pinned py-git-client cover the full exchange for the
    # info/verify requests GitClient uses, including lazy startup and retries.
    @override
    def _request(self, command: bytes, name: str) -> tuple[bytes, str]:
        timeline = active_timeline.get()
        if timeline is None:
            return super()._request(command, name)
        with timeline.record("git", "cat-file", "--batch-command", "request"):
            return super()._request(command, name)

    @override
    def _stop(self) -> None:
        timeline = active_timeline.get()
        if timeline is None or self._process is None:
            super()._stop()
        else:
            with timeline.record("git", "cat-file", "--batch-command", "shutdown"):
                super()._stop()
