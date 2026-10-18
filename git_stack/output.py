"""Turns events into the messages the CLI prints."""

import sys

from git_stack import events


def _untracked(e):
    moved = f" and moved {', '.join(e.children)} onto {e.parent}" if e.children else ""
    if e.deleted_tip is None:
        return f"{e.branch} was merged into {e.parent}; stopped tracking it{moved}"
    return f"{e.branch} was merged into {e.parent}; deleted it (was {e.deleted_tip[:12]}){moved}"


MESSAGES = {
    events.Squashing: lambda e: f"squashing {e.count} commits on {e.branch}",
    events.Restacking: lambda e: f"restacking {e.branch} onto {e.parent}",
    events.PullingUpstream: lambda e: f"pulling {e.count} new commit(s) from {e.branch}'s upstream",
    events.FastForwarded: lambda e: f"fast-forwarded {e.branch} to {e.upstream}",
    events.TrunkDiverged: lambda e: f"{e.branch} has diverged from {e.upstream}; not updating it",
    events.SkippedMerged: lambda e: (
        f"{e.branch} looks merged into {e.parent}; skipping it and moving its children onto "
        f"{e.parent}. Run `git stack sync` to remove it from the stack"),
    events.Untracked: _untracked,
    events.RemoteBranchDeleted: lambda e: f"deleted {e.upstream}",
    events.RemoteBranchDeleteFailed: lambda e: f"couldn't delete {e.upstream}; leaving {e.branch} alone",
    events.RemoteBranchKept: lambda e: (
        f"note: {e.upstream} still exists; delete it with `git push {e.remote} --delete {e.name}`, "
        f"or pass `--delete remote`"),
    events.ParentSkipped: lambda e: f"skipping {e.branch} because its parent {e.parent} was skipped",
    events.MergedWithNewCommits: lambda e: (
        f"skipping {e.branch}: it was merged into {e.parent}, but it or its remote branch has "
        f"commits that {e.parent} doesn't"),
    events.UpstreamGone: lambda e: (
        f"{e.branch}'s remote branch is gone but its changes aren't in {e.parent}; leaving it alone"),
    events.BrokenBranch: lambda e: f"skipping {e.branch}: {e.reason}",
    events.ParentMissing: lambda e: f"skipping {e.branch}: its parent {e.parent} doesn't exist",
    events.PullRequestLookupFailed: lambda e: (
        f"couldn't look up pull requests on GitHub, so only checking for merges locally: {e.error}"),
    events.PullRequestFound: lambda e: f"found pull request #{e.number} for {e.branch}",
    events.PullRequestOpened: lambda e: f"opened draft pull request #{e.number} for {e.branch}: {e.url}",
    events.PullRequestRetargeted: lambda e: (
        f"changed the base of {e.branch}'s pull request #{e.number} to {e.base}"),
}

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


def print_event(event):
    message = MESSAGES[type(event)](event)
    if isinstance(event, WARNINGS):
        print(f"warning: {message}", file=sys.stderr)
    else:
        print(message)
