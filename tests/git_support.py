"""The Git executable and command records used by the tests."""

import os
import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import pygit2
from pygit2.enums import ConfigLevel

from git_stack.git_command import find_git

# Keep the POSIX spawn fast path and avoid inheriting handles on Windows.
CLOSE_FDS = sys.platform == "win32"


@cache
def real_git() -> str:
    """Prefer the executable in Git's exec directory over a PATH wrapper."""
    program = find_git("")
    output = subprocess.check_output([program, "--exec-path"])
    candidate = Path(os.fsdecode(output.rstrip(b"\r\n"))) / Path(program).name
    return str(candidate) if candidate.is_file() else program


class GitCall(list[str]):
    """Arguments, executable and client close_fds setting recorded by git_calls."""

    def __init__(self, args: Sequence[str], program: str, *, close_fds: bool) -> None:
        super().__init__(args)
        self.program = program
        self.close_fds = close_fds


@contextmanager
def isolate_libgit2_config(directory: Path) -> Generator[None]:
    """Read config only from the test's directory and repository."""
    search_paths = pygit2.settings.search_path
    levels = (ConfigLevel.PROGRAMDATA, ConfigLevel.SYSTEM, ConfigLevel.XDG, ConfigLevel.GLOBAL)
    originals = {level: search_paths[level] for level in levels}
    try:
        for level in levels:
            search_paths[level] = str(directory)
        yield
    finally:
        for level, path in originals.items():
            search_paths[level] = path
