# git-stack

A small, dependency-free tool for stacked branches/PRs that survive squash
merges. No hosted service; everything lives in your local git config. On
GitHub, it can also use the [`gh` CLI](https://cli.github.com) to open draft
pull requests and to tell which ones were merged (see [GitHub](#github)).

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
git stack push                  # push both; open a draft PR for use-api against add-api
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

Once PR 1 is merged, on GitHub or by merging it into `main` locally and pushing
(see [When does `sync` consider a branch merged?](#when-does-sync-consider-a-branch-merged)):

```sh
git stack sync -d local         # delete add-api, rebase use-api onto main, switch to it
git stack push
```

`sync` only touches the current stack. To untrack (or delete) merged branches
in every stack without restacking anything, run `git stack clean`.

## Install

Requires Python 3.9+ and git 2.38+. The GitHub integration also needs
[`gh`](https://cli.github.com), logged in with `gh auth login`.

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

## GitHub

git-stack works out merges through an adapter, chosen by the `stack.adapter`
setting:

- `git`: looks only at the repository.
- `github`: asks GitHub, through `gh`, about each branch's pull request, and
  falls back to the `git` adapter for branches merged without one (for
  example, merged into their parent locally). It doesn't fall back for a
  branch whose parent is the repository's default branch (e.g. `main`) and
  whose pull request isn't merged, since it assumes nobody merges into the
  default branch locally.

If `stack.adapter` isn't set, git-stack uses `github` when the remote's URL is
on github.com and `gh` is installed, and `git` otherwise. Set it to `github` for
a GitHub Enterprise host, or to `git` to turn the GitHub integration off:

```sh
git config stack.adapter git
```

With the GitHub adapter:

- `git stack push` opens a draft pull request for the current branch, based on
  its stack parent, if it doesn't have an open one yet. Other branches in the
  stack don't get one until you push from them. The pull request's number is
  recorded in `branch.<name>.stackPr`.
- `push` also changes the base of the stack's open pull requests to match their
  stack parents, e.g. to `main` once the parent PR was merged.
- A pull request that GitHub says was merged counts as merged once its merge
  commit is in the local parent branch (`sync` fetches it), even if the parent
  changed the merged lines since, or the merge changed them.
- If GitHub can't be reached, git-stack warns and detects merges like the `git`
  adapter does.

## Commands

| Command | What it does |
| --- | --- |
| `git stack branch <name>` | Create `<name>` on top of the current branch and track it. |
| `git stack track <parent> [<branch>...] [--base <sha>]` | Track existing branches (default: the current branch) as a stack on `<parent>`, each stacked on the one before it, e.g. `git stack track main pr1 pr2 pr3`. `--base` is where the first branch starts and defaults to the merge-base with `<parent>`; pass the old parent tip explicitly if the parent was already squash-merged. Later branches use the merge-base with the branch before them. |
| `git stack rename [<old>] <new>` | Rename a branch (the current one by default) and move the branches stacked on it onto the new name. Unsets its upstream and forgets its pull request, since the remote branch isn't renamed; the next `git stack push` pushes it under the new name (and opens a new draft pull request with the GitHub adapter). |
| `git stack info` | Show all tracked stacks as a tree, marking the current branch, branches that were merged, and branches that need a restack. |
| `git stack restack [--onto <parent>] [--squash]` | Rebase every branch in the current stack onto its parent, bottom-up. `--onto` first retargets the current branch. Branches that were merged are skipped and their children restacked onto their parent; `sync` or `clean` removes them from the stack. `--squash` first squashes each branch in the current stack into one commit (keeping all the messages), so a conflict is resolved once per branch instead of once per commit. Refuses to run outside a stack. |
| `git stack sync [--remote origin] [-d local\|remote\|both]` | Fetch with `--prune`, fast-forward the current stack's root (e.g. `main`) to its upstream, stop tracking the stack's branches that were merged, move their children down, restack the stack, and pull in commits others pushed to its branches. Other stacks are left alone. Refuses to run outside a stack. Merged branches are kept unless `-d`/`--delete` says to delete their local branches, their remote branches, or both. |
| `git stack clean [--remote origin] [-d local\|remote\|both]` | Fetch with `--prune`, fast-forward every stack root, and stop tracking merged branches in every stack, moving their children down. Nothing is restacked, so there are no conflicts; the children are rebased the next time you `sync` or `restack` their stack. `-d` works as for `sync`. Runs from any branch. |
| `git stack push [--remote origin]` | Force-push (with lease) and `--set-upstream` every branch in the current stack. Skips merged branches. Refuses to run outside a stack, or if a remote branch was deleted or has commits the local one doesn't; run `sync` first. With the [GitHub adapter](#github), opens a draft pull request for the current branch if it has none, and updates the base of the stack's pull requests. |
| `git stack upgrade [--ref main]` | Replace the installed `git-stack` with the latest version from GitHub (or the given branch, tag or commit). |

### When does `sync` consider a branch merged?

With the [GitHub adapter](#github), a branch whose pull request GitHub says was
merged counts as merged once the merge is in its local parent, unless the branch
or its remote branch has commits that weren't merged. Otherwise:

Its changes must already be in its parent (`main`, or the parent PR's branch if
it was merged into that): either merging it into the parent would change
nothing, or the parent has a commit with the same changes as the whole branch
(a squash merge) or commits with the same changes as each of its commits (a
rebase merge), even if the parent edited the same lines again afterwards. This
holds however it was merged: squash, rebase or merge commit, on GitHub or
locally. In addition, either:

- its upstream branch is gone after `git fetch --prune` (e.g. GitHub's
  "Automatically delete head branches" deleted it), or
- it has no upstream, or everything on its upstream is in the parent too, and
  it changes something relative to the commit it was built on (so a new branch
  with no commits isn't mistaken for a merged one).

A merged branch is no longer tracked, but `sync` and `clean` keep it unless you
pass `--delete local` (or `-d local`) to delete it. If its upstream branch
still exists, they say so; `--delete remote` deletes it, and `--delete both`
deletes the local and remote branches. Deleting a remote branch is leased to
the fetched tip, so it fails rather than deleting commits pushed since. Since a
branch is untracked by the first `sync` or `clean` that sees it merged, pass
`--delete` on that run if you want it deleted. Deleting the branch you're on
switches to its first child, or to its parent if it has none.

A branch whose remote was deleted without merging (a closed PR) fails the first
check, so `sync` warns and leaves it in the stack rather than silently dropping
its commits. A merged branch whose remote branch got new commits after the
merge is skipped with a warning, along with the branches stacked on it, since
restacking it would replay its merged commits.

`restack` uses the same check but changes nothing about them: it skips merged
branches and restacks their children onto their parent. `push` skips them too,
and points their children's pull requests at their parent. It also refuses to
push a branch whose remote branch was deleted, since that's usually a merged
pull request `sync` hasn't seen yet.

If a branch's metadata is broken (for example its parent was deleted by hand),
`sync` warns and skips that branch and everything stacked on it, and carries on
with the rest of the stack.

`sync` rebases the stack's bottom branch onto the updated `main`, but nothing is
pushed until you run `git stack push`, so until then pull requests still show
the old commits. `sync` ends by listing the branches whose remote branch is out
of date.

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
