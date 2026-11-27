"""Git executable selection, the variables git-stack adds to what it starts, and patch output
handling."""

import os
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from git_client import CLIENT_TIMEOUT, Arg, EnvValue, Git, GitCache, PerformanceRecorder, Timeout


class ProcessEnv:
    """Variables for the processes a command starts (git, and so its hooks, and gh) on top of
    os.environ, which stays as it is: changing it would race with C code reading it in other
    threads of a host process using git-stack, and reach what they start too. A command's
    clients share one (see GitClient.at), so its threads and its clients for other worktrees
    pass the variables on too, and another command's clients don't."""

    def __init__(self) -> None:
        # Replaced whole, never changed, so a thread starting a process meanwhile reads the
        # old variables or the new ones.
        self.variables: Mapping[str, str] = {}

    @contextmanager
    def adding(self, variables: Mapping[str, str]) -> Generator[None]:
        """Add variables to what processes started meanwhile get."""
        before = self.variables
        self.variables = {**before, **variables}
        try:
            yield
        finally:
            self.variables = before

    def environ(self) -> dict[str, str] | None:
        """A whole environment for a process that isn't git's (e.g. gh): os.environ with the
        variables, or None, for os.environ as it is, if there are none."""
        return {**os.environ, **self.variables} if self.variables else None


class StackGit(Git):
    """A Git whose processes get process_env's variables too."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        program: str,
        process_env: ProcessEnv,
        performance: PerformanceRecorder | None = None,
        cache: GitCache | None = None,
        object_id_length: int | None = None,
    ) -> None:
        super().__init__(
            directory, program=program, performance=performance, cache=cache, object_id_length=object_id_length
        )
        self.process_env = process_env

    def environment(self, env: Mapping[str, EnvValue] | None = None, *, english: bool = False) -> dict[str, str]:
        if variables := self.process_env.variables:
            env = {**variables, **(env or {})}
        return super().environment(env, english=english)


class PatchGit(StackGit):
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
