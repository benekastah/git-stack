"""Integration roots and the ordinary stacks that use them."""

import json
import pathlib
import shutil

import pytest
from git_client.testing import GitCall

from git_stack import cli
from git_stack import integrations as integration_module
from git_stack.core import RemoteDeletion
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.integrations import Integration, Integrations
from git_stack.stack import StackClient
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


def test_group_integration_refreshes_its_leaves_and_keeps_source_order(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("bottom", group="topic", from_branch="main")
    repo.commit("a", "bottom\n", "bottom")
    client.branch("leaf")
    repo.commit("b", "leaf\n", "leaf")
    client.branch("other", group="topic", from_branch="main")
    repo.commit("c", "other\n", "other")
    source(repo, "unrelated", "a", tracked=True)
    repo.git("switch", "-qc", "release", "main")
    client.branch("elsewhere", group="topic")
    repo.commit("a", "elsewhere\n", "elsewhere")
    repo.git("switch", "leaf")

    repo.stack("integrate", "combined", "-g")

    assert "(of leaf, other)" in repo.stack("info", "combined", "--offline").stdout
    client.branch("new-top")
    repo.commit("a", "new top\n", "new top")
    client.branch("added", group="topic", from_branch="main")
    repo.write("added.txt", "added\n")
    repo.git("add", "added.txt")
    repo.git("commit", "-m", "added")
    assert "(of other, new-top, added) (needs restack)" in repo.stack("info", "combined", "--offline").stdout

    repo.stack("restack", "combined")

    assert repo.git("show", "combined:a") == "new top"
    assert repo.git("show", "combined:added.txt") == "added"
    assert "(of other, new-top, added) (needs restack)" not in repo.stack("info", "combined", "--offline").stdout

    previous = tip(repo, "combined")
    repo.stack("integrate", "combined", "-g")
    assert tip(repo, "combined") == previous


def test_group_integration_uses_the_remote_default_and_tracks_group_rename(repo: Repo, client: StackClient) -> None:
    repo.git("switch", "-qc", "trunk")
    repo.git("push", "--quiet", "--set-upstream", "origin", "trunk")
    repo.git("remote", "set-head", "origin", "trunk")
    client.branch("only", group="topic")
    repo.commit("a", "only\n", "only")

    repo.stack("integrate", "combined", "--group")
    assert repo.git("rev-parse", "combined^1") == tip(repo, "trunk")
    client.group_rename("renamed", "topic")
    client.branch("new", group="renamed", from_branch="trunk")
    repo.commit("b", "new\n", "new")

    repo.stack("restack", "combined")

    assert repo.git("show", "combined:b") == "new"


def test_pull_updates_a_new_group_source_before_rebuilding(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("first", group="topic")
    repo.commit("a", "first\n", "first")
    repo.stack("integrate", "combined", "-g")
    client.branch("new", group="topic", from_branch="main")
    repo.commit("b", "new\n", "new")
    repo.commit_on_forge("main", "c", "trunk change\n", "trunk change")

    repo.stack("pull", "combined", "-g", "topic", "--latest")

    assert repo.git("merge-base", "combined", "new") == tip(repo, "new")
    assert repo.git("show", "combined:c") == "trunk change"
    repo.assert_stacked("new", "main", ["new"])


def test_group_integration_can_refresh_with_no_remaining_leaves(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("only", group="topic")
    repo.commit("a", "only\n", "only")
    repo.stack("integrate", "combined", "-g")
    client.group_remove(["only"])

    repo.stack("restack", "combined")

    assert tip(repo, "combined") == tip(repo, "main")
    assert "combined (integration)" in repo.stack("info", "combined", "--offline").stdout
    client.branch("new", group="topic", from_branch="main")
    repo.commit("b", "new\n", "new")
    repo.stack("restack", "combined")

    assert repo.git("show", "combined:b") == "new"


def test_group_integration_can_rebuild_an_adopted_ancestor_snapshot(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("leaf", group="topic")
    repo.commit("a", "snapshot\n", "snapshot")
    repo.git("branch", "combined")
    repo.commit("b", "new work\n", "new work")
    repo.stack("integrate", "combined", "--adopt", "-g")

    repo.stack("restack", "combined")

    assert repo.git("show", "combined:b") == "new work"


def test_group_integration_uses_the_parent_after_sync_deletes_a_merged_leaf(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("bottom", group="topic")
    repo.commit("a", "bottom\n", "bottom")
    client.branch("leaf")
    repo.commit("b", "leaf\n", "leaf")
    repo.stack("integrate", "combined", "-g")
    repo.git("switch", "bottom")
    repo.git("merge", "--ff-only", "leaf")

    repo.stack("pull", "combined", "-g", "topic", "-d", "local")

    assert not repo.git("branch", "--list", "leaf")
    assert "(of bottom)" in repo.stack("info", "combined", "--offline").stdout
    assert repo.git("show", "combined:b") == "leaf"


def test_group_integration_sync_can_retry_after_its_last_leaf_lands(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("only", group="topic")
    repo.commit("a", "only\n", "only")
    repo.stack("integrate", "combined", "-g")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "only")
    repo.git("push", "-q", "origin", "main")

    command = ("sync", "combined", "-g", "topic", "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, expect_success=False).stderr
    assert not repo.git("branch", "--list", "only")

    repo.stack(*command)

    assert tip(repo, "combined", remote=True) == tip(repo, "main")


def test_group_rename_updates_an_interrupted_initial_integration(
    repo: Repo, client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("leaf", group="topic")
    repo.commit("a", "leaf\n", "leaf")

    def stop(integrations: Integrations, name: str, config: Integration) -> None:
        assert tip(repo, name) == config["tip"]
        raise StackError("interrupted after updating ref")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "save", stop)
        result = repo.stack("integrate", "combined", "-g", expect_success=False)
        assert "interrupted after updating ref" in result.stderr

    client.group_rename("renamed", "topic")
    repo.stack("integrate", "--abort", "combined")
    repo.stack("restack", "combined")

    assert repo.git("show", "combined:a") == "leaf"
    assert "(of leaf)" in repo.stack("info", "combined", "--offline").stdout


def test_restack_refuses_to_move_a_new_group_leaf_onto_an_integration(repo: Repo, client: StackClient) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("first", group="topic")
    repo.commit("a", "first\n", "first")
    repo.stack("integrate", "combined", "-g")
    client.branch("fresh", group="topic", from_branch="main")
    repo.commit("b", "fresh\n", "fresh")
    previous = tip(repo, "combined")

    result = repo.stack("restack", "fresh", "--onto", "combined", expect_success=False)

    assert "fresh is an integration or depends on one" in result.stderr
    assert tip(repo, "combined") == previous
    repo.assert_stacked("fresh", "main", ["fresh"])


def test_group_integration_can_record_an_already_landed_leaf_in_a_linked_worktree(
    repo: Repo, client: StackClient
) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("first", group="topic")
    repo.commit("a", "first\n", "first")
    repo.stack("integrate", "combined", "-g")
    previous = tip(repo, "combined")
    repo.git("worktree", "add", str(repo.tmp / "linked"), "combined")
    client.branch("empty", group="topic", from_branch="main")

    repo.stack("restack", "combined")

    assert tip(repo, "combined") == previous
    assert "(of first, empty)" in repo.stack("info", "combined", "--offline").stdout

    client.group_remove(["empty"])
    repo.commit("b", "not in the group\n", "not in the group")
    repo.stack("restack", "combined")

    assert tip(repo, "combined") == previous
    assert "(of first)" in repo.stack("info", "combined", "--offline").stdout


@pytest.mark.parametrize("with_child", [False, True])
def test_adopt_refuses_a_new_input_of_a_group_integration(repo: Repo, client: StackClient, with_child: bool) -> None:
    repo.git("remote", "set-head", "origin", "main")
    client.branch("first", group="topic")
    repo.commit("a", "first\n", "first")
    repo.stack("integrate", "combined", "-g")
    client.branch("fresh", group="topic", from_branch="main")
    repo.commit("b", "fresh\n", "fresh")
    if with_child:
        client.branch("child")
        repo.commit("c", "child\n", "child")
    source(repo, "other", "c")

    result = repo.stack("integrate", "fresh", "first", "other", "--adopt", expect_success=False)

    assert "fresh is an input of integration combined" in result.stderr
    assert client.stacks().parent("fresh") == "main"
    assert Integrations(client).read("fresh") is None


def test_group_integration_requires_a_group(repo: Repo, client: StackClient) -> None:
    client.branch("solo")
    result = repo.stack("integrate", "combined", "-g", expect_success=False)

    assert "isn't in a group" in result.stderr
    assert not repo.git("branch", "--list", "combined")


def test_info_marks_landed_integration_input_and_refresh(repo: Repo, gh: FakeGitHub) -> None:
    integration(repo)
    work(repo)
    repo.git("push", "--set-upstream", "origin", "one")
    included = tip(repo, "one")
    repo.squash_merge_on_forge("one", delete_branch=False)
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=included,
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main", remote=True)},
    )
    repo.git("fetch", "origin")
    repo.git("branch", "-f", "main", "origin/main")
    repo.git("config", "color.stack", "always")

    result = repo.stack("info")

    assert "\033[36mone\033[m" in result.stdout
    assert "\033[33mcombined\033[m" in result.stdout and "!" in result.stdout
    assert "\033[33mcombined\033[m \033[33m!\033[m (of " in result.stdout
    repo.git("config", "color.stack", "never")
    assert "combined (of one, two) (needs restack)" in repo.stack("info", "--offline").stdout
    repo.git("config", "color.stack", "always")
    repo.stack("restack")
    refreshed = repo.stack("info")
    assert "\033[36mone\033[m" in refreshed.stdout
    assert "!" not in refreshed.stdout


def work(repo: Repo) -> None:
    repo.git("switch", "--quiet", "combined")
    repo.stack("create", "work")
    repo.commit("c", "work\n", "work")


def test_create_refresh_and_replace_ordered_sources(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    before = tip(repo, "combined")
    assert repo.git("rev-parse", "combined~1^2", "combined^2").splitlines() == [tip(repo, "one"), tip(repo, "two")]
    assert repo.git("rev-parse", "refs/heads/combined~2") == tip(repo, "main")

    repo.git("switch", "--quiet", "combined")
    assert repo.stack("info", "--offline").stdout.splitlines()[0] == "combined (of one, two) *"
    git_calls.clear()
    repo.stack("restack")
    assert tip(repo, "combined") == before
    assert not any(call[:2] == ["worktree", "add"] for call in git_calls)

    repo.stack("integrate", "combined", "two")
    assert repo.stack("info", "--offline").stdout.splitlines()[0] == "combined (of two) *"
    assert repo.git("show", "combined:b") == repo.git("show", "two:b")
    assert repo.git("rev-parse", "refs/heads/combined^1") == tip(repo, "main")
    assert repo.git("branch", "--show-current") == "combined"
    assert repo.git("status", "--porcelain") == ""


def test_integration_appends_new_commits_in_the_existing_build_worktree(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    previous = tip(repo, "combined")
    repo.git("switch", "one")
    repo.commit("a", "later change\n", "later change")
    git_calls.clear()

    repo.stack("restack", "combined")

    assert repo.git("rev-parse", "combined^") == previous
    assert repo.git("show", "combined:a") == "later change"
    assert sum(call[0] == "cherry-pick" for call in git_calls) == 1
    assert not any(call[:2] in (["worktree", "add"], ["worktree", "remove"]) for call in git_calls)


def test_integration_refresh_preserves_shared_changes_when_one_source_reverts(repo: Repo) -> None:
    for name in ("one", "two"):
        source(repo, name, "a")
        repo.write("a", "shared change\n")
        repo.git("commit", "--amend", "-am", f"shared change in {name}")
    repo.stack("integrate", "combined", "one", "two")
    repo.git("switch", "one")
    repo.git("revert", "--no-edit", "HEAD")

    repo.stack("restack", "combined")
    repo.stack("integrate", "fresh", "one", "two")

    assert repo.git("show", "combined:a") == "shared change"
    assert repo.git("rev-parse", "combined^{tree}") == repo.git("rev-parse", "fresh^{tree}")


def test_integration_rebuilds_for_paths_that_alias_on_case_insensitive_filesystems(
    repo: Repo, git_calls: list[GitCall]
) -> None:
    repo.git("switch", "-qc", "one", "main")
    repo.write("alpha", "keep this\n")
    repo.git("add", "alpha")
    repo.git("commit", "-m", "add alpha")
    source(repo, "two", "b")
    repo.stack("integrate", "combined", "one", "two")
    repo.git("switch", "two")
    repo.write("ALPHA", "temporary\n")
    repo.git("add", "ALPHA")
    repo.git("commit", "-m", "add uppercase alpha")
    repo.git("revert", "--no-edit", "HEAD")
    git_calls.clear()

    repo.stack("restack", "combined")

    assert repo.git("show", "combined:alpha") == "keep this"
    assert not any(call[0] == "cherry-pick" for call in git_calls)


def test_integration_rebuilds_when_an_appended_commit_aliases_an_unchanged_root_file(
    repo: Repo, git_calls: list[GitCall]
) -> None:
    repo.git("config", "core.ignorecase", "true")
    integration(repo)
    repo.git("switch", "one")
    before = tip(repo, "one")
    clean_tree = repo.git("rev-parse", "HEAD^{tree}")
    # Record a spelling Git can represent even on a case-insensitive worktree.
    blob = repo.git("rev-parse", "HEAD:c")
    repo.git("update-index", "--add", "--cacheinfo", f"100644,{blob},C")
    alias_tree = repo.git("write-tree")
    repo.git("read-tree", clean_tree)
    alias = repo.git("commit-tree", alias_tree, "-p", before, "-m", "add alias")
    without_alias = repo.git("commit-tree", clean_tree, "-p", alias, "-m", "remove alias")
    repo.git("switch", "two")
    repo.git("update-ref", "refs/heads/one", without_alias, before)
    git_calls.clear()

    repo.stack("restack", "combined")

    assert repo.git("show", "combined:c") == repo.git("show", "main:c")
    assert not any(call[0] == "cherry-pick" for call in git_calls)


def test_integration_combines_source_tips_without_replaying_reverted_conflicts(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    repo.git("revert", "--no-edit", "HEAD")

    repo.stack("integrate", "combined", "one", "two")

    assert repo.git("show", "combined:a") == "one"


def test_integration_refresh_respects_merge_signature_verification(repo: Repo, git_calls: list[GitCall]) -> None:
    integration(repo)
    previous = tip(repo, "combined")
    repo.git("config", "merge.verifySignatures", "true")
    repo.git("switch", "one")
    repo.commit("a", "unsigned change\n", "unsigned change")
    git_calls.clear()

    result = repo.stack("restack", "combined", expect_success=False)

    assert "signature" in result.stderr.lower()
    assert tip(repo, "combined") == previous
    assert not any(call[0] == "cherry-pick" for call in git_calls)


def test_releasing_the_cached_worktree_keeps_the_integration(repo: Repo) -> None:
    integration(repo)
    previous = tip(repo, "combined")
    path = Integrations(repo.client).path("combined")
    assert path.is_dir()

    repo.stack("integrate", "--abort", "combined")

    assert tip(repo, "combined") == previous
    assert not path.exists()


def test_integration_rebuilds_after_a_source_commit_is_amended(repo: Repo) -> None:
    integration(repo)
    previous = tip(repo, "combined")
    repo.git("switch", "one")
    repo.write("a", "rewritten change\n")
    repo.git("commit", "--amend", "-am", "rewritten change")

    repo.stack("restack", "combined")

    assert repo.git("rev-parse", "combined~1^2") == tip(repo, "one")
    assert repo.git("merge-base", "combined", previous) == tip(repo, "two")
    assert repo.git("show", "combined:a") == "rewritten change"
    assert repo.git("show", "combined:b") == "two"


def test_failed_integration_discards_its_conflicting_checkout(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")

    result = repo.stack("integrate", "combined", "one", "two", expect_success=False)

    assert "restack" in result.stderr and "two" in result.stderr
    assert not repo.git("branch", "--list", "combined")
    assert Integrations(repo.client).pending("combined") is None
    assert not Integrations(repo.client).path("combined").exists()


def test_integration_preserves_source_merge_edits_and_aborts_native_merge_conflicts(repo: Repo) -> None:
    source(repo, "side", "c")
    source(repo, "one", "a")
    repo.merge_with_an_edit("side", "d", "merge edit\n")
    source(repo, "two", "b")
    repo.stack("integrate", "combined", "one", "two")
    assert repo.git("show", "combined:d") == "merge edit"
    assert repo.git("merge-base", "combined", "one") == tip(repo, "one")

    repo.commit("b", "later change\n", "later change")
    repo.stack("restack", "combined")
    assert repo.git("rev-parse", "combined^2") == tip(repo, "two")
    assert repo.git("show", "combined:d") == "merge edit"

    previous = tip(repo, "combined")
    repo.commit("a", "conflict with one\n", "conflict")
    repo.merge_with_an_edit("side", "d", "other merge edit\n")
    result = repo.stack("restack", "combined", expect_success=False)

    assert "git stack restack two" in result.stderr
    assert tip(repo, "combined") == previous
    assert Integrations(repo.client).pending("combined") is None
    assert not Integrations(repo.client).path("combined").exists()


def test_retry_keeps_a_staged_resolution_from_a_legacy_build(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    integrations = Integrations(repo.client)
    path = integrations.path("combined")

    def stop(integrations: Integrations, name: str, source: str) -> StackError:
        build = integrations.pending(name)
        assert build is not None
        build.pop("replay")
        build["integration"].pop("automatic")
        integrations.journal(name, build)
        return StackError("legacy merge stopped")

    command = ("integrate", "combined", "one", "two")
    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "conflict", stop)
        assert "legacy merge stopped" in repo.stack(*command, expect_success=False).stderr
    (path / "a").write_text("manual resolution\n", encoding="utf-8")
    repo.git("add", "a", cwd=path)

    assert "merge --continue" in repo.stack(*command, expect_success=False).stderr
    assert repo.git("show", ":a", cwd=path) == "manual resolution"

    repo.git("commit", "--no-edit", cwd=path)
    repo.stack(*command)
    assert repo.git("show", "combined:a") == "manual resolution"


def test_integration_rejects_alias_of_generated_tip(repo: Repo) -> None:
    integration(repo)
    before = tip(repo, "combined")
    repo.git("branch", "copied-base", "combined")

    failure = repo.stack("integrate", "combined", "copied-base", "two", expect_success=False)

    assert "copied-base contains the generated tip" in failure.stderr
    assert tip(repo, "combined") == before


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
    assert repo.git("show", "combined:a") == repo.git("show", "one:a")

    repo.stack("group", "remove", "one")
    before = tip(repo, "one")
    repo.commit_on_forge("main", "d", "another\n", "another")
    repo.stack("pull", "-g", "both", "--latest")
    assert tip(repo, "one") == before
    assert repo.git("show", "combined:a") == repo.git("show", f"{before}:a")


def resolve_source(repo: Repo, parent: str = "main") -> None:
    repo.git("switch", "--quiet", "two")
    if not repo.config_exists("two", "stackParent"):
        repo.stack("track", "main", "two")
    command = ("restack", "two", "--onto", parent)
    assert "CONFLICT" in repo.stack(*command, expect_success=False).stdout
    repo.write("a", "resolved\n")
    repo.git("add", "a")
    repo.git("-c", "core.editor=true", "rebase", "--continue")
    repo.stack(*command)


def interrupt_integration_build(repo: Repo, monkeypatch: pytest.MonkeyPatch, *command: str) -> pathlib.Path:
    def stop_before_publishing(*args: object, **kwargs: object) -> None:
        raise StackError("interrupted before publication")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "publish", stop_before_publishing)
        assert "interrupted before publication" in repo.stack(*command, expect_success=False).stderr
    return Integrations(repo.client).path("combined")


def test_conflict_can_be_resolved_by_restacking_the_source(repo: Repo) -> None:
    source(repo, "one", "a")
    source(repo, "two", "a")
    command = ("integrate", "combined", "one", "two")
    assert "git stack restack two" in repo.stack(*command, expect_success=False).stderr
    resolve_source(repo, parent="one")

    repo.stack(*command)

    assert repo.git("show", "refs/heads/combined:a") == "resolved"


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
    assert repo.git("show", "combined:b") == repo.git("show", "two:b")


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

    before = tip(repo, "combined")
    result = repo.stack("pull", "combined", expect_success=False)

    assert "git stack restack one" in result.stderr
    assert tip(repo, "combined") == before
    assert repo.git("show", "one:a") == "new work"


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


def test_aborting_missing_worktree_preserves_previous_tip(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    before = tip(repo, "combined")
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "new source change\n", "change")
    path = interrupt_integration_build(repo, monkeypatch, "restack", "combined")
    shutil.rmtree(path)

    repo.stack("integrate", "--abort", "combined")

    assert tip(repo, "combined") == before
    assert str(path) not in repo.git("worktree", "list", "--porcelain")
    assert repo.client.git.config_get("combined", "stackIntegrationBuild") is None


def test_input_moving_during_replay_prevents_publish(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    before = tip(repo, "combined")
    old_one = tip(repo, "one")
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "changed\n", "changed")
    run = GitClient.run

    def move_input(git: GitClient, *args: str, check: bool = True, input: str | None = None) -> str:
        result = run(git, *args, check=check, input=input)
        if args[:1] == ("cherry-pick",) and tip(repo, "one") == old_one:
            repo.git("update-ref", "refs/heads/one", tip(repo, "main"), old_one)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "run", move_input)
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

    assert "git stack restack two" in result.stderr
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
    assert "git stack restack two" in repo.stack(*command, expect_success=False).stderr
    assert repo.git("branch", "--list", "one") == ""
    resolve_source(repo)

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
    assert repo.git("merge-base", "combined", replacement) == replacement
    assert repo.git("merge-base", "source-child", replacement, cwd=repo.origin) == replacement
    saved = Integrations(repo.client).read("combined")
    assert saved is not None and "retired_sources" not in saved


@pytest.mark.parametrize(("conflict", "delete_local"), [(False, True), (True, True), (True, False)])
def test_retired_source_keeps_squash_evidence_offline(
    repo: Repo, gh: FakeGitHub, conflict: bool, delete_local: bool
) -> None:
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
    command = ("sync", "combined", "one", "--delete", "local") if delete_local else ("sync", "combined", "one")
    result = repo.stack(*command, expect_success=not conflict)
    assert (repo.git("branch", "--list", "one") == "") is delete_local
    repo.git("config", "stack.adapter", "git")
    if conflict:
        assert "git stack restack two" in result.stderr
        resolve_source(repo)
        repo.stack(*command)
    repo.stack("restack", "combined")
    assert repo.git("show", "combined:a") == ("resolved" if conflict else "landed replacement")


def test_retry_with_only_retired_source_preserves_group_scope(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.stack("create", "outside", "--from", "combined")
    repo.commit("c", "outside work\n", "outside work")
    before = tip(repo, "outside")
    old_work = tip(repo, "work")
    repo.stack("group", "add", "both", "one", "work")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.commit("a", "conflict\n", "conflict")
    command = ("sync", "-g", "both", "--delete", "local")
    assert "git stack restack two" in repo.stack(*command, expect_success=False).stderr
    resolve_source(repo)

    repo.stack(*command)

    assert tip(repo, "outside") == before
    assert tip(repo, "work") != old_work


def test_retry_keeps_unselected_group_peer_out_of_scope(repo: Repo) -> None:
    integration(repo, tracked=True)
    repo.stack("create", "child", "--from", "one")
    repo.commit("c", "child work\n", "child")
    source(repo, "peer", "c", tracked=True)
    repo.stack("group", "add", "both", "one", "peer")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.commit("a", "conflict\n", "conflict")
    before = tip(repo, "peer")
    command = ("sync", "combined", "one", "--delete", "local")
    assert "git stack restack two" in repo.stack(*command, expect_success=False).stderr
    resolve_source(repo)

    repo.stack("sync", "-g", "both")
    assert repo.git("ls-remote", "origin", "refs/heads/combined") == ""
    result = repo.stack(*command)

    assert "peer" not in result.stdout
    assert tip(repo, "peer") == before


@pytest.mark.parametrize("group_args", [("-g", "both"), ("-g",)])
@pytest.mark.parametrize("rename_group", [False, True])
def test_retry_selects_recorded_integration_after_its_last_work_lands(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, group_args: tuple[str, ...], rename_group: bool
) -> None:
    integration(repo, tracked=True)
    work(repo)
    landed = tip(repo, "work")
    source(repo, "peer", "b", tracked=True)
    repo.stack("group", "add", "both", "one", "work", "peer")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.git("cherry-pick", landed)
    repo.git("switch", "peer")
    command = ("sync", *group_args, "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, expect_success=False).stderr
    assert not {"one", "work"}.intersection(repo.client.stacks())

    if rename_group:
        repo.stack("group", "rename", "both", "renamed")
        command = ("sync", *(("-g", "renamed") if len(group_args) > 1 else group_args), "--delete", "local")
    repo.stack(*command)

    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


def test_group_retry_does_not_publish_integration_on_another_remote(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration(repo, tracked=True)
    work(repo)
    landed = tip(repo, "work")
    repo.git("push", "--set-upstream", "origin", "one", "work")
    source(repo, "peer", "b", tracked=True)
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", str(repo.origin), str(other))
    repo.git("remote", "add", "other", str(other))
    repo.git("push", "--set-upstream", "other", "peer")
    repo.stack("group", "add", "both", "one", "work", "peer")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.git("cherry-pick", landed)
    command = ("sync", "-g", "both", "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, "--remote", "origin", expect_success=False).stderr
    assert not {"one", "work"}.intersection(repo.client.stacks())

    for selectors in (("-g", "both"), ("one", "-g", "both")):
        repo.stack("sync", *selectors, "--remote", "other")
        assert repo.git("ls-remote", "other", "refs/heads/combined") == ""

    repo.stack(*command, "--remote", "origin")
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


@pytest.mark.parametrize("method", ["add", "rename", "create", "merge", "pull"])
def test_reused_group_name_does_not_inherit_integration_retry(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    integration(repo, tracked=True)
    work(repo)
    landed = tip(repo, "work")
    repo.stack("group", "add", "both", "one", "work")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.git("cherry-pick", landed)
    command = ("sync", "-g", "both", "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, expect_success=False).stderr
    if method == "create":
        repo.stack("create", "peer", "--from", "main", "--group", "both")
        repo.commit("c", "new peer\n", "new peer")
    elif method in ("merge", "pull"):
        source(repo, "both", "a", tracked=True)
        repo.stack("create", "peer")
        repo.commit("b", "new peer\n", "new peer")
        repo.git("switch", "main")
        repo.git("merge", "--ff-only", "both")
        if method == "pull":
            base = repo.git("merge-base", "both", "two")
            repo.stack("track", "both", "two", "--base", base)
            repo.stack("pull", "combined", "both")
        else:
            repo.stack("clean")
    else:
        source(repo, "peer", "c", tracked=True)
    if method == "rename":
        repo.stack("group", "add", "new", "peer")
        repo.stack("group", "rename", "new", "both")
    elif method == "add":
        repo.stack("group", "add", "both", "peer")

    repo.stack(*command)
    assert repo.git("ls-remote", "origin", "refs/heads/combined") == ""


def test_successful_group_retry_consumes_integration_selection(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo, tracked=True)
    work(repo)
    landed = tip(repo, "work")
    repo.stack("create", "child", "--from", "one")
    repo.commit("c", "child\n", "child")
    source(repo, "peer", "b", tracked=True)
    repo.stack("group", "add", "both", "one", "work", "peer")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.git("cherry-pick", landed)
    command = ("sync", "-g", "both", "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, expect_success=False).stderr
    repo.stack("group", "remove", "child")
    repo.stack(*command)
    published = tip(repo, "combined", remote=True)

    repo.commit("b", "advance two\n", "advance two")
    repo.stack(*command)

    assert tip(repo, "combined", remote=True) == published


def test_retry_accepts_legacy_retired_group_metadata(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo, tracked=True)
    work(repo)
    landed = tip(repo, "work")
    repo.stack("group", "add", "both", "one", "work")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    repo.git("switch", "two")
    repo.git("cherry-pick", landed)
    command = ("sync", "one", "-g", "both", "--delete", "local")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        assert "push failed" in repo.stack(*command, expect_success=False).stderr
    integrations = Integrations(repo.client)
    config = integrations.read("combined")
    assert config is not None
    for entry in config.get("retired_sources", {}).values():
        entry.pop("integration_groups", None)
        entry.pop("refresh_remote", None)
        entry.pop("members", None)
    integrations.save("combined", config)

    repo.stack(*command)
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


def test_retry_does_not_follow_retired_source_child_into_an_unselected_stack(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration(repo, tracked=True)
    repo.stack("create", "child", "--from", "one")
    repo.commit("c", "child work\n", "child")
    source(repo, "unrelated", "b", tracked=True)
    repo.git("push", "--set-upstream", "origin", "unrelated")
    published = tip(repo, "unrelated", remote=True)
    repo.commit("b", "unpublished work\n", "advance unrelated")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    command = ("sync", "one", "combined", "--delete", "local")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise StackError("interrupted before build")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "build", interrupt)
        assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
    repo.stack("restack", "child", "--onto", "unrelated")
    repo.stack(*command)

    assert tip(repo, "unrelated", remote=True) == published
    assert tip(repo, "combined", remote=True) == tip(repo, "combined")


def test_retry_keeps_child_when_its_stack_bottom_tracks_another_remote(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(repo, "parent", "a", tracked=True)
    other = repo.tmp / "other.git"
    repo.git("clone", "--bare", str(repo.origin), str(other))
    repo.git("remote", "add", "other", str(other))
    repo.git("push", "--set-upstream", "other", "parent")
    repo.stack("create", "one")
    repo.commit("b", "source work\n", "source work")
    repo.stack("create", "child")
    repo.commit("c", "child work\n", "child work")
    repo.git("push", "--set-upstream", "origin", "one", "child")
    repo.commit("c", "unpublished child work\n", "advance child")
    repo.git("switch", "-c", "two", "main")
    repo.git("commit", "--allow-empty", "-m", "two")
    repo.git("branch", "release", "one")
    repo.stack("integrate", "combined", "one", "two", "--root", "release")
    repo.git("branch", "-f", "parent", "one")
    command = ("sync", "combined", "one", "--remote", "origin", "--delete", "local")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise StackError("interrupted before build")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "build", interrupt)
        assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
    assert "one" not in repo.client.stacks()

    repo.stack(*command)

    assert tip(repo, "child", remote=True) == tip(repo, "child")


def test_retry_keeps_selected_sibling_after_source_stack_splits(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    source(repo, "parent", "a", tracked=True)
    repo.stack("create", "child")
    repo.commit("b", "child work\n", "child work")
    repo.stack("create", "sibling", "--from", "parent")
    repo.commit("c", "sibling work\n", "sibling work")
    repo.git("push", "--set-upstream", "origin", "sibling")
    repo.commit("c", "unpublished sibling work\n", "advance sibling")
    repo.stack("integrate", "combined", "parent", "child")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "child")
    command = ("sync", "child", "combined", "--delete", "local")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise StackError("interrupted before build")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "build", interrupt)
        assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
    assert not {"parent", "child"}.intersection(repo.client.stacks())

    repo.stack(*command)

    assert tip(repo, "sibling", remote=True) == tip(repo, "sibling")


def test_retry_keeps_surviving_child_after_source_parent_merges(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    source(repo, "parent", "a", tracked=True)
    repo.stack("create", "one")
    repo.commit("b", "source work\n", "source work")
    repo.stack("create", "child")
    repo.commit("c", "child work\n", "child work")
    repo.git("push", "--set-upstream", "origin", "child")
    repo.commit("c", "unpublished child work\n", "advance child")
    source(repo, "two", "c")
    repo.git("branch", "release", "one")
    repo.stack("integrate", "combined", "one", "two", "--root", "release")
    repo.git("branch", "-f", "parent", "one")
    command = ("sync", "combined", "one", "--delete", "local")

    def interrupt(*args: object, **kwargs: object) -> None:
        raise StackError("interrupted before build")

    with monkeypatch.context() as patch:
        patch.setattr(Integrations, "build", interrupt)
        assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
        assert repo.git("branch", "--list", "one") == ""
        repo.git("switch", "main")
        repo.git("merge", "--ff-only", "parent")
        assert "interrupted before build" in repo.stack(*command, expect_success=False).stderr
        assert repo.git("branch", "--list", "parent") == ""

    repo.stack(*command)

    assert tip(repo, "child", remote=True) == tip(repo, "child")


@pytest.mark.parametrize("mode", ["squash", "fast-forward", "untracked", "no-upstream", "invalid-upstream"])
def test_narrow_retry_keeps_queued_source_deletion_until_child_is_selected(
    repo: Repo, gh: FakeGitHub, mode: str
) -> None:
    integration(repo, tracked=True)
    repo.stack("create", "source-child", "--from", "one")
    repo.commit("c", "child work\n", "child work")
    repo.stack("push")
    old_child = tip(repo, "source-child", remote=True)
    if mode == "no-upstream":
        repo.git("branch", "--unset-upstream", "source-child")
    elif mode == "invalid-upstream":
        repo.git("config", "--unset", "branch.source-child.merge")
    if mode == "squash":
        repo.squash_merge_on_forge("one", delete_branch=False)
    else:
        repo.git("fetch", "origin", cwd=repo.forge)
        repo.git("merge", "--ff-only", "origin/one", cwd=repo.forge)
        repo.git("push", "origin", "main", cwd=repo.forge)
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=tip(repo, "one"),
        baseRefName="main",
        mergeCommit={"oid": tip(repo, "main", remote=True)},
    )
    gh.add_pr(
        number=2, state="OPEN", headRefName="source-child", headRefOid=old_child, baseRefName="one", mergeCommit=None
    )
    repo.git("switch", "two")
    repo.commit("a", "conflict\n", "conflict")
    command = ("sync", "combined", "one", "--delete", "both")
    assert "git stack restack two" in repo.stack(*command, expect_success=False).stderr
    resolve_source(repo)
    if mode == "untracked":
        repo.stack("untrack", "source-child")

    repo.stack("sync", "combined", "--delete", "both")

    assert repo.git("ls-remote", "origin", "refs/heads/one")
    assert tip(repo, "source-child", remote=True) == old_child
    assert gh.load()["prs"][1]["baseRefName"] == "one"
    if mode == "untracked":
        return
    if mode in ("no-upstream", "invalid-upstream"):
        repo.stack(*command)
        assert repo.git("ls-remote", "origin", "refs/heads/one")
        repo.git("branch", "--set-upstream-to=origin/source-child", "source-child")
    repo.stack(*command)
    assert repo.git("ls-remote", "origin", "refs/heads/one") == ""
    assert tip(repo, "source-child", remote=True) == tip(repo, "source-child")
    assert (tip(repo, "source-child") != old_child) is (mode == "squash")
    assert gh.load()["prs"][1]["baseRefName"] == "main"


def test_sync_keeps_old_pr_base_until_new_base_exists_remotely(repo: Repo, gh: FakeGitHub) -> None:
    source(repo, "parent", "a", tracked=True)
    repo.stack("create", "one")
    repo.commit("b", "source work\n", "source work")
    repo.stack("create", "child")
    repo.commit("c", "child work\n", "child work")
    repo.git("push", "--set-upstream", "origin", "one", "child")
    gh.add_pr(
        number=1, state="OPEN", headRefName="child", headRefOid=tip(repo, "child"), baseRefName="one", mergeCommit=None
    )
    source(repo, "two", "c")
    repo.stack("integrate", "combined", "one", "two")
    repo.git("branch", "-f", "parent", "one")
    command = ("sync", "combined", "one", "--delete", "remote")

    repo.stack(*command)

    assert repo.git("ls-remote", "origin", "refs/heads/one")
    assert repo.git("ls-remote", "origin", "refs/heads/parent") == ""
    assert gh.load()["prs"][0]["baseRefName"] == "one"
    repo.git("push", "--set-upstream", "origin", "parent")
    repo.stack(*command)
    assert repo.git("ls-remote", "origin", "refs/heads/one") == ""
    assert gh.load()["prs"][0]["baseRefName"] == "parent"


def test_queued_alias_deletions_keep_both_child_dependencies(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo, tracked=True)
    repo.stack("integrate", "other-base", "one", "two")
    repo.stack("create", "child-one", "--from", "one")
    repo.commit("c", "first child\n", "first child")
    repo.git("branch", "alias", "one")
    repo.stack("track", "main", "alias")
    repo.stack("create", "child-alias", "--from", "alias")
    repo.commit("c", "second child\n", "second child")
    repo.git("push", "--set-upstream", "origin", "one", "child-one", "child-alias")
    repo.git("branch", "--set-upstream-to=origin/one", "alias")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    passthrough = GitClient.passthrough

    def reject_push(git: GitClient, *args: str) -> bool:
        return False if args[:2] == ("push", "--atomic") else passthrough(git, *args)

    with monkeypatch.context() as patch:
        patch.setattr(GitClient, "passthrough", reject_push)
        failed = repo.stack("sync", "combined", "one", "alias", "--delete", "both", expect_success=False)
        assert "push failed" in failed.stderr
    assert repo.git("branch", "--list", "one", "alias") == ""
    integrations = Integrations(repo.client)
    [(_, deletion)] = integrations.pending_deletions({"combined"}, "origin")
    integrations.remember_deletion("other-base", deletion)

    repo.stack("sync", "combined", "child-alias", "--delete", "both")

    assert repo.git("ls-remote", "origin", "refs/heads/one")
    repo.stack("sync", "combined", "child-one", "child-alias", "--delete", "both")
    assert repo.git("ls-remote", "origin", "refs/heads/one") == ""
    assert integrations.pending_deletions({"combined", "other-base"}, "origin") == []


def test_legacy_queued_deletion_does_not_require_other_remote_stacks(repo: Repo) -> None:
    integration(repo, tracked=True)
    work(repo)
    repo.git("push", "--set-upstream", "origin", "one")
    deletion = RemoteDeletion("origin", "refs/heads/one", tip(repo, "one"))
    key = Integrations(repo.client).deletion_key(deletion)
    repo.git("config", f"branch.combined.{key}", json.dumps([deletion.remote, deletion.ref, deletion.tip]))
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "one")
    source(repo, "unrelated", "c", tracked=True)
    other = repo.tmp / "other.git"
    repo.git("init", "--bare", str(other))
    repo.git("remote", "add", "other", str(other))
    repo.git("push", "--set-upstream", "other", "unrelated")

    repo.stack("sync", "combined", "one", "--delete", "remote")

    assert repo.git("ls-remote", "origin", "refs/heads/one") == ""
    assert repo.git("ls-remote", "other", "refs/heads/unrelated")


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
    assert repo.git("show", "combined:a") == repo.git("show", "one:a")
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


def test_rename_refuses_to_move_input_during_first_integration_build(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    source(repo, "one", "a")
    source(repo, "two", "b")
    path = interrupt_integration_build(repo, monkeypatch, "integrate", "combined", "one", "two")
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


def test_rename_refuses_pending_integration_build(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    integration(repo)
    repo.git("switch", "--quiet", "two")
    repo.commit("b", "new source change\n", "change")
    path = interrupt_integration_build(repo, monkeypatch, "restack", "combined")

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
    assert repo.git("show", "combined:a") == repo.git("show", "one:a")
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
    assert "removing the temporary worktree for combined" not in result.stdout
    if not restack_first:
        assert result.stdout.index("rebuilding integration combined") < result.stdout.index(
            "restacking work onto combined"
        )
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
    pushes = [call for call in git_calls if call[0] == "push"]
    assert len(pushes) == 1
    assert "--atomic" in pushes[0] and "--dry-run" not in pushes[0]


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

    assert "git stack restack two" in repo.stack("push", expect_success=False).stderr

    assert tip(repo, "combined", remote=True) == base
    assert tip(repo, "work", remote=True) == work_tip
    assert Integrations(repo.client).pending("combined") is None


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
