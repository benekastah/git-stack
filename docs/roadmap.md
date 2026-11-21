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
`--rebuild-stack`, since GitHub can't take pull requests out of a stack.
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

## Get rid of the performance tests' tables of counts

`tests/test_operations_*.py` and `tests/large/test_distance.py` allot git processes and `gh` commands
in a table for each command, by stack size, because today's counts are irregular: they grow with the
stack in ways that fit no one equation. Once the commands are made to run a flat number (a few, with
one rebase for a chain), each table becomes a constant, or an equation where growth is inherent (a
rebase for each branch replayed), and the tables go.

## Run each command in as few git and gh processes as it can

A command should start a handful of git processes however long the stack or large the repository (the
performance tests in `tests/test_operations_*.py` and `tests/large/` allot what each may run, and warn
when one runs fewer, so each fix below lowers a number there). What each runs today is mostly not the
command's own work. Measured for a stack of 3 branches (cold, as a user runs it):

| Command | Now | Planned minimum | What it needs, and what goes |
| --- | ---: | ---: | --- |
| `parent`, `show-stack` | 3 | 2 | the config, and one `rev-parse` for HEAD and the stopped-operation check; no refs |
| `base` | 5 | 3 | those two, and one `merge-base --is-ancestor`; no refs, no `cat-file` |
| `next`, `prev` | 4 | 3 | those two, and the `switch` |
| `info` (offline), warm | 4 | 3 | config, refs (with HEAD), and nothing else: the color setting is in the config |
| `info`, cold | 8 (+1 per branch) | 4 | the landed checks (a `merge-tree` per branch) in one process |
| `create` | 9 | 4 | config, `rev-parse`, `switch`, and one write for the parent and the base |
| `rename` | 11 | 5 | config, refs, `branch --move`, and one write for every branch it moves |
| `untrack` | 13 (2 branches) | 3 | config, refs, and one write that removes every key |
| `restack` | 8 up to date, 37 behind (+12 per branch) | 4, 6 | config, refs, status, one check that every base is in its branch's history, one `rebase` for a chain (see below), one write |
| `push` | 15 | 5 | config, refs, the ancestry check, one `git push --set-upstream`, one write of what it recorded |
| `push --prs` | 19 git, 9 gh | 6 git, 3 gh | those, and one `gh` to find the pull requests that exist and one to open and describe all that don't |
| `prs` | 5 git, 4 gh | 3 git, 2 gh | config, refs, and one `gh` query and one batched update |
| `pull` | 13 | 5 | config, `rev-parse`, status, `fetch`, refs once more, one ancestry check |
| `clean` | 8 | 5 | config, `rev-parse`, refs, `fetch`, refs again; the upstream is in the refs |
| `sync` (nothing to do) | 14 | 7 | `pull`'s, and the default branch |
| `import` | 19 (+8 per branch) | 8 | |

These come from five things that repeat across commands, which is why they're fixed first, as their
own changes, and each command follows with the numbers it makes possible:

1. **What's read once stays read.** `config --list` runs 3 times in `push` and 4 in `restack` and
   `untrack`, and the refs again after each write, because the snapshot is dropped when anything
   changes. A write updates it instead.
2. **Answer from what's read.** `config --get-colorbool` (the color setting), `git remote` (in the
   config) and `rev-parse main@{upstream}` (in the refs' `%(upstream)`) need no process.
3. **Checks in bulk.** `merge-base --is-ancestor` runs once per branch in `push --prs`, `pull`, `sync`
   and `restack`, and `merge-tree` once per branch in a cold `info`. One `rev-list --boundary` says
   whether every base is in its branch's history, and `merge-tree --stdin` (check the git versions
   that have it) does the landed checks.
4. **Writes in bulk.** `git config` sets one key per process, so `untrack` is a process per key and
   `push` six. A writer that follows git's own lock protocol (`config.lock`, then a rename) sets any
   number in one: it belongs in py-git-client, which git-recommit and move-forward share.
5. **Don't read what a command doesn't use.** `parent` reads the refs and starts `cat-file` to print
   one config value.

A chain of branches is one `git rebase --update-refs` (see `restack_chain`: branches follow one
another when each is stacked on the one before and based on its tip; others are rebased on their own),
so a restack costs a rebase for the chain, not one for each branch. `gh` is never more than one query
and one mutation for any number of pull requests, since GraphQL takes many aliased mutations in one
request.

The numbers are the minimum for the work each does, from the traces above, and are checked as each
change lands: the tests warn when a command runs fewer than allotted, and the number is lowered.
