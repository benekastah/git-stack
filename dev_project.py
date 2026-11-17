"""git-stack's own additions to dev.py (see its docstring): the man page, and git blame.

    python dev.py man [--check]      # man/git-stack.1 from README.md (--check: fail if stale)
    python dev.py types-other-platforms  # basedpyright as if on Windows and macOS too
    python dev.py pre-commit-man     # what the pre-commit hook runs after dev.py pre-commit

`install` also links the man page into ~/.local/share/man/man1 (an editable install doesn't
install it; `install_links`) and makes `git blame` skip the commits in .git-blame-ignore-revs.
"""

import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from dev import ROOT, WINDOWS, run, tool, venv

PAGE = ROOT / "man" / "git-stack.1"
MAN_PAGE_INPUTS = ["README.md", "man/git-stack.lua", "man/build.py"]
"""What man/git-stack.1 is built from."""


def man(args: list[str]) -> None:
    """Build man/git-stack.1 from README.md; args go to man/build.py (e.g. --check)."""
    venv()
    run(tool("python"), str(ROOT / "man" / "build.py"), *args)


def types_other_platforms(_: list[str]) -> None:
    """basedpyright as if on Windows and macOS: `dev.py static` checks only as if on Linux
    (pyproject.toml's pythonPlatform), which misses a call another system lacks."""
    venv()
    for platform in ("Windows", "Darwin"):
        run(tool("basedpyright"), "--pythonplatform", platform)


def pre_commit_man(_: list[str]) -> None:
    """Regenerate man/git-stack.1 when the commit changes what it's built from, and add it to
    the same commit. It's built from the staged README, so it matches what's committed even
    if only part of the README is."""
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *MAN_PAGE_INPUTS], cwd=ROOT, check=False)
    if staged.returncode == 0:
        return
    if staged.returncode != 1:
        sys.exit(staged.returncode)
    readme = subprocess.run(["git", "show", ":README.md"], cwd=ROOT, capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as scratch:
        staged_readme = Path(scratch) / "README.md"
        staged_readme.write_bytes(readme)
        man(["--readme", str(staged_readme)])
    subprocess.run(["git", "add", "--", str(PAGE.relative_to(ROOT))], cwd=ROOT, check=True)


def _man_dir() -> Path:
    return Path.home() / ".local" / "share" / "man" / "man1"


def install_links() -> list[tuple[Path, Path]]:
    """The man page's link, where `man` looks, for `install` to make with the command's (none
    on Windows, which has no `man`)."""
    return [] if WINDOWS else [(_man_dir() / PAGE.name, PAGE)]


def after_install() -> None:
    """Warn if `man` won't find the man page `install` linked, and make `git blame` in this
    clone skip the commits in .git-blame-ignore-revs."""
    if not WINDOWS:
        _check_man_path()
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


def _check_man_path() -> None:
    found = shutil.which("man") is not None and (
        subprocess.run(["man", "-w", "git-stack"], capture_output=True, check=False).returncode == 0
    )
    if not found:
        print(
            f"warning: man doesn't search {_man_dir().parent}, so git stack --help won't find the man page;"
            ' add export MANPATH="$HOME/.local/share/man:" to your shell\'s startup file',
            file=sys.stderr,
        )


COMMANDS: dict[str, Callable[[list[str]], None]] = {
    "man": man,
    "pre-commit-man": pre_commit_man,
    "types-other-platforms": types_other_platforms,
}
