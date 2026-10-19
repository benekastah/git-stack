# Known issues

Most severe first.

## A remote branch with the same name is assumed to be yours

In a shared repository, someone else may already have pushed a branch with the
name you give a new branch (e.g. `fix-tests`). git-stack treats it as yours:

- `push` refuses because the remote branch "has commits that `<branch>`
  doesn't", and tells you to run `sync` or pass `--force`. `sync` can't help,
  since the new branch has no upstream to pull from, so `--force` is the only
  way forward, and it overwrites the other person's branch and their pull
  request.
- With the GitHub adapter, their open pull request counts as the branch's, so
  after a forced push `push` retargets it to your branch's parent. A merged one
  of theirs counts too if its head descends from your branch's base, which can
  make `info` and `sync` report your branch as merged (or merged with new
  commits).

`branch` doesn't check whether the name is taken on the remote. Prefixing
branch names with your username (e.g. `paul/fix-tests`) avoids this.

## `info` and the scripting commands can corrupt a running restack

`info`, `parent`, `base`, `show-stack`, `up` and `down` settle interrupted
restacks and pulls (`settled_branches`) whenever no rebase or cherry-pick is in
progress, which writes git config, though the README says the scripting
commands change nothing. If one runs while `restack` or `sync` is between
writing a branch's pending parent and base and starting its rebase (or, for a
pull, during the `git switch` before the cherry-pick), it discards the pending
keys, so the finished rebase is never recorded:

- the branch keeps its old base. For a branch on another stacked branch, that
  base is no longer in its history, so the next command fails with "recorded
  base isn't in its history (was it rebased by hand?)" until it's fixed with
  `track --base`.
- a pull isn't recorded in `stackPulled`.

The README suggests running the scripting commands from shell prompts and
editors, which is exactly what runs them in the background.

## `sync` always moves the whole stack onto the latest root

`sync` fast-forwards the root (e.g. `main`) and rebases every branch in the
stack onto it, and `restack` does the same with whatever the local root is.
There's no way to restack children onto a merged parent's replacement without
also moving them onto the newest `main`. On a busy repository, every
`sync` + `push` rewrites every branch in the stack, so each pull request reruns
CI, may lose approvals if the repository dismisses stale reviews, and loses
GitHub's "changes since your last review" view.

## `push` pushes before it retargets pull requests

`push` pushes every branch, then changes the bases of their pull requests. If a
parent's pull request was merged but its remote branch wasn't deleted, a child
rebased onto a newer `main` is briefly (or, if retargeting fails, until the
next `push`) compared against the old parent branch, so its pull request shows
every commit `main` gained since. Reviewers, and code owners of the files those
commits touch, may be notified or requested in that window.

## `restack --squash` commits with `--no-verify` and captured stdio

`squash_branch` skips commit-msg hooks (e.g. ones that add a ticket ID or
Change-Id), and because stdin/stdout are captured, GPG signing that prompts on
the terminal fails.

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

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.
