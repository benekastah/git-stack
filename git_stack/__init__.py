"""git stack: manage stacked branches that survive squash merges.

Each tracked branch records two things in git config:

  branch.<name>.stackParent  the branch it is stacked on (e.g. pr1, or main)
  branch.<name>.stackBase    the parent commit it was last rebased onto

Knowing the base lets us replay only the branch's own commits with
`git rebase --onto <parent> <base> <branch>`, so a parent that was squash-merged
(and whose individual commits therefore never appear in main) doesn't cause
conflicts.

A third key, branch.<name>.stackPulled, records the last upstream tip whose
commits were pulled into the branch, so a pull whose conflicts the user resolved
isn't repeated.

How a merge is detected depends on the adapter (the stack.adapter setting): the
git adapter only looks at the repository, and the GitHub adapter asks GitHub
about each branch's pull request first, finding it by the branch's name.
"""
