"""The CLI performance log and the wall time it attributes to each tool."""

import json
import pathlib
import shlex
import sys

import pytest
from git_client.testing import GitCall

from git_stack import performance
from git_stack.cli import build_parser
from git_stack.git import GitClient
from git_stack.performance import Timeline
from tests.helpers import FakeGitHub, Repo
from tests.test_integrate import source


def test_performance_is_off_by_default(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    assert repo.stack("parent").stdout == "pr2\n"
    assert not performance_log.exists()


@pytest.mark.usefixtures("performance_enabled")
def test_local_config_can_disable_global_performance_logging(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", "")
    assert repo.stack("parent").stdout == "pr2\n"
    assert not performance_log.exists()


def test_local_config_enables_reporting_and_controls_the_cap(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", str(performance_log))
    repo.git("config", "--local", "stack.perfLogMaxBytes", "1k")
    performance.append_performance_log(performance_log, ["old"], "Performance timeline\n" + "old\n" * 400)

    assert repo.stack("parent").stdout == "pr2\n"

    assert performance_log.stat().st_size <= 1024
    assert "Performance timeline\n" in performance_log.read_text(encoding="utf-8")


@pytest.mark.usefixtures("performance_enabled")
def test_cli_appends_a_command_and_report_for_success_and_failure(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    successful = repo.stack("info", "--offline")
    repo.git("switch", "--quiet", "main")
    failed = repo.stack("restack", expect_success=False)

    assert successful.stderr == ""
    assert "Performance timeline" not in failed.stderr
    first, second = performance_log.read_text(encoding="utf-8").strip().split("\n\n")
    for entry, args in ((first, ("info", "--offline")), (second, ("restack",))):
        command, _, report = entry.partition("\n")
        assert shlex.split(command) == ["git", "stack", "-C", str(repo.path), *args]
        assert report.startswith("Performance timeline\n")
        assert "Total" in report


@pytest.mark.usefixtures("performance_enabled")
def test_cli_preserves_command_error_if_log_cannot_be_written(repo: Repo, performance_log: pathlib.Path) -> None:
    performance_log.parent.write_text("a file", encoding="utf-8")
    result = repo.stack("restack", expect_success=False)

    assert result.returncode == 1
    assert "Performance timeline" not in result.stderr
    assert "couldn't write the performance log" in result.stderr


def test_quiet_show_stack_stays_silent_if_log_cannot_be_written(repo: Repo, performance_log: pathlib.Path) -> None:
    performance_log.mkdir(parents=True)
    repo.git("config", "--local", "stack.perfLog", str(performance_log))

    result = repo.stack("show-stack", "--quiet", expect_success=False)

    assert (result.returncode, result.stdout, result.stderr) == (1, "", "")


def test_relative_log_path_is_relative_to_the_command_directory(repo: Repo) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", "state/log.txt")
    repo.stack("parent")

    path = repo.path / "state" / "log.txt"
    assert "Performance timeline\n" in path.read_text(encoding="utf-8")


def test_filesystem_root_is_rejected_as_a_log_path(repo: Repo) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", repo.path.anchor)

    result = repo.stack("parent")

    assert result.stdout == "pr2\n"
    assert "couldn't write the performance log" in result.stderr


def test_log_cap_retains_newest_whole_reports(performance_log: pathlib.Path) -> None:
    report = "Performance timeline\n" + "é" * 6
    for command in ("old", "middle", "recent"):
        performance.append_performance_log(performance_log, [command], report, max_bytes=110)

    assert performance_log.stat().st_size <= 110
    assert performance_log.read_text(encoding="utf-8").splitlines() == [
        "git stack middle",
        "Performance timeline",
        "é" * 6,
        "",
        "git stack recent",
        "Performance timeline",
        "é" * 6,
        "",
    ]


def test_oversized_report_respects_cap(performance_log: pathlib.Path) -> None:
    performance.append_performance_log(performance_log, ["info"], "Performance timeline\n" + "é" * 100, max_bytes=100)

    assert performance_log.stat().st_size <= 100
    assert "truncated" in performance_log.read_text(encoding="utf-8")


def test_perf_log_does_not_change_an_existing_unrelated_file(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", str(performance_log))
    repo.git("config", "--local", "stack.perfLogMaxBytes", "100")
    performance_log.parent.mkdir()
    contents = b"[unrelated config]\n" * 20
    performance_log.write_bytes(contents)

    result = repo.stack("parent")

    assert result.stdout == "pr2\n"
    assert "couldn't write the performance log" in result.stderr
    assert performance_log.read_bytes() == contents


@pytest.mark.skipif(sys.platform == "win32", reason="creating a file symlink requires privileges on Windows")
def test_perf_log_does_not_follow_a_file_symlink(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", str(performance_log))
    target = repo.tmp / "other.log"
    contents = b"git stack info\nPerformance timeline\n\n"
    target.write_bytes(contents)
    performance_log.parent.mkdir()
    performance_log.symlink_to(target)

    result = repo.stack("parent")

    assert "couldn't write the performance log" in result.stderr
    assert target.read_bytes() == contents


@pytest.mark.parametrize("flag", ["-p", "--perf", "--performance"])
def test_old_performance_flags_are_rejected(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([flag, "info"])
    assert error.value.code == 2


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


def timed_commands(report: str) -> list[list[str]]:
    return [shlex.split(line.strip())[2:] for line in report.splitlines() if line.startswith("  +")]


def assert_git_processes_recorded(report: str, git_calls: list[GitCall]) -> None:
    calls = [call[1:] for call in timed_commands(report) if call[0] == "git" and call[1] != "cat-file"]
    # The initial config read decides whether to start the recorder.
    assert git_calls[0][:2] == ["config", "--list"]
    assert sorted(calls) == sorted(call for call in git_calls[1:] if call[0] != "cat-file")


@pytest.mark.usefixtures("performance_enabled")
def test_info_records_workers_and_keeps_json_parseable(
    repo: Repo, gh: FakeGitHub, git_calls: list[GitCall], performance_log: pathlib.Path
) -> None:
    repo.make_stack()
    repo.client.push()
    # Moving the root forces a branch-status worker to check ancestry and conflicts.
    repo.git("switch", "--quiet", "main")
    repo.commit("c", "main moved\n", "advance main")
    repo.git("switch", "--quiet", "pr3")
    git_calls.clear()
    result = repo.stack("info", "--json")
    report = performance_log.read_text(encoding="utf-8")

    assert isinstance(json.loads(result.stdout), list)
    assert report.count("Performance timeline") == 1
    assert_git_processes_recorded(report, git_calls)
    calls = timed_commands(report)
    assert ["git", "cat-file", "--batch-command", "request"] in calls
    assert ["git", "cat-file", "--batch-command", "shutdown"] in calls
    assert any(call[:3] == ["gh", "api", "graphql"] for call in calls)
    assert any(call[:2] == ["git", "merge-tree"] for call in calls)
    assert "query=<omitted>" in report
    assert "Performance timeline" not in repo.stack("info", "--offline").stderr


@pytest.mark.usefixtures("performance_enabled")
def test_prs_records_github_stack_post(repo: Repo, gh: FakeGitHub, performance_log: pathlib.Path) -> None:
    gh.enable_stacks()
    repo.make_stack()
    repo.client.push()
    result = repo.stack("prs", "--json")
    report = performance_log.read_text(encoding="utf-8")

    assert json.loads(result.stdout.splitlines()[-1])["type"] == "result"
    posts = [call for call in timed_commands(report) if call[:2] == ["gh", "api"] and "POST" in call]
    assert any(call[-2:] == ["--input", "-"] for call in posts)
    assert "pull_requests" not in report
    assert gh.stacks() == [[1, 2, 3]]


@pytest.mark.usefixtures("performance_enabled")
def test_integrate_records_linked_worktree_calls(
    repo: Repo, git_calls: list[GitCall], performance_log: pathlib.Path
) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    git_calls.clear()
    repo.stack("integrate", "combined", "one", "two")
    report = performance_log.read_text(encoding="utf-8")

    assert_git_processes_recorded(report, git_calls)
    assert any(call[:3] == ["git", "merge", "--no-ff"] for call in timed_commands(report))


@pytest.mark.usefixtures("performance_enabled")
def test_timeline_records_failed_gh_calls(repo: Repo, gh: FakeGitHub, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.client.push()
    gh.fail_opening("pr1")
    result = repo.stack("prs", "--json", expect_success=False)
    report = performance_log.read_text(encoding="utf-8")

    assert result.returncode == 1
    assert json.loads(result.stdout.splitlines()[-1])["type"] == "error"
    assert "git stack:" in result.stderr and "Performance timeline" not in result.stderr
    assert any(call[:3] == ["gh", "pr", "create"] for call in timed_commands(report))


@pytest.mark.usefixtures("performance_enabled")
def test_failed_git_call_keeps_the_command_error(repo: Repo, performance_log: pathlib.Path) -> None:
    repo.make_stack()
    repo.add_hook(repo.path, "pre-push", "#!/bin/sh\nexit 1\n")
    result = repo.stack("push", "--json", expect_success=False)
    report = performance_log.read_text(encoding="utf-8")

    assert result.returncode == 1
    assert json.loads(result.stdout)["type"] == "error"
    assert "git stack:" in result.stderr and "Performance timeline" not in result.stderr
    assert any(call[:2] == ["git", "push"] for call in timed_commands(report))


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
