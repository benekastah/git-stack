"""The GitHub adapter, with tests/fake_gh.py standing in for gh."""

from git_stack import events
from git_stack.stack import BranchStatus, StackClient
from tests.helpers import FakeGitHub, Repo


def merge_pr1_on_forge_with_a_tweak(repo: Repo, gh: FakeGitHub) -> None:
    """Squash-merge pr1 as pull request #5 with an extra change, so the merge
    doesn't match pr1's changes."""
    repo.client.push()
    repo.git("pull", "--quiet", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "origin/pr1", cwd=repo.forge)
    (repo.forge / "a").write_text("tweaked by the maintainer\n")
    repo.git("commit", "--quiet", "-am", "pr1 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr1", cwd=repo.forge)
    repo.git("config", "branch.pr1.stackPr", "5")
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=repo.git("rev-parse", "pr1"),
              baseRefName="main", mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)})
    gh.clear_calls()


def test_push_opens_draft_prs_for_current_branch_and_those_above(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    client.push()
    client.push()

    prs = gh.load()["prs"]
    assert [(p["headRefName"], p["baseRefName"], p["isDraft"]) for p in prs] == [
        ("pr2", "pr1", True), ("pr3", "pr2", True)]
    assert repo.config("pr2", "stackPr") == "1"
    assert repo.config("pr3", "stackPr") == "2"
    assert not repo.config_exists("pr1", "stackPr")
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == "pr1"
    assert all("github.com/acme/widgets" in call for call in gh.calls("pr"))


def test_push_from_bottom_opens_prs_for_whole_stack(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    client.push()

    assert [(p["number"], p["headRefName"], p["baseRefName"]) for p in gh.load()["prs"]] == [
        (1, "pr1", "main"), (2, "pr2", "pr1"), (3, "pr3", "pr2")]


def test_push_named_branch_opens_prs_from_it_up(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()

    client.push("pr2")

    assert [p["headRefName"] for p in gh.load()["prs"]] == ["pr2", "pr3"]
    assert repo.git("branch", "--show-current") == "pr3"


def test_push_whole_stack_opens_prs_from_its_bottom(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()

    client.push(whole_stack=True)

    assert [p["headRefName"] for p in gh.load()["prs"]] == ["pr1", "pr2", "pr3"]


def test_push_from_merged_branch_opens_prs_only_above_it(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr1")

    client.push()

    assert [(p["headRefName"], p["baseRefName"]) for p in gh.load()["prs"]] == [
        ("pr2", "main"), ("pr3", "pr2")]
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""


def test_push_records_existing_pr(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    gh.add_pr(number=7, state="OPEN", headRefName="pr3", headRefOid=None, baseRefName="pr2",
              mergeCommit=None)
    repo.git("switch", "--quiet", "pr3")

    client.push()

    assert repo.config("pr3", "stackPr") == "7"
    assert gh.calls("pr", "create") == []


def test_push_retargets_pr_whose_parent_changed(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    client.push()
    repo.squash_merge_on_forge("pr1")
    client.sync()

    client.push()

    assert events.PullRequestRetargeted("pr2", 1, "main") in emitted
    assert gh.load()["prs"][0]["baseRefName"] == "main"


def test_sync_trusts_merged_pr_whose_merge_differs_from_branch(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert len(gh.calls("api", "graphql")) == 1


def test_restack_ignores_merged_pr_until_parent_has_the_merge(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("fetch", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    client.restack()

    assert not any(isinstance(event, events.SkippedMerged) for event in emitted)
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_sync_skips_merged_pr_with_local_commits_after_merge(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "after merge\n", "pr1 after merge")

    client.sync()

    assert events.MergedWithNewCommits("pr1", "main") in emitted
    assert repo.config("pr1", "stackParent") == "main"


def test_open_pr_on_default_branch_overrides_local_detection(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("config", "branch.pr1.stackPr", "5")
    gh.add_pr(number=5, state="OPEN", headRefName="pr1", headRefOid=repo.git("rev-parse", "pr1"),
              baseRefName="main", mergeCommit=None)
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("push", "--quiet")

    assert repo.branch_info("pr1").status is BranchStatus.NEEDS_RESTACK


def test_open_pr_on_stacked_parent_falls_back_to_local_detection(
        repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("config", "branch.pr2.stackPr", "6")
    gh.add_pr(number=6, state="OPEN", headRefName="pr2", headRefOid=repo.git("rev-parse", "pr2"),
              baseRefName="pr1", mergeCommit=None)
    repo.git("switch", "--quiet", "pr1")
    repo.git("merge", "--quiet", "--squash", "pr2")
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)")
    repo.git("push", "--quiet")

    client.sync()

    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "pr1", ["pr3 c1"])


def test_failed_pr_lookup_falls_back_to_local_detection(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.git("config", "branch.pr1.stackPr", "5")
    repo.squash_merge_on_forge("pr1")
    gh.fail("api")

    client.sync()

    assert any(isinstance(event, events.PullRequestLookupFailed) for event in emitted)
    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_malformed_pr_falls_back_to_local_detection(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    client.push()
    repo.git("config", "branch.pr1.stackPr", "5")
    repo.squash_merge_on_forge("pr1")
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=None, baseRefName="main",
              mergeCommit=None)

    client.sync()

    assert any(isinstance(event, events.PullRequestLookupFailed)
               and "expected headRefOid to be a str" in event.error for event in emitted)
    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_git_adapter_setting_leaves_github_alone(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    repo.git("config", "stack.adapter", "git")

    client.push()
    client.sync()

    assert gh.load()["calls"] == []
    assert not repo.config_exists("pr3", "stackPr")
