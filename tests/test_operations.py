"""tests/operations.py: declaring how many git and gh operations a test (or a block of it) runs."""

import os
import subprocess
import threading
import warnings
from typing import Any

import pytest

from tests import operations
from tests.helpers import FakeGitHub, InProcessGh, Repo
from tests.operations import (
    TRACE_PARENT_VARIABLE,
    TRACE_VARIABLE,
    UnderBudget,
    expect_operations,
    subcommand,
)


def run_git(repo: Repo, *args: str) -> None:
    repo.client.git.run(*args)


LOOKUP = ["api", "graphql", "-f", "query=pullRequests(baseRefName: $base, states: MERGED", "-f", "base=main"]
"""A gh command the fake answers."""


def run_gh() -> None:
    InProcessGh().run(["gh", *LOOKUP], capture_output=True)


def test_a_with_block_counts_the_git_processes_it_starts(repo: Repo) -> None:
    with expect_operations(git=2) as operations:
        run_git(repo, "rev-parse", "HEAD")
        run_git(repo, "status", "--porcelain")

    # Whole commands, with the directory git is run in, so a test can say which command ran.
    assert operations.git == [
        ["-C", str(repo.path), "rev-parse", "HEAD"],
        ["-C", str(repo.path), "status", "--porcelain"],
    ]
    assert operations.gh == []


def test_a_with_block_counts_the_gh_commands_it_runs(repo: Repo, gh: FakeGitHub) -> None:
    with expect_operations(git=0, gh=2) as operations:
        run_gh()
        run_gh()

    assert operations.gh == [LOOKUP, LOOKUP]
    assert operations.git == []


def test_a_with_block_counts_nothing_before_or_after_it(repo: Repo) -> None:
    run_git(repo, "rev-parse", "HEAD")
    with expect_operations(git=1):
        run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "rev-parse", "HEAD")


def test_gh_defaults_to_none(repo: Repo, gh: FakeGitHub) -> None:
    with pytest.raises(AssertionError, match="ran 1 gh command, 0 allotted"), expect_operations(git=0):
        run_gh()


@pytest.mark.parametrize("allotted", [1, 0])
def test_more_git_processes_than_allotted_fails_and_lists_them(repo: Repo, allotted: int) -> None:
    with pytest.raises(AssertionError) as failure, expect_operations(git=allotted):
        run_git(repo, "rev-parse", "HEAD")
        run_git(repo, "status", "--porcelain")

    message = str(failure.value)
    assert f"ran 2 git processes, {allotted} allotted" in message
    assert "git rev-parse HEAD" in message
    assert "git status --porcelain" in message


def test_fewer_git_processes_than_allotted_warns_to_lower_the_number(repo: Repo) -> None:
    @expect_operations(git=3)
    def two() -> None:
        run_git(repo, "rev-parse", "HEAD")
        run_git(repo, "status", "--porcelain")

    with pytest.warns(UnderBudget, match="ran 2 of the 3 git processes allotted: lower the number to 2"):
        two()


def test_exactly_the_allotted_number_is_silent(repo: Repo, gh: FakeGitHub) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with expect_operations(git=1, gh=1):
            run_git(repo, "rev-parse", "HEAD")
            run_gh()


def test_fewer_gh_commands_than_allotted_warns_to_lower_the_number(repo: Repo, gh: FakeGitHub) -> None:
    with (
        pytest.warns(UnderBudget, match="ran 1 of the 2 gh commands allotted: lower the number to 1"),
        expect_operations(git=0, gh=2),
    ):
        run_gh()


def test_more_gh_commands_than_allotted_fails_and_lists_them(repo: Repo, gh: FakeGitHub) -> None:
    with pytest.raises(AssertionError) as failure, expect_operations(git=0, gh=1):
        run_gh()
        run_gh()

    assert "ran 2 gh commands, 1 allotted" in str(failure.value)
    assert "gh api graphql -f 'query=pullRequests(baseRefName: $base, states: MERGED' -f base=main" in str(
        failure.value
    )


def test_it_does_not_hide_an_error_in_the_block(repo: Repo) -> None:
    with pytest.raises(ValueError, match="in the block"), expect_operations(git=5):
        raise ValueError("in the block")


def test_nested_blocks_each_count_what_ran_inside_them(repo: Repo) -> None:
    with expect_operations(git=3):
        run_git(repo, "rev-parse", "HEAD")
        with expect_operations(git=2):
            run_git(repo, "status", "--porcelain")
            run_git(repo, "rev-parse", "HEAD")


def test_it_counts_git_processes_started_in_other_threads(repo: Repo) -> None:
    # git-stack checks each branch's status from worker threads.
    with expect_operations(git=2):
        threads = [threading.Thread(target=run_git, args=(repo, "rev-parse", "HEAD")) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


# As the performance tests use it: on the test itself, which pytest still gives its fixtures and
# parameters, and runs once for each.
@pytest.mark.parametrize("name", ["first", "second"])
@expect_operations(git=1)
def test_the_decorator_leaves_a_tests_fixtures_and_parameters_alone(repo: Repo, name: str) -> None:
    run_git(repo, "rev-parse", "HEAD")


def test_the_decorator_checks_each_call_afresh(repo: Repo) -> None:
    # Calling it twice must count one each time, not two the second time.
    @expect_operations(git=1)
    def once() -> None:
        run_git(repo, "rev-parse", "HEAD")

    once()
    once()


def test_the_decorator_fails_the_test_that_runs_a_different_number(repo: Repo) -> None:
    @expect_operations(git=1)
    def twice() -> None:
        run_git(repo, "rev-parse", "HEAD")
        run_git(repo, "rev-parse", "HEAD")

    with pytest.raises(AssertionError, match="ran 2 git processes, 1 allotted"):
        twice()


def test_a_with_block_counts_the_git_processes_of_a_git_stack_process(repo: Repo) -> None:
    # The stack is made before the block, so that what it takes isn't counted.
    repo.make_stack()

    with expect_operations(git=3) as operations:
        repo.stack_process("parent")

    assert [subcommand(command)[0] for command in operations.git] == ["config", "rev-parse", "for-each-ref"]
    assert all(command[0] == "-C" for command in operations.git)


def test_a_git_stack_process_runs_the_same_git_processes_as_one_in_this_process(repo: Repo) -> None:
    # The two ways of counting must agree, or the counts in tests that use one mean nothing for
    # the others.
    repo.make_stack()
    with expect_operations(git=3) as here:
        repo.stack("parent")

    with expect_operations(git=3) as separate:
        repo.stack_process("parent")

    # The same commands, whole: both are given the repository as the same `-C <path>`.
    assert separate.git == here.git


def test_a_with_block_counts_the_gh_commands_of_a_git_stack_process(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()

    with expect_operations(git=25, gh=8) as operations:
        repo.stack_process("push", "--prs")

    assert [command[:2] for command in operations.gh].count(["pr", "create"]) == 3


def test_only_what_git_stack_starts_is_counted_not_what_git_starts_for_it(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    with expect_operations(git=39) as operations:
        repo.stack_process("restack")

    # Each branch is rebased by a `git rebase`, which runs `git notes copy` itself.
    ran = [subcommand(command)[0] for command in operations.git]
    assert [name for name in ran if name in ("rebase", "notes")] == ["rebase", "rebase"]


def test_the_tests_own_git_is_not_counted_in_a_block_with_a_git_stack_process(repo: Repo) -> None:
    repo.make_stack()

    # Only the new process is traced, so this status isn't one of the 3.

    with expect_operations(git=3):
        repo.git("status", "--porcelain")
        repo.stack_process("parent")


def test_nested_blocks_each_count_a_git_stack_process(repo: Repo) -> None:
    repo.make_stack()

    with expect_operations(git=3), expect_operations(git=3):
        repo.stack_process("parent")


@pytest.mark.usefixtures("separate_processes")
def test_a_performance_test_cant_run_git_stack_in_this_process(repo: Repo) -> None:
    repo.make_stack()

    with pytest.raises(pytest.fail.Exception, match="stack_process"):
        repo.stack("parent")

    assert repo.stack_process("parent").stdout == "pr2\n"


def test_a_block_that_ends_first_doesnt_end_an_equal_one_around_it(repo: Repo) -> None:
    # Both are empty when the inner one ends, so equal; the outer one must still be recording.
    with expect_operations(git=1):
        with expect_operations(git=0):
            pass
        run_git(repo, "rev-parse", "HEAD")


def test_only_the_git_stack_process_gets_the_trace_variable(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.make_stack()
    environments: list[dict[str, str] | None] = []
    run: Any = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        # As the process starts, when it would inherit this process's environment: a variable set
        # here for the length of the call and restored after would be caught by nothing later.
        assert TRACE_VARIABLE not in os.environ
        environments.append(kwargs.get("env"))
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)

    with expect_operations(git=3):
        repo.stack_process("parent")

    # And not left in it after, for every later process this one starts to inherit.
    assert TRACE_VARIABLE not in os.environ
    (child,) = environments
    assert child is not None
    assert TRACE_VARIABLE in child


def test_a_trace_session_inherited_from_the_test_run_doesnt_hide_git_stacks_processes(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    # As if the tests were run by a git with trace2 on: every process then has a parent session, and
    # so a `/` in its own, which is what marks one that git started for git-stack.
    monkeypatch.setenv(TRACE_PARENT_VARIABLE, "20260101T000000Z-H0-P0")

    # Counted, not just allotted: counting none would only warn.
    with expect_operations(git=3) as ran:
        repo.stack_process("parent")
    assert len(ran.git) == 3


def test_a_block_records_how_long_it_took_and_is_reported_once_it_finishes(repo: Repo) -> None:
    operations.FINISHED.clear()

    with expect_operations(git=1) as ran:
        run_git(repo, "rev-parse", "HEAD")

    assert ran.seconds > 0
    assert [ran] == operations.FINISHED


def test_a_block_that_failed_is_not_reported(repo: Repo) -> None:
    operations.FINISHED.clear()

    # One more process than allotted.
    with pytest.raises(AssertionError), expect_operations(git=0):
        run_git(repo, "rev-parse", "HEAD")

    assert operations.FINISHED == []


def test_a_block_that_ran_fewer_than_allotted_is_reported(repo: Repo) -> None:
    operations.FINISHED.clear()

    with pytest.warns(operations.UnderBudget), expect_operations(git=2) as ran:
        run_git(repo, "rev-parse", "HEAD")

    assert [ran] == operations.FINISHED
