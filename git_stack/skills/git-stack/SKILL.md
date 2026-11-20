---
name: git-stack
description: Create and manage stacked pull requests with git-stack (`git stack`). Use when a repo has stacked branches or PRs, the user mentions git stack, stacks or stacked PRs, or you are about to start a branch that builds on another unmerged branch.
---

# git-stack

When `stack.perfLog` is set, use that log to report total wall time,
how many Git and `gh` calls ran, the time spent in each tool, and the time spent elsewhere.
Count each `cat-file` batch request as a Git call. Include batch shutdown in Git time only.
Report any overlap shown and flag the slowest call if it explains a delay.

git-stack keeps stacked branches rebased on each other. It records, in the repo's git config, each
branch's **parent** (`branch.<name>.stackParent`) and the exact parent commit it was built on, its
**base** (`branch.<name>.stackBase`). A restack replays only the branch's own commits from that base
onto the parent. That keeps the parent's commits out of the replay after it is squash-merged.

Words: **stack** (branches built on each other; it can fork), **root** (the branch a stack starts
from, usually `main`), **tracked** (git-stack knows the branch), **group** (stacks that were one stack before their
bottom branch merged). Most commands act on the *current stack*: every branch connected to the one you're
on, including siblings.
Commands select just that stack. Pass `-g` to include its group.
From an ordinary untracked branch such as `main`, plain `git stack info` shows all stacks.

## Rules

1. **Look first.** Run `git stack info --all --offline` before changing anything: it shows each stack
   and which branches need a restack. Without `--offline`, `info` also asks GitHub about each pull
   request (about a second), for PR links and GitHub's view of what merged; add that only when you
   need it.
2. **Never use plain git on a tracked branch** for what a git-stack command does. It moves the branch
   without updating the recorded base, and the next restack then replays the wrong commits.
   - New branch: `git stack create <name>`, not `git switch -c`.
   - Rebase onto the parent or main: `git stack restack` or `git stack sync`, not `git rebase`.
   - Push: `git stack push`, not `git push`. Update from remote: `git stack pull` or `git stack sync`,
     not `git pull`.
   - Rename: `git stack rename <new>`, not `git branch -m`. Stop tracking: `git stack untrack`.
   - Committing and amending a branch's own commits are fine; restack afterwards so the branches
     above it follow.
3. **Commit before running.** `restack` and `sync` refuse a dirty worktree.
4. **Don't pass `--allow-shared` or `--force` on your own.** A shared stack (someone else's commits)
   is protected on purpose. Ask the user first.

## Starting and managing a stack

```sh
git stack create add-api --from main   # PR 1, on main
# ...commit...
git stack create use-api       # PR 2, on add-api
# ...commit...
git stack push --prs           # push both, open draft PRs, each into its parent's branch
```

| Task | Command |
| --- | --- |
| New branch on the current one | `git stack create <name>` (`--group <group>` starts it in a group) |
| Combine several branch tips into one root | `git stack integrate <name> <source-a> <source-b>` |
| Track existing branches | `git stack track <parent> <branch>...` (`--base <sha>` if the parent moved) |
| Track open PRs, e.g. new clone | `git stack import` (`-n` previews) |
| Move through the stack | `git stack next`, `git stack prev` |
| Push without opening PRs | `git stack push` |
| Open or update PRs and their stack sections | `git stack prs` |
| Rename | `git stack rename <new>` |
| Stop tracking | `git stack untrack <branch>` |
| Group stacks to act on them together | `git stack group add <name> <branch>...` |
| Who owns a stack | `git stack ownership show` |

Use `git-stack-restack` to replay branches on local parents, `git-stack-pull` to
fetch and update locally, `git-stack-push` to publish local work, and
`git-stack-sync` to fetch, update and publish in one command.

Use `git-stack-rearrange` to change a stack's shape, `git-stack-troubleshoot`
to diagnose a broken stack, `git-stack-split` to break up a large change, and
`git-stack-integrate` to build work above several unmerged inputs.

## Reading state in scripts

`git stack parent [<branch>]`, `git stack base [<branch>]` and `git stack show-stack [<branch>]` print
one thing per line (`show-stack -q` only sets the exit status). They only read config, so they're fast
and safe. Every command takes `--json` for one JSON object per line: events, then a `result` or
`error`. Parse that, not the text, which can change.

```sh
git diff $(git stack parent)...        # what this branch's PR changes
git log --oneline $(git stack base)..  # this branch's own commits
```

Run `git stack -h` for the commands, and e.g.
`git stack restack --help` for one command's flags. Don't run `--help` without a
command: it opens a man page in a pager, which an agent can't use.
