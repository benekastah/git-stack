"""The agent skills in `skills/` stay true to the command line they describe."""

import pathlib
import re
import shlex

import pytest

from git_stack.cli import build_parser
from tests.conftest import ROOT

SKILLS = ROOT / "git_stack" / "skills"
SKILL_FILES = sorted(SKILLS.glob("*/*.md"))

CODE_SPAN = re.compile(r"`([^`\n]+)`")
OPTIONAL = re.compile(r"\[[^\]]*\]")
PLACEHOLDER = re.compile(r"<[^>]*>")
ALTERNATIVES = re.compile(r"(\w+)(?:\|\w+)+")
SHELL_JOINERS = re.compile(r"\s(?:&&|\|\||;|\|)\s")


def stack_commands(text: str) -> list[str]:
    """The `git stack ...` command lines in text's code spans and fenced blocks,
    each as the arguments git-stack would get: placeholders filled in, optional
    parts dropped, the first of a|b|c taken, and a trailing comment cut."""
    lines = [m.group(1) for m in CODE_SPAN.finditer(text)]
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            lines.append(line.strip())
    commands: list[str] = []
    for line in lines:
        for part in SHELL_JOINERS.split(line.split(" #")[0]):
            if not part.startswith("git stack "):
                continue
            part = OPTIONAL.sub("", part)
            part = PLACEHOLDER.sub("x", part).replace("...", "")
            part = ALTERNATIVES.sub(r"\1", part)
            if command := " ".join(part.removeprefix("git stack ").split()):
                commands.append(command)
    return commands


def test_the_core_skill_exists() -> None:
    assert (SKILLS / "git-stack" / "SKILL.md").is_file()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_git_stack_command_in_a_skill_parses(path: pathlib.Path) -> None:
    parser = build_parser()
    commands = stack_commands(path.read_text(encoding="utf-8"))
    assert commands or path.name != "SKILL.md"
    for command in commands:
        try:
            args = parser.parse_args(shlex.split(command))
        except SystemExit as e:
            if e.code == 0:  # --help
                continue
            pytest.fail(f"`git stack {command}` in {path.name} isn't a git-stack command line")
        assert args.performance and "--performance" in shlex.split(command)


@pytest.mark.parametrize("path", sorted(SKILLS.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_a_skill_names_itself_and_says_when_to_use_it(path: pathlib.Path) -> None:
    head = path.read_text(encoding="utf-8").split("---")[1]
    fields = dict(line.split(": ", 1) for line in head.strip().splitlines() if ": " in line)
    assert fields["name"] == path.parent.name
    assert fields["description"].strip()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
@pytest.mark.parametrize("hangs", ["rebase -i", "rebase --interactive", "git stack --help"])
def test_a_skill_doesnt_tell_an_agent_to_run_what_waits_for_a_person(path: pathlib.Path, hangs: str) -> None:
    """These open an editor or a pager, which an agent can't use. (`git stack <command> --help` is fine.)"""
    assert hangs not in path.read_text(encoding="utf-8")


def test_stack_commands_reads_what_a_skill_writes() -> None:
    text = "Run `git stack sync [-l] <branch>` then:\n```sh\ngit stack push --prs  # go\nmake test && git stack info\n```\n"
    assert stack_commands(text) == ["sync x", "push --prs", "info"]


def test_stack_commands_reads_a_fenced_block_indented_in_a_list_item() -> None:
    text = "- step:\n  ```sh\n  git stack track x y --base z\n  ```\n"
    assert stack_commands(text) == ["track x y --base z"]
