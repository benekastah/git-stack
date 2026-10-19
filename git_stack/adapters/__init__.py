"""Adapters work out whether branches were merged, and may act on pushes.

The stack.adapter setting picks one: git looks only at the repository, and
github also asks GitHub about each branch's pull request.
"""

import shutil

from git_stack.adapters.git import GitAdapter
from git_stack.adapters.github import GitHubAdapter, github_repo
from git_stack.core import StackCore
from git_stack.errors import StackError

ADAPTER_SETTING = "stack.adapter"


def make_adapter(core: StackCore, remote: str, fetch: bool) -> GitAdapter:
    """The adapter stack.adapter asks for. If unset, GitHub's when remote is on
    github.com and gh is installed, and git's otherwise.

    fetch says whether the adapter may fetch from remote. It's False for
    commands that otherwise don't fetch (info and restack).
    """
    setting = core.git.run("config", "--get", ADAPTER_SETTING, check=False)
    if setting not in ("", "git", "github"):
        raise StackError(f"{ADAPTER_SETTING} must be git or github, not {setting}")
    if setting == "git":
        return GitAdapter(core)
    repo = github_repo(core.git, remote)
    if setting == "github":
        if repo is None:
            raise StackError(f"{ADAPTER_SETTING} is github, but {remote}'s URL isn't a GitHub repository")
        if not shutil.which("gh"):
            raise StackError(f"{ADAPTER_SETTING} is github, which needs the gh CLI on your PATH")
        return GitHubAdapter(core, remote, *repo, fetch=fetch)
    if repo and repo[0] == "github.com" and shutil.which("gh"):
        return GitHubAdapter(core, remote, *repo, fetch=fetch)
    return GitAdapter(core)
