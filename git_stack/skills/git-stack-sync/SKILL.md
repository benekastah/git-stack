---
name: git-stack-sync
description: Bring stacked branches up to date with git-stack - restack, sync, pull, clean, push - and finish a restack or sync that stopped on a conflict or warned. Use after a PR in a stack merged, main changed, review feedback changed a lower branch, someone pushed to a branch, or a `git stack` command stopped.
---

# Keeping a stack current

Read the `git-stack` skill first. Commit your work first: `restack` and `sync` refuse a dirty
worktree (untracked files are fine unless one is in the way of a commit being replayed).

## Which command

| Situation | Run |
| --- | --- |
| You committed to a branch that has branches stacked on it | `git stack restack`, then `git stack push` |
| A PR merged, `main` moved, or someone pushed to your branch | `git stack sync` |
| Also delete merged local branches | `git stack sync -d local` |
| Put the stack on the latest `main` | `git stack sync --latest` (or `restack --latest`) |
| Catch up locally only, no fetch-push | `git stack pull` (or `restack`) |
| Untrack merged branches in every stack, restacking nothing | `git stack clean` |
| Branch never pushed, or PRs to open | `git stack push --prs` (`sync` pushes work branches that already have an upstream) |

`sync` fetches, fast-forwards the root, untracks merged branches (moving their children onto the
parent), restacks, pulls in commits others pushed, then pushes with leases. A branch directly on an
ordinary root only moves if it conflicts with the root's new commits or with `--latest`.

For stacks on an integration root, `sync` refreshes the integration and publishes it with the
eligible work. Use `git-stack-integrate` to manage these inputs or resolve a merge stopped in the
temporary integration worktree.

Ask the user before `-d remote`, `-d both`, `--force`, `--allow-shared`, `--rewritten-upstream`,
`--pull-tracked-upstream` and `--ignore-revert`: each overrides a protection.

## When it stops on a conflict

Nothing is pushed while a restack or pull is stopped. Git-stack records the pending result and
finishes it when you rerun it.

1. `git status` shows the conflicted files. Resolve them, then `git add` each.
2. Continue without an editor: `GIT_EDITOR=true git rebase --continue` (for a pull,
   `git cherry-pick --continue`). Repeat while it stops again.
3. Rerun the command you started (`git stack restack` or `git stack sync`) to finish the rest of the
   stack and record the new bases.
4. Check with `git stack info --all --offline`, then `git stack push` if you didn't `sync`.

- Don't `git rebase --abort` unless the user asks: the pending record stays, and rerunning the
  command restarts the same conflict.
- If an untracked file stops a commit being replayed (git says which), move it away and run
  `git rebase --continue`. For a pull, run `git cherry-pick --abort` instead (`--continue` would skip
  the commit) and rerun `git stack sync`.
- The same line conflicting on every commit of a branch is a limit of line-based merging. A syntax-aware merge driver
  such as mergiraf avoids it; suggest it to the user, don't enable it yourself.

## Warnings you may meet

- **Checked out in another worktree**: the branch, and the ones on it, are skipped. Finish or
  switch that worktree, then rerun.
- **Merged, then reverted**: the branch and its children are left alone. `git stack unrevert <name> <revert>` brings
  it back as a new branch; if it wasn't really a revert, `--ignore-revert <branch>`. Ask the user.
- **Upstream has unseen commits and its history was rewritten**: don't guess. Inspect, then the user
  picks `--rewritten-upstream` (replay them) or `git stack push --force` (replace the remote).
- **Shared stack** (someone else's commits): needs `--allow-shared`. Ask first.
- **Remote branch deleted without a merge**: the branch is kept with a warning. Nothing to do.
- **Broken metadata** (parent deleted by hand): see `git-stack-troubleshoot`.
