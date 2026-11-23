"""Git executable selection and patch output handling."""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from git_client import CLIENT_TIMEOUT, Arg, EnvValue, Git, Timeout


class PatchGit(Git):
    """Preserve whitespace in the text passed to the shared patch-ID reader."""

    def run(
        self,
        *args: Arg,
        input: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        timeout: Timeout = CLIENT_TIMEOUT,
        strip: bool = False,
    ) -> str:
        return super().run(*args, input=input, env=env, timeout=timeout, strip=strip)


def find_git(variable: str) -> str:
    if program := os.environ.get(variable):
        if sys.platform == "win32" and Path(program).suffix.lower() in {".bat", ".cmd"}:
            raise ValueError(f"${variable} must name an executable, not a batch file")
        return program
    executable = "git.exe" if sys.platform == "win32" else "git"
    for directory in os.get_exec_path():
        # A repository can contain its own executable. Ignore relative PATH entries.
        candidate = Path(directory) / executable
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(f"git isn't in any directory on PATH; ${variable} can name it")
