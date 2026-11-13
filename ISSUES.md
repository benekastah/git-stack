# Known issues

Most severe first.

## Aborting a restack onto an ancestor can record it as completed

`settle_restack` treats the pending parent tip being an ancestor of the branch
as proof that a rebase finished. If `restack --onto` targets an ancestor
already in the branch's history, that is true before the rebase starts. When
the rebase stops on a conflict and the user runs `git rebase --abort`, the
next command that settles metadata records the new parent and base anyway.
Reproduced with `main -> parent -> child`, where both branches edit the same
line: `restack --onto main` from `child` conflicts, aborting restores the old
branch, and `info` then changes its recorded parent from `parent` to `main`.
Later restacks can replay the old parent's commits as if they belong to the
child. Record enough pre-rebase state to distinguish an abort from completion.

## Skipping a pulled commit can cause it to be dropped from the remote

`settle_pull` checks completion using sets of commit identities (author, author
date, and subject). Two different upstream commits can share an identity. If
the first conflicts, the user skips it, and the second is applied, the set
comparison says both were picked and records the whole upstream tip as pulled.
Reproduced with two commits with the same author, date, and subject but changes
to different files: after `git cherry-pick --skip`, the next `sync` force-pushed
the branch without the skipped change. Compare the individual commits or their
occurrences, and do not mark the upstream as fully pulled after a skip.

## Changed remote commits can be mistaken for a rebase of local work

`take_upstream_rebase` decides that an upstream branch is a rebased copy of
the local branch by comparing each commit's author, author date, and subject,
without comparing its changes. If another clone amends a commit's contents
without changing those identity fields and force-pushes it, `sync` (or `push`
without `--force`) can move the local branch to the changed remote commit. The
local commit is no longer on the branch, although it may be recoverable from
the reflog. Reproduced by amending a pushed commit's file in another clone with
the same subject and author date, then running `sync`: the local branch moved
to the amended content. Refuse or ask for an explicit choice when matching
identities have different changes; conflict resolution during a real rebase
needs special care.

## `unrevert` rewrites shared stacks without an override

`unrevert --stack` moves the named branches and calls `restack_all` directly,
without `ensure_owned` or an `--allow-shared` option. It can rebase branches
marked shared even though `restack` and `sync` would refuse. Reproduced with
a shared stack on a merged and reverted branch: `unrevert` moved and rebased
its child without asking for an override.

## Shared ownership can disappear when a stack splits

Ownership is stored per branch. `set_parent` copies shared ownership to the
branch being moved, but not to its existing descendants. If an owned stack is
attached under a shared branch, then the shared branch at their join is
untracked or merged, its descendants can become a separate stack whose stored
ownership is still owned. Reproduced by attaching an owned `pr1 -> pr2 -> pr3`
stack to a shared branch and untracking `pr1`: `pr2` then reports owned.
That permits subsequent rewrites without `--allow-shared`.

## A failed import can leave shared branches marked owned

`import` creates and tracks branches in `apply_import` before
`classify_imported_stacks` marks them shared. If the process stops, a Git
command fails, or a config write fails in between, those branches remain
tracked without a shared marker and are treated as owned. Reproduced by
failing the author lookup after importing someone else's PR; `restack`, `sync`,
and `push` would then lack the intended shared-stack protection. Compute the
classification before applying the plan, and ensure partial application fails
closed.

## `clean -d` deletes branches in shared stacks without an override

`clean` processes every tracked stack and doesn't call `ensure_owned`. With
`-d local`, `-d remote`, or `-d both`, it can delete merged branches marked
shared without `--allow-shared`. Reproduced with `clean -d both`, which deleted
both the local and remote branch. The deletion is still subject to the normal
merge check and remote lease, but the shared ownership choice is ignored.

## Retargeting can unstack unrelated pull requests

With `--rebuild-stack`, a PR base change that GitHub rejects calls `unstack`
on its entire GitHub stack. Unlike the separate rebuild path, this doesn't
check for unmerged PRs outside the selected local stack. Reproduced with an
unrelated open PR in the same GitHub stack: `prs --rebuild-stack` removed it
from that stack too. The option authorizes rebuilding, but doesn't identify the
additional PRs before doing so.

## A rebuild dry run can omit the unstack it would need

`prs --rebuild-stack --dry-run` skips PR retargeting. If the GitHub stack has
the right PR numbers but a PR's base differs from its local parent, the dry run
reports no rebuild, while the real command must unstack that GitHub stack to
retarget the PR. Reproduced by changing the stored base of a PR in a stack
without changing its membership. The preview should report the affected stack
and its PRs before the user opts in.

## Old copies of rebased commits can be pulled back in

`unpulled_commits` recognizes old copies of the branch's own rebased commits by
the tip `git stack push` last pushed, the branch's reflog, and patch-id
equivalence. Old copies get cherry-picked back in as if a collaborator pushed
them if the rebase changed their patches (e.g. resolved conflicts) and they
were pushed either from another clone or machine, or with a plain `git push`
whose reflog entry has since expired.

Re-tracking with `track --base` now guards the upstream tip seen at that time;
this issue remains for rebases where that guard was not recorded.

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
