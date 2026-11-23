import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Any

import pygit2
import pytest
from pygit2.enums import ConfigLevel

from git_stack import events
from git_stack.adapters import github
from git_stack.git import GIT_VARIABLE
from git_stack.git_command import GitCommand
from git_stack.stack import StackClient
from tests import operations
from tests.git_support import GitCall, real_git
from tests.helpers import FakeGitHub, InProcessGh, Repo, install_command

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


# git itself, rather than a wrapper (such as git-ai) or shim on PATH, which can
# make each of the tests' many git processes several times slower. git-stack,
# which reads $GIT_STACK_GIT, runs it too.
os.environ[GIT_VARIABLE] = real_git()
GIT_EXEC_PATH = pathlib.Path(real_git()).parent


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "large: a test of a large repository (tests/large); run by `dev.py large-tests`, never by default"
    )


def pytest_ignore_collect(collection_path: pathlib.Path, config: pytest.Config) -> bool | None:
    """Collect tests/large only when `-m large` asks for it: building and copying the repository
    takes minutes and a few hundred MB, so it's never part of `dev.py tests` or CI's test jobs."""
    if collection_path == ROOT / "tests" / "large" and "large" not in (config.getoption("markexpr") or ""):
        return True
    return None


TEST_GIT_CONFIG = """\
[user]
    name = Test
    email = test@example.com
[init]
    defaultBranch = main
[maintenance]
    auto = false
[gc]
    auto = 0
[core]
    excludesFile =
    attributesFile =
"""


@pytest.fixture(scope="session", autouse=True)
def git_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[pathlib.Path]:
    """Isolate Git and libgit2 before constructing session-scoped repositories."""
    directory = tmp_path_factory.mktemp("git-environment")
    global_config = directory / ".gitconfig"
    global_config.write_text(TEST_GIT_CONFIG, encoding="utf-8")
    ceilings = (tmp_path_factory.getbasetemp().resolve(), pathlib.Path(tempfile.gettempdir()).resolve())

    search_paths = pygit2.settings.search_path
    levels = (ConfigLevel.PROGRAMDATA, ConfigLevel.SYSTEM, ConfigLevel.XDG, ConfigLevel.GLOBAL)
    original_paths = {level: search_paths[level] for level in levels}
    with pytest.MonkeyPatch.context() as patch:
        for name in tuple(os.environ):
            if (name.startswith("GIT_") and name != GIT_VARIABLE) or name == "SSH_ASKPASS":
                patch.delenv(name)
        patch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        patch.setenv("GIT_ATTR_NOSYSTEM", "1")
        patch.setenv("GIT_CEILING_DIRECTORIES", os.pathsep.join(map(str, ceilings)))
        patch.setenv("GIT_ALLOW_PROTOCOL", "file")
        patch.setenv("GIT_TERMINAL_PROMPT", "0")
        patch.setenv("GIT_EDITOR", "true")

        try:
            for level in levels:
                search_paths[level] = str(directory)
            yield global_config
        finally:
            for level, path in original_paths.items():
                search_paths[level] = path


@pytest.fixture(autouse=True)
def git_config(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, git_environment: pathlib.Path
) -> Iterator[pathlib.Path]:
    """Give each test its own global config and a working directory outside any repository."""
    directory = tmp_path / "git-environment"
    directory.mkdir()
    config = directory / ".gitconfig"
    shutil.copyfile(git_environment, config)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.chdir(directory)

    search_paths = pygit2.settings.search_path
    original = search_paths[ConfigLevel.GLOBAL]
    search_paths[ConfigLevel.GLOBAL] = str(directory)
    try:
        yield config
    finally:
        search_paths[ConfigLevel.GLOBAL] = original


@pytest.fixture
def performance_log(tmp_path: pathlib.Path) -> pathlib.Path:
    """A log path for tests that enable performance reporting."""
    return tmp_path / "perf log" / "perf.log"


@pytest.fixture
def performance_enabled(git_config: pathlib.Path, performance_log: pathlib.Path) -> None:
    with git_config.open("a", encoding="utf-8") as config:
        config.write(f'\n[stack]\n\tperfLog = "{performance_log.as_posix()}"\n')


def make_repositories(directory: pathlib.Path) -> None:
    """origin.git, local and forge as the repo fixture starts with them."""
    repo = Repo(path=directory / "local", origin=directory / "origin.git", forge=directory / "forge", tmp=directory)
    repo.git("init", "--quiet", "--bare", str(repo.origin), cwd=directory)
    repo.git("clone", "--quiet", str(repo.origin), str(repo.path), cwd=directory)
    for name in ("a", "b", "c"):
        repo.write(name, "base\n")
    repo.git("add", ".")
    repo.git("commit", "--quiet", "-m", "init")
    repo.git("push", "--quiet", "--set-upstream", "origin", "main")
    repo.git("clone", "--quiet", str(repo.origin), str(repo.forge), cwd=directory)


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory, git_environment: pathlib.Path) -> pathlib.Path:
    """Build a repository and its two clones once per test worker."""
    directory = tmp_path_factory.mktemp("repo-template")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", str(git_environment))
        make_repositories(directory)
    for sample in directory.glob("**/hooks/*.sample"):
        sample.unlink()
    return directory


@pytest.fixture(params=[1, 3, 10], ids=lambda size: f"{size}-branches")
def chain(repo: Repo, request: pytest.FixtureRequest) -> list[str]:
    """A stack of 1, 3 and 10 branches, the last checked out: the sizes the performance tests
    (tests/test_operations_*.py) run each scenario at, to show what grows with the stack. A
    fixture, so that the commits it makes aren't counted by a decorator on the test."""
    return repo.make_chain(request.param)


@pytest.fixture
def separate_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """For performance tests: git stack runs only as a process of its own (`Repo.stack_process`),
    as a user runs it, so nothing Python caches in this process between runs skews a count."""

    def refuse(self: Repo, *args: str, **kwargs: Any) -> None:
        pytest.fail(
            "a performance test runs git stack with stack_process: in this process, Python caches what it learns"
        )

    monkeypatch.setattr(Repo, "stack", refuse)


@pytest.fixture(autouse=True)
def count_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record the git processes and gh commands a test runs, for tests.operations.expect_operations."""
    operations.install(monkeypatch)


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[GitCall]:
    calls: list[GitCall] = []
    process = GitCommand.process

    def recording(self: GitCommand, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(GitCall(args, self.program))
        return process(self, *args, **kwargs)

    monkeypatch.setattr(GitCommand, "process", recording)
    return calls


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, repo_template: pathlib.Path) -> Repo:
    """A repository with a, b and c committed to main and pushed to origin,
    made once per test worker and copied for each test, which is much quicker
    than making them with git each time.

    PATH has tmp_path / "bin" first, which other fixtures can add commands to
    (see install_command), then GIT_EXEC_PATH, so git is the git there; and
    PYTHONPATH has the repository's root, so git stack runs from it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(GIT_EXEC_PATH), os.environ["PATH"]]))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    repos = tmp_path / "repos"
    shutil.copytree(repo_template, repos, symlinks=True)
    repo = Repo(path=repos / "local", origin=repos / "origin.git", forge=repos / "forge", tmp=repos)
    for clone in (repo.path, repo.forge):
        repo.git("remote", "set-url", "origin", str(repo.origin), cwd=clone)
    return repo


@pytest.fixture
def gh(repo: Repo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Make repo look like a GitHub repository, with tests/fake_gh.py as gh."""
    # For git-stack run as a process of its own; in this one, the adapter
    # runs fake_gh in the test's process (see InProcessGh), which is much
    # quicker than starting Python for each gh command.
    install_command(tmp_path / "bin", "gh", "tests.fake_gh")
    monkeypatch.setattr(github, "subprocess", InProcessGh())
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
