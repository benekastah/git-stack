# Known issues

Most severe first.

## `sync` always moves the whole stack onto the latest root

`sync` fast-forwards the root (e.g. `main`) and rebases every branch in the
stack onto it, and `restack` does the same with whatever the local root is.
There's no way to restack children onto a merged parent's replacement without
also moving them onto the newest `main`. On a busy repository, every
`sync` + `push` rewrites every branch in the stack, so each pull request reruns
CI, may lose approvals if the repository dismisses stale reviews, and loses
GitHub's "changes since your last review" view.

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

## Untracked files aren't checked before restacking

`restack`, `sync` and `clean` refuse to run with uncommitted changes, but
ignore untracked files (`ensure_clean_worktree` passes
`--untracked-files=no`). If a commit being replayed adds a file that's
untracked in the worktree, git stops the rebase to avoid overwriting it, and
git-stack reports it as a conflict ("stopped on a conflict. Resolve it, run
`git rebase --continue`"), though there's nothing to resolve: the untracked
file has to be moved out of the way first.

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.
