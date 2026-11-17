"""Build a combined base on detached HEAD and publish it after all picks succeed."""

import json
from dataclasses import asdict, dataclass, replace
from typing import cast
from uuid import uuid4

from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id

PENDING = "stack.pendingReplay"
TIPS = "stackReplayTips"
PIN_PREFIX = "refs/git-stack/replay/"


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
    in_pick: bool = False
    ready: bool = False
    remaining: tuple[str, ...] = ()
    latest: bool = False
    token: str = ""

    @property
    def refs(self) -> str:
        return PIN_PREFIX + self.token + "/"

    def was_published(self, git: GitClient) -> bool:
        return git.rev(self.refs + "published") == self.head

    def save(self, git: GitClient) -> None:
        git.write_config(PENDING, json.dumps(asdict(self), ensure_ascii=True))

    def clear(self, git: GitClient) -> None:
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
        value = fields[key]
        if not isinstance(value, list):
            raise TypeError(key)
        values = cast(list[object], value)
        if any(not isinstance(item, str) for item in values):
            raise TypeError(key)
        return tuple(str(item) for item in values)

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
        boolean("in_pick"),
        boolean("ready"),
        strings("remaining"),
        boolean("latest"),
        string("token"),
    )
    validate_pending(git, job)
    return job


def validate_pending(git: GitClient, job: Replay) -> None:
    if len(job.token) != 32 or not all(c in "0123456789abcdef" for c in job.token):
        raise ValueError("invalid replay token")
    for branch in (job.branch, *job.parents, *job.remaining):
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


def plan(
    git: GitClient,
    branch: str,
    parents: tuple[str, ...],
    base: str,
    original: str,
    remaining: tuple[str, ...] = (),
    latest: bool = False,
) -> Replay:
    tips = tuple(git.tip(parent) for parent in parents)
    secondary: list[str] = []
    for i, (parent, tip) in enumerate(zip(parents, tips, strict=True)):
        if i == 0:
            continue
        if not any(git.ok("merge-base", tip, previous) for previous in tips[:i]):
            raise StackError(f"{parent} has no shared history with the preceding bases of {branch}")
        secondary.extend(commits(git, tip, tips[:i], parent))
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
    )


def advance(replay: Replay, picked_tip: str) -> Replay:
    return replace(
        replay,
        head=picked_tip,
        secondary=replay.secondary[1:] if replay.secondary else (),
        own=replay.own if replay.secondary else replay.own[1:],
        in_pick=False,
    )


def continued(git: GitClient, replay: Replay, head: str) -> bool:
    commit = (replay.secondary or replay.own)[0]
    return (
        git.run("show", "-s", "--format=%P", head) == replay.head
        and (
            not git.ok("reflog", "exists", "HEAD")
            or git.run("log", "-1", "--format=%gs", "-g", "HEAD").startswith("commit (cherry-pick):")
        )
        and git.identity_counts("-1", head) == git.identity_counts("-1", commit)
    )


def pick(git: GitClient, commit: str, secondary: bool) -> bool:
    if git.passthrough("cherry-pick", "--allow-empty", commit):
        return True
    # Git versions before 2.45 lack --empty=drop. Only skip when Git's pick
    # produced an empty change, with no unresolved paths or staged amendments.
    return (
        git.rev("CHERRY_PICK_HEAD") == commit
        and not git.run("status", "--porcelain", "--untracked-files=no")
        and skip_empty(git, commit, secondary)
    )


def skip_empty(git: GitClient, commit: str, secondary: bool) -> bool:
    if not secondary:
        raise StackError(
            f"own commit {commit[:12]} is already supplied by a base. "
            "Run `git cherry-pick --abort` and rerun restack before changing the dependencies."
        )
    return git.passthrough("cherry-pick", "--skip")


def publish(git: GitClient, replay: Replay) -> None:
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
        input=f"update {ref} {replay.head} {replay.before}\ncreate {replay.refs}published {replay.head}\n{inputs}",
    )


def prepare(git: GitClient, replay: Replay) -> Replay | None:
    """Finish the worktree phases. Return None after an aborted pick."""
    head = git.rev("HEAD")
    if str(git.toplevel()) != replay.worktree:
        raise StackError(f"resume the pending replay in {replay.worktree}")
    if replay.ready:
        return replay
    if replay.in_pick:
        if git.current_branch() is not None:
            raise StackError("the pending replay's detached checkout has changed")
        if head == replay.head:
            return None
        if head is None or not continued(git, replay, head):
            raise StackError("HEAD does not match the pending replay's cherry-pick")
        replay = advance(replay, head)
        replay.save(git)
    if git.current_branch() is not None or head != replay.head:
        if replay.head != replay.tips[0] or (git.current_branch() or head) != replay.original:
            raise StackError("HEAD does not match the pending replay")
        git.run("switch", "--quiet", "--detach", replay.head)
    return replay


def run(git: GitClient, replay: Replay) -> Replay | None:
    prepared = prepare(git, replay)
    if prepared is None or prepared.ready:
        return prepared
    replay = prepared
    while True:
        if not replay.secondary and not replay.boundary:
            replay = replace(replay, boundary=replay.head)
            replay.save(git)
        queue = replay.secondary or replay.own
        if not queue:
            replay = replace(replay, ready=True)
            replay.save(git)
            return replay
        commit = queue[0]
        replay = replace(replay, in_pick=True)
        replay.save(git)
        if not pick(git, commit, bool(replay.secondary)):
            if git.operation_in_progress():
                raise StackError(
                    f"replaying {replay.branch} stopped at {commit[:12]}. Resolve the conflict and run "
                    "`git cherry-pick --continue`, or run `git cherry-pick --abort`, then rerun the stack command"
                )
            raise StackError(f"could not apply {commit[:12]} while replaying {replay.branch}")
        replay = advance(replay, git.run("rev-parse", "HEAD"))
        replay.save(git)
