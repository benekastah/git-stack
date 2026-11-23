"""Local Git reads and CLI fallbacks for repository-specific semantics."""

import os
from pathlib import Path

import pygit2

from git_stack.performance import measure_native

# These variables can direct Git to a different repository, config or graph.
GIT_REPOSITORY_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)


def open_repository(directory: Path) -> pygit2.Repository | None:
    if GIT_REPOSITORY_ENV & os.environ.keys():
        return None
    try:
        with measure_native("discover"):
            if ceilings := os.environ.get("GIT_CEILING_DIRECTORIES"):
                discovered = pygit2.discover_repository(os.fspath(directory), False, ceilings)
                return pygit2.Repository(discovered) if discovered is not None else None
            return pygit2.Repository(directory)
    except pygit2.GitError, ValueError, OSError:
        # Git handles its own additional formats and reports discovery errors.
        return None


def common_directory(repository: pygit2.Repository) -> Path:
    directory = Path(repository.path)
    try:
        relative = (directory / "commondir").read_text(encoding="utf-8", errors="surrogateescape").rstrip("\r\n")
    except FileNotFoundError:
        return directory
    return (directory / relative).resolve()


def config_key(key: str) -> str:
    section, dot, rest = key.partition(".")
    subsection, last_dot, name = rest.rpartition(".")
    return f"{section.lower()}{dot}{subsection}{last_dot}{name.lower()}"
