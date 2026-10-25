"""Groups of stacks: the stacks that were one until the branch at their bottom merged."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def make_siblings(repo: Repo, client: StackClient) -> None:
    """main <- a <- (b, c), pushed, then a squash-merged."""
    client.branch("a")
    repo.commit("a", "a\n", "a c1")
    client.branch("b")
    repo.commit("b", "b\n", "b c1")
    repo.git("switch", "--quiet", "a")
    client.branch("c")
    repo.commit("c", "c\n", "c c1")
    client.push()
    repo.git("switch", "--quiet", "b")
    client.push()
    repo.squash_merge_on_forge("a")


def test_sync_puts_children_of_a_merged_branch_on_main_in_its_group(repo: Repo, client: StackClient) -> None:
    make_siblings(repo, client)
    repo.git("switch", "--quiet", "b")

    client.sync()

    assert repo.config("b", "stackGroup") == "a"
    assert repo.config("c", "stackGroup") == "a"
    assert client.show_stack("b") == ["b", "c"]
    assert client.show_stack("c") == ["c", "b"]
    assert repo.branch_info("b").group == "a"


def test_rerun_after_a_conflict_still_syncs_every_stack_in_the_group(repo: Repo, client: StackClient) -> None:
    make_siblings(repo, client)
    # main also changes b, so restacking b onto main conflicts.
    repo.git("pull", "--quiet", cwd=repo.forge)
    (repo.forge / "b").write_text("main\n")
    repo.git("commit", "--quiet", "-am", "main edits b", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", cwd=repo.forge)
    repo.git("switch", "--quiet", "b")
    with pytest.raises(StackError, match="stopped on a conflict"):
        client.sync()
    repo.write("b", "b\n")
    repo.git("add", "b")
    repo.git("rebase", "--continue")

    client.sync()

    repo.assert_stacked("b", "main", ["b c1"])
    repo.assert_stacked("c", "main", ["c c1"])


def test_push_from_one_stack_in_a_group_pushes_them_all(repo: Repo, client: StackClient) -> None:
    make_siblings(repo, client)
    repo.git("switch", "--quiet", "b")
    client.sync()
    repo.commit("b", "b again\n", "b c2")
    repo.git("switch", "--quiet", "c")
    repo.commit("c", "c again\n", "c c2")

    client.push()

    assert repo.remote_subject("b") == "b c2"
    assert repo.remote_subject("c") == "c c2"


def test_children_of_a_merged_branch_in_a_group_stay_in_it(repo: Repo, client: StackClient) -> None:
    client.branch("a")
    repo.commit("a", "a\n", "a c1")
    client.branch("b")
    repo.commit("b", "b\n", "b c1")
    client.branch("b2")
    repo.commit("c", "b2\n", "b2 c1")
    repo.git("switch", "--quiet", "a")
    client.branch("c")
    repo.commit("a", "c\n", "c c1")
    client.push()
    repo.git("switch", "--quiet", "b2")
    client.push()
    repo.squash_merge_on_forge("a")
    client.sync()
    repo.squash_merge_on_forge("b")

    client.sync()

    assert repo.config("b2", "stackGroup") == "a"
    assert client.show_stack("c") == ["c", "b2"]


def test_branch_whose_merged_parent_had_only_it_on_it_shows_no_group(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "pr2")

    client.sync()

    assert repo.config("pr2", "stackGroup") == "pr1"
    assert repo.branch_info("pr2").group is None


def test_untracked_branch_leaves_its_group(repo: Repo, client: StackClient) -> None:
    make_siblings(repo, client)
    repo.git("switch", "--quiet", "b")
    client.sync()

    client.untrack(["c"])

    assert not repo.config_exists("c", "stackGroup")
    assert client.show_stack("b") == ["b"]
    assert repo.branch_info("b").group is None


def test_group_spans_stacks_on_different_roots(repo: Repo, client: StackClient) -> None:
    repo.git("branch", "release", "main")
    repo.git("push", "--quiet", "--set-upstream", "origin", "release")
    make_siblings(repo, client)
    repo.git("switch", "--quiet", "b")
    client.sync()
    repo.git("switch", "--quiet", "c")
    client.restack(onto="release")
    # release moves on the remote, so syncing from b fast-forwards it too.
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-C", "release", "origin/release", cwd=repo.forge)
    (repo.forge / "d").write_text("release\n")
    repo.git("add", "d", cwd=repo.forge)
    repo.git("commit", "--quiet", "-m", "release fix", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "release", cwd=repo.forge)
    repo.git("switch", "--quiet", "b")

    client.sync()

    assert client.show_stack("b") == ["b", "c"]
    assert repo.git("rev-parse", "release") == repo.git("rev-parse", "origin/release")
    assert repo.branch_info("b").group == "a"
    assert repo.branch_info("c").group == "a"
