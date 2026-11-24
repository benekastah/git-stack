# Roadmap

What's left to build in git-stack. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

## Choose which path of a tree-shaped stack is its GitHub stack

A stack on GitHub is a chain, so `prs` links a stack that branches out only
while a GitHub stack already runs through it, and otherwise leaves it to
description sections. To link one, the user should say which path it is. At a
fork (a branch with two or more children), the fork's config records which
child continues the GitHub stack (e.g. `branch.B.stackGitHubChild = C`); other
branches need nothing, since their only child continues it. A command, e.g.
`git stack github-stack <branch>`, picks the path from the root to `<branch>`,
setting it at each fork on the way.

With no choice made, `prs` follows an existing GitHub stack through a fork
(and records it); otherwise it links the path up to the first fork and stops,
with a hint to choose, so choosing later only adds pull requests to the
stack's top, and never needs unstacking. A new branch on the chosen path's
last branch joins it, being its only child.

Changing the choice once the other child is in the GitHub stack needs
`--pr-rebuild`, since GitHub can't take pull requests out of a stack.
`info` marks the GitHub stack's branches, and `--json` gains a field for it.

Groups aren't the way to say it: a group holds whole stacks, not paths
within one, and groups are made automatically after merges.

## Import only works on PRs

I should be able to express how I'd like to import remote branches:

```bash
git stack import branch-a branch-b
```

This would create a stack branch-a -> branch-b. I'm not sure how best to import
more complicated shapes; maybe some kind of basic syntax could work here.

## Shortcuts for what agents (and people) do with a branch's own commits

Skills and the README give recipes built on `parent` and `base`:

```sh
git diff $(git stack parent)...         # what this branch's pull request changes
git log --oneline $(git stack base)..   # this branch's own commits
git rebase -i $(git stack base)         # reword or reorder them
```

Commands for these would be easier to find and to get right, and the last one
can't be used by an agent at all, since it opens an editor. Which ones are worth
having (`diff`, `log`, something to reword, squash or reorder a branch's own
commits without an editor) is undecided.

## A command that prints the documentation

`git stack --help` opens a man page in a pager, and uv doesn't install it, so
neither a person without `man` nor an agent can read the documentation from
git-stack itself. The README is too long to hand over whole. A `git stack docs
[<section>]` command that prints sections of a bundled copy would let the agent
skills point at the right part of it. How the sections are named, and whether
the man page and this command share one source, is undecided.

## Open an interactive stack view from `git stack`

Explore opening a terminal UI when `git stack` runs without a subcommand in
an interactive terminal. Start by showing the stack tree, branch status, and
the current branch. Let users move through the tree and select a branch to
check out. Consider rearranging stacks from the same view, with a preview
of the changes before applying them. Keep named subcommands scriptable and
preserve the existing command-line behavior outside an interactive terminal.

## Get rid of the performance tests' tables of counts

`tests/test_operations_*.py` and `tests/large/test_distance.py` allot git processes and `gh` commands
in a table for each command, by stack size, because today's counts are irregular: they grow with the
stack in ways that fit no one equation. Once the commands are made to run a flat number (a few, with
one rebase for a chain), each table becomes a constant, or an equation where growth is inherent (a
rebase for each branch replayed), and the tables go.

## Minimize and overlap subprocesses for every command

A command should reach the theoretical minimum process count for the Git and GitHub operations
it needs, given Git's supported interfaces and the dependencies between reads and writes.
Use the largest safe batches and overlap independent expensive subprocesses with bounded
concurrency. Keep cheap in-process work serial, and preserve live protection, leases, and
error reporting across batches. The performance tests allot what each
command may run at 1, 3 and 10 branches (`tests/test_operations_*.py`, and `tests/large/` in a
repository of 500,000 commits and 5,000 branches), and warn when one runs fewer, so each change
lowers a number there; the tables are the measure of where a command is, and the ones that still grow
with the stack are what's left to do. Measure both deep stacks and groups of independent stacks,
with and without GitHub opt-in, and document the remaining processes that a command needs for
publication, recovery, live protection, and older Git or GitHub versions. Extend the budgets when
another command or path is optimized.

What a command runs that isn't its own work comes from a few things that repeat across commands:

1. **What's read once stays read.** `config --list` is read again after each write, and the refs after
   each command that may change them, because the snapshot is dropped when anything changes. A write
   of ours can update what it holds instead.
2. **Answer from what's read.** `git remote`, a remote's URL and `rev-parse <branch>@{upstream}` are in
   the config and the refs already read.
3. **Checks in bulk.** Use ancestry and trees already available for fast-forward containment.
   Batch the remaining independent landed checks in `merge-tree --stdin` and preserve the
   fallback for older Git. Recompute checks whose inputs change when a parent is removed or
   rebased.
4. **Writes in bulk.** `git config` sets one key per process, so recording a stack's branches costs
   processes in proportion to its length. Writing many keys in one process means editing the config file
   under git's own lock (`config.lock`, then a rename) in py-git-client, which git-recommit and
   move-forward share. It's the riskiest piece here, since it replaces git as the writer of
   `.git/config`: it should only edit what it can parse with certainty (`branch.<name>.<key>`), fall
   back to `git config` for anything else, and be tested against `git config` itself over many
   generated files and values. Writing less is the other lever, and should come first where it's enough.
5. **Don't read what a command doesn't use.** `parent` and `base` have no need of the refs.

A chain of branches is one `git rebase --update-refs`: branches follow one another when each is stacked
on the one before and based on its tip, and the rest are rebased on their own, so a restack costs a
rebase for the chain, not for each branch. `sync` restacks branch by branch, interleaved with merging
and pulling each, so it needs to be split into passes (classify, restack together through the same
code `restack` uses, pull) before it can follow.

## Consider storing stack state in refs

Explore a configurable choice between the current config-backed state and dedicated refs.
Config can remain appropriate for individual use. Refs could support atomic updates to stack
metadata and give teams a way to share selected state. Examine which metadata belongs in refs,
how old clients would coexist with ref-backed repositories, and how users would migrate or
switch back. Compare the two approaches before choosing a format or default.
