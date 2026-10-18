"""git stack clean."""

from tests.helpers import Repo


def test_clean_untracks_merged_branches_in_every_stack_without_restacking(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("switch", "--quiet", "main")
    repo.stack("branch", "solo")
    repo.commit("c", "solo\n", "solo c1")
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.squash_merge_on_forge("solo")
    repo.git("switch", "--quiet", "main")
    pr2 = repo.git("rev-parse", "pr2")

    result = repo.stack("clean")

    assert "moved pr2 onto main" in result.stdout
    repo.assert_untracked("pr1")
    repo.assert_untracked("solo")
    assert repo.config("pr2", "stackParent") == "main"
    assert repo.git("rev-parse", "pr2") == pr2
    assert repo.git("rev-parse", "main") == repo.git("rev-parse", "origin/main")


def test_clean_delete_both_deletes_merged_branch(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    repo.git("switch", "--quiet", "pr1")

    repo.stack("clean", "--delete", "both")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""
    assert repo.git("branch", "--show-current") == "pr2"
