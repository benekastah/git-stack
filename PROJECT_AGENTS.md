# git-stack's own rules

What's true of git-stack alone. `AGENTS.md` has the rules it shares with every project
made from python-template; where the two disagree, this file wins. Keep this file current when
the shape of the code or a rule here changes. The README is the user documentation, and the
man page is generated from it.

## Versioning

Every merge into `main` must update `__version__` in `git_stack/_version.py`.
Bump the patch component relative to the current version on `main` unless the
change warrants a minor or major bump. Include the bump in the commit that
lands on `main` so each merged build has a distinct version in performance logs.

## Shape of the code

- `git_stack/stack.py` (`StackClient`) and `git_stack/core.py` (`StackCore`) do
  the work. `git_stack/git.py` (`GitClient`) runs git; the adapters in
  `git_stack/adapters/` say which branches were merged, locally or by asking
  GitHub. The GitHub adapter validates PR changes before an atomic push and
  plans any retargets that must run before publication. PR body updates use
  bounded GraphQL batches and send the body data on stdin. Run the dry push
  before applying those edits so stale leases leave the PR bases alone.
  Push and sync can reuse the API's default branch when the remote has one direct
  HTTPS URL on github.com for both fetch and push. Otherwise check the live
  HEAD of the fetch remote and any separate push destinations.
- `GitClient` runs git through [py-git-client](https://github.com/benekastah/py-git-client)
  (`git_client`), which git-stack shares with git-recommit and move-forward:
  its `Git` starts each process, the quickest way each system allows (and
  finds git, `$GIT_STACK_GIT` or the one on PATH), in the user's locale but
  with git's own messages in English, so it can read them; its `CatFile` is the
  snapshot's long-lived `git cat-file`, and its batched reads (`refs`,
  `config`, `patch_ids`) and error classes (e.g. `ConfigLockedError`) are
  git-stack's. What git-stack adds stays in `GitClient`: the snapshot and its
  caches, `check=False`, and turning a `GitError` into a `StackError` (see
  `git_errors`), which the command line reports as its own. Change how git is
  started or read in py-git-client, not here.
- `git_stack/stacks.py` (`Stacks`) holds the tracked branches as trees, with
  the rules for groups and ownership, without touching the repository.
  `StackCore.stacks()` reads it, a command changes it in memory, and
  `StackCore.save()` writes only what changed. Walk and change the tree through
  it rather than with a `{branch: parent}` map of your own.
- `git_stack/integrations.py` keeps integration roots out of `Stacks`. Its branch config
  records exact tips, merge evidence, build inputs and pending publication. The merge
  checkpoint stores only the step and HEAD so it can advance without rewriting those
  inputs. A build that cherry-picks pins its plan on disk. Fast-forward changes to
  independent, linear sources can extend a previous build, and successful builds
  reuse a detached worktree. Conflicts in a new build discard it so sources can be
  restacked. Existing stopped operations are recoverable. Sync records merged
  source tips and stack selectors before untracking
  them so a retry can recover the inputs, group scope and remote. Queued remote deletions
  retain their dependent branches and leases. The checkout recovery marker
  survives worktree cleanup.
  The refresh commands order selected source stacks before the integration builds, then
  use the ordinary single-parent rebase path for the work above each integration.
- Each command is a module in `git_stack/commands/` with `register`, `run` and
  `show`: `run` does the command and returns a dataclass result, and `show`
  prints it as text. With `--json`, the CLI prints the result as JSON instead
  (see below), so the result has to hold everything the command reports.
- Each git call is slow enough to add up, so `GitClient` reads branch config
  and refs in bulk and caches them. Refs are cached only within a command (see
  `GitClient.snapshot`): a command that changes anything is `@exclusive`, which
  takes the snapshot under the lock, and one that only reads is `@snapshot`.
  Run git through `GitClient` so the snapshot is forgotten when a git command
  may change refs, and only add a command to `READ_ONLY_COMMANDS` if it never
  does.
- Progress and warnings are events (`git_stack/events.py`), passed to the
  client's `on_event`. `git_stack/output.py` turns them into text, or into JSON
  lines.
- When `stack.perfLog` is set, `git_stack/performance.py` records one timeline at py-git-client's
  `Git.process` boundary, in the snapshot's `CatFile`, and at both `gh` launches.
  Branch-status workers inherit the same recorder. Keep subprocess input,
  output and environment out of the timeline.
  The CLI writes the command line, version and timeline to `stack.perfLog`,
  capped by `stack.perfLogMaxBytes`. Tests configure their own log paths.
- `git_stack/_version.py` defines the package version for setuptools and
  adds the source commit to performance reports when it can identify it.
- A value from a fixed set (an option's, a setting's, GitHub's) is an enum,
  converted where it's read (e.g. `Delete`, `Ownership`, `GitHubState`), so
  the code compares members rather than strings, and adding a value happens in
  one place. Give it the values the outside world uses, so JSON output, which
  prints an enum as its value, doesn't change.
- git-stack's only runtime dependency is py-git-client, which has none of its
  own. Add no other: use the standard library, and add dev dependencies to
  `pyproject.toml`'s `dev` group. py-git-client isn't on PyPI, so
  `dependencies` names it by its git URL and a release's full commit, with
  the release's tag in a comment (`uv add --raw-sources`): pip and pipx read
  that, unlike uv's `[tool.uv.sources]`, and don't read `uv.lock`, so a tag,
  which can move, could install code CI never ran. To take a new release,
  change the commit and the comment, and run `uv lock`.

- `git_stack/skills/<name>/SKILL.md` are agent skills for people's agents, kept short and pointing at
  `git stack --help` and the README for detail. `tests/test_skills.py` checks that every
  `git stack ...` command they show parses with `cli.build_parser()`, so a renamed command or
  flag fails there. Change a skill with the behavior it describes. `git_stack/skills.py` and
  `install-skills` link them into each agent's skills directory; `AGENTS` there says which agents
  it knows, so another is one entry.

## Don't break output formats

Scripts, editors and other tools (e.g. move-forward) parse git-stack's output,
so these are interfaces:

- **`--json`**, which every command takes before or after its name: a JSON
  object per event, one per line, then a `result` or `error` line. An event's
  JSON `type` is its class's
  `event_type`, and its fields are its dataclass fields. A result's fields are
  its dataclass fields, under the names `git_stack/json_fields.py` gives them
  (e.g. `json_name("stacks")`). So renaming or removing a field of an event or
  result, or an `event_type`, breaks scripts, even when it's only a refactor.
- **`info --json`**, which prints a single list, for compatibility.
- **The scripting commands** (`parent`, `base`, `show-stack`), which print one
  thing per line, and `show-stack --quiet`'s exit status.
- **Exit statuses**: 0 on success, 1 when a command fails, and 2 (from
  argparse) for arguments it doesn't understand.

Adding an event type, a field or a result field is fine. Renaming, removing or
changing the meaning of one isn't, nor changing a value's type (e.g. a string
to a list). If a change seems to need it, add the new form alongside the old one
and ask first. The README's "JSON output" section documents each command's
result; keep it in step with the result types.

Text meant for people (messages, `info`'s tree) can change.

## Tests

- `tests/test_cli.py` checks how each command maps its arguments and prints its
  result, with a mock `StackClient` (made once, as `create_autospec` is slow,
  and reset for each test); the other tests run git-stack against real
  repositories (see `tests/helpers.py`), copied for each test from ones
  `repo_templates` builds once. `Repo.stack` runs the command line in
  the test process, which is much quicker than starting one; only
  `tests/test_end_to_end.py` runs it as a separate process (`Repo.stack_process`),
  for what only that checks, like the entry point. A new command or `--json`
  result needs a case in `test_json_prints_the_result_on_stdout`, unless it doesn't use
  the `StackClient` (`install-skills`), which tests its own. The `gh`
  fixture's fake GitHub (`tests/fake_gh.py`) answers git-stack's `gh`
  commands in the test's process too (`InProcessGh`), and as a command for
  `Repo.stack_process`.
- The tests load py-git-client's pytest plugin (`git_client.testing`, in
  `tests/conftest.py`): each test's git has a config of its own, none of the
  user's config or git's variables (e.g. a hook's `GIT_DIR`), and runs from a
  directory outside any repository, so a git run without naming its
  repository fails rather than reach the clone the tests were started in.
  Code that names `ROOT` (`dev.py`, `dev_project.py`) still runs there. `git_calls` lists the git processes
  git-stack starts; the tests' own (`Repo.git`) aren't in it.
- The man page, `man/git-stack.1`, is generated from the README by
  `dev.py man` (the template's), and `dev.py static` fails if it's out of
  date: the pre-commit hook regenerates it (or run `python3 dev.py man`).

## Linux, macOS and Windows

On top of `AGENTS.md`'s rules:

- Compare paths as `Path`s, not as strings: git writes Windows paths with `/`.
  To tell whether two paths name the same file, use `git.same_file`, since
  resolving a path doesn't fix its case.
- Name a branch to git as `refs/heads/<name>` (or `refs/remotes/<remote>/<name>`),
  never by its bare name, which git takes for a tag of the same name first. Only
  `git switch` takes a bare name, through `git.SWITCH`.
  `tests/test_branch_names.py` runs commands with a tag named like every branch
  and fails on git's "ambiguous" warning; add a case there for a new command.
- Run git through `GitClient`.
- A feature that needs a Unix-only module or call keeps it behind a function
  with a Windows version too, as `git_stack/file_lock.py` does. `dev.py static`
  type-checks as if on Linux, and `dev.py types-other-platforms` (which CI runs)
  as if on Windows and macOS, so basedpyright sees each version, and reports a
  call one of them lacks.
- In tests, make a fake command with `install_command` (`tests/helpers.py`).

## Where git-stack differs from the template

Keep these when resolving a `copier update`'s conflicts:

- `dev_project.py` adds to `dev.py`: `types-other-platforms`, the man page's
  SYNOPSIS (`MAN_SYNOPSIS`), and install's `blame.ignoreRevsFile` and its running
  `git stack install-skills` (not an `install_links()` entry, since the links are
  directory links Windows needs a junction for, which that command makes).
- `pyproject.toml`: every setting the template doesn't have, and every value
  that differs from it, e.g. setuptools and its `data-files` (the man page;
  the package is at the root, not in `src/`), `distlib` in `dev`, Ruff's
  line length, extra rules, `force-exclude` and `dev.py`'s exclusions, and
  basedpyright's `include` and relaxed rules.
- CI: `.github/workflows/project.yml` runs the type checks as if on Windows
  and macOS. `ci.yml` has two edits of git-stack's, each commented: the tests
  job sets up the Windows Dev Drive (`.github/actions/fast-temp`), and the
  time limits. `tests/conftest.py`
  gives CI 8 test workers. `.github/workflows/measure.yml` is git-stack's own.
- The extra lines in `.gitattributes` and `.gitignore`.
