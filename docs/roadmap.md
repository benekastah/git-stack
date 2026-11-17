# Roadmap

What's left to build in git-stack. Only what's still to do lives here: when something
is done, remove it in the same change. What's wrong with what's built is in
[issues.md](issues.md).

## Multiple bases

Allow a branch to depend on several branches. Keep the first base as its
primary parent and replay the other bases' changes in their specified order,
then replay the dependent branch's own commits. The original base branches
are never rewritten to assemble the dependent branch. Multiple-base branches
and their descendants may be pushed, but git-stack must not open pull requests
for them.

The implementation needs to cover these parts together:

- Record the ordered bases and the commits last incorporated from each one.
  Keep existing single-parent metadata and scripting output compatible.
- Walk dependencies before dependents, including across stacks, and reject
  cycles. Selecting a dependent must include the bases needed to restack it;
  a shared dependency retains its ownership protections.
- Distinguish replayed base commits from the dependent branch's own commits.
  Rebuild the combined history when a base advances or is rewritten, without
  duplicating overlapping changes or losing the dependent branch's changes.
  Refuse histories that cannot be replayed safely.
- Record enough state before rewriting to recover from conflicts, aborts and
  interruptions. Never advance recorded bases for a replay that did not
  finish. Respect branches checked out in other worktrees.
- Show all bases and restack status in `info`; maintain the existing JSON and
  scripting interfaces. Account for dependencies in rename, untrack, clean,
  pull and sync as well as restack.
- Allow push while refusing PR creation or retargeting for a multiple-base
  branch and any descendant. Ordinary base branches remain eligible for PRs.
- Test independent and overlapping bases, rewritten bases, local commits,
  conflicts and recovery, cycles, shared stacks, worktrees, PR restrictions,
  and compatibility with existing single-parent stacks.
