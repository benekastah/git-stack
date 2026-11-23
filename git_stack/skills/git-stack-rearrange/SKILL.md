---
name: git-stack-rearrange
description: Reshape stacked branches with git-stack - move a branch to another parent, insert or remove one in the middle, swap two, fold one into its parent, split one in two, squash a branch's commits, rename. Use when the user wants a stack's shape or a branch's commits changed.
---

# Rearranging a stack

When `stack.perfLog` is set, use that log to report total wall time,
how many Git and `gh` calls ran, the time spent in each tool, and the time spent elsewhere.
Count each `cat-file` batch request as a Git call. Include batch shutdown in Git time only.
Report any overlap shown and flag the slowest call if it explains a delay.

Read the approval rules in `git-stack` first. Show the proposed stack
shape and affected PRs, including descendants. Verify their bases and queue
status and exclude queued branches from the proposed moves. Then get the
user's approval before running any move below.

Start with a clean worktree. `git stack restack <branch> --onto <parent>`
moves the branch and its descendants. `git stack track <parent> <branch>`
records a new parent and base. Naming the branch means you needn't switch to it.

After rearranging, run `git stack info --all` to verify the shape.
Before publishing with `git stack push --protect-prs`, `git stack sync --protect-prs`, or `git stack prs --protect-prs`,
recheck the queue and confirm the user's approval covers the resulting PR
base changes.

## Moves

**Move a branch (and what's on it) onto another parent**, e.g. `b` onto `main`:

```sh
git stack restack b --onto main
```

**Insert `a2` between `a` and `b`:**

```sh
git stack create a2 --from a   # switches to a2; ...commit...
git stack restack b --onto a2
```

**Remove `b` from between `a` and `c`**, dropping b's changes from `c`:

```sh
git stack restack c --onto a
git stack untrack b          # then delete b yourself if the user wants it gone
```

**Fold `b` (directly on `a`) into `a`**, keeping its changes:

```sh
git switch a
git merge --ff-only b        # if it can't fast-forward, restack b first
git stack untrack b
git stack track a c          # c was on b; put it on a
```

**Swap `a` and `b`** (`main` → `a` → `b` → `c` becomes `main` → `b` → `a` → `c`):

```sh
git stack restack b --onto main    # b, with c, onto main
git stack restack a --onto b       # a onto b
git stack restack c --onto a       # c back on top
```

**Rename:** `git stack rename <new>`. Plain `git branch -m` leaves the branch's children pointing
at the old name.

## Split a branch in two

`big` has two commits; make `big` the first and `part2` the second. Set `part2`'s base *before*
moving `big`, or `part2` silently loses its commit (it's then only in the reflog):

```sh
git stack create part2 --from big   # same tip as big; you're on part2
git stack track big part2 --base HEAD~1
git branch -f big HEAD~1         # big is now the first commit
git stack restack
```

To split by files rather than commits, make a branch per step with `git stack create <name>`, bring each
step's files with `git checkout <big> -- <paths>` and commit. Check nothing was lost with
`git diff <big> <top of stack>`: it must be empty.

## Squash a branch's own commits

```sh
git switch a
git reset --soft $(git stack base)
git commit -m "<message>"
git stack restack                # the branches above follow
```

This needs no editor. Reordering, splitting or regrouping commits inside a branch has no git-stack
command, and git's interactive rebase needs a person at an editor. If `git-recommit` is installed
(`command -v git-recommit`), use it, and its skill if that's installed too: give it the range
`$(git stack base)..HEAD`, then `git stack restack` so the branches above follow. If it isn't
installed, ask the user.

## Groups

Stacks that were one stack until their bottom merged form a group, so commands cover them together.
`git stack group add <name> <branch>...` puts stacks in one by hand (see the README's Groups).
