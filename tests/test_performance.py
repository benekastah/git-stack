"""The opt-in CLI timeline and the wall time it attributes to each tool."""

import json
import pathlib
import shlex

import pytest
from git_client.testing import GitCall

from git_stack.cli import build_parser
from git_stack.git import GitClient
from git_stack.performance import Timeline
from tests.helpers import FakeGitHub, Repo
from tests.test_integrate import source

pytestmark = pytest.mark.usefixtures("performance_home")


def test_cli_appends_the_command_and_stderr_report(repo: Repo, performance_home: pathlib.Path) -> None:
    repo.make_stack()
    path = performance_home / ".local" / "git-stack" / "perf.log"
    commands = [("info", "--offline", "-p"), ("--perf", "-C", "missing", "restack")]
    reports = [repo.stack(*commands[0]), repo.stack(*commands[1], expect_success=False)]
    entries = []
    for args, result in zip(commands, reports, strict=True):
        command = shlex.join(("git", "stack", "-C", str(repo.path), *args))
        timeline = result.stderr[result.stderr.index("Performance timeline\n") :]
        entries.append(f"{command}\n{timeline}\n")

    assert path.read_text(encoding="utf-8") == "".join(entries)
    repo.stack("info", "--offline")
    assert path.read_text(encoding="utf-8") == "".join(entries)


def test_cli_keeps_the_timeline_when_the_log_cannot_be_written(repo: Repo, performance_home: pathlib.Path) -> None:
    (performance_home / ".local").write_text("a file", encoding="utf-8")
    result = repo.stack("restack", "-p", expect_success=False)

    assert result.returncode == 1
    assert "Performance timeline\n" in result.stderr
    assert "couldn't write the performance log" in result.stderr


@pytest.mark.parametrize("flag", ["-p", "--perf", "--performance"])
@pytest.mark.parametrize(
    "command",
    ["{flag} info", "info {flag}", "{flag} group add team", "group {flag} add team", "group add team {flag}"],
)
def test_performance_aliases_at_each_parser_level(flag: str, command: str) -> None:
    args = build_parser().parse_args(command.format(flag=flag).split())
    assert args.performance is True


def test_timeline_uses_start_order_and_unions_concurrent_intervals() -> None:
    now = 0.0
    timeline = Timeline(clock=lambda: now)
    now = 1.0
    with timeline.record("git", "fetch"):
        now = 2.0
        with timeline.record("gh", "api", "graphql", "-f", "query=private query"):
            now = 3.0
            with timeline.record("git", "status"):
                now = 4.0
            now = 6.0
        now = 7.0
    now = 8.0
    with pytest.raises(RuntimeError, match="failed"), timeline.record("gh", "pr", "view"):
        now = 9.0
        raise RuntimeError("failed")
    now = 10.0

    assert timeline.render().splitlines() == [
        "Performance timeline",
        "  +1.000s   6.000s  git fetch",
        "  +2.000s   4.000s  gh api graphql -f 'query=<omitted>'",
        "  +3.000s   1.000s  git status",
        "  +8.000s   1.000s  gh pr view",
        "  Total       10.000s",
        "  Git          6.000s (60.0%)",
        "  gh           5.000s (50.0%)",
        "  Other        3.000s (30.0%)",
        "  Git and gh   4.000s (40.0% overlap)",
    ]


def test_timeline_without_subprocesses() -> None:
    clock = iter([5.0, 7.0])
    timeline = Timeline(clock=lambda: next(clock))

    assert timeline.render().splitlines() == [
        "Performance timeline",
        "  Total        2.000s",
        "  Git          0.000s (0.0%)",
        "  gh           0.000s (0.0%)",
        "  Other        2.000s (100.0%)",
    ]


def timed_commands(stderr: str) -> list[list[str]]:
    return [shlex.split(line.strip())[2:] for line in stderr.splitlines() if line.startswith("  +")]


def assert_git_processes_recorded(stderr: str, git_calls: list[GitCall]) -> None:
    calls = [call[1:] for call in timed_commands(stderr) if call[0] == "git" and call[1] != "cat-file"]
    assert sorted(calls) == sorted(call for call in git_calls if call[0] != "cat-file")


def test_info_records_workers_and_keeps_json_parseable(repo: Repo, gh: FakeGitHub, git_calls: list[GitCall]) -> None:
    repo.make_stack()
    repo.client.push()
    # Moving the root forces a branch-status worker to check ancestry and conflicts.
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main moved\n", "advance main")
    repo.git("switch", "--quiet", "pr3")
    git_calls.clear()
    result = repo.stack("info", "--json", "--performance")

    assert isinstance(json.loads(result.stdout), list)
    assert result.stderr.count("Performance timeline") == 1
    assert_git_processes_recorded(result.stderr, git_calls)
    calls = timed_commands(result.stderr)
    assert ["git", "cat-file", "--batch-command", "request"] in calls
    assert ["git", "cat-file", "--batch-command", "shutdown"] in calls
    assert any(call[:3] == ["gh", "api", "graphql"] for call in calls)
    assert any(call[:2] == ["git", "merge-tree"] for call in calls)
    assert "query=<omitted>" in result.stderr
    assert "Performance timeline" not in repo.stack("info", "--offline").stderr


def test_prs_records_github_stack_post(repo: Repo, gh: FakeGitHub) -> None:
    gh.enable_stacks()
    repo.make_stack()
    repo.client.push()
    result = repo.stack("prs", "--perf", "--json")

    assert json.loads(result.stdout.splitlines()[-1])["type"] == "result"
    posts = [call for call in timed_commands(result.stderr) if call[:2] == ["gh", "api"] and "POST" in call]
    assert any(call[-2:] == ["--input", "-"] for call in posts)
    assert "pull_requests" not in result.stderr
    assert gh.stacks() == [[1, 2, 3]]


def test_integrate_records_linked_worktree_calls(repo: Repo, git_calls: list[GitCall]) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    git_calls.clear()
    result = repo.stack("integrate", "-p", "combined", "one", "two")

    assert_git_processes_recorded(result.stderr, git_calls)
    assert any(call[:3] == ["git", "merge", "--no-ff"] for call in timed_commands(result.stderr))


def test_timeline_follows_command_errors_and_includes_failed_calls(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.client.push()
    gh.fail_opening("pr1")
    result = repo.stack("prs", "-p", "--json", expect_success=False)

    assert result.returncode == 1
    assert json.loads(result.stdout.splitlines()[-1])["type"] == "error"
    assert result.stderr.index("git stack:") < result.stderr.index("Performance timeline")
    assert any(call[:3] == ["gh", "pr", "create"] for call in timed_commands(result.stderr))


def test_failed_git_call_keeps_the_command_error(repo: Repo) -> None:
    result = repo.stack("-C", "missing", "restack", "-p", "--json", expect_success=False)

    assert result.returncode == 1
    assert json.loads(result.stdout)["type"] == "error"
    assert result.stderr.index("git stack:") < result.stderr.index("Performance timeline")
    assert any(call[:2] == ["git", "rev-parse"] for call in timed_commands(result.stderr))


def test_batch_clock_excludes_idle_time(repo: Repo) -> None:
    now = 0.0
    timeline = Timeline(clock=lambda: now)
    git = GitClient(repo.path)
    with timeline.activate(), git.snapshot():
        now = 1.0
        assert git.rev("HEAD")
        now = 8.0
        assert git.rev("HEAD")
        now = 9.0
    now = 10.0

    assert [line.split()[0] for line in timeline.render().splitlines() if line.startswith("  +")] == [
        "+1.000s",
        "+8.000s",
        "+9.000s",
    ]
    assert "Git          0.000s (0.0%)" in timeline.render()
