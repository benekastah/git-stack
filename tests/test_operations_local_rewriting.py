"""How many git processes the commands that rewrite branches locally run, at several stack sizes.

Each command is a `git stack` process of its own, in a repository of its own, so nothing is
cached in Python, or left by another test, to skew a count. The counts that grow with the stack
are declared per size, so a change in how they grow shows."""

import pytest

from tests.helpers import Repo, assert_chain_stacked, chain_tips
from tests.operations import expect_operations

pytestmark = pytest.mark.usefixtures("separate_processes")

# Each count below is for a stack of 1, 3 and 10 branches (the `chain` fixture), keyed by its
# size and with the formula that fits all three. A formula is how the count grows, so a change
# to it, not just to the number, shows up in review.
#
# Restacking replays the branches above the one that changed in one `git rebase` of the last of them.
# What's left for each branch of that chain is writing its base; 20 processes are shared: the record
# of where each was (a write before and one after, and the config read again after the first, which
# the refs read after the rebase need), and the checks and reads of any restack (20 + the branches
# above the one that changed). A stack of one only checks its base.
RESTACK_BEHIND = {1: 3, 3: 22, 10: 29}
# The ancestry of an up-to-date stack is checked in process.
RESTACK_UP_TO_DATE = {1: 3, 3: 3, 10: 3}
# Pull and clean end by printing the stack, asking `git config --get-colorbool` whether to color it.
PULL_NOTHING = {1: 6, 3: 6, 10: 6}
CLEAN_NOTHING = {1: 5, 3: 5, 10: 5}
# Renaming moves what's recorded for the branch, and rewrites the parent (recorded by name) of the
# branch stacked directly on it, but not that of the ones above that: so it grows from 1 branch to 2,
# not after.
RENAME_BOTTOM = {1: 7, 3: 8, 10: 8}
RENAME_TOP = {1: 7, 3: 7, 10: 7}
UNTRACK_TOP = {1: 4, 3: 4, 10: 4}
# Untracking every branch: 1 + 3 * branches.
UNTRACK_ALL = {1: 4, 3: 10, 10: 31}


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
    assert_chain_stacked(repo, chain)


def test_restack_checks_each_branch_of_an_up_to_date_stack(repo: Repo, chain: list[str]) -> None:
    # Nothing is changed after the chain's made, so no branch needs replaying: this is only the checking.
    before = chain_tips(repo, chain)

    with expect_operations(git=RESTACK_UP_TO_DATE[len(chain)]):
        repo.stack_process("restack")

    assert chain_tips(repo, chain) == before


def test_pull_with_nothing_to_pull(repo: Repo, chain: list[str]) -> None:
    # Pushed, so each branch has an upstream to compare with (the origin has nothing new for it).
    repo.client.push()
    before = chain_tips(repo, chain)

    with expect_operations(git=PULL_NOTHING[len(chain)]):
        repo.stack_process("pull")

    assert chain_tips(repo, chain) == before
    assert_chain_stacked(repo, chain)


def test_clean_with_nothing_merged(repo: Repo, chain: list[str]) -> None:
    with expect_operations(git=CLEAN_NOTHING[len(chain)]):
        repo.stack_process("clean")

    # Nothing merged, so every branch is still there and tracked.
    assert_chain_stacked(repo, chain)


def test_rename_the_bottom_branch(repo: Repo, chain: list[str]) -> None:
    # `rename` renames the branch you're on, and the bottom one has the others stacked on it, which
    # is recorded by name, so they have to be updated too.
    repo.git("switch", "--quiet", "chain1")

    with expect_operations(git=RENAME_BOTTOM[len(chain)]):
        repo.stack_process("rename", "renamed")

    assert repo.git("branch", "--list", "chain1") == ""
    assert_chain_stacked(repo, ["renamed", *chain[1:]])


def test_rename_the_top_branch(repo: Repo, chain: list[str]) -> None:
    with expect_operations(git=RENAME_TOP[len(chain)]):
        repo.stack_process("rename", "renamed")

    assert repo.git("branch", "--list", chain[-1]) == ""
    assert_chain_stacked(repo, [*chain[:-1], "renamed"])


def test_untrack_the_top_branch(repo: Repo, chain: list[str]) -> None:
    before = chain_tips(repo, chain)

    with expect_operations(git=UNTRACK_TOP[len(chain)]):
        repo.stack_process("untrack")

    assert not repo.config_exists(chain[-1], "stackParent")
    assert_chain_stacked(repo, chain[:-1])
    # Untracked, not deleted: the branch is where it was.
    assert chain_tips(repo, chain) == before


def test_untrack_every_branch(repo: Repo, chain: list[str]) -> None:
    # A glob (git-stack expands it, not the shell: it's quoted for that), like `untrack 'fix-*'`.
    before = chain_tips(repo, chain)

    with expect_operations(git=UNTRACK_ALL[len(chain)]):
        repo.stack_process("untrack", "chain*")

    assert not any(repo.config_exists(branch, "stackParent") for branch in chain)
    assert chain_tips(repo, chain) == before
