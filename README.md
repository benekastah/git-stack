# git-stack

Stacked pull requests on GitHub (or any git host) without the conflicts that
squash merges cause. It's a command-line tool with nothing to sign up for: it
keeps what it knows in your repository's git config.

## Quick start

```sh
pipx install git+https://github.com/benekastah/git-stack   # see Install for uv and requirements

git switch main
git stack branch add-api        # a branch for PR 1
# ...commit...
git stack branch use-api        # a branch for PR 2, built on PR 1
# ...commit...
git stack push                  # push both
git stack prs                   # open draft PRs for both, each into the branch it's built on
```

Already have pull requests open, e.g. in a fresh clone or from before you used
git-stack? Track them as stacks instead:

```sh
git stack autocreate            # track your open PRs, each on the branch it merges into
```

After that, day to day:

```sh
git stack sync                  # catch up: drop merged PRs, rebase what's left, push
```

## Cheat sheet

| I want to... | Run |
| --- | --- |
| start a PR | `git stack branch <name>`, from `main` or from the PR it builds on |
| see my stacks and what needs doing | `git stack info` |
| update the PRs above one I just changed | `git stack restack`, then `git stack push` |
| catch up after a PR merged, or `main` changed | `git stack sync` |
| also delete merged branches | `git stack sync -d local` |
| put the stack on the latest `main` | `git stack sync --latest` |
| move to the next or previous branch in the stack | `git stack next` / `git stack prev` |
| build a PR on a different parent | `git stack restack --onto <parent>` |
| use git-stack with branches I already have | `git stack track main <branch>...` |
| set up stacks from my open PRs (e.g. in a new clone) | `git stack autocreate` |
| open draft PRs for my stack | `git stack push`, then `git stack prs` |
| rename a branch in a stack | `git stack rename <new>` |
| clean up merged branches in every stack | `git stack clean` |
| bring back a PR whose merge was reverted | `git stack unrevert <branch>` |
| read all of this in the terminal | `git stack --help` |

## Words used here

- **stack**: branches built on each other, e.g. `use-api` built on `add-api`,
  built on `main`. It can branch out, with several PRs built on the same one.
- **parent**: the branch a branch is built on. `add-api` is `use-api`'s parent.
- **root**: the branch at the bottom of a stack, usually `main`. git-stack
  doesn't manage it, but `sync` keeps it up to date.
- **tracked**: a branch git-stack knows about, because you made it with
  `git stack branch`, or added it with `git stack track` or
  `git stack autocreate`.
- **restack**: rebase a branch onto its parent's latest commit, replaying only
  the branch's own commits.
- **the current stack**: the whole stack the branch you're on is part of,
  including branches beside yours, not just the ones above it, and the other
  stacks in its group.
- **group**: stacks that were one stack until the branch at their bottom
  merged (see [Groups](#groups)).

## The problem

PR 2 is built on PR 1's branch. PR 1 is squash-merged into `main`. Now
`git rebase main` on PR 2 conflicts, because it replays PR 1's individual
commits on top of a `main` that already has them, squashed into one commit.
git-stack remembers where each branch's own commits start, so it only
replays those.

## Example workflow

Carrying on from the [Quick start](#quick-start), with `add-api` (PR 1) and
`use-api` (PR 2, built on PR 1).

Address review feedback on PR 1, then bring PR 2 up to date with it:

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

Once PR 1 is merged:

```sh
git stack sync -d local         # delete add-api, rebase use-api onto main, switch to it, push
```

`sync` only touches the current stack. To untrack (or delete) merged branches
in every stack without restacking anything, run `git stack clean`.

## Commands

Several commands act on the current stack: the whole stack the branch you're on
is part of, including branches beside yours. In this stack, running `restack`
from `pr2b` restacks `pr1`, `pr2a`, `pr2b` and `pr3`:

```
main
└── pr1
    ├── pr2a
    │   └── pr3
    └── pr2b *
```

### Groups

When a branch on `main` merges, the branches stacked on it each become the
bottom of a stack of their own. So that commands still treat them as one, as
before the merge, `sync` and `clean` put them in a group, named after the
merged branch. Here, `restack` from `pr2b` still restacks `pr2a`, `pr2b` and
`pr3` after `pr1` merges:

```
main
└── group pr1
    ├── pr2a
    │   └── pr3
    └── pr2b *
```

- A group's stacks stay in it however many of their branches merge later;
  their children join it too.
- A group can span roots, e.g. if you `restack --onto release` one of its
  stacks. Commands on it then cover every root it's on: `sync` fast-forwards
  each, and `prs` gives each root's pull requests their own stack section.
- `info` only shows a group once it has two or more stacks, with
  `group <name>` above its stacks on each root they're on. A group of one is
  just a stack.
- A branch leaves its group when it's untracked, so a group ends once its last
  branch is untracked.

Like git, every command accepts `-C <path>` before it to run as if started in
`<path>`, e.g. `git stack -C ~/src/app info`, and `git -C <path> stack ...`
works too.

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

### `git stack autocreate [--remote origin] [-n]`

Track your open pull requests on GitHub, drafts included, as stacks: each
pull request's branch on the branch it merges into.

**When would I use it?** When you have pull requests open but git-stack doesn't
know about their branches yet: in a fresh clone, on another machine, or for
pull requests you opened before you started using git-stack.

```sh
git stack autocreate -n         # see what it would do
git stack autocreate
# created add-api from origin/add-api, tracking it on main (#12)
# created use-api from origin/use-api, tracking it on add-api (#13)
```

- It only looks at your own open pull requests from branches in the
  repository: GitHub finds them with a search for pull requests by you, so
  other people's are never fetched, however many there are. Pull requests from
  forks don't count. It needs the [GitHub adapter](#github).
- A pull request without a local branch gets one, created from its remote
  branch, with that as its upstream. A local branch without an upstream gets
  its remote branch as its upstream.
- Branches that are already tracked are left alone. If one is tracked on a
  different parent than its pull request merges into, it says so; run
  `git stack track <parent> <branch>` if GitHub is right.
- If one of your pull requests merges into a branch you don't have locally
  (e.g. you built it on a coworker's branch), it's skipped, along with your
  pull requests built on it. `git switch` to that branch and rerun to track
  them.
- Each branch's base is found as for `git stack track`, after fast-forwarding
  the branches your pull requests merge into that aren't tracked (e.g. `main`)
  to their upstreams, as `sync` does. If a parent's commits
  were rewritten after its child was built on them (e.g. the parent was
  rebased and force-pushed from another machine), the child's base can come
  out too old, and its next restack replays the parent's old commits; set it
  with `git stack track <parent> <branch> --base <sha>`.
- `-n`/`--dry-run` prints what it would do without changing anything (besides
  fetching).

### `git stack untrack [<branch>...]`

Stop tracking branches (default: the current one), without deleting them or
changing their commits.

**When would I use it?** When you want git-stack to stop managing a branch but
keep it around: a long-lived branch you don't want `sync` to rebase, or a pull
request you abandoned but might come back to. You don't need it for merged
branches, since `sync` and `clean` untrack those for you.

```sh
git stack untrack experiment
git stack untrack old-poc old-poc-2
```

- Branches stacked on it stay on it, so it becomes the root of their stack,
  like `main`, and `sync` and `clean` fast-forward it to its upstream. To move
  them elsewhere, switch to each and run `git stack restack --onto <parent>`.
- To stop tracking a branch that was merged, use `sync` or `clean`, which also
  move its children down.
- Nothing is untracked unless every branch is tracked.

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

### `git stack info [--json] [--offline]`

Show every tracked stack as a tree, with its root (e.g. `main`) on top and
the branches built on it below.

**When would I use it?** When you want to see your stacks and what needs doing:
which branches depend on which, which need a restack, and which were merged.
It's a good first step before `restack`, `sync` or `push`.

```sh
git stack info
# main
# └── add-api *                   https://github.com/acme/widgets/pull/12
#     ├── use-api (needs restack) https://github.com/acme/widgets/pull/13
#     └── docs (merged)           https://github.com/acme/widgets/pull/14
```

- `*` marks the current branch.
- To draw each stack upside down, with its root at the bottom (as
  `git log --graph` and other stacking tools do), run
  `git config --global stack.orientation root-at-bottom`. The default is
  `root-at-top`.

  ```
      ┌── docs (merged)
      ├── use-api (needs restack)
  ┌── add-api *
  main
  ```
- `(needs restack)`: it's missing its parent's latest commits (or, for a
  branch on `main`, it conflicts with `main`). Run `git stack sync` or
  `git stack restack`.
- `(merged)`: it was merged into its parent (see
  [When does `sync` consider a branch merged?](#when-does-sync-consider-a-branch-merged));
  `sync` or `clean` will stop tracking it.
- `(merged, with new commits)`: it was merged, but its remote branch got
  commits after the merge. `sync` skips it, and the branches stacked on it,
  until you sort it out.
- `(merged, then reverted)`: it was merged, then its parent reverted some or
  all of its changes. `sync`, `clean`, `restack`, `push` and `prs` leave it,
  and the branches stacked on it, alone (see
  [A merged branch was reverted](#a-merged-branch-was-reverted)).
- `(parent <name> missing)`: its parent branch was deleted by hand; fix it
  with `git stack track <parent>`.
- `group <name>`: the stacks below it are in the group `<name>` (see
  [Groups](#groups)).
- `(<name> was reverted)`: it was stacked on `<name>` until that merged, and
  its parent has since reverted some of `<name>`'s changes, so its pull
  request is missing them. `sync` and `clean` say so too (see
  [A merged branch was reverted](#a-merged-branch-was-reverted)).
- With the [GitHub adapter](#github), each branch's pull request (open or
  merged) is linked in a column at the end of its line: blue if it's open,
  grey if it's a draft, and magenta if it was merged.
- On a terminal, the parts of each line are colored. Like git, it follows the
  `color.stack` setting (or `color.ui`): set it to `never` to turn colors
  off, or `always` to keep them when piping. `NO_COLOR` also turns them off.
- `info` doesn't fetch, and compares each branch with its local parent, so a
  merge on the remote only shows up once the local parent has it. `git fetch`
  alone isn't enough, since it doesn't update `main`; run `git stack clean`
  first, which fetches and fast-forwards `main` (and stops tracking merged
  branches, but doesn't restack anything).
- `--json` prints the same information for scripts: a list with an object for
  each branch, each stack's root first, parent before child. `parent` and
  `status` are `null` for a root, and `status` is one of `up-to-date`,
  `needs-restack`, `merged`, `merged-with-new-commits`, `reverted` or
  `parent-missing`.
  `reverted_parent` is the `<name>` of `(<name> was reverted)`, or `null`.
  `group` is the group of a branch on a stack's root, when `info` shows it
  (once the group has two or more stacks), or `null`.
  `pr` is the branch's pull request's `number`, `url` and `state` (`open`,
  `draft` or `merged`), or `null` if it has none (or for a root, or without
  the GitHub adapter).

  ```sh
  git stack info --json
  # [
  #   {
  #     "name": "main",
  #     "parent": null,
  #     "status": null,
  #     "current": false,
  #     "pr": null
  #   },
  #   {
  #     "name": "add-api",
  #     "parent": "main",
  #     "status": "up-to-date",
  #     "current": true,
  #     "pr": {
  #       "number": 12,
  #       "url": "https://github.com/acme/widgets/pull/12",
  #       "state": "open"
  #     }
  #   },
  #   ...
  # ]
  ```

- With the [GitHub adapter](#github), `info` asks GitHub about each branch's
  pull request, which takes about a second. `--offline` skips that: it checks
  for merges like the `git` adapter does, and shows no links. For a shell
  prompt or an editor, the [scripting commands](#scripting) below only read
  git config, and are fast.

### `git stack next`

Switch to the branch stacked on the current one.

**When would I use it?** To move to the next PR in the stack, e.g. from PR 1
to PR 2 after committing to PR 1, without remembering the branch's name.

```sh
git switch main
git stack next                  # to add-api
git stack next                  # to use-api
```

- If several branches are stacked on the current one, it lists them and
  leaves you where you are; pick one with `git switch`.
- Works from a stack's root (e.g. `main`), as long as only one branch is
  stacked on it.

### `git stack prev`

Switch to the branch the current one is stacked on.

**When would I use it?** To move to the previous PR in the stack, e.g. back to
PR 1 to address review feedback while you're working on PR 2.

```sh
git stack prev                  # from use-api to add-api
git stack prev                  # to main
```

- From the bottom of a stack, it switches to the stack's root (e.g. `main`).

### `git stack restack [--onto <parent>] [--squash] [--latest]`

Rebase every branch in the current stack onto its parent, bottom-up, except
that a branch on the stack's root (e.g. `main`) only moves if it conflicts
with the root's latest commit.

**When would I use it?** After you commit to a branch that has other branches
stacked on it, e.g. to address review feedback on PR 1, so the branches above
it include the change. It works locally, without fetching or pushing. With
`--onto`, when a branch should be stacked somewhere else, e.g. PR 2 turns out
not to need PR 1. With `--squash`, when a restack keeps stopping on the same
conflict, commit after commit. With `--latest`, when you want the stack on
the latest `main` anyway, e.g. because the repository requires pull requests
to be up to date before they're merged.

```sh
git stack restack
git stack restack --onto main           # move the current branch onto main, then restack
git stack restack --squash              # squash each branch first, then restack
git stack restack --latest              # also move the stack onto main's latest commit
```

- Only the branches' own commits are replayed (from their recorded base), so a
  parent that was squash-merged doesn't cause conflicts. See
  [How it works](#how-it-works).
- `--onto` retargets only the current branch, onto another local branch. The
  branches stacked on it come along.
- `--latest` moves the branches on the stack's root onto its latest commit
  even if they don't conflict with it (and the rest of the stack with them).
- `--squash` first squashes each branch in the current stack into one commit,
  keeping all the messages and the oldest commit's author and date, so a
  conflict is resolved once per branch instead of once per commit. It commits
  as `git commit` does, running your pre-commit and commit-msg hooks and
  signing if you've set that up. If the commit fails (e.g. a hook rejects it),
  that branch is left as it was.
- Merged branches are skipped and their children restacked onto their parent;
  `sync` or `clean` removes them from the stack.
- Doesn't fetch or push. Needs a clean worktree, and refuses to run outside a
  stack.
- If a rebase stops on a conflict, see [Conflicts](#conflicts).
- A branch checked out in another worktree is skipped, with a warning; see
  [Branches checked out in other worktrees](#branches-checked-out-in-other-worktrees).

### `git stack sync [--remote origin] [-d local|remote|both] [--latest] [--no-push]`

Bring the current stack up to date with the remote:

1. Fetch with `--prune`.
2. Fast-forward the stack's root (e.g. `main`) to its upstream.
3. Go through the stack's branches, each before the branches stacked on it:
   - stop tracking a merged one, moving its children onto its parent;
   - restack the rest, like `restack` does: a branch on the root only moves
     if it conflicts with the root's new commits, or with `--latest`;
   - and pull in commits others pushed to it (see
     [Pulling commits others pushed](#pulling-commits-others-pushed)).
4. Push the branches that differ from the remote branch they already have,
   like `push` does.

**When would I use it?** Your everyday command for catching up: after a pull
request in the stack was merged, when `main` changed in a way that conflicts
with your stack, or when someone else pushed to one of your branches. It
pushes at the end, so there's nothing to follow it with. To catch up locally
without touching the remote, use `restack`, or `sync --no-push`.

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
- Only branches that already have a remote branch of the same name, as
  `git stack push` sets it, are pushed. A branch that was never pushed isn't,
  and no pull requests are opened: use `git stack push` and `git stack prs`
  for those. It never forces, so it fails if `push` would refuse. As with
  `push`, the branches are pushed in one atomic push.
- Nothing is pushed if a restack or pull stops on a conflict; rerun `sync`
  once it's resolved.
- `--no-push` leaves the remote alone, and ends by listing the branches whose
  remote branch is out of date.
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
  `sync` pushes the rest, then fails, listing the branches it couldn't push
  for that reason (unless it was run with `--no-push`).
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
after committing, after a `restack`, and for a branch `sync` doesn't push
because it was never pushed. Use it instead of `git push`, since restacking
rewrites the branches' history. It also keeps pull
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
- The branches are pushed in one atomic push: if any of them can't be pushed
  (e.g. because its remote branch changed since your last fetch), none are.
- Refuses to push a branch whose remote branch was deleted, since that's
  usually a merged pull request `sync` hasn't seen yet. If it wasn't, run
  `git branch --unset-upstream <branch>` to push it again.
- With the [GitHub adapter](#github), points each open pull request at its
  stack parent (e.g. at `main` once the parent PR was merged), and lists the
  branches that have none, which `git stack prs` opens.

### `git stack prs [--remote origin] [-n]`

Open a draft pull request on GitHub for each branch in the current stack that
has none, into its stack parent, bottom-up.

**When would I use it?** After pushing a new stack, or new branches in one,
to open their pull requests with the right bases.

```sh
git stack push
git stack prs -n                # see what it would open
git stack prs
# opened draft pull request #12 for add-api into main: https://github.com/...
# opened draft pull request #13 for use-api into add-api: https://github.com/...
```

- Each pull request's title is its branch's oldest commit's subject, and its
  body the repository's pull request template, `pull_request_template.md`
  (in any case) in `.github/`, the repository's root or `docs/`, as on the
  remote's default branch. Templates in a `PULL_REQUEST_TEMPLATE/` directory
  aren't used, as GitHub only uses them when asked for by name.
- Branches that already have a pull request (open or merged), merged branches,
  and branches with no commits of their own are skipped. An open pull request
  that isn't into its branch's stack parent (say it was opened from the link
  GitHub prints on push, which targets `main`) is pointed at it, as `push`
  does.
- Then it adds the stack to the end of each open pull request's description,
  as a tree from the stack's root, with that pull request marked, and keeps
  it up to date each time it runs. Pull requests merged since stay in it, as
  a history, though `sync` stops tracking their branches. A stack with only
  one pull request gets none. Text you add outside the stack is kept, but
  edits to the stack itself are overwritten.

  ```
  <!-- git-stack -->
  <!-- Kept up to date by `git stack prs`, which overwrites any edits to this section. -->
  ---
  **Stack**
  - `main`
    - #12 (merged)
      - #13 👈
        - #14
  <!-- /git-stack -->
  ```

- Refuses unless every branch in the stack has been pushed with
  `git stack push` and matches its remote branch, so each pull request shows
  what you have locally, as of your last fetch. Also refuses if the stack's
  root (e.g. `main`) isn't on the remote, since the pull requests at the
  bottom of the stack merge into it.
- If opening one fails, it says which it had opened already; rerun it once the
  problem is fixed to open the rest.
- Needs the [GitHub adapter](#github).
- `git stack pr` is an alias.
- `-n`/`--dry-run` prints the pull requests it would open, with their titles,
  without opening them.

### `git stack unrevert [<branch>] [--remote origin]`

Rebuild a branch (default: the current one) whose merge into its parent was
reverted, as commits on its parent's tip that revert the reverts, and move the
branches that were stacked on it back onto it.

**When would I use it?** When `info` shows a branch as
`(merged, then reverted)`, or a branch that was stacked on it as
`(<branch> was reverted)`, and you want to bring its changes back, e.g. to fix
whatever got it reverted and merge it again.

```sh
git stack unrevert add-api
# rebuilt add-api on main, reverting 1 revert
# note: run `git stack restack` to move use-api onto it
# note: run `git stack push` and `git stack prs` to open a new pull request for it
```

- Only the reverts still in effect are reverted, newest first: if only some
  of the branch's commits were reverted, only those come back, and a revert
  that was itself reverted is left alone. The commits get git's usual
  messages for reverting a revert; amend them as you like.
- The branch's old commits are dropped, since its parent already has them (or
  a squash of them) in its history. Its new base is its parent's tip.
- It works whether or not `sync` or `clean` already stopped tracking it, and
  even if they deleted it, as long as a branch stacked on it is still tracked:
  that branch recorded what it was when it merged. If it still exists, it's
  tracked again, unless it changed since it merged, in which case it's left
  alone (rename it to have it recreated instead). If it was deleted, it's
  recreated. Once its commits are garbage-collected (by default, two weeks
  after it was deleted, if nothing else has them), it can't be.
- The branches that were stacked on it are moved back onto it, which changes
  their pull requests' bases the next time you push. Like the ones still
  stacked on it, `restack` or `sync` moves them onto its new commits.
- Its merged pull request can't be reopened, and no longer counts as the
  branch's. If its remote branch was deleted when it was merged, its upstream
  is unset, so `git stack push` pushes it again; then `git stack prs` opens a
  new pull request.
- If a revert stops on a conflict, resolve it and run `git revert --continue`;
  the next git-stack command records the new base. To put the branch back as
  it was instead, run `git revert --abort`, then `git reset --hard` to the
  commit the error message names.
- Works on the local parent, without fetching, so run `git stack sync` first
  if the revert only happened on the remote. Needs a clean worktree.

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

## Conflicts

If a rebase, cherry-pick or revert stops on a conflict, resolve it and run
`git rebase --continue` / `git cherry-pick --continue` /
`git revert --continue` (or `--abort`) as usual,
then rerun the git-stack command to finish the rest of the stack. The new
parent and base (and the pulled upstream commits) are recorded only if the
operation actually completed.

`restack`, `sync` and `clean` refuse to start with uncommitted changes, but not
with untracked files. If one is in the way of a commit being replayed, git
stops without a conflict and says which file. Move or delete it, then run
`git rebase --continue`, or for a pull, `git cherry-pick --abort` (git's
`--continue` would skip the commit that couldn't be applied) and rerun
`git stack sync`.

## Branches checked out in other worktrees

git won't rebase, check out or delete a branch that's checked out in another
worktree, or that a rebase stopped partway through there, and doing it anyway
would change files under whatever is using that worktree. So git-stack leaves
such a branch alone, with a warning, and carries on with the rest:

- A branch that needs restacking is skipped, along with the branches stacked
  on it. A branch that's already up to date isn't a problem.
- `sync` doesn't push a branch it skipped for this reason, and fails once it
  has pushed the rest.
- `sync` doesn't pull new commits from its upstream into it, and
  `restack --squash` doesn't squash it.
- `sync -d local` and `clean -d local` stop tracking it once it's merged, but
  don't delete it.
- A stack's root (e.g. `main`) isn't fast-forwarded.
- A restack or pull of it that stopped on a conflict there stays pending, so
  rerunning git-stack there after `git rebase --continue` (or
  `git cherry-pick --continue`) still records it.

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

If `pr1` is merged and then reverted on `main` (all of it, or some of its
commits), `info` shows it as `(merged, then reverted)`, and `sync`, `clean`,
`restack`, `push` and `prs` leave it, and the branches stacked on it, alone.

If `sync` or `clean` had already stopped tracking `pr1` (and moved `pr2` onto
`main`) by the time it was reverted, `info` shows `pr2` as
`(pr1 was reverted)`, and `sync` and `clean` warn about it.

Either way, to bring its changes back, rebuild it on `main` with
[`unrevert`](#git-stack-unrevert-branch---remote-origin), which reverts the
reverts, then restack its children onto it and open a new pull request:

```sh
git stack unrevert pr1             # moves pr2 back onto it, if it had moved
git stack restack                  # move pr2 onto the new pr1
git stack push
git stack prs                      # open a new one for pr1
```

To drop it instead, move `pr2` onto `main` (from `pr2`,
`git stack restack --onto main`), then stop tracking it with
`git stack untrack pr1`. If it was already untracked, stop the warning about
it with `git config --unset branch.pr2.stackMergedParent`.

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

A branch stacked directly on a stack's root (e.g. `main`) is only restacked
when it conflicts with the root's latest commit (or with `--latest`), not
every time the root moves. A pull request only shows the branch's own
changes either way, and leaving it alone means its CI doesn't rerun and its
reviews aren't dismissed just because `main` moved on. Branches stacked on
other branches are restacked whenever their parent changes, since they need
its new commits.

When a merged branch on a stack's root stops being tracked, each branch that
was stacked on it gets its group (or its name) in `branch.<name>.stackGroup`.

When a merged branch stops being tracked, each branch that was stacked on it
records what it was, in `branch.<name>.stackMergedParent`: its name, the
parent it merged into, its base and tip, and (with the GitHub adapter) its pull
request's merge commit. That's how a later revert of it is noticed, and how
`unrevert` can bring it back even once it's untracked or deleted.

Checking whether a branch conflicts with its parent, whether its changes
already landed there, and whether they were reverted since, can take a second
or more for a branch far behind `main` in a big repository. Since the answers
only depend on the commits involved, git-stack remembers the last ones, with
those commits, in `branch.<name>.stackConflictCheck`,
`branch.<name>.stackLandedCheck`, `branch.<name>.stackRevertedCheck` and
`branch.<name>.stackMergedParentRevertedCheck`, and only checks again once one
of them changes.

## GitHub

If your repository is on github.com and [`gh`](https://cli.github.com) is
installed and logged in (`gh auth login`), git-stack uses GitHub automatically;
there's nothing to set up. The rest of this section is the details.

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
`git@github-work:org/repo.git`) and `gh` is installed, and `git` otherwise.
Set it to `github` for a GitHub Enterprise host, or to `git` to turn the
GitHub integration off:

```sh
git config stack.adapter git
```

With the GitHub adapter:

- A branch's pull request is the one whose head is a remote branch of the same
  name in the same repository, however it was opened, as long as that remote
  branch is the branch's upstream (as `git stack push` sets it). A pull request
  from someone else's branch that happens to have the same name doesn't count.
  An open one wins; otherwise the newest merged one counts, if its head
  descends from the commit the branch is stacked on (so one merged from an
  earlier branch of the same name doesn't). Closed pull requests are ignored.
  If a merged pull request's last commit was never fetched (say GitHub's
  "Update branch" made it right before the merge, and the branch was deleted
  on merge), `sync`, `clean` and `push` fetch it from the pull request. Until
  then, `info` and `restack`, which don't fetch, can't tell that pull request
  is the branch's, and check the branch like the `git` adapter does.
- `git stack push` changes the base of the stack's open pull requests to match
  their stack parents, e.g. to `main` once the parent PR was merged. It does
  that before pushing, so a pull request is never compared with its old base
  after its branch was rebased onto the new one, which would show every commit
  the new base gained since. (A pull request whose parent isn't on GitHub
  yet is pointed at it after the push instead.)
- `git stack prs` opens draft pull requests, into each branch's stack parent,
  points the existing open ones at their stack parents too, and keeps the
  stack in each open one's description. (The link GitHub prints when a branch
  is first pushed targets the default branch instead; the next `prs` or
  `push` retargets a pull request opened from it.)
- A pull request that GitHub says was merged counts as merged once its merge
  commit is in the local parent branch (`sync` fetches it), even if the parent
  changed the merged lines since, or the merge changed them.
- If pull requests can't be looked up (e.g. `gh` isn't logged in, its token
  isn't authorized for your organization's SSO, or GitHub can't be reached),
  `sync`, `clean`, `push` and `prs` stop before changing anything, since
  checking for merges locally could untrack a branch whose pull request is
  still open, or restack one that was merged. `info` and `restack` warn and
  check for merges like the `git` adapter does. To run a command with the `git` adapter just
  this once, use `git -c stack.adapter=git stack <command>`.

## When does `sync` consider a branch merged?

`sync`, `clean`, `restack`, `push`, `prs` and `info` all use this check.

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

A merged branch counts as reverted instead if, since it was merged, its parent
got a commit that undoes the whole branch, one of its commits, or (with the
GitHub adapter) its pull request's merge commit, the way `git revert` does, and
no later commit redid it. Changes undone by hand, or a revert that was edited
(e.g. to resolve a conflict), aren't recognized, so the branch still counts as
merged.

## Pulling commits others pushed

After restacking a branch, `sync` cherry-picks onto it any commits on its
upstream that it doesn't already have (fast-forwarding when it can). Commits
that are older versions of your own rebased commits, merge commits (e.g.
GitHub's "Update branch"), and changes already in the parent are skipped.

Only an upstream that's the remote branch of the same name, as `git stack push`
sets it, is pulled from. A branch made with `git switch -c <branch> origin/main`
has `origin/main` as its upstream, and `main`'s commits aren't the branch's to
take, so nothing is pulled into it until `git stack push` gives it its own.

If a pull stops on a conflict and you abort it (or skip some of the commits),
the commits you didn't take aren't counted as pulled: the next `sync` offers
them again, and `push` refuses to drop them. To drop them anyway, run
`git stack push --force`.

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

### The man page

`git stack --help` opens git-stack's man page (git runs `man git-stack` for
it); `git stack -h` and `git stack <command> --help` print git-stack's own
shorter help. pipx installs the page into `~/.local/share/man`, which `man`
searches when `~/.local/bin` is on your `PATH`. uv doesn't install man pages,
so link it there yourself:

```sh
mkdir -p ~/.local/share/man/man1
ln -s "$(uv tool dir)/git-stack/share/man/man1/git-stack.1" ~/.local/share/man/man1/
```

### From a local clone

To run git-stack from a clone of this repository, e.g. to use changes before
they're merged, see [Development](#development).

## Development

### Setting up

Clone the repository, then run its `dev-install.sh`:

```sh
git clone https://github.com/benekastah/git-stack ~/src/git-stack
~/src/git-stack/dev-install.sh
```

`dev-install.sh` installs git-stack in editable mode into a virtualenv in
`~/.local/share/git-stack/venv`, so changes to the clone take effect without
reinstalling. It links the `git-stack` command into `~/.local/bin` and the man
page into `~/.local/share/man/man1` (an editable install doesn't install it),
makes the clone use the [git hooks](#git-hooks) in `git_hooks/`, and makes
`git blame` skip [reformatting commits](#skipping-reformatting-in-git-blame).
It also sets up the clone's own virtualenv, `.venv`, with the dev
dependencies (e.g. Ruff and mypy) that the [checks](#checks) and the hooks
use. It's safe to rerun, e.g. when `pyproject.toml` changes, to pick up a new
command-line entry point.

To check the install worked, `git stack -h` should print git-stack's usage,
and `git stack --help` should open the man page. `dev-install.sh` warns if
either won't work:

- If `git stack` isn't found, `~/.local/bin` isn't on your `PATH`; add it in
  your shell's startup file.
- If `git stack --help` says "No manual entry for git-stack", `man` isn't
  searching `~/.local/share/man`. Most systems search it because `~/.local/bin`
  is on your `PATH`; otherwise, add `export MANPATH="$HOME/.local/share/man:"`
  to your shell's startup file (the trailing `:` keeps the system's manual
  directories too).

### Checks

Run every check (shellcheck on the shell scripts, that the Python code is
formatted with [Ruff](https://docs.astral.sh/ruff/), Ruff's linter with its
default rules plus the ones `pyproject.toml` adds (e.g. a limit on functions'
cyclomatic complexity), mypy in strict mode, then the tests) with:

```sh
./check.sh
```

If the formatting check fails, format the code with:

```sh
.venv/bin/ruff format
```

If the linter fails, `.venv/bin/ruff check --fix` fixes what it can.

It runs `dev-venv.sh` first, which creates `.venv` if it's missing and installs
the `dev` dependency group from `pyproject.toml` into it (with
`pip install --group dev`, which needs pip 25.1+), and reinstalls it whenever
`pyproject.toml` has changed since. Arguments are passed to pytest, so `./check.sh -k sync` runs only the
tests whose names contain `sync`. git-stack itself still has no runtime
dependencies.

The tests run in parallel, with one worker per CPU (pytest-xdist's `-n auto`).
Set `PYTEST_XDIST_AUTO_NUM_WORKERS` to use another number of workers, e.g.
`PYTEST_XDIST_AUTO_NUM_WORKERS=4 ./check.sh`, or pass `-n 0` to run them one
at a time, e.g. to debug a failing test.

### Git hooks

`dev-install.sh` makes the clone use the hooks in `git_hooks/`. To turn them on
without it, run `git config core.hooksPath git_hooks`. They bring `.venv` up to
date with `dev-venv.sh` when they need it, so a new dev dependency (say, after
pulling a change that adds one) is installed by the next commit or push that
uses it.

- `pre-commit` formats the staged Python files with Ruff, and regenerates the
  man page, `man/git-stack.1`, and adds it to any commit that changes this
  README. It works on what's staged, so a partly staged file commits just its
  staged part, formatted.
- `pre-push` runs `./check.sh` before every push that pushes commits. Skip it
  with `git push --no-verify`.

### Skipping reformatting in `git blame`

`.git-blame-ignore-revs` lists commits that only reformat code, so `git blame`
attributes their lines to the commits before them. GitHub's blame view reads
it by itself; `dev-install.sh` sets `blame.ignoreRevsFile` so git does too. To
set it without `dev-install.sh`, run:

```sh
git config blame.ignoreRevsFile ':(optional).git-blame-ignore-revs'
```

(Leave out `:(optional)` if your git doesn't support it. Then `git blame` fails
with a commit from before the file existed checked out.) A commit listed there
only matches if it keeps its hash when merged, so merge a reformatting pull
request with a merge commit rather than squashing it.

### Generating the man page

`man/git-stack.1` is generated from this README by `man/build.py`, with pandoc
(from the `pypandoc_binary` dev dependency) and the filter `man/git-stack.lua`,
which leaves out the Install and Development sections. The pre-commit hook
keeps it up to date; without the hook, regenerate it with
`.venv/bin/python man/build.py`. The tests fail if it's out of date.
