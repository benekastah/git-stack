"""git-stack's own additions to dev.py (see its docstring): the man page's SYNOPSIS, and git
blame.

    python dev.py types-other-platforms  # basedpyright as if on Windows and macOS too

`install` also makes `git blame` skip the commits in .git-blame-ignore-revs.
"""

import subprocess
from collections.abc import Callable

from dev import ROOT, run, tool, venv

MAN_SYNOPSIS = {"git-stack": "**git stack** [**-C** *path*] *command* [*args*]"}
"""man/git-stack.1's SYNOPSIS (dev.py man makes the page from README.md)."""


def types_other_platforms(_: list[str]) -> None:
    """basedpyright as if on Windows and macOS: `dev.py static` checks only as if on Linux
    (pyproject.toml's pythonPlatform), which misses a call another system lacks."""
    venv()
    for platform in ("Windows", "Darwin"):
        run(tool("basedpyright"), "--pythonplatform", platform)


def after_install() -> None:
    """Make `git blame` in this clone skip the commits in .git-blame-ignore-revs."""
    # `git blame` fails if the file is missing (e.g. with an older commit checked out), unless
    # this git supports marking the path :(optional).
    prefix = ":(optional)" if _git_has_optional_paths() else ""
    run("git", "config", "blame.ignoreRevsFile", f"{prefix}.git-blame-ignore-revs")
    print("made git blame skip the commits in .git-blame-ignore-revs")


def _git_has_optional_paths() -> bool:
    """Whether this git takes a path marked :(optional), which it ignores if missing."""
    probe = subprocess.run(
        [
            "git",
            "-c",
            f"blame.ignoreRevsFile=:(optional){ROOT / 'no-such-file'}",
            "blame",
            "-L",
            "1,1",
            "--",
            "LICENSE",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


COMMANDS: dict[str, Callable[[list[str]], None]] = {
    "types-other-platforms": types_other_platforms,
}
