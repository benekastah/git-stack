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
- Each command is a module in `git_stack/commands/` with `register`, `run` and
  `show`: `run` does the command and returns a dataclass result, and `show`
  prints it as text. With `--json`, the CLI prints the result as JSON instead
  (see below), so the result has to hold everything the command reports.
- Progress and warnings are events (`git_stack/events.py`), passed to the
  client's `on_event`. `git_stack/output.py` turns them into text, or into JSON
  lines.
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
  repositories (see `tests/helpers.py`). A new command or `--json` result needs
  a case in `test_json_prints_the_result_on_stdout`.
- Run `./check.sh` (formatting, Ruff, mypy in strict mode, shellcheck, the
  tests) before committing. The pre-push hook runs it too.
- Update the README with behavior changes; the pre-commit hook regenerates
  `man/git-stack.1` from it (or run `.venv/bin/python man/build.py`).

## Linux, macOS and Windows

git-stack should run on Linux, macOS and Windows. It doesn't run on Windows yet
(see "No Windows support" in `ISSUES.md`): the lock uses `fcntl`, and the tests
rely on symlinks. Don't add to that:

- Use `os.path` or `pathlib` for paths, not strings joined with `/`.
- Run git through `GitClient`, with arguments as a list, never through a shell,
  and don't call other Unix tools (`sed`, `grep`, ...) from Python.
- Don't use Unix-only modules or calls (`fcntl`, `os.fork`, `signal`, ...)
  beyond the lock, which `ISSUES.md` already lists. If a feature needs one, keep
  it behind a function with a plan for Windows, and add it to that issue.
- Don't assume `\n` line endings in files git or the user wrote, or a
  case-sensitive filesystem (macOS and Windows usually aren't).
- Shell scripts (`check.sh`, the hooks) are for development only; git-stack
  itself mustn't need a shell.
