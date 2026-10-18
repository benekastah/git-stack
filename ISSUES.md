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
checking the branch's reflog, plus patch-id equivalence. Old copies get
cherry-picked back in as if a collaborator pushed them when:

- the branch was pushed from another clone or machine,
- the reflog entry has expired, or
- the rebase changed the patch (e.g. resolved conflicts), so patch-ids differ.

## Branches checked out in other worktrees

Restacking, switching to or deleting a branch that's checked out in another
worktree fails partway through a `sync`, `clean` or `restack`. (A stack's root,
e.g. `main`, checked out elsewhere is left alone with a warning rather than
fast-forwarded.)

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

## Branch and parent names are resolved like any revision

Most git calls pass bare names (`git rev-parse <parent>`,
`git merge-tree <parent> <branch>`), so a tag with the same name as a branch
wins over the branch.

## `restack --squash` commits with `--no-verify` and captured stdio

`squash_branch` skips commit-msg hooks (e.g. ones that add a ticket ID or
Change-Id), and because stdin/stdout are captured, GPG signing that prompts on
the terminal fails.
