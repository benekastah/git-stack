"""How many git processes the read-only commands run, at several stack sizes: none of them should
run more for a longer stack, except the first `info`, which checks each branch for having landed
in its parent (and remembers the answer on disk, in `.git/git-stack-cache.json`, for the next
process).

Each command is a `git stack` process of its own, as a user runs it, in a repository of its
own, so nothing is cached in Python, or left by another test, to skew a count. Each test also
checks what the command did: running fewer operations than allotted only warns, so a command that
stopped doing its work would otherwise pass."""

import pytest

from tests.helpers import Repo, mentions
from tests.operations import expect_operations

pytestmark = pytest.mark.usefixtures("separate_processes")


def below_the_last(chain: list[str]) -> str:
    """The branch the chain's last is stacked on: main, for a chain of one."""
    return chain[-2] if len(chain) > 1 else "main"


# `chain` makes the stack before the decorator starts counting. The same count at 1, 3 and 10
# branches is the point: nothing here depends on the stack's length.
#
# Each of these starts Git to read config and refs. Stopped worktree operations and
# the ancestry check for base are handled in-process.
@expect_operations(git=2)
def test_parent_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    assert repo.stack_process("parent").stdout == f"{below_the_last(chain)}\n"


@expect_operations(git=2)
def test_show_stack_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    assert repo.stack_process("show-stack").stdout == "".join(f"{branch}\n" for branch in chain)


@expect_operations(git=2)
def test_base_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    assert repo.stack_process("base").stdout.strip() == repo.git("rev-parse", below_the_last(chain))


# ...and prev and next also `git switch` to the branch.
@expect_operations(git=3)
def test_prev_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    repo.stack_process("prev")

    assert repo.git("branch", "--show-current") == below_the_last(chain)


def test_next_runs_the_same_for_any_stack_size(repo: Repo, chain: list[str]) -> None:
    # The chain's last branch is checked out and has no next one: step back first, outside the block.
    repo.stack_process("prev")

    with expect_operations(git=3):
        repo.stack_process("next")

    assert repo.git("branch", "--show-current") == chain[-1]


def test_the_first_info_checks_each_branch_for_having_landed_in_its_parent(repo: Repo, chain: list[str]) -> None:
    # Config, refs, color, and one `merge-tree` for each branch: merging it into its parent, to see whether
    # that changes nothing, which is how a branch whose changes landed (a merged one) is found.
    # That is the one count here that grows with the stack. Each test has a repository of its own,
    # with no cache of those checks on disk yet, so this first `info` always has them all to do.
    with expect_operations(git=3 + len(chain)):
        result = repo.stack_process("info", "--offline")

    assert all(mentions(result.stdout, branch) for branch in chain)


@pytest.mark.parametrize(
    ("args", "git"),
    [(["info", "--offline"], 3), (["info", "--all", "--offline"], 3), (["info", "--offline", "--json"], 2)],
    ids=["info", "info-all", "info-json"],
)
def test_info_runs_the_same_for_any_stack_size_once_its_branches_are_checked(
    repo: Repo, chain: list[str], args: list[str], git: int
) -> None:
    # Once, outside the block, to fill the on-disk cache of those checks: a second process reads
    # them, with no `merge-tree` for any branch, so it's the same for any stack size.
    repo.stack_process("info", "--offline")

    with expect_operations(git=git):
        result = repo.stack_process(*args)

    assert all(mentions(result.stdout, branch) for branch in chain)


@pytest.mark.parametrize(
    ("output", "branch", "found"),
    [
        ("└── chain10 *\n", "chain1", False),
        ("└── chain1 *\n", "chain1", True),
        ("main\n└── chain1\n    └── chain10\n", "chain10", True),
        ("feature-chain1\n", "chain1", False),
    ],
)
def test_mentions_finds_whole_branch_names_only(output: str, branch: str, found: bool) -> None:
    assert mentions(output, branch) is found
