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

MAN_SYNOPSIS = {"git-stack": "**git stack** [**-C** *path*] [`--performance`] *command* [*args*]"}
"""man/git-stack.1's SYNOPSIS (dev.py man makes the page from README.md)."""


def types_other_platforms(_: list[str]) -> None:
    """basedpyright as if on Windows and macOS: `dev.py static` checks only as if on Linux
    (pyproject.toml's pythonPlatform), which misses a call another system lacks."""
    venv()
    for platform in ("Windows", "Darwin"):
        run(tool("basedpyright"), "--pythonplatform", platform)


def after_install(replace: bool = False) -> None:
    """Make `git blame` in this clone skip the commits in .git-blame-ignore-revs, and link the
    agent skills (see `install_skills`). replace is dev.py install's --replace."""
    # `git blame` fails if the file is missing (e.g. with an older commit checked out), unless
    # this git supports marking the path :(optional).
    prefix = ":(optional)" if _git_has_optional_paths() else ""
    run("git", "config", "blame.ignoreRevsFile", f"{prefix}.git-blame-ignore-revs")
    print("made git blame skip the commits in .git-blame-ignore-revs")
    install_skills(replace)


def install_skills(replace: bool) -> None:
    """Link this clone's agent skills into the skills directory of each coding agent found, as
    `git stack install-skills` does (with --replace if replace), failing the install if it
    refuses a link, e.g. one to another clone's skills. That command makes the links (a
    symlink, or a junction on Windows), so they aren't `install_links()`, which makes plain
    symlinks Windows usually can't. It's no failure to find no agent."""
    if run_code(tool("git-stack"), "install-skills", *(["--replace"] if replace else [])) != 0:
        sys.exit(
            "install: couldn't link the agent skills (see above); `git stack install-skills"
            " --replace` takes over a link to another clone's"
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
