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


def test_install_fails_when_the_skills_cant_be_linked(monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType) -> None:
    monkeypatch.setattr(dev_project, "run", lambda *args: None)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: True)
    monkeypatch.setattr(dev_project, "run_code", lambda *args: 1)

    with pytest.raises(SystemExit) as exited:
        dev_project.after_install()

    # --replace helps when a link is to another copy's skills, so that's said.
    assert str(exited.value.code) == (
        "install: couldn't link the agent skills; `git stack install-skills --replace` "
        "takes over a link to another copy's skills"
    )


def test_install_doesnt_suggest_replace_when_it_was_given(
    monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType
) -> None:
    monkeypatch.setattr(dev_project, "run", lambda *args: None)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: True)
    monkeypatch.setattr(dev_project, "run_code", lambda *args: 1)

    with pytest.raises(SystemExit) as exited:
        dev_project.after_install(replace=True)

    assert str(exited.value.code) == "install: couldn't link the agent skills"


@pytest.mark.parametrize(("replace", "flags"), [(False, ()), (True, ("--replace",))])
def test_install_passes_replace_to_install_skills(
    monkeypatch: pytest.MonkeyPatch, dev: ModuleType, dev_project: ModuleType, replace: bool, flags: tuple[str, ...]
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(dev_project, "run", lambda *args: None)
    monkeypatch.setattr(dev_project, "_git_has_optional_paths", lambda: True)
    monkeypatch.setattr(dev_project, "run_code", lambda *args: commands.append(args) or 0)

    dev_project.after_install(replace=replace)

    assert commands == [(dev.tool("git-stack"), "install-skills", *flags)]


def test_the_man_page_has_a_synopsis() -> None:
    """dev.py static only checks the page matches what it's made from, so losing MAN_SYNOPSIS
    from dev_project.py (which then makes a page without one) goes unnoticed."""
    assert ".SH SYNOPSIS" in (ROOT / "man" / "git-stack.1").read_text(encoding="utf-8")


@pytest.mark.parametrize("code", [0, 3])
def test_large_tests_runs_only_the_large_tests_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch, dev: ModuleType, dev_project: ModuleType, code: int
) -> None:
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(dev_project, "venv", lambda: None)
    monkeypatch.setattr(dev_project, "run_code", lambda *args: commands.append(args) or code)

    if code:
        with pytest.raises(SystemExit) as exited:
            dev_project.large_tests(["-k", "sync"])
        assert exited.value.code == code
    else:
        dev_project.large_tests(["-k", "sync"])

    assert commands == [(dev.tool("python"), "-m", "pytest", "-m", "large", "--numprocesses", "0", "-k", "sync")]
    assert "large-tests" in dev_project.COMMANDS


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


class FakeCommands:
    """What `release` runs: gh and git through subprocess.run (to ask whether the release exists, and
    for HEAD), and gh through dev.py's run (to make it)."""

    def __init__(self, *, released: bool = False, view_failure: str | None = None) -> None:
        self.released = released
        self.view_failure = view_failure
        self.asked: list[list[str]] = []
        self.ran: list[tuple[str, ...]] = []

    def subprocess_run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.asked.append(args)
        if args[:3] == ["gh", "release", "view"]:
            if self.view_failure is not None:
                return subprocess.CompletedProcess(args, 1, "", self.view_failure)
            return subprocess.CompletedProcess(
                args, 0 if self.released else 1, "", "" if self.released else "release not found"
            )
        assert args == ["git", "rev-parse", "HEAD"]
        return subprocess.CompletedProcess(args, 0, "abc123\n", "")

    def run(self, *args: str) -> None:
        self.ran.append(args)


@pytest.fixture
def release_commands(monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType, tmp_path: Path) -> FakeCommands:
    commands = FakeCommands()
    monkeypatch.setattr(dev_project.subprocess, "run", commands.subprocess_run)
    monkeypatch.setattr(dev_project, "run", commands.run)
    monkeypatch.setattr(dev_project, "VERSION_FILE", tmp_path / "version.txt")
    return commands


def write_version(dev_project: ModuleType, text: str) -> None:
    dev_project.VERSION_FILE.write_text(text, encoding="utf-8")


def test_release_makes_the_github_release_of_the_version_at_head_with_generated_notes(
    dev_project: ModuleType, release_commands: FakeCommands
) -> None:
    write_version(dev_project, "0.2.0\n")

    dev_project.release([])

    assert release_commands.ran == [
        ("gh", "release", "create", "v0.2.0", "--target", "abc123", "--title", "v0.2.0", "--generate-notes")
    ]
    assert "release" in dev_project.COMMANDS


def test_release_marks_a_version_with_a_suffix_as_a_prerelease(
    dev_project: ModuleType, release_commands: FakeCommands
) -> None:
    write_version(dev_project, "0.3.0rc1\n")

    dev_project.release([])

    assert release_commands.ran[0][-1] == "--prerelease"
    assert release_commands.ran[0][3] == "v0.3.0rc1"


def test_release_leaves_a_version_that_is_already_released_alone(
    dev_project: ModuleType, release_commands: FakeCommands, capsys: pytest.CaptureFixture[str]
) -> None:
    release_commands.released = True
    write_version(dev_project, "0.2.0\n")

    dev_project.release([])

    assert release_commands.ran == []
    assert "v0.2.0 is already released" in capsys.readouterr().out


@pytest.mark.parametrize(
    "text",
    [
        "v0.2.0\n",
        "0.2\n",
        "0.2.0 and more\n",
        "\n",
        "0.2.0\n0.3.0\n",
        "0.3.0-rc.1\n",
        "0.3.0-1\n",
        "0.3.0beta\n",
        "0.3.0rc\n",
    ],
)
def test_release_refuses_a_version_txt_that_isnt_just_a_version(
    dev_project: ModuleType, release_commands: FakeCommands, text: str
) -> None:
    write_version(dev_project, text)

    with pytest.raises(SystemExit, match="version.txt"):
        dev_project.release([])

    assert release_commands.asked == []
    assert release_commands.ran == []


def test_release_doesnt_take_a_failure_of_gh_for_a_version_that_isnt_released_yet(
    dev_project: ModuleType, release_commands: FakeCommands
) -> None:
    release_commands.view_failure = "HTTP 401: Bad credentials"
    write_version(dev_project, "0.2.0\n")

    with pytest.raises(SystemExit, match="Bad credentials"):
        dev_project.release([])

    assert release_commands.ran == []


def test_release_says_when_gh_isnt_installed(
    monkeypatch: pytest.MonkeyPatch, dev_project: ModuleType, release_commands: FakeCommands
) -> None:
    def missing(args: list[str], **kwargs: object) -> object:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(dev_project.subprocess, "run", missing)
    write_version(dev_project, "0.2.0\n")

    with pytest.raises(SystemExit, match="gh"):
        dev_project.release([])


def test_the_version_in_version_txt_is_one_release_takes(dev_project: ModuleType) -> None:
    assert dev_project.VERSION.fullmatch((ROOT / "version.txt").read_text("utf-8").strip())
