---
name: git-stack-integrate
description: Build or adopt a git-stack integration root from several local branches, refresh work above it, recover an interrupted build, or promote the work after its inputs land.
---

# Integration branches

When `stack.perfLog` is set, use that log to report total wall time,
how many Git and `gh` calls ran, the time spent in each tool, and the time spent elsewhere.
Count each `cat-file` batch request as a Git call. Include batch shutdown in Git time only.
Report any overlap shown and flag the slowest call if it explains a delay.

Use an integration root when one stack needs several unmerged branch tips.
It combines the tips with native Git merges and serves as the work stack's
single parent. Check the selected stacks and source branches first with
`git stack info --all --offline`.

- Create with `git stack integrate <name> <source-a> <source-b>`. Source
  names are local branches in merge order. The build starts at `main` by
  default. Use `git stack integrate <name> <source-a> <source-b> --root <root>`
  for a different ordinary untracked root.
- Register an existing snapshot with
  `git stack integrate --adopt <name> <source-a> <source-b>`. Adoption keeps
  the current tip until the next refresh. Rerun `git stack integrate <name> <source>`
  to replace the source list of an existing integration. The new list takes
  effect after a successful build.
- Start the work with `git stack create <work> --from <name>`. Commit work
  on the work branches. An integration root and the branches stacked on it
  cannot supply inputs to an integration.

## Refresh and publish

- `git stack restack <name>` refreshes from local refs. `git stack pull <name>`
  fetches and refreshes locally. `git stack sync <name>` also publishes the
  integration and eligible work in one atomic push with leases. If another
  worktree blocks a selected work branch, sync stops before publishing its base.
- From the work branch, `git stack push` rebuilds the integration from local
  tips, restacks the selected work, and publishes both atomically. To review
  that rebase before publishing, run `git stack restack <name>` first.
  Use `git stack push --prs` to publish both and open draft PRs for the work.
  Standalone `git stack prs` requires the integration to be on the remote.
- A typical edit is to commit on a source, run `git stack restack <name>`,
  switch to the work branch, commit the work, and run `git stack push`.
- Naming the integration selects its dependent stacks. Use a group selector
  to include source stacks that also need restacking or syncing. Excluded
  sources contribute their local tips.
- A source drops out when its exact included tip is proven to have landed on
  the configured root. If a source is missing and there is no proof, inspect
  it and confirm the replacement inputs before changing the source list.
  `git stack info` marks landed sources in cyan and marks an integration that
  needs refreshing as needing a restack.

## Conflicts between sources

Keep independent branches parallel when they merge cleanly. When two source
branches conflict during a build or refresh, recommend stacking them, with
the prerequisite below the dependent work. Resolve the conflict during
`git stack restack <upper> --onto <lower>` once both branches are tracked.
The upper branch then carries the resolution into later builds. Keep both
source names when first creating an integration with only those two inputs.
For an existing integration, the upper branch can replace both source names
because its history includes the lower branch.

If the user chooses to rearrange the sources during a stopped build, preserve
any resolution they want to keep before aborting the build. Follow the
`git-stack-rearrange` skill for the branch changes.

## Recover or promote

- If a merge conflicts, use the temporary worktree path reported by git-stack.
  Resolve and stage the conflicts there, run
  `git -C "<path>" -c core.editor=true merge --continue`, then rerun the
  original command with the same selectors. The retry keeps surviving work
  in the source stack selected even if sync removed the branch you named.
  Keep that work selected when retrying a queued remote branch deletion.
  To discard the build, use
  `git stack integrate --abort <name>` from the regular worktree.
- If the integration tip has direct commits, inspect the commits listed by
  git-stack. `git stack restack <name> --force` replaces that integration's
  tip and removes those commits from it. Get authorization for that rewrite.
- To promote the work after its inputs land on `main`, run
  `git stack restack <work> --onto main` with the bottom work branch, then
  `git stack push <work>` to update the work's PRs. Check that no stack or PR
  uses the integration before deleting its refs with the user's approval.
