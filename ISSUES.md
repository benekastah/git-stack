# Known issues

## `push` on an untracked branch pushes every stack

`stack_of` on an untracked branch such as `main` returns everything stacked on
it, so `git stack push` from `main` force-pushes every tracked branch in every
stack. The README says it pushes "every branch in the current stack". This also
recreates remote branches for merged branches that are still tracked, which
can reopen PR branches and trigger CI.

Possible fix: refuse to push from an untracked branch, as `restack --squash`
does.

## A reverted change can look merged

The git adapter's patch-id check (`landed_as_patches`) counts a branch as merged
if its parent has a commit with the same changes, even if a later commit on the
parent reverted it. `sync` then untracks the branch (or deletes it with
`-d local`) and restacks its children onto a parent without its changes. The
GitHub adapter falls back to this check for branches whose pull request isn't
merged.

## No `untrack` command

The only way to drop a branch from a stack without deleting it is
`git config --unset branch.<name>.stackParent` (plus the other `stack*` keys).

## Old copies of rebased commits can be pulled back in

`unpulled_commits` recognizes old copies of the branch's own rebased commits by
checking the branch's reflog, plus patch-id equivalence. Old copies get
cherry-picked back in as if a collaborator pushed them when:

- the branch was pushed from another clone or machine,
- the reflog entry has expired, or
- the rebase changed the patch (e.g. resolved conflicts), so patch-ids differ.

## Fork workflows aren't supported

- `sync` fetches only `--remote` (default `origin`). If `main` tracks
  `upstream/main`, it's never fetched, so `main` isn't fast-forwarded and merges
  aren't detected.
- `push` checks the lease against `refs/remotes/<remote>/<branch>` and pushes to
  a remote branch of the same name, ignoring the configured upstream. A branch
  whose remote branch has a different name gets a new remote branch, and
  `--set-upstream` switches its upstream to that.
- `info` and `restack` take the GitHub repository from `origin`, and `sync` and
  `push` from `--remote`, which for a fork is the fork, not the repository its
  pull requests are on.

## `restack --squash` commits with `--no-verify` and captured stdio

`squash_branch` skips commit-msg hooks (e.g. ones that add a ticket ID or
Change-Id), and because stdin/stdout are captured, GPG signing that prompts on
the terminal fails.

## `track` can pick a stale base for later branches

For `git stack track main pr1 pr2`, pr2's base is `merge-base pr1 pr2`. If pr1
was rebased after pr2 branched from it, that merge-base is too old, and the
next restack replays pr1's old commits onto pr2.

## `track` leaves pending keys alone

`cmd_track` doesn't call `settle` or clear `stackPending*` keys. Leftover keys
from an interrupted restack could later overwrite the parent and base `track`
just wrote.

## `sync` rewrites every tracked stack

Documented, but surprising: `sync` restacks and pulls into every tracked stack,
not only the current one.
