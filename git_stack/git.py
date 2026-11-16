import contextlib
import functools
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import TextIO

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
# The diff of the patch GitClient.commit_patch_ids ends each command's output
# with.
END_MARKER_DIFF = "diff --git a/end b/end\n--- a/end\n+++ b/end\n@@ -0,0 +1 @@\n+end\n"
# Characters a branch name can't have, but a revision (e.g. main~1) can.
REVISION_SYNTAX = frozenset("~^:?*[\\ ")
# Whether git's processes close the file descriptors they'd otherwise inherit.
# On Linux and macOS, not closing them lets Python start git with posix_spawn,
# which on macOS is much quicker than forking. Python opens files
# non-inheritable, so git inherits only what git-stack itself inherited, as it
# would if run from the shell. A pipe, though, is inheritable for a moment
# where Python can't open it non-inheritable at once (macOS before 27, which
# has no pipe2), and a git another thread starts then keeps it open, so whoever
# reads it waits for that git to end. Most git processes end soon, but the
# `git cat-file` that GitClient._object starts runs until its snapshot ends,
# which can wait for that reader, so it always closes them. On Windows, closing them keeps a git
# process from inheriting the pipes another thread is starting a process with.
CLOSE_FDS = os.name == "nt"


@functools.cache
def git_program() -> str:
    """The git that git-stack runs: $GIT_STACK_GIT if set, otherwise the git on
    PATH (a wrapper such as git-ai included), as an absolute path, which Python
    needs to use posix_spawn (see CLOSE_FDS). On Windows, only a git.exe, as
    starting "git" would find: a git.cmd or git.bat would run in cmd.exe, which
    interprets characters such as & that a branch name can have."""
    return os.environ.get("GIT_STACK_GIT") or shutil.which("git.exe" if os.name == "nt" else "git") or "git"


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


def decode(output: bytes) -> str:
    """git's output as text: UTF-8, which git writes on every system (rather
    than in the locale's encoding, which on Windows usually isn't), keeping
    bytes that aren't (e.g. a file's in a diff) as they are, so encode gives
    them back unchanged."""
    return output.decode("utf-8", "surrogateescape")


def encode(text: str) -> bytes:
    """text, from decode, as the bytes git wrote."""
    return text.encode("utf-8", "surrogateescape")


class GitClient:
    """Runs git in directory, as `git -C <directory>` does."""

    def __init__(self, directory: str | os.PathLike[str] = ".") -> None:
        self.directory = pathlib.Path(directory)
        self._common_dir: pathlib.Path | None = None
        self._toplevel: pathlib.Path | None = None
        self._object_id_length: int | None = None
        # See _object, is_ancestor and merged_tree: what they've found, for the
        # commits they were asked about, and the cat-file process _object runs
        # within a snapshot.
        self._objects: dict[tuple[str, str], str] = {}
        self._ancestry: dict[tuple[str, str], bool] = {}
        self._merged_trees: dict[tuple[str, str], str | None] = {}
        self._cat_file: subprocess.Popen[bytes] | None = None
        self._cat_file_lock = threading.Lock()
        # See branch_config: whether it has read the config file, and the
        # file's stat when it did (None if there was no file).
        self._config_read = False
        self._config_stat: tuple[int, int, int] | None = None
        self._branch_config: dict[tuple[str, str], str] = {}
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

    def _command(self, *args: str) -> list[str]:
        return [git_program(), "-C", str(self.directory), *args]

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
        """Run git and return stripped stdout.

        Input and output are bytes, decoded and encoded here (see decode), so
        Windows doesn't turn \n into \r\n on the way in.
        """
        result = subprocess.run(
            self._command(*args),
            capture_output=True,
            input=None if input is None else encode(input),
            close_fds=CLOSE_FDS,
            check=False,
        )
        self._ran(args)
        if check and result.returncode != 0:
            raise StackError(f"git {' '.join(args)} failed:\n{decode(result.stderr).strip()}")
        return decode(result.stdout).strip()

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded."""
        result = subprocess.run(self._command(*args), capture_output=True, close_fds=CLOSE_FDS, check=False)
        self._ran(args)
        return result.returncode == 0

    def passthrough(self, *args: str) -> bool:
        """Run git with output going straight to the terminal (stdout to
        passthrough_stdout)."""
        result = subprocess.run(self._command(*args), stdout=self.passthrough_stdout, close_fds=CLOSE_FDS, check=False)
        self._ran(args)
        return result.returncode == 0

    @contextmanager
    def snapshot(self) -> Generator[None, None, None]:
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
                self._stop_cat_file()

    def _snapshot_refs(self) -> dict[str, tuple[str, str]] | None:
        """The refs (see snapshot), reading them if they haven't been, or None
        outside a snapshot block."""
        if not self._snapshot_depth:
            return None
        with self._refs_lock:
            if self._refs is None:
                refs = {}
                upstreams = self._upstream_refs()
                # Unless there are too many to name on a command line (Windows
                # limits one to 32767 characters); then list every
                # remote-tracking branch.
                patterns = (
                    upstreams
                    if sum(len(u) + 1 for u in upstreams) <= MAX_UPSTREAM_PATTERNS_LENGTH
                    else ["refs/remotes/"]
                )
                listing = self.run(
                    "for-each-ref",
                    "--format=%(refname)%00%(objectname)%00%(upstream)%00%(HEAD)",
                    "refs/heads/",
                    *patterns,
                )
                head = None
                for line in listing.splitlines():
                    name, commit, upstream, current = line.split("\0")
                    refs[name] = (commit, upstream)
                    if current == "*":
                        head = name.removeprefix("refs/heads/")
                self._refs, self._listed_upstreams, self._head = refs, frozenset(upstreams), head
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
        """The absolute path of the git directory every worktree shares."""
        if self._common_dir is None:
            self._common_dir = pathlib.Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir"))
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
            config = {}
            for entry in filter(
                None, self.run("config", "--null", "--get-regexp", r"^branch\.", check=False).split("\0")
            ):
                key, _, value = entry.partition("\n")
                # Branch names can contain dots, but keys can't.
                branch, _, name = key.removeprefix("branch.").rpartition(".")
                config[branch, name] = value
            self._branch_config, self._config_stat, self._config_read = config, stat, True
        return self._branch_config

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
        """Run `git config <args>`, retrying for up to CONFIG_LOCK_TIMEOUT
        seconds while another process has the config file locked.

        git config doesn't wait for the lock itself, and fails at once instead.
        A write either happens or doesn't, so retrying it is safe.

        Writing anything but git-stack's own settings (see core.PARENT_KEY and
        the rest, which all start with "stack") forgets the ref snapshot, since
        it may change a branch's upstream.
        """
        deadline = time.monotonic() + CONFIG_LOCK_TIMEOUT
        delay = 0.01
        while True:
            result = subprocess.run(
                self._command("config", *args),
                capture_output=True,
                encoding="utf-8",
                errors="surrogateescape",
                close_fds=CLOSE_FDS,
                check=False,
            )
            locked = result.returncode != 0 and "could not lock config file" in result.stderr
            if not locked or time.monotonic() >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        key = next((a for a in args if not a.startswith("-")), "")
        if not key.rpartition(".")[2].lower().startswith("stack"):
            # A config write can't move a worktree's HEAD.
            self._forget_refs(worktrees=False)
        if check and result.returncode != 0:
            raise StackError(f"git config {' '.join(args)} failed:\n{result.stderr.strip()}")

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
        return self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) or None

    def tip(self, branch: str) -> str:
        """The commit local branch points at. Fails if branch doesn't exist."""
        if (refs := self._snapshot_refs()) is not None and (entry := refs.get(f"refs/heads/{branch}")):
            return entry[0]
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
        each is looked up once; within a snapshot, by a `git cat-file
        --batch-check` that runs until it ends, rather than a process each."""
        if (known := self._objects.get((object_id, kind))) is not None:
            return known
        query = f"{object_id}^{{{kind}}}"
        if not self._snapshot_depth:
            found = self.run("rev-parse", "--verify", "--quiet", query, check=False) or None
        else:
            with self._cat_file_lock:
                if self._cat_file is None:
                    self._cat_file = subprocess.Popen(
                        self._command("cat-file", "--batch-check=%(objectname)"),
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        # Not CLOSE_FDS: it runs until the snapshot ends, so
                        # a pipe it inherited might never end (see CLOSE_FDS).
                        close_fds=True,
                    )
                assert self._cat_file.stdin is not None and self._cat_file.stdout is not None
                try:
                    self._cat_file.stdin.write(encode(query) + b"\n")
                    self._cat_file.stdin.flush()
                    answer = decode(self._cat_file.stdout.readline()).strip()
                except OSError:  # e.g. a broken pipe, if it stopped before
                    answer = ""
                if not answer:
                    # So the next lookup starts another.
                    self._end_cat_file(kill=True)
                    raise StackError(f"git cat-file stopped while looking up {query}")
            # Or "<query> missing", or "<query> ambiguous".
            found = None if " " in answer else answer
        if found is not None:
            self._objects[object_id, kind] = found
        return found

    def _stop_cat_file(self) -> None:
        """End the `git cat-file` _object started, if it did."""
        with self._cat_file_lock:
            self._end_cat_file()

    def _end_cat_file(self, kill: bool = False) -> None:
        """End the `git cat-file` _object started, if it did (killing it if
        kill is set), and forget it. The caller holds _cat_file_lock."""
        process, self._cat_file = self._cat_file, None
        if process is None:
            return
        assert process.stdin is not None and process.stdout is not None
        if kill:
            process.kill()
        with contextlib.suppress(OSError):  # a broken pipe, if it stopped with a query unread
            process.stdin.close()
        process.wait()
        process.stdout.close()

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
        paths = self.run(
            "rev-parse", "--path-format=absolute", *(arg for name in names for arg in ("--git-path", name))
        ).splitlines()
        return any(pathlib.Path(path).exists() for path in paths)

    def has_conflicts(self) -> bool:
        """Whether the index has unmerged paths, i.e. a merge stopped on a conflict."""
        return bool(self.run("diff", "--name-only", "--diff-filter=U"))

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
        result = subprocess.run(
            self._command("push", "--dry-run", "--no-verify", *args),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            close_fds=CLOSE_FDS,
            check=False,
        )
        return result.stderr.strip() if result.returncode != 0 else None

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
        result = subprocess.run(
            self._command("merge-tree", "--write-tree", "--name-only", target, branch),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            close_fds=CLOSE_FDS,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise StackError(f"git merge-tree {target} {branch} failed:\n{result.stderr.strip()}")
        return result.returncode == 1

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
        result = subprocess.run(
            self._command("merge-tree", "--write-tree", ours, theirs),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            close_fds=CLOSE_FDS,
            check=False,
        )
        return result.stdout.splitlines()[0] if result.returncode == 0 else None

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
        `git <command>` prints, in the order it prints them. For a diff, the
        commit is all zeros.

        Each command runs, but one `git patch-id` reads them all: after each
        command's output comes a patch of our own, whose commit (1, 2, ...)
        can't be a real one's, to mark where it ends.
        """
        length = self.object_id_length()
        patches = []
        for i, (command, *rest) in enumerate(commands, 1):
            if command == "diff":
                # A diff has no commit line, so give it one, rather than rely on
                # the blank line after the marker before it to end the marker.
                patches.append(f"commit {'0' * length}")
            patches.append(self.run("--literal-pathspecs", command, "--no-color", "--no-ext-diff", *rest))
            patches.append(f"commit {i:0{length}x}\n\n{END_MARKER_DIFF}")
        results: list[list[tuple[str, str]]] = [[]]
        for line in self.run("patch-id", "--stable", input="\n".join(patches) + "\n").splitlines():
            patch_id, commit = line.split()
            if commit == f"{len(results):0{length}x}":
                results.append([])
            else:
                results[-1].append((patch_id, commit))
        if len(results) != len(commands) + 1:
            raise StackError(f"git patch-id printed {len(results) - 1} end markers for {len(commands)} commands")
        return results[:-1]

    def object_id_length(self) -> int:
        """How many hex digits the repository's object IDs have: 40 for SHA-1,
        64 for SHA-256. Taken from the ref snapshot if it's been read."""
        if self._object_id_length is None:
            # Read once, since another thread may forget it meanwhile.
            if refs := self._refs:
                self._object_id_length = len(next(iter(refs.values()))[0])
            else:
                self._object_id_length = 64 if self.run("rev-parse", "--show-object-format") == "sha256" else 40
        return self._object_id_length

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
            env = {**os.environ, "GIT_INDEX_FILE": str(pathlib.Path(directory) / "index")}

            def run(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(
                    self._command(*args), input=input, env=env, capture_output=True, close_fds=CLOSE_FDS, check=False
                )

            if run("read-tree", new_base).returncode:
                raise StackError("couldn't prepare an index to check the upstream rebase")
            for (commit, _), (upstream, _) in zip(ours, theirs, strict=True):
                patch = run("show", "--format=", "--binary", "--full-index", "--no-color", "--no-ext-diff", commit)
                if patch.returncode:
                    raise StackError(f"couldn't read changes in {commit}")
                if patch.stdout and run("apply", "--cached", "--3way", input=patch.stdout).returncode:
                    return False
                tree = run("write-tree")
                if tree.returncode or tree.stdout.decode().strip() != self.tree(upstream):
                    return False
        return True
