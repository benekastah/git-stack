# Known issues

Most severe first.

## Deleting a merged branch's remote branch can delete someone else's

`untrack_merged` deletes (with `-d remote` or `-d both`) whatever the merged
branch's upstream is, not only the branch of the same name that
`git stack push` sets up. `pull_upstream` already refuses other upstreams for
this reason, but `untrack_merged` doesn't check `tracks_same_name`.

A branch made with `git switch -c feature origin/release` tracks
`origin/release`. If it's tracked with `git stack track release feature`, never
pushed with `git stack push` (which would change its upstream), and its change
lands in `release`, then `git stack sync -d both` runs
`git push origin :refs/heads/release` and deletes the shared branch.
Reproduced with the git adapter. Without `-d remote`, the note it prints
suggests `git push origin --delete release` instead.

Only the same-name remote branch should be deleted (or suggested for
deletion); any other upstream should be left alone, as if there were none.

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

## A change undone some other way can look like a revert

Revert detection compares patch-ids without context lines, so a revert still
matches after later commits change the lines around it. The flip side is that
any later commit on the parent that undoes exactly one of the branch's
changes in the same file (say, setting a version number back), for whatever
reason, counts as reverting it. The branch is then left alone as
`(merged, then reverted)` instead of being untracked, and the branches that
were stacked on it are warned about. Nothing is lost, and
`clean --ignore-revert <branch>` (or `sync`'s) says it wasn't a revert, but
until then every command skips it.

## Reusing a merged branch's name can join unrelated stacks into one group

When a branch on a stack's root merges, `remove_merged` puts its children in a
group named after it (if it wasn't in one already), without checking whether
that group already exists. Short names like `fix` or `cleanup` get reused, so
when a second, unrelated `fix` merges, its children join the first `fix`'s
group. From then on `sync`, `restack`, `push` and `prs` on either stack cover
both, e.g. `sync` restacks and pushes the other stack's branches too.
Reproduced: after two `fix` branches merged one after the other, each with a
branch stacked on it, `git stack show-stack` on one listed both.

`git stack group remove` from one of them separates them again. A fix could
pick a name that isn't in use yet (e.g. `fix-2`), or record something unique
to the merge alongside the name.

## The branch config cache could miss a write

`GitClient.branch_config` rereads `.git/config` only when its inode, mtime or
size has changed since it last read it. `git config` writes a new file and
renames it into place, so each write normally gets a new inode. But a
filesystem that reuses inode numbers (e.g. ext4) could give the file the inode
it had two writes earlier, and two writes in the same timestamp tick that
leave the file the same size (e.g. overwriting a commit ID with another) would
then look like no change, and git-stack would keep using the old values. Not
reproduced; on btrfs, where inode numbers aren't reused, it can't happen.

Comparing the file's contents (or a hash of them) instead of its metadata
would rule it out at little cost, since the file is small.

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.

## `prs` says "the current stack" for named stacks

When every branch looks merged, `git stack prs` prints "nothing to open: every
branch in the current stack looks merged", even when stacks were named with
branches or `-g`. `git stack push` already words this by what was named.

## No Windows support

git-stack is written for Unix-like systems. The git-stack lock
(`StackCore.exclusive`) uses `fcntl.flock`, which Windows doesn't have, so it
won't even start there, and the tests rely on symlinks. Low priority until
someone needs it: `msvcrt.locking`, or a lock file created with `O_EXCL`,
would replace `flock`.
