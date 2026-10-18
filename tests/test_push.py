"""git stack push."""

from tests.helpers import Repo


def test_push_refuses_to_drop_unpulled_commits(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")
    repo.git("fetch", "--quiet")

    result = repo.stack("push", expect_success=False)

    assert "origin/pr1 has commits that pr1 doesn't" in result.stderr
    assert repo.remote_subject("pr1") == "collab"


def test_push_refuses_when_remote_moved_since_last_fetch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.commit_on_forge("pr1", "d", "collab\n", "collab")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    repo.stack("push", expect_success=False)

    assert repo.remote_subject("pr1") == "collab"


def test_push_refuses_outside_a_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")

    result = repo.stack("push", expect_success=False)

    assert "main isn't in a stack" in result.stderr
    assert repo.git("branch", "--list", cwd=repo.origin) == "* main"


def test_push_skips_branch_merged_locally(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    result = repo.stack("push")

    assert "pr1 looks merged into main" in result.stderr
    assert repo.git("branch", "--list", "--format=%(refname:short)", cwd=repo.origin) == "main\npr2\npr3"


def test_push_refuses_to_recreate_deleted_remote_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.git("fetch", "--quiet", "--prune")
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "pr2 again\n", "pr2 c2")

    result = repo.stack("push", expect_success=False)

    assert "pr1's remote branch was deleted" in result.stderr
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.remote_subject("pr2") == "pr2 c1"


def test_push_pushes_every_branch_in_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    repo.stack("push")

    for branch in ("pr1", "pr2", "pr3"):
        assert repo.git("rev-parse", f"origin/{branch}") == repo.git("rev-parse", branch)
        assert repo.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") == f"origin/{branch}"
