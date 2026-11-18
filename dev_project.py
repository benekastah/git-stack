"""git-stack's own additions to dev.py (see its docstring): the man page's SYNOPSIS, and git
blame.

    python dev.py types-other-platforms  # basedpyright as if on Windows and macOS too

`install` also makes `git blame` skip the commits in .git-blame-ignore-revs, and links the agent
skills into the directories of the coding agents it finds (`git stack install-skills`).
"""

import subprocess
import sys
from collections.abc import Callable

from dev import ROOT, run, run_code, tool, venv

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
    install_skills()


def install_skills() -> None:
    """Link this clone's agent skills into each coding agent's skills directory, as
    `git stack install-skills` does for the agents it finds. That command makes the links (a
    symlink, or a junction on Windows), so they aren't `install_links()`: they'd need a
    symlink Windows usually can't make. It fails when it finds no agent, which isn't this
    install's failure, so that only warns."""
    if run_code(tool("git-stack"), "install-skills") != 0:
        print(
            "warning: couldn't link the agent skills (see above); run `git stack install-skills`"
            " once an agent is installed (add --replace to take over another clone's links)",
            file=sys.stderr,
        )


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
