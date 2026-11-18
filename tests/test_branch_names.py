"""Branches are passed to git as branches, never by a bare name a tag could
have too: git takes a bare name for a tag before a branch, warning that it's
ambiguous. Each test tags every branch name, then fails on that warning."""

import os
import pathlib
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from git_client import Arg, CatFile, Git, decode

from git_stack.core import Delete
from git_stack.stack import BranchStatus, StackClient
from tests.helpers import FakeGitHub, Repo


@pytest.fixture
def no_ambiguous_names(monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> Iterator[None]:
    """Fail the test if git warns that a name GitClient passed it is
    ambiguous, in the output it captures or in what it lets through to the
    terminal (e.g. a rebase's)."""
    warnings: list[str] = []
    process = Git.process

    def checked(self: Git, *args: Arg, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        result = process(self, *args, **kwargs)
        # What it captured, and what it let through to the terminal, e.g. a rebase's.
        text = decode(result.stderr or b"") + capfd.readouterr().err
        command = " ".join(map(os.fsdecode, args))
        warnings.extend(f"{line} (git {command})" for line in text.splitlines() if "is ambiguous" in line)
        return result

    def asked_of_rev_parse(method: Any) -> Any:
        def ask(self: CatFile, name: str, *args: Any, **kwargs: Any) -> Any:
            # cat-file keeps its warnings to itself, so ask rev-parse too, which says them.
            assert isinstance(self.git, Git)
            checked(self.git, "rev-parse", "--verify", "--quiet", name)
            return method(self, name, *args, **kwargs)

        return ask

    # Every git process GitClient starts, and every name it asks its cat-file.
    monkeypatch.setattr(Git, "process", checked)
    monkeypatch.setattr(CatFile, "resolve", asked_of_rev_parse(CatFile.resolve))
    monkeypatch.setattr(CatFile, "verify", asked_of_rev_parse(CatFile.verify))
    yield
    assert warnings == []


def shadow(repo: Repo, *branches: str) -> None:
    """Tag each of branches at the first commit, so its bare name means the
    tag. Pushing doesn't send tags, so the remote doesn't get them."""
    first = repo.git("rev-list", "--max-parents=0", "refs/heads/main")
    for branch in branches:
        repo.git("tag", branch, first)


def stacked_on(repo: Repo, branch: str, parent: str) -> bool:
    """Whether branch is tracked on parent, and built on its tip."""
    tip = repo.git("rev-parse", f"refs/heads/{parent}")
    return repo.config(branch, "stackParent") == parent and repo.git("merge-base", tip, f"refs/heads/{branch}") == tip


@pytest.mark.usefixtures("no_ambiguous_names")
def test_restack_info_and_push(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    shadow(repo, "main", "pr1", "pr2", "pr3")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 third\n", "pr1 c3")

    client.restack()

    assert stacked_on(repo, "pr2", "pr1")
    assert stacked_on(repo, "pr3", "pr2")
    assert {b.name: b.status for b in client.info(offline=True) if b.parent} == {
        "pr1": BranchStatus.UP_TO_DATE,
        "pr2": BranchStatus.UP_TO_DATE,
        "pr3": BranchStatus.UP_TO_DATE,
    }
    assert client.push().pushed == ["pr1", "pr2", "pr3"]
    assert client.parent("pr2") == "pr1"
    assert client.show_stack("pr2") == ["pr1", "pr2", "pr3"]


@pytest.mark.usefixtures("no_ambiguous_names")
def test_create_from_and_restack_onto_a_named_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    shadow(repo, "main", "pr1", "pr2", "pr3")
    repo.git("switch", "--quiet", "main")

    client.branch("pr1b", from_branch="pr1")
    client.restack(["pr2"], onto="main")

    assert stacked_on(repo, "pr1b", "pr1")
    assert stacked_on(repo, "pr2", "main")
    assert stacked_on(repo, "pr3", "pr2")


@pytest.mark.usefixtures("no_ambiguous_names")
def test_sync_untracks_and_deletes_a_merged_branch(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    shadow(repo, "main", "pr1", "pr2", "pr3")

    client.sync(delete=Delete.LOCAL)

    assert repo.git("branch", "--list", "pr1") == ""
    assert stacked_on(repo, "pr2", "main")
    assert stacked_on(repo, "pr3", "pr2")


@pytest.mark.usefixtures("no_ambiguous_names")
def test_sync_pulls_commits_pushed_by_others(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.commit_on_forge("pr2", "b", "theirs\n", "theirs")
    shadow(repo, "main", "pr1", "pr2", "pr3")

    client.sync(push=False)

    assert "theirs" in repo.git("log", "--format=%s", "refs/heads/main..refs/heads/pr2").splitlines()


@pytest.mark.usefixtures("no_ambiguous_names")
def test_clean_rename_untrack_and_moving_around(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")
    shadow(repo, "main", "pr1", "pr2", "pr3", "renamed")

    client.clean()
    client.rename("renamed", "pr3")
    repo.git("switch", "--quiet", "pr2")
    assert client.next() == "renamed"
    assert client.prev() == "pr2"
    client.untrack(["renamed"])

    assert repo.config("pr2", "stackParent") == "main"
    assert not repo.config_exists("renamed", "stackParent")


@pytest.mark.usefixtures("no_ambiguous_names")
def test_opening_pull_requests_and_syncing_with_github(repo: Repo, gh: FakeGitHub, client: StackClient) -> None:
    repo.make_stack()
    shadow(repo, "main", "pr1", "pr2", "pr3")
    client.push(prs=True)
    repo.squash_merge_on_forge("pr1")
    # Merge the pull request push opened for pr1.
    state = gh.load()
    pr = next(pr for pr in state["prs"] if pr["headRefName"] == "pr1")
    pr.update(state="MERGED", mergeCommit={"oid": repo.git("rev-parse", "HEAD", cwd=repo.forge)})
    gh.save(state)

    client.sync(delete=Delete.BOTH)

    assert repo.git("branch", "--list", "pr1") == ""
    assert stacked_on(repo, "pr2", "main")


@pytest.mark.usefixtures("no_ambiguous_names")
def test_import_tracks_a_local_branch_on_a_trunk_checked_out_elsewhere(
    repo: Repo, gh: FakeGitHub, client: StackClient, tmp_path: pathlib.Path
) -> None:
    repo.git("switch", "--quiet", "-c", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.git("push", "--quiet", "-u", "origin", "pr1")
    gh.add_pr(number=1, state="OPEN", headRefName="pr1", baseRefName="main", headRefOid=repo.git("rev-parse", "HEAD"))
    repo.commit_on_forge("main", "d", "main\n", "main c1")
    repo.git("worktree", "add", "--quiet", str(tmp_path / "other"), "main")  # so import can't move main
    shadow(repo, "main", "pr1")

    client.import_stacks()

    assert stacked_on(repo, "pr1", "main")


@pytest.mark.usefixtures("no_ambiguous_names")
def test_integration_uses_branches_even_with_same_named_tags(repo: Repo) -> None:
    repo.make_stack()
    shadow(repo, "main", "pr1", "pr2")
    repo.git("tag", "combined", "refs/heads/main")

    repo.stack("integrate", "combined", "pr1", "pr2")
    repo.stack("sync", "combined")

    assert repo.git("rev-parse", "refs/heads/combined^2") == repo.git("rev-parse", "refs/heads/pr2")
