"""git-stack's own additions to dev.py (see its docstring): the man page's SYNOPSIS, and git
blame.

    python dev.py types-other-platforms  # basedpyright as if on Windows and macOS too
    python dev.py large-tests            # the tests of a 500,000-commit repository (minutes, a few hundred MB)
    python dev.py release                # the GitHub release of version.txt's version, if it has none yet

`install` also makes `git blame` skip the commits in .git-blame-ignore-revs, and links the agent
skills into the directories of the coding agents it finds (`git stack install-skills`).
"""

import re
import subprocess
import sys
from collections.abc import Callable

from dev import ROOT, run, run_code, tool, venv

VERSION_FILE = ROOT / "version.txt"
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}((?:a|b|rc){NUMBER})?", re.ASCII)
"""What version.txt has: MAJOR.MINOR.PATCH, and, for a pre-release, a suffix as PEP 440 spells it
(0.3.0rc1, 0.3.0a2, 0.3.0b1), since setuptools reads the file as a Python version. No leading
zeros, which Python's normalization would drop from the version but not from the tag."""

MAN_SYNOPSIS = {"git-stack": "**git stack** [**-C** *path*] [`--json`] *command* [*args*]"}
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
    refuses a link, e.g. one to another copy's skills. That command makes the links (a
    symlink, or a junction on Windows), so they aren't `install_links()`, which makes plain
    symlinks Windows usually can't. It's no failure to find no agent."""
    if run_code(tool("git-stack"), "install-skills", *(["--replace"] if replace else [])) != 0:
        # --replace only helps with a link to another copy's skills, not a real directory in the way or
        # an I/O error, which the command says itself; so it's offered, as one thing to try, unless it was given.
        hint = "" if replace else "; `git stack install-skills --replace` takes over a link to another copy's skills"
        sys.exit(f"install: couldn't link the agent skills{hint}")


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


def large_tests(pytest_args: list[str]) -> None:
    """The tests of a large repository (tests/large): the same scenarios as the small tests, with
    the same declared counts, in a repository of 500,000 commits and 5,000 branches that the first
    run builds (about a minute) and pytest's cache keeps. One process at a time, so they don't
    compete, and their times, which are reported and never asserted, mean something. Any
    arguments go to pytest."""
    venv()
    code = run_code(tool("python"), "-m", "pytest", "-m", "large", "--numprocesses", "0", *pytest_args)
    if code:
        sys.exit(code)


def release(_: list[str]) -> None:
    """Make the GitHub release of the version in version.txt, v<version>, at HEAD, with notes
    generated from the pull requests since the last release, unless there's one already. A
    version with a suffix (0.3.0rc1) is a pre-release. The release workflow runs this when a
    change to version.txt lands on main; gh must be logged in (in CI, with GH_TOKEN)."""
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    match = VERSION.fullmatch(version)
    if match is None:
        sys.exit(f"{VERSION_FILE.name} has {version!r}, which isn't a version like 1.2.3 or 1.2.3rc1")
    tag = f"v{version}"
    try:
        view = subprocess.run(
            ["gh", "release", "view", tag], capture_output=True, encoding="utf-8", check=False, cwd=ROOT
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, encoding="utf-8", check=True, cwd=ROOT
        ).stdout.strip()
    except FileNotFoundError as error:
        sys.exit(f"release needs {error.filename}, which isn't installed")
    if view.returncode == 0:
        print(f"{tag} is already released")
        return
    if "release not found" not in view.stderr.lower():
        # Not that there's none: gh couldn't ask (not logged in, no network), and making one would fail too.
        sys.exit(f"gh release view {tag} failed:\n{view.stderr.strip()}")
    prerelease = ["--prerelease"] if match[1] else []
    run("gh", "release", "create", tag, "--target", commit, "--title", tag, "--generate-notes", *prerelease)


COMMANDS: dict[str, Callable[[list[str]], None]] = {
    "types-other-platforms": types_other_platforms,
    "large-tests": large_tests,
    "release": release,
}
