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
  name doesn't). Closed pull requests are ignored. If a merged pull request's
  last commit was never fetched (say GitHub's "Update branch" made it right
  before the merge, and the branch was deleted on merge), git-stack fetches it
  from the pull request.
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
relative to the one before. It's also fine to do `git -C <path> stack ...`.

Several commands act on "the current stack": every tracked branch stacked on
the current branch's bottom-most tracked ancestor, including branches beside
the current one, not just the ones above it. In this stack, running `restack`
from `pr2b` restacks `pr1`, `pr2a`, `pr2b` and `pr3`:

```
main
└── pr1
    ├── pr2a
    │   └── pr3
    └── pr2b *
```

### `git stack branch <name>`

Create `<name>` on top of the current branch, switch to it, and track it.

**When would I use it?** Whenever you start a new branch for a pull request,
use this instead of `git switch -c`, so git-stack knows what it's built on. Run
it from `main` for a change that stands on its own, or from another PR's branch
for a change that depends on that PR before it's merged.

```sh
git switch main
git stack branch add-api        # stacked on main
git stack branch use-api        # stacked on add-api
```

- Uncommitted changes come along to the new branch, as with `git switch -c`.
- Refuses a name a remote already has a branch of (as of your last fetch),
  since that's most likely someone else's branch, and pushing yours would
  overwrite it. In a shared repository, prefixing branch names with your
  username (e.g. `jane/add-api`) avoids picking a name that's taken.
- The new branch has no upstream until you `git stack push` it, even if
  `branch.autoSetupMerge` would normally give it its parent's.
- Whatever untracked branch is at the bottom of a stack (usually `main`) is its
  root, and `sync` and `clean` fast-forward it to its upstream. If you branch
  off a branch that isn't tracked, that branch becomes the root.

### `git stack track <parent> [<branch>...] [--base <sha>]`

Track existing branches (default: the current branch) as a stack on `<parent>`,
each stacked on the one before it.

**When would I use it?** When you have branches that git-stack doesn't know
about yet: made with plain `git switch -c`, or from before you installed
git-stack. Also to record a different parent or base for a branch that's
already tracked without rebasing it, e.g. after rebasing it by hand. To move a
branch onto another parent and rebase it in one go, use
`git stack restack --onto` instead.

```sh
git stack track main            # stack the current branch on main
git stack track main pr1 pr2 pr3
git stack track pr1             # move the current branch onto pr1
```

Each branch's base, the commit it was built on, is where it forked from the
branch before it (or from `<parent>`). Like `git rebase`, this is found from
that branch's reflog, so a parent rewritten since is handled. Without a reflog
entry, it's their merge-base.

- `--base` sets the first branch's base. Pass it if the parent was already
  squash-merged, since the fork point can't be worked out then: use the old
  parent tip the branch was built on, e.g. `git stack track main pr2 --base <old pr1 tip>`.
- `<parent>` must be a local branch (`main`, not `origin/main`).
- Tracking a branch that's already tracked changes its parent and base. It
  doesn't rebase anything; run `git stack restack` for that.
- Nothing is written unless every branch can be tracked, so a bad argument
  doesn't leave the stack half-tracked.

### `git stack untrack [<branch>]`

Stop tracking a branch (default: the current one), without deleting it or
changing its commits.

**When would I use it?** When you want git-stack to stop managing a branch but
keep it around: a long-lived branch you don't want `sync` to rebase, or a pull
request you abandoned but might come back to. You don't need it for merged
branches, since `sync` and `clean` untrack those for you.

```sh
git stack untrack experiment
```

- Branches stacked on it stay on it, so it becomes the root of their stack,
  like `main`, and `sync` and `clean` fast-forward it to its upstream. To move
  them elsewhere, switch to each and run `git stack restack --onto <parent>`.
- To stop tracking a branch that was merged, use `sync` or `clean`, which also
  move its children down.

### `git stack rename [<old>] <new>`

Rename a branch (default: the current one) and keep the branches stacked on it
on it.

**When would I use it?** When you want to rename a branch in a stack. A plain
`git branch -m` leaves the branches stacked on it pointing at the old name, so
they'd show up with a missing parent.

```sh
git stack rename api-v2                 # rename the current branch
git stack rename add-api api-v2
```

- Like `branch`, refuses a new name a remote already has a branch of.
- Its remote branch isn't renamed, and its pull request stays with the old
  name. The renamed branch's upstream is unset, so the next `git stack push`
  pushes it under the new name, and you have to open its pull request again.
  Delete the old remote branch yourself (`git push origin :add-api`) if you
  no longer want it.

### `git stack info [--json]`

Show every tracked stack as a tree.

**When would I use it?** When you want to see your stacks and what needs doing:
which branches depend on which, which need a restack, and which were merged.
It's a good first step before `restack`, `sync` or `push`.

```sh
git stack info
# main
# └── add-api *
#     ├── use-api (needs restack)
#     └── docs (merged)
```

- `*` marks the current branch.
- `(needs restack)`: its parent moved since it was last rebased; run
  `git stack restack` or `git stack sync`.
- `(merged)`: it was merged into its parent (see
  [When does `sync` consider a branch merged?](#when-does-sync-consider-a-branch-merged));
  `sync` or `clean` will stop tracking it.
- `(merged, with new commits)`: it was merged, but its remote branch got
  commits after the merge. `sync` skips it, and the branches stacked on it,
  until you sort it out.
- `(parent <name> missing)`: its parent branch was deleted by hand; fix it
  with `git stack track <parent>`.
- `info` doesn't fetch, so merges on the remote only show up after a `sync`,
  `clean` or `git fetch`.
- `--json` prints the same information for scripts: a list with an object for
  each branch, each stack's root first, parent before child. `parent` and
  `status` are `null` for a root, and `status` is one of `up-to-date`,
  `needs-restack`, `merged`, `merged-with-new-commits` or `parent-missing`.

  ```sh
  git stack info --json
  # [
  #   {"name": "main", "parent": null, "status": null, "current": false},
  #   {"name": "add-api", "parent": "main", "status": "up-to-date", "current": true},
  #   ...
  # ]
  ```

- With the [GitHub adapter](#github), `info` asks GitHub about each branch's
  pull request, so it can take a moment. For a shell prompt or an editor, the
  [scripting commands](#scripting) below only read git config, and are fast.

### `git stack up`

Switch to the branch stacked on the current one.

**When would I use it?** To move up the stack, e.g. from PR 1 to PR 2 after
committing to PR 1, without remembering the branch's name.

```sh
git switch main
git stack up                    # to add-api
git stack up                    # to use-api
```

- If several branches are stacked on the current one, it lists them and
  leaves you where you are; pick one with `git switch`.
- Works from a stack's root (e.g. `main`), as long as only one branch is
  stacked on it.

### `git stack down`

Switch to the branch the current one is stacked on.

**When would I use it?** To move down the stack, e.g. back to PR 1 to address
review feedback while you're working on PR 2.

```sh
git stack down                  # from use-api to add-api
git stack down                  # to main
```

- From the bottom of a stack, it switches to the stack's root (e.g. `main`).

### `git stack restack [--onto <parent>] [--squash]`

Rebase every branch in the current stack onto its parent, bottom-up.

**When would I use it?** After you commit to a branch that has other branches
stacked on it, e.g. to address review feedback on PR 1, so the branches above
it include the change. It works locally, without fetching or pushing. With
`--onto`, when a branch should be stacked somewhere else, e.g. PR 2 turns out
not to need PR 1. With `--squash`, when a restack keeps stopping on the same
conflict, commit after commit.

```sh
git stack restack
git stack restack --onto main           # move the current branch onto main, then restack
git stack restack --squash              # squash each branch first, then restack
```

- Only the branches' own commits are replayed (from their recorded base), so a
  parent that was squash-merged doesn't cause conflicts. See
  [How it works](#how-it-works).
- `--onto` retargets only the current branch, onto another local branch. The
  branches stacked on it come along.
- `--squash` first squashes each branch in the current stack into one commit,
  keeping all the messages and the oldest commit's author and date, so a
  conflict is resolved once per branch instead of once per commit. It commits
  with `--no-verify`, so commit-msg hooks don't run.
- Merged branches are skipped and their children restacked onto their parent;
  `sync` or `clean` removes them from the stack.
- Doesn't fetch or push. Needs a clean worktree, and refuses to run outside a
  stack.
- If a rebase stops on a conflict, see [Conflicts](#conflicts).
- A branch checked out in another worktree is skipped, with a warning; see
  [Branches checked out in other worktrees](#branches-checked-out-in-other-worktrees).

### `git stack sync [--remote origin] [-d local|remote|both]`

Bring the current stack up to date with the remote:

1. Fetch with `--prune`.
2. Fast-forward the stack's root (e.g. `main`) to its upstream.
3. Stop tracking the stack's merged branches, moving their children onto their
   parent.
4. Restack the rest.
5. Pull in commits others pushed to its branches (see
   [Pulling commits others pushed](#pulling-commits-others-pushed)).

**When would I use it?** Your everyday command for catching up with the remote:
after a pull request in the stack was merged, when `main` has moved on and you
want the stack on top of it, or when someone else pushed to one of your
branches. Follow it with `git stack push`.

```sh
git stack sync
git stack sync -d local         # also delete merged branches locally
git stack sync -d both          # ...and their remote branches
```

- Only the current stack is synced; to untrack merged branches in every stack,
  use `clean`.
- Merged branches are kept unless `-d`/`--delete` says to delete their local
  branches, their remote branches, or both. A branch is untracked by the first
  `sync` or `clean` that sees it merged, so pass `-d` on that run if you want
  it deleted. Deleting a remote branch is leased to the fetched tip, so it
  fails rather than deleting commits pushed since. Deleting the branch you're
  on switches to its first child, or to its parent if it has none.
- Nothing is pushed. Pull requests show the old commits until you
  `git stack push`, and `sync` ends by listing the branches whose remote
  branch is out of date.
- A branch whose remote branch was deleted without being merged (e.g. a
  closed pull request) is kept, with a warning, rather than dropping its
  commits.
- A merged branch whose remote branch got new commits after the merge is
  skipped with a warning, along with the branches stacked on it, since
  restacking it would replay its merged commits.
- A branch with broken metadata (e.g. its parent was deleted by hand) is
  skipped with a warning, along with everything stacked on it; the rest of the
  stack is still synced.
- The root isn't fast-forwarded if it has diverged from its upstream; `sync`
  warns instead.
- Branches checked out in another worktree are left alone, with a warning;
  see [Branches checked out in other worktrees](#branches-checked-out-in-other-worktrees).
- Needs a clean worktree, and refuses to run outside a stack.

### `git stack clean [--remote origin] [-d local|remote|both]`

Fetch with `--prune`, fast-forward every stack's root, and stop tracking merged
branches in every stack, moving their children onto their parent.

**When would I use it?** For housekeeping across every stack at once, e.g.
after pull requests in several stacks were merged, without having to resolve
conflicts in stacks you're not working on right now. For the stack you're
working on, use `sync`.

```sh
git stack clean
git stack clean -d local
```

- Nothing is restacked, so there are no conflicts. The children are rebased
  the next time you `sync` or `restack` their stack.
- `-d` works as for `sync`.
- Runs from any branch, even one that isn't in a stack. Needs a clean
  worktree.

### `git stack push [<branch>] [--remote origin] [--force]`

Force-push (with lease) every branch in the current stack, or in `<branch>`'s,
and set each one's upstream.

**When would I use it?** Whenever the remote should match your local branches:
after committing, and after a `restack` or `sync`. Use it instead of
`git push`, since restacking rewrites the branches' history. It also keeps pull
requests pointed at the right base, and records what it pushed, so `sync` won't
pull the old copies of your rebased commits back in.

```sh
git stack push
git stack push use-api          # push use-api's stack from anywhere
git stack push --force          # drop commits on the remote you didn't pull
```

- Each branch is pushed to a remote branch of the same name.
- Refuses, even with `--force`, if that remote branch exists but isn't the
  branch's upstream, since it's probably someone else's branch that happens to
  have the same name. Rename yours with `git stack rename`, or, if the remote
  branch is yours (say you pushed it with a plain `git push`), set it as the
  upstream with `git branch --set-upstream-to`.
- Merged branches are skipped.
- Refuses if a remote branch has commits the local one doesn't, so you don't
  overwrite a collaborator's work: run `git stack sync` to pull them in first,
  or pass `--force` to drop them. Either way, each lease is pinned to the
  remote-tracking ref that was checked, so if someone pushes after your last
  fetch, the push fails instead of overwriting their work.
- Refuses to push a branch whose remote branch was deleted, since that's
  usually a merged pull request `sync` hasn't seen yet. If it wasn't, run
  `git branch --unset-upstream <branch>` to push it again.
- With the [GitHub adapter](#github), points each open pull request at its
  stack parent (e.g. at `main` once the parent PR was merged), and prints a
  link to open one for each branch that has none.

## Scripting

These commands print one thing per line, for use in scripts, aliases, shell
prompts and editors. They only read git config, so they're fast, and they're
safe to run while another git-stack command is running. They fail with exit
status 1 for a branch that isn't in a stack.

They don't change any branch. The only thing they write, like every command,
is the result of a restack or pull you finished by hand after a conflict.

### `git stack parent [<branch>]`

Print the branch that `<branch>` (default: the current branch) is stacked on.

**When would I use it?** To compare a branch with what it's built on, the way
its pull request does:

```sh
git diff $(git stack parent)...         # what this branch's pull request changes
git log --oneline $(git stack parent)..
```

### `git stack base [<branch>]`

Print the commit `<branch>` (default: the current branch) was last rebased
onto: where its own commits start.

**When would I use it?** To act on just a branch's own commits. Unlike
`git stack parent`, this still works after the parent was squash-merged or
rewritten, since it doesn't depend on the parent's current history:

```sh
git log --oneline $(git stack base)..   # this branch's own commits
git rebase -i $(git stack base)         # reword or reorder them
```

- It's the commit `restack` replays the branch's commits from, so it's only
  out of date while a restack is stopped on a conflict.

### `git stack show-stack [-q] [<branch>]`

Print the branches in `<branch>`'s (default: the current branch's) stack,
bottom first, one per line. The stack's root (e.g. `main`) isn't included.

**When would I use it?** To run something on every branch in a stack, or, with
`-q`, to check whether a branch is in a stack at all:

```sh
for branch in $(git stack show-stack); do
    git switch --quiet "$branch" && make test || break
done

if git stack show-stack -q; then
    echo "in a stack"
fi
```

- `-q`/`--quiet` prints nothing, not even an error: it only exits with status
  0 if the branch is in a stack, and 1 if it isn't.
- As with the other commands, the stack includes branches beside the current
  one, not just those above and below it.

## When does `sync` consider a branch merged?

`sync`, `clean`, `restack`, `push` and `info` all use this check.

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

A branch whose remote branch was deleted without merging (a closed PR) fails
the first check, so it's not considered merged.

## Pulling commits others pushed

After restacking a branch, `sync` cherry-picks onto it any commits on its
upstream that it doesn't already have (fast-forwarding when it can). Commits
that are older versions of your own rebased commits, merge commits (e.g.
GitHub's "Update branch"), and changes already in the parent are skipped.

If a pull stops on a conflict and you abort it (or skip some of the commits),
the commits you didn't take aren't counted as pulled: the next `sync` offers
them again, and `push` refuses to drop them. To drop them anyway, run
`git stack push --force`.

## Conflicts

If a rebase or cherry-pick stops on a conflict, resolve it and run
`git rebase --continue` / `git cherry-pick --continue` (or `--abort`) as usual,
then rerun the git-stack command to finish the rest of the stack. The new
parent and base (and the pulled upstream commits) are recorded only if the
operation actually completed.

## Branches checked out in other worktrees

git won't rebase, check out or delete a branch that's checked out in another
worktree, and doing it anyway would change files under whatever is using that
worktree. So git-stack leaves such a branch alone, with a warning, and carries
on with the rest:

- A branch that needs restacking is skipped, along with the branches stacked
  on it. A branch that's already up to date isn't a problem.
- `sync` doesn't pull new commits from its upstream into it, and
  `restack --squash` doesn't squash it.
- `sync -d local` and `clean -d local` stop tracking it once it's merged, but
  don't delete it.
- A stack's root (e.g. `main`) isn't fast-forwarded.

Once that worktree is on another branch, rerun the command to restack or pull
what it skipped. A merged branch it didn't delete is no longer tracked, so
delete it yourself with `git branch -D`.

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
