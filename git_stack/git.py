import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from git_stack.errors import StackError

DEFAULT_REMOTE = "origin"
# How long to keep retrying a git config write that finds the config file locked
# by another process, which usually only holds it for a moment.
CONFIG_LOCK_TIMEOUT = 5.0
# The git commands git-stack runs that never move a ref or change a branch's
# upstream, so the ref snapshot (see GitClient.snapshot) outlives them. See
# changes_refs for a few more.
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
# Characters a branch name can't have, but a revision (e.g. main~1) can.
REVISION_SYNTAX = frozenset("~^:?*[\\ ")


def changes_refs(args: tuple[str, ...]) -> bool:
    """Whether `git <args>` may move a ref or change a branch's upstream,
    which the ref snapshot holds. Only commands known to be read-only don't."""
    # The command is the first argument that isn't an option to git itself
    # (e.g. --literal-pathspecs), or the value of one (e.g. -c's).
    start = 0
    while start < len(args) and args[start].startswith("-"):
        start += 2 if args[start] in GIT_OPTIONS_WITH_VALUES else 1
    command, rest = (args[start], list(args[start + 1 :])) if start < len(args) else ("", [])
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


def is_branch_name(name: str) -> bool:
    """Whether name could be a branch's (or ref's) name, rather than a revision
    such as main~1 or main@{upstream}."""
    return bool(name) and "@{" not in name and REVISION_SYNTAX.isdisjoint(name)


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
        self.directory = os.fspath(directory)
        self._common_dir: str | None = None
        # See branch_config.
        self._config_stat: tuple[int, int, int] | None = None
        self._branch_config: dict[tuple[str, str], str] = {}
        # See snapshot: how many snapshot blocks we're in, and the refs, as
        # {refname: (commit, the full refname of its upstream, or "")}, once
        # read. The lock is for commands that read refs in several threads.
        self._snapshot_depth = 0
        self._refs: dict[str, tuple[str, str]] | None = None
        self._refs_lock = threading.Lock()
        # Where passthrough sends git's stdout (default: ours), e.g. stderr when
        # our stdout is for --json output.
        self.passthrough_stdout: TextIO | None = None

    def _command(self, *args: str) -> list[str]:
        return ["git", "-C", self.directory, *args]

    def _ran(self, args: tuple[str, ...]) -> None:
        """Forget the ref snapshot if `git <args>`, which just ran, may have
        changed it, whether or not it succeeded."""
        if changes_refs(args):
            self._forget_refs()

    def _forget_refs(self) -> None:
        """Forget the ref snapshot, waiting for a thread reading it to finish,
        so it can't then keep what it read from before the change."""
        with self._refs_lock:
            self._refs = None

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        """Run git and return stripped stdout.

        Input and output are bytes, decoded and encoded here (see decode), so
        Windows doesn't turn \n into \r\n on the way in.
        """
        result = subprocess.run(
            self._command(*args), capture_output=True, input=None if input is None else encode(input), check=False
        )
        self._ran(args)
        if check and result.returncode != 0:
            raise StackError(f"git {' '.join(args)} failed:\n{decode(result.stderr).strip()}")
        return decode(result.stdout).strip()

    def ok(self, *args: str) -> bool:
        """Run git and report whether it succeeded."""
        result = subprocess.run(self._command(*args), capture_output=True, check=False)
        self._ran(args)
        return result.returncode == 0

    def passthrough(self, *args: str) -> bool:
        """Run git with output going straight to the terminal (stdout to
        passthrough_stdout)."""
        result = subprocess.run(self._command(*args), stdout=self.passthrough_stdout, check=False)
        self._ran(args)
        return result.returncode == 0

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """Within this, read every local and remote-tracking branch's commit,
        and each local branch's upstream, with one git call, and read them
        again only after a git call that may have changed them (see
        changes_refs). A command asks about the same few refs over and over,
        and each git call is slow enough to add up.

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
                self._refs = None

    def _snapshot_refs(self) -> dict[str, tuple[str, str]] | None:
        """The refs (see snapshot), reading them if they haven't been, or None
        outside a snapshot block."""
        if not self._snapshot_depth:
            return None
        with self._refs_lock:
            if self._refs is None:
                refs = {}
                listing = self.run(
                    "for-each-ref", "--format=%(refname)%00%(objectname)%00%(upstream)", "refs/heads", "refs/remotes"
                )
                for line in listing.splitlines():
                    name, commit, upstream = line.split("\0")
                    refs[name] = (commit, upstream)
                self._refs = refs
            return self._refs

    def common_dir(self) -> str:
        """The absolute path of the git directory every worktree shares."""
        if self._common_dir is None:
            self._common_dir = self.run("rev-parse", "--path-format=absolute", "--git-common-dir")
        return self._common_dir

    def branch_config(self, fresh: bool = False) -> dict[tuple[str, str], str]:
        """Every branch.<name>.<key> setting, as {(name, key in lower case): value}.

        Read with one git call, and read again only once the repository's config
        file has changed, which is whenever anything (us, a git command we run,
        or another process) writes to it. Commands look up the same few keys of
        every tracked branch over and over, and each git call is slow enough to
        add up. With fresh, it's read again regardless, for when a missed write
        (see "The branch config cache could miss a write" in ISSUES.md) would
        matter.
        """
        # Before reading, so a write that lands in between is read again next time.
        st = os.stat(os.path.join(self.common_dir(), "config"))
        stat = (st.st_ino, st.st_mtime_ns, st.st_size)
        if fresh or stat != self._config_stat:
            config = {}
            for entry in filter(
                None, self.run("config", "--null", "--get-regexp", r"^branch\.", check=False).split("\0")
            ):
                key, _, value = entry.partition("\n")
                # Branch names can contain dots, but keys can't.
                branch, _, name = key.removeprefix("branch.").rpartition(".")
                config[branch, name] = value
            self._branch_config, self._config_stat = config, stat
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
                check=False,
            )
            locked = result.returncode != 0 and "could not lock config file" in result.stderr
            if not locked or time.monotonic() >= deadline:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        key = next((a for a in args if not a.startswith("-")), "")
        if not key.rpartition(".")[2].lower().startswith("stack"):
            self._forget_refs()
        if check and result.returncode != 0:
            raise StackError(f"git config {' '.join(args)} failed:\n{result.stderr.strip()}")

    def rev(self, ref: str) -> str | None:
        """The commit ref names, or None if it names none. A local or
        remote-tracking branch's full refname, or a local branch's
        <name>@{upstream}, is looked up in the snapshot, if there is one and
        has it (see snapshot)."""
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
        return self.ok("merge-base", "--is-ancestor", ancestor, descendant)

    def author_email(self) -> str | None:
        """The email address git authors commits with, or None if it has none."""
        ident = self.run("var", "GIT_AUTHOR_IDENT", check=False)
        start, end = ident.find("<"), ident.rfind(">")
        if not 0 <= start < end:
            return None
        return ident[start + 1 : end] or None

    def current_branch(self) -> str | None:
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
        self.run("switch", "--quiet", branch)

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
        return any(os.path.exists(path) for path in paths)

    def has_conflicts(self) -> bool:
        """Whether the index has unmerged paths, i.e. a merge stopped on a conflict."""
        return bool(self.run("diff", "--name-only", "--diff-filter=U"))

    def worktree_of(self, branch: str) -> str | None:
        """The path of the worktree branch is checked out in, if any, or that a
        rebase of branch is in progress in, in the system's form (git gives
        C:/... on Windows). The rebase detaches that worktree's HEAD, so
        `git worktree list` doesn't show the branch."""
        path = None
        for line in self.run("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif path is not None and (
                line == f"branch refs/heads/{branch}" or (line == "detached" and self.rebasing_in(path) == branch)
            ):
                return os.path.normpath(path)
        return None

    def rebasing_in(self, worktree: str) -> str | None:
        """The branch a rebase in progress in worktree is rebasing, if any."""
        git_dir = GitClient(worktree).run("rev-parse", "--absolute-git-dir", check=False)
        if not git_dir:  # e.g. the worktree was deleted without `git worktree remove`
            return None
        for name in ("rebase-merge", "rebase-apply"):
            try:
                with open(os.path.join(git_dir, name, "head-name"), encoding="utf-8") as f:
                    head = f.read().strip()
            except OSError:
                continue
            return head.removeprefix("refs/heads/") if head.startswith("refs/heads/") else None
        return None

    def other_worktree_of(self, branch: str) -> str | None:
        """The path of the worktree branch is checked out (or being rebased) in,
        if that isn't this one. git won't check out, rebase or delete a branch
        that's checked out or being rebased elsewhere."""
        path = self.worktree_of(branch)
        if path is None or os.path.realpath(path) == os.path.realpath(self.run("rev-parse", "--show-toplevel")):
            return None
        return path

    def upstream_gone(self, branch: str) -> bool:
        """Whether branch has an upstream that no longer exists, e.g. after
        `git fetch --prune` saw it deleted."""
        if (refs := self._snapshot_refs()) is not None:
            upstream = refs.get(f"refs/heads/{branch}", ("", ""))[1]
            return bool(upstream) and upstream not in refs
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

    def remote_default_branch(self, remote: str) -> str | None:
        """The branch remote's HEAD points at on the server, or None if it can't
        be asked or its HEAD isn't a branch.

        Asked every time rather than read from refs/remotes/<remote>/HEAD, which
        fetch doesn't update when the server's default branch changes.
        """
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
            check=False,
        )
        return result.stderr.strip() if result.returncode != 0 else None

    def fetch(self, remote: str) -> None:
        if not self.passthrough("fetch", "--prune", remote):
            raise StackError(f"fetching {remote} failed")

    def reflog_tips(self, branch: str) -> list[str]:
        """Every commit branch has pointed at, as far back as its reflog goes."""
        return self.run("log", "--walk-reflogs", "--format=%H", f"refs/heads/{branch}", check=False).split()

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
            check=False,
        )
        if result.returncode not in (0, 1):
            raise StackError(f"git merge-tree {target} {branch} failed:\n{result.stderr.strip()}")
        return result.returncode == 1

    def contained_in(self, branch: str, target: str) -> bool:
        """Whether merging branch into target would change nothing.

        This is true after a squash merge even though none of branch's commits are
        in target's history.
        """
        return self.merged_tree(target, branch) == self.run("rev-parse", f"{target}^{{tree}}")

    def merged_tree(self, ours: str, theirs: str) -> str | None:
        """The tree merging theirs into ours gives, or None if it conflicts."""
        result = subprocess.run(
            self._command("merge-tree", "--write-tree", ours, theirs),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.splitlines()[0]

    def merges_with_own_changes(self, *args: str) -> list[str]:
        """The merge commits `git rev-list <args>` lists that change something
        merging their parents wouldn't: their tree isn't what a clean merge of
        their parents gives, as for a resolved conflict or an edit made while
        merging. Merges of more than two parents always count.

        Their own changes are in neither parent, so commits alone don't
        account for them.
        """
        merges = []
        for line in self.run("rev-list", "--merges", "--parents", *args).splitlines():
            commit, *parents = line.split()
            if len(parents) != 2 or self.merged_tree(*parents) != self.run("rev-parse", f"{commit}^{{tree}}"):
                merges.append(commit)
        return merges

    def patch_ids(self, *args: str) -> set[str]:
        """The patch-ids of the commits or diff that `git <args>` prints."""
        return {patch_id for patch_id, _ in self.commit_patch_ids(*args)}

    def commit_patch_ids(self, *args: str) -> list[tuple[str, str]]:
        """(patch-id, commit) for each commit that `git <args>` prints, in the
        order it prints them. For a diff, the commit is all zeros."""
        command, *rest = args
        patches = self.run("--literal-pathspecs", command, "--no-color", "--no-ext-diff", *rest)
        lines = self.run("patch-id", "--stable", input=patches + "\n").splitlines()
        return [(patch_id, commit) for patch_id, commit in (line.split() for line in lines)]

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
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(directory, "index")}

            def run(*args: str, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(self._command(*args), input=input, env=env, capture_output=True, check=False)

            if run("read-tree", new_base).returncode:
                raise StackError("couldn't prepare an index to check the upstream rebase")
            for (commit, _), (upstream, _) in zip(ours, theirs, strict=True):
                patch = run("show", "--format=", "--binary", "--full-index", "--no-color", "--no-ext-diff", commit)
                if patch.returncode:
                    raise StackError(f"couldn't read changes in {commit}")
                if patch.stdout and run("apply", "--cached", "--3way", input=patch.stdout).returncode:
                    return False
                tree = run("write-tree")
                if tree.returncode or tree.stdout.decode().strip() != self.run("rev-parse", f"{upstream}^{{tree}}"):
                    return False
        return True
