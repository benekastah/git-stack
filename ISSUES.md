# Known issues

Most severe first.

## A reverted change can look merged

A branch that was merged and then reverted on its parent still counts as
merged: the GitHub adapter trusts the merged pull request, and the git
adapter's patch-id check (`landed_as_patches`) finds a commit on the parent with
the same changes, ignoring the later revert. `sync` then untracks the branch (or
deletes it with `-d local`) and restacks its children onto a parent without its
changes, without any warning that the branch was reverted.

The README's "A merged branch was reverted" describes recovering by recreating
the branch and moving its children back with `restack --onto`.

## Old copies of rebased commits can be pulled back in

`unpulled_commits` recognizes old copies of the branch's own rebased commits by
the tip `git stack push` last pushed, the branch's reflog, and patch-id
equivalence. Old copies get cherry-picked back in as if a collaborator pushed
them if the rebase changed their patches (e.g. resolved conflicts) and they
were pushed either from another clone or machine, or with a plain `git push`
whose reflog entry has since expired.

## Rewriting a branch by hand makes `sync` pull its whole old history back in

A special case of the above, with a much bigger blast radius. If a branch's
commits are rewritten so that none of the old patches survive (split into
several branches, squashed, or rebuilt with `git commit-tree`) and the branch is
then re-tracked with `git stack track <parent> <branch> --base <sha>`, nothing
records the old remote tip as known. `sync` then cherry-picks every commit on
the branch's remote branch, all of them the user's own pre-rewrite commits, and
stops on conflicts in every file they touched.

Seen when splitting one large PR into a stack:

1. `machinery` (with `instrumentation` stacked on it) had been pushed. The
   remote `instrumentation` was 10 commits ahead of its old base: 7 old copies
   of `machinery`'s commits plus 3 of its own.
2. `machinery`'s content was split into 5 new commits on 5 branches built with
   `git commit-tree`, `machinery` was moved to one of them with `update-ref`,
   and everything was re-tracked with `git stack track`. `instrumentation` was
   tracked with `--base <old machinery tip>` and restacked; its 3 commits
   replayed (with conflicts resolved by hand).
3. `git stack sync` on `instrumentation` started cherry-picking all 10 remote
   commits and stopped on the first, with 13 conflicted files.

`unpulled_commits` didn't recognize them because:

- `instrumentation`'s reflog had a single entry, the restack's
  `rebase (finish)`, so the remote tip it used to point at wasn't in it. (Why
  the earlier entries were missing isn't known.)
- `stackPulled` held the new local tip, not the remote one.
- The rewrite changed every patch, so `--cherry-pick` matched none.

`machinery` itself wasn't pulled into, since its reflog still had its old tip.

The fix was `git cherry-pick --abort`, then `git stack push --force` to drop
the old commits from the remote. But the stop message only says to resolve and
`--continue`, and resolving would have applied every change a second time.

Possible fixes:

- `track --base` (or any `track` of a branch with a same-name upstream) could
  record the upstream tip as pulled, since re-tracking says the local branch is
  the one to keep.
- Before pulling, if every unpulled commit's author and committer is the
  current user, warn and skip instead, pointing at `push --force`.
- When a pull stops on a conflict, mention `git cherry-pick --abort` and
  `push --force` as the way out if the commits are old copies of your own.

## A branch can look merged when its parent made the same change

When the git adapter decides (with the GitHub adapter, that's a branch without
a pull request, or one stacked on a branch other than the default branch), a
branch counts as merged if its parent has commits with the same patches, even
if they came from someone else's pull request. On a busy repository that's
plausible for small, mechanical changes, such as a dependency bump or
regenerated file. `sync` and `clean` then untrack the branch, and delete it
with `-d local`, though its pull request was never merged. Its changes are in
the parent, but its own commits and messages are dropped.

## Two commands that restack at once can corrupt each other's records

Each command that restacks or pulls (`restack`, `sync`, and the others that
settle first: `track`, `untrack`, `rename`, `clean`, `push`) takes a pending
restack or pull it finds unfinished as aborted, and discards it. If another
such command is between writing its pending keys and starting its rebase or
cherry-pick, that rebase is never recorded, so the branch keeps its old base.
There's no lock to stop two from running at once.

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.
