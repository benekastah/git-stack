"""dev_project.py: git-stack's additions to dev.py's install and pre-commit hook."""

import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.helpers import Repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dev(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT))
    import dev

    return dev


@pytest.fixture
def dev_project(dev: ModuleType) -> ModuleType:
    import dev_project

    return dev_project


@pytest.mark.parametrize(
    ("optional_paths", "ignore_revs_file"),
    [(True, ":(optional).git-blame-ignore-revs"), (False, ".git-blame-ignore-revs")],
)
def test_install_links_the_man_page_and_sets_up_git_blame(
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
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: optional_paths)

    dev.install([])

    assert commands[-1] == ("git", "config", "blame.ignoreRevsFile", ignore_revs_file)
    man_link = tmp_path / ".local" / "share" / "man" / "man1" / "git-stack.1"
    if sys.platform == "win32":
        assert not man_link.exists()
    else:
        assert man_link.readlink() == ROOT / "man" / "git-stack.1"


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


def commit_new(repo: Repo, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo.path / name).parent.mkdir(parents=True, exist_ok=True)
        repo.write(name, content)
    repo.git("add", *files)
    repo.git("commit", "--quiet", "-m", "add files")


def built_from(monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType, repo: Repo) -> list[str]:
    """Point dev_project at repo, with `man` recording the README each build reads and
    writing a page from it."""
    monkeypatch.setattr(dev_project, "ROOT", repo.path)
    monkeypatch.setattr(dev_project, "PAGE", repo.path / "man" / "git-stack.1")
    readmes: list[str] = []

    def man(args: list[str]) -> None:
        assert args[0] == "--readme"
        readmes.append(Path(args[1]).read_text(encoding="utf-8"))
        (repo.path / "man" / "git-stack.1").write_text(f"page of {readmes[-1]}", encoding="utf-8")

    monkeypatch.setattr(dev_project, "man", man)
    return readmes


def test_pre_commit_man_builds_the_page_from_the_staged_readme_and_stages_it(
    monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType, repo: Repo
) -> None:
    commit_new(repo, {"README.md": "old\n", "man/git-stack.1": "page of old\n"})
    repo.write("README.md", "staged\n")
    repo.git("add", "README.md")
    repo.write("README.md", "unstaged\n")
    readmes = built_from(monkeypatch, dev_project, repo)

    dev_project.pre_commit_man([])

    assert readmes == ["staged\n"]
    assert repo.git("show", ":man/git-stack.1") == "page of staged"


def test_pre_commit_man_does_nothing_when_the_page_s_inputs_are_not_staged(
    monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType, repo: Repo
) -> None:
    commit_new(repo, {"README.md": "old\n", "man/git-stack.1": "page of old\n"})
    repo.write("README.md", "unstaged\n")
    repo.write("a", "staged\n")
    repo.git("add", "a")
    readmes = built_from(monkeypatch, dev_project, repo)

    dev_project.pre_commit_man([])

    assert readmes == []
