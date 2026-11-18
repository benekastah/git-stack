# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

## A second clone can't push a branch with a managed draft base

Ownership of a managed draft base is recorded only in the local
`stackDraftBase` setting. A clone without it (a second machine, or the setting
lost) finds `refs/heads/git-stack/draft-base/<hash>` already on the remote and
`DraftBase.plan` raises "already exists; refusing to adopt it", which fails the
whole atomic push, unrelated branches included. It could adopt the ref when it
matches the recorded boundary (or the PR's current base), and say so otherwise.

## Managed draft bases can be left on the remote

Only the path that drops to one dependency, or none, deletes the managed ref.
Closing the draft's PR, deleting or untracking the combined branch, or `clean`
leaves it behind, and nothing else reads `stackDraftBase` to find it.

## `sync` changes a draft's bases without an event

`reduce_draft_dependencies` reparents the branch and replaces its secondary
bases without emitting an event, unlike `move_off_deleted_parents`
(`MovedOffDeletedParent`). It also only runs for an open GitHub draft, so the
same graph syncs differently depending on PR state, and with bases
`(main, x)` where `x` already includes `main` it collapses to `x` though
nothing merged (its docstring says "once one lands").

## One ready-for-review PR blocks every push

If a PR that targets a managed base is marked ready for review,
`managed_draft_updates` and `retiring_draft_bases` raise "convert it to a draft
before pushing", which stops the push of the whole stack, unrelated branches
included. Neither error is tested. It could skip that branch and warn.

## Smaller draft-base gaps

- `managed_draft_updates` (used by `push`) doesn't check that the recorded
  `stackReplayTips` still match the parents' tips, as `plan_draft_base` (used
  by `prs`) does.
- `sync_branch` assigns `parent` and overwrites it before use.
- `DraftBase`'s guards (invalid record, a different upstream, the ref moved
  outside git-stack, already exists, push failure) and these `StackClient`
  paths are untested: the early returns in `reduce_draft_dependencies`, a PR no
  longer a draft, retiring when the parent isn't pushed, and a failed delete of
  the managed ref.

## `clean` then `restack` copies the trunk's history into a multi-base branch

If a branch has bases `(first, second)` and `second` merges into `main`,
`clean` replaces `second` with its own base, so the bases become
`(first, main)`. The next `restack` replays `main` as a secondary base with
nothing excluded (`Stacks._without_dependency`, `StackCore.replay_root_forks`),
so every `main` commit since `first` forked is cherry-picked onto the branch as
new work. Its PR then shows them, and merging `first` later duplicates them.
A replacement base that is the primary base's root (or an ancestor of it)
should be dropped instead.

## A broken replay journal can't be cleared

While a replay is stopped, `stack.pendingReplay` makes every exclusive command
except `restack` and `sync` refuse to run. Some errors keep the journal and
recur on every rerun: the worktree it stopped in was deleted ("resume the
pending replay in ..."), a commit was made during the stop ("moved
unexpectedly"), a local branch is named `git-stack-replay` (so the scratch
branch can't be created), or the record fails validation. The only way out is
`git config --unset stack.pendingReplay` and deleting `refs/git-stack/replay/*`
by hand. There should be an abort (e.g. `restack --abort-replay`) that restores
the checkout and clears the journal, documented in the README's conflicts
section. Discarding a journal (a `git rebase --abort`, or a crash right after a
stage is marked in progress) also emits no event, and drops the remaining
queue silently.

## A recoverable publish failure discards resolved conflicts

If the target branch is checked out in another worktree, or a parent gets a
commit, after the user resolved several replay stages, `replay.publish` raises
and `finish` clears the journal, so the next run starts over and hits the same
conflicts. An untracked file blocking the final switch keeps the journal
instead. The "checked out" case should keep it too, and clear it only when the
parent tips or boundary actually changed.

## `sync` keeps a merged base without saying so

A merged branch that a multi-base graph depends on is skipped in `sync_branch`
with a bare `return`: no event, and it isn't added to `state.skipped`. Its
single-base children are then restacked onto the merged branch rather than
moved onto `main`, and pushed with their PRs still on it, so adding an
unrelated multi-base branch changes how they sync. It should say the branch was
merged and needs `clean`, and either move its primary children as
`remove_merged` would or skip them.

## `bases` doesn't replace `base` for a multi-base branch

`base` refuses a multi-base branch and the README deprecates it for `bases`, but
`bases` prints the source tips, while `stackBase` is the boundary before the
branch's own commits. So `git log $(git stack base)..` has no working
replacement on a combined branch, and nothing prints where the own commits
start (needed by `rebase -i`). `base` could return the recorded base for every
branch.

## Multi-base branches miss the revert-check cache

`GitAdapter.reverted` stores its result under one key per branch, though its
inputs include the parent's tip. With several parents each check overwrites
the other's entry, so both miss on every run: about 7 extra git processes per
parent for each `info`, `push`, `sync` and `restack`, including a `log -p`
over the parent's history. The parent should be part of the key. Graph
branches also run one uncached `merge-base --is-ancestor` per command
(`graph_branch_status` through `recorded_base`).

## Replaying a multi-base branch is slow

Each commit copied from a secondary base is its own stage, costing about 15 git
processes (about 0.4 s on Windows): `switch`, `update-ref`, `branch --force`,
two journal config writes, `rebase`, the copy verification, and a re-read of
refs and config, since a config write counts as changing refs. Rebasing each
base's run of commits in one stage, or keeping the journal in a file (or not
treating it as a ref change), would cut most of it.

## Smaller multi-base gaps

- The replay worktree check compares paths as strings (`replay.py`); use
  `git.same_file`.
- `--empty=ask` is deprecated and warns at every own-commit stage; `stop` needs
  git 2.45, newer than the README's 2.38 minimum.
- `clean` reports a dependent as "moved onto" a new parent (and lists it in
  `children`) when the removed branch was only its secondary base.
- The README says in one place that a multi-base branch appears under each
  parent in `info`, and in another once under its first base (the code does
  the latter). The new `info` notes and `restack --onto` being repeatable
  aren't documented.
- `track --on X` on a multi-base branch says to use `--on`.
- Connecting stacks in different groups with `track --on` or `restack --onto`
  merges them silently; `group add` and `branch -g` refuse without `--move`.
- Nothing documents how an older git-stack sees a multi-base branch (a missing
  parent), that its `rename`, `clean`, `untrack` or `track` can leave a stale
  or cyclic base list that every newer command then rejects, or how to repair
  it with `git config`.
- `tests/test_branch_names.py` has no case for `parents`, `bases`,
  `track --on`, `restack --onto A --onto B`, or a multi-base restack, `info`
  or `sync`. The replay journal's validation and error paths, the pending-replay
  refusal, `base`/`bases` errors, graph status of a missing, broken or reverted
  parent, the blocked graph sync, and the cycle check in `Stacks` are untested.
- `Stacks` clears its primary-path and group indexes on every edge change, so a
  loop of `reparent` or `remove_merged` calls is quadratic (0.10 s vs 0.03 s on
  a 300-branch chain); negligible next to a git process.

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

It also only watches the common `.git/config`, so settings changed in another
config file (`git config --worktree` with `extensions.worktreeConfig`, an
included file, or, for `GitClient.config_value`'s `stack.adapter`,
`stack.orientation` and remote URLs, the global config) aren't seen until
something writes `.git/config`. The command line reads them afresh each time
it runs; a script that keeps a `StackClient` across such a change doesn't.

`forget` rereads the config rather than trusting the cache, so untracking a
branch can't leave its settings behind, but every other read still trusts it.
Comparing the file's contents (or a hash of them) instead of its metadata
would rule out the missed write at little cost, since the file is small.

## An older git-stack can't finish a restack this one stopped

A restack that stops on a conflict is recorded in one setting,
`stackPendingRestack`, which versions before it don't read. Finishing the
rebase with an older git-stack leaves that record behind, and the next run
of a newer one may then apply it over what the older one did since (e.g. a
`track` onto another parent). Finish a stopped restack with the version
that started it, or a newer one that understands its recovery record.
Multi-base replay uses a separate `stack.pendingReplay` journal. Versions
without multi-base replay support cannot finish it. If a replay was started
with the earlier cherry-pick workflow, use that version to continue or abort
it before upgrading to a version that stages multi-base replay with rebase.
