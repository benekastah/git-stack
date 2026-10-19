import os
import pathlib
import subprocess

import pytest

from git_stack import events
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, Repo

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_GH = pathlib.Path(__file__).resolve().parent / "fake_gh.py"


@pytest.fixture(scope="session")
def git_exec_path() -> pathlib.Path:
    """The directory git runs its own commands from, which holds the git
    binary itself rather than any wrapper (such as git-ai) or shim on PATH.
    Wrappers can make each of the tests' many git processes several times
    slower."""
    return pathlib.Path(subprocess.run(["git", "--exec-path"], check=True, capture_output=True,
                                       text=True).stdout.strip())


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, git_exec_path: pathlib.Path) -> Repo:
    """A repository with a, b and c committed to main and pushed to origin.

    git, git stack run as a separate process, and repo.client see an isolated
    git config and identity for the rest of the test. git is the git in
    git_exec_path, from tmp_path / "bin", which other fixtures can add
    commands to.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # git may find its commands and templates relative to its own path, which
    # through a symlink is bin_dir, so point it at the real ones.
    (bin_dir / "git").symlink_to((git_exec_path / "git").resolve())
    monkeypatch.setenv("GIT_EXEC_PATH", str(git_exec_path))
    monkeypatch.setenv("GIT_TEMPLATE_DIR",
                       str(git_exec_path.parent.parent / "share" / "git-core" / "templates"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    global_config = tmp_path / "gitconfig"
    global_config.write_text("[init]\n\tdefaultBranch = main\n")
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
        monkeypatch.setenv(name, value)

    repo = Repo(path=tmp_path / "local", origin=tmp_path / "origin.git", forge=tmp_path / "forge",
                tmp=tmp_path)
    subprocess.run(["git", "init", "--quiet", "--bare", repo.origin], check=True)
    subprocess.run(["git", "clone", "--quiet", repo.origin, repo.path], check=True, capture_output=True)
    for name in ("a", "b", "c"):
        repo.write(name, "base\n")
    repo.git("add", ".")
    repo.git("commit", "--quiet", "-m", "init")
    repo.git("push", "--quiet", "--set-upstream", "origin", "main")
    subprocess.run(["git", "clone", "--quiet", repo.origin, repo.forge], check=True, capture_output=True)
    # Everything should target repo.path explicitly. Run from a directory
    # that isn't a repository, so anything that doesn't fails instead of
    # touching the repository the tests were started in.
    monkeypatch.chdir(tmp_path)
    return repo


@pytest.fixture
def gh(repo: Repo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Make repo look like a GitHub repository, with tests/fake_gh.py as gh."""
    (tmp_path / "bin" / "gh").symlink_to(FAKE_GH)
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
