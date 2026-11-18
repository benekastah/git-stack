---
name: git-stack-split
description: Plan and build a large change as a stack of small pull requests with git-stack - decide the steps, make each one a branch that builds and passes on its own, and check nothing was lost. Use when a change is too big for one PR, or the user asks to split a branch or PR.
---

# Splitting a change into a stack

Read the `git-stack` skill first. Reviewers read a small PR better than a big one. Aim for steps of a
few hundred changed lines, within the project's own limits (see its contributing docs or agent
instructions).

## Plan before branching

1. **Look at the change:** `git diff --stat <base>...<big branch>` (or the working tree).
2. **Map dependencies:** what each piece needs from the others. Then choose steps so that:
   - each is one kind of change (a refactor, a feature or a fix), not a mix;
   - each builds and passes the checks on its own, since the PRs are reviewed, and merged, one at a time;
   - new code lands with the code that uses it, not a PR early;
   - old and new code can run together (the steps may deploy in any order, or at different times).
3. **Parallel or stacked:** pieces that don't depend on each other are separate stacks from `main`,
   so they can be reviewed and merged in any order; pieces that depend on one another are a stack.
   Group related stacks so one command covers them: `git stack group add <name> <branch>...`.
4. **Show the user the plan** (branches, order, what goes in each, rough size) and get agreement
   before building it.

## Build it

For a change that's already one branch `big`, carve it by files, one branch per step:

```sh
git switch main
git stack create step1-model
git checkout big -- lib/model.py   # this step's files
git commit -m "..."
git stack create step2-api         # stacked on step1-model
git checkout big -- lib/api.py
git commit -m "..."
```

For a change that's several commits on `big`, split by commit instead: see `git-stack-rearrange`
("Split a branch in two").

## Check it

- Nothing was lost: `git diff big <top branch>` is empty, once every step is in.
- Each step passes the project's checks:
  ```sh
  for branch in $(git stack show-stack); do
      git switch --quiet "$branch" && <the project's checks> || break
  done
  ```
- The shape is what you planned: `git stack info --all --offline`.

Then `git stack push --prs` opens a draft PR per branch, each into its parent's branch, with a stack
section listing the others. Write each PR's description to say where it sits in the stack and what
the next step adds.
