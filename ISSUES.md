# Known issues

Most severe first.

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

## The current worktree can look like another one on a case-insensitive filesystem

`GitClient.other_worktree_of` tells whether a branch is checked out in another
worktree by comparing resolved paths: the one `git worktree list` gives and
`git rev-parse --show-toplevel`. `Path.resolve()` doesn't fix a path's case,
so on macOS or Windows, if the two spell the same directory in different cases
(e.g. the worktree was added as `~/Code/wt` and is now reached as
`~/code/wt`), the current worktree counts as another. `restack`, `sync` and
`pull` then skip its branch, and `-d` doesn't delete it, each saying it's
checked out elsewhere. Not reproduced. Comparing with `os.path.samefile` (or
device and inode) would fix it.

## The branch config cache could miss a write

`GitClient.branch_config` rereads `.git/config` only when its inode, mtime or
size has changed since it last read it. `git config` writes a new file and
renames it into place, so each write normally gets a new inode. But a
filesystem that reuses inode numbers (e.g. ext4) could give the file the inode
it had two writes earlier, and two writes in the same timestamp tick that
leave the file the same size (e.g. overwriting a commit ID with another) would
then look like no change, and git-stack would keep using the old values. Not
reproduced; on btrfs, where inode numbers aren't reused, it can't happen.

It also only watches the common `.git/config`, so `branch.*` settings changed
in another config file (`git config --worktree` with `extensions.worktreeConfig`,
or an included file) aren't seen until something writes `.git/config`.

`forget` rereads the config rather than trusting the cache, so untracking a
branch can't leave its settings behind, but every other read still trusts it.
Comparing the file's contents (or a hash of them) instead of its metadata
would rule out the missed write at little cost, since the file is small.

## A repository without a config file gets a traceback

git works without `.git/config`, but `GitClient.branch_config` stats it to
decide whether to reread it, and the `FileNotFoundError` isn't a `StackError`,
so every command that reads stack metadata ends in a Python traceback. With
`--json` there's no `error` line, which scripts rely on. Reproduced with
`git stack info --json`. Treating a missing file as an empty config (and its
stat as "changed") would fix it.

## The check cache can leave temporary files behind

`CheckCache._write` writes to a `mkstemp` file and renames it over
`git-stack-cache.json`, but if the write or the rename fails (e.g. on Windows,
while another process has the cache open), it ignores the error without
removing the temporary file. Each failed write leaves another
`.git-stack-cache-*` file in the git directory. Removing it in the `except`
would fix it.

## Some git calls are repeated where they could be cached

Each git call costs a process, and a few are made over and over in one
command:

- `other_worktree_of`, which restack, sync and pull call for each branch, runs
  `git worktree list`, a `rev-parse --absolute-git-dir` for each detached
  worktree, and `rev-parse --show-toplevel` every time. The worktree list could
  be read once per snapshot, as refs are, and the toplevel cached, as
  `common_dir` is.
- With `-d remote` (or `both`), `untrack_merged` asks the remote for its
  default branch (`git ls-remote`, a network round trip) for each merged branch
  it deletes; once per command and remote would do.
- The ref snapshot reads every local and remote-tracking branch again after
  any git command that may change refs. In a clone with tens of thousands of
  remote branches, each reread may cost more than the calls it saves; reading
  only the remotes in use would bound it.

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

## GitHub's username format is checked in `stack.py`

`GITHUB_LOGIN`, which `import_stacks` checks `--author` against, is GitHub's
format, but it lives in `git_stack/stack.py` rather than in
`git_stack/adapters/github.py` with the rest of what's GitHub-specific (e.g.
`pull_request_number`). It should move there, still checked before the pull
requests are looked up, so its error doesn't get the "check `gh auth status`"
hint.

## Worktree paths are strings, not `Path`s

`GitClient.worktree_of` and `other_worktree_of` return strings, while
`GitClient.directory` and `common_dir()` are `Path`s, so `other_worktree_of`
wraps its result in a `Path` again to compare it, and the tests compare
worktree paths in events as strings, which `AGENTS.md` says not to do. They
could return `Path`s, made strings only where an event (whose `path` field is
a string in JSON) is built.
