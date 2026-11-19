---
name: git-stack-troubleshoot
description: Diagnose and repair a broken stack in git-stack - a branch's recorded base or parent is wrong or missing, plain git was used instead of a git stack command, a branch vanished from `info`, a lock or stopped restack won't clear, or the recorded config looks corrupted. Use when a `git stack` command errors about a base or parent, or the stack doesn't match the branches.
---

# Troubleshooting a stack

When `stack.perfLog` is set, use that log to report total wall time,
how many Git and `gh` calls ran, the time spent in each tool, and the time spent elsewhere.
Count each `cat-file` batch request as a Git call. Include batch shutdown in Git time only.
Report any overlap shown and flag the slowest call if it explains a delay.

Read the `git-stack` skill first. Look before changing anything, and change the least:

```sh
git stack info --all --offline                  # the stacks, and what each branch needs
git config --get-regexp '^branch\..*\.stack'    # what git-stack recorded
git status                                      # a rebase or cherry-pick still in progress?
```

Per branch it records `stackParent` (its parent), `stackBase` (the parent commit it was built on)
and sometimes `stackGroup`, `stackOwnership` and `stackPending...` (a stopped restack or pull).
Prefer `git stack track <parent> <branch>` to editing these by hand. Before any hand edit, back up
`.git/config`. Never `git reset --hard`, and never delete `.git/config`.

## Symptoms and fixes

**"b's recorded base isn't in its history (was it rebased by hand?)"** A plain `git rebase`, a
reset, or a deleted key. Two ways back; ask the user which they want:
- *Undo it* (when the old tip is in the reflog): `git reflog show b`, then
  `git switch b && git reset --keep 'b@{1}'`.
- *Keep the result:* find where b's own commits start (`git log --oneline <parent>..b`), and record the
  commit just before them as the base, then restack:
  ```sh
  git stack track <parent> b --base <sha>
  git stack restack
  ```

**`info` shows "(parent a missing)"** The parent was deleted, or renamed, outside git-stack. If it was
merged, the message prints the exact `git stack track <parent> <branch> --base <sha>` to run. If it was
renamed, `git stack track <new name> <child>`.

**A branch exists but isn't in `info` (and neither are the branches on it)** Its parent key names a
branch that isn't tracked, e.g. after a plain `git branch -m` of the parent. Fix it with
`git stack track <parent> <branch>`: the base is worked out again from the reflog or merge-base.

**"needs restack" won't clear, or restack says it stopped on a conflict** `git status`: if
a rebase is in progress, finish it (see `git-stack-sync`). After a `git rebase --abort`, a pending
record stays, and rerunning `git stack restack` starts the same conflict again.

**"could not lock config file .git/config: File exists"** (after about 5 seconds) A git process died
holding it. Check no git is running, then remove `.git/config.lock`.

**A git-stack command says it's waiting** Another git-stack command holds `.git/git-stack.lock`
(in any worktree) and it's released when that exits. Wait; don't delete it.

**Results look stale** `.git/git-stack-cache.json` caches merge and conflict checks. It is safe to
delete.

**One branch's records are beyond repair** `git stack untrack <branch>` removes its keys (its
children stay on it, as the bottom of their own stack), then `git stack track <parent> <branch>` or
`git stack restack --onto <parent>` rebuilds it.

## Prevent it

Use the git stack commands (`create`, `restack`, `sync`, `push`, `rename`) instead of plain git on tracked branches.
If it looks like a bug in git-stack rather than a stray git command, stop, and report it to the
user with the command, its output and the `git config --get-regexp` listing, rather than working around it.
