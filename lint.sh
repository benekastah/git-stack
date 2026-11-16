#!/bin/sh
# Every check but the tests: shellcheck on the shell scripts, that the Python
# code is formatted (fix it with `.venv/bin/ruff format`), Ruff's linter, and
# basedpyright (every system's code, Windows-only code too). Each
# runs even if one before it failed, so one run reports every failure.
#
# Uses the tools in .venv, which dev-venv.sh (which check.sh runs) or, in CI,
# `uv sync --locked` installs. It doesn't run dev-venv.sh itself, so CI's
# install fails if uv.lock is out of date rather than updating it.

set -eu

cd "$(dirname "$0")"

status=0
git ls-files -z '*.sh' 'git_hooks/*' | xargs -0 .venv/bin/shellcheck || status=1
.venv/bin/ruff format --check --quiet || status=1
.venv/bin/ruff check --quiet || status=1
.venv/bin/basedpyright || status=1
exit "$status"
