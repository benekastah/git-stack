"""The CLI's wall-clock timeline of Git and GitHub calls."""

import os
import pathlib
import shlex
import stat
from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, BinaryIO, Literal

from git_client import PerformanceRecorder

from git_stack.errors import StackError
from git_stack.file_lock import file_lock

if TYPE_CHECKING:
    from git_stack.git import GitClient

Tool = Literal["git", "gh", "native", "cat-file"]
MAX_PERFORMANCE_LOG_BYTES = 1_000_000
LOG_PATH_SETTING = "stack.perfLog"
LOG_LIMIT_SETTING = "stack.perfLogMaxBytes"
REPORT_HEADING = "Performance timeline"
VERSION_PREFIX = "  Version    "
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
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        # Exclusive creation also rejects dangling symlinks on platforms
        # without O_NOFOLLOW, where O_CREAT alone would create their target.
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "r+b") as log:
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode) or not os.path.samestat(entry, os.fstat(log.fileno())):
            raise OSError(f"{path} must be a regular file, not a symlink")
        yield log


def _read_report(log: BinaryIO) -> bool:
    """Consume a complete report, including truncation formats from older builds."""
    command = log.readline(MAX_PERFORMANCE_LOG_BYTES).rstrip(b"\r\n")
    if command == TRUNCATION_NOTICE.rstrip(b"\n"):
        return log.readline(2) == b"\n"

    heading = log.readline(len(TRUNCATION_NOTICE)).rstrip(b"\r\n")
    if heading == TRUNCATION_NOTICE.rstrip(b"\n"):
        # Older builds could truncate within the command itself.
        return (b"git stack".startswith(command) or command.startswith(b"git stack ")) and log.readline(2) == b"\n"
    if heading != REPORT_HEADING.encode() or not (command == b"git stack" or command.startswith(b"git stack ")):
        return False

    for line in iter(log.readline, b""):
        if line in (b"\n", b"\r\n"):
            # Older reports could have a blank line before the truncation
            # marker if their byte limit fell right on a line boundary.
            boundary = log.tell()
            if log.read(len(TRUNCATION_NOTICE)) != TRUNCATION_NOTICE:
                log.seek(boundary)
            return True
    return False


def _retained_report_start(log: BinaryIO, end: int, remaining: int) -> int | None:
    """Validate the file and find the oldest complete report that will fit."""
    log.seek(0)
    start, retain = 0, end
    while start < end:
        if not _read_report(log):
            return None
        if start >= end - remaining and retain == end:
            retain = start
        start = log.tell()
    return retain


def append_performance_log(
    path: pathlib.Path, argv: list[str], report: str, *, max_bytes: int = MAX_PERFORMANCE_LOG_BYTES
) -> None:
    """Keep the newest timelines with their commands, within the byte limit."""
    path = path.expanduser()
    if not path.name:
        raise IsADirectoryError(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = _command_line(("git", "stack", *argv))
    report = report.rstrip("\n")
    entry = f"{command}\n{report}\n\n".encode("utf-8", errors="backslashreplace")
    if len(entry) > max_bytes:
        if max_bytes < len(TRUNCATION_NOTICE):
            raise OSError("performance log size limit is too small for a truncation notice")
        heading = f"{REPORT_HEADING}\n"
        version_line = report.removeprefix(heading).partition("\n")[0]
        if version_line.startswith(VERSION_PREFIX):
            heading += f"{version_line}\n"
        header = f"{command}\n{heading}".encode("utf-8", errors="backslashreplace")
        limit = max_bytes - len(TRUNCATION_NOTICE) - 1
        if limit < len(header):
            entry = TRUNCATION_NOTICE
        else:
            prefix = entry[:limit].decode("utf-8", errors="ignore").encode("utf-8")
            entry = prefix.rstrip(b"\n") + b"\n" + TRUNCATION_NOTICE
    # Commands in different repositories can finish at the same time.
    with (
        file_lock(path.with_name(f".{path.name}.git-stack-perf-lock"), lambda: None),
        _open_log(path) as log,
    ):
        end = log.seek(0, os.SEEK_END)
        remaining = 0 if entry == TRUNCATION_NOTICE else max_bytes - len(entry)
        start = _retained_report_start(log, end, remaining) if end else 0
        if start is None:
            raise OSError(f"{path} already exists and is not a performance log")
        if start:
            log.seek(start)
            recent = log.read()
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
        self.git = PerformanceRecorder()

    @contextmanager
    def activate(self) -> Generator[None]:
        token = active_timeline.set(self)
        try:
            yield
        finally:
            active_timeline.reset(token)

    @contextmanager
    def record(self, tool: Literal["gh", "native"], *args: str) -> Generator[None]:
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

    def render(self, *, version: str | None = None) -> str:
        """Format the timeline after the command and its workers have finished."""
        total = self._clock() - self._start
        calls = sorted((*self._calls, *self._git_calls(total)), key=lambda call: call.start)
        lines = [REPORT_HEADING]
        if version is not None:
            lines.append(f"{VERSION_PREFIX}{version}")
        for call in calls:
            command = _command_line((call.tool, *call.args))
            lines.append(f"  +{call.start:.3f}s  {call.end - call.start:6.3f}s  {command}")

        git = _occupied_time([call for call in calls if call.tool in {"git", "cat-file"}])
        gh = _occupied_time([call for call in calls if call.tool == "gh"])
        busy = _occupied_time([call for call in calls if call.tool != "native"])

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

    def _git_calls(self, total: float) -> Generator[_Call]:
        # With a Git client, typed read events already include the subprocess intervals.
        for event in self.git.events:
            start = event.start - self._start
            end = total if event.end is None else event.end - self._start
            if event.kind == "process" and event.pid is not None:
                # CatFile's launch counts once. Its separate work intervals exclude idle time.
                yield _Call("git", event.args, start, start if event.persistent else end)
            elif event.kind == "process_work":
                yield _Call("cat-file", (event.name.removeprefix("CatFile."),), start, end)


active_timeline: ContextVar[Timeline | None] = ContextVar("active_timeline", default=None)


def measure_gh(args: tuple[str, ...]) -> AbstractContextManager[None]:
    timeline = active_timeline.get()
    return timeline.record("gh", *args) if timeline is not None else nullcontext()


def measure_native(name: str) -> AbstractContextManager[None]:
    timeline = active_timeline.get()
    return timeline.record("native", name) if timeline is not None else nullcontext()
