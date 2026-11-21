"""How many git processes and gh commands the commands that talk to GitHub run, at several stack
sizes, against tests/fake_gh.py.

Each command is a `git stack` process of its own, in a repository of its own, so nothing is
cached in Python, or left by another test, to skew a count. The counts that grow with the stack
are declared per size, so a change in how they grow shows."""

import pytest

from tests.helpers import FakeGitHub, Repo
from tests.operations import expect_operations

# `gh` makes the repository look like a GitHub one, with tests/fake_gh.py as `gh` (in this process,
# for the setup that uses the client, and as a command for the `git stack` processes the tests
# start, which read and write the same state file: that's how a pull request opened here is seen
# there, and how the commands they run are counted).
pytestmark = [pytest.mark.usefixtures("separate_processes", "gh")]

# Each is (git processes, gh commands) at 1, 3 and 10 branches.
# Pushing starts a few processes for each branch (a stack of 10 pushes in 39 against 12 for 1)...
PUSH = {1: (12, 0), 3: (18, 0), 10: (39, 0)}
# ...and opening a pull request for each costs more of both.
PUSH_OPENING_PRS = {1: (14, 2), 3: (25, 8), 10: (60, 15)}
# With every pull request open, git doesn't grow with the stack, and gh stays at 4.
PUSH_WITH_PRS_OPEN = {1: (8, 2), 3: (9, 4), 10: (9, 4)}
PRS_ALL_OPEN = {1: (4, 2), 3: (5, 4), 10: (5, 4)}
# Nothing to do: gh is asked once, and git 11 + branches.
SYNC_NOTHING = {1: (12, 1), 3: (14, 1), 10: (21, 1)}
# The bottom merged: the rest are restacked onto main and pushed again.
SYNC_BOTTOM_MERGED = {1: (32, 1), 3: (72, 5), 10: (205, 6)}
# gh is asked twice, however many pull requests there are.
IMPORT = {1: (19, 2), 3: (36, 2), 10: (92, 2)}
# info asks GitHub once for every branch.
INFO = {1: (4, 1), 3: (4, 1), 10: (4, 1)}


def open_pull_requests(repo: Repo) -> None:
    """Push the stack and open a pull request for each branch, as setup: in this process, outside
    what a test counts, into the state the fake gh keeps for the process that does."""
    repo.client.push(prs=True)


def test_push_a_stack_that_was_never_pushed(repo: Repo, chain: list[str]) -> None:
    git, gh = PUSH[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("push")


def test_push_opening_a_pull_request_for_each_branch(repo: Repo, chain: list[str]) -> None:
    git, gh = PUSH_OPENING_PRS[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("push", "--prs")


def test_push_when_every_branch_has_a_pull_request(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    git, gh = PUSH_WITH_PRS_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("push", "--prs")


def test_prs_when_every_branch_has_a_pull_request(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    git, gh = PRS_ALL_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("prs")


def test_sync_with_nothing_to_do(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    git, gh = SYNC_NOTHING[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("sync")


def test_sync_after_the_bottom_pull_request_was_merged(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    # What GitHub's "Squash and merge" does: main gets one new commit with the pull request's
    # changes, which no branch above it has, and the pull request is marked merged. `sync` then
    # moves the branches above onto main, which is most of what it costs.
    repo.squash_merge_on_forge("chain1")
    state = gh.load()
    state["prs"][0]["state"] = "MERGED"
    gh.save(state)
    git, gh_commands = SYNC_BOTTOM_MERGED[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("sync")


def test_import_tracks_each_open_pull_request(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    # Forget the stack, keeping its pull requests, which is what `import` is for: tracking them
    # again, as in a fresh clone.
    repo.client.untrack(["chain*"])
    git, gh = IMPORT[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("import")


def test_info_asks_github_about_each_branch(repo: Repo, chain: list[str]) -> None:
    # Without --offline, so GitHub is asked, for every branch at once: the gh count stays at 1.
    open_pull_requests(repo)
    git, gh = INFO[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("info")
