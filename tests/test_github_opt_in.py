"""GitHub access at the CLI boundary, with a real Git remote and fake gh."""

import pytest

from git_stack.cli import build_parser
from tests.helpers import FakeGitHub, Repo


@pytest.mark.parametrize(
    "command",
    [("pull",), ("sync",), ("clean",), ("push",), ("integrate", "combined"), ("unrevert", "restore", "HEAD")],
)
def test_with_prs_is_accepted_by_every_pr_aware_command(command: tuple[str, ...]) -> None:
    parser = build_parser()

    assert parser.parse_args([*command, "--prs"]).prs


@pytest.mark.parametrize("command", ["info", "restack", "pull", "clean", "integrate"])
def test_default_commands_launch_no_gh(repo: Repo, gh: FakeGitHub, command: str) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")
    repo.git("config", "stack.adapter", "github")

    args = ("combined", "pr1", "pr2") if command == "integrate" else ()
    repo.stack_process(command, *args)

    assert gh.calls() == []


@pytest.mark.parametrize("command", ["push", "sync"])
def test_existing_github_ref_checks_prs_by_default(repo: Repo, gh: FakeGitHub, command: str) -> None:
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

    denied = repo.stack_process(command, expect_success=False)
    assert "merge queue" in denied.stderr
    assert "--force" in denied.stderr
    assert repo.remote_subject("pr3") == "pr3 c1"
    assert gh.calls("api", "graphql")

    gh.clear_calls()
    repo.stack_process(command, "--force")
    assert repo.remote_subject("pr3") == "update pr3"


def test_clean_checks_prs_before_remote_delete(repo: Repo, gh: FakeGitHub) -> None:
    repo.client.branch("landed")
    repo.commit("a", "landed\n", "landed")
    repo.git("push", "--set-upstream", "origin", "landed")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "refs/heads/landed")
    repo.git("push", "origin", "main")

    repo.stack_process("clean", "-d", "remote")
    assert repo.git("ls-remote", "origin", "refs/heads/landed") == ""
    assert gh.calls("api", "graphql")


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
    flags = () if mode == "default" else ("--prs",)

    denied = repo.stack(*command, *flags, "-d", "local", expect_success=False)

    assert "can't delete one" in denied.stderr
    assert "--prs" in denied.stderr
    assert repo.git("rev-parse", "refs/heads/one") == original
    assert repo.config("one", "stackParent") == "main"
    assert bool(gh.calls()) is (mode == "no-merged-pr" or (mode == "default" and command[0] == "sync"))


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

    repo.stack(*command, "--prs", "-d", "local")
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


@pytest.mark.parametrize("option", ["--git-only", "--with-prs", "--protect-prs", "--allow-pr-update", "--pr-rebuild"])
def test_removed_pr_options_are_rejected(repo: Repo, gh: FakeGitHub, option: str) -> None:
    result = repo.stack("push", option, expect_success=False)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert gh.calls() == []


def test_github_opt_in_does_not_carry_to_next_invocation(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()
    repo.git("push", "--set-upstream", "origin", "pr1", "pr2", "pr3")

    repo.stack("pull", "--prs")
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


def test_push_rebuild_opens_missing_prs(repo: Repo, gh: FakeGitHub) -> None:
    repo.make_stack()

    repo.stack("push", "--prs", "--force")

    assert [(pr["headRefName"], pr["baseRefName"], pr["isDraft"]) for pr in gh.load()["prs"]] == [
        ("pr1", "main", True),
        ("pr2", "pr1", True),
        ("pr3", "pr2", True),
    ]


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
    repo.stack("integrate", "combined", "one", "two", "--root", "integration-base", "--prs")
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

    repo.stack("sync", "combined", "one", "--prs", "-d", "local")

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
    repo.stack("integrate", "combined", "one", "two", "--root", "integration-base", "--prs")
    repo.git("switch", "main")
    repo.git("merge", "--ff-only", "refs/heads/one")
    repo.git("branch", "--set-upstream-to=main", "one")
    gh.clear_calls()

    repo.stack("clean", "-d", "local")
    assert repo.git("branch", "--list", "one") == ""
    repo.stack("restack", "combined")

    assert source not in repo.git("rev-list", "refs/heads/combined").splitlines()
    assert gh.calls() == []
