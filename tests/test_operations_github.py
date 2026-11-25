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

# Each is (git processes, gh commands) at 1, 3 and 10 branches. A command that ends by printing
# the stack runs one `git config --get-colorbool` to decide whether to color it.
# Publishing records two config keys for each new branch.
PUSH = {1: (9, 0), 3: (13, 0), 10: (27, 0)}
# ...and opening a pull request for each costs more of both.
PUSH_OPENING_PRS = {1: (9, 3), 3: (13, 7), 10: (27, 7)}
# With every pull request open, git and gh no longer grow with the stack, and gh stays at 4
# (2 for a stack of 1).
PUSH_WITH_PRS_OPEN = {1: (7, 3), 3: (7, 5), 10: (7, 5)}
PRS_ALL_OPEN = {1: (3, 3), 3: (3, 5), 10: (3, 5)}
# Nothing to do: gh is asked once, and git seven times.
SYNC_NOTHING = {1: (7, 1), 3: (7, 1), 10: (7, 1)}
# The bottom merged, as GitHub reports it (the merge commit and the head it had): the rest are
# restacked onto main and pushed again. The fake supplies the GitHub merge evidence.
# With --prs, sync also updates descriptions and links the surviving PRs.
SYNC_BOTTOM_MERGED = {1: (30, 1), 3: (62, 13), 10: (174, 13)}
# gh is asked twice, however many pull requests there are.
IMPORT = {1: (14, 2), 3: (28, 2), 10: (77, 2)}
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


@pytest.mark.parametrize("count", [2, 5])
def test_push_opening_prs_for_multiple_stacks_in_a_group(repo: Repo, gh: FakeGitHub, count: int) -> None:
    expected: list[tuple[str, str]] = []
    for i in range(count):
        repo.git("switch", "--quiet", "main")
        lower, upper = f"lower-{i}", f"upper-{i}"
        repo.client.branch(lower)
        repo.commit("a", f"{lower}\n", lower)
        repo.client.branch(upper)
        repo.commit("b", f"{upper}\n", upper)
        expected.extend(((lower, "main"), (upper, lower)))
    repo.client.group_add("work", [f"lower-{i}" for i in range(count)])

    with expect_operations(git=7 + 4 * count, gh=5):
        repo.stack_process("push", "--group", "work", "--prs")

    requests = gh.load()["prs"]
    assert sorted((pr["headRefName"], pr["baseRefName"]) for pr in requests) == sorted(expected)
    assert all(
        repo.git("rev-parse", branch, cwd=repo.origin) == repo.git("rev-parse", branch) for branch, _ in expected
    )
    assert all(all(f"#{other['number']}" in pr["body"] for other in requests) for pr in requests)


def test_push_when_every_branch_has_a_pull_request(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    git, gh_commands = PUSH_WITH_PRS_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("push", "--prs")

    # No new pull request: each branch has the one it had.
    assert_pull_requests(gh, chain)


def test_prs_when_every_branch_has_a_pull_request(repo: Repo, gh: FakeGitHub, chain: list[str]) -> None:
    open_pull_requests(repo)
    git, gh_commands = PRS_ALL_OPEN[len(chain)]

    with expect_operations(git=git, gh=gh_commands):
        repo.stack_process("prs")

    assert_pull_requests(gh, chain)


def test_sync_with_nothing_to_do(repo: Repo, chain: list[str]) -> None:
    open_pull_requests(repo)
    before = chain_tips(repo, chain)
    git, gh = SYNC_NOTHING[len(chain)]

    with expect_operations(git=git, gh=gh):
        repo.stack_process("sync", "--prs")

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
        repo.stack_process("sync", "--prs")

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
