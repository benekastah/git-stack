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

## Group sync can lose its integration selector after the last work branch lands

If a group sync removes the last selected work branch above an integration
and the atomic push fails, the group can lose its connection to the
integration when no source was retired. To retry, name the integration
explicitly with `git stack sync <integration>` and include any surviving
stacks that still need to sync.

## The scripting commands run more than the README says, and can write config

The README says `parent`, `base` and `show-stack` "only read git config, so they're fast, and
they're safe to run while another git-stack command is running". They start 3 git processes
(`config --list`, a check for a stopped rebase, cherry-pick, revert or merge, and a `for-each-ref`),
and `base`
5 (also the batch `cat-file` and a `merge-base --is-ancestor`), however long the stack is
(`tests/test_operations_read_only.py` allots them). A shell prompt or editor running one on every
keystroke pays for them.

Each also begins with `settled_stacks`, which records a restack or a pull the user finished by hand, so
one of them can write branch config, without the lock that the commands that change metadata
hold, while another git-stack process may be running. That only happens when such a restack is
waiting to be recorded, but then the prompt or editor is the one that writes it. Either the
README's claims go, or these commands settle only under the lock, or not at all. The check for a
stopped operation has to stay: it's what keeps `settled_stacks` from settling in the middle of a
rebase.

## Commands start processes for every branch, many of them one at a time

`tests/test_operations_local_rewriting.py` pins what each costs at 1, 3 and 10 branches, and
several grow with the stack even when there's no work: `restack` of an up to date stack starts
6 + 2 * branches git processes (26 for 10 branches), `pull` with nothing to pull 11 + branches,
and `clean` with nothing merged 10 + branches. `untrack`ing a whole stack, 3 + 3 * branches, has
work for each branch (removing its config), done one `git config` process at a time.
Restacking replays a branch for 12 processes each (the rebase, and the checks around it). In a
repository with thousands of branches and a stack of a dozen, these are the ones to batch (one
`for-each-ref` or `config` read in place of one per branch), which the tests would then show.

## An older git-stack can't finish a restack this one stopped

A restack that stops on a conflict is recorded in one setting,
`stackPendingRestack`, which versions before it don't read. Finishing the
rebase with an older git-stack leaves that record behind, and the next run
of a newer one may then apply it over what the older one did since (e.g. a
`track` onto another parent). Finish a stopped restack with the version
that started it, or a newer one.
