"""git stack clean."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_clean_untracks_merged_branches_in_every_stack_without_restacking(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "main")
    client.branch("solo")
    repo.commit("c", "solo\n", "solo c1")
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.squash_merge_on_forge("solo")
    repo.git("switch", "--quiet", "main")
    pr2 = repo.git("rev-parse", "pr2")

    branches = client.clean()

    assert [(b.name, b.parent) for b in branches] == [("main", None), ("pr2", "main"), ("pr3", "pr2")]

    assert events.Untracked("pr1", "main", ("pr2",), deleted_tip=None) in emitted
    repo.assert_untracked("pr1")
    repo.assert_untracked("solo")
    assert repo.config("pr2", "stackParent") == "main"
    assert repo.git("rev-parse", "pr2") == pr2
    assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")


def test_clean_delete_both_deletes_merged_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    repo.git("switch", "--quiet", "pr1")

    client.clean(delete="both")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.git("branch", "--show-current") == "pr2"


def test_clean_keeps_tracking_reverted_branch(repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")

    client.clean()

    assert events.Reverted("pr1", "main") in emitted
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_clean_ignore_revert_untracks_reverted_branch_as_merged(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")

    client.clean(ignore_reverts=["pr1"])

    repo.assert_untracked("pr1")
    assert repo.config("pr2", "stackParent") == "main"


def test_clean_ignore_revert_refuses_branch_with_no_revert_to_ignore(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="there's no revert of nope to ignore in any stack"):
        client.clean(ignore_reverts=["nope"])


@pytest.mark.parametrize(
    ("checkout", "delete"),
    [
        ("main", None),  # a root, which clean fast-forwards
        ("pr3", "local"),  # a tracked branch, which clean could delete
        ("pr3", "both"),
    ],
)
def test_clean_refuses_uncommitted_changes_it_could_get_in_the_way_of(
    repo: Repo, client: StackClient, checkout: str, delete: str | None
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", checkout)
    repo.write("a", "uncommitted\n")

    with pytest.raises(StackError, match="uncommitted changes"):
        client.clean(delete=delete)

    assert repo.config("pr1", "stackParent") == "main"


@pytest.mark.parametrize(
    ("checkout", "delete"),
    [
        ("pr3", None),  # tracked, but not deleted
        ("feature", "local"),  # neither a root nor tracked
    ],
)
def test_clean_runs_with_uncommitted_changes_it_doesnt_touch(
    repo: Repo, client: StackClient, checkout: str, delete: str | None
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("switch", "--quiet", "-C", "feature", "pr3")
    repo.git("switch", "--quiet", checkout)
    repo.write("c", "uncommitted\n")

    client.clean(delete=delete)

    assert not repo.config_exists("pr1", "stackParent")
    assert repo.git("branch", "--show-current") == checkout
    assert (repo.path / "c").read_text() == "uncommitted\n"
