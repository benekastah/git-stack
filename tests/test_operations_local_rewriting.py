"""How many git processes the commands that rewrite branches locally run, at several stack sizes.

Each command is a `git stack` process of its own, in a repository of its own, so nothing is
cached in Python, or left by another test, to skew a count. The counts that grow with the stack
are declared per size, so a change in how they grow shows."""

import pytest

from tests.helpers import Repo
from tests.operations import expect_operations

pytestmark = pytest.mark.usefixtures("separate_processes")

# Each count below is for a stack of 1, 3 and 10 branches (the `chain` fixture), keyed by its
# size and with the formula that fits all three. A formula is how the count grows, so a change
# to it, not just to the number, shows up in review.
#
# Restacking replays each branch above the one that changed, and each replay costs 12 (4 + 12 *
# branches, from 3 branches up). A stack of 1 has nothing above it to replay, so it's only the check
# of whether it needs it, the same as an up to date stack's (8).
RESTACK_BEHIND = {1: 8, 3: 40, 10: 124}
# Even an up to date stack is checked a branch at a time: 6 + 2 * branches.
RESTACK_UP_TO_DATE = {1: 8, 3: 12, 10: 26}
# Nothing to pull: 11 + branches.
PULL_NOTHING = {1: 12, 3: 14, 10: 21}
# Nothing merged, and nothing pushed: 10 + branches.
CLEAN_NOTHING = {1: 11, 3: 13, 10: 20}
# Renaming moves what's recorded for the branch, and for the branch stacked directly on it (which
# records its parent by name), but not the ones above that: so it grows from 1 branch to 2, not after.
RENAME_BOTTOM = {1: 9, 3: 10, 10: 10}
RENAME_TOP = {1: 9, 3: 9, 10: 9}
UNTRACK_TOP = {1: 6, 3: 6, 10: 6}
# Untracking every branch: 3 + 3 * branches.
UNTRACK_ALL = {1: 6, 3: 12, 10: 33}


def tips(repo: Repo, chain: list[str]) -> dict[str, str]:
    return {branch: repo.git("rev-parse", branch) for branch in chain}


def assert_stacked(repo: Repo, chain: list[str]) -> None:
    """Each branch is recorded on the one before it (the first on main), which is in its history.
    The tests check this, which is what the command is for, because running fewer operations than
    allotted only warns, so one that stopped doing its work would otherwise pass."""
    for branch, parent in zip(chain, ["main", *chain], strict=False):
        # Fails the test if it isn't: `merge-base --is-ancestor` exits with 1.
        repo.git("merge-base", "--is-ancestor", parent, branch)
        assert repo.config(branch, "stackParent") == parent


def commit_to_bottom(repo: Repo) -> None:
    """A commit on the lowest branch, which the ones above it have to be restacked onto: every
    other branch in the stack is then behind its parent."""
    # `switch` rather than a stack command, since the point is a change git-stack didn't make.
    repo.git("switch", "--quiet", "chain1")
    repo.write("extra.txt", "extra\n")
    repo.git("add", "extra.txt")
    repo.git("commit", "--quiet", "-m", "extra")


def test_restack_replays_each_branch_above_a_change(repo: Repo, chain: list[str]) -> None:
    commit_to_bottom(repo)

    with expect_operations(git=RESTACK_BEHIND[len(chain)]):
        repo.stack_process("restack")

    # Every branch above the one that changed has it in its history now.
    assert_stacked(repo, chain)


def test_restack_checks_each_branch_of_an_up_to_date_stack(repo: Repo, chain: list[str]) -> None:
    # Nothing is changed after the chain's made, so no branch needs replaying: this is only the checking.
    before = tips(repo, chain)

    with expect_operations(git=RESTACK_UP_TO_DATE[len(chain)]):
        repo.stack_process("restack")

    assert tips(repo, chain) == before


def test_pull_with_nothing_to_pull(repo: Repo, chain: list[str]) -> None:
    # Pushed, so each branch has an upstream to compare with (the origin has nothing new for it).
    repo.client.push()
    before = tips(repo, chain)

    with expect_operations(git=PULL_NOTHING[len(chain)]):
        repo.stack_process("pull")

    assert tips(repo, chain) == before
    assert_stacked(repo, chain)


def test_clean_with_nothing_merged(repo: Repo, chain: list[str]) -> None:
    with expect_operations(git=CLEAN_NOTHING[len(chain)]):
        repo.stack_process("clean")

    # Nothing merged, so every branch is still there and tracked.
    assert_stacked(repo, chain)


def test_rename_the_bottom_branch(repo: Repo, chain: list[str]) -> None:
    # `rename` renames the branch you're on, and the bottom one has the others stacked on it, which
    # is recorded by name, so they have to be updated too.
    repo.git("switch", "--quiet", "chain1")

    with expect_operations(git=RENAME_BOTTOM[len(chain)]):
        repo.stack_process("rename", "renamed")

    assert repo.git("branch", "--list", "chain1") == ""
    assert_stacked(repo, ["renamed", *chain[1:]])


def test_rename_the_top_branch(repo: Repo, chain: list[str]) -> None:
    with expect_operations(git=RENAME_TOP[len(chain)]):
        repo.stack_process("rename", "renamed")

    assert repo.git("branch", "--list", chain[-1]) == ""
    assert_stacked(repo, [*chain[:-1], "renamed"])


def test_untrack_the_top_branch(repo: Repo, chain: list[str]) -> None:
    with expect_operations(git=UNTRACK_TOP[len(chain)]):
        repo.stack_process("untrack")

    assert not repo.config_exists(chain[-1], "stackParent")
    assert_stacked(repo, chain[:-1])


def test_untrack_every_branch(repo: Repo, chain: list[str]) -> None:
    # A glob (git-stack expands it, not the shell: it's quoted for that), like `untrack 'fix-*'`.
    with expect_operations(git=UNTRACK_ALL[len(chain)]):
        repo.stack_process("untrack", "chain*")

    assert not any(repo.config_exists(branch, "stackParent") for branch in chain)
