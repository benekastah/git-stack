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

1. **Look first.** Run `git stack info --all` before changing anything: it shows each stack
   with line counts from each branch's recorded base and which branches need a restack.
   Add `--prs` for PR links and GitHub's view of which branches merged.
2. **Never use plain git on a tracked branch** for what a git-stack command does. It moves the branch
   without updating the recorded base, and the next restack then replays the wrong commits.
   - New branch: `git stack create <name>`, not `git switch -c`.
   - Rebase onto the parent or main: `git stack restack` or `git stack sync --protect-prs`, not `git rebase`.
   - Push: `git stack push --protect-prs`, not `git push`. Update from remote: `git stack pull` or `git stack sync --protect-prs`,
     not `git pull`.
   - Rename: `git stack rename <new>`, not `git branch -m`. Stop tracking: `git stack untrack`.
   - After amending a branch's own commits, restack so the branches above it
     follow. Before rewriting published commits, check the PR protections below.
3. **Commit before running.** `restack` and `sync` refuse a dirty worktree.
4. **Don't pass `--allow-shared` or `--force` on your own.** A shared stack (someone else's commits)
   is protected on purpose. Ask the user first.

## Protect open pull requests

Before an operation that can update published branches or PRs, check their
actual upstreams and GitHub PRs. A branch argument can select the whole stack,
including siblings and descendants. A group can select several stacks. Check
each affected PR's base, head, draft status, approvals, required checks,
mergeability, and queue status. Pause if an affected PR's queue status is
unknown.

- **Queued:** Preserve its head, base, GitHub stack membership, and queue entry
  by default. Exclude it from local moves and rebases. If publication would
  affect it, stop and ask before using the explicit override below.
- **Approved, out of draft, and targeting `main`:** Preserve its reviewed head
  and base by default, even if checks are pending. Before changing either or
  the reviewed diff, name the PR, explain the risk to its approvals and merge
  timing, and get the user's explicit approval covering that change.
- **Fully ready to merge:** When required reviews and checks are satisfied and
  the PR is mergeable, prefer letting it merge first. If an update is needed,
  call out the risk of losing approvals or rerunning checks and get explicit
  approval before proceeding.
- **Any PR with existing approvals:** Before a push, sync, or PR retarget that
  could alter its head, base, or reviewed diff, name it and warn that fresh
  review may be required. If the repository's rules make that outcome known,
  say so.

Recheck affected PRs before publishing and before retrying after a conflict or
failed push. A command can edit PR bases before the push or fail while updating
PRs after it. Inspect the live refs and PRs after a failure, and report changes
that already happened before deciding whether to retry.

## PR access and publishing flags

- Use `--prs` with `info` or `restack` for PR information. With `push`,
  `--prs` also opens missing draft PRs.
- Use `--with-prs` for PR access with `pull`, `sync`, `clean`, `push`,
  `integrate`, or `unrevert`. The `--protect-prs` option already enables PR
  access on commands that accept it.
- Use `--pr-rebuild` with `push`, `sync`, or `prs` only after the user
  approves rebuilding the remote PR stack. Review the diagnostic or run
  `git stack prs --protect-prs --dry-run --pr-rebuild` to preview changes
  involving existing PRs. Show the user any draft PRs listed for closure and
  get approval to close them before running the rebuild. The flag authorizes
  eligible draft closures without a prompt.

Use `--protect-prs` for every `push`, `sync`, and `prs`, and for
`git stack clean` with `-d remote` or `-d both`. If protection stops a change, tell the user
which PR and what would change. Get explicit approval before adding
`--allow-pr-update <number>` for that PR. Repeat the option only for PRs
the user approved updating. For a PR from a fork, use
`--allow-pr-update 'OWNER/REPO#NUMBER'` with the base repository. A queued PR
also requires `--force`, which you may pass only with the user's explicit permission. Always keep
`--protect-prs` on these retries. Leave a merge-ready PR targeting `main`
on that base.

## Approval for rearranging existing stacks

- Get the user's approval before changing an existing stack's parent
  relationships, branch order, or group membership. Show the proposed change
  and the affected branches, descendants, and pull requests.
- Before asking for approval, check the affected PRs on GitHub for their
  bases, merge readiness, and queue status. Pause if queue status is unknown.
  Keep queued PRs targeting their current base and exclude their branches from
  the local rearrangement, even when publication is planned for later. Warn
  that moving a merge-ready, conflict-free PR off `main` will delay its merge.
  Recheck the queue before running the approved move.
- Before publishing a rearrangement, recheck PR bases and queue status.
  Confirm the user's approval covers the affected branches and PR base changes.
  If the approval covers only local steps, ask before publishing with
  `push`, `sync`, or `prs`, or editing a PR directly. Ask again if the
  scope changes.

## Starting and managing a stack

```sh
git stack create add-api --from main   # PR 1, on main
# ...commit...
git stack create use-api       # PR 2, on add-api
# ...commit...
git stack push --protect-prs --prs  # push both, open draft PRs, each into its parent's branch
```

| Task | Command |
| --- | --- |
| New branch on the current one | `git stack create <name>` (`--group <group>` starts it in a group) |
| Combine several branch tips into one root | `git stack integrate <name> <source-a> <source-b>` |
| Track existing branches | `git stack track <parent> <branch>...` (`--base <sha>` if the parent moved) |
| Track open PRs, e.g. new clone | `git stack import` (`-n` previews) |
| Move through the stack | `git stack next`, `git stack prev` |
| Push without opening PRs | `git stack push --protect-prs` |
| Open or update PRs and their stack sections | `git stack prs --protect-prs` |
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
