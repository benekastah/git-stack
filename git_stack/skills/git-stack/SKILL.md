---
name: git-stack
description: Create and manage stacked pull requests with git-stack (`git stack`). Use when a repo has stacked branches or PRs, the user mentions git stack, stacks or stacked PRs, or you are about to start a branch that builds on another unmerged branch.
---

# git-stack

Use `--performance` on every git-stack command. In your summary, report total wall time
and the time spent in Git, `gh`, and other work. Include any overlap shown and flag
the slowest call if it explains a delay.

git-stack keeps stacked branches rebased on each other. It records, in the repo's git config, each
branch's **parent** (`branch.<name>.stackParent`) and the exact parent commit it was built on, its
**base** (`branch.<name>.stackBase`). A restack replays only the branch's own commits from that base
onto the parent. That keeps the parent's commits out of the replay after it is squash-merged.

Words: **stack** (branches built on each other; it can fork), **root** (the branch a stack starts
from, usually `main`), **tracked** (git-stack knows the branch), **group** (stacks that were one stack before their
bottom branch merged). Most commands act on the *current stack*: every branch connected to the one you're
on, including siblings.
Commands select just that stack. Pass `-g` to include its group.

## Rules

1. **Look first.** Run `git stack --performance info --all --offline` before changing anything: it shows each stack
   and which branches need a restack. Without `--offline`, `info` also asks GitHub about each pull
   request (about a second), for PR links and GitHub's view of what merged; add that only when you
   need it.
2. **Never use plain git on a tracked branch** for what a git-stack command does. It moves the branch
   without updating the recorded base, and the next restack then replays the wrong commits.
   - New branch: `git stack --performance create <name>`, not `git switch -c`.
   - Rebase onto the parent or main: `git stack --performance restack` or `git stack --performance sync`, not `git rebase`.
   - Push: `git stack --performance push`, not `git push`. Update from remote: `git stack --performance pull` or `git stack --performance sync`,
     not `git pull`.
   - Rename: `git stack --performance rename <new>`, not `git branch -m`. Stop tracking: `git stack --performance untrack`.
   - Committing and amending a branch's own commits are fine; restack afterwards so the branches
     above it follow.
3. **Commit before running.** `restack` and `sync` refuse a dirty worktree.
4. **Don't pass `--allow-shared` or `--force` on your own.** A shared stack (someone else's commits)
   is protected on purpose. Ask the user first.

## Starting and managing a stack

```sh
git stack --performance create add-api --from main   # PR 1, on main
# ...commit...
git stack --performance create use-api       # PR 2, on add-api
# ...commit...
git stack --performance push --prs           # push both, open draft PRs, each into its parent's branch
```

| Task | Command |
| --- | --- |
| New branch on the current one | `git stack --performance create <name>` (`--group <group>` starts it in a group) |
| Combine several branch tips into one root | `git stack --performance integrate <name> <source-a> <source-b>` |
| Track existing branches | `git stack --performance track <parent> <branch>...` (`--base <sha>` if the parent moved) |
| Track open PRs, e.g. new clone | `git stack --performance import` (`-n` previews) |
| Move through the stack | `git stack --performance next`, `git stack --performance prev` |
| Push without opening PRs | `git stack --performance push` |
| Open or update PRs and their stack sections | `git stack --performance prs` |
| Rename | `git stack --performance rename <new>` |
| Stop tracking | `git stack --performance untrack <branch>` |
| Group stacks to act on them together | `git stack --performance group add <name> <branch>...` |
| Who owns a stack | `git stack --performance ownership show` |

Keeping a stack current (restack, sync, conflicts) is the `git-stack-sync` skill. Reshaping it is
`git-stack-rearrange`. When something looks wrong, use `git-stack-troubleshoot`. Breaking a big change
into a stack is `git-stack-split`. Building work above several unmerged inputs is
`git-stack-integrate`.

## Reading state in scripts

`git stack --performance parent [<branch>]`, `git stack --performance base [<branch>]` and `git stack --performance show-stack [<branch>]` print
one thing per line (`show-stack -q` only sets the exit status). They only read config, so they're fast
and safe. Every command takes `--json` for one JSON object per line: events, then a `result` or
`error`. Parse that, not the text, which can change.

```sh
git diff $(git stack --performance parent)...        # what this branch's PR changes
git log --oneline $(git stack --performance base)..  # this branch's own commits
```

Run `git stack --performance -h` for the commands, and e.g.
`git stack --performance restack --help` for one command's flags. Don't run `--help` without a
command: it opens a man page in a pager, which an agent can't use.
