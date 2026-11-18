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
