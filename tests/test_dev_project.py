"""dev_project.py: git-stack's additions to dev.py's install."""

import os
import re
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dev(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT))
    import dev

    # The clone running the tests may be a scratch one in a temporary directory, which
    # install refuses outright; the tests' installs go to a home of their own.
    monkeypatch.setattr(dev, "_temporary", lambda names: None)
    return dev


@pytest.fixture
def dev_project(dev: ModuleType) -> ModuleType:
    import dev_project

    return dev_project


@pytest.mark.parametrize(
    ("optional_paths", "ignore_revs_file"),
    [(True, ":(optional).git-blame-ignore-revs"), (False, ".git-blame-ignore-revs")],
)
def test_install_sets_up_git_blame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    dev: ModuleType,
    dev_project: ModuleType,
    optional_paths: bool,
    ignore_revs_file: str,
) -> None:
    bin_dir = tmp_path / ".local" / "bin"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # On PATH already, so install doesn't add it to the real user's PATH on Windows.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(dev, "venv", lambda: None)
    monkeypatch.setattr(dev, "run", lambda *args: commands.append(args))
    monkeypatch.setattr(dev_project, "run", lambda *args: commands.append(args))
    monkeypatch.setattr(dev_project, "run_code", lambda *args: 0)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: optional_paths)

    dev.install([])

    assert ("git", "config", "blame.ignoreRevsFile", ignore_revs_file) in commands


def test_install_links_the_agent_skills_to_this_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dev_project: ModuleType
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(dev_project, "run", lambda *args: None)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: True)

    dev_project.after_install()

    link = tmp_path / ".claude" / "skills" / "git-stack"
    assert link.resolve() == (ROOT / "git_stack" / "skills" / "git-stack").resolve()


def test_install_only_warns_when_the_skills_cant_be_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], dev_project: ModuleType
) -> None:
    monkeypatch.setattr(dev_project, "run", lambda *args: None)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: True)
    monkeypatch.setattr(dev_project, "run_code", lambda *args: 1)

    dev_project.after_install()

    assert "git stack install-skills" in capsys.readouterr().err


def git_version() -> tuple[int, int]:
    version = subprocess.run(["git", "version"], capture_output=True, encoding="utf-8", check=True).stdout
    match = re.search(r"(\d+)\.(\d+)", version)
    assert match is not None, version
    return int(match[1]), int(match[2])


@pytest.mark.skipif(git_version() < (2, 52), reason="git takes :(optional) paths from 2.52")
def test_the_optional_paths_probe_says_yes_on_a_git_that_takes_them(dev_project: ModuleType) -> None:
    # The real probe, in this clone: a broken one would answer no, and install would set a
    # blame.ignoreRevsFile that fails on checkouts without the file.
    assert dev_project._git_has_optional_paths()  # pyright: ignore[reportPrivateUsage]
