# Known issues

Most severe first.

## A reverted change can look merged

The git adapter's patch-id check (`landed_as_patches`) counts a branch as merged
if its parent has a commit with the same changes, even if a later commit on the
parent reverted it. `sync` then untracks the branch (or deletes it with
`-d local`) and restacks its children onto a parent without its changes. The
GitHub adapter falls back to this check for branches without a merged pull
request, except those stacked directly on the default branch that have an open
pull request.

## Old copies of rebased commits can be pulled back in

`unpulled_commits` recognizes old copies of the branch's own rebased commits by
checking the branch's reflog, plus patch-id equivalence. Old copies get
cherry-picked back in as if a collaborator pushed them when:

- the branch was pushed from another clone or machine,
- the reflog entry has expired, or
- the rebase changed the patch (e.g. resolved conflicts), so patch-ids differ.

## `track` can pick a stale base for later branches

For `git stack track main pr1 pr2`, pr2's base is `merge-base pr1 pr2`. If pr1
was rebased after pr2 branched from it, that merge-base is too old, and the
next restack replays pr1's old commits onto pr2.

## `track` leaves pending keys alone

`cmd_track` doesn't call `settle` or clear `stackPending*` keys. Leftover keys
from an interrupted restack could later overwrite the parent and base `track`
just wrote.

## Branches checked out in other worktrees

`fast_forward_trunks` moves a root with `git update-ref` unless it's checked out
in the current worktree, so a `main` checked out in another worktree is moved
under it, and that worktree then shows the difference as staged changes.
Restacking, switching to or deleting a branch checked out elsewhere fails
partway through a `sync` or `clean`.

## `git stack branch` can give the new branch its parent's upstream

`branch` runs `git switch --create` without `--no-track`. With
`branch.autoSetupMerge=inherit`, a branch created on a pushed `pr1` gets
`origin/pr1` as its upstream (with `always`, local `pr1`). Until the first
`push` resets it, `sync` lists it as unpushed, merge detection looks at the
parent's remote branch, and if `origin/pr1` is deleted the new branch counts as
"upstream gone" and can be untracked (or deleted with `-d local`).

## A parent that isn't a local branch breaks pull requests

`track` and `restack --onto` accept any commit as the parent, e.g.
`origin/main` or a SHA. With the GitHub adapter, `push` passes it as the pull
request's base (`gh pr create --base origin/main` fails), retargets existing
pull requests to it, and never matches it against the default branch.

## Fork workflows aren't supported

- `sync` and `clean` fetch only `--remote` (default `origin`). If `main` tracks
  `upstream/main`, it's never fetched, so `main` isn't fast-forwarded and merges
  aren't detected.
- `push` checks the lease against `refs/remotes/<remote>/<branch>` and pushes to
  a remote branch of the same name, ignoring the configured upstream. A branch
  whose remote branch has a different name gets a new remote branch, and
  `--set-upstream` switches its upstream to that.
- `info` and `restack` take the GitHub repository from `origin`, and `sync`, `clean`
  and `push` from `--remote`, which for a fork is the fork, not the repository its
  pull requests are on.

## SSH host aliases aren't recognized as GitHub

`github_repo` takes the host from the remote URL, so a remote like
`git@github-work:org/repo.git` (a `Host` alias in `~/.ssh/config`) gets the git
adapter. Setting `stack.adapter github` doesn't help: `gh` is called with
`--hostname github-work`, the lookup fails, and `push` fails after pushing.

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.

## `restack --squash` commits with `--no-verify` and captured stdio

`squash_branch` skips commit-msg hooks (e.g. ones that add a ticket ID or
Change-Id), and because stdin/stdout are captured, GPG signing that prompts on
the terminal fails.

## No `untrack` command

The only way to drop a branch from a stack without deleting it is
`git config --unset branch.<name>.stackParent` (plus the other `stack*` keys).

## Opened pull requests skip the repository's template

`gh pr create --fill` builds the body from the commits, so the repository's
pull request template isn't used.
