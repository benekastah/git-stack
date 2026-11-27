"""The package version, from version.txt, and optional source revision for performance reports."""

import json
import os
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import cast

from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id
from git_stack.git_command import ProcessEnv
from git_stack.git_read import GIT_REPOSITORY_ENV


def read_version(checkout: Path) -> str:
    """The version in the version.txt of a checkout (or an editable install of one: a pyproject.toml
    beside it says so, since an installed package's directory above is site-packages, where another
    package may leave a version.txt of its own), else the one the installed package's metadata has,
    which setuptools took from that file when it was built."""
    if (checkout / "pyproject.toml").is_file():
        try:
            return (checkout / "version.txt").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    try:
        return installed_version("git-stack")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = read_version(Path(__file__).resolve().parents[1])


def build_version() -> str:
    """Identify the installed package and, when available, its source commit."""
    checkout = Path(__file__).resolve().parents[1]
    revision = _checkout_revision(checkout) or _installed_revision()
    return f"{__version__} (git {revision})" if revision else __version__


def _checkout_revision(checkout: Path) -> str | None:
    if GIT_REPOSITORY_ENV & os.environ.keys():
        return None
    # Require this package's source checkout, including linked worktrees.
    if not (checkout / ".git").exists() or not (checkout / "pyproject.toml").is_file():
        return None
    try:
        revision = GitClient(checkout, process_env=ProcessEnv()).rev("HEAD")
    except StackError, OSError:
        return None
    return revision if revision is not None and is_object_id(revision) else None


def _installed_revision() -> str | None:
    try:
        metadata: object = json.loads(distribution("git-stack").read_text("direct_url.json") or "{}")
    except PackageNotFoundError, OSError, ValueError:
        return None
    vcs: object = cast("dict[str, object]", metadata).get("vcs_info") if isinstance(metadata, dict) else None
    if not isinstance(vcs, dict):
        return None
    git_info = cast("dict[str, object]", vcs)
    if git_info.get("vcs") != "git":
        return None
    revision = git_info.get("commit_id")
    return revision if isinstance(revision, str) and is_object_id(revision) else None
