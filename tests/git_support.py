"""The Git executable and command records used by the tests."""

import os
import subprocess
import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path

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
    """Arguments recorded by the git_calls fixture, plus their executable."""

    def __init__(self, args: Sequence[str], program: str) -> None:
        super().__init__(args)
        self.program = program
