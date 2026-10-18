"""The GitHub adapter, with tests/fake_gh.py standing in for gh."""

from tests.helpers import FakeGitHub, Repo


def merge_pr1_on_forge_with_a_tweak(repo: Repo, gh: FakeGitHub) -> None:
    """Squash-merge pr1 as pull request #5 with an extra change, so the merge
    doesn't match pr1's changes."""
    repo.stack("push")
    repo.git("pull", "--quiet", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "origin/pr1", cwd=repo.forge)
    (repo.forge / "a").write_text("tweaked by the maintainer\n")
    repo.git("commit", "--quiet", "-am", "pr1 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr1", cwd=repo.forge)
    repo.git("config", "branch.pr1.stackPr", "5")
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=repo.git("rev-parse", "pr1"),
              baseRefName="main", mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)})
    gh.clear_calls()


def test_push_opens_draft_pr_for_current_branch_only(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    repo.stack("push")
    repo.stack("push")

    prs = gh.load()["prs"]
    assert [(p["headRefName"], p["baseRefName"], p["isDraft"]) for p in prs] == [("pr2", "pr1", True)]
    assert repo.config("pr2", "stackPr") == "1"
    assert not repo.config_exists("pr1", "stackPr")
    assert not repo.config_exists("pr3", "stackPr")
    assert len(gh.calls("pr", "create")) == 1
    assert all("github.com/acme/widgets" in call for call in gh.calls("pr"))


def test_push_from_merged_branch_opens_no_pr(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr1")

    repo.stack("push")

    assert gh.calls("pr", "create") == []
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""


def test_push_records_existing_pr(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    gh.add_pr(number=7, state="OPEN", headRefName="pr1", headRefOid=None, baseRefName="main",
                mergeCommit=None)
    repo.git("switch", "--quiet", "pr1")

    repo.stack("push")

    assert repo.config("pr1", "stackPr") == "7"
    assert gh.calls("pr", "create") == []


def test_push_retargets_pr_whose_parent_changed(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")
    repo.stack("sync")

    result = repo.stack("push")

    assert "changed the base of pr2's pull request #1 to main" in result.stdout
    assert gh.load()["prs"][0]["baseRefName"] == "main"


def test_sync_trusts_merged_pr_whose_merge_differs_from_branch(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)

    repo.stack("sync")

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert len(gh.calls("api", "graphql")) == 1


def test_restack_ignores_merged_pr_until_parent_has_the_merge(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("fetch", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    result = repo.stack("restack")

    assert "looks merged" not in result.stderr
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_sync_skips_merged_pr_with_local_commits_after_merge(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "after merge\n", "pr1 after merge")

    result = repo.stack("sync")

    assert "skipping pr1: it was merged into main, but it or its remote branch has commits" in result.stderr
    assert repo.config("pr1", "stackParent") == "main"


def test_open_pr_on_default_branch_overrides_local_detection(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("config", "branch.pr1.stackPr", "5")
    gh.add_pr(number=5, state="OPEN", headRefName="pr1", headRefOid=repo.git("rev-parse", "pr1"),
                baseRefName="main", mergeCommit=None)
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("push", "--quiet")

    assert "└── pr1 (needs restack)\n" in repo.stack("info").stdout


def test_open_pr_on_stacked_parent_falls_back_to_local_detection(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("config", "branch.pr2.stackPr", "6")
    gh.add_pr(number=6, state="OPEN", headRefName="pr2", headRefOid=repo.git("rev-parse", "pr2"),
                baseRefName="pr1", mergeCommit=None)
    repo.git("switch", "--quiet", "pr1")
    repo.git("merge", "--quiet", "--squash", "pr2")
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)")
    repo.git("push", "--quiet")

    repo.stack("sync")

    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "pr1", ["pr3 c1"])


def test_failed_pr_lookup_falls_back_to_local_detection(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("config", "branch.pr1.stackPr", "5")
    repo.squash_merge_on_forge("pr1")
    gh.fail("api")

    result = repo.stack("sync")

    assert "couldn't look up pull requests on GitHub" in result.stderr
    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_malformed_pr_falls_back_to_local_detection(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("config", "branch.pr1.stackPr", "5")
    repo.squash_merge_on_forge("pr1")
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=None, baseRefName="main",
                mergeCommit=None)

    result = repo.stack("sync")

    assert "expected headRefOid to be a str" in result.stderr
    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_git_adapter_setting_leaves_github_alone(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("config", "stack.adapter", "git")

    repo.stack("push")
    repo.stack("sync")

    assert gh.load()["calls"] == []
    assert not repo.config_exists("pr3", "stackPr")
