"""The GitHub adapter, with tests/fake_gh.py standing in for gh."""

import os
import pathlib

import pytest

from git_stack import events
from git_stack.adapters.github import github_repo
from git_stack.errors import StackError
from git_stack.git import GitClient
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
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=repo.git("rev-parse", "pr1"),
              baseRefName="main", mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)})
    gh.clear_calls()


def compare_url(base: str, head: str) -> str:
    return f"https://github.com/acme/widgets/compare/{base}...{head}?expand=1"


def test_push_links_to_opening_prs_for_branches_without_one(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    client.push()

    assert [e for e in emitted if isinstance(e, events.NoPullRequest)] == [
        events.NoPullRequest("pr1", compare_url("main", "pr1")),
        events.NoPullRequest("pr2", compare_url("pr1", "pr2")),
        events.NoPullRequest("pr3", compare_url("pr2", "pr3"))]
    assert gh.calls("pr") == []


def test_push_link_quotes_branch_names(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    client.branch("feature/50%")
    repo.commit("a", "feature\n", "feature c1")

    client.push()

    assert events.NoPullRequest("feature/50%", compare_url("main", "feature/50%25")) in emitted


def test_push_retargets_pr_opened_elsewhere_instead_of_linking(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    gh.add_pr(number=7, state="OPEN", headRefName="pr2", headRefOid=repo.git("rev-parse", "main"), baseRefName="main",
              mergeCommit=None)

    client.push()

    assert events.PullRequestRetargeted("pr2", 7, "pr1") in emitted
    assert gh.load()["prs"][0]["baseRefName"] == "pr1"
    assert [e.branch for e in emitted if isinstance(e, events.NoPullRequest)] == ["pr1", "pr3"]


def test_push_links_to_new_pr_for_branch_whose_pr_was_closed(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    gh.add_pr(number=7, state="CLOSED", headRefName="pr3", headRefOid=repo.git("rev-parse", "main"), baseRefName="pr2",
              mergeCommit=None)

    client.push()

    assert events.NoPullRequest("pr3", compare_url("pr2", "pr3")) in emitted
    assert gh.calls("pr", "edit") == []


def test_push_ignores_pr_from_fork_with_same_branch_name(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    gh.add_pr(number=7, state="OPEN", isCrossRepository=True, headRefName="pr3", headRefOid=repo.git("rev-parse", "main"),
              baseRefName="main", mergeCommit=None)

    client.push()

    assert events.NoPullRequest("pr3", compare_url("pr2", "pr3")) in emitted
    assert gh.calls("pr", "edit") == []


def test_merged_pr_of_earlier_branch_with_same_name_is_ignored(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.commit("a", "old pr1\n", "old pr1")
    old_pr1 = repo.git("rev-parse", "HEAD")
    repo.commit("a", "later\n", "later")
    repo.git("push", "--quiet")
    gh.add_pr(number=3, state="MERGED", headRefName="pr1", headRefOid=old_pr1, baseRefName="main",
              mergeCommit={"oid": old_pr1})
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    client.sync()
    client.push()

    assert not any(isinstance(event, events.MergedWithNewCommits) for event in emitted)
    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
    assert events.NoPullRequest("pr1", compare_url("main", "pr1")) in emitted


def test_push_after_failed_pr_lookup_leaves_prs_alone(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    gh.fail("api")

    with pytest.raises(StackError, match="couldn't look up pull requests"):
        client.push()

    assert repo.git("branch", "--list", "pr3", cwd=repo.origin) != ""
    assert gh.calls("pr") == []


def test_push_retargets_pr_whose_parent_changed(
        repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    client.push()
    gh.add_pr(number=1, state="OPEN", headRefName="pr2", headRefOid=repo.git("rev-parse", "pr2"),
              baseRefName="pr1", mergeCommit=None)
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


@pytest.fixture
def fake_ssh(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ssh whose config has `Host github-work` with `HostName github.com`."""
    bin_dir = tmp_path / "ssh-bin"
    bin_dir.mkdir()
    ssh = bin_dir / "ssh"
    ssh.write_text('#!/bin/sh\n[ "$1" = -G ] || exit 255\n'
                   'if [ "$2" = github-work ]; then echo "hostname github.com"; else echo "hostname $2"; fi\n')
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


@pytest.mark.parametrize("url", ["git@github-work:acme/widgets.git", "ssh://git@github-work/acme/widgets.git"])
def test_github_repo_resolves_ssh_host_alias(repo: Repo, fake_ssh: None, url: str) -> None:
    repo.git("config", "remote.origin.url", url)

    assert github_repo(GitClient(repo.path), "origin") == ("github.com", "acme", "widgets")


def test_github_repo_leaves_https_host_alone(repo: Repo, fake_ssh: None) -> None:
    repo.git("config", "remote.origin.url", "https://github-work/acme/widgets.git")

    assert github_repo(GitClient(repo.path), "origin") == ("github-work", "acme", "widgets")
