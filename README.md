# git-stack

A small, dependency-free tool for stacked branches/PRs that survive squash
merges. No hosted service; everything lives in your local git config. On
GitHub, it can also use the [`gh` CLI](https://cli.github.com) to tell which
pull requests were merged and to keep their base branches in step with the
stack (see [GitHub](#github)).

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
git stack push                  # push both, printing links to open their PRs
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

Requires Python 3.11+ and git 2.38+. The GitHub integration also needs
[`gh`](https://cli.github.com), logged in with `gh auth login`.

Install it with [pipx](https://pipx.pypa.io) or
[uv](https://docs.astral.sh/uv/):

```sh
pipx install git+https://github.com/benekastah/git-stack
# or
uv tool install git+https://github.com/benekastah/git-stack
```

Either one puts `git-stack` on your `PATH` (run `pipx ensurepath` or
`uv tool update-shell` if it isn't), so git runs it as `git stack`. To install
a specific branch, tag or commit, add `@<ref>` to the URL. To update to the
latest version, run `pipx upgrade git-stack` or `uv tool upgrade git-stack`.

### From a local clone

To run git-stack from a clone of this repository, install it in editable mode,
so changes to the clone take effect without reinstalling:

```sh
pipx install --editable /path/to/git-stack
# or
uv tool install --editable /path/to/git-stack
```

Without pipx or uv, install it into a virtualenv and link its `git-stack`
into a directory on your `PATH`:

```sh
python3 -m venv ~/.local/share/git-stack/venv
~/.local/share/git-stack/venv/bin/pip install --editable /path/to/git-stack
ln -s ~/.local/share/git-stack/venv/bin/git-stack ~/.local/bin/git-stack
```

Reinstall only when `pyproject.toml` changes, for example when a command-line
entry point is added.

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
on github.com (including through a `Host` alias in `~/.ssh/config`, e.g.
`git@github-work:org/repo.git`) and `gh` is installed, and `git` otherwise. Set it to `github` for
a GitHub Enterprise host, or to `git` to turn the GitHub integration off:

```sh
git config stack.adapter git
```

With the GitHub adapter:

- A branch's pull request is the one whose head is a remote branch of the same
  name in the same repository, however it was opened. An open one wins;
  otherwise the newest merged one counts, if its head descends from the commit
  the branch is stacked on (so one merged from an earlier branch of the same
  name doesn't). Closed pull requests are ignored.
- `git stack push` changes the base of the stack's open pull requests to match
  their stack parents, e.g. to `main` once the parent PR was merged.
- git-stack doesn't open pull requests. For each pushed branch without one,
  `push` prints a link to GitHub's page for opening one against its stack
  parent, with the repository's pull request template filled in. (The link
  GitHub prints when a branch is first pushed targets the default branch
  instead; `push` retargets a pull request opened from it the next time it
  runs.)
- A pull request that GitHub says was merged counts as merged once its merge
  commit is in the local parent branch (`sync` fetches it), even if the parent
  changed the merged lines since, or the merge changed them.
- If GitHub can't be reached, git-stack warns and detects merges like the `git`
  adapter does. `push` still pushes, but then fails without retargeting pull
  requests or printing links.

## Commands

Like git, every command accepts `-C <path>` before it to run as if started in
`<path>`, e.g. `git stack -C ~/src/app info`. Each `-C` after the first is
relative to the one before.

| Command | What it does |
| --- | --- |
| `git stack branch <name>` | Create `<name>` on top of the current branch and track it. |
| `git stack track <parent> [<branch>...] [--base <sha>]` | Track existing branches (default: the current branch) as a stack on `<parent>`, each stacked on the one before it, e.g. `git stack track main pr1 pr2 pr3`. `--base` is where the first branch starts and defaults to the merge-base with `<parent>`; pass the old parent tip explicitly if the parent was already squash-merged. Later branches use the merge-base with the branch before them. |
| `git stack rename [<old>] <new>` | Rename a branch (the current one by default) and move the branches stacked on it onto the new name. Unsets its upstream, since the remote branch isn't renamed (its pull request stays with the old name); the next `git stack push` pushes it under the new name, and its pull request has to be opened again. |
| `git stack info` | Show all tracked stacks as a tree, marking the current branch, branches that were merged, and branches that need a restack. |
| `git stack restack [--onto <parent>] [--squash]` | Rebase every branch in the current stack onto its parent, bottom-up. `--onto` first retargets the current branch. Branches that were merged are skipped and their children restacked onto their parent; `sync` or `clean` removes them from the stack. `--squash` first squashes each branch in the current stack into one commit (keeping all the messages), so a conflict is resolved once per branch instead of once per commit. Refuses to run outside a stack. |
| `git stack sync [--remote origin] [-d local\|remote\|both]` | Fetch with `--prune`, fast-forward the current stack's root (e.g. `main`) to its upstream, stop tracking the stack's branches that were merged, move their children down, restack the stack, and pull in commits others pushed to its branches. Other stacks are left alone. Refuses to run outside a stack. Merged branches are kept unless `-d`/`--delete` says to delete their local branches, their remote branches, or both. |
| `git stack clean [--remote origin] [-d local\|remote\|both]` | Fetch with `--prune`, fast-forward every stack root, and stop tracking merged branches in every stack, moving their children down. Nothing is restacked, so there are no conflicts; the children are rebased the next time you `sync` or `restack` their stack. `-d` works as for `sync`. Runs from any branch. |
| `git stack push [<branch>] [--remote origin] [--force]` | Force-push (with lease) and `--set-upstream` every branch in the current stack, or `<branch>`'s. Skips merged branches. Refuses to run outside a stack, or if a remote branch was deleted or has commits the local one doesn't; run `sync` first, or pass `--force` to drop those commits. With the [GitHub adapter](#github), updates the base of the stack's pull requests, and prints a link to open one for each branch that has none. |

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

If a pull stops on a conflict and you abort it (or skip some of the commits),
the commits you didn't take aren't counted as pulled: the next `sync` offers
them again, and `push` refuses to drop them. To drop them anyway, run
`git stack push --force`.

`push` pins each lease to the remote-tracking ref it checked, so if someone
pushes after your last fetch, the push fails instead of overwriting their
work, even with `--force`.

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

### A merged branch was reverted

If `pr1` is merged and then reverted on `main`, git-stack still considers it
merged: its pull request was merged, and `main` has a commit with its changes.
`sync` stops tracking `pr1` (and deletes it with `-d local`) and restacks
`pr2` onto `main`, which no longer has `pr1`'s changes. Tracking the old `pr1`
again doesn't help, since it still looks merged.

Instead, recreate `pr1` with new commits on top of `main`, then move `pr2` back
onto it:

```sh
git switch main
git stack branch pr1-again
git revert <the revert commit>     # or cherry-pick pr1's commits
git switch pr2
git stack restack --onto pr1-again
git stack push                     # point pr2's pull request at pr1-again, and link to open one for it
```

`restack --onto` moves only the current branch, so if `pr1` had several
children, run it from each one.

## Development

Run every check (mypy in strict mode, then the tests) with:

```sh
./check.sh
```

The first run creates a virtualenv in `.venv` and installs the `dev`
dependency group from `pyproject.toml` into it (with `pip install --group dev`,
which needs pip 25.1+). Later runs reinstall it whenever `pyproject.toml`
changes. Arguments are passed to pytest, so `./check.sh -k sync` runs only the
tests whose names contain `sync`. git-stack itself still has no runtime
dependencies.
