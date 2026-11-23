"""Local Git reads and CLI fallbacks for repository-specific semantics."""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pygit2

from git_stack.errors import StackError
from git_stack.git_command import GitCommand, decode

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


@dataclass(frozen=True)
class Ref:
    name: str
    object: str
    upstream: str
    symref: str
    head: bool


def refs(git: GitCommand, *patterns: str) -> dict[str, Ref]:
    output = git.run_bytes(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(upstream)%00%(symref)%00%(HEAD)",
        "--end-of-options",
        *patterns,
    )
    result: dict[str, Ref] = {}
    for line in decode(output).split("\n"):
        if line:
            name, oid, upstream, symref, head = line.split("\0")
            result[name] = Ref(name, oid, upstream, symref, head == "*")
    return result


def config(git: GitCommand) -> dict[str, list[str | None]]:
    entries: dict[str, list[str | None]] = {}
    for entry in decode(git.run_bytes("config", "--list", "-z")).split("\0"):
        if entry:
            key, newline, value = entry.partition("\n")
            entries.setdefault(key, []).append(value if newline else None)
    return entries


def config_key(key: str) -> str:
    section, dot, rest = key.partition(".")
    subsection, last_dot, name = rest.rpartition(".")
    return f"{section.lower()}{dot}{subsection}{last_dot}{name.lower()}"


_PATCH_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--no-relative",
    "--ignore-submodules=none",
    "--no-textconv",
)
_COMMIT_OPTIONS = ("--no-show-signature", "--no-abbrev-commit", "--pretty=medium")
_END_DIFF = b"diff --git a/end b/end\n--- a/end\n+++ b/end\n@@ -0,0 +1 @@\n+end\n"


def patch_ids(git: GitCommand, commands: Sequence[Sequence[str]], oid_length: int) -> list[list[tuple[str, str]]]:
    patches: list[bytes] = []
    for number, (command, *args) in enumerate(commands, 1):
        if command == "diff":
            patches.append(b"commit " + b"0" * oid_length + b"\n")
        options = (*_PATCH_OPTIONS, *_COMMIT_OPTIONS) if command in {"log", "show"} else _PATCH_OPTIONS
        env = {}
        if "--" in args:
            start = args.index("--") + 1
            args[start:] = [f":(top,literal){path}" for path in args[start:]]
            env = {"GIT_LITERAL_PATHSPECS": "0", "GIT_ICASE_PATHSPECS": "0"}
        patches.append(git.run_bytes(command, *options, *args, env=env))
        # patch-id labels each result with the commit from its input. A synthetic
        # patch separates the results of successive commands, including empty ones.
        patches.append(f"\ncommit {number:0{oid_length}x}\n\n".encode("ascii") + _END_DIFF)
    results: list[list[tuple[str, str]]] = [[]]
    for line in decode(git.run_bytes("patch-id", "--stable", input=b"".join(patches))).splitlines():
        patch_id, commit = line.split()
        if commit == f"{len(results):0{oid_length}x}":
            results.append([])
        else:
            results[-1].append((patch_id, commit))
    if len(results) != len(commands) + 1:
        raise StackError("git patch-id did not report every patch group")
    return results[:-1]
