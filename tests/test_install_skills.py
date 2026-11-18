"""`git stack install-skills` links the packaged agent skills into each agent's skills directory."""

import json
import pathlib
import sys

import pytest

from git_stack.cli import main
from tests.test_skills import SKILLS

SKILL_NAMES = sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


@pytest.fixture
def home(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A home directory with no agent in it, and no agent's variables set."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def test_it_links_every_skill_for_each_agent_it_finds(home: pathlib.Path) -> None:
    (home / ".claude").mkdir()
    (home / ".codex").mkdir()

    assert main(["install-skills"]) == 0

    for skills in (home / ".claude" / "skills", home / ".agents" / "skills"):
        for name in SKILL_NAMES:
            assert (skills / name).resolve() == (SKILLS / name).resolve()
            assert (skills / name / "SKILL.md").is_file()


def test_it_leaves_an_agent_it_doesnt_find_alone(home: pathlib.Path) -> None:
    (home / ".claude").mkdir()

    main(["install-skills"])

    assert not (home / ".agents").exists()


def test_a_named_agent_is_installed_even_if_not_found(home: pathlib.Path) -> None:
    assert main(["install-skills", "--codex"]) == 0

    assert (home / ".agents" / "skills" / "git-stack").is_symlink()
    assert not (home / ".claude").exists()


def test_it_says_so_and_succeeds_when_it_finds_no_agent(home: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install-skills"]) == 0

    assert "--claude" in capsys.readouterr().out
    assert not any(home.iterdir())


def test_json_lists_nothing_when_it_finds_no_agent(home: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install-skills", "--json"]) == 0

    assert json.loads(capsys.readouterr().out)["installed"] == []


def test_claude_config_dir_says_where_claude_keeps_its_skills(
    home: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert main(["install-skills", "--claude"]) == 0

    assert (config / "skills" / "git-stack").is_symlink()
    assert not (home / ".claude").exists()


def test_a_second_run_changes_nothing(home: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["install-skills", "--claude"])
    capsys.readouterr()

    assert main(["install-skills", "--claude", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert {i["action"] for i in result["installed"]} == {"unchanged"}


def link_to_elsewhere(home: pathlib.Path, tmp_path: pathlib.Path, skill: str = "git-stack") -> pathlib.Path:
    """A link where Claude looks for skill, to a directory that isn't git-stack's."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    link = home / ".claude" / "skills" / skill
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(elsewhere, target_is_directory=True)
    return link


def test_it_leaves_a_link_to_somewhere_else_and_changes_nothing(
    home: pathlib.Path, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = link_to_elsewhere(home, tmp_path, "git-stack-sync")

    assert main(["install-skills", "--claude"]) == 1

    assert "--replace" in capsys.readouterr().err
    assert link.resolve() == (tmp_path / "elsewhere").resolve()
    assert [p.name for p in link.parent.iterdir()] == ["git-stack-sync"]


def test_replace_replaces_a_link_to_somewhere_else(home: pathlib.Path, tmp_path: pathlib.Path) -> None:
    link = link_to_elsewhere(home, tmp_path)

    assert main(["install-skills", "--claude", "--replace"]) == 0

    assert link.resolve() == (SKILLS / "git-stack").resolve()


def test_it_links_a_skill_once_where_two_agents_share_a_skills_directory(
    home: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = home / ".agents" / "skills"
    shared.mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / ".claude" / "skills").symlink_to(shared, target_is_directory=True)

    assert main(["install-skills", "--claude", "--codex", "--json"]) == 0

    installed = json.loads(capsys.readouterr().out)["installed"]
    assert [(i["agent"], i["action"]) for i in installed if i["skill"] == "git-stack"] == [
        ("claude", "linked"),
        ("codex", "unchanged"),
    ]
    assert (shared / "git-stack").resolve() == (SKILLS / "git-stack").resolve()


def test_a_refusal_in_the_second_agents_directory_links_nothing_for_the_first(
    home: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "git-stack-sync").symlink_to(elsewhere, target_is_directory=True)

    assert main(["install-skills", "--claude", "--codex"]) == 1

    assert not (home / ".claude").exists()


def test_it_replaces_a_link_whose_target_is_gone(home: pathlib.Path, tmp_path: pathlib.Path) -> None:
    link = link_to_elsewhere(home, tmp_path)
    (tmp_path / "elsewhere").rmdir()

    assert main(["install-skills", "--claude"]) == 0

    assert link.resolve() == (SKILLS / "git-stack").resolve()


def test_it_refuses_to_replace_a_directory_of_your_own(home: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    mine = home / ".claude" / "skills" / "git-stack"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")

    assert main(["install-skills", "--claude", "--replace"]) == 1

    assert "git-stack" in capsys.readouterr().err
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_json_lists_what_was_installed(home: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["install-skills", "--codex", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["installed"] == [
        {
            "agent": "codex",
            "skill": name,
            "path": str(home / ".agents" / "skills" / name),
            "action": "linked",
        }
        for name in SKILL_NAMES
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is how Windows links without symlink rights")
def test_on_windows_it_falls_back_to_a_junction_when_symlinks_arent_allowed(
    home: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("a required privilege is not held by the client")

    monkeypatch.setattr("os.symlink", refuse)

    assert main(["install-skills", "--claude"]) == 0

    link = home / ".claude" / "skills" / "git-stack"
    assert link.resolve() == (SKILLS / "git-stack").resolve()
    assert (link / "SKILL.md").is_file()
