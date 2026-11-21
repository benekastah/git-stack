"""How many git processes the read-only commands run, at several stack sizes: none of them should
run more for a longer stack, except the first `info`, which checks each branch against its parent
(and remembers the answer on disk, in `.git/git-stack-cache.json`, for the next process).

Each command is a `git stack` process of its own, as a user runs it, in a repository of its
own, so nothing is cached in Python, or left by another test, to skew a count."""

import pytest

from tests.helpers import Repo
from tests.operations import expect_operations

pytestmark = pytest.mark.usefixtures("separate_processes")


# `chain` makes the stack before the decorator starts counting, and is used by name, not as an
# argument, since the test doesn't need what it returns. The same count at 1, 3 and 10 branches
# is the point: nothing here depends on the stack's length.
#
# Each of these reads the config, checks for a stopped rebase or cherry-pick, and reads the refs.
@pytest.mark.usefixtures("chain")
@expect_operations(git=3)
def test_parent_runs_the_same_for_any_stack_size(repo: Repo) -> None:
    repo.stack_process("parent")


@pytest.mark.usefixtures("chain")
@expect_operations(git=3)
def test_show_stack_runs_the_same_for_any_stack_size(repo: Repo) -> None:
    repo.stack_process("show-stack")


# ...and base also checks that the base is in the branch's history.
@pytest.mark.usefixtures("chain")
@expect_operations(git=5)
def test_base_runs_the_same_for_any_stack_size(repo: Repo) -> None:
    repo.stack_process("base")


@pytest.mark.usefixtures("chain")
@expect_operations(git=4)
def test_prev_runs_the_same_for_any_stack_size(repo: Repo) -> None:
    repo.stack_process("prev")


def test_next_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    # The chain's last branch is checked out and has no next one: step back first, outside the block.
    repo.stack_process("prev")

    with expect_operations(git=4):
        repo.stack_process("next")


def test_the_first_info_checks_each_branch_against_its_parent(repo: Repo, chain: list[str]) -> None:
    # The config, a check for a stopped operation, the refs, a long-lived `cat-file`, and the
    # colors, and one `merge-tree` for each branch: whether it conflicts with its parent. That
    # is the one count here that grows with the stack. Each test has a repository of its own, with
    # no cache of those checks on disk yet, so this first `info` always has them all to do.
    with expect_operations(git=5 + len(chain)):
        repo.stack_process("info", "--offline")


@pytest.mark.parametrize(
    ("args", "git"),
    [(["info", "--offline"], 4), (["info", "--all", "--offline"], 4), (["info", "--offline", "--json"], 3)],
    ids=["info", "info-all", "info-json"],
)
def test_info_runs_the_same_for_any_stack_size_once_its_branches_are_checked(
    repo: Repo, chain: list[str], args: list[str], git: int
) -> None:
    # Once, outside the block, to fill the on-disk cache of those checks: a second process reads
    # them, with no `merge-tree` for any branch, so it's the same for any stack size.
    repo.stack_process("info", "--offline")

    with expect_operations(git=git):
        repo.stack_process(*args)
