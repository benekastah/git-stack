# Known issues

Most severe first.

## A branch can look merged when its parent made the same change

When the git adapter decides (with the GitHub adapter, that's a branch without
a pull request, or one stacked on a branch other than the default branch), a
branch counts as merged if its parent has commits with the same patches, even
if they came from someone else's pull request. On a busy repository that's
plausible for small, mechanical changes, such as a dependency bump or
regenerated file. `sync` and `clean` then untrack the branch and can delete its
local and/or remote branch with `-d local`, `-d remote`, or `-d both`, though
its pull request was never merged. Its changes are in the parent, but its own
commits and messages can be lost from the branch.

## Old copies of rebased commits can be pulled back in

`unpulled_commits` recognizes old copies of the branch's own rebased commits by
the tip `git stack push` last pushed, the branch's reflog, and patch-id
equivalence. Old copies get cherry-picked back in as if a collaborator pushed
them if the rebase changed their patches (e.g. resolved conflicts) and they
were pushed either from another clone or machine, or with a plain `git push`
whose reflog entry has since expired.

`pull` and `sync` now refuse unseen commits when the upstream diverges from
the last known tip, or when that tip is unknown and the branches diverge,
unless `--rewritten-upstream` is given. Re-tracking with `track --base` also
guards the upstream tip seen then. If rewritten copies are appended to a
history that still contains the last known tip, they can still look like new
collaborator commits; there is no reliable distinction from Git history alone.

## `import` fast-forwards the trunks of stacks it doesn't import

`fast_forward_import_trunks` gets every pull request it looked up, before the
branches and `--pr` narrow them to the stack trees named, and before `-f` is
checked. So the untracked branches the other trees merge into (e.g. a local
`release/1.x`) are fast-forwarded too, even when nothing is imported. It's
what `sync` would do to them anyway, but it's a change nobody asked for.
Fast-forwarding only the trunks of the steps it creates or tracks would fix
it.

## A failed push can leave pull requests retargeted or unstacked

Before pushing, the GitHub adapter retargets pull requests whose new bases
already exist remotely. With `--rebuild-stack` it can also unstack their
GitHub stack. A later retarget or the atomic Git push can fail, leaving those
GitHub changes in place even though no branch was pushed. In particular, an
unstack followed by a rejected push can leave the pull requests unstacked.
The error reports that nothing was pushed, but does not describe the remaining
GitHub changes or how to recover them.

Preflight all possible failures, and report partial GitHub changes and recovery
instructions if an operation still fails; restoring them automatically would
need to account for concurrent edits on GitHub.

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
would replace `flock`. Before a 1.0 release, either support Windows or clearly
state in the installation requirements that it is not supported.

## GitHub's username format is checked in `stack.py`

`GITHUB_LOGIN`, which `import_stacks` checks `--author` against, is GitHub's
format, but it lives in `git_stack/stack.py` rather than in
`git_stack/adapters/github.py` with the rest of what's GitHub-specific (e.g.
`pull_request_number`). It should move there, still checked before the pull
requests are looked up, so its error doesn't get the "check `gh auth status`"
hint.
