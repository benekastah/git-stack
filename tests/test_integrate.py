"""Integration roots and the ordinary stacks that use them."""

import pathlib
import shlex
import shutil

import pytest
from git_client.testing import GitCall

from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.integrations import Integration, Integrations
from tests.helpers import FakeGitHub, Repo


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
    assert "combined (integration)" in repo.stack("info", "--offline").stdout
    git_calls.clear()
    repo.stack("restack")
    assert tip(repo, "combined") == before
    assert not any(call[:2] == ["worktree", "add"] for call in git_calls)

    repo.stack("integrate", "combined", "two")
    assert repo.git("rev-parse", "refs/heads/combined^2") == tip(repo, "two")
    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")
    assert repo.git("branch", "--show-current") == "combined"
    assert repo.git("status", "--porcelain") == ""


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
    repo.stack("push")
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
