import os
import pathlib
import subprocess
import tempfile
import threading
from collections import Counter
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import TextIO

import git_client
from git_client import CatFile, Git, GitError, config_key, git_common_dir, git_paths

from git_stack.errors import StackError

DEFAULT_REMOTE = "origin"
# How long to keep retrying a git config write that finds the config file locked
# by another process, which usually only holds it for a moment.
CONFIG_LOCK_TIMEOUT = 5.0
# The git commands git-stack runs that never move a ref or HEAD, or change a
# branch's upstream, so the ref snapshot (see GitClient.snapshot) outlives them.
# See changes_refs for a few more.
READ_ONLY_COMMANDS = frozenset(
    {
        "cat-file",
        "diff",
        "for-each-ref",
        "log",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "merge-tree",
        "patch-id",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
        "var",
    }
)
# Options to git itself that take a value as the next argument.
GIT_OPTIONS_WITH_VALUES = frozenset({"-c", "-C", "--git-dir", "--work-tree", "--namespace"})
# `git switch --quiet`, for switching to a branch, which has to be named by
# its bare name. git switch only takes branches, so a tag with the same name
# can't be what's meant, but git still warns that the name is ambiguous, which
# this turns off. Everywhere else, branches are named as refs/heads/<name>.
SWITCH = ("-c", "core.warnAmbiguousRefs=false", "switch", "--quiet")
# How long the upstreams' refnames can be, together, for the ref snapshot to
# name them to git (see GitClient._snapshot_refs).
MAX_UPSTREAM_PATTERNS_LENGTH = 16384
# The environment variable that names the git to run, if not the one on PATH.
GIT_VARIABLE = "GIT_STACK_GIT"
# git's variables that can make the git directory another than the one at the
# top of the worktree, which git_common_dir doesn't read.
GIT_DIR_VARIABLES = frozenset({"GIT_DIR", "GIT_COMMON_DIR"})
# Characters a branch name can't have, but a revision (e.g. main~1) can.
REVISION_SYNTAX = frozenset("~^:?*[\\ ")


def split_command(args: tuple[str, ...]) -> tuple[str, list[str]]:
    """The command `git <args>` runs, and its arguments: the first argument
    that isn't an option to git itself (e.g. --literal-pathspecs), or the value
    of one (e.g. -c's), and the rest."""
    start = 0
    while start < len(args) and args[start].startswith("-"):
        start += 2 if args[start] in GIT_OPTIONS_WITH_VALUES else 1
    return (args[start], list(args[start + 1 :])) if start < len(args) else ("", [])


def changes_refs(args: tuple[str, ...]) -> bool:
    """Whether `git <args>` may move a ref or HEAD, or change a branch's
    upstream, which the ref snapshot holds. Only commands known to be read-only
    don't."""
    command, rest = split_command(args)
    if command in READ_ONLY_COMMANDS:
        return False
    if command == "branch":
        return rest != ["--show-current"]
    if command == "worktree":
        return rest[:1] != ["list"]
    if command == "remote":
        return rest[:1] not in ([], ["get-url"])
    if command == "config":
        return not any(a.startswith("--get") for a in rest)
    if command == "symbolic-ref":
        # Given a ref to point it at, it changes it.
        return len([a for a in rest if not a.startswith("-")]) != 1
    # Including switch, which creates a branch from a remote one of its name
    # (--guess) when there's no local one.
    return True


def changes_worktrees(args: tuple[str, ...]) -> bool:
    """Whether `git <args>`, which changes refs (see changes_refs), may also
    change which branch a worktree has checked out (or that it's detached, or
    rebasing), or the worktrees themselves. Only commands known to leave
    every worktree's HEAD alone don't."""
    command, rest = split_command(args)
    if command == "update-ref":
        return "HEAD" in rest
    return command not in {"fetch", "push", "config", "remote", "tag"}


def is_branch_name(name: str) -> bool:
    """Whether name could be a branch's (or ref's) name, rather than a revision
    such as main~1 or main@{upstream}."""
    return bool(name) and "@{" not in name and REVISION_SYNTAX.isdisjoint(name)


def is_object_id(name: str) -> bool:
    """Whether name is as long as a full object ID (40 hex digits for SHA-1, 64
    for SHA-256), which, unlike a ref, can't come to name another object."""
    return len(name) in (40, 64) and all(c in "0123456789abcdef" for c in name)


def same_file(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Whether a and b are the same file or directory, however each is spelled:
    on a case-insensitive filesystem (as macOS's and Windows' usually are),
    paths that differ in case can be. False if either doesn't exist."""
    try:
        return a.samefile(b)
    except OSError:
        return False


@contextmanager
def git_errors() -> Generator[None]:
    """Report a git command that fails in the block as a StackError, which
    the command line prints as git-stack's own errors."""
    try:
        yield
    except GitError as e:
        raise StackError(str(e)) from e


class GitClient:
    """Runs git in directory, as `git -C <directory>` does: $GIT_STACK_GIT if
    set, otherwise the git on PATH (see git_client.find_git), started through
    py-git-client's Git. A StackError if there's no such git."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = pathlib.Path(directory)
        try:
            self._git = Git(self.directory, env_var=GIT_VARIABLE)
        except (FileNotFoundError, ValueError) as e:
            # find_git's: no git on PATH, or on Windows, a batch file.
            raise StackError(str(e)) from e
        self._common_dir: pathlib.Path | None = None
        self._toplevel: pathlib.Path | None = None
        # See _object, is_ancestor and merged_tree: what they've found, for the
        # commits they were asked about, and the cat-file _object asks within a
        # snapshot, which starts its process when first asked.
        self._objects: dict[tuple[str, str], str] = {}
        self._ancestry: dict[tuple[str, str], bool] = {}
        self._merged_trees: dict[tuple[str, str], str | None] = {}
        self._cat_file = CatFile(self._git)
        # See branch_config: whether it has read the config file, and the
        # file's stat when it did (None if there was no file).
        self._config_read = False
        self._config_stat: tuple[int, int, int] | None = None
        self._branch_config: dict[tuple[str, str], str] = {}
        self._config: dict[str, list[str | None]] = {}
        # See snapshot: how many snapshot blocks we're in; the refs, as
        # {refname: (commit, the full refname of its upstream, or "")}, the
        # upstreams' refnames they were listed for, and the branch HEAD is on,
        # if it's one of them; `git worktree list --porcelain`'s output,
        # {worktree: the branch a rebase in it is rebasing} and {branch: its
        # reflog's commits}, once read; and {remote: its default branch}. The
        # lock is for commands that read them in several threads.
        self._snapshot_depth = 0
        self._refs: dict[str, tuple[str, str]] | None = None
        self._listed_upstreams: frozenset[str] = frozenset()
        self._head: str | None = None
        self._worktrees: str | None = None
        self._rebasing: dict[pathlib.Path, str | None] = {}
        self._reflogs: dict[str, tuple[str, ...]] = {}
        self._remote_defaults: dict[str, str | None] = {}
        self._refs_lock = threading.Lock()
        # Where passthrough sends git's stdout (default: ours), e.g. stderr when
        # our stdout is for --json output.
        self.passthrough_stdout: TextIO | None = None

    def _ran(self, args: tuple[str, ...]) -> None:
        """Forget the ref snapshot if `git <args>`, which just ran, may have
        changed it, whether or not it succeeded."""
        if changes_refs(args):
            self._forget_refs(worktrees=changes_worktrees(args))

    def _forget_refs(self, worktrees: bool = True) -> None:
        """Forget the ref snapshot and the reflogs, and unless worktrees is
        false, the worktrees' branches, so a thread reading them meanwhile
        can't then keep what it read from before the change: this waits for a
        thread holding the lock, and the others store what they read only if
        this hasn't replaced their dict meanwhile (see reflog_tips)."""
        with self._refs_lock:
            self._refs = None
            self._reflogs = {}
            if worktrees:
                self._worktrees = None
                self._rebasing = {}

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        """Run git and return stripped stdout, even if it fails when check is
        false."""
        try:
            return self._git.run(*args, input=input)
        except GitError as e:
            if check:
                raise StackError(f"git {' '.join(args)} failed:\n{e.stderr.strip()}") from e
            return e.stdout.strip()
        finally:
            self._ran(args)

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded: any failure is a no (see
        git_client's succeeds)."""
        try:
            return self._git.succeeds(*args)
        finally:
            self._ran(args)

    def passthrough(self, *args: str) -> bool:
        """Run git with output going straight to the terminal (stdout to
        passthrough_stdout)."""
        try:
            return self._git.passthrough(*args, stdout=self.passthrough_stdout)
        finally:
            self._ran(args)

    @contextmanager
    def snapshot(self) -> Generator[None]:
        """Within this, read every local branch's commit and upstream, its
        upstream's commit, and which is checked out (see current_branch), with
        one git call, and read them again only after a git call that may have
        changed them (see changes_refs). A command asks about the same few refs
        over and over, and each git call is slow enough to add up. The
        worktrees' branches (see worktree_of) and branches' reflogs (see
        reflog_tips) are read once too, and forgotten along with the refs
        (the worktrees' branches only after a git call that may move a
        worktree's HEAD; see changes_worktrees), and each remote's default
        branch (see remote_default_branch) is asked once.

        Other remote-tracking branches aren't read (unless there are too many
        upstreams to name), since a clone can have tens of thousands of them;
        they're asked of git as they're needed.

        Changes made meanwhile by anything else, such as another process, or
        a test running git, aren't seen, so a snapshot is only for the length
        of one command, which holds the lock (see StackCore.exclusive) if it
        changes anything. Blocks can nest; the snapshot is forgotten when the
        outermost one ends.

        A ref the snapshot doesn't have is still asked of git, which on a
        case-insensitive filesystem (as macOS's and Windows' usually are) can
        find it under a name that differs in case.
        """
        self._snapshot_depth += 1
        try:
            yield
        finally:
            self._snapshot_depth -= 1
            if not self._snapshot_depth:
                self._forget_refs()
                self._remote_defaults = {}
                self._cat_file.close()

    def _snapshot_refs(self) -> dict[str, tuple[str, str]] | None:
        """The refs (see snapshot), reading them if they haven't been, or None
        outside a snapshot block."""
        if not self._snapshot_depth:
            return None
        with self._refs_lock:
            if self._refs is None:
                upstreams = self._upstream_refs()
                # Unless there are too many to name on a command line (Windows
                # limits one to 32767 characters); then list every
                # remote-tracking branch.
                patterns = (
                    upstreams
                    if sum(len(u) + 1 for u in upstreams) <= MAX_UPSTREAM_PATTERNS_LENGTH
                    else ["refs/remotes/"]
                )
                with git_errors():
                    listed = git_client.refs(self._git, "refs/heads/", *patterns)
                head = next((ref.name.removeprefix("refs/heads/") for ref in listed.values() if ref.head), None)
                self._refs = {ref.name: (ref.object, ref.upstream) for ref in listed.values()}
                self._listed_upstreams, self._head = frozenset(upstreams), head
            return self._refs

    def _upstream_refs(self) -> list[str]:
        """The refnames of local branches' upstreams on remotes, as their
        config (branch.<name>.remote and .merge) gives them with the usual
        fetch refspec (refs/remotes/<remote>/<branch>)."""
        config = self.branch_config()
        return sorted(
            {
                f"refs/remotes/{remote}/{merge.removeprefix('refs/heads/')}"
                for (branch, key), remote in config.items()
                if key == "remote"
                and remote != "."
                and (merge := config.get((branch, "merge"), "")).startswith("refs/heads/")
            }
        )

    def common_dir(self) -> pathlib.Path:
        """The absolute path of the git directory every worktree shares: read
        from the files at the top of a worktree, without git, unless git's
        variables may name another (see git_client.git_common_dir)."""
        if self._common_dir is None:
            found = None if GIT_DIR_VARIABLES & os.environ.keys() else git_common_dir(self.directory)
            self._common_dir = found or pathlib.Path(
                self.run("rev-parse", "--path-format=absolute", "--git-common-dir")
            )
        return self._common_dir

    def branch_config(self, fresh: bool = False) -> dict[tuple[str, str], str]:
        """Every branch.<name>.<key> setting, as {(name, key in lower case): value}.

        Read with one git call, and read again only once the repository's config
        file has changed, which is whenever anything (us, a git command we run,
        or another process) writes to it. Commands look up the same few keys of
        every tracked branch over and over, and each git call is slow enough to
        add up. With fresh, it's read again regardless, for when a missed write
        (see "The branch config cache could miss a write" in docs/issues.md) would
        matter.
        """
        # Before reading, so a write that lands in between is read again next time.
        stat: tuple[int, int, int] | None
        try:
            st = (self.common_dir() / "config").stat()
            stat = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            # git works without one, as if it were empty.
            stat = None
        if fresh or not self._config_read or stat != self._config_stat:
            branches: dict[tuple[str, str], str] = {}
            with git_errors():
                entries = git_client.config(self._git)
            for key, values in entries.items():
                if key.startswith("branch."):
                    # Branch names can contain dots, but keys can't. The last
                    # value wins, as git reads it; a key with none is "".
                    branch, _, name = key.removeprefix("branch.").rpartition(".")
                    branches[branch, name] = values[-1] or ""
            self._config, self._branch_config = entries, branches
            self._config_stat, self._config_read = stat, True
        return self._branch_config

    def config_value(self, key: str) -> str | None:
        """key's value, as `git config --get` gives it (the last, and "" for
        a key with none), or None if it's unset. Read with the branch config
        (see branch_config), so it's no git call of its own, and read again
        when it is: once .git/config changes."""
        self.branch_config()
        values = self._config.get(config_key(key))
        return None if values is None else values[-1] or ""

    def config_get(self, branch: str, key: str) -> str | None:
        return self.branch_config().get((branch, key.lower())) or None

    def config_set(self, branch: str, key: str, value: str) -> None:
        self.write_config(f"branch.{branch}.{key}", value)

    def config_unset(self, branch: str, key: str, value: str | None = None) -> None:
        """Unset branch's key, or, if value is given, only if it's still value."""
        self.write_config(
            "--unset",
            *(("--fixed-value",) if value is not None else ()),
            f"branch.{branch}.{key}",
            *((value,) if value is not None else ()),
            check=False,
        )

    def write_config(self, *args: str, check: bool = True) -> None:
        """Run `git config <args>`, trying it again for up to
        CONFIG_LOCK_TIMEOUT seconds while another process has the config file
        locked (see git_client's Git.write_config).

        Writing anything but git-stack's own settings (see core.PARENT_KEY and
        the rest, which all start with "stack") forgets the ref snapshot, since
        it may change a branch's upstream.
        """
        try:
            self._git.write_config(*args, wait=CONFIG_LOCK_TIMEOUT)
        except GitError as e:
            if check:
                raise StackError(f"git config {' '.join(args)} failed:\n{e.stderr.strip()}") from e
        finally:
            key = next((a for a in args if not a.startswith("-")), "")
            if not key.rpartition(".")[2].lower().startswith("stack"):
                # A config write can't move a worktree's HEAD.
                self._forget_refs(worktrees=False)

    def rev(self, ref: str) -> str | None:
        """The commit ref names, or None if it names none. A local or
        remote-tracking branch's full refname, or a local branch's
        <name>@{upstream}, is looked up in the snapshot, if there is one and
        has it (see snapshot)."""
        if is_object_id(ref):
            return self._object(ref, "commit")
        branch = ref.removesuffix("@{upstream}")
        # Only read the snapshot if it could answer, e.g. not for HEAD.
        cacheable = is_branch_name(branch) and (branch != ref or ref.startswith(("refs/heads/", "refs/remotes/")))
        refs = self._snapshot_refs() if cacheable else None
        if refs is not None and ref in refs:
            return refs[ref][0]
        if refs is not None and branch != ref and (local := refs.get(f"refs/heads/{branch}")):
            upstream = local[1]
            if not upstream:
                return None
            if upstream in refs:
                return refs[upstream][0]
        return self._resolve(f"{ref}^{{commit}}")

    def tip(self, branch: str) -> str:
        """The commit local branch points at. Fails if branch doesn't exist.
        Within a snapshot that doesn't have it, asked of the snapshot's cat-file."""
        if (refs := self._snapshot_refs()) is not None and (entry := refs.get(f"refs/heads/{branch}")):
            return entry[0]
        if self._snapshot_depth:
            with git_errors():
                return self._cat_file.verify(f"refs/heads/{branch}")
        return self.run("rev-parse", "--verify", f"refs/heads/{branch}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether ancestor is descendant or one of its ancestors. Within a
        snapshot, where finding the commits they name costs nothing for a
        branch, asked once for the same two commits, which don't change."""
        if not self._snapshot_depth:
            return self.ok("merge-base", "--is-ancestor", ancestor, descendant)
        a, d = self.rev(ancestor), self.rev(descendant)
        if a is None or d is None:
            return False
        if (a, d) not in self._ancestry:
            self._ancestry[a, d] = self.ok("merge-base", "--is-ancestor", a, d)
        return self._ancestry[a, d]

    def tree(self, rev: str) -> str | None:
        """The tree of the commit rev names, or None if it names none."""
        commit = self.rev(rev)
        return self._object(commit, "tree") if commit is not None else None

    def _object(self, object_id: str, kind: str) -> str | None:
        """The kind object (e.g. a commit's tree) that object_id names, as
        `object_id^{kind}`, or None if there's none. Objects don't change, so
        each is looked up once; within a snapshot, by a `git cat-file` that
        runs until it ends, rather than a process each."""
        if (known := self._objects.get((object_id, kind))) is not None:
            return known
        found = self._resolve(f"{object_id}^{{{kind}}}")
        if found is not None:
            self._objects[object_id, kind] = found
        return found

    def _resolve(self, name: str) -> str | None:
        """The object name names, or None if it names none: within a snapshot,
        asked of a `git cat-file` that runs until it ends, rather than a
        process each."""
        if not self._snapshot_depth:
            return self.run("rev-parse", "--verify", "--quiet", name, check=False) or None
        with git_errors():
            return self._cat_file.resolve(name)

    def author_email(self) -> str | None:
        """The email address git authors commits with, or None if it has none."""
        ident = self.run("var", "GIT_AUTHOR_IDENT", check=False)
        start, end = ident.find("<"), ident.rfind(">")
        if not 0 <= start < end:
            return None
        return ident[start + 1 : end] or None

    def current_branch(self) -> str | None:
        """The branch checked out in this worktree, or None if HEAD is
        detached. Read from the snapshot if there is one and HEAD is on a
        branch that has a commit; an unborn one isn't a ref yet."""
        if self._snapshot_refs() is not None and self._head is not None:
            return self._head
        return self.run("branch", "--show-current") or None

    def local_branch_exists(self, name: str) -> bool:
        if (refs := self._snapshot_refs()) is not None and f"refs/heads/{name}" in refs:
            return True
        return self.ok("show-ref", "--verify", "--quiet", f"refs/heads/{name}")

    def local_branches(self) -> set[str]:
        if (refs := self._snapshot_refs()) is not None:
            return {ref.removeprefix("refs/heads/") for ref in refs if ref.startswith("refs/heads/")}
        return set(self.run("for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads").splitlines())

    def switch(self, branch: str) -> None:
        self.run(*SWITCH, branch)

    def operation_in_progress(self) -> bool:
        """Whether a rebase, cherry-pick, revert or merge has stopped and is
        waiting for the user.

        A cherry-pick (or revert) of several commits that stopped before
        changing anything (e.g. because of an untracked file in the way) leaves
        only its sequencer state behind, not CHERRY_PICK_HEAD.
        """
        names = ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer", "MERGE_HEAD")
        with git_errors():
            return any(path.exists() for path in git_paths(self._git, *names))

    def has_conflicts(self) -> bool:
        """Whether the index has unmerged paths, i.e. a merge stopped on a
        conflict, anywhere in the worktree, even with diff.relative set."""
        return bool(self.run("diff", "--no-relative", "--name-only", "--diff-filter=U"))

    def worktree_of(self, branch: str) -> pathlib.Path | None:
        """The path of the worktree branch is checked out in, if any, or that a
        rebase of branch is in progress in. The rebase detaches that worktree's
        HEAD, so `git worktree list` doesn't show the branch."""
        path = None
        for line in self._worktree_list().splitlines():
            if line.startswith("worktree "):
                path = pathlib.Path(line.removeprefix("worktree "))
            elif path is not None and (
                line == f"branch refs/heads/{branch}" or (line == "detached" and self.rebasing_in(path) == branch)
            ):
                return path
        return None

    def _worktree_list(self) -> str:
        """`git worktree list --porcelain`'s output, read once per snapshot."""
        if not self._snapshot_depth:
            return self.run("worktree", "list", "--porcelain")
        with self._refs_lock:
            if self._worktrees is None:
                self._worktrees = self.run("worktree", "list", "--porcelain")
            return self._worktrees

    def rebasing_in(self, worktree: pathlib.Path) -> str | None:
        """The branch a rebase in progress in worktree is rebasing, if any,
        found once per snapshot."""
        rebasing = self._rebasing
        if self._snapshot_depth and worktree in rebasing:
            return rebasing[worktree]
        branch = self._read_rebasing(worktree)
        with self._refs_lock:
            # As in reflog_tips.
            if self._snapshot_depth and self._rebasing is rebasing:
                rebasing[worktree] = branch
        return branch

    def _read_rebasing(self, worktree: pathlib.Path) -> str | None:
        git_dir = GitClient(worktree).run("rev-parse", "--absolute-git-dir", check=False)
        if not git_dir:  # e.g. the worktree was deleted without `git worktree remove`
            return None
        for name in ("rebase-merge", "rebase-apply"):
            try:
                head = (pathlib.Path(git_dir) / name / "head-name").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            return head.removeprefix("refs/heads/") if head.startswith("refs/heads/") else None
        return None

    def other_worktree_of(self, branch: str) -> pathlib.Path | None:
        """The path of the worktree branch is checked out (or being rebased) in,
        if that isn't this one. git won't check out, rebase or delete a branch
        that's checked out or being rebased elsewhere."""
        path = self.worktree_of(branch)
        if path is None or same_file(path, self.toplevel()):
            return None
        return path

    def upstream_gone(self, branch: str) -> bool:
        """Whether branch has an upstream that no longer exists, e.g. after
        `git fetch --prune` saw it deleted."""
        if (refs := self._snapshot_refs()) is not None:
            upstream = refs.get(f"refs/heads/{branch}", ("", ""))[1]
            if not upstream or upstream in refs:
                return False
            # Unless it wasn't listed, as with an unusual fetch refspec.
            if upstream.startswith("refs/heads/") or upstream in self._listed_upstreams:
                return True
        return self.run("for-each-ref", "--format=%(upstream:track)", f"refs/heads/{branch}") == "[gone]"

    def remotes(self) -> list[str]:
        return self.run("remote").split()

    def tracks_same_name(self, branch: str, remote: str) -> bool:
        """Whether branch's upstream is the branch of the same name on remote,
        which is how `git stack push` (or `git push -u`) sets it up."""
        return (
            self.config_get(branch, "remote") == remote and self.config_get(branch, "merge") == f"refs/heads/{branch}"
        )

    def has_branch_upstream(self, branch: str, remote: str) -> bool:
        """Whether branch's upstream is a branch on remote that git resolves to
        its remote-tracking branch, rather than config that only names remote
        (e.g. with a merge ref that isn't a branch, which git ignores)."""
        ref = self.config_get(branch, "merge")
        if self.config_get(branch, "remote") != remote or ref is None or not ref.startswith("refs/heads/"):
            return False
        upstream = self.run("rev-parse", "--symbolic-full-name", f"{branch}@{{upstream}}", check=False)
        return upstream == f"refs/remotes/{remote}/{ref.removeprefix('refs/heads/')}"

    def remote_branch_name(self, branch: str, remote: str) -> str:
        """The configured branch name on remote, or branch if it has no such upstream."""
        ref = self.config_get(branch, "merge")
        if self.config_get(branch, "remote") == remote and ref and ref.startswith("refs/heads/"):
            return ref.removeprefix("refs/heads/")
        return branch

    def toplevel(self) -> pathlib.Path:
        """The top directory of the worktree directory is in."""
        if self._toplevel is None:
            self._toplevel = pathlib.Path(self.run("rev-parse", "--show-toplevel"))
        return self._toplevel

    def remote_default_branch(self, remote: str) -> str | None:
        """The branch remote's HEAD points at on the server, or None if it can't
        be asked or its HEAD isn't a branch.

        Asked rather than read from refs/remotes/<remote>/HEAD, which fetch
        doesn't update when the server's default branch changes, once per
        snapshot.
        """
        if self._snapshot_depth and remote in self._remote_defaults:
            return self._remote_defaults[remote]
        default = self._ask_remote_default_branch(remote)
        if self._snapshot_depth:
            self._remote_defaults[remote] = default
        return default

    def _ask_remote_default_branch(self, remote: str) -> str | None:
        heads = self.run("ls-remote", "--symref", remote, "HEAD", check=False).splitlines()
        for line in heads:
            target, _, name = line.partition("\t")
            if name == "HEAD" and target.startswith("ref: refs/heads/"):
                return target.removeprefix("ref: refs/heads/")
        return None

    def remote_branch(self, branch: str) -> tuple[str, str, str] | None:
        """Return (remote, ref, tip) for branch's upstream on a remote, or None."""
        remote = self.config_get(branch, "remote")
        ref = self.config_get(branch, "merge")
        tip = self.rev(f"{branch}@{{upstream}}")
        if not remote or remote == "." or not ref or tip is None:
            return None
        return remote, ref, tip

    def delete_remote_branch(self, remote: str, ref: str, tip: str) -> bool:
        """Delete ref on remote, failing if it no longer points at tip."""
        return self.passthrough("push", f"--force-with-lease={ref}:{tip}", remote, f":{ref}")

    def push_would_fail(self, *args: str) -> str | None:
        """Why `git push <args>` would fail (git's error output), found with a
        dry run, or None if it wouldn't. The dry run skips the pre-push hook,
        which the real push runs, so it doesn't run twice."""
        try:
            self._git.run("push", "--dry-run", "--no-verify", *args)
        except GitError as e:
            return e.stderr.strip()
        return None

    def fetch(self, remote: str) -> None:
        if not self.passthrough("fetch", "--prune", remote):
            raise StackError(f"fetching {remote} failed")

    def reflog_tips(self, branch: str) -> list[str]:
        """Every commit branch has pointed at, as far back as its reflog goes.
        Read once per snapshot, and forgotten with the refs (moving a branch
        adds to its reflog)."""
        reflogs = self._reflogs
        if self._snapshot_depth and (known := reflogs.get(branch)) is not None:
            return list(known)
        tips = self.run("log", "--walk-reflogs", "--format=%H", f"refs/heads/{branch}", check=False).split()
        with self._refs_lock:
            # Unless _forget_refs, which replaces the dict, ran meanwhile.
            if self._snapshot_depth and self._reflogs is reflogs:
                reflogs[branch] = tuple(tips)
        return tips

    def finished_rebase(self, branch: str, before: str, onto: str) -> bool:
        """Whether the branch reflog records a rebase onto onto since before."""
        entries = self.run(
            "log", "--walk-reflogs", "--format=%H%x00%gs", f"refs/heads/{branch}", check=False
        ).splitlines()
        finished = False
        for entry in entries:
            tip, _, message = entry.partition("\0")
            if tip == before:
                return finished
            if message == f"rebase (finish): refs/heads/{branch} onto {onto}":
                finished = True
        return False

    def conflicts(self, branch: str, target: str) -> bool:
        """Whether merging branch into target would conflict."""
        try:
            # merge-tree's no (1) is a conflict.
            return not self._git.ok("merge-tree", "--write-tree", "--name-only", target, branch)
        except GitError as e:
            raise StackError(f"git merge-tree {target} {branch} failed:\n{e.stderr.strip()}") from e

    def contained_in(self, branch: str, target: str) -> bool:
        """Whether merging branch into target would change nothing.

        This is true after a squash merge even though none of branch's commits are
        in target's history. False if target names no commit.
        """
        tree = self.tree(target)
        return tree is not None and self.merged_tree(target, branch) == tree

    def merged_tree(self, ours: str, theirs: str) -> str | None:
        """The tree merging theirs into ours gives, or None if it conflicts (or
        either names no commit). Within a snapshot (see is_ancestor), asked
        once for the same two commits."""
        if not self._snapshot_depth:
            return self._merge_tree(ours, theirs)
        a, b = self.rev(ours), self.rev(theirs)
        if a is None or b is None:
            return None
        if (a, b) not in self._merged_trees:
            self._merged_trees[a, b] = self._merge_tree(a, b)
        return self._merged_trees[a, b]

    def _merge_tree(self, ours: str, theirs: str) -> str | None:
        try:
            return self._git.run("merge-tree", "--write-tree", ours, theirs).splitlines()[0]
        except GitError:
            return None

    def merges_with_own_changes(self, *args: str) -> list[str]:
        """The merge commits `git rev-list <args>` lists that change something
        merging their parents wouldn't: their tree isn't what a clean merge of
        their parents gives, as for a resolved conflict or an edit made while
        merging. Merges of more than two parents always count.

        Their own changes are in neither parent, so commits alone don't
        account for them.
        """
        merges: list[str] = []
        for line in self.run("rev-list", "--merges", "--parents", *args).splitlines():
            commit, *parents = line.split()
            if len(parents) != 2 or self.merged_tree(*parents) != self.tree(commit):
                merges.append(commit)
        return merges

    def commit_patch_ids(self, *commands: Sequence[str]) -> list[list[tuple[str, str]]]:
        """For each of commands, (patch-id, commit) for each commit that
        `git <command>` prints, in the order it prints them, with one `git
        patch-id` for them all (see git_client.patch_ids). For a diff, the
        commit is all zeros. Paths after `--` are files as git lists them
        (e.g. `git diff --name-only`): from the top of the worktree, and
        taken literally."""
        with git_errors():
            return git_client.patch_ids(self._git, *commands, paths_from_top=True)

    def identity_counts(self, *args: str) -> Counter[str]:
        """Count commit identities, including repeated author, date and subject."""
        return Counter(self.run("log", "--format=%an <%ae> %ad %s", "--date=raw", *args).splitlines())

    def is_rebased_copy(self, base: str, tip: str, new_base: str, new_tip: str) -> bool:
        """Compare an upstream history with replaying our commits into an isolated index."""

        def commits(revisions: str) -> list[tuple[str, str]]:
            lines = self.run("log", "--reverse", "--format=%H%x00%an <%ae> %ad %s", "--date=raw", revisions)
            return [(commit, identity) for commit, _, identity in (line.partition("\0") for line in lines.splitlines())]

        ours, theirs = commits(f"{base}..{tip}"), commits(f"{new_base}..{new_tip}")
        if len(ours) != len(theirs) or [i for _, i in ours] != [i for _, i in theirs]:
            return False
        with tempfile.TemporaryDirectory(prefix="git-stack-index-") as directory:
            env = {"GIT_INDEX_FILE": str(pathlib.Path(directory) / "index")}
            # Where git apply runs: in a subdirectory, it leaves out the paths
            # outside it.
            top = str(self.toplevel())

            def run(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
                return self._git.process(*args, capture_output=True, input=input, env=env)

            if run("read-tree", new_base).returncode:
                raise StackError("couldn't prepare an index to check the upstream rebase")
            for (commit, _), (upstream, _) in zip(ours, theirs, strict=True):
                # Every file's changes, even with diff.relative set.
                patch = run(
                    "show",
                    "--format=",
                    "--no-relative",
                    "--binary",
                    "--full-index",
                    "--no-color",
                    "--no-ext-diff",
                    commit,
                )
                if patch.returncode:
                    raise StackError(f"couldn't read changes in {commit}")
                if patch.stdout and run("-C", top, "apply", "--cached", "--3way", input=patch.stdout).returncode:
                    return False
                tree = run("write-tree")
                if tree.returncode or tree.stdout.decode().strip() != self.tree(upstream):
                    return False
        return True
