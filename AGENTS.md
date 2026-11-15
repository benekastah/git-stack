# Working on git-stack

Notes for agents (and people) changing this repo. The README is the user
documentation, and the man page is generated from it; its Development section
says how to set up a clone and run the checks. `ISSUES.md` lists known issues,
most severe first.

## Shape of the code

- `git_stack/stack.py` (`StackClient`) and `git_stack/core.py` (`StackCore`) do
  the work. `git_stack/git.py` (`GitClient`) runs git; the adapters in
  `git_stack/adapters/` say which branches were merged, locally or by asking
  GitHub.
- `git_stack/stacks.py` (`Stacks`) holds the tracked branches as trees, with
  the rules for groups and ownership, without touching the repository.
  `StackCore.stacks()` reads it, a command changes it in memory, and
  `StackCore.save()` writes only what changed. Walk and change the tree through
  it rather than with a `{branch: parent}` map of your own.
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
- A value from a fixed set (an option's, a setting's, GitHub's) is an enum,
  converted where it's read (e.g. `Delete`, `Ownership`, `GitHubState`), so
  the code compares members rather than strings, and adding a value happens in
  one place. Give it the values the outside world uses, so JSON output, which
  prints an enum as its value, doesn't change.
- git-stack has no runtime dependencies. Keep it that way: use the standard
  library, and add dev dependencies to `pyproject.toml`'s `dev` group.

## Don't break output formats

Scripts, editors and other tools (e.g. move-forward) parse git-stack's output,
so these are interfaces:

- **`--json`**, which every command takes: a JSON object per line, one per
  event, then a `result` or `error` line. An event's JSON `type` is its class's
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

## Tests and checks

- Every change to behavior comes with tests, in the same commit, and changes
  the tests that cover the old behavior. Tests say what's correct, not what the
  code happens to do: don't add a test that pins down a bug.
- `tests/test_cli.py` checks how each command maps its arguments and prints its
  result, with a mock `StackClient`; the other tests run git-stack against real
  repositories (see `tests/helpers.py`). `Repo.stack` runs the command line in
  the test process, which is much quicker than starting one; only
  `tests/test_end_to_end.py` runs it as a separate process (`Repo.stack_process`),
  for what only that checks, like the entry point. A new command or `--json` result needs
  a case in `test_json_prints_the_result_on_stdout`.
- Run `./check.sh` (formatting, Ruff, mypy in strict mode, shellcheck, the
  tests) before committing. The pre-push hook runs it too.
- Update the README with behavior changes; the pre-commit hook regenerates
  `man/git-stack.1` from it (or run `.venv/bin/python man/build.py`).

## Linux, macOS and Windows

git-stack runs on Linux, macOS and Windows, and CI runs the tests on all
three. Keep it that way:

- Use `pathlib.Path` and its `/` operator for paths, not `os.path` or strings
  joined with `/`, and compare paths as `Path`s, not as strings: git writes
  Windows paths with `/`. To tell whether two paths name the same file, use
  `git.same_file`, since resolving a path doesn't fix its case.
- Name a branch to git as `refs/heads/<name>` (or `refs/remotes/<remote>/<name>`),
  never by its bare name, which git takes for a tag of the same name first. Only
  `git switch` takes a bare name, through `git.SWITCH`.
  `tests/test_branch_names.py` runs commands with a tag named like every branch
  and fails on git's "ambiguous" warning; add a case there for a new command.
- Run git through `GitClient`, with arguments as a list, never through a shell,
  and don't call other Unix tools (`sed`, `grep`, ...) from Python.
- Don't use Unix-only modules or calls (`fcntl`, `os.fork`, `signal`, ...). If
  a feature needs one, keep it behind a function with a Windows version too,
  as `git_stack/file_lock.py` does. `check.sh` type-checks as if on Windows as
  well, so mypy sees the Windows version.
- Decode git's output, and read and write files, as UTF-8 (`encoding="utf-8"`,
  not `text=True`), not in the locale's encoding, which on Windows usually
  isn't UTF-8.
- In tests, make a fake command with `install_command` (`tests/helpers.py`),
  not a shell script or a symlink, which Windows can't run or usually can't
  make.
- Don't assume `\n` line endings in files git or the user wrote, or a
  case-sensitive filesystem (macOS and Windows usually aren't).
- Shell scripts (`check.sh`, the hooks) are for development only; git-stack
  itself mustn't need a shell.
