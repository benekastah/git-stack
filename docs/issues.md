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
they're safe to run while another git-stack command is running". `parent` starts 2 git processes
(`config --list`, and one `rev-parse` for the stopped rebase, cherry-pick, revert or merge check and
the branch checked out), `show-stack` 3 (also a `for-each-ref`), and `base` 3
(also a `merge-base --is-ancestor`), however long the stack is
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

`tests/test_operations_local_rewriting.py` allots what each may cost at 1, 3 and 10 branches, and
several grow with the stack even when there's no work: `restack` of an up to date stack starts
6 + 2 * branches git processes (26 for 10 branches), `pull` with nothing to pull 11 + branches,
and `clean` with nothing merged 10 + branches. `untrack`ing a whole stack starts 6, 12 and 33
(3 + 3 * branches): each branch's config is removed in a write of its own, and the config is read again
after each. Restacking replays a branch for 12 processes each (the rebase, and the checks around it). In a
repository with thousands of branches and a stack of a dozen, these are the ones to batch (one
`for-each-ref` or `config` read in place of one per branch), which the tests would then show.

## Syncing, importing and opening pull requests start many processes for a long stack

`tests/test_operations_github.py` allots them at 1, 3 and 10 branches. Pushing a stack that was
never pushed starts 12, 18 and 39 git processes; `push --prs` 14, 25 and 60 git and 2, 8 and 15
`gh` (more than one `gh` call for each of 10 pull requests); `sync` after the bottom pull request
merged 34, 74 and 207 git and up to 6 `gh`; and `import` 18, 35 and 91 git for 1, 3 and 10 pull
requests (its `gh` stays at 2). On a stack of a dozen, `sync` is over 200 processes, of which the
restacks are most (see the issue above). `info` and a `sync` with nothing to do keep `gh` at 1.

## `sync` and `push` run different processes on Windows than on Linux and macOS

For an ssh remote (`git@github.com:owner/repo`), `GitHubAdapter.matches_direct_ssh` works out from the
effective ssh settings (`ssh -G`, twice, which prints the configuration and connects to nothing), plus
`git --exec-path` and `git rev-parse --local-env-vars`, whether git would reach GitHub's own endpoint,
and if so takes the API's default branch as the remote's, skipping the `git ls-remote --symref` probe
of the server's HEAD. `uses_openssh` returns False on Windows (Git for Windows adjusts PATH inside its
bundled runtime, so the `ssh` Python finds may not be the one git runs), so there it runs none of that
and always probes. A command must run the same processes on every system (see "Linux, macOS and
Windows" in `PROJECT_AGENTS.md`), and the system test is a crude stand-in for "which ssh does git
run": people do use OpenSSH on Windows (it ships with it, and Git for Windows bundles one). The fix is
to run the same on all three: always probe (one more network round trip for an ssh remote on Linux and
macOS, and no `ssh -G`) or find out which ssh git runs without asking the system.

## An older git-stack can't finish a restack this one stopped

A restack that stops on a conflict is recorded in one setting,
`stackPendingRestack`, which versions before it don't read. Finishing the
rebase with an older git-stack leaves that record behind, and the next run
of a newer one may then apply it over what the older one did since (e.g. a
`track` onto another parent). Finish a stopped restack with the version
that started it, or a newer one.
