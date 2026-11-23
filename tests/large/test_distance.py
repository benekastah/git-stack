"""Stacks forked from main some days ago, in a repository where main gets 500 commits a day: how
many git processes the commands that compare a stack with main run when main is 500, 3,500 and
15,000 commits ahead. The tests/large scenarios stack on main's tip, so they say nothing of this.
It's the same at each distance, so each test runs at all three: a count that grew with the
distance would fail at the others."""

import pytest

from tests.helpers import Repo
from tests.large_repo import Recipe
from tests.operations import expect_operations

pytestmark = pytest.mark.usefixtures("separate_processes")

COMMITS_PER_DAY = Recipe().commits_per_day
DAYS = [1, 7, 30]
SIZES = [1, 3, 10]

# The number of git processes by the number of branches: the same however far behind main the
# stack is, which each test checks at every distance.
INFO_FIRST = {1: 16, 3: 18, 10: 25}
RESTACK = {1: 17, 3: 21, 10: 35}
RESTACK_LATEST = {1: 26, 3: 49, 10: 133}
SYNC = {1: 14, 3: 16, 10: 23}


@pytest.fixture(params=DAYS, ids=lambda days: f"{days}-days-behind")
def days(request: pytest.FixtureRequest) -> int:
    return request.param


@pytest.fixture(params=SIZES, ids=lambda size: f"{size}-branches")
def stale_chain(repo: Repo, days: int, request: pytest.FixtureRequest) -> list[str]:
    """A stack of `request.param` branches forked `days` days before main's tip, the last
    checked out."""
    # main moves on by COMMITS_PER_DAY a day, so this commit is where main was `days` days ago.
    fork = repo.git("rev-parse", f"main~{days * COMMITS_PER_DAY}")
    names = [f"chain{i}" for i in range(1, request.param + 1)]
    repo.git("switch", "--quiet", "-c", names[0], fork)
    for index, name in enumerate(names):
        if index:
            repo.client.branch(name)
        repo.write(f"{name}.txt", f"{name}\n")
        repo.git("add", f"{name}.txt")
        repo.git("commit", "--quiet", "-m", f"{name} c1")
        if not index:
            # The first branch was made from an old commit, not from main's branch, so it's
            # `track` that tells git-stack its parent is main (and finds where it forked).
            repo.client.track("main", [name])
    return names


def test_the_first_info_checks_each_branch_against_a_main_that_moved_on(repo: Repo, stale_chain: list[str]) -> None:
    with expect_operations(git=INFO_FIRST[len(stale_chain)]):
        repo.stack_process("info")


def test_restack_checks_a_stack_a_busy_main_has_left_behind(repo: Repo, stale_chain: list[str]) -> None:
    with expect_operations(git=RESTACK[len(stale_chain)]):
        repo.stack_process("restack")


def test_restack_latest_moves_a_stack_onto_a_busy_main(repo: Repo, stale_chain: list[str]) -> None:
    with expect_operations(git=RESTACK_LATEST[len(stale_chain)]):
        repo.stack_process("restack", "--latest")


def test_sync_a_stack_a_busy_main_has_left_behind(repo: Repo, stale_chain: list[str]) -> None:
    repo.client.push()

    with expect_operations(git=SYNC[len(stale_chain)]):
        repo.stack_process("sync")
