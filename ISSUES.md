# Known issues

Most severe first.

## `import` can track a shared branch, and `sync` rebase it onto `main`

When a pull request merges into a branch that isn't local and has no pull
request, `import` tracks that branch too, on the branch `find_parent` says
it's built on. A long-lived shared branch is exactly that, e.g. `develop` in
git-flow or `release/1.x`: its parent comes out as `main` (or whatever its
history leads to), and it's tracked with `-f` (which allows other people's
commits), or without it if all of its commits are the user's (e.g. a solo
project's `develop`). From then on, `git stack sync` from a branch on top
restacks `develop` onto `main`, and `git stack push` force-pushes it (with
lease), rewriting the shared branch. `plan_one_without_pr` and
`find_parent` are where it happens;
`test_import_with_force_tracks_someone_elses_branch_without_a_pr` exercises
the same path with a short branch.

A branch that merged pull requests have targeted should count as a trunk: be
created from its remote branch but left untracked, as a root like `main`, so
`sync` only fast-forwards it. That takes one GraphQL query per branch without
a pull request. Rules based on commits (e.g. "has merge commits") would
misfire on feature branches that merged `main`.

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

## `import` fast-forwards the trunks of stacks it doesn't import

`fast_forward_import_trunks` gets every pull request it looked up, before the
branches and `--pr` narrow them to the stack trees named, and before `-f` is
checked. So the untracked branches the other trees merge into (e.g. a local
`release/1.x`) are fast-forwarded too, even when nothing is imported. It's
what `sync` would do to them anyway, but it's a change nobody asked for.
Fast-forwarding only the trunks of the steps it creates or tracks would fix
it.

## `prs` says "the current stack" for named stacks

When every branch looks merged, `git stack prs` prints "nothing to open: every
branch in the current stack looks merged", even when stacks were named with
branches or `-g`. `git stack push` already words this by what was named.

## `import --pr` says a deleted author's pull request isn't open

`open_pull_request_author` returns None both when the pull request isn't open
and when its author's account was deleted (GitHub's ghost user), so naming an
open pull request by a deleted account with `--pr` fails with
"#N isn't an open pull request". It should look the pull request up anyway
(its author would show as "a deleted account", as for one found by search).

## `import --pr` says it's looking up your pull requests

With only `--pr` (and no `--author`), `import` looks up the pull requests of
the authors of the ones named, not the user's, but its
`looking-up-pull-requests` event has no author, so it prints "looking up your
open pull requests on GitHub".

## No Windows support

git-stack is written for Unix-like systems. The git-stack lock
(`StackCore.exclusive`) uses `fcntl.flock`, which Windows doesn't have, so it
won't even start there, and the tests rely on symlinks. Low priority until
someone needs it: `msvcrt.locking`, or a lock file created with `O_EXCL`,
would replace `flock`.

## GitHub's username format is checked in `stack.py`

`GITHUB_LOGIN`, which `import_stacks` checks `--author` against, is GitHub's
format, but it lives in `git_stack/stack.py` rather than in
`git_stack/adapters/github.py` with the rest of what's GitHub-specific (e.g.
`pull_request_number`). It should move there, still checked before the pull
requests are looked up, so its error doesn't get the "check `gh auth status`"
hint.
