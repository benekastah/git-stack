"""The GitHub adapter, with tests/fake_gh.py standing in for gh."""

import pathlib
import subprocess

import pytest

from git_stack import events
from git_stack.adapters.git import PullRequestLink, PullRequestState
from git_stack.adapters.github import PullRequest, github_repo
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchStatus, StackClient
from tests.helpers import FakeGitHub, Repo, install_command


def merge_pr1_on_forge_with_a_tweak(repo: Repo, gh: FakeGitHub) -> None:
    """Squash-merge pr1 as pull request #5 with an extra change, so the merge
    doesn't match pr1's changes."""
    repo.client.push()
    repo.git("pull", "--quiet", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "origin/pr1", cwd=repo.forge)
    (repo.forge / "a").write_text("tweaked by the maintainer\n")
    repo.git("commit", "--quiet", "-am", "pr1 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr1", cwd=repo.forge)
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="pr1",
        headRefOid=repo.git("rev-parse", "pr1"),
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    )
    gh.clear_calls()


def without_prs(emitted: list[events.Event]) -> list[str]:
    """The branches push said have no pull request."""
    return [b for e in emitted if isinstance(e, events.NoPullRequests) for b in e.branches]


def test_lookup_finds_open_pr_after_newer_closed_and_fork_prs(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1")
    client.push()
    head = repo.git("rev-parse", "pr1")
    gh.add_pr(number=1, state="OPEN", headRefName="pr1", headRefOid=head, baseRefName="main", mergeCommit=None)
    for number in range(2, 23):
        gh.add_pr(
            number=number,
            state="OPEN" if number % 2 else "CLOSED",
            isCrossRepository=True,
            headRefName="pr1",
            headRefOid=head,
            baseRefName="main",
            mergeCommit=None,
        )

    info = client.info(["pr1"])

    assert info[1].pull_request is not None
    assert info[1].pull_request.number == 1


def test_sync_respects_a_pull_request_with_a_differently_named_upstream(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    client.branch("local-feature")
    repo.commit("a", "first\n", "first")
    repo.git("push", "--quiet", "--set-upstream", "origin", "local-feature:remote-feature")
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="remote-feature",
        headRefOid=repo.git("rev-parse", "local-feature"),
        baseRefName="main",
        mergeCommit=None,
    )
    repo.commit("a", "second\n", "second")

    client.sync()

    assert repo.git("show", "remote-feature:a", cwd=repo.origin) == "second"
    assert client.info(["local-feature"])[1].pull_request is not None
    assert not gh.calls("pr", "edit")


def test_sync_keeps_a_pr_based_on_its_parents_upstream_name(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("local-parent")
    repo.commit("a", "parent\n", "parent")
    repo.git("push", "--quiet", "--set-upstream", "origin", "local-parent:remote-parent")
    client.branch("local-child")
    repo.commit("b", "child\n", "child")
    repo.git("push", "--quiet", "--set-upstream", "origin", "local-child:remote-child")
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="remote-child",
        headRefOid=repo.git("rev-parse", "local-child"),
        baseRefName="remote-parent",
        mergeCommit=None,
    )
    repo.commit("b", "child updated\n", "child updated")

    client.sync()

    assert repo.git("show", "remote-child:b", cwd=repo.origin) == "child updated"
    assert not gh.calls("pr", "edit")


def test_prs_opens_for_the_configured_upstream_name(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    client.branch("local-feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("push", "--quiet", "--set-upstream", "origin", "local-feature:remote-feature")

    result = client.prs()

    assert result.steps[0].number is not None
    assert gh.load()["prs"][0]["headRefName"] == "remote-feature"


def test_sync_wont_guess_a_pr_base_from_another_remote(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("remote", "add", "other", str(repo.origin))
    repo.git("fetch", "--quiet", "other")
    repo.git("branch", "--set-upstream-to=other/pr1", "pr1")
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "updated\n", "updated")
    before = repo.git("rev-parse", "pr2", cwd=repo.origin)

    with pytest.raises(StackError, match="parent pr1 tracks other"):
        client.sync(remote="origin", only_remote=True)
    assert repo.git("rev-parse", "pr2", cwd=repo.origin) == before
    assert not gh.calls("pr", "edit")


def test_push_says_which_branches_have_no_pr(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")

    client.push()

    assert [e for e in emitted if isinstance(e, events.NoPullRequests)] == [
        events.NoPullRequests(("pr1", "pr2", "pr3"))
    ]
    assert gh.calls("pr") == []


def test_push_retargets_pr_opened_elsewhere(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    emitted.clear()
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="main",
        mergeCommit=None,
    )

    client.push()

    assert events.PullRequestRetargeted("pr2", 7, "pr1") in emitted
    assert gh.load()["prs"][0]["baseRefName"] == "pr1"
    assert without_prs(emitted) == ["pr1", "pr3"]


def test_push_counts_branch_whose_pr_was_closed_as_without_one(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    gh.add_pr(
        number=7,
        state="CLOSED",
        headRefName="pr3",
        headRefOid=repo.git("rev-parse", "main"),
        baseRefName="pr2",
        mergeCommit=None,
    )

    client.push()

    assert "pr3" in without_prs(emitted)
    assert gh.calls("pr", "edit") == []


def test_push_ignores_pr_from_fork_with_same_branch_name(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    gh.add_pr(
        number=7,
        state="OPEN",
        isCrossRepository=True,
        headRefName="pr3",
        headRefOid=repo.git("rev-parse", "main"),
        baseRefName="main",
        mergeCommit=None,
    )

    client.push()

    assert "pr3" in without_prs(emitted)
    assert gh.calls("pr", "edit") == []


def test_merged_pr_of_earlier_branch_with_same_name_is_ignored(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.commit("a", "old pr1\n", "old pr1")
    old_pr1 = repo.git("rev-parse", "HEAD")
    repo.commit("a", "later\n", "later")
    repo.git("push", "--quiet")
    gh.add_pr(
        number=3,
        state="MERGED",
        headRefName="pr1",
        headRefOid=old_pr1,
        baseRefName="main",
        mergeCommit={"oid": old_pr1},
    )
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    client.push()
    emitted.clear()

    client.sync()
    client.push()

    assert not any(isinstance(event, events.MergedWithNewCommits) for event in emitted)
    repo.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
    assert "pr1" in without_prs(emitted)


def test_merged_pr_from_someone_elses_branch_with_same_name_is_ignored(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    client.branch("fix")
    repo.commit("a", "mine\n", "mine")
    repo.push_someone_elses_branch("fix")
    theirs = repo.git("rev-parse", "origin/fix")
    repo.git("push", "--quiet", "origin", "fix:main", cwd=repo.forge)
    gh.add_pr(
        number=9, state="MERGED", headRefName="fix", headRefOid=theirs, baseRefName="main", mergeCommit={"oid": theirs}
    )

    client.sync()

    assert not any(isinstance(event, events.MergedWithNewCommits) for event in emitted)
    assert repo.config("fix", "stackParent") == "main"
    assert repo.own_commits("fix", "main") == ["mine"]


def test_push_refuses_when_pr_lookup_fails(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr3")
    repo.commit("c", "pr3 again\n", "pr3 c2")
    gh.fail("api")

    with pytest.raises(StackError, match="couldn't look up pull requests on GitHub"):
        client.push()

    assert repo.remote_subject("pr3") == "pr3 c1"


def test_push_retargets_pr_whose_parent_changed(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    client.push()
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    repo.squash_merge_on_forge("pr1")
    client.sync()

    client.push()

    assert events.PullRequestRetargeted("pr2", 1, "main") in emitted
    assert gh.load()["prs"][0]["baseRefName"] == "main"


def record_retargets_and_pushes(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record, in order, each pull request client retargets and each push it
    makes, as "retarget <branch>" and "push"."""
    order: list[str] = []
    client = repo.client

    def on_event(event: events.Event) -> None:
        if isinstance(event, events.PullRequestRetargeted):
            order.append(f"retarget {event.branch}")
        repo.emitted.append(event)

    passthrough = client.git.passthrough

    def recording_passthrough(*args: str) -> bool:
        if args[0] == "push":
            order.append("push")
        return passthrough(*args)

    monkeypatch.setattr(client, "on_event", on_event)
    monkeypatch.setattr(client.git, "passthrough", recording_passthrough)
    return order


def test_push_retargets_pr_before_pushing_when_parent_was_merged(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    client.push()
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    client.sync(push=False)
    order = record_retargets_and_pushes(repo, monkeypatch)

    client.push()

    assert order == ["retarget pr2", "push"]
    assert gh.load()["prs"][0]["baseRefName"] == "main"


def stack_whose_pull_requests_push_retargets_first(repo: Repo, gh: FakeGitHub) -> None:
    """pr1 <- pr2 <- pr3, pushed, with pull requests #1 for pr2 (on pr1) and #2
    for pr3 (on main, as if retargeted by hand). pr1 is then merged and
    pulled, which doesn't push, so push retargets #1 onto main and #2 onto pr2
    before it pushes."""
    repo.make_stack()
    repo.client.push()
    for number, branch, base in ((1, "pr2", "pr1"), (2, "pr3", "main")):
        gh.add_pr(
            number=number,
            state="OPEN",
            headRefName=branch,
            headRefOid=repo.git("rev-parse", branch),
            baseRefName=base,
            mergeCommit=None,
        )
    repo.squash_merge_on_forge("pr1")
    repo.stack("pull")
    repo.git("switch", "--quiet", "pr3")
    repo.commit("c", "pr3 again\n", "pr3 c2")


def bases(gh: FakeGitHub) -> dict[int, str]:
    return {pr["number"]: pr["baseRefName"] for pr in gh.load()["prs"]}


def test_push_reports_retargets_it_left_when_the_push_fails(repo: Repo, gh: FakeGitHub) -> None:
    stack_whose_pull_requests_push_retargets_first(repo, gh)
    # Refused by the remote itself, as a protected branch would be, which
    # only the real push finds out.
    repo.add_hook(repo.origin, "pre-receive", "#!/bin/sh\nexit 1\n")

    result = repo.stack("push", expect_success=False)

    assert "push failed, so nothing was pushed" in result.stderr
    assert bases(gh) == {1: "main", 2: "pr2"}
    assert "#1's base was changed from pr1 to main" in result.stderr
    assert "gh pr edit 2 --repo github.com/acme/widgets --base main" in result.stderr


def test_push_reports_retargets_it_left_when_a_later_one_fails(repo: Repo, gh: FakeGitHub) -> None:
    stack_whose_pull_requests_push_retargets_first(repo, gh)
    gh.fail_editing(2)
    pr3 = repo.git("rev-parse", "pr3", cwd=repo.origin)

    result = repo.stack("push", expect_success=False)

    assert "updating pull requests failed, so nothing was pushed" in result.stderr
    assert bases(gh) == {1: "main", 2: "main"}
    assert "#1's base was changed from pr1 to main" in result.stderr
    assert "#2's base" not in result.stderr
    assert repo.git("rev-parse", "pr3", cwd=repo.origin) == pr3


def test_push_reports_github_stacks_it_left_unstacked_when_the_push_fails(repo: Repo, gh: FakeGitHub) -> None:
    stack_whose_pull_requests_push_retargets_first(repo, gh)
    # Their bases can only change once they're out of their GitHub stack.
    gh.enable_stacks([1, 2])
    repo.add_hook(repo.origin, "pre-receive", "#!/bin/sh\nexit 1\n")

    result = repo.stack("push", "--rebuild-stack", expect_success=False)

    assert gh.stacks() == []
    assert "GitHub stack 1000 was unstacked (`git stack prs` links its pull requests" in result.stderr


def test_push_runs_the_pre_push_hook_once(repo: Repo, gh: FakeGitHub) -> None:
    stack_whose_pull_requests_push_retargets_first(repo, gh)
    runs = repo.tmp / "pre-push-runs"
    repo.add_hook(repo.path, "pre-push", f'#!/bin/sh\necho run >> "{runs.as_posix()}"\n')

    repo.stack("push")

    assert runs.read_text().splitlines() == ["run"]


def test_push_changes_no_pull_request_when_the_push_would_fail(repo: Repo, gh: FakeGitHub) -> None:
    stack_whose_pull_requests_push_retargets_first(repo, gh)
    # Someone pushes to pr3 after we last fetched.
    repo.commit_on_forge("pr3", "d", "theirs\n", "theirs")

    result = repo.stack("push", expect_success=False)

    assert "push would fail, so nothing was pushed or changed" in result.stderr
    assert "pr3 -> pr3 (stale info)" in result.stderr
    assert bases(gh) == {1: "pr1", 2: "main"}
    assert gh.calls("pr", "edit") == []


@pytest.mark.parametrize("no_stack_field", [False, True])
def test_push_retargets_pr_after_pushing_new_parent(
    repo: Repo, gh: FakeGitHub, client: StackClient, monkeypatch: pytest.MonkeyPatch, no_stack_field: bool
) -> None:
    state = gh.load()
    state["no_stack_field"] = no_stack_field
    gh.save(state)
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    repo.git("switch", "--quiet", "main")
    client.branch("other")
    repo.commit("a", "other\n", "other c1")
    repo.git("switch", "--quiet", "pr2")
    client.restack(onto="other")
    order = record_retargets_and_pushes(repo, monkeypatch)

    client.push()

    assert order == ["push", "retarget pr2"]
    assert gh.load()["prs"][0]["baseRefName"] == "other"


def test_sync_trusts_merged_pr_whose_merge_differs_from_branch(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])
    repo.assert_stacked("pr3", "pr2", ["pr3 c1"])
    assert len(gh.calls("api", "graphql")) == 1


def merge_pr1_after_update_branch(repo: Repo, gh: FakeGitHub) -> str:
    """On GitHub: "Update branch" merges a new main into pr1, then pr1 is
    squash-merged as pull request #5 with a tweak (so its changes don't match
    pr1's) and its branch deleted, all before we fetch the updated pr1.
    Returns pr1's updated head."""
    repo.client.push()
    repo.git("pull", "--quiet", cwd=repo.forge)
    repo.commit_on_forge("main", "d", "main moved\n", "main moved")
    repo.git("fetch", "--quiet", cwd=repo.forge)
    repo.git("switch", "--quiet", "-C", "pr1", "origin/pr1", cwd=repo.forge)
    repo.git("merge", "--quiet", "--no-edit", "origin/main", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "pr1", cwd=repo.forge)
    updated_head = repo.git("rev-parse", "HEAD", cwd=repo.forge)
    repo.git("switch", "--quiet", "-C", "main", "origin/main", cwd=repo.forge)
    repo.git("merge", "--quiet", "--squash", "pr1", cwd=repo.forge)
    (repo.forge / "a").write_text("tweaked by the maintainer\n")
    repo.git("commit", "--quiet", "-am", "pr1 (squashed)", cwd=repo.forge)
    repo.git("push", "--quiet", "origin", "main", ":pr1", cwd=repo.forge)
    repo.git("update-ref", "refs/pull/5/head", updated_head, cwd=repo.origin)
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="pr1",
        headRefOid=updated_head,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    )
    return updated_head


def test_sync_fetches_head_of_merged_pr_whose_branch_was_deleted(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    merge_pr1_after_update_branch(repo, gh)

    client.sync()

    repo.assert_untracked("pr1")
    repo.assert_stacked("pr2", "main", ["pr2 c1"])


def test_info_links_pull_requests(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    gh.add_pr(
        number=8,
        state="OPEN",
        isDraft=True,
        headRefName="pr3",
        headRefOid=repo.git("rev-parse", "pr3"),
        baseRefName="pr2",
        mergeCommit=None,
    )

    assert [(b.name, b.pull_request) for b in client.info()] == [
        ("main", None),
        ("pr1", None),
        ("pr2", PullRequestLink(7, "https://github.com/acme/widgets/pull/7", PullRequestState.OPEN)),
        ("pr3", PullRequestLink(8, "https://github.com/acme/widgets/pull/8", PullRequestState.DRAFT)),
    ]


def test_info_offline_does_not_ask_github(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    gh.clear_calls()

    assert [b.pull_request for b in client.info(offline=True)] == [None] * 4
    assert gh.calls() == []


def test_info_does_not_fetch_head_of_merged_pr(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    updated_head = merge_pr1_after_update_branch(repo, gh)
    repo.git("fetch", "--quiet", "--prune")

    client.info()

    assert client.git.rev(updated_head) is None


def test_restack_ignores_merged_pr_until_parent_has_the_merge(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("fetch", "--quiet")
    repo.git("switch", "--quiet", "pr2")

    client.restack()

    assert not any(isinstance(event, events.SkippedMerged) for event in emitted)
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_sync_skips_merged_pr_with_local_commits_after_merge(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "after merge\n", "pr1 after merge")

    client.sync()

    assert events.MergedWithNewCommits("pr1", "main") in emitted
    assert repo.config("pr1", "stackParent") == "main"


@pytest.mark.parametrize("where", ["local", "remote"])
def test_sync_keeps_merged_pr_whose_branch_has_a_merge_with_changes_of_its_own(
    repo: Repo, gh: FakeGitHub, client: StackClient, where: str
) -> None:
    repo.make_stack()
    client.push()
    head = repo.git("rev-parse", "pr1")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="pr1",
        headRefOid=head,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    )
    # After the pull request merged, main is merged into pr1, with a change
    # that's in neither: its only commits besides that merge are main's.
    if where == "local":
        repo.git("fetch", "--quiet")
        repo.git("switch", "--quiet", "pr1")
        repo.merge_with_an_edit("origin/main", "d", "resolved\n")
    else:
        repo.git("switch", "--quiet", "-C", "pr1", "origin/pr1", cwd=repo.forge)
        repo.merge_with_an_edit("origin/main", "d", "resolved\n", cwd=repo.forge)
        repo.git("push", "--quiet", "origin", "pr1", cwd=repo.forge)

    result = repo.stack("sync", "-d", "both")

    assert "skipping pr1: it was merged into main, but it or its remote branch has commits" in result.stderr
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.git("branch", "--list", "pr1") != ""
    assert repo.git("show", "pr1:d", cwd=repo.origin if where == "remote" else repo.path) == "resolved"


def test_sync_untracks_merged_pr_whose_later_merge_added_nothing_its_parent_lacks(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    head = repo.git("rev-parse", "pr1")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="pr1",
        headRefOid=head,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    )
    repo.commit_on_forge("main", "a", "main again\n", "main again")
    # main is merged into pr1, resolving the conflict as main did, so pr1's
    # merge has changes of its own, but main has them all.
    repo.git("fetch", "--quiet")
    repo.git("switch", "--quiet", "pr1")
    subprocess.run(["git", "merge", "--quiet", "origin/main"], cwd=repo.path, capture_output=True, check=False)
    repo.git("checkout", "--theirs", "a")
    repo.git("add", "a")
    repo.git("commit", "--quiet", "--no-edit")
    repo.git("switch", "--quiet", "pr3")

    result = repo.stack("sync", "-d", "both")

    assert "skipping pr1" not in result.stderr
    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.config("pr2", "stackParent") == "main"


def test_sync_deletes_a_squash_merged_pr_whose_branch_was_kept(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    head = repo.git("rev-parse", "pr1")
    repo.squash_merge_on_forge("pr1", delete_branch=False)
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="pr1",
        headRefOid=head,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)},
    )

    repo.stack("sync", "-d", "both")

    assert repo.git("branch", "--list", "pr1") == ""
    assert repo.git("branch", "--list", "pr1", cwd=repo.origin) == ""


def test_open_pr_on_default_branch_overrides_local_detection(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=5,
        state="OPEN",
        headRefName="pr1",
        headRefOid=repo.git("rev-parse", "pr1"),
        baseRefName="main",
        mergeCommit=None,
    )
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("push", "--quiet")

    assert repo.branch_info("pr1").status is BranchStatus.UP_TO_DATE


def test_open_pr_on_stacked_parent_falls_back_to_local_detection(
    repo: Repo, gh: FakeGitHub, client: StackClient
) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(
        number=6,
        state="OPEN",
        headRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr2"),
        baseRefName="pr1",
        mergeCommit=None,
    )
    repo.git("switch", "--quiet", "pr1")
    repo.git("merge", "--quiet", "--squash", "pr2")
    repo.git("commit", "--quiet", "-m", "pr2 (squashed)")
    repo.git("push", "--quiet")

    client.sync()

    repo.assert_untracked("pr2")
    repo.assert_stacked("pr3", "pr1", ["pr3 c1"])


@pytest.mark.parametrize("command", ["sync", "clean"])
def test_refuses_to_change_anything_when_pr_lookup_fails(
    repo: Repo, gh: FakeGitHub, client: StackClient, command: str
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    old_main = repo.git("rev-parse", "origin/main")
    gh.fail("api")

    with pytest.raises(StackError, match="couldn't look up pull requests on GitHub"):
        getattr(client, command)()

    assert repo.git("rev-parse", "origin/main") == old_main
    assert repo.config("pr1", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "pr1"


def test_refuses_to_sync_when_pr_lookup_answer_is_malformed(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    gh.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=None, baseRefName="main", mergeCommit=None)

    with pytest.raises(StackError, match="expected headRefOid to be a str"):
        client.sync()


def test_info_falls_back_to_local_detection_when_pr_lookup_fails(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    repo.git("fetch", "--quiet", "--prune")
    repo.git("branch", "--force", "main", "origin/main")
    gh.fail("api")

    assert repo.branch_info("pr1").status is BranchStatus.MERGED
    assert any(isinstance(event, events.PullRequestLookupFailed) for event in emitted)


def test_git_adapter_setting_leaves_github_alone(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    repo.git("config", "stack.adapter", "git")

    client.push()
    client.sync()

    assert gh.load()["calls"] == []


@pytest.fixture
def fake_ssh(repo: Repo, tmp_path: pathlib.Path) -> None:
    """An ssh whose config has `Host github-work` with `HostName github.com`
    (see tests/fake_ssh.py)."""
    install_command(tmp_path / "bin", "ssh", "tests.fake_ssh")


@pytest.mark.parametrize("url", ["git@github-work:acme/widgets.git", "ssh://git@github-work/acme/widgets.git"])
def test_github_repo_resolves_ssh_host_alias(repo: Repo, fake_ssh: None, url: str) -> None:
    repo.git("config", "remote.origin.url", url)

    assert github_repo(GitClient(repo.path), "origin") == ("github.com", "acme", "widgets")


def test_github_repo_leaves_https_host_alone(repo: Repo, fake_ssh: None) -> None:
    repo.git("config", "remote.origin.url", "https://github-work/acme/widgets.git")

    assert github_repo(GitClient(repo.path), "origin") == ("github-work", "acme", "widgets")


def test_sync_leaves_reverted_pr_alone_when_its_merge_was_tweaked(
    repo: Repo, gh: FakeGitHub, client: StackClient, emitted: list[events.Event]
) -> None:
    repo.make_stack()
    merge_pr1_on_forge_with_a_tweak(repo, gh)
    # The revert undoes the merge commit, tweak included, so it only matches that.
    repo.revert_on_forge("HEAD")

    client.sync()

    assert events.Reverted("pr1", "main", (repo.git("rev-parse", "main"),), ("pr2",)) in emitted
    assert repo.config("pr1", "stackParent") == "main"


def test_an_unknown_pull_request_state_is_an_error() -> None:
    data = {
        "number": 5,
        "state": "ARCHIVED",
        "isDraft": False,
        "isCrossRepository": False,
        "headRefOid": "abc",
        "baseRefName": "main",
        "mergeCommit": None,
    }

    with pytest.raises(StackError, match="pull request state git-stack doesn't know: ARCHIVED"):
        PullRequest.from_json(data)
