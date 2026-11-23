"""The CLI performance log and the wall time it attributes to each tool."""

import json
import os
import pathlib
import shlex
import sys

import git_client.performance as git_performance
import pytest
from git_client import CatFile, Git, PerformanceEvent, PerformanceRecorder

from git_stack import _version, performance
from git_stack.cli import build_parser
from git_stack.git import GitClient
from git_stack.performance import Timeline
from tests.conftest import ROOT
from tests.git_support import GitCall, real_git
from tests.helpers import FakeGitHub, Repo
from tests.test_integrate import source


@pytest.mark.parametrize("commit", [None, "a" * 40])
def test_build_version_includes_git_install_commit_when_available(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, commit: str | None
) -> None:
    # A wheel installation has no source checkout next to the package.
    monkeypatch.setattr(_version, "__file__", str(tmp_path / "git_stack" / "_version.py"))

    class InstalledPackage:
        def read_text(self, filename: str) -> str | None:
            assert filename == "direct_url.json"
            if commit is None:
                return None
            return json.dumps({"vcs_info": {"vcs": "git", "commit_id": commit}})

    monkeypatch.setattr(_version, "distribution", lambda _: InstalledPackage())
    expected = _version.__version__ if commit is None else f"{_version.__version__} (git {commit})"
    assert _version.build_version() == expected


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
    successful = repo.stack("info")
    repo.git("switch", "--quiet", "main")
    failed = repo.stack("restack", expect_success=False)

    assert successful.stderr == ""
    assert "Performance timeline" not in failed.stderr
    revision = repo.git("rev-parse", "HEAD", cwd=ROOT)
    version = f"{_version.__version__} (git {revision})"
    first, second = performance_log.read_text(encoding="utf-8").strip().split("\n\n")
    for entry, args in ((first, ("info",)), (second, ("restack",))):
        command, _, report = entry.partition("\n")
        assert shlex.split(command) == ["git", "stack", "-C", str(repo.path), *args]
        assert report.startswith(f"Performance timeline\n  Version    {version}\n")
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


def test_truncated_report_preserves_version_and_record_boundary(performance_log: pathlib.Path) -> None:
    report_header = "Performance timeline\n  Version    0.1.0\n"
    header = f"git stack info\n{report_header}".encode()
    limit = len(header) + len(b"[performance report truncated]\n\n") + 1
    performance.append_performance_log(performance_log, ["info"], report_header + "é" * 100, max_bytes=limit)
    assert performance_log.read_bytes() == header + b"[performance report truncated]\n\n"


def test_log_can_rotate_a_legacy_truncation_after_a_line_boundary(performance_log: pathlib.Path) -> None:
    performance_log.parent.mkdir()
    old = b"git stack old\nPerformance timeline\n\n[performance report truncated]\n\n"
    performance_log.write_bytes(old)

    performance.append_performance_log(performance_log, ["new"], "Performance timeline", max_bytes=len(old))

    assert performance_log.read_bytes() == b"git stack new\nPerformance timeline\n\n"


def test_log_accepts_a_legacy_standalone_truncation_before_a_report(performance_log: pathlib.Path) -> None:
    performance_log.parent.mkdir()
    old = b"[performance report truncated]\n\ngit stack old\nPerformance timeline\n\n"
    performance_log.write_bytes(old)

    performance.append_performance_log(performance_log, ["new"], "Performance timeline")

    assert performance_log.read_bytes() == old + b"git stack new\nPerformance timeline\n\n"


def test_log_preserves_unrecognized_data_between_reports(performance_log: pathlib.Path) -> None:
    performance_log.parent.mkdir()
    old = b"git stack old\nPerformance timeline\n\nunrecognized\n\ngit stack other\nPerformance timeline\n\n"
    performance_log.write_bytes(old)

    with pytest.raises(OSError, match="not a performance log"):
        performance.append_performance_log(performance_log, ["new"], "Performance timeline")

    assert performance_log.read_bytes() == old


def test_log_can_replace_a_legacy_truncated_command(performance_log: pathlib.Path) -> None:
    performance_log.parent.mkdir()
    performance_log.write_bytes(b"git sta\n[performance report truncated]\n\n")

    report = "Performance timeline\n  Total  0.000s"
    expected = f"git stack info\n{report}\n\n".encode()
    performance.append_performance_log(performance_log, ["info"], report, max_bytes=len(expected))

    assert performance_log.read_bytes() == expected


@pytest.mark.parametrize("first_line", [b"[unrelated config]\n", b"[performance report truncated]\n\n"])
def test_perf_log_does_not_change_an_existing_unrelated_file(
    repo: Repo, performance_log: pathlib.Path, first_line: bytes
) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", str(performance_log))
    repo.git("config", "--local", "stack.perfLogMaxBytes", "100")
    performance_log.parent.mkdir()
    contents = first_line + b"unrelated content\n" * 20
    performance_log.write_bytes(contents)

    result = repo.stack("parent")

    assert result.stdout == "pr2\n"
    assert "couldn't write the performance log" in result.stderr
    assert performance_log.read_bytes() == contents


def test_log_path_cannot_leave_git_config_locked(repo: Repo) -> None:
    repo.make_stack()
    repo.git("config", "--local", "stack.perfLog", ".git/config")

    assert "couldn't write the performance log" in repo.stack("parent").stderr
    repo.git("config", "--local", "--unset", "stack.perfLog")


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


@pytest.mark.skipif(sys.platform == "win32", reason="creating a file symlink requires privileges on Windows")
def test_perf_log_does_not_create_a_dangling_symlink_target(
    repo: Repo, performance_log: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    # Exercise the fallback used where Git Stack cannot ask the OS for O_NOFOLLOW.
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    repo.git("config", "--local", "stack.perfLog", str(performance_log))
    target = repo.tmp / "missing.log"
    performance_log.parent.mkdir()
    performance_log.symlink_to(target)

    result = repo.stack("info")

    assert "couldn't write the performance log" in result.stderr
    assert not target.exists()


@pytest.mark.parametrize("flag", ["-p", "--perf", "--performance"])
def test_old_performance_flags_are_rejected(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([flag, "info"])
    assert error.value.code == 2


def test_timeline_uses_start_order_and_unions_concurrent_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    timeline = Timeline(clock=lambda: now)
    monkeypatch.setattr(
        PerformanceRecorder,
        "events",
        (
            PerformanceEvent("process", "git", 1.0, 7.0, ("fetch",), pid=1),
            PerformanceEvent("process", "git", 3.0, 4.0, ("status",), pid=2),
            PerformanceEvent("process", "git", 4.0, 4.0, ("failed-to-launch",)),
        ),
    )
    now = 2.0
    with timeline.record("gh", "api", "graphql", "-f", "query=private query"):
        now = 6.0
    now = 7.25
    with timeline.record("native", "resolve"):
        now = 7.75
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
        "  +7.250s   0.500s  native resolve",
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
    calls = [call[1:] for call in timed_commands(report) if call[0] == "git"]
    # The initial config read decides whether to start the recorder.
    assert git_calls[0][:2] == ["config", "--list"]
    assert sorted(calls) == sorted(git_calls[1:])


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
    result = repo.stack("info", "--prs", "--json")
    report = performance_log.read_text(encoding="utf-8")

    assert isinstance(json.loads(result.stdout), list)
    assert report.count("Performance timeline") == 1
    assert_git_processes_recorded(report, git_calls)
    calls = timed_commands(report)
    assert any(call[:3] == ["gh", "api", "graphql"] for call in calls)
    assert any(call[:2] == ["git", "merge-tree"] for call in calls)
    assert "query=<omitted>" in report
    assert "Performance timeline" not in repo.stack("info").stderr


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


def test_native_reads_are_timed_without_counting_cache_hits(repo: Repo) -> None:
    tip = repo.git("rev-parse", "HEAD")
    timeline = Timeline()
    with timeline.activate():
        git = GitClient(repo.path)
        with git.snapshot():
            assert git.rev(tip) == tip
            assert git.rev(tip) == tip

    calls = timed_commands(timeline.render())
    assert calls.count(["native", "resolve"]) == 1
    assert all(call[0] != "git" for call in calls)


def test_cat_file_counts_a_single_launch_and_excludes_idle_time(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 0.0
    monkeypatch.setattr(git_performance, "monotonic", lambda: now)
    timeline = Timeline(clock=lambda: now)
    git = Git(repo.path, program=real_git(), performance=timeline.git)
    now = 1.0
    with CatFile(git) as batch:
        assert batch.resolve("HEAD")
        now = 8.0
        assert batch.resolve("HEAD^{tree}")
    now = 10.0

    report = timeline.render()
    assert [call for call in timed_commands(report) if call[0] == "git"] == [["git", "cat-file", "--batch-command"]]
    assert [call for call in timed_commands(report) if call[0] == "cat-file"] == [
        ["cat-file", "request"],
        ["cat-file", "request"],
        ["cat-file", "shutdown"],
    ]
    assert "  Git          0.000s (0.0%)" in report
    assert "  Other       10.000s (100.0%)" in report


def test_cat_file_work_counts_toward_git_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 8.0])
    timeline = Timeline(clock=lambda: next(clock))
    monkeypatch.setattr(
        PerformanceRecorder,
        "events",
        (
            PerformanceEvent("process", "git", 1.0, 8.0, ("cat-file", "--batch-command"), pid=1, persistent=True),
            PerformanceEvent("process_work", "CatFile.request", 1.0, 2.0),
            PerformanceEvent("process_work", "CatFile.request", 5.0, 7.0),
            PerformanceEvent("process_work", "CatFile.shutdown", 7.0, 8.0),
        ),
    )

    report = timeline.render()
    assert "  Git          4.000s (50.0%)" in report
    assert "  Other        4.000s (50.0%)" in report
