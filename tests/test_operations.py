"""tests/operations.py: declaring how many git and gh operations a test (or a block of it) runs."""

import threading

import pytest

from tests.helpers import FakeGitHub, InProcessGh, Repo
from tests.operations import expect_operations


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

    assert operations.git == [["rev-parse", "HEAD"], ["status", "--porcelain"]]
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
    with pytest.raises(AssertionError, match="expected 0 gh commands, ran 1"), expect_operations(git=0):
        run_gh()


@pytest.mark.parametrize(("expected", "ran"), [(1, 2), (3, 2), (0, 2)])
def test_a_different_number_of_git_processes_fails_and_lists_them(repo: Repo, expected: int, ran: int) -> None:
    with pytest.raises(AssertionError) as failure, expect_operations(git=expected):
        run_git(repo, "rev-parse", "HEAD")
        run_git(repo, "status", "--porcelain")

    message = str(failure.value)
    assert f"expected {expected} git process{'' if expected == 1 else 'es'}, ran {ran}" in message
    assert "git rev-parse HEAD" in message
    assert "git status --porcelain" in message


def test_a_different_number_of_gh_commands_fails_and_lists_them(repo: Repo, gh: FakeGitHub) -> None:
    with pytest.raises(AssertionError) as failure, expect_operations(git=0, gh=1):
        run_gh()
        run_gh()

    assert "expected 1 gh command, ran 2" in str(failure.value)
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
    with expect_operations(git=2):
        threads = [threading.Thread(target=run_git, args=(repo, "rev-parse", "HEAD")) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize("name", ["first", "second"])
@expect_operations(git=1)
def test_the_decorator_leaves_a_tests_fixtures_and_parameters_alone(repo: Repo, name: str) -> None:
    run_git(repo, "rev-parse", "HEAD")


def test_the_decorator_checks_each_call_afresh(repo: Repo) -> None:
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

    with pytest.raises(AssertionError, match="expected 1 git process, ran 2"):
        twice()


def test_a_with_block_counts_the_git_processes_of_a_git_stack_process(repo: Repo) -> None:
    repo.make_stack()

    with expect_operations(git=3) as operations:
        repo.stack_process("parent")

    assert [command[0] for command in operations.git] == ["config", "rev-parse", "for-each-ref"]


def test_a_git_stack_process_runs_the_same_git_processes_as_one_in_this_process(repo: Repo) -> None:
    repo.make_stack()
    with expect_operations(git=3) as here:
        repo.stack("parent")

    with expect_operations(git=3) as separate:
        repo.stack_process("parent")

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

    with expect_operations(git=40) as operations:
        repo.stack_process("restack")

    # Each branch is rebased by a `git rebase`, which runs `git notes copy` itself.
    assert [command[0] for command in operations.git if command[0] in ("rebase", "notes")] == ["rebase", "rebase"]


def test_the_tests_own_git_is_not_counted_in_a_block_with_a_git_stack_process(repo: Repo) -> None:
    repo.make_stack()

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
