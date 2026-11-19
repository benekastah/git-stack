"""The CLI's wall-clock timeline of Git and GitHub calls."""

import os
import pathlib
import shlex
import stat
import subprocess
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import IO, TYPE_CHECKING, BinaryIO, Literal, override

from git_client import CLIENT_TIMEOUT, Arg, CatFile, EnvValue, Git, Timeout

from git_stack.errors import StackError
from git_stack.file_lock import file_lock

if TYPE_CHECKING:
    from git_stack.git import GitClient

Tool = Literal["git", "gh"]
MAX_PERFORMANCE_LOG_BYTES = 1_000_000
LOG_PATH_SETTING = "stack.perfLog"
LOG_LIMIT_SETTING = "stack.perfLogMaxBytes"
REPORT_HEADING = "Performance timeline"
TRUNCATION_NOTICE = b"[performance report truncated]\n\n"


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


def _command_line(args: Iterable[str]) -> str:
    return shlex.join(args).replace("\r", r"\r").replace("\n", r"\n")


def performance_log_settings(git: GitClient) -> tuple[pathlib.Path, int] | None:
    """Read the effective Git settings. None means reporting is disabled."""
    try:
        location = git.config_value(LOG_PATH_SETTING)
        has_limit = bool(location) and git.config_value(LOG_LIMIT_SETTING) is not None
    except StackError:
        # Commands such as install-skills can run outside a repository.
        location = git.run("config", "--get", LOG_PATH_SETTING, check=False)
        has_limit = True
    if not location:
        return None
    path = pathlib.Path(location) if location.startswith("~") else git.directory.absolute() / location
    return path, _configured_log_limit(git) if has_limit else MAX_PERFORMANCE_LOG_BYTES


def _configured_log_limit(git: GitClient) -> int:
    limit = int(git.run("config", "--type=int", "--get", LOG_LIMIT_SETTING, check=False) or "0")
    return limit if limit > 0 else MAX_PERFORMANCE_LOG_BYTES


@contextmanager
def _open_log(path: pathlib.Path) -> Generator[BinaryIO]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "r+b") as log:
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode) or not os.path.samestat(entry, os.fstat(log.fileno())):
            raise OSError(f"{path} must be a regular file, not a symlink")
        yield log


def _is_performance_log(log: BinaryIO) -> bool:
    log.seek(0)
    command = log.readline(MAX_PERFORMANCE_LOG_BYTES).rstrip(b"\r\n")
    if command == TRUNCATION_NOTICE.rstrip():
        return True
    heading = log.readline(len(REPORT_HEADING) + 2).rstrip(b"\r\n")
    return (command == b"git stack" or command.startswith(b"git stack ")) and heading == REPORT_HEADING.encode()


def append_performance_log(
    path: pathlib.Path, argv: list[str], report: str, *, max_bytes: int = MAX_PERFORMANCE_LOG_BYTES
) -> None:
    """Keep the newest timelines with their commands, within the byte limit."""
    path = path.expanduser()
    if not path.name:
        raise IsADirectoryError(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = _command_line(("git", "stack", *argv))
    entry = f"{command}\n{report}\n\n".encode("utf-8", errors="backslashreplace")
    if len(entry) > max_bytes:
        if max_bytes < len(TRUNCATION_NOTICE):
            raise OSError("performance log size limit is too small for a truncation notice")
        header = f"{command}\n{REPORT_HEADING}\n".encode("utf-8", errors="backslashreplace")
        limit = max_bytes - len(TRUNCATION_NOTICE) - 1
        if limit < len(header):
            entry = TRUNCATION_NOTICE
        else:
            prefix = entry[:limit].decode("utf-8", errors="ignore").encode("utf-8")
            entry = prefix + b"\n" + TRUNCATION_NOTICE
    # Commands in different repositories can finish at the same time.
    with (
        file_lock(path.with_name(f"{path.name}.lock"), lambda: None),
        _open_log(path) as log,
    ):
        end = log.seek(0, os.SEEK_END)
        if end and not _is_performance_log(log):
            raise OSError(f"{path} already exists and is not a performance log")
        remaining = max_bytes - len(entry)
        if end > remaining:
            # Start just before the retention window to detect a report that
            # begins exactly at its boundary.
            start = end - remaining
            log.seek(max(0, start - 2))
            _, _, recent = log.read().partition(b"\n\n")
            log.seek(0)
            log.write(recent)
            log.truncate()
        else:
            log.seek(0, os.SEEK_END)
        log.write(entry)


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
        lines = [REPORT_HEADING]
        for call in self._calls:
            command = _command_line((call.tool, *call.args))
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
