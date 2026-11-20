---
name: git-stack-sync
description: Fetch, update and publish stacked branches with git stack sync. Use after a pull request in the stack merges, its root moves, or someone pushes to a branch and the updated stack should be published.
---

# Sync a stack

Read `git-stack` first for selection, ownership and timing. Start with a clean
worktree. Sync fetches, fast-forwards the root, untracks merged branches,
restacks the survivors, incorporates upstream commits, and pushes changed
branches that already track a remote.

Follow the queued and merge-ready PR checks in `git-stack` before syncing.
Preserve queued PRs and warn before any update that could cost a merge-ready
PR its existing approvals, even when its base stays the same.

Before syncing a rearranged stack, verify the affected PR bases and merge
queue status. Confirm the user approved publishing those changes in the full
selected scope, including any stacks selected by group. Follow the approval
rules in `git-stack`.

- Use `git stack sync` for the current stack or `git stack sync <branch>` for a
  named stack. Pass `-g` for its group or `-g <group>` for a named group. Use
  `--remote <name>` if the selected branches track different remotes.
- A branch directly on an ordinary root moves when it conflicts with the
  updated root. Use `git stack sync --latest` to move it either way.
- Use `git stack sync -d local` to delete merged local branches as they are
  untracked. Ask before `-d remote` or `-d both`. For housekeeping across all
  stacks without restacking, use `git stack clean`.
- Sync refreshes selected integration roots and publishes them with eligible
  work. See `git-stack-integrate` for integration merge conflicts.

If sync stops on a rebase or cherry-pick conflict, no branches are pushed.
Use the recovery steps in `git-stack-restack` or `git-stack-pull` for the
operation shown by `git status`. Then rerun the original sync with the same
selectors and options. Check the result with `git stack info --all --offline`.

Ask before overriding protections with `--force`, `--allow-shared`,
`--rewritten-upstream`, `--pull-tracked-upstream` or `--ignore-revert`.
See `git-stack-pull` for upstream history and revert warnings. A branch
checked out elsewhere can be skipped while sync publishes other branches.
Coordinate with whoever is using that worktree before retrying.

Use `git-stack-pull` to fetch and update locally, `git-stack-restack` to use
local refs, and `git-stack-push` to publish a branch for the first time or open
its draft PR.
