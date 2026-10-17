# git-stack

A small, dependency-free tool for stacked branches/PRs that survive squash
merges. No hosted service; everything lives in your local git config.

## The problem

PR 2 is based on PR 1's branch. PR 1 is squash-merged into `main`. Now
`git rebase main` on PR 2 conflicts, because it tries to replay PR 1's
individual commits on top of a `main` that already contains them as one
squashed commit.

## How it works

When a branch is created (or tracked), git-stack records its parent branch and
the exact parent commit it was built on:

```
branch.<name>.stackParent   e.g. pr1
branch.<name>.stackBase     the pr1 commit pr2 was last rebased onto
```

Restacking runs `git rebase --onto <parent> <stackBase> <branch>`, which
replays only the branch's own commits, so PR 1's original commits never
collide with the squashed version in `main`.

## Install

Requires Python 3.9+ and git 2.38+. Put `git-stack` on your `PATH` (e.g.
symlink it into `~/.local/bin`) and git will run it as `git stack`.

## Commands

| Command | What it does |
| --- | --- |
| `git stack branch <name>` | Create `<name>` on top of the current branch and track it. |
| `git stack track <parent> [--base <sha>]` | Track an existing branch. `--base` defaults to the merge-base with `<parent>`; pass the old parent tip explicitly if the parent was already squash-merged. |
| `git stack info` | Show all tracked stacks as a tree, marking the current branch and branches that need a restack. |
| `git stack restack [--onto <parent>]` | Rebase every branch in the current stack onto its parent, bottom-up. `--onto` first retargets the current branch. On an untracked branch like `main`, restacks everything stacked on it. |
| `git stack sync [--remote origin]` | Fetch with `--prune`, fast-forward `main` (and any other stack root) to its upstream, delete branches that were merged, move their children down, and restack all stacks. |
| `git stack push [--remote origin]` | `git push --force-with-lease --set-upstream` every branch in the current stack. |

### When does `sync` consider a branch merged?

Both must hold:

1. Its upstream branch is gone after `git fetch --prune` (enable "Automatically
   delete head branches" on GitHub). Branches that were never pushed with an
   upstream are never considered merged.
2. Its changes are already in the stack's root (e.g. `main`): merging it into
   `main` would change nothing.

A branch whose remote was deleted without merging (a closed PR) fails the
second check, so `sync` warns and leaves it and its children alone rather than
silently dropping its commits from the stack.

Note that `sync` restacks every tracked stack, including rebasing bottom
branches onto the updated `main`. Nothing is pushed until you run
`git stack push`.

### Conflicts

If a rebase stops on a conflict, resolve it and run `git rebase --continue`
(or `git rebase --abort`) as usual, then rerun the git-stack command to finish
the rest of the stack. The new base is recorded only if the rebase actually
completed.

## Tests

```
python3 -m unittest discover -s tests
```
