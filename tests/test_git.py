"""GitClient's reading of branch config and refs, which it caches."""

import json
import os
import pathlib
import subprocess
from typing import Any

import pytest

from git_stack.git import GitClient, changes_refs
from tests.conftest import GIT_REPOSITORY_VARIABLES
from tests.helpers import Repo


def test_config_get_sees_changes_made_by_other_processes(repo: Repo) -> None:
    git = GitClient(repo.path)
    assert git.config_get("main", "stackParent") is None

    repo.git("config", "branch.main.stackParent", "trunk")

    assert git.config_get("main", "stackParent") == "trunk"


def test_config_get_handles_dots_in_branch_names_and_any_key_case(repo: Repo) -> None:
    repo.git("config", "branch.release.1.2.stackParent", "main")

    assert GitClient(repo.path).config_get("release.1.2", "STACKPARENT") == "main"


def test_tests_ignore_a_hooks_repository(repo: Repo) -> None:
    """The pre-push hook runs the tests with GIT_DIR set to the repository
    being pushed, which their git commands mustn't use."""
    assert not set(GIT_REPOSITORY_VARIABLES) & os.environ.keys()
    # As paths, since git writes Windows paths with forward slashes.
    assert pathlib.Path(repo.git("rev-parse", "--absolute-git-dir")).resolve() == (repo.path / ".git").resolve()


def test_snapshot_reads_refs_once(repo: Repo, git_calls: list[list[str]]) -> None:
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


def test_snapshot_asks_git_about_refs_it_doesnt_have(repo: Repo, git_calls: list[list[str]]) -> None:
    """On a case-insensitive filesystem, git can find a ref under a name that
    differs in case, which the snapshot can't."""
    repo.git("branch", "feature")
    git = GitClient(repo.path)
    outside = (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature"))

    with git.snapshot():
        assert not git.local_branch_exists("nope")
        assert git.rev("refs/heads/nope") is None
        assert (git.local_branch_exists("Feature"), git.rev("refs/heads/Feature")) == outside


def test_snapshot_isnt_read_for_refs_it_cant_answer(repo: Repo, git_calls: list[list[str]]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        git.rev("HEAD")
        git.rev("main~0")

    assert [call[0] for call in git_calls] == ["rev-parse", "rev-parse"]


def test_snapshot_reads_refs_again_after_git_changes_them(repo: Repo) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.local_branch_exists("new")

        git.run("branch", "new")

        assert git.local_branch_exists("new")
        assert git.rev("new@{upstream}") is None

        git.run("branch", "--set-upstream-to=origin/main", "new")

        assert git.rev("new@{upstream}") == git.tip("main")


def test_snapshot_outlives_stack_metadata_writes_but_not_upstream_changes(
    repo: Repo, git_calls: list[list[str]]
) -> None:
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


def test_snapshot_reads_only_the_remote_branches_upstreams_are(repo: Repo, git_calls: list[list[str]]) -> None:
    """A clone can have tens of thousands of remote branches; the others are
    asked of git as they're needed."""
    repo.git("push", "--quiet", "origin", "main:other")
    repo.git("fetch", "--quiet")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.rev("refs/remotes/origin/other") == git.tip("main")
        assert git.rev("main@{upstream}") == git.tip("main")

    listing = next(call for call in git_calls if call[0] == "for-each-ref")
    assert listing[2:] == ["refs/heads/", "refs/remotes/origin/main"]


def test_snapshot_reads_every_remote_branch_when_upstreams_are_too_many_to_name(
    repo: Repo, git_calls: list[list[str]]
) -> None:
    """Naming them all could make a command line longer than Windows allows."""
    main = repo.git("rev-parse", "main")
    # 600 upstreams, whose refnames come to over 35000 characters.
    names = [f"{'long-branch-name-' * 2}{i:04}" for i in range(600)]
    subprocess.run(
        ["git", "update-ref", "--stdin"],
        # As bytes, so Windows doesn't end lines with \r\n.
        input="".join(f"create refs/heads/{n} {main}\ncreate refs/remotes/origin/{n} {main}\n" for n in names).encode(),
        cwd=repo.path,
        check=True,
    )
    with (repo.path / ".git" / "config").open("a", encoding="utf-8", newline="\n") as f:
        f.writelines(f'[branch "{n}"]\n\tremote = origin\n\tmerge = refs/heads/{n}\n' for n in names)
    git = GitClient(repo.path)

    with git.snapshot():
        assert all(git.rev(f"{n}@{{upstream}}") == main for n in names)
        assert not git.upstream_gone(names[0])

    listing = next(call for call in git_calls if call[0] == "for-each-ref")
    assert listing[2:] == ["refs/heads/", "refs/remotes/"]


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


def test_snapshot_reads_worktrees_and_the_remotes_default_branch_once(repo: Repo, git_calls: list[list[str]]) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        for _ in range(3):
            assert git.other_worktree_of("main") is None
            assert git.remote_default_branch("origin") == "main"

    commands = [call[0] for call in git_calls]
    assert commands.count("worktree") == 1
    assert commands.count("ls-remote") == 1
    assert [call[:2] for call in git_calls].count(["rev-parse", "--show-toplevel"]) == 1


def test_switching_forgets_the_worktrees(repo: Repo) -> None:
    repo.git("branch", "other")
    git = GitClient(repo.path)
    with git.snapshot():
        assert git.worktree_of("main") is not None
        git.switch("other")
        assert git.worktree_of("main") is None
        assert git.worktree_of("other") is not None


def test_snapshot_reads_the_current_branch_with_the_refs(repo: Repo, git_calls: list[list[str]]) -> None:
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
        ["git", "patch-id", "--stable"], input=output.encode() + b"\n", cwd=repo.path, capture_output=True, check=True
    )
    return [(patch_id, commit) for patch_id, commit in (line.split() for line in result.stdout.decode().splitlines())]


def test_commit_patch_ids_reads_each_commands_patches_with_one_patch_id(repo: Repo, git_calls: list[list[str]]) -> None:
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
    git.object_id_length()
    git_calls.clear()

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


@pytest.mark.parametrize("sha256", [False, True])
def test_object_id_length(tmp_path: pathlib.Path, repo: Repo, git_calls: list[list[str]], sha256: bool) -> None:
    path = sha256_repo(tmp_path, repo).path if sha256 else repo.path
    assert GitClient(path).object_id_length() == (64 if sha256 else 40)
    git = GitClient(path)
    with git.snapshot():
        git.rev("refs/heads/main")
        git_calls.clear()
        assert git.object_id_length() == (64 if sha256 else 40)
    # Taken from the snapshot, which has read the refs.
    assert git_calls == []


def test_commit_patch_ids_in_a_sha256_repository(tmp_path: pathlib.Path, repo: Repo) -> None:
    sha256 = sha256_repo(tmp_path, repo)
    commands = [("log", "-p", "HEAD~1..HEAD"), ("diff", "HEAD~1", "HEAD")]

    assert GitClient(sha256.path).commit_patch_ids(*commands) == [
        separate_patch_ids(sha256, *command) for command in commands
    ]


def test_snapshot_looks_up_objects_with_one_cat_file(
    repo: Repo, git_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit("a", "one\n", "one")
    repo.commit("a", "two\n", "two")
    commits = repo.git("rev-list", "HEAD").split()
    git = GitClient(repo.path)
    started: list[subprocess.Popen[Any]] = []
    popen = subprocess.Popen

    def recording(args: list[str], *rest: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = popen(args, *rest, **kwargs)
        if "cat-file" in args:
            started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", recording)

    with git.snapshot():
        for _ in range(2):
            for commit in commits:
                assert git.rev(commit) == commit
                assert git.tree(commit) == repo.git("rev-parse", f"{commit}^{{tree}}")
        assert git.rev("0" * 40) is None

    assert [call[0] for call in git_calls] == ["cat-file"]
    # It ends with the snapshot.
    assert [process.poll() for process in started] == [0]


def test_snapshot_asks_about_the_same_commits_once_but_sees_a_branch_move(
    repo: Repo, git_calls: list[list[str]]
) -> None:
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
    assert (commands.count("merge-base"), commands.count("merge-tree")) == (2, 2)


def test_nothing_is_contained_in_a_branch_that_doesnt_exist(repo: Repo) -> None:
    git = GitClient(repo.path)
    with git.snapshot():
        assert not git.contained_in("refs/heads/main", "refs/heads/nope")
