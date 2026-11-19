import os
import pathlib

import pytest

# Before anything imports it, so pytest rewrites its asserts, as a plugin's.
pytest.register_assert_rewrite("git_client.testing")

from git_client.testing import RepoTemplates, real_git

from git_stack import events
from git_stack.adapters import github
from git_stack.git import GIT_VARIABLE
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, InProcessGh, Repo, install_command

# py-git-client's plugin gives every test (session fixtures included) a git
# config of its own, and keeps the user's config and git's variables (e.g. a
# hook's GIT_DIR) from reaching it; and its fixtures (repo_templates,
# git_calls, outside_any_repository) are the tests'.
pytest_plugins = ["git_client.testing"]

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
# which reads $GIT_STACK_GIT once (see git_client.find_git), runs it too.
os.environ[GIT_VARIABLE] = real_git()
GIT_EXEC_PATH = pathlib.Path(real_git()).parent


@pytest.fixture(autouse=True)
def _outside_any_repository(outside_any_repository: pathlib.Path) -> None:  # pyright: ignore[reportUnusedFunction]
    """Run every test from a directory where git finds no repository, so
    anything that doesn't name its repository fails, rather than change the
    one the tests were started in (its config, say)."""


@pytest.fixture(autouse=True)
def performance_log(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Keep in-process and subprocess performance logs under the test's directory."""
    path = tmp_path / "perf-log" / "perf.log"
    monkeypatch.setenv("GIT_STACK_PERF_LOG", str(path))
    return path


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


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, repo_templates: RepoTemplates) -> Repo:
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
    repo_templates.build("repo", make_repositories)
    repos = repo_templates.copy("repo", tmp_path / "repos")
    return Repo(path=repos / "local", origin=repos / "origin.git", forge=repos / "forge", tmp=repos)


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
