# Issues

What's wrong with what's built: bugs, and gaps against the rules in `AGENTS.md` and
`PROJECT_AGENTS.md`, most severe first. New work is in [roadmap.md](roadmap.md). When a change
fixes one, remove it in the same change; when a change leaves something wrong, add it.

## `import` fast-forwards the trunks of stacks it doesn't import

`fast_forward_import_trunks` gets every pull request it looked up, before the
branches and `--pr` narrow them to the stack trees named, and before `-f` is
checked. So the untracked branches the other trees merge into (e.g. a local
`release/1.x`) are fast-forwarded too, even when nothing is imported. It's
what `sync` would do to them anyway, but it's a change nobody asked for.
Fast-forwarding only the trunks of the steps it creates or tracks would fix
it.

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

## `import --pr` says a deleted author's pull request isn't open

`open_pull_request_author` returns None both when the pull request isn't open
and when its author's account was deleted (GitHub's ghost user), so naming an
open pull request by a deleted account with `--pr` fails with
"#N isn't an open pull request". It should look the pull request up anyway
(its author would show as "a deleted account", as for one found by search).

## GitHub's username format is checked in `stack.py`

`GITHUB_LOGIN`, which `import_stacks` checks `--author` against, is GitHub's
format, but it lives in `git_stack/stack.py` rather than in
`git_stack/adapters/github.py` with the rest of what's GitHub-specific (e.g.
`pull_request_number`). It should move there, still checked before the pull
requests are looked up, so its error doesn't get the "check `gh auth status`"
hint.

## An older git-stack can't finish a restack this one stopped

A restack that stops on a conflict is recorded in one setting,
`stackPendingRestack`, which versions before it don't read. Finishing the
rebase with an older git-stack leaves that record behind, and the next run
of a newer one may then apply it over what the older one did since (e.g. a
`track` onto another parent). Finish a stopped restack with the version
that started it, or a newer one.
