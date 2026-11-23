"""Run the Git commands whose behavior git-stack shares with the Git CLI."""

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from time import monotonic, sleep
from typing import TextIO

from git_stack.performance import measure_git


def decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


class GitError(Exception):
    def __init__(self, args: tuple[str, ...], result: subprocess.CompletedProcess[bytes]) -> None:
        self.stdout = decode(result.stdout or b"")
        self.stderr = decode(result.stderr or b"")
        super().__init__(f"git {' '.join(args)} failed:\n{self.stderr.strip()}")


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


class GitCommand:
    def __init__(self, directory: Path, *, env_var: str) -> None:
        self.directory = str(directory)
        self.program = find_git(env_var)

    def process(
        self,
        *args: str,
        capture_output: bool = False,
        input: bytes | None = None,
        stdout: int | TextIO | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        if capture_output:
            # Hooks and helpers still inherit the user's locale.
            environment["GIT_TEXTDOMAINDIR"] = os.devnull
        environment.update(env or {})
        with measure_git(args):
            return subprocess.run(
                [self.program, "-C", self.directory, *args],
                input=input,
                stdout=subprocess.PIPE if capture_output and stdout is None else stdout,
                stderr=subprocess.PIPE if capture_output else None,
                env=environment,
                check=False,
            )

    def run_bytes(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        result = self.process(*args, capture_output=True, input=input, env=env)
        if result.returncode:
            raise GitError(args, result)
        return result.stdout

    def run(
        self, *args: str, input: str | None = None, env: Mapping[str, str] | None = None, strip: bool = True
    ) -> str:
        output = decode(
            self.run_bytes(*args, input=None if input is None else input.encode("utf-8", "surrogateescape"), env=env)
        )
        return output.strip() if strip else output

    def succeeds(self, *args: str) -> bool:
        return self.process(*args, capture_output=True, stdout=subprocess.DEVNULL).returncode == 0

    def ok(self, *args: str, env: Mapping[str, str] | None = None) -> bool:
        result = self.process(*args, capture_output=True, stdout=subprocess.DEVNULL, env=env)
        if result.returncode not in (0, 1):
            raise GitError(args, result)
        return result.returncode == 0

    def passthrough(self, *args: str, stdout: TextIO | None = None) -> bool:
        return self.process(*args, stdout=stdout).returncode == 0

    def write_config(self, *args: str, wait: float) -> None:
        deadline = monotonic() + wait
        while True:
            try:
                self.run("config", *args)
            except GitError as error:
                remaining = deadline - monotonic()
                if remaining <= 0 or "could not lock config file" not in error.stderr:
                    raise
                sleep(min(remaining, 0.05))
            else:
                return
