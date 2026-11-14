# Known issues

Most severe first.

## A stale remote HEAD can bypass default-branch protection

The push and sync safeguards trust the local `refs/remotes/<remote>/HEAD` if
it exists, without checking whether the server's default branch has changed.
Git fetch does not update this symbolic ref automatically. A feature branch
tracking the server's new default can therefore be pushed to it by `sync`
without `--force`. The merged-branch deletion path uses the same stale marker:
with `-d remote` it can delete the server's actual default branch if the server
permits it and the stack root has a different name.

Reproduced both outcomes in disposable repositories whose local remote HEAD
pointed to `old` while the server's HEAD pointed to `main`. Resolve the current
server default before pushing or deleting, and fail safely if it cannot be
verified. The check must not treat the cached symbolic ref as authoritative.

## A merged GitHub PR can hide merge-only changes made afterward

When GitHub reports a pull request merged, `GitHubAdapter.unmerged_commits`
checks for later work with `--no-merges`. A subsequent merge commit can contain
changes absent from both its parents, such as conflict resolutions. If its
other commits are already in the merged PR or parent, git-stack reports the
branch merged despite those changes. `sync -d both` or `clean -d both` can
then delete both local and remote copies of the branch.

Reproduced through `sync` and `clean` with both local-only and remote-only
merge-resolution changes. Check for unaccounted merge commits or their full
tree changes before treating a GitHub-merged branch as safe to untrack or
delete; the upstream pull guard alone doesn't cover this merge-detection path.

## Patch-based merge detection can discard merge-only local changes

The git adapter's patch-based landing check compares the parent's commits with
the branch's non-merge commits. If the parent independently acquired all those
patches, it can classify the branch as merged even though a merge commit on
the branch made an additional change that is absent from the parent.
`sync -d local` can then delete the only branch containing that change.

Reproduced through `sync` with a merge-only edit that remained absent from
the parent. Check the complete branch changes, including merge commits,
before treating matching non-merge patches as evidence that it has landed.

## Invalid upstream metadata can let `push --force` overwrite another branch

`remote_branch_name` falls back to the local branch name when its configured
merge ref is not under `refs/heads/`. The ownership check treats the mere
presence of a matching `branch.<name>.remote` as proof that this fallback is
the branch's upstream, even if Git resolves no upstream. Consequently,
`push --force` can overwrite an unrelated remote branch with the same name.

Reproduced with `branch.feature.remote=origin` and
`branch.feature.merge=refs/tags/unrelated`: Git reported no upstream, but
`push --force` replaced `origin/feature`. Require a valid, resolved remote
branch ref before considering an existing destination to be the configured
upstream; `--force` must not override the unrelated-branch check.

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

## `sync` and `pull` still fetch `origin` when the stack uses another remote

When no `--remote` is given, `sync` infers the stack's configured upstream
remote, but its fetch set still includes the default argument `origin`. In a
clone created with a different remote name (for example, `upstream`) and no
`origin`, both `sync` and `pull` fail trying to fetch the nonexistent remote,
even when the stack and its root track `upstream`. Reproduced with a
single-remote clone. Fetch only the remotes actually needed by the selected
stack and roots when no remote was explicitly named.

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

## `pull` recovery messages can direct users to a pushing command

`pull` runs the local portion of `sync`, but shared-stack refusals and some
interrupted cherry-pick instructions still tell the user to rerun `git stack
sync`. The re-track guard likewise recommends `sync --pull-tracked-upstream`.
Following those instructions can push branches, although the user chose `pull`
to keep the remote unchanged. Preserve the original command in recovery and
refusal messages, and suggest the corresponding `pull` invocation.

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
