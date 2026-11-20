---
name: git-stack-integrate
description: Build or adopt a git-stack integration root from local branches, create or refresh a group integration, refresh work above it, recover an interrupted build, or promote the work after its inputs land.
---

# Integration branches

When `stack.perfLog` is set, use that log to report total wall time,
how many Git and `gh` calls ran, the time spent in each tool, and the time spent elsewhere.
Count each `cat-file` batch request as a Git call. Include batch shutdown in Git time only.
Report any overlap shown and flag the slowest call if it explains a delay.

Use an integration root when one stack needs several unmerged branch tips.
It merges source tips and serves as the work stack's single parent. A refresh
can cherry-pick appended commits when the source histories are independent.
Check the selected stacks and source branches first with
`git stack info --all --offline`.

- Create with `git stack integrate <name> <source-a> <source-b>`. Source
  names are local branches in application order. The build starts at `main` by
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

## Group integration

From a stack in the group, run `git stack integrate <name> -g` to create a
group integration. It follows the leaves of the group's stacks on the remote
default branch. The next refresh picks up new leaves automatically.

## Refresh and publish

- `git stack restack <name>` refreshes from local refs. `git stack pull <name>`
  fetches and refreshes locally. `git stack sync --protect-prs <name>` also publishes the
  integration and eligible work in one atomic push with leases. If another
  worktree blocks a selected work branch, sync stops before publishing its base.
- From the work branch, `git stack push --protect-prs` rebuilds the integration from local
  tips, restacks the selected work, and publishes both atomically. To review
  that rebase before publishing, run `git stack restack <name>` first.
  Use `git stack push --protect-prs --prs` to publish both and open draft PRs for the work.
  Standalone `git stack prs --protect-prs` requires the integration to be on the remote.
- A typical edit is to commit on a source, run `git stack restack <name>`,
  switch to the work branch, commit the work, and run `git stack push --protect-prs`.
- Restacking the integration or a branch above it includes the tracked
  source branches and their ancestors. Check those branches for shared work
  and queued PRs before restacking. With pull or sync, use a group selector
  to include source stacks that need updating. Other sources contribute
  their local tips.
- A source drops out when its exact included tip is proven to have landed on
  the configured root. If a source is missing and there is no proof, inspect
  it and confirm the replacement inputs before changing the source list.
  `git stack info` marks landed sources in cyan and marks an integration that
  needs refreshing as needing a restack.

## Conflicts between sources

A merge or cherry-pick conflict aborts the build and keeps the previous
integration tip. An existing integration can reuse its worktree after a
clean abort. Keep independent branches parallel when they merge cleanly. When two source
branches conflict during a build or refresh, recommend stacking them, with
the prerequisite below the dependent work. Follow `git-stack-rearrange`
and get the user's approval before resolving the conflict with
`git stack restack <upper> --onto <lower>`.
The upper branch then carries the resolution into later builds. Keep both
source names when first creating an integration with only those two inputs.
For an existing integration, the upper branch can replace both source names
because its history includes the lower branch.

Rerun the original command with its selectors after restacking. Follow the
`git-stack-rearrange` skill for the branch changes.

## Recover or promote

- Preserve any staged resolution in a legacy stopped build before discarding it.
- Use `git stack integrate --abort <name>` to discard a build interrupted for
  another reason or release its cached worktree.
- If the integration tip has direct commits, inspect the commits listed by
  git-stack. `git stack restack <name> --force` replaces that integration's
  tip and removes those commits from it. Get authorization for that rewrite.
- To promote work after its inputs land on `main`, get the user's approval
  to move the bottom work branch with `git stack restack <work> --onto main`.
  Confirm the approval covers publication and PR retargeting before running
  `git stack push --protect-prs <work>`. Check that no stack or PR uses the integration
  before deleting its refs with the user's approval.
