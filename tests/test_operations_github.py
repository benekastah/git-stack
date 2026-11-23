"""How many git processes and gh commands the commands that talk to GitHub run, at several stack
sizes, against tests/fake_gh.py.

Each command is a `git stack` process of its own, in a repository of its own, so nothing is
cached in Python, or left by another test, to skew a count. The counts that grow with the stack
are declared per size, so a change in how they grow shows."""

import pytest

from tests.helpers import FakeGitHub, Repo, assert_chain_stacked, chain_tips, mentions
from tests.operations import expect_operations

# `gh` makes the repository look like a GitHub one, with tests/fake_gh.py as `gh` (in this process,
# for the setup that uses the client, and as a command for the `git stack` processes the tests
# start, which read and write the same state file: that's how a pull request opened here is seen
# there, and how the commands they run are counted).
pytestmark = [pytest.mark.usefixtures("separate_processes", "gh")]

# Each is (git processes, gh commands) at 1, 3 and 10 branches.
# Pushing starts a few processes for each branch (a stack of 10 pushes in 37 against 10 for 1)...
PUSH = {1: (10, 0), 3: (16, 0), 10: (37, 0)}
# ...and opening a pull request for each costs more of both.
PUSH_OPENING_PRS = {1: (11, 2), 3: (20, 8), 10: (48, 15)}
# With every pull request open, git doesn't grow with the stack from 3 branches up, and gh stays at 4
# (2 for a stack of 1).
PUSH_WITH_PRS_OPEN = {1: (7, 2), 3: (8, 4), 10: (8, 4)}
PRS_ALL_OPEN = {1: (3, 2), 3: (4, 4), 10: (4, 4)}
# Nothing to do: gh is asked once, and git 9 times.
SYNC_NOTHING = {1: (9, 1), 3: (9, 1), 10: (9, 1)}
# The bottom merged, as GitHub reports it (the merge commit and the head it had): the rest are
# restacked onto main and pushed again. The fake supplies the GitHub merge evidence.
SYNC_BOTTOM_MERGED = {1: (31, 1), 3: (66, 5), 10: (185, 6)}
# gh is asked twice, however many pull requests there are.
IMPORT = {1: (18, 2), 3: (34, 2), 10: (90, 2)}
# info asks GitHub about all the branches in one request.
INFO = {1: (3, 1), 3: (3, 1), 10: (3, 1)}


# The tests also check what the command did, since running fewer operations than allotted only
# warns, so one that stopped doing its work would otherwise pass.


def assert_pull_requests(gh: FakeGitHub, chain: list[str]) -> None:
    """A pull request for each branch of the chain, into the one before it (the first into main),
    and no other, so a duplicate for a branch fails."""
    pull_requests = [(pr["headRefName"], pr["baseRefName"]) for pr in gh.load()["prs"]]
    assert sorted(pull_requests) == sorted(zip(chain, ["main", *chain], strict=False))


def open_pull_requests(repo: Repo) -> None:
    """Push the stack and open a pull request for each branch, as setup: in this process, outside
    what a test counts, into the state the fake gh keeps for the process that does."""
    repo.client.push(prs=True)


def test_push_a_stack_that_was_never_pushed(repo: Repo, chain: list[str]) -> None:
    git, gh = PUSH[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("push")

    # Every branch is on the remote, as it is here.
    assert {b: repo.git("rev-parse", f"refs/remotes/origin/{b}") for b in chain} == chain_tips(repo, chain)


def test_push_opening_a_pull_request_for_each_branch(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    git, gh_commands = PUSH_OPENING_PRS[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("push", "--prs")

    # A pull request for each branch, into the one before it (the first into main).
    assert_pull_requests(gh, chain)


def test_push_when_every_branch_has_a_pull_request(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    created = len(gh.calls("pr", "create"))
    git, gh_commands = PUSH_WITH_PRS_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("push", "--prs")

    # No new pull request: each branch has the one it had.
    assert_pull_requests(gh, chain)
    assert len(gh.calls("pr", "create")) == created


def test_prs_when_every_branch_has_a_pull_request(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    created = len(gh.calls("pr", "create"))
    git, gh_commands = PRS_ALL_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("prs")

    assert len(gh.calls("pr", "create")) == created
    assert_pull_requests(gh, chain)


def test_sync_with_nothing_to_do(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    before = chain_tips(repo, chain)
    git, gh = SYNC_NOTHING[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("sync")

    assert chain_tips(repo, chain) == before
    assert_chain_stacked(repo, chain)


def test_sync_after_the_bottom_pull_request_was_merged(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    # What GitHub's "Squash and merge" does: main gets one new commit with the pull request's
    # changes, which no branch above it has, and the pull request is marked merged. `sync` then
    # moves the branches above onto main, which is most of what it costs.
    head = repo.git("rev-parse", "chain1")
    repo.squash_merge_on_forge("chain1")
    merge = repo.git("rev-parse", "HEAD", cwd=repo.forge)
    # As GitHub reports it: merged, by that commit, with the commit the pull request had. Without
    # them `sync` doesn't trust GitHub's word and finds the merge from git alone, which isn't what
    # a user's sync does. The pull request is found by its branch, not by its place in the list.
    state = gh.load()
    pull_request = next(pr for pr in state["prs"] if pr["headRefName"] == "chain1")
    pull_request.update(state="MERGED", headRefOid=head, mergeCommit={"oid": merge})
    gh.save(state)
    git, gh_commands = SYNC_BOTTOM_MERGED[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("sync")

    # The merged branch isn't tracked, and the rest are on main now.
    assert not repo.config_exists("chain1", "stackParent")
    assert_chain_stacked(repo, chain[1:])


def test_import_tracks_each_open_pull_request(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    # Forget the stack, keeping its pull requests, which is what `import` is for: tracking them
    # again, as in a fresh clone.
    repo.client.untrack(["chain*"])
    git, gh = IMPORT[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("import")

    assert_chain_stacked(repo, chain)


def test_info_asks_github_about_each_branch(repo: Repo, chain: list[str]) -> None:
    # Every branch is requested at once, so the gh count stays at 1.
    open_pull_requests(repo)
    git, gh = INFO[len(chain)]

    with expect_operations(git=git, gh=gh):
        result = repo.stack_process("info", "--prs")

    assert all(mentions(result.stdout, branch) for branch in chain)
    # With the links of the pull requests, which is what asking GitHub is for: one for each.
    assert result.stdout.count("/pull/") == len(chain)
