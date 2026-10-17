# git-stack

A small, dependency-free tool for stacked branches/PRs that survive squash
merges. No hosted service; everything lives in your local git config.

## The problem

PR 2 is based on PR 1's branch. PR 1 is squash-merged into `main`. Now
`git rebase main` on PR 2 conflicts, because it tries to replay PR 1's
individual commits on top of a `main` that already contains them as one
squashed commit.

## Example workflow

Build a stack of two PRs on `main`:

```sh
git switch main
git stack branch add-api        # PR 1
# ...commit...
git stack branch use-api        # PR 2, stacked on add-api
# ...commit...
git stack push                  # push both; open PR 2 against add-api
```

Address review feedback on PR 1, then move PR 2 on top of it:

```sh
git switch add-api
# ...commit...
git stack info
# main
# └── add-api *
#     └── use-api (needs restack)
git stack restack               # rebase use-api onto the new add-api
git stack push
```

Once PR 1 is squash-merged on GitHub (and its branch deleted; see
[When does `sync` consider a branch merged?](#when-does-sync-consider-a-branch-merged)):

```sh
git stack sync                  # delete add-api, rebase use-api onto main
git stack push
```

## Install

Requires Python 3.9+ and git 2.38+.

```sh
curl -fsSL https://raw.githubusercontent.com/benekastah/git-stack/main/install.sh | sh
```

This installs `git-stack` into `~/.local/bin`, which must be on your `PATH`
for git to run it as `git stack`. Set `GIT_STACK_DIR` to install somewhere
else, or `GIT_STACK_REF` to install a specific branch, tag or commit:

```sh
curl -fsSL https://raw.githubusercontent.com/benekastah/git-stack/main/install.sh | GIT_STACK_DIR="$HOME/bin" sh
```

Run `git stack upgrade` to update to the latest version. To install by hand,
put `git-stack` from this repo anywhere on your `PATH` (and update it with
`git pull`).

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

## Commands

| Command | What it does |
| --- | --- |
| `git stack branch <name>` | Create `<name>` on top of the current branch and track it. |
| `git stack track <parent> [--base <sha>]` | Track an existing branch. `--base` defaults to the merge-base with `<parent>`; pass the old parent tip explicitly if the parent was already squash-merged. |
| `git stack info` | Show all tracked stacks as a tree, marking the current branch and branches that need a restack. |
| `git stack restack [--onto <parent>]` | Rebase every branch in the current stack onto its parent, bottom-up. `--onto` first retargets the current branch. On an untracked branch like `main`, restacks everything stacked on it. |
| `git stack sync [--remote origin]` | Fetch with `--prune`, fast-forward `main` (and any other stack root) to its upstream, delete branches that were merged, move their children down, restack all stacks, and pull in commits others pushed to tracked branches. |
| `git stack push [--remote origin]` | Force-push (with lease) and `--set-upstream` every branch in the current stack. Refuses if a remote branch has commits the local one doesn't; run `sync` first. |
| `git stack upgrade [--ref main]` | Replace the installed `git-stack` with the latest version from GitHub (or the given branch, tag or commit). |

### When does `sync` consider a branch merged?

Both must hold:

1. Its upstream branch is gone after `git fetch --prune` (enable "Automatically
   delete head branches" on GitHub). Branches that were never pushed with an
   upstream are never considered merged.
2. Its changes are already in its parent (`main`, or the parent PR's branch if
   it was merged into that): merging it into the parent would change nothing.

A branch whose remote was deleted without merging (a closed PR) fails the
second check, so `sync` warns and leaves it and its children alone rather than
silently dropping its commits from the stack.

If a branch's metadata is broken (for example its parent was deleted by hand),
`sync` warns and skips that branch and everything stacked on it, and carries on
with the other stacks.

Note that `sync` restacks every tracked stack, including rebasing bottom
branches onto the updated `main`. Nothing is pushed until you run
`git stack push`.

### Pulling commits others pushed

After restacking a branch, `sync` cherry-picks onto it any commits on its
upstream that it doesn't already have (fast-forwarding when it can). Commits
that are older versions of your own rebased commits, merge commits (e.g.
GitHub's "Update branch"), and changes already in the parent are skipped.

`push` pins each lease to the remote-tracking ref it checked, so if someone
pushes after your last fetch, the push fails instead of overwriting their
work.

### Conflicts

If a rebase or cherry-pick stops on a conflict, resolve it and run
`git rebase --continue` / `git cherry-pick --continue` (or `--abort`) as usual,
then rerun the git-stack command to finish the rest of the stack. The new
parent and base (and the pulled upstream commits) are recorded only if the
operation actually completed.

## Common issues

### The same line conflicts on every commit during a restack

If a parent branch and several of its child's commits all edit the same line
(say, a function's argument list), restacking replays the child one commit at a
time, so you resolve a conflict on that line for every commit. Git merges whole
lines, so it can't tell that the edits touch different arguments.

[Mergiraf](https://mergiraf.org) is a merge driver that merges by syntax tree
instead of by line, so edits to different parts of the same line often merge
cleanly. It can be enabled just for yourself (globally or for one repository)
without committing anything, and git-stack's rebases pick it up automatically.

## Tests

```
python3 -m unittest discover -s tests
```
