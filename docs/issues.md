# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

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

## Group sync can lose its integration selector after the last work branch lands

If a group sync removes the last selected work branch above an integration
and the atomic push fails, the group can lose its connection to the
integration when no source was retired. To retry, name the integration
explicitly with `git stack sync <integration>` and include any surviving
stacks that still need to sync.

## A writing command run by a hook of a lock-holding git-stack waits forever

While a git-stack command holds the lock, git's hooks it triggers inherit
`GIT_STACK_LOCK_HELD`, and the reading commands skip settling rather than wait
for that lock. A command that changes metadata (`@exclusive`) doesn't check it:
a post-checkout hook running `git stack track ...` during `git stack branch`
waits for the lock its grandparent holds, which waits for the hook, so neither
finishes. It could fail fast instead, with a StackError, when
`lock_held_by_caller()` and trying the lock without waiting fails.

## GIT_STACK_LOCK_HELD is set in the whole process's environment

`StackCore.exclusive` sets it in `os.environ`, which costs:

- In a host process that uses git-stack as a library, the children of every
  thread inherit it while any thread holds the lock, so a git-stack another
  thread starts skips settling though it could have waited.
- Setting the environment races with C code calling `getenv` in other threads.
- A long-lived descendant (e.g. a daemon a hook starts) keeps the value after
  the lock is released, and a git-stack it starts later skips settling.

Either would avoid these: try the lock without waiting before skipping, so a
stale or foreign value costs nothing when the lock is free; or pass the
variable only to the processes git-stack itself starts, rather than through
`os.environ`.

## A test that deadlocks can hang the suite

`Repo.stack_process`'s timeout kills only its direct child, so a grandchild
holding the pipes open keeps it waiting, and the in-process paths (e.g.
`Repo.stack`, a thread waiting for the lock) have no timeout at all. A
regression to a deadlock, like the one `GIT_STACK_LOCK_HELD` prevents, then
hangs until CI's job limit instead of failing. Start test processes in their
own process group and kill the group on timeout, and add a fixture that calls
`faulthandler.dump_traceback_later` so a hung test prints where it's stuck.

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
