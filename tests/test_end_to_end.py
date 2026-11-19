"""git stack run as a separate process, the way users run it, to check what
the mocked CLI tests can't: the entry point, and printing what StackClient
reports."""

import json
import pathlib
import subprocess
import sys

from tests.helpers import FakeGitHub, Repo


def test_stack_through_squash_merge(repo: Repo) -> None:
    repo.stack_process("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.stack_process("branch", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.stack_process("push")
    repo.squash_merge_on_forge("pr1")

    result = repo.stack_process("pull", "-d", "local")

    assert "pr1 was merged into main; deleted it" in result.stdout
    assert "restacking pr2 onto main" in result.stdout
    assert result.stdout.endswith("pull requests of:\n  pr2\n")
    assert repo.stack_process("info").stdout == "main\n└── pr2 *\n"


def test_non_ascii_branch_names_and_messages(repo: Repo) -> None:
    # Decoded and printed as UTF-8 whatever the locale, e.g. on Windows, where
    # it's usually not.
    repo.stack_process("branch", "née")
    repo.commit("a", "née\n", "café ☕")
    repo.stack_process("push")
    repo.commit_on_forge("née", "b", "naïve\n", "naïve fix ✓")
    repo.stack_process("branch", "über")

    repo.git("switch", "--quiet", "née")
    repo.stack_process("pull")
    repo.git("switch", "--quiet", "über")

    assert repo.git("log", "--format=%s", "-2", "née").splitlines() == ["naïve fix ✓", "café ☕"]
    assert repo.stack_process("info").stdout == "main\n└── née\n    └── über *\n"
    assert repo.stack_process("parent").stdout == "née\n"


def test_files_that_arent_utf_8(repo: Repo) -> None:
    # Checking for a squash merge compares the branch's diff, which has the
    # file's Latin-1 bytes in it, with main's.
    repo.stack_process("branch", "pr1")
    (repo.path / "a").write_bytes(b"caf\xe9\n")
    repo.git("commit", "--quiet", "-am", "Latin-1")
    repo.stack_process("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    repo.git("switch", "--quiet", "main")

    repo.stack_process("clean")

    repo.assert_untracked("pr1")


def test_warnings_go_to_stderr(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    result = repo.stack_process("restack")

    assert "warning: pr1 looks merged into main" in result.stderr
    assert "looks merged" not in result.stdout


def test_failure_exits_nonzero_with_message(repo: Repo) -> None:
    result = repo.stack_process("restack", expect_success=False)

    assert result.returncode == 1
    assert result.stderr == "git stack: main isn't in a stack; switch to a stacked branch to restack its stack\n"


def test_performance_timeline_follows_stdout_when_streams_are_combined(repo: Repo, tmp_path: pathlib.Path) -> None:
    repo.make_stack()
    # Keep the subprocess's performance log in the test's own directory.
    run_git_stack = (
        "import pathlib, runpy, sys\n"
        "home = pathlib.Path(sys.argv.pop(1))\n"
        "pathlib.Path.home = lambda: home\n"
        "runpy.run_module('git_stack', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", run_git_stack, str(tmp_path), "-C", str(repo.path), "parent", "--performance"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        check=True,
    )
    assert result.stdout.startswith("pr2\nPerformance timeline\n")
    assert (tmp_path / ".local" / "git-stack" / "perf.log").is_file()


def test_push_says_to_open_pull_requests_with_prs(repo: Repo, gh: FakeGitHub) -> None:
    repo.stack_process("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")

    result = repo.stack_process("push")

    assert "pr1 has no pull request; open drafts with `git stack prs`" in result.stdout


def test_each_directory_is_relative_to_the_one_before(repo: Repo) -> None:
    (repo.tmp / "elsewhere").mkdir()
    repo.git("switch", "--quiet", "-c", "pr1")

    result = subprocess.run(
        [sys.executable, "-m", "git_stack", "-C", "elsewhere", "-C", "../local", "track", "main"],
        cwd=repo.tmp,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert repo.config("pr1", "stackParent") == "main"


def test_show_stack_quiet_answers_with_exit_status(repo: Repo) -> None:
    repo.make_stack()

    assert repo.stack_process("show-stack", "--quiet").stdout == ""
    repo.git("switch", "--quiet", "main")
    result = repo.stack_process("show-stack", "--quiet", expect_success=False)
    assert (result.returncode, result.stdout, result.stderr) == (1, "", "")


def test_clean_json_prints_only_json_on_stdout(repo: Repo) -> None:
    repo.make_stack()
    repo.stack_process("push")
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "main")

    result = repo.stack_process("clean", "-d", "local", "--json")

    *events, last = [json.loads(line) for line in result.stdout.splitlines()]
    untracked = [e for e in events if e["type"] == "untracked"]
    assert [(e["branch"], e["children"], e["deleted_tip"] is not None) for e in untracked] == [("pr1", ["pr2"], True)]
    assert last["type"] == "result"
    assert [b["name"] for b in last["stacks"]] == ["main", "pr2", "pr3"]


def test_clean_json_reports_what_stopped_it(repo: Repo) -> None:
    repo.make_stack()
    (repo.path / "a").write_text("uncommitted\n")

    result = repo.stack_process("clean", "-d", "local", "--json", expect_success=False)

    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "type": "error",
        "message": "you have uncommitted changes; commit or stash them first",
        "events": [],
    }
    assert result.stderr == "git stack: you have uncommitted changes; commit or stash them first\n"


def test_sync_json_prints_only_json_on_stdout(repo: Repo) -> None:
    repo.make_stack()
    repo.stack_process("push")
    repo.squash_merge_on_forge("pr1")

    result = repo.stack_process("sync", "-d", "local", "--json")

    *events, last = [json.loads(line) for line in result.stdout.splitlines()]
    types = [e["type"] for e in events]
    assert "untracked" in types and "restacking" in types
    assert last["unpushed"] == [] and last["events"] == events
    assert [b["name"] for b in last["stacks"]] == ["main", "pr2", "pr3"]
