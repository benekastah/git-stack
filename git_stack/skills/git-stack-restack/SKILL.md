---
name: git-stack-restack
description: Replay a stack onto its local parents with git stack restack. Use after changing a lower branch, when moving a branch to a new parent, or to finish a stopped restack.
---

# Restack onto local parents

Read `git-stack` first for selection, ownership and timing. Start with a clean
worktree. Restack replays each branch's own commits from its recorded base,
starting at the bottom. It uses local refs. Exclude queued PR branches from
local rewrites, including with `--latest`, and follow the open-PR checks in
`git-stack` before publishing.

- Use `git stack restack` for the current stack or `git stack restack <branch>`
  for a named stack. Pass `-g` to include its group. Restacking an
  integration or work above it also restacks tracked sources and their
  ancestors. Check those dependencies for shared work and queued PRs.
- A branch directly on an ordinary root moves if it conflicts with that
  root. Use `git stack restack --latest` to move it onto the latest local
  root either way. Use `git stack restack --prs` if you need GitHub merge
  evidence, such as for a squash merge with edits. Use `git-stack-pull` when
  you also need to fetch.
- Use `git stack restack <branch> --onto <parent>` to move one branch and its
  descendants only after the user approves the rearrangement. Follow
  `git-stack-rearrange` to check affected PRs before moving them.
- A branch that needs restacking in another worktree is skipped along with
  its descendants. Coordinate with whoever is using the worktree before
  retrying. Ask before overriding protections with `--allow-shared` or
  `--force`. See `git-stack-integrate` for a stopped integration build.

## Finish a stopped rebase

1. Use `git status` in the worktree where the rebase stopped. Resolve conflicts
   and stage the resolved files with `git add`.
2. Run `GIT_EDITOR=true git rebase --continue`. Repeat if another commit conflicts.
3. Rerun the original git-stack command with the same selectors and options
   so it records the new bases and finishes the rest of the stack.
4. Check `git stack info --all`. If publication is requested, use
   `git-stack-push`.

If an untracked file stops the rebase, move it out of the way before
continuing. Ask before `git rebase --abort`. Git-stack keeps the pending
record, so retrying restack after abort starts the rebase again. Repeated
conflicts on the same line can be a limitation of line-based merging. A
syntax-aware merge driver such as mergiraf may help. Ask before enabling one.
For broken bases or parents, see `git-stack-troubleshoot`.
