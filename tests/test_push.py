"""git stack push."""

import sys

import pytest
from git_client.testing import GitCall

from git_stack import events
from git_stack.errors import StackError
from git_stack.stack import StackClient
from tests.helpers import FakeGitHub, Repo


def test_push_checks_leases_once_when_no_pull_request_needs_retargeting(
    repo: Repo, gh: FakeGitHub, git_calls: list[GitCall]
) -> None:
    repo.make_stack()
    repo.stack("push", "--prs")

    assert gh.calls("pr", "create")
    pushes = [call for call in git_calls if call[0] == "push"]
    assert len(pushes) == 1
    assert "--dry-run" not in pushes[0]
    assert "--atomic" in pushes[0] and any(arg.startswith("--force-with-lease=") for arg in pushes[0])


@pytest.mark.parametrize("prefix", [("config", "branch."), ("log", "--walk-reflogs"), ("rev-list", "--parents")])
def test_repush_skips_already_recorded_work(
    repo: Repo, client: StackClient, git_calls: list[GitCall], prefix: tuple[str, str]
) -> None:
    repo.make_stack()
    client.push()
    git_calls.clear()

    client.push()

    assert not any(len(call) >= 2 and call[0] == prefix[0] and call[1].startswith(prefix[1]) for call in git_calls)


def test_push_records_changed_tips_together(repo: Repo, client: StackClient, git_calls: list[GitCall]) -> None:
    repo.make_stack()
    client.push()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 advanced\n", "advance pr1")
    client.restack()
    git_calls.clear()

    client.push()

    assert not any(call[:2] in (["log", "--walk-reflogs"], ["rev-list", "--parents"]) for call in git_calls)
    publication = next(i for i, call in enumerate(git_calls) if call[0] == "push" and "--set-upstream" in call)
    assert sum(call[:2] == ["config", "--list"] for call in git_calls[publication + 1 :]) == 2
    for branch in ("pr1", "pr2", "pr3"):
        assert repo.config(branch, "stackPulled") == repo.git("rev-parse", f"refs/heads/{branch}")
        assert repo.config(branch, "stackPulledUpstream") == f"origin refs/heads/{branch}"


def test_push_records_remote_tip_if_a_hook_moves_the_local_branch(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    pushed = repo.git("rev-parse", "refs/heads/feature")
    tree = repo.git("rev-parse", f"{pushed}^{{tree}}")
    moved = repo.git("commit-tree", tree, "-p", pushed, "-m", "advanced by hook")
    repo.add_hook(repo.path, "pre-push", f"#!/bin/sh\ngit update-ref refs/heads/feature {moved}\n")

    client.push()

    assert repo.git("rev-parse", "refs/heads/feature") == moved
    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == pushed
    assert repo.config("feature", "stackPulled") == pushed


def test_push_does_not_record_a_tracking_ref_changed_after_publication(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    planned = repo.git("rev-parse", "feature")
    tree = repo.git("rev-parse", f"{planned}^{{tree}}")
    unseen = repo.git("commit-tree", tree, "-p", planned, "-m", "another writer's commit")
    passthrough = client.git.passthrough

    def publish_and_fetch(*args: str) -> bool:
        result = passthrough(*args)
        if result and args[0] == "push":
            repo.git("push", "--quiet", str(repo.origin), f"{unseen}:refs/heads/feature")
            repo.git("fetch", "--quiet", "origin")
        return result

    monkeypatch.setattr(client.git, "passthrough", publish_and_fetch)

    client.push()

    assert not repo.config_exists("feature", "stackPulled") or repo.config("feature", "stackPulled") != unseen
    with pytest.raises(StackError, match="has commits that feature doesn't"):
        client.push()


@pytest.mark.parametrize(
    "push_config",
    [
        "pushurls",
        "cleared pushurl",
        pytest.param(
            "trailing space", marks=pytest.mark.skipif(sys.platform == "win32", reason="Win32 trims the path")
        ),
    ],
)
def test_push_checks_leases_at_all_destinations_before_writing(
    repo: Repo, client: StackClient, push_config: str
) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    old = repo.git("rev-parse", "feature")
    client.push()
    name = f"{repo.origin.name} " if push_config == "trailing space" else "other-push.git"
    other = repo.origin.with_name(name)
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/feature", repo.git("rev-parse", "main"), cwd=other)
    if push_config != "cleared pushurl":
        repo.git("config", "--add", "remote.origin.pushurl", str(repo.origin))
        repo.git("config", "--add", "remote.origin.pushurl", str(other))
    else:
        repo.git("config", "--add", "remote.origin.url", str(other))
        repo.git("config", "--add", "remote.origin.pushurl", "")
    repo.commit("a", "local advance\n", "advance")

    with pytest.raises(StackError, match="push would fail"):
        client.push()

    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == old


def test_push_reports_partial_success_when_a_second_attempt_fails(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.push()
    repo.git("config", "--add", "remote.origin.pushurl", str(repo.origin))
    repo.git("config", "--add", "remote.origin.pushurl", str(repo.origin))
    repo.commit("a", "local advance\n", "advance")
    tip = repo.git("rev-parse", "feature")
    repo.add_hook(repo.path, "pre-push", "#!/bin/sh\n[ ! -e .git/push-once ] || exit 1\n: > .git/push-once\n")

    with pytest.raises(StackError, match="some destinations may have been updated"):
        client.push()

    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == tip


def test_push_resolves_an_explicit_destination_once(repo: Repo, client: StackClient, git_calls: list[GitCall]) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.push()
    other = repo.tmp / "other-push.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("config", "remote.origin.pushurl", str(other))
    repo.commit("a", "local advance\n", "advance")
    git_calls.clear()

    client.push()

    assert sum(call[:5] == ["remote", "get-url", "--push", "--all", "origin"] for call in git_calls) == 1


@pytest.mark.parametrize("push_config", ["pushurl", "pushInsteadOf", "multiple URLs", "second rewrite"])
def test_push_protects_the_default_branch_at_the_actual_push_destination(
    repo: Repo, client: StackClient, push_config: str
) -> None:
    repo.git("push", "--quiet", "origin", "main:release")
    other = repo.tmp / "push.git"
    repo.git("clone", "--quiet", "--bare", str(repo.origin), str(other))
    repo.git("symbolic-ref", "HEAD", "refs/heads/release", cwd=other)
    if push_config == "pushurl":
        repo.git("config", "remote.origin.pushurl", str(other))
    elif push_config in ("pushInsteadOf", "second rewrite"):
        repo.git("config", f"url.{other}.pushInsteadOf", str(repo.origin))
        if push_config == "second rewrite":
            repo.git("config", f"url.{repo.origin}.insteadOf", str(other))
    else:
        repo.git("config", "--add", "remote.origin.url", str(other))
    repo.git("fetch", "origin")
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("branch", "--set-upstream-to=origin/release", "feature")
    before = repo.git("rev-parse", "refs/heads/release", cwd=other)

    result = repo.stack("push", expect_success=False)

    assert "rerun `git stack push --force`" in result.stderr
    assert repo.git("rev-parse", "refs/heads/release", cwd=other) == before


@pytest.mark.parametrize(("advance", "git_budget"), [(True, 18), (False, 14)])
def test_push_prs_keeps_subprocess_work_bounded(
    repo: Repo, gh: FakeGitHub, git_calls: list[GitCall], advance: bool, git_budget: int
) -> None:
    repo.client.branch("lower")
    repo.commit("a", "lower\n", "first")
    repo.client.branch("upper")
    repo.commit("b", "upper\n", "second")
    repo.client.push()
    if advance:
        repo.git("switch", "--quiet", "lower")
        repo.commit("c", "lower advanced\n", "advance lower")
        repo.client.restack()
    # Other repositories' push rewrites should not add a remote probe here.
    repo.git("config", f"url.{repo.origin}.pushInsteadOf", "other-repositories:")
    gh.fail_opening("lower")
    gh.clear_calls()
    git_calls.clear()

    result = repo.stack("push", "--prs", expect_success=False)

    assert "opening a pull request for lower failed" in result.stderr
    assert repo.remote_subject("lower") == ("advance lower" if advance else "first")
    assert repo.remote_subject("upper") == "second"
    # The Git fixture counts each launched process, including the batch process once.
    assert len(git_calls) <= git_budget, git_calls
    gh_calls = gh.calls()
    assert len(gh_calls) <= 2, gh_calls


@pytest.mark.parametrize("command", ["push", "sync"])
def test_no_verify_skips_pre_push_hook(repo: Repo, client: StackClient, command: str) -> None:
    client.branch("feature")
    repo.commit("a", "first\n", "first")
    if command == "sync":
        client.push()
        repo.commit("a", "second\n", "second")
    repo.add_hook(repo.path, "pre-push", "#!/bin/sh\nexit 1\n")

    repo.stack(command, expect_success=False)
    repo.stack(command, "--no-verify")

    assert repo.remote_subject("feature") == ("second" if command == "sync" else "first")


def test_push_force_targets_the_configured_upstream_name(repo: Repo, client: StackClient) -> None:
    client.branch("local-feature")
    repo.commit("a", "local\n", "local")
    repo.git("push", "--quiet", "--set-upstream", "origin", "local-feature:remote-feature")
    repo.commit_on_forge("remote-feature", "b", "remote\n", "remote")
    repo.git("fetch", "--quiet")

    client.push(force=True)

    assert repo.git("show", "remote-feature:a", cwd=repo.origin) == "local"
    assert repo.git("rev-parse", "remote-feature", cwd=repo.origin) == repo.git("rev-parse", "local-feature")
    assert not repo.git("branch", "--list", "local-feature", cwd=repo.origin)
    assert repo.git("rev-parse", "--abbrev-ref", "local-feature@{upstream}") == "origin/remote-feature"


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


def test_upstream_rebase_must_change_the_same_occurrence(repo: Repo, client: StackClient) -> None:
    repo.write("d", "old\nseparator\nold\n")
    repo.git("add", "d")
    repo.git("commit", "--quiet", "-m", "add d")
    base = repo.git("rev-parse", "HEAD")
    client.branch("pr1")
    repo.commit("d", "new\nseparator\nold\n", "edit")
    local = repo.git("rev-parse", "HEAD")
    repo.commit("d", "old\nseparator\nnew\n", "temporary")
    repo.git("reset", "--soft", "HEAD~")
    repo.git("commit", "--quiet", "--amend", "--no-edit")
    remote = repo.git("rev-parse", "HEAD")

    assert not client.git.is_rebased_copy(base, local, base, remote)


def test_upstream_rebase_must_preserve_commit_order(repo: Repo, client: StackClient) -> None:
    base = repo.git("rev-parse", "HEAD")
    client.branch("pr1")
    repo.commit("a", "first\n", "first")
    first = repo.git("rev-parse", "HEAD")
    repo.commit("b", "second\n", "second")
    second = repo.git("rev-parse", "HEAD")
    repo.git("switch", "--quiet", "--detach", base)
    repo.git("cherry-pick", "--quiet", second)
    repo.git("cherry-pick", "--quiet", first)
    reordered = repo.git("rev-parse", "HEAD")

    assert not client.git.is_rebased_copy(base, second, base, reordered)


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


@pytest.mark.parametrize("force", [False, True])
def test_push_refuses_an_upstream_that_isnt_a_branch(repo: Repo, client: StackClient, force: bool) -> None:
    client.branch("feature")
    repo.commit("a", "mine\n", "mine")
    repo.push_someone_elses_branch("feature")
    # Names origin, but git resolves no upstream from a merge ref that isn't a
    # branch, so origin/feature isn't feature's.
    repo.git("config", "branch.feature.remote", "origin")
    repo.git("config", "branch.feature.merge", "refs/tags/unrelated")

    result = repo.stack("push", *(["--force"] if force else []), expect_success=False)

    assert "feature's upstream on origin isn't a branch" in result.stderr
    assert repo.remote_subject("feature") == "theirs"


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
    assert events.Reverted("pr1", "main", (repo.git("rev-parse", "main"),), ("pr2",)) in emitted
    assert events.ParentSkipped("pr2", "pr1") in emitted
    assert repo.remote_subject("pr2") == "pr2 c1"
