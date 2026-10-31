"""Turns events into the messages the CLI prints, and events and results into
the JSON --json prints."""

import json
import sys
from typing import Any, assert_never

from git_stack import events
from git_stack.json_fields import to_json

WARNINGS = (
    events.TrunkDiverged,
    events.TrunkInOtherWorktree,
    events.NotRestackedInOtherWorktree,
    events.NotSquashedInOtherWorktree,
    events.NotPulledInOtherWorktree,
    events.NotDeletedInOtherWorktree,
    events.SkippedMerged,
    events.RemoteBranchDeleteFailed,
    events.ParentSkipped,
    events.MergedWithNewCommits,
    events.Reverted,
    events.UpstreamGone,
    events.BrokenBranch,
    events.ParentMissing,
    events.PullRequestLookupFailed,
    events.GitHubStackMismatch,
)


# Complex only because it has a case for every event, which reads best in one place.
def message(event: events.Event) -> str:  # noqa: C901
    match event:
        case events.WaitingForLock():
            return "waiting for another git stack command in this repository to finish"
        case events.Fetching():
            return f"fetching {event.remote}, and pruning its deleted branches"
        case events.LookingUpPullRequests(author=None):
            return "looking up your open pull requests on GitHub"
        case events.LookingUpPullRequests(author=author):
            return f"looking up {author}'s open pull requests on GitHub"
        case events.Squashing():
            return f"squashing {event.count} commits on {event.branch}"
        case events.Restacking():
            return f"restacking {event.branch} onto {event.parent}"
        case events.PullingUpstream():
            return f"pulling {event.count} new commit(s) from {event.branch}'s upstream"
        case events.TookUpstreamRebase():
            return f"moved {event.branch} to {event.upstream}, which has its commits rebased"
        case events.FastForwarded():
            return f"fast-forwarded {event.branch} to {event.upstream}"
        case events.TrunkDiverged():
            return f"{event.branch} has diverged from {event.upstream}; not updating it"
        case events.TrunkInOtherWorktree():
            return (
                f"{event.branch} is checked out in {event.path}; not fast-forwarding it to "
                f"{event.upstream}. Run `git -C {event.path} merge --ff-only` to update it"
            )
        case events.NotRestackedInOtherWorktree():
            return (
                f"skipping {event.branch}: it's checked out in {event.path}, so it can't be "
                f"restacked here. Switch that worktree to another branch, then rerun this command"
            )
        case events.NotSquashedInOtherWorktree():
            return f"not squashing {event.branch}: it's checked out in {event.path}"
        case events.NotPulledInOtherWorktree():
            return (
                f"not pulling new commits from {event.branch}'s upstream: it's checked out in "
                f"{event.path}. Run `git stack sync` there, or switch that worktree to another "
                f"branch and rerun it here"
            )
        case events.NotDeletedInOtherWorktree():
            return (
                f"not deleting {event.branch}: it's checked out in {event.path}. Delete it with "
                f"`git branch -D {event.branch}` once that worktree is on another branch"
            )
        case events.SkippedMerged():
            return (
                f"{event.branch} looks merged into {event.parent}; skipping it and moving its "
                f"children onto {event.parent}. Run `git stack sync` to remove it from the stack"
            )
        case events.Untracked():
            moved = f" and moved {', '.join(event.children)} onto {event.parent}" if event.children else ""
            if event.deleted_tip is None:
                return f"{event.branch} was merged into {event.parent}; stopped tracking it{moved}"
            return f"{event.branch} was merged into {event.parent}; deleted it (was {event.deleted_tip[:12]}){moved}"
        case events.RemoteBranchDeleted():
            return f"deleted {event.upstream}"
        case events.RemoteBranchDeleteFailed():
            return f"couldn't delete {event.upstream}; leaving {event.branch} alone"
        case events.RemoteBranchKept():
            return (
                f"note: {event.upstream} still exists; delete it with "
                f"`git push {event.remote} --delete {event.name}`, or pass `--delete remote`"
            )
        case events.ParentSkipped():
            return f"skipping {event.branch} because its parent {event.parent} was skipped"
        case events.MergedWithNewCommits():
            return (
                f"skipping {event.branch}: it was merged into {event.parent}, but it or its "
                f"remote branch has commits that {event.parent} doesn't"
            )
        case events.Reverted():
            return (
                f"skipping {event.branch}: it was merged into {event.parent}, then {event.parent} reverted "
                f'some of its changes; see "A merged branch was reverted" in `git stack --help`, or if '
                f"that wasn't a revert, run `git stack clean --ignore-revert {event.branch}`"
            )
        case events.UpstreamGone():
            return f"{event.branch}'s remote branch is gone but its changes aren't in {event.parent}; leaving it alone"
        case events.BrokenBranch():
            return f"skipping {event.branch}: {event.reason}"
        case events.ParentMissing():
            return f"skipping {event.branch}: its parent {event.parent} doesn't exist"
        case events.PullRequestLookupFailed():
            return f"couldn't look up pull requests on GitHub, so only checking for merges locally: {event.error}"
        case events.NoPullRequests():
            have = "has" if len(event.branches) == 1 else "have"
            return f"{', '.join(event.branches)} {have} no pull request; open drafts with `git stack prs`"
        case events.GitHubStackLinked():
            return f"linked {pull_requests(event.numbers)} into a stack on GitHub"
        case events.GitHubStackUnstacked():
            return (
                f"unstacked stack {event.stack} on GitHub, which doesn't change the base of a pull request "
                f"in a stack, to change #{event.number}'s; `git stack prs` links them again"
            )
        case events.GitHubStackMismatch():
            stacks = ", ".join(map(str, event.github_stacks))
            return (
                f"didn't link {pull_requests(event.numbers)} into a stack on GitHub: some are in "
                f"{'stacks' if len(event.github_stacks) > 1 else 'stack'} {stacks} there, which don't match "
                f"them, and pull requests can't be taken out of a stack"
            )
        case events.PullRequestRetargeted():
            return f"changed the base of {event.branch}'s pull request #{event.number} to {event.base}"
        case _:
            assert_never(event)


def pull_requests(numbers: tuple[int, ...]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def event_json(event: events.Event) -> dict[str, Any]:
    """event as --json prints it: its type, the message the CLI would print,
    whether that's a warning, and its fields."""
    return {
        "type": event.event_type,
        "message": message(event),
        "warning": isinstance(event, WARNINGS),
        **to_json(event),
    }


def print_event(event: events.Event) -> None:
    if isinstance(event, WARNINGS):
        print(f"warning: {message(event)}", file=sys.stderr)
    else:
        print(message(event))


class JsonStream:
    """What --json prints: a JSON object per line, as a command goes. Each
    event (see event_json), then {"type": "result", ...} with the command's
    result (see to_json), or {"type": "error", "message": ...} if it fails.
    The last line also has "events", every event again, so it alone says
    everything that happened."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def event(self, event: events.Event) -> None:
        self.events.append(event_json(event))
        self._print(self.events[-1])

    def result(self, result: Any) -> None:
        self._print({"type": "result", **to_json(result), "events": self.events})

    def error(self, message: str) -> None:
        self._print({"type": "error", "message": message, "events": self.events})

    def _print(self, record: dict[str, Any]) -> None:
        # Flushed, so a tool reading the stream sees each line as it happens.
        print(json.dumps(record), flush=True)
