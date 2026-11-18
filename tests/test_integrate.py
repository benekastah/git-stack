"""Integration roots and the ordinary stacks that use them."""

import pathlib
import shlex
import shutil

import pytest
from git_client.testing import GitCall

from git_stack import cli
from git_stack import integrations as integration_module
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.integrations import Integration, Integrations
from git_stack.stacks import StackNode, Stacks
from tests.helpers import FakeGitHub, Repo, captured_output


def tip(repo: Repo, branch: str, *, remote: bool = False) -> str:
    return repo.git("rev-parse", f"refs/heads/{branch}", cwd=repo.origin if remote else None)


def source(repo: Repo, branch: str, file: str, *, tracked: bool = False) -> None:
    repo.git("switch", "--quiet", "main")
    if tracked:
        repo.client.branch(branch)
    else:
        repo.git("switch", "--quiet", "-c", branch)
    repo.commit(file, f"{branch}\n", branch)


def integration(repo: Repo, *, tracked: bool = False) -> None:
    source(repo, "one", "a", tracked=tracked)
    source(repo, "two", "b")
    repo.stack("integrate", "combined", "one", "two")


def work(repo: Repo) -> None:
    repo.git("switch", "--quiet", "combined")
    repo.stack("create", "work")
    repo.commit("c", "work\n", "work")


def test_create_refresh_and_replace_ordered_sources(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    before = tip(repo, "combined")
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == tip(repo, "one")
    assert repo.git("rev-parse", "refs/heads/combined^1^1") == tip(repo, "main")

    repo.git("switch", "--quiet", "combined")
    assert repo.stack("info", "--offline").stdout.splitlines()[0] == "combined (of one, two) *"
    git_calls.clear()
    repo.stack("restack")
    assert tip(repo, "combined") == before
    assert not any(call[:2] == ["worktree", "add"] for call in git_calls)

    repo.stack("integrate", "combined", "two")
    assert repo.stack("info", "--offline").stdout.splitlines()[0] == "combined (of two) *"
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")
    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")
    assert repo.git("branch", "--show-current") == "combined"
    assert repo.git("status", "--porcelain") == ""


def test_unchanged_integration_verifies_refs_without_reloading_snapshot(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    work(repo)
    git_calls.clear()

    result = repo.stack("restack")

    assert sum(call[:1] == ["for-each-ref"] for call in git_calls) == 1
    assert any(call[:2] == ["update-ref", "--stdin"] for call in git_calls)
    assert "rebuilding integration" not in result.stdout


@pytest.mark.parametrize("command", ["restack", "pull", "sync"])
@pytest.mark.parametrize("checkout", ["combined", "work"])
def test_refresh_from_root_or_work_branch(repo: Repo, command: str, checkout: str) -> None:
    integration(repo)
    work(repo)
    if command == "sync":
        repo.stack("push")
    source(repo, "replacement", "a")
    repo.git("branch", "--force", "one", "refs/heads/replacement")
    repo.git("switch", "--quiet", checkout)

    repo.stack(command)

    assert repo.git("branch", "--show-current") == checkout
    assert repo.git("log", "--format=%s", "refs/heads/combined..refs/heads/work") == "work"
    assert repo.git("show", "refs/heads/work:a") == "replacement"
    if command == "sync":
        for branch in ("combined", "work"):
            assert tip(repo, branch, remote=True) == tip(repo, branch)
        assert repo.config("combined", "remote") == "origin"
    else:
        assert repo.git("ls-remote", "--heads", "origin", "combined") == ""


@pytest.mark.parametrize("tracked", [False, True])
def test_adopt_preserves_snapshot_until_refresh(repo: Repo, tracked: bool) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    repo.git("branch", "combined", "refs/heads/one")
    before = tip(repo, "combined")
    if tracked:
        repo.stack("track", "main", "combined")
    work(repo)

    repo.stack("integrate", "--adopt", "combined", "one", "two")

    assert tip(repo, "combined") == before
    assert repo.config("work", "stackParent") == "combined"
    assert "combined" not in repo.client.stacks()
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "advanced\n", "advance source before first refresh")
    repo.stack("restack", "combined")
    assert tip(repo, "combined") != before
    assert repo.git("show", "refs/heads/combined:b") == "two"


@pytest.mark.parametrize("command", ["restack", "pull", "sync"])
def test_refresh_refuses_extra_commits_before_moving_refs(repo: Repo, command: str) -> None:
    integration(repo)
    work(repo)
    repo.git("switch", "--quiet", "combined")
    repo.commit("c", "extra\n", "extra first")
    first = repo.git("log", "-1", "--format=%h")
    repo.commit("c", "extra more\n", "extra second")
    second = repo.git("log", "-1", "--format=%h")
    tips = {branch: tip(repo, branch) for branch in ("main", "combined", "work")}
    repo.commit_on_forge("main", "b", "upstream\n", "upstream")
    args = ["--force"] if command == "sync" else []

    result = repo.stack(command, "work", *args, expect_success=False)

    assert result.stderr.splitlines()[1:3] == [f"{second} extra second", f"{first} extra first"]
    assert "git stack restack combined --force" in result.stderr
    assert {branch: tip(repo, branch) for branch in tips} == tips
    repo.stack("restack", "combined", "--force")
    assert repo.git("log", "--format=%s", "refs/heads/combined..refs/heads/work") == "work"


def test_selected_sources_run_first_and_excluded_sources_stay_put(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("group", "add", "both", "work", "one")
    repo.commit_on_forge("main", "d", "upstream\n", "upstream")
    before = tip(repo, "one")
    repo.stack("pull", "-g", "both", "--latest")
    assert tip(repo, "one") != before
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == tip(repo, "one")

    repo.stack("group", "remove", "one")
    before = tip(repo, "one")
    repo.commit_on_forge("main", "d", "another\n", "another")
    repo.stack("pull", "-g", "both", "--latest")
    assert tip(repo, "one") == before
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == before


def conflict_worktree(stderr: str) -> pathlib.Path:
    line = next(line for line in stderr.splitlines() if "merge --continue" in line)
    return pathlib.Path(shlex.split(line.strip())[2])


def test_conflict_continue_and_abort(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    result = repo.stack("integrate", "combined", "one", "two", expect_success=False)
    path = conflict_worktree(result.stderr)
    assert repo.git("branch", "--list", "combined") == ""
    (path / "a").write_text("resolved\n", encoding="utf-8")
    repo.git("add", "a", cwd=path)
    repo.git("-c", "core.editor=true", "merge", "--continue", cwd=path)

    repo.stack("integrate", "combined", "one", "two")

    assert repo.git("show", "refs/heads/combined:a") == "resolved"
    assert not path.exists()
    prior = tip(repo, "combined")
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflict again\n", "another")
    repo.stack("restack", "combined", expect_success=False)
    repo.git("merge", "--abort", cwd=path)
    repo.stack("integrate", "--abort", "combined")
    assert not path.exists()
    assert tip(repo, "combined") == prior


def test_missing_source_needs_exact_landing_evidence(repo: Repo) -> None:
    integration(repo)
    previous = tip(repo, "combined")
    one = tip(repo, "one")
    repo.git("branch", "-D", "one")
    failure = repo.stack("restack", "combined", expect_success=False)
    assert "git stack integrate combined" in failure.stderr
    assert tip(repo, "combined") == previous

    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", one)
    repo.stack("restack", "combined")
    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")


def test_sync_pushes_integration_and_work_atomically(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    work(repo)
    repo.git("push", "--set-upstream", "origin", "refs/heads/work")
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "updated\n", "update")
    repo.git("switch", "--quiet", "work")
    git_calls.clear()

    repo.stack("sync")

    pushes = [call for call in git_calls if call[:2] == ["push", "--atomic"]]
    assert len(pushes) == 1
    assert "--force-with-lease=refs/heads/combined:" in pushes[0]
    assert any(arg.startswith("--force-with-lease=refs/heads/work:") for arg in pushes[0])
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")
    assert tip(repo, "work", remote=True) == tip(repo, "work")


def test_squash_landing_of_deleted_sources_and_promotion(repo: Repo, gh: FakeGitHub) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    for branch in ("one", "two"):
        repo.git("push", "--quiet", "--set-upstream", "origin", f"refs/heads/{branch}:refs/heads/review-{branch}")
    repo.stack("integrate", "combined", "one", "two")
    work(repo)
    repo.stack("sync")
    repo.stack("push", "--prs")
    work_pr = gh.load()["prs"][0]
    assert work_pr["baseRefName"] == "combined"
    assert repo.git("rev-parse", "combined@{upstream}") == tip(repo, "combined")
    for number, branch in enumerate(("one", "two"), start=10):
        included = tip(repo, branch)
        repo.squash_merge_on_forge(f"review-{branch}")
        gh.add_pr(
            number=number,
            state="MERGED",
            isCrossRepository=branch == "one",
            headRefName=f"review-{branch}",
            headRefOid=included,
            baseRefName="main",
            mergeCommit={"oid": tip(repo, "main", remote=True)},
        )
        repo.git("branch", "--delete", "--force", branch)

    for number in range(100, 111):
        gh.add_pr(
            number=number,
            state="MERGED",
            headRefName="review-one",
            headRefOid=tip(repo, "main"),
            baseRefName="main",
            mergeCommit={"oid": tip(repo, "main", remote=True)},
        )
    repo.stack("pull")
    assert tip(repo, "combined") == tip(repo, "main")
    assert repo.git("log", "--format=%s", "refs/heads/main..refs/heads/work") == "work"

    repo.stack("restack", "--onto", "main")
    repo.stack("push")
    assert repo.config("work", "stackParent") == "main"
    assert repo.git("log", "--format=%s", "refs/heads/main..refs/heads/work") == "work"
    assert gh.load()["prs"][0]["baseRefName"] == "main"


def test_squash_evidence_for_older_tip_does_not_drop_new_work(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    repo.git("switch", "--quiet", "one")
    included = tip(repo, "one")
    repo.git("push", "--quiet", "origin", "refs/heads/one")
    repo.squash_merge_on_forge("one")
    gh.add_pr(
        number=5,
        state="MERGED",
        headRefName="one",
        headRefOid=included,
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main", remote=True)},
    )
    repo.commit("a", "new work\n", "new work")

    result = repo.stack("pull", "combined", expect_success=False)

    assert "merge --continue" in result.stderr
    repo.stack("integrate", "--abort", "combined")


def test_integration_checked_out_in_another_worktree(repo: Repo) -> None:
    integration(repo)
    work(repo)
    path = repo.tmp / "another"
    repo.git("worktree", "add", str(path), "combined")
    before = tip(repo, "combined")
    work_before = tip(repo, "work")
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "changed\n", "changed")
    repo.git("switch", "--quiet", "work")

    result = repo.stack("restack", expect_success=False)

    assert "checked out in another worktree" in result.stderr
    assert tip(repo, "combined") == before
    assert tip(repo, "work") == work_before


@pytest.mark.parametrize("other_worktree", [True, False])
def test_sync_leaves_remote_base_and_work_unchanged_when_work_is_blocked(repo: Repo, other_worktree: bool) -> None:
    integration(repo)
    work(repo)
    repo.stack("push")
    before = {branch: tip(repo, branch, remote=True) for branch in ("combined", "work")}
    repo.git("switch", "one")
    repo.commit("a", "changed source\n", "advance source")
    if other_worktree:
        repo.git("worktree", "add", str(repo.tmp / "other"), "work")
    else:
        repo.git("config", "branch.work.stackBase", tip(repo, "one"))

    failure = repo.stack("sync", "combined", expect_success=False)

    assert "didn't push" in failure.stderr
    assert {branch: tip(repo, branch, remote=True) for branch in before} == before
    if other_worktree:
        repo.git("worktree", "remove", str(repo.tmp / "other"))
    else:
        repo.git("config", "branch.work.stackBase", before["combined"])
    repo.stack("sync", "combined")
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")
    assert tip(repo, "work", remote=True) == tip(repo, "work")


def test_aborting_missing_worktree_preserves_previous_tip(repo: Repo) -> None:
    integration(repo)
    before = tip(repo, "combined")
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflicting\n", "conflict")
    result = repo.stack("restack", "combined", expect_success=False)
    path = conflict_worktree(result.stderr)
    shutil.rmtree(path)

    repo.stack("integrate", "--abort", "combined")

    assert tip(repo, "combined") == before
    assert str(path) not in repo.git("worktree", "list", "--porcelain")
    assert repo.client.git.config_get("combined", "stackIntegrationBuild") is None


def test_input_moving_during_merge_prevents_publish(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    before = tip(repo, "combined")
    old_one = tip(repo, "one")
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "changed\n", "changed")
    passthrough = GitClient.passthrough

    def move_input(git: GitClient, *args: str) -> bool:
        result = passthrough(git, *args)
        if args[:1] == ("merge",) and tip(repo, "one") == old_one:
            repo.git("update-ref", "refs/heads/one", tip(repo, "main"), old_one)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", move_input)
        result = repo.stack("restack", "combined", expect_success=False)

    assert "integration refs changed" in result.stderr
    assert tip(repo, "combined") == before
    failure = repo.stack("restack", "combined", expect_success=False)
    assert "git stack integrate --abort combined" in failure.stderr
    repo.stack("integrate", "--abort", "combined")
    repo.stack("restack", "combined")
    assert repo.git("show", "refs/heads/combined:b") == "changed"


def test_interruption_after_ref_update_is_recovered(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    before = tip(repo, "combined")
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "changed\n", "changed")

    def stop_after_update(integrations: Integrations, name: str, config: Integration) -> None:
        assert tip(repo, name) == config["tip"]
        raise StackError("interrupted after ref update")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "save", stop_after_update)
        result = repo.stack("restack", "combined", expect_success=False)
    assert "interrupted after ref update" in result.stderr
    built = tip(repo, "combined")
    assert built != before

    repo.stack("restack", "combined")

    assert tip(repo, "combined") == built
    assert repo.client.git.config_get("combined", "stackIntegrationBuild") is None


def test_sync_lease_rejection_keeps_work_and_integration_remote_refs_atomic(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration(repo)
    work(repo)
    repo.stack("sync")
    repo.stack("push")
    previous_work = tip(repo, "work", remote=True)
    previous_integration = tip(repo, "combined", remote=True)
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "changed\n", "changed")
    repo.git("switch", "--quiet", "work")
    passthrough = GitClient.passthrough

    def move_remote(git: GitClient, *args: str) -> bool:
        if args[:2] == ("push", "--atomic"):
            repo.git("update-ref", "refs/heads/combined", tip(repo, "main", remote=True), cwd=repo.origin)
        return passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", move_remote)
        result = repo.stack("sync", expect_success=False)

    assert "push failed" in result.stderr
    assert tip(repo, "work", remote=True) == previous_work
    assert tip(repo, "combined", remote=True) != previous_integration
    assert tip(repo, "combined", remote=True) == tip(repo, "main", remote=True)
    repo.stack("sync", "--force")
    assert tip(repo, "work", remote=True) == tip(repo, "work")
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


def test_failed_refresh_stops_selected_source_remote_deletion(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("push", "one")
    repo.stack("push", "work")
    repo.stack("group", "add", "both", "work", "one")
    one = tip(repo, "one")
    old_work = tip(repo, "work")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", one)
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflict with landed source\n", "conflict")
    repo.git("switch", "--quiet", "work")

    result = repo.stack("sync", "-g", "both", "--delete", "remote", expect_success=False)

    assert "merge --continue" in result.stderr
    assert tip(repo, "one", remote=True) == one
    assert tip(repo, "work", remote=True) == old_work
    assert tip(repo, "work") == old_work


@pytest.mark.parametrize("interrupt_before_build", [False, True])
def test_sync_resumes_conflict_after_deleting_rewritten_source(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, interrupt_before_build: bool
) -> None:
    integration(repo, tracked=True)
    repo.stack("create", "source-child", "--from", "one")
    repo.commit("c", "child work\n", "child work")
    repo.git("push", "--set-upstream", "origin", "source-child")
    child_before = tip(repo, "source-child")
    child_base = tip(repo, "one")
    if interrupt_before_build:
        source(repo, "one-peer", "c", tracked=True)
    repo.git("switch", "main")
    repo.commit("a", "landed replacement\n", "rewrite and land source")
    replacement = tip(repo, "main")
    repo.git("branch", "-f", "one", replacement)
    repo.git("switch", "two")
    repo.commit("a", "conflicting change\n", "conflict")

    selector = ("one*", "--ignore-revert", "one") if interrupt_before_build else ("one",)
    command = ("sync", "combined", *selector, "--delete", "local")
    if interrupt_before_build:

        def stop_before_journal(*args: object, **kwargs: object) -> None:
            raise StackError("interrupted before build")

        with monkeypatch.context() as patch:
            patch.setattr(Integrations, "build", stop_before_journal)
            assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
        assert repo.git("branch", "--list", "one") == ""
    path = conflict_worktree(repo.stack(*command, expect_success=False).stderr)
    assert repo.git("branch", "--list", "one") == ""
    (path / "a").write_text("resolved\n", encoding="utf-8")
    repo.git("add", "a", cwd=path)
    repo.git("-c", "core.editor=true", "merge", "--continue", cwd=path)

    if interrupt_before_build:
        repo.stack("sync", "combined")
        assert repo.git("rev-parse", "source-child^", cwd=repo.origin) != replacement
    else:
        repo.git("branch", "-f", "source-child", child_before)
        repo.git("config", "branch.source-child.stackBase", child_base)
        repo.git("worktree", "add", str(repo.tmp / "blocked-child"), "source-child")
        failure = repo.stack(*command, expect_success=False)
        assert "couldn't update selected integration source stacks: source-child" in failure.stderr
        assert repo.git("ls-remote", "origin", "refs/heads/combined") == ""
        repo.git("worktree", "remove", str(repo.tmp / "blocked-child"))
    repo.stack(*command)

    assert repo.git("show", "refs/heads/combined:a") == "resolved"
    assert repo.git("rev-parse", "refs/heads/combined^1") == replacement
    assert repo.git("merge-base", "source-child", replacement, cwd=repo.origin) == replacement
    assert not path.exists()
    saved = Integrations(repo.client).read("combined")
    assert saved is not None and "retired_sources" not in saved


@pytest.mark.parametrize("conflict", [False, True])
def test_retired_source_keeps_squash_evidence_offline(repo: Repo, gh: FakeGitHub, conflict: bool) -> None:
    integration(repo, tracked=True)
    repo.git("branch", "-f", "one", "main")
    repo.git("switch", "one")
    repo.commit("a", "landed replacement\n", "rewrite source")
    repo.git("push", "--set-upstream", "origin", "one")
    included = tip(repo, "one")
    repo.squash_merge_on_forge("one")
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=included,
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main", remote=True)},
    )
    repo.git("switch", "two")
    if conflict:
        repo.commit("a", "conflicting change\n", "conflict")
    command = ("sync", "combined", "one", "--delete", "local")
    result = repo.stack(*command, expect_success=not conflict)
    assert repo.git("branch", "--list", "one") == ""
    repo.git("config", "stack.adapter", "git")
    if conflict:
        path = conflict_worktree(result.stderr)
        (path / "a").write_text("resolved\n", encoding="utf-8")
        repo.git("add", "a", cwd=path)
        repo.git("-c", "core.editor=true", "merge", "--continue", cwd=path)
        repo.stack(*command)
    repo.stack("restack", "combined")
    assert repo.git("show", "combined:a") == ("resolved" if conflict else "landed replacement")


def test_resume_from_detached_checkout_after_publication_is_interrupted(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration(repo)
    work(repo)
    before = tip(repo, "combined")
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "changed\n", "changed")
    repo.git("switch", "--quiet", "combined")
    run = GitClient.run

    def stop_after_detach(git: GitClient, *args: str, check: bool = True, input: str | None = None) -> str:
        output = run(git, *args, check=check, input=input)
        if args[:3] == ("switch", "--quiet", "--detach"):
            raise StackError("interrupted after detaching")
        return output

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "run", stop_after_detach)
        result = repo.stack("restack", expect_success=False)
    assert "interrupted after detaching" in result.stderr
    assert repo.git("branch", "--show-current") == ""
    assert tip(repo, "combined") == before

    repo.stack("restack")

    assert repo.git("branch", "--show-current") == "combined"
    assert repo.git("show", "refs/heads/work:a") == "changed"
    assert repo.client.git.config_get("combined", "stackIntegrationBuild") is None


def test_sync_leaves_source_branch_and_pr_outside_the_selected_group(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("sync")
    repo.stack("push", "--prs", "work")
    repo.stack("push", "--prs", "one")
    state = gh.load()
    source_pr = next(pr for pr in state["prs"] if pr["headRefName"] == "one")
    source_pr["baseRefName"] = "combined"
    gh.save(state)
    repo.stack("group", "add", "app", "work")
    repo.commit_on_forge("main", "d", "upstream\n", "upstream")
    before = tip(repo, "one")

    repo.stack("sync", "-g", "app", "--latest")

    assert tip(repo, "one") == tip(repo, "one", remote=True) == before
    assert next(pr for pr in gh.load()["prs"] if pr["headRefName"] == "one")["baseRefName"] == "combined"

    repo.stack("group", "add", "app", "one")
    repo.stack("sync", "-g", "app", "--latest")

    assert tip(repo, "one") == tip(repo, "one", remote=True) != before
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == tip(repo, "one")
    assert next(pr for pr in gh.load()["prs"] if pr["headRefName"] == "one")["baseRefName"] == "main"


def test_clean_leaves_integration_tip_in_place(repo: Repo) -> None:
    integration(repo)
    work(repo)
    before = tip(repo, "combined")
    repo.git("push", "--quiet", "--set-upstream", "origin", "refs/heads/combined")
    repo.commit_on_forge("combined", "d", "upstream\n", "upstream added")

    repo.stack("clean")

    assert tip(repo, "combined") == before


@pytest.mark.parametrize("protected", ["release", "combined"])
def test_clean_keeps_remote_ref_used_by_integration(repo: Repo, protected: str) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    source(repo, protected, "a", tracked=True)
    repo.git("branch", "release-local", "main")
    repo.stack("integrate", "combined-local", "one", "two", "--root", "release-local")
    local = f"{protected}-local"
    repo.git("push", "--set-upstream", "origin", f"{local}:{protected}")
    repo.git("switch", "main")
    # Use the root tip as the alias's upstream so the merged branch has no unseen work.
    if protected == "combined":
        repo.git("branch", "-f", protected, "combined-local")
    repo.git("branch", f"--set-upstream-to=origin/{protected}", protected)
    before = tip(repo, protected, remote=True)
    repo.git("merge", "--ff-only", protected)

    repo.stack("clean", "--delete", "remote")

    assert tip(repo, protected, remote=True) == before
    assert protected not in repo.client.stacks()


def test_rename_updates_integration_inputs_and_root(repo: Repo) -> None:
    integration(repo, tracked=True)
    before = tip(repo, "combined")

    repo.stack("rename", "one", "renamed-source")
    repo.stack("rename", "main", "renamed-root")
    repo.stack("restack", "combined")

    assert "(of renamed-source, two)" in repo.stack("info", "--offline", "--all").stdout
    assert tip(repo, "combined") == before


def test_rename_refuses_to_move_input_during_first_integration_build(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    path = conflict_worktree(repo.stack("integrate", "combined", "one", "two", expect_success=False).stderr)
    before = tip(repo, "one")

    failure = repo.stack("rename", "one", "renamed-source", expect_success=False)

    assert "git stack integrate --abort combined" in failure.stderr
    assert path.is_dir()
    assert tip(repo, "one") == before
    assert repo.git("branch", "--list", "renamed-source") == ""
    repo.git("branch", "unrelated", "main")
    failure = repo.stack("rename", "unrelated", "combined", expect_success=False)
    assert "git stack integrate --abort combined" in failure.stderr
    assert repo.git("branch", "--list", "combined") == ""


def test_unrevert_refuses_to_move_source_onto_its_integration(repo: Repo) -> None:
    repo.commit("c", "change\n", "change file")
    repo.git("revert", "--no-edit", "HEAD")
    reverted = tip(repo, "main")
    integration(repo, tracked=True)
    before = tip(repo, "one")

    failure = repo.stack(
        "unrevert", "resurrect", reverted, "--from", "combined", "--stack", "one", expect_success=False
    )

    assert "one is an integration or depends on one" in failure.stderr
    assert repo.config("one", "stackParent") == "main"
    assert tip(repo, "one") == before
    assert repo.git("branch", "--list", "resurrect") == ""


@pytest.mark.parametrize("select_integration", [True, False])
def test_sync_keeps_custom_root_when_a_merged_branch_tracks_it(repo: Repo, select_integration: bool) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    repo.git("branch", "release", "refs/heads/main")
    repo.git("push", "--set-upstream", "origin", "refs/heads/release")
    release = tip(repo, "release")
    repo.stack("integrate", "combined", "one", "two", "--root", "release")
    source(repo, "alias", "a", tracked=True)
    repo.git("branch", "--set-upstream-to=origin/release", "alias")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", "refs/heads/alias")

    names = ("combined", "alias") if select_integration else ("alias",)
    repo.stack("sync", *names, "--delete", "remote")

    assert tip(repo, "release", remote=True) == release


def test_sync_publishes_validated_work_when_local_ref_moves(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    work(repo)
    repo.stack("sync")
    repo.stack("push")
    repo.commit("c", "local work\n", "local work")
    work_tip = tip(repo, "work")
    passthrough = GitClient.passthrough

    def move_work(git: GitClient, *args: str) -> bool:
        if args[:2] == ("push", "--atomic"):
            repo.git("update-ref", "refs/heads/work", tip(repo, "main"), work_tip)
        return passthrough(git, *args)

    monkeypatch.setattr(GitClient, "passthrough", move_work)
    repo.stack("sync")

    assert tip(repo, "work", remote=True) == work_tip
    assert repo.config("work", "stackPulled") == work_tip


def test_first_sync_retries_after_tracking_write_fails(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    work(repo)
    run = GitClient.run

    def fail_tracking(git: GitClient, *args: str, check: bool = True, input: str | None = None) -> str:
        if args[:2] == ("branch", "--set-upstream-to=refs/remotes/origin/combined"):
            raise StackError("failed to write upstream")
        return run(git, *args, check=check, input=input)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "run", fail_tracking)
        assert "failed to write upstream" in repo.stack("sync", expect_success=False).stderr
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")

    repo.stack("sync")

    assert repo.config("combined", "remote") == "origin"


@pytest.mark.parametrize("remote_moved", [False, True])
def test_sync_retries_a_rejected_atomic_deletion(repo: Repo, remote_moved: bool) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("push", "one")
    repo.stack("group", "add", "both", "work", "one")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", "refs/heads/one")
    repo.git("switch", "--quiet", "work")
    repo.add_hook(repo.origin, "pre-receive", "#!/bin/sh\nexit 1\n")

    assert "push failed" in repo.stack("sync", "--delete", "both", "-g", "both", expect_success=False).stderr
    (repo.origin / "hooks" / "pre-receive").unlink()
    if remote_moved:
        repo.commit_on_forge("one", "d", "remote work\n", "advance remote branch")
    expected = repo.git("ls-remote", "--heads", "origin", "one") if remote_moved else ""

    repo.stack("sync", "--delete", "both", "-g", "both")

    assert repo.git("ls-remote", "--heads", "origin", "one") == expected
    assert repo.git("branch", "--list", "one") == ""


def test_recovery_survives_completion_from_another_worktree(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    work(repo)
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "new\n", "advance source")
    second = repo.tmp / "second"
    repo.git("worktree", "add", str(second), "main")
    repo.git("switch", "--quiet", "combined")

    def stop_before_checkout(git: GitClient, branch: str) -> None:
        raise StackError("interrupted before restoring checkout")

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "switch", stop_before_checkout)
        assert "interrupted" in repo.stack("restack", expect_success=False).stderr
    with captured_output() as output:
        status = cli.main(["-C", str(second), "restack", "combined"])
    assert status == 0, output

    repo.stack("restack")

    assert repo.git("branch", "--show-current") == "combined"


def test_rename_refuses_pending_integration_build(repo: Repo) -> None:
    integration(repo)
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflicting\n", "conflict")
    result = repo.stack("restack", "combined", expect_success=False)
    path = conflict_worktree(result.stderr)

    assert "--abort" in repo.stack("rename", "combined", "renamed", expect_success=False).stderr
    repo.stack("integrate", "--abort", "combined")

    assert not path.exists()


def test_import_keeps_registered_integration_as_an_untracked_root(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    work(repo)
    repo.stack("sync")
    repo.stack("push")
    for number, name, base in ((1, "combined", "main"), (2, "work", "combined")):
        gh.add_pr(
            number=number,
            state="OPEN",
            author="me",
            headRefName=name,
            baseRefName=base,
            headRefOid=tip(repo, name),
            mergeCommit=None,
        )
    repo.stack("untrack", "work")

    repo.stack("import")

    assert "combined" not in repo.client.stacks()
    assert repo.config("work", "stackParent") == "combined"


def test_missing_integration_root_does_not_block_unrelated_track(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    repo.git("branch", "release", "refs/heads/main")
    repo.stack("integrate", "combined", "one", "two", "--root", "release")
    repo.git("branch", "--delete", "release")
    repo.git("branch", "ordinary", "refs/heads/main")

    repo.stack("track", "main", "ordinary")

    assert repo.config("ordinary", "stackParent") == "main"


@pytest.mark.parametrize("mirror", [False, True])
def test_squash_evidence_survives_pruned_alias_and_mirror(repo: Repo, gh: FakeGitHub, mirror: bool) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    if mirror:
        repo.git("remote", "add", "mirror", str(repo.origin))
    remote = "mirror" if mirror else "origin"
    repo.git("push", "--set-upstream", remote, "refs/heads/one:refs/heads/review-one")
    repo.squash_merge_on_forge("review-one")
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="review-one",
        headRefOid=tip(repo, "one"),
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main", remote=True)},
    )
    repo.git("fetch", "--prune", remote)
    repo.git("fetch", "origin")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", "refs/remotes/origin/main")

    repo.stack("integrate", "combined", "one", "two")

    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")


def test_pull_does_not_fetch_unneeded_source_remote(repo: Repo) -> None:
    integration(repo)
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", "refs/heads/one")
    repo.git("remote", "add", "mirror", str(repo.tmp / "unavailable"))
    repo.git("config", "branch.one.remote", "mirror")
    repo.git("config", "branch.one.merge", "refs/heads/one")

    repo.stack("pull", "combined")

    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")


def test_unchanged_integration_reuses_batched_evidence(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    first = [c for c in gh.load()["calls"] if any("states: MERGED" in arg for arg in c)]
    gh.clear_calls()

    repo.stack("restack", "combined")

    assert len(first) == 1
    assert not any("states: MERGED" in arg for call in gh.load()["calls"] for arg in call)


def test_selecting_grouped_integration_traverses_group_once(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    count = 32
    graph = Stacks(StackNode(f"leaf-{index}", "combined", "group") for index in range(count))
    children = Stacks.children
    expanded: list[str] = []

    def count_children(stacks: Stacks, branch: str) -> list[str]:
        expanded.append(branch)
        return children(stacks, branch)

    monkeypatch.setattr(Stacks, "children", count_children)

    selected = repo.client.refresh_selection(["combined"], (), graph, "restack")

    assert len(selected.branches) == count
    assert len(expanded) == count + 1


def test_integration_writes_full_journal_only_at_build_boundaries(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)

    writes = [c for c in git_calls if "branch.combined.stackIntegrationBuild" in c and "--unset" not in c]
    assert len(writes) == 2
    assert len([c for c in git_calls if c[:1] == ["check-ref-format"]]) == 2


def test_naming_integration_root_does_not_select_cross_root_group_peers(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("group", "add", "both", "work", "one")
    original = tip(repo, "one")
    repo.commit_on_forge("main", "d", "upstream\n", "upstream")

    repo.stack("pull", "combined", "--latest")

    assert tip(repo, "one") == original
    assert repo.git("rev-parse", "refs/heads/combined^1^1") == tip(repo, "main")


def test_blocked_integration_leaves_selected_source_and_trunk_unchanged(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("group", "add", "both", "work", "one")
    path = repo.tmp / "another"
    repo.git("worktree", "add", str(path), "combined")
    original = {name: tip(repo, name) for name in ("main", "one", "combined", "work")}
    repo.commit_on_forge("main", "d", "upstream\n", "upstream")

    assert (
        "checked out in another worktree" in repo.stack("pull", "-g", "both", "--latest", expect_success=False).stderr
    )

    assert {name: tip(repo, name) for name in original} == original


def test_negative_evidence_expires_with_unchanged_root_and_source(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    repo.git("push", "origin", "refs/heads/one")
    repo.squash_merge_on_forge("one")
    repo.git("fetch", "origin")
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--ff-only", "refs/remotes/origin/main")
    repo.stack("integrate", "combined", "one", "two")
    assert repo.git("rev-parse", "refs/heads/combined^1^2") == tip(repo, "one")
    gh.add_pr(
        number=4,
        state="MERGED",
        headRefName="one",
        headRefOid=tip(repo, "one"),
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main")},
    )
    monkeypatch.setattr(integration_module, "EVIDENCE_RECHECK_SECONDS", 0)

    repo.stack("restack", "combined")

    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")


@pytest.mark.parametrize("command", ["pull", "sync"])
def test_recovering_deleted_parent_checks_integration_tip(repo: Repo, gh: FakeGitHub, command: str) -> None:
    integration(repo)
    repo.stack("sync", "combined")
    repo.git("switch", "--quiet", "combined")
    repo.git("switch", "--quiet", "-c", "retired")
    repo.commit("a", "retired\n", "retired")
    repo.stack("create", "work")
    repo.commit("c", "work\n", "work")
    repo.stack("push")
    retired, work_tip = tip(repo, "retired"), tip(repo, "work")
    repo.git("switch", "--quiet", "combined")
    repo.git("merge", "--ff-only", "refs/heads/retired")
    repo.git("branch", "--delete", "retired")
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="retired",
        headRefOid=retired,
        baseRefName="combined",
        mergeCommit={"oid": tip(repo, "combined")},
    )
    gh.add_pr(
        number=2,
        state="OPEN",
        headRefName="work",
        headRefOid=work_tip,
        baseRefName="combined",
        mergeCommit=None,
    )

    result = repo.stack(command, "work", expect_success=False)

    assert "differs from its last generated tip" in result.stderr
    assert repo.config("work", "stackParent") == "retired"
    assert tip(repo, "work") == tip(repo, "work", remote=True) == work_tip


def test_recovered_parent_is_refreshed_before_work_is_rebased(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    repo.stack("sync", "combined")
    previous = tip(repo, "combined")
    repo.git("switch", "--quiet", "combined")
    repo.git("switch", "--quiet", "-c", "retired")
    repo.stack("create", "work")
    repo.commit("c", "work\n", "work")
    repo.stack("push")
    repo.git("branch", "--delete", "retired")
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="retired",
        headRefOid=previous,
        baseRefName="combined",
        mergeCommit={"oid": previous},
    )
    gh.add_pr(
        number=2,
        state="OPEN",
        headRefName="work",
        headRefOid=tip(repo, "work"),
        baseRefName="combined",
        mergeCommit=None,
    )
    repo.git("switch", "--quiet", "one")
    repo.commit("a", "next input\n", "advance input")

    repo.stack("pull", "work")

    assert tip(repo, "combined") != previous
    assert repo.config("work", "stackParent") == "combined"
    assert repo.git("show", "refs/heads/work:a") == "next input"
    assert repo.git("log", "--format=%s", "refs/heads/combined..refs/heads/work") == "work"


def test_prs_requires_integration_to_be_published_first(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    work(repo)
    repo.git("push", "--set-upstream", "origin", "refs/heads/work")

    result = repo.stack("prs", expect_success=False)

    assert "publish it with `git stack push --prs`" in result.stderr
    assert gh.load()["prs"] == []


@pytest.mark.parametrize("args", [(), ("--prs",)])
def test_push_refuses_to_replace_a_remote_integration_deleted_since_last_fetch(
    repo: Repo, gh: FakeGitHub, args: tuple[str, ...]
) -> None:
    integration(repo)
    work(repo)
    repo.stack("push")
    before = tip(repo, "work", remote=True)
    repo.git("update-ref", "-d", "refs/heads/combined", cwd=repo.origin)
    gh.add_pr(
        number=1,
        state="OPEN",
        headRefName="work",
        headRefOid=before,
        baseRefName="main",
        mergeCommit=None,
    )
    repo.commit("c", "next work commit\n", "advance work")

    result = repo.stack("push", *args, expect_success=False)

    assert "checking whether origin will accept the push" in result.stdout
    assert "push would fail" in result.stderr
    assert tip(repo, "work", remote=True) == before
    assert gh.calls("pr", "edit") == []


def test_push_prs_accepts_published_integration_without_cached_ref(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    work(repo)
    repo.stack("sync", "combined")
    repo.git("update-ref", "-d", "refs/remotes/origin/combined")

    repo.stack("push", "--prs")

    assert [(pr["headRefName"], pr["baseRefName"]) for pr in gh.load()["prs"]] == [("work", "combined")]


@pytest.mark.parametrize("restack_first", [False, True])
def test_push_refreshes_integration_and_publishes_work_with_its_base(
    repo: Repo, git_calls: list[GitCall], restack_first: bool
) -> None:
    integration(repo)
    work(repo)
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "latest source\n", "advance source")
    source_tip = tip(repo, "two")
    if restack_first:
        repo.stack("restack", "combined")
        assert repo.git("branch", "--show-current") == "two"
    repo.git("switch", "--quiet", "work")
    repo.commit("c", "latest work\n", "advance work")
    git_calls.clear()

    result = repo.stack("push")

    assert ("rebuilding integration combined" in result.stdout) is not restack_first
    assert ("removing the temporary worktree for combined" in result.stdout) is not restack_first
    assert "checking whether origin will accept the push" in result.stdout
    if not restack_first:
        assert result.stdout.index("rebuilding integration combined") < result.stdout.index("Merge made by")
    assert repo.git("show", "refs/heads/work:b", cwd=repo.origin) == "latest source"
    remote_base = tip(repo, "combined", remote=True)
    assert remote_base == tip(repo, "combined")
    assert tip(repo, "work", remote=True) == tip(repo, "work")
    assert repo.git("log", "--format=%s", f"{remote_base}..refs/heads/work", cwd=repo.origin).splitlines() == [
        "advance work",
        "work",
    ]
    assert repo.config("work", "remote") == repo.config("combined", "remote") == "origin"
    assert tip(repo, "two") == source_tip
    assert repo.git("ls-remote", "--heads", "origin", "two") == ""
    assert len([c for c in git_calls if c[:2] == ["push", "--atomic"]]) == 1


@pytest.mark.parametrize("existing_pr", [False, True])
def test_first_push_prs_creates_or_retargets_only_the_work_pull_request(
    repo: Repo, gh: FakeGitHub, existing_pr: bool
) -> None:
    integration(repo)
    work(repo)
    if existing_pr:
        repo.git("push", "--set-upstream", "origin", "refs/heads/work")
        gh.add_pr(
            number=1,
            state="OPEN",
            headRefName="work",
            headRefOid=tip(repo, "work"),
            baseRefName="main",
            mergeCommit=None,
        )

    repo.stack("push", "--prs")

    assert [(pr["headRefName"], pr["baseRefName"]) for pr in gh.load()["prs"]] == [("work", "combined")]
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


def test_push_conflict_leaves_remote_integration_and_work_untouched(repo: Repo) -> None:
    integration(repo)
    work(repo)
    repo.stack("push")
    base = tip(repo, "combined", remote=True)
    work_tip = tip(repo, "work", remote=True)
    repo.git("switch", "--quiet", "two")
    repo.commit("a", "conflict with one\n", "conflict")
    repo.git("switch", "--quiet", "work")
    repo.commit("c", "unpushed work\n", "advance work")

    assert "merge --continue" in repo.stack("push", expect_success=False).stderr

    assert tip(repo, "combined", remote=True) == base
    assert tip(repo, "work", remote=True) == work_tip
    repo.stack("integrate", "--abort", "combined")


def test_push_recovers_new_work_upstream_after_publication(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    work(repo)
    run = GitClient.run

    def fail_tracking(git: GitClient, *args: str, check: bool = True, input: str | None = None) -> str:
        if args[:2] == ("branch", "--set-upstream-to=refs/remotes/origin/work"):
            raise StackError("failed to set work's upstream")
        return run(git, *args, check=check, input=input)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "run", fail_tracking)
        assert "failed to set work's upstream" in repo.stack("push", expect_success=False).stderr
    assert tip(repo, "work", remote=True) == tip(repo, "work")

    repo.stack("push")

    assert repo.config("work", "remote") == "origin"
