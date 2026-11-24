import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import Counter
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TextIO

import git_client
import pygit2
from git_client import (
    ConfigSet,
    ConfigUnset,
    Git,
    GitCache,
    GitError,
    NoAnswerError,
    PerformanceRecorder,
    PushResult,
    Ref,
    decode,
    push,
)

from git_stack import git_read
from git_stack.errors import StackError
from git_stack.git_command import PatchGit, find_git
from git_stack.git_read import config_key
from git_stack.performance import active_timeline, measure_native

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
    if command in READ_ONLY_COMMANDS or args == ("--exec-path",):
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
        raise StackError(f"git {' '.join(e.command)} failed:\n{e.stderr.strip()}") from e
    except NoAnswerError as e:
        raise StackError(str(e)) from e


class _NativeReads(threading.local):
    # Each worker owns its libgit2 repository handle, including its caches.
    repository: pygit2.Repository | None = None
    graph_usable: bool | None = None


class GitClient:
    """Runs git in directory, as `git -C <directory>` does: $GIT_STACK_GIT if
    set, otherwise the git on PATH. Local object and graph lookups use pygit2
    where it can read the graph Git uses."""

    def __init__(self, directory: str | os.PathLike[str] = ".", *, close_fds: bool = True) -> None:
        self.directory = pathlib.Path(directory)
        try:
            timeline = active_timeline.get()
            self._git = Git(
                self.directory,
                program=find_git(GIT_VARIABLE),
                performance=timeline.git if timeline else None,
                close_fds=close_fds,
            )
        except (FileNotFoundError, ValueError) as e:
            # No git on PATH, or on Windows, a batch file.
            raise StackError(str(e)) from e
        self._common_dir: pathlib.Path | None = None
        self._toplevel: pathlib.Path | None = None
        # See _object, is_ancestor and merged_tree: what they've found, for the
        # commits they were asked about.
        self._objects: dict[tuple[str, str], str] = {}
        self._ancestry: dict[tuple[str, str], bool] = {}
        self._merged_trees: dict[tuple[str, str], str | None] = {}
        self._native = _NativeReads()
        # See branch_config: whether it has read the config file, and the
        # file's stat when it did (None if there was no file).
        self._config_read = False
        self._config_stat: tuple[int, int, int] | None = None
        self._branch_config: dict[tuple[str, str], str] = {}
        self._config: dict[str, list[str | None]] = {}
        # See snapshot: how many snapshot blocks we're in; the refs by full name, the
        # upstreams' refnames they were listed for, and the branch HEAD is on,
        # if it's one of them; `git worktree list --porcelain`'s output,
        # {worktree: the branch a rebase in it is rebasing} and {branch: its
        # reflog's commits}, once read; and {remote: its default branch}. The
        # lock is for commands that read them in several threads.
        self._snapshot_depth = 0
        self._refs: dict[str, Ref] | None = None
        self._has_replacements = False
        self._refs_config: dict[str, list[str | None]] = {}
        self._listed_upstreams: frozenset[str] = frozenset()
        self._head: str | None = None
        self._worktrees: str | None = None
        self._rebasing: dict[pathlib.Path, str | None] = {}
        self._reflogs: dict[str, tuple[str, ...]] = {}
        self._remote_defaults: dict[str, str | None] = {}
        self._push_defaults: dict[str, frozenset[str] | None] = {}
        self._refs_lock = threading.Lock()
        # Where passthrough sends git's stdout (default: ours), e.g. stderr when
        # our stdout is for --json output.
        self.passthrough_stdout: TextIO | None = None

    @contextmanager
    def record_performance(self, recorder: PerformanceRecorder) -> Generator[None]:
        """Send Git events to the command's recorder while it runs."""
        original = self._git.performance
        self._git.performance = recorder
        try:
            yield
        finally:
            self._git.performance = original

    @contextmanager
    def isolate_parallel_launches(self) -> Generator[None]:
        """Keep parallel Git launches from inheriting extra file descriptors.

        Enter before submitting any work and join all workers before leaving.
        On macOS without pipe2, a subprocess pipe briefly exists before Python marks
        it close-on-exec. Inheriting it can delay EOF until the sibling exits.
        """
        git = self._git
        original = git.close_fds
        git.close_fds = True
        try:
            yield
        finally:
            git.close_fds = original

    def _git_with(
        self, client_type: type[Git] = Git, *, cache: GitCache | None = None, object_id_length: int | None = None
    ) -> Git:
        return client_type(
            self.directory,
            program=self._git.program,
            performance=self._git.performance,
            close_fds=self._git.close_fds,
            cache=cache,
            object_id_length=object_id_length,
        )

    def _ran(self, args: tuple[str, ...]) -> None:
        """Forget the ref snapshot if `git <args>`, which just ran, may have
        changed it, whether or not it succeeded."""
        if changes_refs(args):
            worktrees = changes_worktrees(args)
            if worktrees:
                self._git.forget()
            self._forget_refs(worktrees=worktrees)

    def _forget_refs(self, worktrees: bool = True) -> None:
        """Forget the ref snapshot and the reflogs, and unless worktrees is
        false, the worktrees' branches, so a thread reading them meanwhile
        can't then keep what it read from before the change: this waits for a
        thread holding the lock, and the others store what they read only if
        this hasn't replaced their dict meanwhile (see reflog_tips)."""
        with self._refs_lock:
            self._refs = None
            self._native = _NativeReads()
            self._objects = {}
            self._reflogs = {}
            if worktrees:
                self._worktrees = None
                self._rebasing = {}

    def run(
        self, *args: str, check: bool = True, input: str | None = None, env: Mapping[str, str] | None = None
    ) -> str:
        """Run git and return stripped stdout, even if it fails when check is
        false."""
        try:
            return self._git.run(*args, input=input, env=env)
        except GitError as e:
            if check:
                raise StackError(f"git {' '.join(args)} failed:\n{e.stderr.strip()}") from e
            return e.stdout.strip()
        except NoAnswerError as e:
            raise StackError(str(e)) from e
        finally:
            self._ran(args)

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded."""
        try:
            with git_errors():
                return self._git.succeeds(*args)
        finally:
            self._ran(args)

    def ref_transaction(self, commands: Sequence[str], *, publish: bool) -> None:
        """Prepare the ref transaction to check its inputs, then publish or abort it."""
        action = "commit" if publish else "abort"
        input = "\0".join(("start", *commands, "prepare", action, ""))
        if publish:
            self.run("update-ref", "--stdin", "-z", input=input)
        else:
            # An aborted transaction leaves every ref and reflog unchanged.
            with git_errors():
                self._git.run("update-ref", "--stdin", "-z", input=input)

    def passthrough(self, *args: str) -> bool:
        """Run git with output going straight to the terminal (stdout to
        passthrough_stdout)."""
        try:
            with git_errors():
                return self._git.passthrough(*args, stdout=self.passthrough_stdout)
        finally:
            self._ran(args)

    @property
    def program(self) -> str:
        """The selected Git executable, including any user-supplied wrapper."""
        return self._git.program

    def push_report(self, *args: str) -> PushResult:
        """Run one push with a structured result for each reported destination."""
        try:
            with git_errors():
                result = push(self._git, *args)
            print(result.stdout, end="", file=self.passthrough_stdout or sys.stdout)
            print(result.stderr, end="", file=sys.stderr)
            return result
        finally:
            self._ran(("push", *args))

    def addressable_push_urls(self, remote: str, urls: Iterable[str]) -> set[str]:
        """The URLs a repair can address without losing named transport settings."""
        self.branch_config(fresh=True)
        # An explicit URL would drop connection and server-command settings
        # attached to the named remote, potentially addressing another repo.
        transport_settings = ("receivepack", "vcs", "proxy", "proxyauthmethod", "mirror")
        if any(config_key(f"remote.{remote}.{key}") in self._config for key in transport_settings):
            return set()
        rules = ("insteadof", "pushinsteadof")
        configured = self._configured_remote_urls(remote, "pushurl") or self._configured_remote_urls(remote, "url")
        if self._ambiguous_push_urls(configured, rules):
            return set()
        return {
            url
            for url in urls
            if not self._url_is_rewritten(url, *rules)
            and config_key(f"remotes.{url}") not in self._config
            and not any(key.startswith(f"remote.{url}.") for key in self._config)
        }

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
        outermost one ends, along with the library's cache of Git metadata.

        A ref the snapshot doesn't have is still asked of git, which on a
        case-insensitive filesystem (as macOS's and Windows' usually are) can
        find it under a name that differs in case.
        """
        original_git = self._git
        if not self._snapshot_depth:
            self._git = self._git_with(cache=GitCache())
        self._snapshot_depth += 1
        try:
            yield
        finally:
            self._snapshot_depth -= 1
            if not self._snapshot_depth:
                self._git = original_git
                self._forget_refs()
                self._remote_defaults = {}
                self._push_defaults = {}
                self._refs_config = {}
                self._ancestry.clear()

    def _snapshot_refs(self) -> dict[str, Ref] | None:
        """The refs (see snapshot), reading them if they haven't been, or None
        outside a snapshot block."""
        if not self._snapshot_depth:
            return None
        with self._refs_lock:
            if self._refs is None:
                upstreams = self._upstream_refs()
                config = self._config
                # Unless there are too many to name on a command line (Windows
                # limits one to 32767 characters); then list every
                # remote-tracking branch.
                patterns = (
                    upstreams
                    if sum(len(u) + 1 for u in upstreams) <= MAX_UPSTREAM_PATTERNS_LENGTH
                    else ["refs/remotes/"]
                )
                with git_errors():
                    listed = git_client.refs(self._git, "refs/heads/", "refs/replace/", *patterns)
                head = next((ref.name.removeprefix("refs/heads/") for ref in listed.values() if ref.head), None)
                self._has_replacements = any(ref.startswith("refs/replace/") for ref in listed)
                self._refs = listed
                self._refs_config = config
                self._listed_upstreams, self._head = frozenset(upstreams), head
            return self._refs

    def cacheable_history(self) -> bool:
        """Whether commit IDs describe the same graph across commands."""
        if self._snapshot_refs() is None or self._has_replacements:
            return False
        if {"GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE"} & os.environ.keys():
            return False
        common = self.common_dir()
        return not ((common / "shallow").exists() or (common / "info" / "grafts").exists())

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

    def _open_repository(self) -> pygit2.Repository | None:
        if not self._snapshot_depth:
            return git_read.open_repository(self.directory)
        if self._native.repository is None:
            self._native.repository = git_read.open_repository(self.directory)
        return self._native.repository

    def _graph_repository(self) -> pygit2.Repository | None:
        repo = self._open_repository()
        if repo is None:
            return None
        usable = self._native.graph_usable if self._snapshot_depth else None
        if usable is None:
            try:
                with measure_native("graph"):
                    common = git_read.common_directory(repo)
                    has_replacements = (
                        self._has_replacements
                        if self._refs is not None
                        else any(ref.startswith("refs/replace/") for ref in repo.listall_references())
                    )
                    config = repo.config
                    usable = (
                        not repo.is_shallow
                        and not has_replacements
                        and not (common / "info" / "grafts").exists()
                        and "extensions.partialclone" not in config
                        and not any(
                            entry.name.endswith(".promisor") for entry in config if entry.name.startswith("remote.")
                        )
                    )
            except pygit2.GitError, ValueError, OSError:
                usable = False
            if self._snapshot_depth:
                self._native.graph_usable = usable
        return repo if usable else None

    def common_dir(self) -> pathlib.Path:
        """Where all worktrees share Git metadata."""
        if self._common_dir is None:
            repo = self._open_repository()
            if repo is not None:
                self._common_dir = git_read.common_directory(repo)
            else:
                with git_errors():
                    self._common_dir = git_client.git_common_dir(self._git)
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

    def direct_remote_url(self, remote: str) -> str | None:
        """The configured URL when fetch and push use it without a rewrite."""
        url = self._shared_remote_url(remote)
        return None if url is not None and self._url_is_rewritten(url, "insteadof") else url

    def _shared_remote_url(self, remote: str) -> str | None:
        """A single configured URL that both fetch and push resolve the same way."""
        self.branch_config()
        urls = self._config.get(config_key(f"remote.{remote}.url"), [])
        if len(urls) != 1 or not urls[0] or config_key(f"remote.{remote}.pushurl") in self._config:
            return None
        url = urls[0]
        return None if self._url_is_rewritten(url, "pushinsteadof") else url

    def _url_is_rewritten(self, url: str, *rules: str) -> bool:
        return any(
            prefix is None or url.startswith(prefix)
            for key, prefixes in self._config.items()
            if key.startswith("url.") and key.rpartition(".")[2] in rules
            for prefix in prefixes
        )

    def config_get(self, branch: str, key: str) -> str | None:
        return self.branch_config().get((branch, key.lower())) or None

    def config_set(self, branch: str, key: str, value: str) -> None:
        self._write_config(ConfigSet(f"branch.{branch}.{key}", value))

    def config_unset(self, branch: str, key: str, value: str | None = None) -> None:
        """Unset a branch setting, optionally only if it still has the given value."""
        self._write_config(ConfigUnset(f"branch.{branch}.{key}", value))

    def _write_config(self, change: ConfigSet | ConfigUnset) -> None:
        """Write branch config, retrying while the config file is locked."""
        try:
            self._git.write_configs(change, wait=CONFIG_LOCK_TIMEOUT)
        except GitError as error:
            if isinstance(change, ConfigSet):
                raise StackError(f"git config {change.key} {change.value} failed:\n{error.stderr.strip()}") from error
        except NoAnswerError as error:
            raise StackError(str(error)) from error
        finally:
            if not change.key.rpartition(".")[2].lower().startswith("stack"):
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
            return refs[ref].object
        if refs is not None and branch != ref and (local := refs.get(f"refs/heads/{branch}")):
            upstream = local.upstream
            if not upstream:
                return None
            if upstream in refs:
                return refs[upstream].object
        return self._resolve(f"{ref}^{{commit}}")

    def tip(self, branch: str) -> str:
        """The commit a local branch points at. Fail if the branch does not exist."""
        if (tip := self.rev(f"refs/heads/{branch}")) is not None:
            return tip
        return self.run("rev-parse", "--verify", f"refs/heads/{branch}")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Whether the two revisions resolve to commits and one is an ancestor of the other."""
        if not self._snapshot_depth and self._graph_repository() is None:
            return self.ok("merge-base", "--is-ancestor", ancestor, descendant)
        a, d = self.rev(ancestor), self.rev(descendant)
        if a is None or d is None:
            return False
        if (a, d) in self._ancestry and self._snapshot_depth:
            return self._ancestry[a, d]
        repo = self._graph_repository()
        answer = self._descendant_of(repo, d, a)
        if self._snapshot_depth:
            self._ancestry[a, d] = answer
        return answer

    def _descendant_of(self, repo: pygit2.Repository | None, descendant: str, ancestor: str) -> bool:
        if repo is not None:
            if ancestor == descendant:
                return True
            try:
                with measure_native("ancestry"):
                    return repo.descendant_of(descendant, ancestor)
            except pygit2.GitError, ValueError:
                pass
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def check_ancestry(self, ancestor: str, descendant: str, *, env: Mapping[str, str] | None = None) -> bool:
        """Check ancestry without memoization. Git errors are not a negative answer."""
        with git_errors():
            return self._git.ok("merge-base", "--is-ancestor", ancestor, descendant, env=env)

    def tree(self, rev: str) -> str | None:
        """The tree of the commit rev names, or None if it names none."""
        commit = self.rev(rev)
        return self._object(commit, "tree") if commit is not None else None

    def first_subject(self, base: str, branch: str) -> str | None:
        """The first subject Git displays for commits on branch since base."""
        ref = f"refs/heads/{branch}"
        repo = self._graph_repository()
        if repo is not None and all(
            self.config_value(key) is None
            for key in ("i18n.logOutputEncoding", "i18n.commitEncoding", "log.showSignature")
        ):
            tip = self.tip(branch)
            if tip == base:
                return None
            # In a linear range the oldest commit is the one based on base.
            # Git still orders merged histories and formats unusual messages.
            try:
                with measure_native("subject"):
                    commit = repo[tip].peel(pygit2.Commit)
                    while len(commit.parent_ids) == 1:
                        parent = commit.parent_ids[0]
                        if str(parent) == base:
                            first, _, rest = commit.raw_message.decode("utf-8").partition("\n")
                            if (
                                not commit.message_encoding
                                and first
                                and first == first.strip()
                                and first.splitlines() == [first]
                                and "\0" not in first
                                and (not rest or rest.startswith("\n"))
                            ):
                                return first
                            break
                        commit = repo[parent].peel(pygit2.Commit)
            except KeyError, pygit2.GitError, ValueError:
                pass
        lines = self.run("log", "--reverse", "--format=%s", f"{base}..{ref}").splitlines()
        return lines[0] if lines else None

    def first_named_blob(self, ref: str, directories: Sequence[str], names: Collection[str]) -> str | None:
        """Read the first matching blob in the given directories, in Git tree order."""
        repo = self._graph_repository()
        tip = self.rev(ref)
        if repo is not None and tip is not None:
            try:
                with measure_native("tree-blob"):
                    return git_read.first_named_blob(repo[tip].peel(pygit2.Tree), directories, names)
            except KeyError, pygit2.GitError, ValueError:
                pass
        for directory in directories:
            listing = self.run("ls-tree", "-z", f"{ref}:{directory}", check=False)
            for entry in filter(None, listing.split("\0")):
                info, _, name = entry.partition("\t")
                _, kind, oid = info.split()
                if kind == "blob" and name.lower() in names:
                    return self.run("cat-file", "blob", oid)
        return None

    def _object(self, object_id: str, kind: str) -> str | None:
        """Resolve an object by type, caching results until the next ref change."""
        if (known := self._objects.get((object_id, kind))) is not None:
            return known
        found = self._resolve(f"{object_id}^{{{kind}}}")
        if found is not None:
            self._objects[object_id, kind] = found
        return found

    def _resolve(self, name: str) -> str | None:
        """Resolve simple names in pygit2 and leave Git's revision syntax to Git."""
        base, _, suffix = name.partition("^{")
        simple = (
            is_object_id(base)
            or base == "HEAD"
            or (base.startswith(("refs/heads/", "refs/remotes/")) and is_branch_name(base))
        )
        if simple and suffix in {"commit}", "tree}"} and (repo := self._graph_repository()) is not None:
            kind = pygit2.Commit if suffix == "commit}" else pygit2.Tree
            with measure_native("resolve"):
                try:
                    if is_object_id(base):
                        return str(repo[base].peel(kind).id)
                    try:
                        reference = repo.lookup_reference(base)
                    except KeyError:
                        return None
                    return str(reference.peel(kind).id)
                except KeyError, pygit2.GitError, ValueError, TypeError:
                    # Git can resolve objects that need a lazy fetch from a promisor remote.
                    pass
        args = ("rev-parse", "--verify", "--quiet", "--end-of-options", name)
        with git_errors():
            result = self._git.process(*args, capture_output=True)
        if self._snapshot_depth and result.returncode not in (0, 1):
            raise StackError(f"git {' '.join(args)} failed:\n{decode(result.stderr).strip()}")
        return decode(result.stdout).strip() or None

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
        repo = self._open_repository()
        if repo is not None:
            try:
                with measure_native("current-branch"):
                    target = repo.lookup_reference("HEAD").target
                    if isinstance(target, pygit2.Oid):
                        return None
                    # Git resolves chained aliases with its own traversal limit.
                    if target.startswith("refs/heads/") and isinstance(
                        repo.lookup_reference(target).target, pygit2.Oid
                    ):
                        return target.removeprefix("refs/heads/")
            except KeyError, pygit2.GitError, ValueError:
                pass
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
        if (repo := self._open_repository()) is not None:
            return any((pathlib.Path(repo.path) / name).exists() for name in names)
        with git_errors():
            paths = git_client.git_paths(self._git, *names)
        return any(path.exists() for path in paths)

    def has_conflicts(self) -> bool:
        """Whether the index has unmerged paths, i.e. a merge stopped on a
        conflict, anywhere in the worktree, even with diff.relative set."""
        return bool(self.run("diff", "--no-relative", "--name-only", "--diff-filter=U"))

    def worktree_of(self, branch: str, *, fresh: bool = False) -> pathlib.Path | None:
        """The path of the worktree branch is checked out in, if any, or that a
        rebase of branch is in progress in. The rebase detaches that worktree's
        HEAD, so `git worktree list` doesn't show the branch."""
        path = None
        for line in self._worktree_list(fresh=fresh).splitlines():
            if line.startswith("worktree "):
                path = pathlib.Path(line.removeprefix("worktree "))
            elif path is not None and (
                line == f"branch refs/heads/{branch}" or (line == "detached" and self.rebasing_in(path) == branch)
            ):
                return path
        return None

    def _worktree_list(self, *, fresh: bool = False) -> str:
        """`git worktree list --porcelain`'s output, read once per snapshot."""
        if not self._snapshot_depth:
            return self.run("worktree", "list", "--porcelain")
        with self._refs_lock:
            if fresh or self._worktrees is None:
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

    def other_worktree_of(self, branch: str, *, fresh: bool = False) -> pathlib.Path | None:
        """The path of the worktree branch is checked out (or being rebased) in,
        if that isn't this one. git won't check out, rebase or delete a branch
        that's checked out or being rebased elsewhere."""
        path = self.worktree_of(branch, fresh=fresh)
        if path is None or same_file(path, self.toplevel()):
            return None
        return path

    def upstream_gone(self, branch: str) -> bool:
        """Whether branch has an upstream that no longer exists, e.g. after
        `git fetch --prune` saw it deleted."""
        if (refs := self._snapshot_refs()) is not None:
            local = refs.get(f"refs/heads/{branch}")
            upstream = local.upstream if local is not None else ""
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

    def upstream_is_at_tip(self, branch: str) -> bool:
        """Whether the ref snapshot proves that the branch and its upstream match."""
        self.branch_config()
        refs = self._snapshot_refs()
        if refs is None or self._refs_config != self._config:
            return False
        local = refs.get(f"refs/heads/{branch}")
        upstream = refs.get(local.upstream) if local is not None else None
        return upstream is not None and local is not None and upstream.object == local.object

    def has_branch_upstream(self, branch: str, remote: str) -> bool:
        """Whether branch's upstream is a branch on remote that git resolves to
        its remote-tracking branch, rather than config that only names remote
        (e.g. with a merge ref that isn't a branch, which git ignores)."""
        ref = self.config_get(branch, "merge")
        if self.config_get(branch, "remote") != remote or ref is None or not ref.startswith("refs/heads/"):
            return False
        expected = f"refs/remotes/{remote}/{ref.removeprefix('refs/heads/')}"
        if (refs := self._snapshot_refs()) is not None:
            local = refs.get(f"refs/heads/{branch}")
            upstream = refs.get(expected)
            # rev-parse dereferences symbolic refs and fails if the upstream is missing.
            # A fetch-refspec edit can change the mapping without moving any refs.
            fetch_key = config_key(f"remote.{remote}.fetch")
            if (
                local is not None
                and local.upstream == expected
                and upstream is not None
                and not upstream.symref
                and self._refs_config.get(fetch_key) == self._config.get(fetch_key)
            ):
                return True
        upstream = self.run("rev-parse", "--symbolic-full-name", f"{branch}@{{upstream}}", check=False)
        return upstream == expected

    def remote_branch_name(self, branch: str, remote: str) -> str:
        """The configured branch name on remote, or branch if it has no such upstream."""
        ref = self.config_get(branch, "merge")
        if self.config_get(branch, "remote") == remote and ref and ref.startswith("refs/heads/"):
            return ref.removeprefix("refs/heads/")
        return branch

    def toplevel(self) -> pathlib.Path:
        """The top directory of the worktree directory is in."""
        if self._toplevel is None:
            repo = self._open_repository()
            if repo is not None and not repo.is_bare:
                gitdir = pathlib.Path(repo.path)
                # Git detects inside-git-dir calls and honors worktree-specific core.worktree.
                if not (gitdir / "config.worktree").exists() and not self.directory.resolve().is_relative_to(
                    gitdir.resolve()
                ):
                    self._toplevel = pathlib.Path(repo.workdir)
            if self._toplevel is None:
                self._toplevel = pathlib.Path(self.run("rev-parse", "--show-toplevel"))
        return self._toplevel

    def remote_default_branch(self, remote: str, *, known: str | None = None) -> str | None:
        """The branch remote's HEAD points at on the server, or None if it can't
        be asked or its HEAD isn't a branch.

        Asked once per snapshot unless the caller already knows the answer
        from the server. The local refs/remotes/<remote>/HEAD can be stale.
        """
        if self._snapshot_depth and remote in self._remote_defaults:
            return self._remote_defaults[remote]
        default = known if known is not None else self._ask_remote_default_branch(remote)
        if self._snapshot_depth:
            self._remote_defaults[remote] = default
        return default

    def _ask_remote_default_branch(self, remote: str) -> str | None:
        return self._default_from_heads(self.run("ls-remote", "--symref", remote, "HEAD", check=False))

    @staticmethod
    def _default_from_heads(heads: str) -> str | None:
        for line in heads.splitlines():
            target, _, name = line.partition("\t")
            if name == "HEAD" and target.startswith("ref: refs/heads/"):
                return target.removeprefix("ref: refs/heads/")
        return None

    def push_default_branches(self, remote: str, *, known: str | None = None) -> frozenset[str] | None:
        """Protect the fetch default and every distinct push destination's default."""
        if self._snapshot_depth and remote in self._push_defaults:
            return self._push_defaults[remote]
        if self._shared_remote_url(remote) is not None:
            default = self.remote_default_branch(remote, known=known)
            defaults = frozenset({default}) if default is not None else None
        else:
            defaults = self._push_destination_defaults(remote, known=known)
        if self._snapshot_depth:
            self._push_defaults[remote] = defaults
        return defaults

    def push_urls(self, remote: str) -> tuple[str, ...]:
        """The effective push URLs for a remote."""
        shared_url = self._shared_remote_url(remote)
        # The main config file can stay unchanged when an include or worktree
        # config changes, even during a restack hook. Ask Git in those cases.
        separate_config = any(
            key in ("include.path", "extensions.worktreeconfig") or key.startswith("includeif.") for key in self._config
        )
        return self._resolved_push_urls(remote) if shared_url is None or separate_config else (shared_url,)

    def configured_push_urls(self, remote: str) -> list[str]:
        """Original destinations, for host identity behind an insteadOf transport rewrite."""
        explicit = self._configured_remote_urls(remote, "pushurl")
        return explicit or [
            url
            for url in self._configured_remote_urls(remote, "url")
            if not self._url_is_rewritten(url, "pushinsteadof")
        ]

    def _resolved_push_urls(self, remote: str) -> tuple[str, ...]:
        try:
            output = self._git.run("remote", "get-url", "--push", "--all", remote, strip=False)
        except GitError:
            return ()
        return tuple(output.removesuffix("\n").split("\n")) if output else ()

    def _push_destination_defaults(self, remote: str, *, known: str | None) -> frozenset[str] | None:
        push_urls = self._resolved_push_urls(remote)
        if not push_urls:
            return None
        urls = self._configured_remote_urls(remote, "url")
        explicit = self._configured_remote_urls(remote, "pushurl")
        rules = ("insteadof",) if explicit else ("insteadof", "pushinsteadof")
        if len(push_urls) > 1 and self._ambiguous_push_urls(explicit or urls, rules):
            return None
        fetch_url = urls[0] if urls else None
        if fetch_url is not None and self._url_is_rewritten(fetch_url, "insteadof"):
            fetch_url = None
        distinct = [url for url in dict.fromkeys(push_urls) if url != fetch_url]
        if not distinct or (len(distinct) == 1 and known is not None):
            default = self.remote_default_branch(remote, known=known)
            return self._combine_defaults(default, map(self._default_at_resolved_url, distinct))
        # The transports can wait independently. Keep fetch errors first and
        # join every probe before releasing the ref snapshot or publishing.
        with self.isolate_parallel_launches(), ThreadPoolExecutor(max_workers=min(4, len(distinct))) as pool:
            probes = [pool.submit(self._default_at_resolved_url, url) for url in distinct]
            default = self.remote_default_branch(remote, known=known)
            return self._combine_defaults(default, (probe.result() for probe in probes))

    @staticmethod
    def _combine_defaults(default: str | None, branches: Iterable[str | None]) -> frozenset[str] | None:
        if default is None:
            return None
        defaults = {default}
        for branch in branches:
            if branch is None:
                return None
            defaults.add(branch)
        return frozenset(defaults)

    def _configured_remote_urls(self, remote: str, setting: str) -> list[str]:
        """Configured URLs after the last empty value clears the list."""
        urls: list[str] = []
        for url in self._config.get(config_key(f"remote.{remote}.{setting}"), []):
            if url:
                urls.append(url)
            else:
                urls.clear()
        return urls

    def _ambiguous_push_urls(self, configured: list[str], rules: tuple[str, ...]) -> bool:
        """Git separates URLs with newlines and leaves newlines in URLs unescaped."""
        return any("\n" in url for url in configured) or any(
            key.startswith("url.")
            and "\n" in key
            and key.rpartition(".")[2] in rules
            and any(url.startswith(prefix or "") for prefix in prefixes for url in configured)
            for key, prefixes in self._config.items()
        )

    def _default_at_resolved_url(self, url: str) -> str | None:
        # Git has already applied the URL rewrite for the push. A single
        # rewrite from a temporary name avoids applying a second rule, and
        # keeps credentials in the URL out of the command line.
        probe = f"git-stack-probe-{uuid.uuid4().hex}"
        count = int(os.environ.get("GIT_CONFIG_COUNT") or "0")
        env = {
            "GIT_CONFIG_COUNT": str(count + 1),
            f"GIT_CONFIG_KEY_{count}": f"url.{url}.insteadOf",
            f"GIT_CONFIG_VALUE_{count}": probe,
        }
        try:
            heads = self._git.run("ls-remote", "--symref", "--", probe, "HEAD", env=env)
        except GitError:
            return None
        return self._default_from_heads(heads)

    def remote_branch(self, branch: str) -> tuple[str, str, str] | None:
        """Return (remote, ref, tip) for branch's upstream on a remote, or None."""
        remote = self.config_get(branch, "remote")
        ref = self.config_get(branch, "merge")
        tip = self.rev(f"{branch}@{{upstream}}")
        if not remote or remote == "." or not ref or tip is None:
            return None
        return remote, ref, tip

    def delete_remote_branch(self, remote: str, ref: str, tip: str, no_verify: bool = False) -> bool:
        """Delete ref on remote, failing if it no longer points at tip."""
        return self.passthrough(
            "push", *(["--no-verify"] if no_verify else []), f"--force-with-lease={ref}:{tip}", remote, f":{ref}"
        )

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
        if tree is None:
            return False
        if self._graph_repository() is not None:
            branch_tree = self.tree(branch)
            if branch_tree is not None and self.is_ancestor(branch, target):
                return True
            if branch_tree is not None and self.is_ancestor(target, branch):
                return branch_tree == tree
        return self.merged_tree(target, branch) == tree

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
        patch-id` for them all. For a diff, the
        commit is all zeros. Paths after `--` are files as git lists them
        (e.g. `git diff --name-only`): from the top of the worktree, and
        taken literally."""
        if not commands:
            return []
        # libgit2 opens SHA-1 repositories. For other formats, let Git tell the library.
        git = self._git_with(
            PatchGit, cache=self._git.cache, object_id_length=40 if self._open_repository() is not None else None
        )
        with git_errors():
            return git_client.patch_ids(git, *commands, paths_from_top=True)

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
