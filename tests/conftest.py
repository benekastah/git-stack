import os
import pathlib
import shutil
import subprocess
from typing import Any

import pytest

from git_stack import events
from git_stack.git import CLOSE_FDS
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, GitCommand, Repo, install_command, mark_git_commands

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


CI_WORKERS = 8
"""pytest -n auto's workers in CI, rather than one per CPU (3 or 4 on its runners): the tests
spend most of their time waiting for the git processes they start, and the files they write,
so more keep the CPUs busy."""


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """-n auto's workers: CI_WORKERS in CI ($CI set), unless PYTEST_XDIST_AUTO_NUM_WORKERS
    says how many; otherwise (None) pytest-xdist's own choice."""
    if os.environ.get("CI") and "PYTEST_XDIST_AUTO_NUM_WORKERS" not in os.environ:
        return CI_WORKERS
    return None


# git itself, from the directory it runs its own commands from, rather than a
# wrapper (such as git-ai) or shim on PATH, which can make each of the tests'
# many git processes several times slower. git-stack, which reads
# GIT_STACK_GIT once (see git_program), runs it too.
GIT_EXEC_PATH = pathlib.Path(
    subprocess.run(["git", "--exec-path"], check=True, capture_output=True, text=True).stdout.strip()
)
if (real_git := GIT_EXEC_PATH / ("git.exe" if os.name == "nt" else "git")).exists():
    os.environ["GIT_STACK_GIT"] = str(real_git)


def isolate_git(mp: pytest.MonkeyPatch, directory: pathlib.Path, path: list[pathlib.Path]) -> None:
    """Give git and git stack, in this process or another, an isolated git
    config (directory / "gitconfig") and identity, with path before PATH."""
    mp.setenv("PATH", os.pathsep.join([*map(str, path), os.environ["PATH"]]))
    # A git hook runs with these pointing at its repository (in a linked
    # worktree, GIT_DIR is absolute), so tests run from a hook (e.g. `dev.py
    # check` from one) would otherwise commit to, and push from, the
    # repository being pushed.
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
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """origin.git, local and forge as the repo fixture starts with them, made
    once (for each test worker) and copied for each test, which is much
    quicker than making them with git each time."""
    template = tmp_path_factory.mktemp("template")
    repo = Repo(path=template / "local", origin=template / "origin.git", forge=template / "forge", tmp=template)
    with pytest.MonkeyPatch.context() as mp:
        isolate_git(mp, template, [GIT_EXEC_PATH])
        repo.git("init", "--quiet", "--bare", str(repo.origin), cwd=template)
        repo.git("clone", "--quiet", str(repo.origin), str(repo.path), cwd=template)
        for name in ("a", "b", "c"):
            repo.write(name, "base\n")
        repo.git("add", ".")
        repo.git("commit", "--quiet", "-m", "init")
        repo.git("push", "--quiet", "--set-upstream", "origin", "main")
        repo.git("clone", "--quiet", str(repo.origin), str(repo.forge), cwd=template)
    # Fewer files to copy. The hooks directories stay, for Repo.add_hook.
    for sample in template.glob("**/hooks/*.sample"):
        sample.unlink()
    return template


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, repo_template: pathlib.Path) -> Repo:
    """A repository with a, b and c committed to main and pushed to origin.

    git and git stack, in this process or another, see an isolated git config
    and identity for the rest of the test. PATH has tmp_path /
    "bin" first, which other fixtures can add commands to (see
    install_command), then GIT_EXEC_PATH, so git is the git there.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    isolate_git(monkeypatch, tmp_path, [bin_dir, GIT_EXEC_PATH])
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
    """The git processes GitClient starts from now on, each as its arguments
    without `git -C <dir>`. Each must pass close_fds=CLOSE_FDS, or close them
    if it's the `git cat-file` that runs until a snapshot ends, which this
    checks only for the calls the tests that use it reach."""
    calls: list[list[str]] = []
    mark_git_commands(monkeypatch)

    class Popen(subprocess.Popen[Any]):
        def __init__(self, args: list[str], *rest: Any, **kwargs: Any) -> None:
            if isinstance(args, GitCommand):
                calls.append(args[3:])
                long_lived = args[3:5] == ["cat-file", "--batch-check=%(objectname)"]
                assert kwargs.get("close_fds") == (long_lived or CLOSE_FDS)
            super().__init__(args, *rest, **kwargs)

    # subprocess.run starts its process with subprocess.Popen too.
    monkeypatch.setattr(subprocess, "Popen", Popen)
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
