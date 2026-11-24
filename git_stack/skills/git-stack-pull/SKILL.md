---
name: git-stack-pull
description: Fetch and bring a stack up to date locally with git stack pull. Use when upstream commits are needed without publishing, or to finish a stopped pull.
---

# Pull into a local stack

Read `git-stack` first for selection, ownership and timing. Start with a clean
worktree. Pull fetches, fast-forwards local roots, untracks merged branches,
restacks survivors, and takes unseen upstream commits. It updates local refs.
Exclude queued PR branches from local rewrites, including with `--latest`.
Follow the open-PR checks in `git-stack` before publishing the result.

- Use `git stack pull` for the current stack or `git stack pull <branch>` for a
  named stack. Pass `-g` to include its group. Use `--remote <name>` to select
  a remote when branches track several. Use `git stack pull --github` when
  you need GitHub merge evidence or PR information.
- Use `git stack pull --latest` to bring the stack onto the updated root even
  when it merges cleanly. Use `git stack pull -d local` to delete merged
  local branches as they are untracked.
- If an upstream was deleted without evidence of a merge, pull keeps the
  local branch and warns. If a merged branch was reverted, inspect the revert.
  Use `git stack unrevert <name> <revert>` to bring it back, or ask before using
  `--ignore-revert <branch>` when the reported revert is spurious.
- If the upstream was rewritten or it has unseen commits from before the
  branch was tracked with an explicit base, inspect both histories. Ask before
  using `--rewritten-upstream` or `--pull-tracked-upstream` to take those
  commits. Unseen upstream merge commits need manual inspection and integration.
  Ask before using `--allow-shared`. If the user wants to replace upstream
  commits, see `git-stack-push`.

## Finish a stopped pull

Inspect `git status` in the worktree where it stopped. For a rebase, follow
`git-stack-restack`. For a cherry-pick conflict, resolve and stage the files,
then run `GIT_EDITOR=true git cherry-pick --continue` and repeat as needed.
If an untracked file prevented the cherry-pick from applying, move the file
aside and use `git cherry-pick --abort`. Continuing would skip the commit
that never applied.

Rerun the original git-stack command with the same selectors and options.
For a pull, rerun `git stack pull` with its original arguments. Check
`git stack info --all`. If another worktree blocked an update,
coordinate with whoever is using it before retrying. Use
`git-stack-integrate` for an integration merge conflict.
