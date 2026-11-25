"""The package version and optional source revision for performance reports."""

import json
import os
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import cast

from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id
from git_stack.git_read import GIT_REPOSITORY_ENV

__version__ = "0.1.41"


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
        revision = GitClient(checkout).rev("HEAD")
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
