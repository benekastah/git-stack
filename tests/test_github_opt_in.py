"""GitHub access at the CLI boundary, with a real Git remote and fake gh."""

import pytest

from git_stack.cli import build_parser
from tests.helpers import FakeGitHub, Repo


@pytest.mark.parametrize(
    "command",
    [("pull",), ("sync",), ("clean",), ("push",), ("integrate", "combined"), ("unrevert", "restore", "HEAD")],
)
def test_github_alias_is_accepted_by_every_github_command(command: tuple[str, ...]) -> None:
    parser = build_parser()

    assert parser.parse_args([*command, "--gh"]) == parser.parse_args([*command, "--github"])


@pytest.mark.parametrize("command", ["info", "restack", "pull", "sync", "clean", "push", "integrate"])
def test_default_commands_launch_no_gh(repo: Repo, gh: FakeGitHub, command: str) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")
    repo.git("config", "stack.adapter", "github")

    args = ("combined", "pr1", "pr2") if command == "integrate" else ()
    repo.stack_process(command, *args)

    assert gh.calls() == []


@pytest.mark.parametrize("command", ["push", "sync"])
def test_existing_github_ref_requires_explicit_choice(repo: Repo, gh: FakeGitHub, command: str) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")
    repo.commit("c", "new local work\n", "update pr3")
    gh.add_pr(
        number=3,
        state="OPEN",
        headRefName="pr3",
        baseRefName="pr2",
        headRefOid=repo.git("rev-parse", "refs/remotes/origin/pr3"),
        mergeCommit=None,
        isInMergeQueue=True,
    )

    for flags in ((), ("--force",)):
        denied = repo.stack_process(command, *flags, expect_success=False)
        assert "--protect-prs" in denied.stderr
        assert "--git-only" in denied.stderr
        assert repo.remote_subject("pr3") == "pr3 c1"
        assert gh.calls() == []

    denied = repo.stack_process(command, "--protect-prs", expect_success=False)
    assert "merge queue" in denied.stderr
    assert repo.remote_subject("pr3") == "pr3 c1"
    assert gh.calls("api", "graphql")

    gh.clear_calls()
    repo.stack_process(command, "--git-only")
    assert repo.remote_subject("pr3") == "update pr3"
    assert gh.calls() == []


def test_clean_refuses_remote_delete_without_opt_in(repo: Repo, gh: FakeGitHub) -> None:
    repo.client.branch("landed")
    repo.commit("a", "landed\n", "landed")
    repo.git("push", "--set-upstream", "origin", "landed")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "refs/heads/landed")
    repo.git("push", "origin", "main")

    denied = repo.stack_process("clean", "-d", "remote", expect_success=False)
    assert "--protect-prs" in denied.stderr
    assert repo.remote_subject("landed") == "landed"
    assert repo.config("landed", "stackParent") == "main"
    assert gh.calls() == []

    repo.stack_process("clean", "-d", "remote", "--git-only")
    assert repo.git("ls-remote", "origin", "refs/heads/landed") == ""
    assert gh.calls() == []


def squash_fixed_integration_source(repo: Repo, *, upstream: str = "one", build_after_merge: bool = False) -> str:
    repo.client.branch("one")
    repo.commit("a", "one\n", "one")
    repo.client.branch("two", from_branch="main")
    repo.commit("b", "two\n", "two")
    if not build_after_merge:
        repo.stack("integrate", "combined", "one", "two")
    repo.git("push", "--set-upstream", "origin", f"refs/heads/one:refs/heads/{upstream}")
    repo.squash_merge_on_forge(upstream)
    if build_after_merge:
        repo.git("fetch", "--quiet", "origin")
        repo.git("switch", "main")
        repo.git("merge", "--ff-only", "refs/remotes/origin/main")
        repo.stack("integrate", "combined", "one", "two")
    return repo.git("rev-parse", "refs/heads/one")


@pytest.mark.parametrize(
    "command",
    [("sync", "combined", "one"), ("sync", "one"), ("pull", "one"), ("clean",)],
)
@pytest.mark.parametrize("mode", ["default", "git-adapter", "git-remote", "no-merged-pr"])
def test_deletion_requires_exact_integration_source_landing(
    repo: Repo, gh: FakeGitHub, command: tuple[str, ...], mode: str
) -> None:
    original = squash_fixed_integration_source(repo)
    if mode == "git-adapter":
        repo.git("config", "stack.adapter", "git")
    elif mode == "git-remote":
        repo.git("remote", "set-url", "origin", str(repo.origin))
    flags = () if mode == "default" else ("--github",)

    denied = repo.stack(*command, *flags, "-d", "local", expect_success=False)

    assert "can't delete one" in denied.stderr
    assert "--github" in denied.stderr
    assert repo.git("rev-parse", "refs/heads/one") == original
    assert repo.config("one", "stackParent") == "main"
    assert bool(gh.calls()) is (mode == "no-merged-pr")


@pytest.mark.parametrize("command", [("clean",), ("pull", "one")])
@pytest.mark.parametrize("upstream", ["one", "review-one"])
def test_cleanup_saves_github_landing_proof_for_later_local_refresh(
    repo: Repo, gh: FakeGitHub, command: tuple[str, ...], upstream: str
) -> None:
    original = squash_fixed_integration_source(repo, upstream=upstream, build_after_merge=upstream == "one")
    assert original in repo.git("rev-list", "refs/heads/combined").splitlines()
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName=upstream,
        headRefOid=original,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "refs/heads/main", cwd=repo.origin)},
    )

    repo.stack(*command, "--github", "-d", "local")
    assert repo.git("branch", "--list", "one") == ""
    assert gh.calls("api", "graphql")
    gh.clear_calls()

    repo.stack("restack", "combined")

    assert original not in repo.git("rev-list", "refs/heads/combined").splitlines()
    assert repo.git("show", "combined:a") == "one"
    assert repo.git("show", "combined:b") == "two"
    assert gh.calls() == []


def test_local_clean_can_untrack_source_held_by_other_worktree(repo: Repo, gh: FakeGitHub) -> None:
    original = squash_fixed_integration_source(repo)
    repo.git("worktree", "add", str(repo.tmp / "source-worktree"), "one")

    repo.stack("clean", "-d", "local")

    assert repo.git("rev-parse", "refs/heads/one") == original
    repo.assert_untracked("one")
    assert gh.calls() == []


@pytest.mark.parametrize("opt_in", ["--github", "--gh", "--prs", "--protect-prs", "--rebuild-stack"])
def test_git_only_conflicts_with_github_opt_in(repo: Repo, gh: FakeGitHub, opt_in: str) -> None:
    result = repo.stack("push", "--git-only", opt_in, expect_success=False)
    assert result.returncode == 2
    assert "--git-only cannot be combined with GitHub options" in result.stderr
    assert gh.calls() == []


@pytest.mark.parametrize("flag", ["--github", "--gh"])
def test_github_opt_in_does_not_carry_to_next_invocation(repo: Repo, gh: FakeGitHub, flag: str) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")

    repo.stack("pull", flag)
    assert gh.calls("api", "graphql")
    gh.clear_calls()
    repo.stack("pull")
    assert gh.calls() == []


def test_default_unrevert_launches_no_gh(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")
    repo.squash_merge_on_forge("pr1")
    repo.revert_on_forge("HEAD")
    repo.stack("pull")

    repo.stack_process("unrevert", "restore", "main", "--stack", "pr2")

    assert repo.config("pr2", "stackParent") == "restore"
    assert gh.calls() == []


def test_default_first_push_launches_no_gh(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()

    repo.stack_process("push")

    assert repo.remote_subject("pr3") == "pr3 c1"
    assert gh.calls() == []


def test_local_pull_can_retire_merged_group_integration_source(repo: Repo, gh: FakeGitHub) -> None:
    repo.client.branch("one", group="topic")
    repo.commit("a", "one\n", "one")
    repo.client.branch("two", from_branch="main", group="topic")
    repo.commit("b", "two\n", "two")
    repo.git("remote", "set-head", "origin", "main")
    repo.stack("integrate", "combined", "-g")
    repo.git("push", "--set-upstream", "origin", "one")
    repo.squash_merge_on_forge("one")

    repo.stack("pull", "combined", "-g", "topic", "-d", "local")

    assert repo.git("branch", "--list", "one") == ""
    assert repo.git("show", "combined:a") == "one"
    assert repo.git("show", "combined:b") == "two"
    assert gh.calls() == []


@pytest.mark.parametrize("separate_remote", [True, False])
def test_clean_checks_queue_on_a_second_github_enterprise_host(
    repo: Repo, gh: FakeGitHub, separate_remote: bool
) -> None:
    mirror = repo.tmp / "mirror.git"
    repo.git("clone", "--bare", "--quiet", str(repo.origin), str(mirror))
    primary_url = "https://github.acme.example/acme/widgets.git"
    mirror_url = "https://github.mirror.example/acme/mirror.git"
    repo.git("config", "remote.origin.url", primary_url)
    repo.git("config", "--add", f"url.{repo.origin}.insteadOf", primary_url)
    repo.git("config", f"url.{mirror}.insteadOf", mirror_url)
    repo.git("config", "stack.adapter", "github")
    repo.client.branch("landed")
    repo.commit("a", "landed\n", "landed")
    if separate_remote:
        repo.git("remote", "add", "mirror", mirror_url)
        repo.git("push", "--set-upstream", "mirror", "landed")
    else:
        repo.git("push", "--set-upstream", "origin", "landed")
        repo.git("push", str(mirror), "landed")
        repo.git("config", "remote.origin.pushurl", mirror_url)
    landed = repo.git("rev-parse", "refs/heads/landed")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "refs/heads/landed")
    gh.add_pr(
        number=7,
        state="OPEN",
        headRefName="unrelated",
        baseRefName="landed",
        headRefOid=landed,
        mergeCommit=None,
        baseRepository="acme/mirror",
        isInMergeQueue=True,
    )
    clean = ("clean", "--remote", "origin", "-d", "remote")
    denied = repo.stack(*clean, expect_success=False)
    assert "--protect-prs" in denied.stderr
    assert gh.calls() == []

    denied = repo.stack(*clean, "--protect-prs", expect_success=False)

    assert "merge queue" in denied.stderr
    assert repo.git("ls-remote", str(mirror), "refs/heads/landed").split()[0] == landed
    assert any(
        "github.mirror.example" in call and any("associatedPullRequests" in arg for arg in call)
        for call in gh.calls("api", "graphql")
    )


def test_cleanup_does_not_trust_landing_after_integration_root_moves_back(repo: Repo, gh: FakeGitHub) -> None:
    original_root = repo.git("rev-parse", "refs/heads/main")
    repo.client.branch("one")
    repo.commit("a", "one\n", "one")
    source = repo.git("rev-parse", "refs/heads/one")
    repo.client.branch("two", from_branch="main")
    repo.commit("b", "two\n", "two")
    repo.git("push", "--set-upstream", "origin", "one")
    repo.squash_merge_on_forge("one")
    repo.git("fetch", "--quiet", "origin")
    landed = repo.git("rev-parse", "refs/remotes/origin/main")
    repo.git("branch", "--no-track", "integration-base", landed)
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=source,
        baseRefName="main",
        mergeCommit={"oid": landed},
    )
    repo.stack("integrate", "combined", "one", "two", "--root", "integration-base", "--github")
    repo.git("branch", "--force", "integration-base", original_root)
    repo.git("config", "branch.one.merge", "refs/heads/review-one")
    gh.clear_calls()

    result = repo.stack("clean", "-d", "local", expect_success=False)

    assert "can't delete one" in result.stderr
    assert repo.git("rev-parse", "refs/heads/one") == source
    assert gh.calls() == []


def test_selected_sync_can_retire_source_landed_under_recorded_upstream(repo: Repo, gh: FakeGitHub) -> None:
    original = squash_fixed_integration_source(repo)
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=original,
        baseRefName="main",
        mergeCommit={"oid": repo.git("rev-parse", "refs/heads/main", cwd=repo.origin)},
    )
    repo.git("config", "branch.one.merge", "refs/heads/review-one")

    repo.stack("sync", "combined", "one", "--github", "-d", "local")

    assert repo.git("branch", "--list", "one") == ""
    assert original not in repo.git("rev-list", "refs/heads/combined").splitlines()


def test_cleanup_keeps_valid_landing_proof_after_switch_to_local_upstream(repo: Repo, gh: FakeGitHub) -> None:
    source = squash_fixed_integration_source(repo)
    repo.git("fetch", "--quiet", "origin")
    landed = repo.git("rev-parse", "refs/remotes/origin/main")
    repo.git("branch", "--no-track", "integration-base", landed)
    gh.add_pr(
        number=1,
        state="MERGED",
        headRefName="one",
        headRefOid=source,
        baseRefName="main",
        mergeCommit={"oid": landed},
    )
    repo.stack("integrate", "combined", "one", "two", "--root", "integration-base", "--github")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "refs/heads/one")
    repo.git("branch", "--set-upstream-to=main", "one")
    gh.clear_calls()

    repo.stack("clean", "-d", "local")
    assert repo.git("branch", "--list", "one") == ""
    repo.stack("restack", "combined")

    assert source not in repo.git("rev-list", "refs/heads/combined").splitlines()
    assert gh.calls() == []
