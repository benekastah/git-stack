import os
import pathlib
import subprocess

import pytest

from git_stack import events
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, Repo

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_GH = pathlib.Path(__file__).resolve().parent / "fake_gh.py"


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """A repository with a, b and c committed to main and pushed to origin,
    and the current directory for the rest of the test.

    git, git stack run as a separate process, and repo.client see an isolated
    git config and identity for the rest of the test.
    """
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
    monkeypatch.chdir(repo.path)
    return repo


@pytest.fixture
def gh(repo: Repo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Make repo look like a GitHub repository, with tests/fake_gh.py as gh."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").symlink_to(FAKE_GH)
    gh = FakeGitHub(tmp_path / "gh-state.json")
    gh.save({"prs": [], "calls": [], "fail": []})
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
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
