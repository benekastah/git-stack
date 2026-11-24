"""git stack push."""

import json
import pathlib
import shlex
import sys
import threading
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import pytest
from git_client import PushDestination, PushRef, PushStatus
from git_client import PushResult as GitPushResult

from git_stack import events
from git_stack.errors import StackError
from git_stack.git import GIT_VARIABLE, GitClient, split_command
from git_stack.push_repair import repairable_url
from git_stack.stack import StackClient
from tests.git_support import GitCall, real_git
from tests.helpers import FakeGitHub, Repo, install_command


def publication_calls(calls: list[GitCall]) -> list[list[str]]:
    commands = (split_command(tuple(call)) for call in calls)
    return [[command, *args] for command, args in commands if command == "push"]


def test_deep_stack_publishes_every_branch_with_one_atomic_push(
    repo: Repo, gh: FakeGitHub, git_calls: list[GitCall]
) -> None:
    chain = repo.make_chain(8)
    git_calls.clear()

    repo.stack("push", "--prs")

    assert {pr["headRefName"] for pr in gh.load()["prs"]} == set(chain)
    assert all(repo.git("rev-parse", b, cwd=repo.origin) == repo.git("rev-parse", b) for b in chain)
    pushes = publication_calls(git_calls)
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
        "custom executable",
        pytest.param(
            "trailing space", marks=pytest.mark.skipif(sys.platform == "win32", reason="Win32 trims the path")
        ),
    ],
)
def test_partial_push_reports_status_and_prints_a_leased_rollback(
    repo: Repo,
    client: StackClient,
    git_calls: list[GitCall],
    push_config: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.push()
    original = repo.git("rev-parse", "feature")
    name = f"{repo.origin.name} " if push_config == "trailing space" else "other-push.git"
    other = repo.origin.with_name(name)
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    divergent = repo.git("rev-parse", "main")
    repo.git("update-ref", "refs/heads/feature", divergent, cwd=other)
    if push_config != "cleared pushurl":
        repo.git("config", "--add", "remote.origin.pushurl", str(repo.origin))
        repo.git("config", "--add", "remote.origin.pushurl", str(other))
    else:
        repo.git("config", "--add", "remote.origin.url", str(other))
        repo.git("config", "--add", "remote.origin.pushurl", "")
    repo.commit("a", "local advance\n", "advance")

    program = real_git()
    if push_config == "custom executable":
        bin_dir = tmp_path / "alternate executable"
        bin_dir.mkdir()
        install_command(bin_dir, "fake-git", "tests.fake_git")
        program = str(bin_dir / ("fake-git.exe" if sys.platform == "win32" else "fake-git"))
        monkeypatch.setenv(GIT_VARIABLE, program)
        monkeypatch.setenv("FAKE_GIT_LOG", str(tmp_path / "git-log.jsonl"))
        monkeypatch.setenv("REAL_GIT", real_git())
        client = StackClient(GitClient(repo.path))

    git_calls.clear()
    with pytest.raises(StackError) as error:
        client.push()

    pushes = publication_calls(git_calls)
    assert len(pushes) == 1 and "--atomic" in pushes[0] and "--dry-run" not in pushes[0]
    published = repo.git("rev-parse", "feature")
    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == published
    assert repo.git("rev-parse", "refs/heads/feature", cwd=other) == divergent
    detail = str(error.value)
    assert "Push destination 1 updated refs/heads/feature" in detail
    assert "Push destination 2 rejected refs/heads/feature" in detail
    args = shlex.split(next(line for line in detail.splitlines() if " -c push.pushOption= push " in line))
    assert args[0] == pathlib.Path(program).as_posix()
    assert f"--force-with-lease=refs/heads/feature:{published}" in args
    assert f"{original}^{{commit}}:refs/heads/feature" in args

    # The command addresses just the successful destination and cannot undo a later update.
    repo.git("update-ref", "refs/heads/feature", divergent, cwd=repo.origin)
    with pytest.raises(pytest.fail.Exception, match="stale info"):
        repo.git(*args[1:])
    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == divergent
    repo.git("update-ref", "refs/heads/feature", published, cwd=repo.origin)

    repo.git("branch", original, published)
    repo.git(*args[1:])

    assert repo.git("rev-parse", "refs/heads/feature", cwd=repo.origin) == original
    assert repo.git("rev-parse", "refs/heads/feature", cwd=other) == divergent


@pytest.mark.parametrize(
    ("redacted_url", "configured_url"),
    [
        ("ssh://example.com/repo", "ssh://alice@example.com/repo"),
        ("example.com:project/repo", "alice@example.com:project/repo"),
        ("https://example.com/repo", "https://alice:token@example.com/repo"),
    ],
)
def test_partial_push_does_not_attribute_a_report_with_a_redacted_url_collision(
    redacted_url: str, configured_url: str
) -> None:
    ref = "refs/heads/feature"
    old, new = "a" * 40, "b" * 40
    refs = (PushRef(ref, ref, PushStatus.FAST_FORWARD, "", old, new),)
    independent = "ssh://example.com/independent"
    result = GitPushResult(1, "", "", (PushDestination(redacted_url, refs), PushDestination(independent, refs)))

    details, repairs = StackClient.describe_partial_push(
        result, (configured_url, redacted_url, independent), {redacted_url, independent}, {ref: (old, ref)}
    )

    assert details == [
        "Push destination 1 outcome uncertain",
        "Push destination 2 outcome uncertain",
        "Push destination 3 updated refs/heads/feature",
    ]
    assert repairs == []


def test_rollback_requires_a_slash_in_an_scp_destination() -> None:
    # A slashless destination can select a legacy remote of the same name.
    assert not repairable_url("example.com:repo")
    assert repairable_url("example.com:project/repo")


@pytest.mark.parametrize("fail_after_push", [False, True])
def test_partial_push_configuration_error_reports_retained_pr_retarget(
    repo: Repo, gh: FakeGitHub, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch, fail_after_push: bool
) -> None:
    repo.make_stack()
    repo.client.push()
    old = repo.git("rev-parse", "pr2")
    gh.add_pr(
        number=2,
        state="OPEN",
        headRefName="pr2",
        headRefOid=old,
        baseRefName="main",
        mergeCommit=None,
    )
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/pr2", repo.git("rev-parse", "main"), cwd=other)
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    repo.git("switch", "--quiet", "pr2")
    repo.commit("b", "local advance\n", "advance")
    repo.client.restack()
    addressable = GitClient.addressable_push_urls

    def read_config(self: GitClient, remote: str, urls: Iterable[str]) -> set[str]:
        if bool(publication_calls(git_calls)) == fail_after_push:
            raise StackError("Git config unavailable")
        return addressable(self, remote, urls)

    monkeypatch.setattr(GitClient, "addressable_push_urls", read_config)
    git_calls.clear()

    result = repo.stack("push", "--with-prs", expect_success=False)

    assert "#2's base was changed from main to pr1" in result.stderr
    assert "Git config unavailable" in result.stderr
    assert " -c push.pushOption= push " not in result.stderr
    assert len(publication_calls(git_calls)) == int(fail_after_push)
    assert gh.load()["prs"][0]["baseRefName"] == "pr1"
    if fail_after_push:
        assert "Push destination 1 updated refs/heads/pr2" in result.stderr
    else:
        assert "Nothing was pushed" in result.stderr


@pytest.mark.parametrize("remove_setting_in_hook", [False, True])
def test_partial_push_does_not_repair_with_a_different_receive_pack(
    repo: Repo, git_calls: list[GitCall], remove_setting_in_hook: bool
) -> None:
    repo.client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.client.push()
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/feature", repo.git("rev-parse", "main"), cwd=other)
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    repo.git("config", "remote.origin.receivepack", "git-receive-pack")
    if remove_setting_in_hook:
        repo.add_hook(repo.path, "pre-push", "#!/bin/sh\ngit config --unset remote.origin.receivepack || :\n")
    repo.commit("a", "local advance\n", "advance")
    git_calls.clear()

    result = repo.stack("push", expect_success=False)

    assert "Inspect each destination" in result.stderr
    assert " -c push.pushOption= push " not in result.stderr
    assert len(publication_calls(git_calls)) == 1
    assert "Push destination 1 updated refs/heads/feature" in result.stderr
    assert repo.remote_subject("feature") == "advance"


def test_partial_push_does_not_undo_an_up_to_date_destination(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.client.push()
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/feature", repo.git("rev-parse", "main"), cwd=other)
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    repo.commit("a", "local advance\n", "advance")
    # Publish the exact same value independently, leaving origin/feature stale.
    repo.git("push", str(repo.origin), "feature")
    git_calls.clear()

    result = repo.stack("push", expect_success=False)

    assert "Inspect each destination" in result.stderr
    assert "Push destination 1 up to date refs/heads/feature" in result.stderr
    assert " -c push.pushOption= push " not in result.stderr
    assert len(publication_calls(git_calls)) == 1
    assert repo.remote_subject("feature") == "advance"


def test_partial_push_reports_a_created_ref_without_guessing_a_rollback_lease(
    repo: Repo, git_calls: list[GitCall]
) -> None:
    repo.client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/feature", repo.git("rev-parse", "main"), cwd=other)
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    git_calls.clear()

    result = repo.stack("push", expect_success=False)

    assert "Push destination 1 updated refs/heads/feature" in result.stderr
    assert "Push destination 2 rejected refs/heads/feature" in result.stderr
    assert " -c push.pushOption= push " not in result.stderr
    assert len(publication_calls(git_calls)) == 1
    assert repo.remote_subject("feature") == "feature"


@pytest.mark.parametrize("blocked_by", ["ambiguous report", "write protection"])
def test_partial_push_withholds_unsafe_repair_commands(
    repo: Repo, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch, blocked_by: str
) -> None:
    repo.client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.client.push()
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    repo.git("update-ref", "refs/heads/feature", repo.git("rev-parse", "main"), cwd=other)
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    repo.commit("a", "local advance\n", "advance")
    report = GitClient.push_report
    protect = StackClient.protect_remote_heads

    def incomplete_report(git: GitClient, *args: str) -> GitPushResult:
        outcome = report(git, *args)
        return replace(outcome, destinations=outcome.destinations[:1])

    def protect_heads(client: StackClient, *args: Any, **kwargs: Any) -> None:
        if repo.remote_subject("feature") == "advance":
            raise StackError("PR entered the merge queue")
        protect(client, *args, **kwargs)

    if blocked_by == "ambiguous report":
        monkeypatch.setattr(GitClient, "push_report", incomplete_report)
    else:
        monkeypatch.setattr(StackClient, "protect_remote_heads", protect_heads)
    git_calls.clear()

    result = repo.stack("push", expect_success=False)

    assert "Inspect each destination" in result.stderr
    assert " -c push.pushOption= push " not in result.stderr
    assert ("PR entered the merge queue" in result.stderr) == (blocked_by == "write protection")
    assert "Push destination 1 updated refs/heads/feature" in result.stderr
    if blocked_by == "ambiguous report":
        assert "Push destination 2 outcome uncertain" in result.stderr
    assert len(publication_calls(git_calls)) == 1
    assert repo.remote_subject("feature") == "advance"


def test_successful_multi_destination_push_keeps_one_push_and_json_output(repo: Repo, git_calls: list[GitCall]) -> None:
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(other))
    for destination in (repo.origin, other):
        repo.git("config", "--add", "remote.origin.pushurl", str(destination))
    repo.client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    git_calls.clear()

    result = repo.stack("push", "--json")

    assert json.loads(result.stdout.splitlines()[-1])["type"] == "result"
    assert len(publication_calls(git_calls)) == 1
    assert repo.git("config", "branch.feature.remote") == "origin"
    assert repo.git("rev-parse", "feature", cwd=other) == repo.git("rev-parse", "feature", cwd=repo.origin)


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


@pytest.mark.parametrize("push_config", ["pushurl", "pushInsteadOf", "multiple URLs", "second rewrite"])
def test_push_protects_the_default_branch_at_the_actual_push_destination(
    repo: Repo, client: StackClient, push_config: str
) -> None:
    repo.git("push", "--quiet", "origin", "main:release")
    other = repo.tmp / "push.git"
    repo.git("clone", "--quiet", "--bare", str(repo.origin), str(other))
    repo.git("symbolic-ref", "HEAD", "refs/heads/release", cwd=other)
    origin_url = repo.git("config", "--get", "remote.origin.url")
    other_url = other.as_posix()
    if push_config == "pushurl":
        repo.git("config", "remote.origin.pushurl", other_url)
    elif push_config in ("pushInsteadOf", "second rewrite"):
        repo.git("config", f"url.{other_url}.pushInsteadOf", origin_url)
        if push_config == "second rewrite":
            repo.git("config", f"url.{origin_url}.insteadOf", other_url)
    else:
        repo.git("config", "--add", "remote.origin.url", other_url)
    assert other_url in repo.git("remote", "get-url", "--push", "--all", "origin").splitlines()
    repo.git("fetch", "origin")
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("branch", "--set-upstream-to=origin/release", "feature")
    before = repo.git("rev-parse", "refs/heads/release", cwd=other)

    result = repo.stack("push", expect_success=False)

    assert "rerun `git stack push --force`" in result.stderr
    assert repo.git("rev-parse", "refs/heads/release", cwd=other) == before


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 disallows newlines in paths")
def test_push_refuses_an_ambiguous_default_branch_probe(repo: Repo, client: StackClient) -> None:
    repo.git("push", "--quiet", "origin", "main:release")
    decoy = repo.tmp / "decoy.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(decoy))
    destination = repo.origin.with_name(f"{repo.origin.name}\n") / decoy.relative_to(decoy.anchor)
    destination.parent.mkdir(parents=True)
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(destination))
    repo.git("symbolic-ref", "HEAD", "refs/heads/release", cwd=destination)
    repo.git("config", "remote.origin.pushurl", str(destination))
    repo.git("fetch", "--quiet", "origin")
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    repo.git("branch", "--set-upstream-to=origin/release", "feature")
    before = repo.git("rev-parse", "refs/heads/release", cwd=destination)

    with pytest.raises(StackError, match="default branch isn't known"):
        client.push()

    assert repo.git("rev-parse", "refs/heads/release", cwd=destination) == before


def test_push_overlaps_independent_remote_default_probes(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.push()
    destination = repo.tmp / "other.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(destination))
    repo.git("config", "remote.origin.pushurl", str(destination))
    repo.commit("a", "advanced\n", "advance")
    both_probing = threading.Barrier(2)
    fetch_default = GitClient._ask_remote_default_branch  # pyright: ignore[reportPrivateUsage]
    push_default = GitClient._default_at_resolved_url  # pyright: ignore[reportPrivateUsage]

    def fetch(git: GitClient, remote: str) -> str | None:
        both_probing.wait(timeout=3)
        return fetch_default(git, remote)

    def push(git: GitClient, url: str) -> str | None:
        both_probing.wait(timeout=3)
        return push_default(git, url)

    monkeypatch.setattr(GitClient, "_ask_remote_default_branch", fetch)
    monkeypatch.setattr(GitClient, "_default_at_resolved_url", push)

    client.push()

    assert repo.git("log", "-1", "--format=%s", "refs/heads/feature", cwd=destination) == "advance"


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 disallows newlines in paths")
@pytest.mark.parametrize("multiple", [False, True])
def test_push_accepts_a_separate_unambiguous_destination(repo: Repo, client: StackClient, multiple: bool) -> None:
    client.branch("feature")
    repo.commit("a", "feature\n", "feature")
    client.push()
    fetch = repo.tmp / "fetch with a\nnewline.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(fetch))
    repo.git("config", "remote.origin.url", str(fetch))
    repo.git("config", "remote.origin.pushurl", str(repo.origin))
    if multiple:
        second = repo.tmp / "second.git"
        repo.git("clone", "--bare", "--quiet", str(repo.origin), str(second))
        repo.git("config", "--add", "remote.origin.pushurl", str(second))
    repo.commit("a", "local advance\n", "advance")

    client.push()

    assert repo.remote_subject("feature") == "advance"


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
    # Changed remote refs need one batched live PR protection query.
    assert len(gh_calls) <= (3 if advance else 2), gh_calls


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


@pytest.mark.parametrize("command", ["push", "sync"])
@pytest.mark.parametrize(
    ("queued", "options", "error"),
    [
        (True, (), "merge queue"),
        (True, ("--protect-prs", "--force"), "merge queue"),
        (True, ("--protect-prs", "--allow-pr-update", "3"), "merge queue"),
        (True, ("--protect-prs", "--force", "--allow-pr-update", "2"), "merge queue"),
        (True, ("--protect-prs", "--force", "--allow-pr-update", "3"), None),
        (True, ("--force",), None),
        (False, (), None),
        (False, ("--protect-prs",), "approved"),
        (False, ("--protect-prs", "--allow-pr-update", "3"), None),
    ],
)
def test_pr_protection_on_head_updates(
    repo: Repo, gh: FakeGitHub, command: str, queued: bool, options: tuple[str, ...], error: str | None
) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=3,
        state="OPEN",
        headRefName="pr3",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr3"),
        mergeCommit=None,
        reviewDecision="APPROVED",
        isInMergeQueue=queued,
    )
    repo.commit("c", "update\n", "advance pr3")

    result = repo.stack(command, "--with-prs", *options, expect_success=error is None)

    if error:
        assert error in result.stderr
        assert "#3" in result.stderr
    assert repo.remote_subject("pr3") == ("pr3 c1" if error else "advance pr3")


@pytest.mark.parametrize(
    ("queued", "merge_status", "reason"), [(False, "CLEAN", "ready to merge"), (True, "BLOCKED", "merge queue")]
)
def test_protected_prs_keeps_ready_or_queued_pr_targeting_main(
    repo: Repo, gh: FakeGitHub, queued: bool, merge_status: str, reason: str
) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=2,
        state="OPEN",
        headRefName="pr2",
        baseRefName="main",
        headRefOid=repo.git("rev-parse", "pr2"),
        mergeCommit=None,
        reviewDecision="APPROVED",
        mergeStateStatus=merge_status,
        isInMergeQueue=queued,
        body="",
    )

    result = repo.stack("prs", "--protect-prs", "--force", "--allow-pr-update", "2", expect_success=False)

    assert "#2" in result.stderr
    assert reason in result.stderr
    assert [pr["number"] for pr in gh.load()["prs"]] == [2]
    assert not gh.calls("pr", "edit")


def test_protected_push_only_checks_prs_whose_heads_would_change(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="pr1",
        baseRefName="main",
        mergeCommit=None,
        headRefOid=repo.git("rev-parse", "pr1"),
        isInMergeQueue=True,
    )
    repo.commit("c", "updated\n", "advance pr3")

    repo.stack("push", "--protect-prs")

    assert repo.remote_subject("pr3") == "advance pr3"
    assert repo.remote_subject("pr1") == "pr1 c2"


@pytest.mark.parametrize("failed", [False, True])
def test_push_batches_retargets_to_published_bases(repo: Repo, gh: FakeGitHub, failed: bool) -> None:
    repo.make_stack()
    repo.stack("push")
    for number in (2, 3):
        gh.add_pr(
            number=number,
            state="OPEN",
            headRefName=f"pr{number}",
            baseRefName="main",
            headRefOid=repo.git("rev-parse", f"refs/heads/pr{number}"),
            mergeCommit=None,
        )
    if failed:
        gh.fail_editing(2)
    repo.commit("c", "advance\n", "advance pr3")

    result = repo.stack("push", "--with-prs", expect_success=not failed)

    assert not gh.calls("pr", "edit")
    assert [pr["baseRefName"] for pr in gh.load()["prs"]] == ["main" if failed else "pr1", "pr2"]
    assert repo.remote_subject("pr3") == ("pr3 c1" if failed else "advance pr3")
    if failed:
        assert "couldn't retarget #2" in result.stderr
        assert "#3's base was changed from main to pr2" in result.stderr


def test_protected_push_preserves_an_individual_approval(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=3,
        state="OPEN",
        headRefName="pr3",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr3"),
        mergeCommit=None,
        reviewDecision="REVIEW_REQUIRED",
        latestOpinionatedReviews=["APPROVED"],
    )
    repo.commit("c", "update\n", "advance pr3")

    result = repo.stack("push", "--protect-prs", expect_success=False)

    assert "PR #3: it is approved" in result.stderr
    assert repo.remote_subject("pr3") == "pr3 c1"


def test_protected_prs_leaves_queued_description_alone(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push", "--prs")
    state = gh.load()
    state["prs"][0].update(isInMergeQueue=True, body="")
    gh.save(state)

    result = repo.stack("prs", "--protect-prs", expect_success=False)

    assert "merge queue" in result.stderr
    assert gh.load()["prs"][0]["body"] == ""


def test_protected_push_preserves_approved_pr_on_a_changed_base(repo: Repo, gh: FakeGitHub) -> None:
    repo.client.branch("base")
    repo.commit("a", "base contribution\n", "publish base")
    repo.stack("push")
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="unselected",
        baseRefName="base",
        headRefOid=repo.git("rev-parse", "base"),
        mergeCommit=None,
        reviewDecision="APPROVED",
    )
    repo.commit("a", "base rewritten\n", "update base")

    result = repo.stack("push", "--protect-prs", expect_success=False)

    assert "the base of PR #7" in result.stderr
    assert repo.remote_subject("base") == "publish base"


def test_fork_pr_requires_consent_scoped_to_its_base_repo(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="pr3",
        baseRefName="main",
        headRefOid=repo.git("rev-parse", "pr3"),
        mergeCommit=None,
        isCrossRepository=True,
        headRepository="acme/widgets",
        baseRepository="upstream/widgets",
        isInMergeQueue=True,
    )
    repo.commit("c", "advance\n", "update fork")
    result = repo.stack("push", "--protect-prs", "--force", "--allow-pr-update", "7", expect_success=False)

    assert "upstream/widgets#7" in result.stderr
    assert repo.remote_subject("pr3") == "pr3 c1"

    repo.stack("push", "--protect-prs", "--force", "--allow-pr-update", "upstream/widgets#7")

    assert repo.remote_subject("pr3") == "update fork"


def test_queue_protection_follows_ref_pagination(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    for number in range(1, 12):
        gh.add_pr(
            number=number,
            state="OPEN",
            headRefName="pr3",
            baseRefName="main",
            headRefOid=repo.git("rev-parse", "pr3"),
            mergeCommit=None,
            isInMergeQueue=number == 1,
        )
    repo.commit("c", "advance\n", "update pr3")

    result = repo.stack("push", "--with-prs", expect_success=False)

    assert "PR #1: it is in the merge queue" in result.stderr
    assert repo.remote_subject("pr3") == "pr3 c1"


@pytest.mark.parametrize("command", ["clean", "sync"])
def test_commands_keep_the_remote_base_of_a_queued_pr(repo: Repo, gh: FakeGitHub, command: str) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.merge_on_forge("pr1", delete_branch=False)
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="unselected",
        baseRefName="pr1",
        headRefOid=repo.git("rev-parse", "pr1"),
        mergeCommit=None,
        isInMergeQueue=True,
    )

    result = repo.stack(command, "--with-prs", "-d", "remote", expect_success=False)

    assert "the base of PR #7: it is in the merge queue" in result.stderr
    assert repo.remote_subject("pr1") == "pr1 c2"


def test_push_does_not_check_a_github_fetch_repository_that_isnt_pushed(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    gh.add_pr(
        number=3,
        state="OPEN",
        headRefName="pr3",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr3"),
        mergeCommit=None,
        isInMergeQueue=True,
    )
    mirror = repo.tmp / "mirror.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(mirror))
    repo.git("config", "remote.origin.pushurl", str(mirror))
    repo.commit("c", "advance\n", "update mirror")

    repo.stack("push", "--protect-prs")

    assert repo.git("log", "-1", "--format=%s", "pr3", cwd=mirror) == "update mirror"
    assert repo.remote_subject("pr3") == "pr3 c1"


def test_bare_pr_consent_cannot_authorize_a_different_push_repository(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.stack("push")
    mirror = repo.tmp / "mirror.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(mirror))
    url = "https://github.com/acme/mirror.git"
    repo.git("config", f"url.{mirror}.insteadOf", url)
    repo.git("config", "remote.origin.pushurl", url)
    gh.add_pr(
        number=3,
        state="OPEN",
        headRefName="pr3",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "pr3"),
        mergeCommit=None,
        baseRepository="acme/mirror",
        reviewDecision="APPROVED",
    )
    repo.commit("c", "advance\n", "update mirror")

    rejected = repo.stack("push", "--protect-prs", "--allow-pr-update", "3", expect_success=False)
    assert "acme/mirror#3" in rejected.stderr
    assert repo.git("log", "-1", "--format=%s", "pr3", cwd=mirror) == "pr3 c1"

    repo.stack("push", "--protect-prs", "--allow-pr-update", "acme/mirror#3")

    assert repo.git("log", "-1", "--format=%s", "pr3", cwd=mirror) == "update mirror"
    assert repo.remote_subject("pr3") == "pr3 c1"


def test_nine_changed_refs_fit_one_protection_query(repo: Repo, gh: FakeGitHub) -> None:
    branches = repo.make_chain(9)
    repo.stack("push")
    for branch in branches:
        repo.git("switch", "--quiet", branch)
        repo.commit(f"{branch}.txt", "updated\n", "update branch")
    gh.clear_calls()

    repo.stack("push", "--with-prs")

    checks = [call for call in gh.calls("api", "graphql") if any("ref0:" in arg for arg in call)]
    assert len(checks) == 1
    assert sum(arg.startswith("ref") and "=" in arg for arg in checks[0]) == 18
    assert all(repo.remote_subject(branch) == "update branch" for branch in branches)
