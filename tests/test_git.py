"""GitClient's reading of branch config and refs, which it caches."""

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import git_client
import pytest
from git_client import Git, GitError, GitTimeoutError

from git_stack.adapters.git import GitAdapter
from git_stack.core import StackCore
from git_stack.errors import StackError
from git_stack.git import GIT_VARIABLE, GitClient, changes_refs, changes_worktrees, same_file
from tests.git_support import GitCall, real_git
from tests.helpers import Repo, install_command


def test_git_stack_runs_git_through_the_shared_client(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[object, ...]] = []
    process = Git.process

    def record(self: Git, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert self.close_fds
        commands.append(args)
        return process(self, *args, **kwargs)

    monkeypatch.setattr(Git, "process", record)

    assert GitClient(repo.path).run("branch", "--show-current") == "main"
    assert commands == [("branch", "--show-current")]


def test_cli_runner_uses_configured_git_executable(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, git_calls: list[GitCall]
) -> None:
    # After repo, whose client has found git already. The tests' git, spelled
    # as nothing else names it.
    program = os.path.join(os.path.dirname(real_git()), ".", os.path.basename(real_git()))
    monkeypatch.setenv(GIT_VARIABLE, program)

    GitClient(repo.path).run("status")

    assert [call.program for call in git_calls] == [program]


# The command line reports StackErrors as git-stack's own; any other error,
# such as git_client's GitError, would print a traceback. So each read
# through git_client is a StackError when git fails.


def test_resolving_an_object_outside_a_repository_is_a_stack_error(tmp_path: pathlib.Path) -> None:
    git = GitClient(tmp_path)
    with pytest.raises(StackError, match="not a git repository"), git.snapshot():
        git.tree("0" * 40)


def test_failing_to_read_the_refs_is_a_stack_error(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> None:
        command = ("for-each-ref",)
        raise GitError(command, 128, "fatal: refs broke")

    monkeypatch.setattr(git_client, "refs", failing)
    git = GitClient(repo.path)
    with pytest.raises(StackError, match="refs broke"), git.snapshot():
        git.local_branches()


def test_missing_git_answer_is_a_stack_error(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args: Any, **kwargs: Any) -> None:
        raise GitTimeoutError(("for-each-ref",), None, "")

    monkeypatch.setattr(git_client, "refs", timed_out)
    git = GitClient(repo.path)
    with pytest.raises(StackError, match="timed out"), git.snapshot():
        git.local_branches()


def test_operation_paths_are_cached_only_inside_a_snapshot(
    repo: Repo, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = repo.path / ".git"
    monkeypatch.setenv("GIT_DIR", str(git_dir))
    git = GitClient(repo.path)

    with git.snapshot():
        assert not git.operation_in_progress()
        (git_dir / "MERGE_HEAD").touch()
        assert git.operation_in_progress()
        assert len(git_calls) == 1

    assert git.operation_in_progress()
    assert len(git_calls) == 2


def test_a_broken_config_is_a_stack_error(repo: Repo) -> None:
    git = GitClient(repo.path)
    # Found before the config breaks, as git can't find it after.
    git.common_dir()
    (repo.path / ".git" / "config").write_text("[broken\n", encoding="utf-8")

    with pytest.raises(StackError, match="bad config"):
        git.branch_config()


def test_failing_to_read_patch_ids_is_a_stack_error(repo: Repo) -> None:
    with pytest.raises(StackError, match="nope"):
        GitClient(repo.path).commit_patch_ids(("log", "-p", "nope"))


def test_config_get_sees_changes_made_by_other_processes(repo: Repo) -> None:
    git = GitClient(repo.path)
    assert git.config_get("main", "stackParent") is None

    repo.git("config", "branch.main.stackParent", "trunk")

    assert git.config_get("main", "stackParent") == "trunk"


@pytest.mark.parametrize("explicit", [False, True])
def test_push_destinations_see_changes_in_an_included_config(repo: Repo, explicit: bool) -> None:
    included = repo.tmp / "remote-config"
    included.write_text("", encoding="utf-8")
    repo.git("config", "include.path", str(included))
    if explicit:
        repo.git("config", "--file", str(included), "remote.origin.pushurl", str(repo.origin))
    git = GitClient(repo.path)
    with git.snapshot():
        assert tuple(map(pathlib.Path, git.push_urls("origin"))) == (repo.origin,)
        if not explicit:
            repo.git("config", "--file", str(included), "remote.origin.pushurl", str(repo.origin))
        repo.git("config", "--file", str(included), "--add", "remote.origin.pushurl", "second-destination")
        assert tuple(map(pathlib.Path, git.push_urls("origin"))) == (repo.origin, pathlib.Path("second-destination"))


def test_config_get_handles_dots_in_branch_names_and_any_key_case(repo: Repo) -> None:
    repo.git("config", "branch.release.1.2.stackParent", "main")

    assert GitClient(repo.path).config_get("release.1.2", "STACKPARENT") == "main"


def test_snapshot_reads_refs_once(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    main = repo.git("rev-parse", "main")
    # As a command has, to know which upstreams to read.
    git.branch_config()
    git_calls.clear()

    with git.snapshot():
        assert git.rev("refs/heads/main") == main
        assert git.tip("main") == main
        assert git.rev("main@{upstream}") == main
        assert git.rev("refs/remotes/origin/main") == main
        assert git.local_branch_exists("main")
        assert git.local_branches() == {"main"}
        assert not git.upstream_gone("main")

    assert [call[0] for call in git_calls] == ["for-each-ref"]


def test_snapshot_asks_git_about_refs_it_doesnt_have(repo: Repo, git_calls: list[GitCall]) -> None:
    """On a case-insensitive filesystem, git can find a ref under a name that
    differs in case, which the snapshot can't."""
    repo.git("branch", "feature")
    git = GitClient(repo.path)
    outside = (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature"))

    with git.snapshot():
        assert not git.local_branch_exists("nope")
        assert git.rev("refs/heads/nope") is None
        assert (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature")) == outside


def test_snapshot_isnt_read_for_refs_it_cant_answer(repo: Repo, git_calls: list[GitCall]) -> None:
    """HEAD is native; Git parses the two revision expressions."""
    git = GitClient(repo.path)
    main = repo.git("rev-parse", "main")
    with git.snapshot():
        assert git.rev("HEAD") == main
        assert git.rev("main~0") == main
        assert git.rev("main~5") is None

    assert [call[0] for call in git_calls] == ["rev-parse", "rev-parse"]


def test_snapshot_can_resolve_a_branch_it_did_not_list(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        git.local_branches()
        repo.git("branch", "later")
        assert git.tip("later") == repo.git("rev-parse", "main")
        with pytest.raises(StackError, match="refs/heads/nope"):
            git.tip("nope")

    # The config, for the upstreams the snapshot lists, then the refs.
    assert [call[0] for call in git_calls] == ["config", "for-each-ref", "rev-parse"]


def test_snapshot_reads_refs_again_after_git_changes_them(repo: Repo) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.local_branch_exists("new")

        git.run("branch", "new")

        assert git.local_branch_exists("new")
        assert git.rev("new@{upstream}") is None

        git.run("branch", "--set-upstream-to=origin/main", "new")

        assert git.rev("new@{upstream}") == git.tip("main")


def test_snapshot_outlives_stack_metadata_writes_but_not_upstream_changes(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    repo.git("branch", "--no-track", "new")
    git.branch_config()
    git_calls.clear()
    with git.snapshot():
        assert git.rev("new@{upstream}") is None
        git.config_set("new", "stackParent", "main")
        assert git.rev("new@{upstream}") is None
        assert [call[0] for call in git_calls] == ["for-each-ref", "config"]

        git.config_set("new", "remote", "origin")
        git.config_set("new", "merge", "refs/heads/main")

        assert git.rev("new@{upstream}") == git.tip("main")


def test_snapshot_ends_with_the_outermost_block(repo: Repo) -> None:
    """Changes made by other processes are seen once the snapshot ends."""
    main = repo.git("rev-parse", "main")
    repo.commit("a", "more\n", "more")
    moved = repo.git("rev-parse", "main")
    repo.git("update-ref", "refs/heads/main", main)
    git = GitClient(repo.path)
    with git.snapshot():
        with git.snapshot():
            assert git.tip("main") == main
        repo.git("update-ref", "refs/heads/main", moved)
        assert git.tip("main") == main

    assert git.tip("main") == moved


def test_snapshot_leaves_revisions_to_git(repo: Repo) -> None:
    repo.commit("a", "more\n", "more")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.rev("refs/heads/main~1") == repo.git("rev-parse", "main~1")
        assert git.rev("main") == repo.git("rev-parse", "main")
        assert git.rev("HEAD^{tree}") is None


def test_snapshot_sees_a_deleted_upstream(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "feature")
    repo.git("push", "--quiet", "-u", "origin", "feature")
    repo.git("push", "--quiet", "origin", ":feature")
    repo.git("fetch", "--quiet", "--prune")
    git = GitClient(repo.path)

    with git.snapshot():
        assert git.upstream_gone("feature")
        assert not git.upstream_gone("main")
        assert git.rev("feature@{upstream}") is None


@pytest.mark.parametrize(
    ("args", "changes"),
    [
        (("rev-parse", "HEAD"), False),
        (("--literal-pathspecs", "log", "main"), False),
        (("branch", "--show-current"), False),
        (("branch", "new"), True),
        # git switch creates a branch from a remote one of its name.
        (("switch", "--quiet", "main"), True),
        (("switch", "--quiet", "--create", "new"), True),
        (("worktree", "list", "--porcelain"), False),
        (("worktree", "add", "x"), True),
        (("remote",), False),
        (("remote", "set-head", "origin", "--auto"), True),
        (("remote", "get-url", "origin"), False),
        (("ls-tree", "HEAD"), False),
        (("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"), False),
        (("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"), True),
        (("-c", "core.quotepath=off", "log"), False),
        (("-c", "log.x=y", "push", "origin"), True),
        (("config", "--get", "remote.origin.url"), False),
        (("config", "remote.origin.url", "x"), True),
        (("rebase", "main"), True),
        (("push", "origin", "main"), True),
    ],
)
def test_changes_refs(args: tuple[str, ...], changes: bool) -> None:
    assert changes_refs(args) is changes


def test_a_repository_without_a_config_file_works(repo: Repo) -> None:
    """git works without one, as if it were empty."""
    repo.make_stack()
    (repo.path / ".git" / "config").unlink()

    result = repo.stack("info", "--all", "--json")

    assert json.loads(result.stdout.splitlines()[-1]) == []
    git = GitClient(repo.path)
    assert git.branch_config() == {}
    git.config_set("main", "stackParent", "trunk")
    assert git.config_get("main", "stackParent") == "trunk"


def test_snapshot_reads_only_the_remote_branches_upstreams_are(repo: Repo, git_calls: list[GitCall]) -> None:
    """A clone can have tens of thousands of remote branches; the others are
    asked of git as they're needed."""
    repo.git("push", "--quiet", "origin", "main:other")
    repo.git("fetch", "--quiet")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.rev("refs/remotes/origin/other") == git.tip("main")
        assert git.rev("main@{upstream}") == git.tip("main")

    listing = next(call for call in git_calls if call[0] == "for-each-ref")
    assert listing[-3:] == ["refs/heads/", "refs/replace/", "refs/remotes/origin/main"]


def test_snapshot_reads_every_remote_branch_when_upstreams_are_too_many_to_name(
    repo: Repo, git_calls: list[GitCall]
) -> None:
    """Naming them all could make a command line longer than Windows allows."""
    main = repo.git("rev-parse", "main")
    # 600 upstreams, whose refnames come to over 35000 characters.
    names = [f"{'long-branch-name-' * 2}{i:04}" for i in range(600)]
    subprocess.run(
        [real_git(), "-C", str(repo.path), "update-ref", "--stdin"],
        # As bytes, so Windows doesn't end lines with \r\n.
        input="".join(f"create refs/heads/{n} {main}\ncreate refs/remotes/origin/{n} {main}\n" for n in names).encode(),
        check=True,
    )
    with (repo.path / ".git" / "config").open("a", encoding="utf-8", newline="\n") as f:
        f.writelines(f'[branch "{n}"]\n\tremote = origin\n\tmerge = refs/heads/{n}\n' for n in names)
    git = GitClient(repo.path)

    with git.snapshot():
        assert all(git.rev(f"{n}@{{upstream}}") == main for n in names)
        assert not git.upstream_gone(names[0])

    listing = next(call for call in git_calls if call[0] == "for-each-ref")
    assert listing[-3:] == ["refs/heads/", "refs/replace/", "refs/remotes/"]


def test_snapshot_can_validate_a_branch_upstream(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.has_branch_upstream("main", "origin")
    assert [call[0] for call in git_calls] == ["config", "for-each-ref"]


def test_snapshot_does_not_validate_an_upstream_after_the_fetch_refspec_changes(repo: Repo) -> None:
    git = GitClient(repo.path)
    repo.git("update-ref", "refs/remotes/mirror/main", "refs/heads/main")
    with git.snapshot():
        assert git.has_branch_upstream("main", "origin")
        repo.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/mirror/*")
        assert not git.has_branch_upstream("main", "origin")


@pytest.mark.parametrize("symbolic", [False, True])
def test_snapshot_rejects_missing_or_symbolic_upstream_refs(repo: Repo, symbolic: bool) -> None:
    repo.git("config", "branch.main.merge", "refs/heads/other")
    if symbolic:
        repo.git("symbolic-ref", "refs/remotes/origin/other", "refs/remotes/origin/main")
    git = GitClient(repo.path)

    with git.snapshot():
        assert not git.has_branch_upstream("main", "origin")


def test_snapshot_reads_an_upstream_with_an_unusual_refspec(repo: Repo) -> None:
    """Its remote-tracking branch isn't where the snapshot looks, so it isn't
    taken as deleted."""
    repo.git("remote", "add", "mirror", str(repo.origin))
    repo.git("config", "remote.mirror.fetch", "+refs/heads/*:refs/remotes/elsewhere/*")
    repo.git("fetch", "--quiet", "mirror")
    repo.git("branch", "--quiet", "--track", "mirrored", "refs/remotes/elsewhere/main")
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.upstream_gone("mirrored")
        assert git.rev("mirrored@{upstream}") == git.tip("main")
        assert not git.has_branch_upstream("mirrored", "mirror")


def test_snapshot_reads_worktrees_and_the_remotes_default_branch_once(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        for _ in range(3):
            assert git.other_worktree_of("main") is None
            assert git.remote_default_branch("origin") == "main"

    commands = [call[0] for call in git_calls]
    assert commands.count("worktree") == 1
    assert commands.count("ls-remote") == 1
    assert all(call[:2] != ["rev-parse", "--show-toplevel"] for call in git_calls)


@pytest.mark.parametrize(
    ("args", "changes"),
    [
        (("fetch", "--prune", "origin"), False),
        (("push", "--atomic", "origin", "main"), False),
        (("config", "branch.main.stackParent", "x"), False),
        (("update-ref", "refs/heads/main", "abc"), False),
        (("update-ref", "--no-deref", "HEAD", "abc"), True),
        (("switch", "--quiet", "main"), True),
        (("rebase", "main"), True),
        (("branch", "-m", "old", "new"), True),
        (("worktree", "add", "x"), True),
    ],
)
def test_changes_worktrees(args: tuple[str, ...], changes: bool) -> None:
    assert changes_worktrees(args) is changes


def test_snapshot_keeps_the_worktrees_while_no_head_moves(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.worktree_of("main") is not None
        git.run("fetch", "--quiet", "origin")
        git.run("update-ref", "refs/heads/other", git.tip("main"))
        git.config_set("other", "remote", "origin")
        assert git.worktree_of("main") is not None
        assert git.worktree_of("other") is None

    assert [call[0] for call in git_calls].count("worktree") == 1


def test_switching_forgets_the_worktrees(repo: Repo) -> None:
    repo.git("branch", "other")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.worktree_of("main") is not None
        git.switch("other")
        assert git.worktree_of("main") is None
        assert git.worktree_of("other") is not None


def test_snapshot_reads_the_current_branch_with_the_refs(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    # As a command has, to know which upstreams to read.
    git.branch_config()
    git_calls.clear()
    with git.snapshot():
        assert [git.current_branch() for _ in range(3)] == ["main"] * 3

    assert [call[0] for call in git_calls] == ["for-each-ref"]


def test_snapshot_sees_the_current_branch_change(repo: Repo) -> None:
    repo.git("branch", "other")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.current_branch() == "main"
        git.switch("other")
        assert git.current_branch() == "other"
        git.run("switch", "--quiet", "--detach")
        assert git.current_branch() is None


def test_snapshot_reads_the_current_branch_of_its_own_worktree(repo: Repo) -> None:
    repo.git("worktree", "add", "--quiet", "-b", "elsewhere", str(repo.tmp / "elsewhere"))

    for directory, branch in ((repo.path, "main"), (repo.tmp / "elsewhere", "elsewhere")):
        git = GitClient(directory)
        with git.snapshot():
            assert git.current_branch() == branch


def test_snapshot_reads_an_unborn_current_branch(repo: Repo) -> None:
    """It has no commit yet, so it isn't a ref."""
    repo.git("switch", "--quiet", "--orphan", "new")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.current_branch() == "new"


def separate_patch_ids(repo: Repo, *command: str) -> list[tuple[str, str]]:
    """`git patch-id --stable` of what `git <command>` prints, run on its own."""
    output = repo.git(command[0], "--no-color", *command[1:])
    result = subprocess.run(
        [real_git(), "-C", str(repo.path), "patch-id", "--stable"],
        input=output.encode() + b"\n",
        capture_output=True,
        check=True,
    )
    return [(patch_id, commit) for patch_id, commit in (line.split() for line in result.stdout.decode().splitlines())]


def test_commit_patch_ids_reads_each_commands_patches_with_one_patch_id(repo: Repo, git_calls: list[GitCall]) -> None:
    base = repo.git("rev-parse", "HEAD")
    repo.commit("a", "one\n", "one")
    repo.git("commit", "--quiet", "--allow-empty", "-m", "empty")
    (repo.path / "binary").write_bytes(b"\0\1\2")
    repo.git("add", "binary")
    repo.git("commit", "--quiet", "-m", "binary")
    repo.commit("b", "two\n", "two")
    commands = [
        ("log", "-p", f"{base}..HEAD"),
        ("log", "-p", "-R", f"{base}..HEAD"),  # the same commits again
        ("log", "-p", "HEAD..HEAD"),  # nothing
        ("diff", base, "HEAD"),
        ("diff", "-R", base, "HEAD"),
        ("diff", "HEAD", "HEAD"),  # nothing
    ]
    git = GitClient(repo.path)

    result = git.commit_patch_ids(*commands)

    assert result == [separate_patch_ids(repo, *command) for command in commands]
    assert [len(patches) for patches in result] == [3, 3, 0, 1, 1, 0]
    assert [call[0] for call in git_calls].count("patch-id") == 1


def sha256_repo(tmp_path: pathlib.Path, repo: Repo) -> Repo:
    """A repository with SHA-256 object IDs, and two commits to a."""
    sha256 = Repo(path=tmp_path / "sha256", origin=repo.origin, forge=repo.forge, tmp=tmp_path)
    repo.git("init", "--quiet", "--object-format=sha256", str(sha256.path))
    sha256.write("a", "one\n")
    sha256.git("add", "a")
    sha256.git("commit", "--quiet", "-m", "one")
    sha256.commit("a", "two\n", "two")
    return sha256


def test_commit_patch_ids_ignore_log_show_root_config(repo: Repo) -> None:
    root = repo.git("rev-list", "--max-parents=0", "HEAD")
    command = ("show", "-p", root)
    expected = separate_patch_ids(repo, *command)
    assert len(expected) == 1
    repo.git("config", "log.showRoot", "false")

    assert GitClient(repo.path).commit_patch_ids(command) == [expected]


def test_commit_patch_ids_preserve_unicode_at_end_of_patch(repo: Repo) -> None:
    repo.write("a", "new\u00a0\n")
    command = [real_git(), "-C", str(repo.path)]
    patch = subprocess.check_output([*command, "diff", "HEAD"])
    expected = subprocess.run([*command, "patch-id", "--stable"], input=patch, capture_output=True, check=True)

    [[(patch_id, commit)]] = GitClient(repo.path).commit_patch_ids(("diff", "HEAD"))

    assert f"{patch_id} {commit}\n".encode() == expected.stdout


def test_commit_patch_ids_in_a_sha256_repository(tmp_path: pathlib.Path, repo: Repo) -> None:
    sha256 = sha256_repo(tmp_path, repo)
    commands = [("log", "-p", "HEAD~1..HEAD"), ("diff", "HEAD~1", "HEAD")]

    assert GitClient(sha256.path).commit_patch_ids(*commands) == [
        separate_patch_ids(sha256, *command) for command in commands
    ]


def test_snapshot_asks_about_the_same_commits_once_but_sees_a_branch_move(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.git("branch", "other")
    git = GitClient(repo.path)
    with git.snapshot():
        for _ in range(2):
            assert git.is_ancestor("refs/heads/other", "refs/heads/main")
            assert git.contained_in("refs/heads/other", "refs/heads/main")
        repo.write("a", "main moves on\n")
        git.run("commit", "--quiet", "-am", "main moves on")

        assert not git.is_ancestor("refs/heads/main", "refs/heads/other")
        assert not git.contained_in("refs/heads/main", "refs/heads/other")

    commands = [call[0] for call in git_calls]
    assert (commands.count("merge-base"), commands.count("merge-tree")) == (0, 2)


def test_nothing_is_contained_in_a_branch_that_doesnt_exist(repo: Repo) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.contained_in("refs/heads/main", "refs/heads/nope")


def test_snapshot_reads_a_reflog_once_until_its_branch_moves(repo: Repo, git_calls: list[GitCall]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        first = git.reflog_tips("main")
        assert git.reflog_tips("main") == first
        repo.write("a", "moved\n")
        git.run("commit", "--quiet", "-am", "moved")

        assert git.reflog_tips("main") == [git.tip("main"), *first]

    assert [call[1] for call in git_calls if call[0] == "log"] == ["--walk-reflogs"] * 2


def test_snapshot_doesnt_keep_a_reflog_read_while_it_was_forgotten(
    repo: Repo, git_calls: list[GitCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    git = GitClient(repo.path)
    run = git.run

    def forgetting_meanwhile(*args: str, **kwargs: Any) -> str:
        """As if another thread moved a branch while the reflog was read."""
        git._forget_refs()  # pyright: ignore[reportPrivateUsage]
        return run(*args, **kwargs)

    with git.snapshot():
        monkeypatch.setattr(git, "run", forgetting_meanwhile)
        git.reflog_tips("main")
        monkeypatch.setattr(git, "run", run)
        git.reflog_tips("main")

    assert [call[1] for call in git_calls if call[0] == "log"] == ["--walk-reflogs"] * 2


def test_snapshot_doesnt_keep_a_rebase_read_while_it_was_forgotten(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    git = GitClient(repo.path)
    read = git._read_rebasing  # pyright: ignore[reportPrivateUsage]
    reads: list[pathlib.Path] = []

    def forgetting_meanwhile(worktree: pathlib.Path) -> str | None:
        """As if another thread moved a branch while it was read."""
        reads.append(worktree)
        git._forget_refs()  # pyright: ignore[reportPrivateUsage]
        return read(worktree)

    monkeypatch.setattr(git, "_read_rebasing", forgetting_meanwhile)
    with git.snapshot():
        git.rebasing_in(repo.path)
        git.rebasing_in(repo.path)

    assert reads == [repo.path] * 2


def test_has_conflicts_sees_one_at_the_top_from_a_subdirectory_with_diff_relative(repo: Repo) -> None:
    """diff.relative would have `git diff` list only the subdirectory's paths."""
    repo.git("switch", "--quiet", "-c", "other")
    repo.commit("a", "other\n", "other")
    repo.git("switch", "--quiet", "main")
    repo.commit("a", "main\n", "main")
    subprocess.run([real_git(), "-C", str(repo.path), "merge", "--quiet", "other"], capture_output=True, check=False)
    (repo.path / "sub").mkdir()
    repo.git("config", "diff.relative", "true")

    assert GitClient(repo.path / "sub").has_conflicts()


@pytest.mark.parametrize("relative", [False, True])
def test_is_rebased_copy_replays_every_file_from_a_subdirectory(repo: Repo, relative: bool) -> None:
    """Run from a subdirectory, `git apply` would leave out the changes
    outside it, and with diff.relative, so would `git show`; then the replay
    wouldn't match."""
    base = repo.git("rev-parse", "HEAD")
    (repo.path / "sub").mkdir()
    repo.write("sub/x", "x\n")
    repo.write("a", "changed\n")
    repo.git("add", ".")
    repo.git("commit", "--quiet", "-m", "change")
    local = repo.git("rev-parse", "HEAD")
    repo.git("switch", "--quiet", "--detach", base)
    repo.commit("b", "moved on\n", "moved on")
    new_base = repo.git("rev-parse", "HEAD")
    repo.git("cherry-pick", "--quiet", local)
    rebased = repo.git("rev-parse", "HEAD")
    repo.git("config", "diff.relative", str(relative).lower())

    assert GitClient(repo.path / "sub").is_rebased_copy(base, local, new_base, rebased)


def test_changes_something_sees_changes_at_the_top_from_a_subdirectory_with_diff_relative(repo: Repo) -> None:
    """diff.relative would have `git diff --quiet` look only at the
    subdirectory's files."""
    repo.client.branch("pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    (repo.path / "sub").mkdir()
    repo.git("config", "diff.relative", "true")

    assert GitAdapter(StackCore(GitClient(repo.path / "sub"))).changes_something("pr1")


def test_commit_patch_ids_reads_every_file_from_a_subdirectory_with_diff_relative(repo: Repo) -> None:
    """diff.relative would have `git log -p` and `git diff` leave out the
    files outside the subdirectory, so the patches wouldn't be the commits'."""
    base = repo.git("rev-parse", "HEAD")
    repo.commit("a", "changed\n", "change a")
    (repo.path / "sub").mkdir()
    commands = [("log", "-p", f"{base}..HEAD"), ("diff", base, "HEAD")]
    from_top = GitClient(repo.path).commit_patch_ids(*commands)
    repo.git("config", "diff.relative", "true")

    assert [len(patches) for patches in from_top] == [1, 1]
    assert GitClient(repo.path / "sub").commit_patch_ids(*commands) == from_top


@pytest.mark.parametrize("relative", [False, True])
def test_changed_files_name_the_files_patch_ids_compare_from_a_subdirectory(repo: Repo, relative: bool) -> None:
    """changed_files lists files from the top, which commit_patch_ids takes
    as paths from the top, whatever directory git-stack runs in (with
    diff.relative, `git diff` would list them from it, leaving out the
    others), and literally, even with characters a pathspec reads as a
    pattern."""
    base = repo.git("rev-parse", "HEAD")
    (repo.path / "sub").mkdir()
    repo.write("sub/x", "x\n")
    repo.write("[ab]", "brackets\n")
    repo.write("a", "changed\n")
    repo.git("add", ".")
    repo.git("commit", "--quiet", "-m", "three files")
    repo.commit("b", "changed\n", "change b")
    top = GitClient(repo.path)
    [from_top] = top.commit_patch_ids(("log", "-p", f"{base}..HEAD"))
    # Only the commit that has it, and only its changes there.
    [brackets] = top.commit_patch_ids(("log", "-p", f"{base}..HEAD", "--", "[ab]"))
    repo.git("config", "diff.relative", str(relative).lower())
    git = GitClient(repo.path / "sub")

    files = GitAdapter(StackCore(git)).changed_files(base, "HEAD")

    assert files == ["[ab]", "a", "b", "sub/x"]
    assert git.commit_patch_ids(("log", "-p", f"{base}..HEAD", "--", *files)) == [from_top]
    assert len(brackets) == 1
    assert git.commit_patch_ids(("log", "-p", f"{base}..HEAD", "--", "[ab]")) == [brackets]


def test_commit_patch_ids_marks_paths_even_with_git_literal_pathspecs(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIT_LITERAL_PATHSPECS, which a user may export, would turn off the
    :(top,literal) its paths are marked with, making them names of no file."""
    repo.commit("a", "changed\n", "change a")
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")

    assert [len(p) for p in GitClient(repo.path).commit_patch_ids(("log", "-p", "HEAD~1..HEAD", "--", "a"))] == [1]


def test_git_runs_in_the_users_locale_with_its_messages_in_english(
    repo: Repo, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every command keeps the user's locale, which the hooks, credential
    helpers and ssh git runs inherit (in C, Ruby's default encoding is
    US-ASCII, which can't read UTF-8 files). git's own messages, which
    git-stack parses (e.g. a config lock error), are in
    English through GIT_TEXTDOMAINDIR instead; passthrough's are the user's."""
    install_command(tmp_path / "bin", "fake-git", "tests.fake_git")
    log = tmp_path / "git-log.jsonl"
    monkeypatch.setenv("FAKE_GIT_LOG", str(log))
    monkeypatch.setenv("REAL_GIT", real_git())
    monkeypatch.setenv(
        GIT_VARIABLE, str(tmp_path / "bin" / ("fake-git.exe" if sys.platform == "win32" else "fake-git"))
    )
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    git = GitClient(repo.path)

    git.config_set("main", "stackParent", "trunk")
    with git.snapshot():
        assert git.tree(git.tip("main")) is not None
    git.passthrough("status")

    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    # By the command and its first argument, after `-C <directory>`.
    by_command = {" ".join(call["args"][2:4]): call for call in calls}
    assert {call["LC_ALL"] for call in calls} == {"C.UTF-8"}
    assert by_command["config branch.main.stackParent"]["GIT_TEXTDOMAINDIR"] is not None
    assert by_command["config --list"]["GIT_TEXTDOMAINDIR"] is not None
    assert by_command["status"]["GIT_TEXTDOMAINDIR"] is None


def test_common_dir_at_the_top_of_a_worktree_starts_no_git(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.git("worktree", "add", "--quiet", "-b", "elsewhere", str(repo.tmp / "elsewhere"))

    assert same_file(GitClient(repo.tmp / "elsewhere").common_dir(), repo.path / ".git")
    assert git_calls == []


def test_common_dir_from_a_subdirectory(repo: Repo) -> None:
    (repo.path / "sub").mkdir()

    assert same_file(GitClient(repo.path / "sub").common_dir(), repo.path / ".git")


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_common_dir_follows_git_dir(repo: Repo, monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    """Where git would look, rather than the .git at the directory: the
    repository GIT_DIR, or GIT_COMMON_DIR, names."""
    (repo.tmp / "other").mkdir()
    repo.git("init", "--quiet", str(repo.tmp / "other"))
    monkeypatch.setenv(variable, str(repo.tmp / "other" / ".git"))

    assert same_file(GitClient(repo.path).common_dir(), repo.tmp / "other" / ".git")


@pytest.mark.parametrize(
    "name", ["rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer", "MERGE_HEAD"]
)
def test_operation_in_progress_sees_each_operations_state(repo: Repo, name: str) -> None:
    git = GitClient(repo.path)
    assert not git.operation_in_progress()
    (repo.path / ".git" / name).touch()

    assert git.operation_in_progress()


def test_config_value_is_what_git_config_get_gives(repo: Repo) -> None:
    """The last value, in any case the key is written in; "" for a key with
    no value, which `git config --get` prints as an empty line; None if unset."""
    with (repo.path / ".git" / "config").open("a", encoding="utf-8") as f:
        f.write("[stack]\n\tAdapter = git\n\tadapter = github\n\tflag\n")
    git = GitClient(repo.path)

    assert git.config_value("stack.adapter") == repo.git("config", "--get", "stack.adapter") == "github"
    assert git.config_value("stack.flag") == repo.git("config", "--get", "stack.flag") == ""
    assert git.config_value("stack.unset") is None


def test_local_objects_and_ancestry_need_no_git_process(repo: Repo, git_calls: list[GitCall]) -> None:
    before = repo.git("rev-parse", "HEAD")
    repo.commit("a", "next\n", "next")
    after = repo.git("rev-parse", "HEAD")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.rev("HEAD") == after
        assert git.tree(after) == tree
        assert git.is_ancestor(before, after)
        assert not git.is_ancestor(after, before)
    assert git_calls == []


def test_replacement_commits_leave_ancestry_to_git(repo: Repo, git_calls: list[GitCall]) -> None:
    before = repo.git("rev-parse", "HEAD")
    repo.commit("a", "next\n", "next")
    after = repo.git("rev-parse", "HEAD")
    repo.git("replace", "--graft", after)
    git = GitClient(repo.path)

    with git.snapshot():
        assert not git.is_ancestor(before, after)
    assert any(call[:2] == ["merge-base", "--is-ancestor"] for call in git_calls)


def test_current_branch_resolves_a_symbolic_branch_alias(repo: Repo, git_calls: list[GitCall]) -> None:
    repo.git("symbolic-ref", "refs/heads/alias", "refs/heads/main")
    repo.git("symbolic-ref", "HEAD", "refs/heads/alias")

    assert GitClient(repo.path).current_branch() == "main"
    assert git_calls == [["branch", "--show-current"]]


def test_an_out_of_range_reflog_revision_without_a_snapshot_is_missing(repo: Repo) -> None:
    assert GitClient(repo.path).rev("HEAD@{99999}") is None


def test_missing_object_can_be_fetched_from_a_promisor_in_git_config(
    repo: Repo, git_config: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # libgit2 does not read Git's overridden global configuration.
    repo.commit_on_forge("main", "a", "new content\n", "remote only")
    commit = repo.git("rev-parse", "HEAD", cwd=repo.forge)
    override = git_config.with_name("git-only")
    override.write_text(
        git_config.read_text(encoding="utf-8") + '[remote "origin"]\n    promisor = true\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(override))

    assert GitClient(repo.path).rev(commit) == commit


def test_operation_in_progress_cli_fallback_handles_unicode_path(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    unusual = repo.path.with_name("local\u2028repo")
    repo.path.rename(unusual)
    git_dir = unusual / ".git"
    (git_dir / "MERGE_HEAD").touch()
    monkeypatch.setenv("GIT_DIR", str(git_dir))

    assert GitClient(unusual).operation_in_progress()


def test_toplevel_respects_a_linked_worktree_config(repo: Repo) -> None:
    linked = repo.tmp / "linked"
    directory = repo.tmp / "configured"
    directory.mkdir()
    repo.git("worktree", "add", "--quiet", "-b", "linked", str(linked))
    repo.git("config", "extensions.worktreeConfig", "true")
    repo.git("config", "--worktree", "core.worktree", str(directory), cwd=linked)

    assert same_file(GitClient(linked).toplevel(), directory)


def test_toplevel_requires_a_working_tree(repo: Repo) -> None:
    with pytest.raises(StackError, match="must be run in a work tree"):
        GitClient(repo.path / ".git").toplevel()
