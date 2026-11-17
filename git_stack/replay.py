"""Build a combined base on a scratch branch and publish it after all stages succeed."""

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import cast
from uuid import uuid4

from git_stack.dependency_plan import PlannedBranch
from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id

PENDING = "stack.pendingReplay"
TIPS = "stackReplayTips"
STALE = "stackReplayStale"
COPIES = "stackReplayCopies"
PIN_PREFIX = "refs/git-stack/replay/"


class EmptyOwnCommit(StackError):
    """An originally nonempty own commit was replayed with no changes."""


@dataclass(frozen=True)
class Replay:
    branch: str
    parents: tuple[str, ...]
    tips: tuple[str, ...]
    original: str
    worktree: str
    before: str
    old_base: str
    secondary: tuple[str, ...]
    own: tuple[str, ...]
    head: str
    boundary: str = ""
    in_stage: bool = False
    ready: bool = False
    remaining: tuple[PlannedBranch, ...] = ()
    latest: bool = False
    token: str = ""
    copies: tuple[str, ...] = ()
    command: str = "restack"
    method: str = "rebase"

    @property
    def refs(self) -> str:
        return PIN_PREFIX + self.token + "/"

    @property
    def scratch(self) -> str:
        return f"git-stack-replay/{self.token}"

    def was_published(self, git: GitClient) -> bool:
        return git.rev(self.refs + "published") == self.head

    def save(self, git: GitClient) -> None:
        git.write_config(PENDING, json.dumps(asdict(self), ensure_ascii=True))

    def clear(self, git: GitClient) -> None:
        if git.local_branch_exists(self.scratch):
            git.run("branch", "--quiet", "--delete", "--force", self.scratch)
        git.write_config("--unset", PENDING)
        refs = git.run("for-each-ref", "--format=%(refname)", PIN_PREFIX).splitlines()
        if refs:
            git.run("update-ref", "--stdin", input="".join(f"delete {ref}\n" for ref in refs))

    def pin(self, git: GitClient) -> None:
        # All queued commits are reachable from these tips.
        tips = dict.fromkeys((self.before, *self.tips, self.old_base))
        git.run("update-ref", "--stdin", input="".join(f"update {self.refs}{i} {tip}\n" for i, tip in enumerate(tips)))
        self.save(git)


def pending(git: GitClient) -> Replay | None:
    value = git.config_value(PENDING)
    if not value:
        return None
    try:
        return parse_pending(git, cast(dict[str, object], json.loads(value)))
    except (TypeError, ValueError, KeyError) as error:
        raise StackError("the pending multi-base replay record is invalid") from error


def parse_pending(git: GitClient, fields: dict[str, object]) -> Replay:
    def string(key: str) -> str:
        value = fields[key]
        if not isinstance(value, str):
            raise TypeError(key)
        return value

    def boolean(key: str) -> bool:
        value = fields[key]
        if not isinstance(value, bool):
            raise TypeError(key)
        return value

    def strings(key: str) -> tuple[str, ...]:
        return string_tuple(fields[key])

    job = Replay(
        string("branch"),
        strings("parents"),
        strings("tips"),
        string("original"),
        string("worktree"),
        string("before"),
        string("old_base"),
        strings("secondary"),
        strings("own"),
        string("head"),
        string("boundary"),
        boolean("in_stage" if "in_stage" in fields else "in_pick"),
        boolean("ready"),
        decode_remaining(fields["remaining"]),
        boolean("latest"),
        string("token"),
        strings("copies"),
        string("command") if "command" in fields else "restack",
        string("method") if "method" in fields else "cherry-pick",
    )
    validate_pending(git, job)
    return job


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError("expected a list")
    values = cast(list[object], value)
    if any(not isinstance(item, str) for item in values):
        raise TypeError("expected strings")
    return tuple(str(item) for item in values)


def decode_remaining(value: object) -> tuple[PlannedBranch, ...]:
    if not isinstance(value, list):
        raise TypeError("expected remaining branches")
    remaining: list[PlannedBranch] = []
    for row in cast(list[object], value):
        if not isinstance(row, list):
            raise TypeError("expected a branch and its dependencies")
        branch, dependencies = cast(list[object], row)
        parents = string_tuple(dependencies)
        if not isinstance(branch, str) or not parents:
            raise ValueError("invalid remaining branch")
        remaining.append((branch, parents))
    return tuple(remaining)


def validate_pending(git: GitClient, job: Replay) -> None:
    if job.method not in {"rebase", "cherry-pick"}:
        raise ValueError("invalid replay method")
    if job.command not in {"restack", "pull", "sync"}:
        raise ValueError("invalid replay command")
    if len(job.token) != 32 or not all(c in "0123456789abcdef" for c in job.token):
        raise ValueError("invalid replay token")
    refnames = {job.branch, *job.parents}
    for branch, parents in job.remaining:
        refnames.update((branch, *parents))
    for branch in refnames:
        if not git.ok("check-ref-format", f"refs/heads/{branch}"):
            raise ValueError("invalid branch name")
    for oid in (job.before, job.old_base, job.head, *job.tips, *job.secondary, *job.own):
        if not is_object_id(oid):
            raise ValueError("invalid object ID")
    if len(job.tips) != len(job.parents) or not job.parents or (job.boundary and not is_object_id(job.boundary)):
        raise ValueError("incomplete replay record")


def commits(git: GitClient, tip: str, excluding: tuple[str, ...], name: str) -> tuple[str, ...]:
    rows = git.run("rev-list", "--reverse", "--topo-order", "--parents", tip, "--not", *excluding, "--")
    result: list[str] = []
    for row in rows.splitlines():
        commit, *parents = row.split()
        if len(parents) != 1:
            raise StackError(f"{name} contains a merge or root commit that cannot be replayed safely")
        result.append(commit)
    return tuple(result)


def require_direct_target(git: GitClient, branch: str) -> None:
    if git.ok("symbolic-ref", "--quiet", f"refs/heads/{branch}"):
        raise StackError(f"cannot replay {branch}: its branch ref is a symbolic ref")


def plan(
    git: GitClient,
    branch: str,
    parents: tuple[str, ...],
    base: str,
    original: str,
    remaining: tuple[PlannedBranch, ...] = (),
    latest: bool = False,
    *,
    root_forks: dict[str, str],
    command: str = "restack",
) -> Replay:
    require_direct_target(git, branch)
    tips = tuple(git.tip(parent) for parent in parents)
    secondary: list[str] = []
    aliases = copy_origins(git)
    for i, (parent, tip) in enumerate(zip(parents, tips, strict=True)):
        if i == 0:
            continue
        if not any(git.ok("merge-base", tip, previous) for previous in tips[:i]):
            raise StackError(f"{parent} has no shared history with the preceding bases of {branch}")
        excluded = tips[:i]
        if fork := root_forks.get(parent):
            excluded += (fork,)
        pending_commits = commits(git, tip, excluded, parent)
        secondary.extend(unique_sources(git, tips[:i], pending_commits, aliases))
    before = git.tip(branch)
    return Replay(
        branch,
        parents,
        tips,
        original,
        str(git.toplevel()),
        before,
        base,
        tuple(secondary),
        commits(git, before, (base,), branch),
        tips[0],
        remaining=remaining,
        latest=latest,
        token=uuid4().hex,
        command=command,
    )


def copy_origins(git: GitClient) -> dict[str, str]:
    """Maps unmodified replay copies to their exact source commits."""
    aliases: dict[str, str] = {}
    for (_, key), value in git.branch_config().items():
        if key == COPIES.lower():
            pairs = value.split()
            for copy, source in zip(pairs[::2], pairs[1::2], strict=False):
                if is_object_id(copy) and is_object_id(source):
                    aliases[copy] = source
    return aliases


def unique_sources(
    git: GitClient, previous: tuple[str, ...], candidates: tuple[str, ...], aliases: dict[str, str]
) -> list[str]:
    """Exclude copies of sources already represented by earlier bases.

    Historical patch equivalence alone is insufficient: another base may
    have independently applied the same patch and then reverted it.
    """
    candidate_origins = {
        commit: source
        for commit in candidates
        if (source := aliases.get(commit)) and unchanged_patch(git, source, commit)
    }
    origins = {candidate_origins.get(commit, commit) for commit in candidates}
    represented = {
        source
        for copy, source in aliases.items()
        if source in origins
        and any(git.is_ancestor(copy, tip) for tip in previous)
        and unchanged_patch(git, source, copy)
    }
    return [
        commit
        for commit in candidates
        if candidate_origins.get(commit, commit) not in represented
        and not (
            commit in candidate_origins and any(git.is_ancestor(candidate_origins[commit], tip) for tip in previous)
        )
    ]


def advance(git: GitClient, replay: Replay, tip: str) -> Replay:
    if not replay.secondary and git.tree(replay.head) == git.tree(tip):
        source = replay.own[0]
        if git.tree(source) != git.tree(f"{source}^"):
            raise EmptyOwnCommit(f"own commit {source[:12]} became empty; replay was discarded and the branch was kept")
    copies = replay.copies
    if replay.secondary and tip != replay.head:
        source = replay.secondary[0]
        if source != tip and unchanged_patch(git, source, tip):
            origin = copy_origins(git).get(source)
            if origin is None or not unchanged_patch(git, origin, source):
                origin = source
            copies += (tip, origin)
    return replace(
        replay,
        head=tip,
        secondary=replay.secondary[1:] if replay.secondary else (),
        own=replay.own if replay.secondary else replay.own[1:],
        in_stage=False,
        copies=copies,
    )


def unchanged_patch(git: GitClient, source: str, result: str) -> bool:
    """Compare the resulting tree with applying the source change verbatim."""
    if git.rev(source) is None or git.rev(result) is None:
        return False
    return git.is_rebased_copy(f"{source}^", source, f"{result}^", result)


def publish(git: GitClient, replay: Replay) -> None:
    require_direct_target(git, replay.branch)
    ref = f"refs/heads/{replay.branch}"
    current = git.run("rev-parse", "--verify", ref)
    if replay.was_published(git):
        if not git.is_ancestor(replay.head, current):
            raise StackError(
                f"{replay.branch} moved after publication; restore its tip {replay.head} to finish recovery"
            )
        return
    if git.other_worktree_of(replay.branch) is not None or git.current_branch() is not None:
        raise StackError(f"cannot publish replay of {replay.branch} while it is checked out")
    inputs = "".join(
        f"verify refs/heads/{parent} {tip}\n" for parent, tip in zip(replay.parents, replay.tips, strict=True)
    )
    git.run(
        "update-ref",
        "-m",
        "git-stack: replay bases",
        "--stdin",
        # Never overwrite another branch by following a symbolic ref.
        input=(
            f"option no-deref\nupdate {ref} {replay.head} {replay.before}\n"
            f"create {replay.refs}published {replay.head}\n{inputs}"
        ),
    )


def resume_stage(git: GitClient, job: Replay) -> Replay | None:
    """Read the temporary branch that Git updates when its rebase finishes."""
    source = (job.secondary or job.own)[0]
    tip = git.rev(f"refs/heads/{job.scratch}")
    if tip == source:
        # An abort leaves the scratch branch at the source. A successful
        # stage with the original parent is handled directly without a rebase.
        return None
    if tip is None or (tip != job.head and git.run("show", "-s", "--format=%P", tip) != job.head):
        raise StackError(f"the temporary branch for replaying {job.branch} moved unexpectedly")
    job = advance(git, job, tip)
    job.save(git)
    return job


def start_stage(git: GitClient, job: Replay, source: str) -> Replay:
    # With no change of parent, keep the existing commit. In all other
    # cases, the scratch tip identifies completion even without a reflog.
    if git.rev(f"{source}^") == job.head:
        job = advance(git, job, source)
        job.save(git)
        return job

    git.run("switch", "--quiet", "--detach", job.head)
    # Keep the completed history reachable when the scratch branch is reset
    # to the next source and Git takes over the checkout.
    git.run("update-ref", job.refs + "head", job.head)
    git.run("branch", "--force", "--no-track", job.scratch, source)
    job = replace(job, in_stage=True)
    job.save(git)
    # Explicitly replay the one planned commit. The queue may be sparse
    # because an earlier base can already carry some of the source commits.
    if git.passthrough(
        "rebase",
        "--merge",
        "--no-update-refs",
        "--no-autostash",
        "--no-autosquash",
        "--no-rebase-merges",
        "--no-fork-point",
        "--reapply-cherry-picks",
        "--keep-empty",
        "--empty=drop" if job.secondary else "--empty=ask",
        "--onto",
        job.head,
        f"{source}^",
        job.scratch,
    ):
        if (result := resume_stage(git, job)) is not None:
            return result
        raise StackError(f"rebasing {source[:12]} for {job.branch} did not move the temporary branch")
    if git.operation_in_progress():
        raise stopped_stage(git, job, source)
    if git.rev(f"refs/heads/{job.scratch}") == source:
        # Git returned without starting a rebase. Retry this stage instead of
        # mistaking the unchanged scratch tip for a later user abort.
        replace(job, in_stage=False).save(git)
    raise StackError(
        f"could not rebase {source[:12]} for {job.branch}; see Git's output, "
        f"then rerun `git stack {job.command}` in this worktree"
    )


def run(git: GitClient, job: Replay) -> Replay | None:
    if str(git.toplevel()) != job.worktree:
        raise StackError(f"resume the pending replay in {job.worktree}")
    if job.method != "rebase":
        raise StackError("this replay started with cherry-pick; finish it with the git-stack version that started it")
    if job.in_stage:
        pending = resume_stage(git, job)
        if pending is None:
            return None
        job = pending
    if job.ready:
        return job
    while True:
        if not job.secondary and not job.boundary:
            job = replace(job, boundary=job.head)
            job.save(git)
        queue = job.secondary or job.own
        if not queue:
            job = replace(job, ready=True)
            job.save(git)
            return job
        job = start_stage(git, job, queue[0])


def stopped_stage(git: GitClient, job: Replay, commit: str) -> StackError:
    if git.has_conflicts():
        instructions = (
            "Resolve the conflicts, stage the resolved files, and run `git rebase --continue`, "
            "or run `git rebase --abort`."
        )
    else:
        instructions = "Address the Git error above, then run `git rebase --continue` or `git rebase --abort`."
        if not job.secondary:
            instructions += " If Git reports an empty own commit, abort to preserve its change."
    return StackError(
        f"replaying {job.branch} stopped at {commit[:12]}. {instructions} "
        f"Then rerun `git stack {job.command}` with the same arguments in this worktree "
        "before switching branches."
    )


def finish(
    git: GitClient,
    job: Replay,
    record: Callable[[Replay], None],
    restore_checkout: Callable[[str], None],
) -> bool:
    """Publish the replay, then record its metadata before clearing the journal.

    A failure after the ref transaction must keep the journal and its receipt
    so the same metadata can be recorded on the next run.
    """
    try:
        result = run(git, job)
    except EmptyOwnCommit:
        restore_checkout(job.original)
        job.clear(git)
        raise
    if result is not None:
        if not result.was_published(git):
            git.run("switch", "--quiet", "--detach", result.head)
        try:
            publish(git, result)
        except StackError:
            if not result.was_published(git):
                restore_checkout(result.original)
                result.clear(git)
            raise
        record(result)
    restore_checkout(job.original)
    job.clear(git)
    return result is not None
