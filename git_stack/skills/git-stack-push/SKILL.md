---
name: git-stack-push
description: Publish local stacked branches and their pull requests with git stack push. Use to publish a new stack, update remotes after a local restack, or diagnose a refused push.
---

# Publish a stack

Read `git-stack` first for selection, ownership and timing. Check
`git stack info --all --offline` and publish only the intended stack. Push
uses leases, sets upstreams and updates pull request bases.

Follow the queued and merge-ready PR checks in `git-stack` before pushing.
Preserve queued PRs and warn before any update that could cost a merge-ready
PR its existing approvals, even when its base stays the same.

Before pushing a rearranged stack or updating its PRs, verify the bases and
merge queue status of all affected PRs and confirm that the user's approval
covers publication and any retargeting. Follow the approval rules in
`git-stack`, including for drafts.

- Use `git stack push` for the current stack or `git stack push <branch>` for a
  named stack. Pass `-g` to include its group. Use `--remote <name>` to
  select one remote when tracked branches use several.
- Use `git stack push --prs` to publish and open missing draft pull requests.
  Use `git stack prs` to open or update PRs for branches already published.
- With work stacked on an integration root, start from a clean worktree.
  Push rebuilds the integration from local tips, restacks the selected work,
  and publishes both. Use `git-stack-integrate` for a stopped merge and
  `git-stack-restack` for a stopped rebase. Resume the original push after
  resolving the operation.

If a lease fails or the upstream has commits the stack hasn't pulled,
inspect what changed. Use `git-stack-pull` to take upstream work locally or
`git-stack-sync` to update and publish. A remote branch with the same name
that isn't your branch's upstream may belong to someone else. Verify its
ownership before setting it as the upstream.

Ask before using `--force` to replace commits or write to a protected
destination, `--allow-shared` to publish shared work, or `--rebuild-stack`
to unstack and rebuild a GitHub stack. Forced stack pushes still use leases.
