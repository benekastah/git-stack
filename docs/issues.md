# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

## A newer system pandoc fails the pre-push hook

`dev.py man --check` (run by `dev.py static`, so by the pre-push hook) fails with "man:
man/git-stack.1 is out of date with README.md" when the committed page is right. It makes the
page with pypandoc, which uses the highest-versioned pandoc among the one on PATH and the one
`pypandoc_binary` bundles (PATH wins a tie), unless `PYPANDOC_PANDOC` is set, which replaces the
search. Here the system pandoc is 3.11 and the pinned binary bundles 3.9, so the page comes out
different.

Workaround, never skipping the hook: set `PYPANDOC_PANDOC` to the bundled binary, as in
`PYPANDOC_PANDOC=<repo>/.venv/lib/python3.15/site-packages/pypandoc/files/pandoc git push`.

The fix belongs in python-template's `dev.py` (it has no issues file of its own, only
`template/docs/issues.md`, the copy projects get), and this repo shouldn't patch it locally:
make `dev.py man` use pypandoc's bundled pandoc, or fail with a message naming both versions, so
a newer system pandoc can't change the page, and every project gets the fix through
`copier update`.

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

## The config cache doesn't see other config files

`GitClient.branch_config` rereads the config only when the contents of the
common `.git/config` have changed or git-stack itself has written a setting,
so settings another process changes in another config file (`git config
--worktree` with `extensions.worktreeConfig`, an included file, the file
`GIT_CONFIG` names, or, for `GitClient.config_value`'s `stack.adapter`,
`stack.orientation` and remote URLs, the global config) aren't seen until
something writes `.git/config` or git-stack writes a setting. The command
line reads them afresh each time it runs; a script that keeps a `StackClient`
across such a change doesn't.

Not every write by git-stack forces a reread: an unset that git refuses
because the setting has several values keeps the cache. So if another process
added a second value to a setting in the file `GIT_CONFIG` names, git-stack
keeps reading the old one.

## A killed group sync or pull can lose its integration selector

A group sync or pull records that the group should still select an integration
only when it ends or an error passes through without publishing it. If the
process is killed outright (e.g. SIGKILL, or a signal Python doesn't turn into
an exception) after untracking the group's last work on the integration, before
that point, nothing is recorded, and a later group sync leaves the integration
unpublished. Retry with `git stack sync <integration>`, naming it.

## A push URL that failed outright can look finished

When a remote has several push URLs and the push fails, git-stack reports each
destination from py-git-client's parse of `git push --porcelain`, which takes a
destination's report as finished when Git prints `Done` after it. Git ends a
destination that failed outright (e.g. with an unpacker error) without `Done`,
but a later destination that succeeds can print a bare `Done` that completes the
earlier one's report too. git-stack then takes that report as finished: the
error's partial-publication summary can list that destination's refs as
updated, rejected or up to date rather than uncertain, and it can offer a
rollback command for it. The command's leases still require the tips Git
reported, so it fails rather than overwrite anything else, but it suggests
undoing an update that may never have happened. This predates pinning
py-git-client e599be1, which only documents it. git-stack never passes
`--quiet` to `git push`, so the related problem the library also documents
(Git leaving successful destinations out of the report) doesn't apply.

## A hook's long-lived descendant inherits GIT_STACK_LOCK_HELD

A process a hook starts that outlives the command (e.g. a daemon) keeps the
variable, so a git-stack it starts later doesn't wait if it finds the lock
taken, by another command that would have finished: a reading command skips
settling, and one that changes metadata fails. With the lock free, it takes
it as usual. Telling a stale value from a live one would need more than the
holder's process ID (e.g. a token the lock file holds).

## Commands start processes for every branch, many of them one at a time

The operation tests exercise stacks of 1, 3 and 10 branches. On the largest,
an up-to-date `restack` starts three Git processes, `untrack` starts 32, and
replaying the branches above a change takes 92. The replay and metadata writes
still run one branch at a time.

## Syncing, importing and opening pull requests start many processes for a long stack

On a new 10-branch stack, `push` starts 26 Git processes and no `gh` processes.
`push --prs` starts 26 Git and seven `gh` processes. The Git work includes
two config writes per published branch. Complex local histories can still
require individual merge and title lookups. Native GitHub stack linking uses
a separate REST write per chain, with live protection before each write.
Syncing after the bottom PR merges takes 174 Git processes and five `gh`
calls. Importing 10 PRs takes 76 Git processes and two `gh` calls. With no
work to do, `sync` takes six Git processes and one `gh` call at all three
tested stack sizes.

## An older git-stack can't finish a restack this one stopped

A restack that stops on a conflict is recorded in one setting,
`stackPendingRestack`, which versions before it don't read. Finishing the
rebase with an older git-stack leaves that record behind, and the next run
of a newer one may then apply it over what the older one did since (e.g. a
`track` onto another parent). Finish a stopped restack with the version
that started it, or a newer one.
