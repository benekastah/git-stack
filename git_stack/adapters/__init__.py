"""Adapters work out whether branches were merged, and may act on pushes.

The stack.adapter setting picks one: git looks only at the repository, and
github also asks GitHub about each branch's pull request.
"""

import enum
import shutil
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar

from git_stack.adapters.git import GitAdapter
from git_stack.adapters.github import GitHubAdapter, github_repo
from git_stack.core import StackCore
from git_stack.errors import StackError

ADAPTER_SETTING = "stack.adapter"


class Adapter(enum.Enum):
    """The adapters ADAPTER_SETTING can ask for. Values are the setting's."""

    GIT = "git"
    GITHUB = "github"


class GitHubAccess(enum.Enum):
    """Whether a CLI invocation opts in to GitHub or explicitly chooses Git alone."""

    REQUIRED_OPT_IN = enum.auto()
    ENABLED = enum.auto()
    GIT_ONLY = enum.auto()


# The CLI sets a policy per invocation. Library callers retain their adapter behavior.
_github_access = ContextVar("github_access", default=GitHubAccess.ENABLED)


def github_access() -> GitHubAccess:
    return _github_access.get()


@contextmanager
def use_github_access(access: GitHubAccess) -> Generator[None]:
    token = _github_access.set(access)
    try:
        yield
    finally:
        _github_access.reset(token)


def make_adapter(
    core: StackCore,
    remote: str,
    fetch: bool,
    *,
    lookup_branches: Iterable[str] | None = None,
    allow_local_remote: bool = False,
) -> GitAdapter:
    """The adapter stack.adapter asks for. If unset, GitHub's when remote is on
    github.com and gh is installed, and git's otherwise.

    fetch says whether the adapter may fetch from remote. It's False for
    commands that otherwise don't fetch (info and restack). Mixed-remote
    restack can fall back to local evidence for non-GitHub remotes.
    """
    if github_access() is not GitHubAccess.ENABLED:
        return GitAdapter(core)
    value = core.git.config_value(ADAPTER_SETTING)
    try:
        setting = Adapter(value) if value else None
    except ValueError:
        choices = " or ".join(a.value for a in Adapter)
        raise StackError(f"{ADAPTER_SETTING} must be {choices}, not {value}") from None
    if setting is Adapter.GIT:
        return GitAdapter(core)
    repo = github_repo(core.git, remote)
    if setting is Adapter.GITHUB:
        if repo is None:
            if allow_local_remote:
                return GitAdapter(core)
            raise StackError(f"{ADAPTER_SETTING} is github, but {remote}'s URL isn't a GitHub repository")
        if not shutil.which("gh"):
            raise StackError(f"{ADAPTER_SETTING} is github, which needs the gh CLI on your PATH")
        return GitHubAdapter(core, remote, *repo, fetch=fetch, lookup_branches=lookup_branches)
    if repo and repo[0] == "github.com" and shutil.which("gh"):
        return GitHubAdapter(core, remote, *repo, fetch=fetch, lookup_branches=lookup_branches)
    return GitAdapter(core)
