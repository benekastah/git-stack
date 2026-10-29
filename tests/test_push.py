"""git stack push."""

import pytest

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_push_refuses_to_drop_unpulled_commits(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.git("fetch", "--quiet")

    with pytest.raises(StackError, match="origin/pr1 has commits that pr1 doesn't"):
        client.push()

    assert repo.remote_subject("pr1") == "collab"


def test_push_refuses_when_remote_moved_since_last_fetch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    with pytest.raises(StackError):
        client.push()

    assert repo.remote_subject("pr1") == "collab"


def test_push_pushes_no_branch_if_one_cant_be_pushed(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr3")
    repo.commit("c", "pr3 again\n", "pr3 c2")

    with pytest.raises(StackError, match="nothing was pushed"):
        client.push()

    assert repo.remote_subject("pr1") == "collab"
    assert repo.remote_subject("pr3") == "pr3 c1"


def test_push_force_drops_unpulled_commits(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.git("fetch", "--quiet")

    client.push(force=True)

    assert repo.remote_subject("pr1") == "pr1 c3"


def test_push_force_refuses_when_remote_moved_since_last_fetch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    with pytest.raises(StackError):
        client.push(force=True)

    assert repo.remote_subject("pr1") == "collab"


def edit_near_on_main_then_rebase_on_forge(repo: Repo, client: StackClient) -> str:
    """Stack pr1 on main, editing a line of a file near one main then edits on
    the remote, so the commit rebased there differs from pr1's in more than
    its line numbers. Push pr1, then rebase it on the remote. Returns the
    rebased tip."""
    repo.write("d", "".join(f"line {n}\n" for n in range(1, 8)))
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "add d")
    repo.git("push", "--quiet")
    client.branch("pr1")
    repo.commit("d", (repo.path / "d").read_text().replace("line 5", "pr1"), "pr1 c1")
    client.push()
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "main", cwd=repo.forge)
    repo.git("pull", "--quiet", cwd=repo.forge)
    repo.commit_on_forge("main", "d", (repo.forge / "d").read_text().replace("line 2", "main"), "main c1")
    repo.rebase_stack_on_forge("pr1")
    return repo.git("rev-parse", "pr1", cwd=repo.origin)


def test_push_takes_a_rebase_from_the_remote(repo: Repo, client: StackClient) -> None:
    rebased = edit_near_on_main_then_rebase_on_forge(repo, client)

    client.push()

    assert repo.git("rev-parse", "pr1") == rebased == repo.git("rev-parse", "pr1", cwd=repo.origin)
    assert repo.config("pr1", "stackBase") == repo.git("rev-parse", "origin/main")


def test_push_doesnt_take_a_rebase_over_local_commits(repo: Repo, client: StackClient) -> None:
    rebased = edit_near_on_main_then_rebase_on_forge(repo, client)
    repo.commit("a", "pr1 again\n", "pr1 c2")

    with pytest.raises(StackError, match="origin/pr1 has commits that pr1 doesn't"):
        client.push()

    assert repo.git("rev-parse", "pr1", cwd=repo.origin) == rebased


def test_push_force_pushes_over_a_rebase_on_the_remote(repo: Repo, client: StackClient) -> None:
    edit_near_on_main_then_rebase_on_forge(repo, client)
    local = repo.git("rev-parse", "pr1")

    client.push(force=True)

    assert repo.git("rev-parse", "pr1", cwd=repo.origin) == local


@pytest.mark.parametrize("force", [False, True])
def test_push_refuses_to_overwrite_someone_elses_branch_of_the_same_name(
    repo: Repo, client: StackClient, force: bool
) -> None:
    client.branch("fix")
    repo.commit("a", "mine\n", "mine")
    repo.push_someone_elses_branch("fix")

    with pytest.raises(StackError, match="origin/fix exists but isn't fix's upstream"):
        client.push(force=force)

    assert repo.remote_subject("fix") == "theirs"


def test_push_refuses_outside_a_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.push()

    assert repo.git("branch", "--list", cwd=repo.origin) == "* main"


def test_push_refuses_untracked_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()

    with pytest.raises(StackError, match="main isn't in a stack"):
        client.push(["main"])


def test_push_skips_branch_merged_locally(repo: Repo, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    client.push()

    assert events.SkippedMerged("pr1", "main") in emitted
    assert repo.git("branch", "--list", "--format=%(refname:short)", cwd=repo.origin) == "main\npr2\npr3"


def test_push_refuses_to_recreate_deleted_remote_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("fetch", "--quiet", "--prune")
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "pr2 again\n", "pr2 c2")

    with pytest.raises(StackError, match="pr1's remote branch was deleted"):
        client.push()

    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.remote_subject("pr2") == "pr2 c1"


def test_push_pushes_every_branch_in_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    client.push()

    for branch in ("pr1", "pr2", "pr3"):
        assert repo.git("rev-parse", f"origin/{branch}") == repo.git("rev-parse", branch)
        assert repo.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") == f"origin/{branch}"


def test_push_named_branch_pushes_its_stack(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    client.push(["pr2"])

    for branch in ("pr1", "pr2", "pr3"):
        assert repo.git("rev-parse", f"origin/{branch}") == repo.git("rev-parse", branch)
    assert repo.git("branch", "--show-current") == "main"


def test_push_skips_reverted_branch_and_its_children(
    repo: Repo, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    repo.git("switch", "--quiet", "main")
    repo.git("pull", "--quiet")
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "pr2 again\n", "pr2 c2")

    assert client.push().pushed == []
    assert events.Reverted("pr1", "main") in emitted
    assert events.ParentSkipped("pr2", "pr1") in emitted
    assert repo.remote_subject("pr2") == "pr2 c1"
