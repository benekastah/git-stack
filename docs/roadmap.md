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
