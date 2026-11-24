---
name: git-stack-sync
description: Fetch, update and publish stacked branches with git stack sync. Use after a pull request in the stack merges, its root moves, or someone pushes to a branch and the updated stack should be published.
---

# Sync a stack

Read `git-stack` first for selection, ownership and timing. Start with a clean
worktree. Sync fetches, fast-forwards the root, untracks merged branches,
restacks the survivors, incorporates upstream commits, and pushes changed
branches that already track a remote.

Before syncing, follow the open-PR checks in `git-stack` for the full scope.
Preserve queued PRs and name each approved PR that could get new commits or a
new base. Warn even if required checks are pending. Sync can retarget a PR
whose head stays the same, for example after its parent merges.

Before syncing a rearranged stack, verify the affected PR bases and merge
queue status. Confirm the user approved publishing those changes in the full
selected scope, including any stacks selected by group. Follow the approval
rules in `git-stack`.

- Use `git stack sync --protect-prs` for the current stack or `git stack sync --protect-prs <branch>` for a
  named stack. Pass `-g` for its group or `-g <group>` for a named group. Use
  `--remote <name>` if the selected branches track different remotes.
- If sync requires a remote PR stack rebuild, show the user the reported
  changes and get approval before retrying with `--pr-rebuild`. Follow
  `git-stack` for approval to close any listed drafts.
- A branch directly on an ordinary root moves when it conflicts with the
  updated root. Use `git stack sync --protect-prs --latest` when the user wants it rebased
  even if it applies cleanly. This can rewrite an already approved PR.
- Use `git stack sync --protect-prs -d local` to delete merged local branches as they are
  untracked. Ask before `-d remote` or `-d both`. For housekeeping across all
  stacks without restacking, use `git stack clean`.
- Sync refreshes selected integration roots and publishes them with eligible
  work. See `git-stack-integrate` for integration merge conflicts.

If sync stops on a rebase or cherry-pick conflict, use `git-stack-restack`
or `git-stack-pull` for the operation shown by `git status`. If publication
fails, inspect live branch tips and PR bases for changes already made.
Recheck queue status and approvals before retrying.

Ask before overriding protections with `--force`, `--allow-shared`,
`--rewritten-upstream`, `--pull-tracked-upstream` or `--ignore-revert`.
See `git-stack-pull` for upstream history and revert warnings. A branch
checked out elsewhere can be skipped while sync publishes other branches.
Coordinate with whoever is using that worktree before retrying.

Use `git-stack-pull` to fetch and update locally, `git-stack-restack` to use
local refs, and `git-stack-push` to publish a branch for the first time or open
its draft PR.
