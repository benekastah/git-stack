import os
import pathlib
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from git_stack import events
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, Repo, install_command

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The environment variables that tell git which repository to use, instead of
# the one in the directory it runs in.
GIT_REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
)


@pytest.fixture(scope="session")
def git_exec_path() -> pathlib.Path:
    """The directory git runs its own commands from, which holds the git
    binary itself rather than any wrapper (such as git-ai) or shim on PATH.
    Wrappers can make each of the tests' many git processes several times
    slower."""
    return pathlib.Path(
        subprocess.run(["git", "--exec-path"], check=True, capture_output=True, text=True).stdout.strip()
    )


def isolate_git(mp: pytest.MonkeyPatch, directory: pathlib.Path, path: list[pathlib.Path]) -> None:
    """Give git, git stack run as a separate process, and StackClient an
    isolated git config (directory / "gitconfig") and identity, with path
    before PATH."""
    mp.setenv("PATH", os.pathsep.join([*map(str, path), os.environ["PATH"]]))
    # A git hook runs with these pointing at its repository (in a linked
    # worktree, GIT_DIR is absolute), so the tests run by the pre-push hook
    # would otherwise commit to, and push from, the repository being pushed.
    for name in GIT_REPOSITORY_VARIABLES:
        mp.delenv(name, raising=False)
    global_config = directory / "gitconfig"
    # Without auto-maintenance, which would otherwise start a `git maintenance
    # run --auto` after each commit, fetch, merge and push: thousands of
    # processes over the tests, none of which they need.
    global_config.write_text("[init]\n\tdefaultBranch = main\n[maintenance]\n\tauto = false\n", encoding="utf-8")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_EDITOR": "true",
        "PYTHONPATH": str(ROOT),
    }.items():
        mp.setenv(name, value)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory, git_exec_path: pathlib.Path) -> pathlib.Path:
    """origin.git, local and forge as the repo fixture starts with them, made
    once (for each test worker) and copied for each test, which is much
    quicker than making them with git each time."""
    template = tmp_path_factory.mktemp("template")
    repo = Repo(path=template / "local", origin=template / "origin.git", forge=template / "forge", tmp=template)
    with pytest.MonkeyPatch.context() as mp:
        isolate_git(mp, template, [git_exec_path])
        subprocess.run(["git", "init", "--quiet", "--bare", repo.origin], check=True)
        subprocess.run(["git", "clone", "--quiet", repo.origin, repo.path], check=True, capture_output=True)
        for name in ("a", "b", "c"):
            repo.write(name, "base\n")
        repo.git("add", ".")
        repo.git("commit", "--quiet", "-m", "init")
        repo.git("push", "--quiet", "--set-upstream", "origin", "main")
        subprocess.run(["git", "clone", "--quiet", repo.origin, repo.forge], check=True, capture_output=True)
    # Fewer files to copy. The hooks directories stay, for Repo.add_hook.
    for sample in template.glob("**/hooks/*.sample"):
        sample.unlink()
    return template


@pytest.fixture
def repo(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, git_exec_path: pathlib.Path, repo_template: pathlib.Path
) -> Repo:
    """A repository with a, b and c committed to main and pushed to origin.

    git, git stack run as a separate process, and repo.client see an isolated
    git config and identity for the rest of the test. PATH has tmp_path /
    "bin" first, which other fixtures can add commands to (see
    install_command), then git_exec_path, so git is the git there.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    isolate_git(monkeypatch, tmp_path, [bin_dir, git_exec_path])
    repo = Repo(path=tmp_path / "local", origin=tmp_path / "origin.git", forge=tmp_path / "forge", tmp=tmp_path)
    for name in ("origin.git", "local", "forge"):
        shutil.copytree(repo_template / name, tmp_path / name, symlinks=True)
    # Point the clones at this test's origin, not the template's.
    for clone in (repo.path, repo.forge):
        repo.git("config", "remote.origin.url", str(repo.origin), cwd=clone)
    # Everything should target repo.path explicitly. Run from a directory
    # that isn't a repository, so anything that doesn't fails instead of
    # touching the repository the tests were started in.
    monkeypatch.chdir(tmp_path)
    return repo


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The git commands GitClient runs from now on, each without `git -C <dir>`
    (which the tests' own git commands don't have)."""
    calls: list[list[str]] = []
    real: Callable[..., Any] = subprocess.run

    def run(args: list[str], *rest: Any, **kwargs: Any) -> Any:
        if args[:2] == ["git", "-C"]:
            calls.append(args[3:])
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


@pytest.fixture
def gh(repo: Repo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Make repo look like a GitHub repository, with tests/fake_gh.py as gh."""
    install_command(tmp_path / "bin", "gh", "tests.fake_gh")
    gh = FakeGitHub(tmp_path / "gh-state.json")
    gh.save({"prs": [], "calls": [], "fail": []})
    monkeypatch.setenv("FAKE_GH_STATE", str(gh.state_path))
    # Look like a GitHub repository while still using the local origin.
    url = "https://github.com/acme/widgets.git"
    repo.git("config", f"url.{repo.origin}.insteadOf", url)
    repo.git("config", "remote.origin.url", url)
    return gh


@pytest.fixture
def client(repo: Repo) -> StackClient:
    """A StackClient working in repo, as a script would use one."""
    return repo.client


@pytest.fixture
def emitted(repo: Repo) -> list[events.Event]:
    """The events client has emitted."""
    return repo.emitted
