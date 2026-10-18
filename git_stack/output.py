"""Turns events into the messages the CLI prints."""

import sys
from typing import assert_never

from git_stack import events

WARNINGS = (
    events.TrunkDiverged,
    events.SkippedMerged,
    events.RemoteBranchDeleteFailed,
    events.ParentSkipped,
    events.MergedWithNewCommits,
    events.UpstreamGone,
    events.BrokenBranch,
    events.ParentMissing,
    events.PullRequestLookupFailed,
)


def message(event: events.Event) -> str:
    match event:
        case events.Squashing():
            return f"squashing {event.count} commits on {event.branch}"
        case events.Restacking():
            return f"restacking {event.branch} onto {event.parent}"
        case events.PullingUpstream():
            return f"pulling {event.count} new commit(s) from {event.branch}'s upstream"
        case events.FastForwarded():
            return f"fast-forwarded {event.branch} to {event.upstream}"
        case events.TrunkDiverged():
            return f"{event.branch} has diverged from {event.upstream}; not updating it"
        case events.SkippedMerged():
            return (f"{event.branch} looks merged into {event.parent}; skipping it and moving its "
                    f"children onto {event.parent}. Run `git stack sync` to remove it from the stack")
        case events.Untracked():
            moved = f" and moved {', '.join(event.children)} onto {event.parent}" if event.children else ""
            if event.deleted_tip is None:
                return f"{event.branch} was merged into {event.parent}; stopped tracking it{moved}"
            return (f"{event.branch} was merged into {event.parent}; "
                    f"deleted it (was {event.deleted_tip[:12]}){moved}")
        case events.RemoteBranchDeleted():
            return f"deleted {event.upstream}"
        case events.RemoteBranchDeleteFailed():
            return f"couldn't delete {event.upstream}; leaving {event.branch} alone"
        case events.RemoteBranchKept():
            return (f"note: {event.upstream} still exists; delete it with "
                    f"`git push {event.remote} --delete {event.name}`, or pass `--delete remote`")
        case events.ParentSkipped():
            return f"skipping {event.branch} because its parent {event.parent} was skipped"
        case events.MergedWithNewCommits():
            return (f"skipping {event.branch}: it was merged into {event.parent}, but it or its "
                    f"remote branch has commits that {event.parent} doesn't")
        case events.UpstreamGone():
            return (f"{event.branch}'s remote branch is gone but its changes aren't in "
                    f"{event.parent}; leaving it alone")
        case events.BrokenBranch():
            return f"skipping {event.branch}: {event.reason}"
        case events.ParentMissing():
            return f"skipping {event.branch}: its parent {event.parent} doesn't exist"
        case events.PullRequestLookupFailed():
            return f"couldn't look up pull requests on GitHub, so only checking for merges locally: {event.error}"
        case events.PullRequestOpened():
            return f"opened draft pull request #{event.number} for {event.branch}: {event.url}"
        case events.PullRequestRetargeted():
            return f"changed the base of {event.branch}'s pull request #{event.number} to {event.base}"
        case _:
            assert_never(event)


def print_event(event: events.Event) -> None:
    if isinstance(event, WARNINGS):
        print(f"warning: {message(event)}", file=sys.stderr)
    else:
        print(message(event))
