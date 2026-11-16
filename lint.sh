#!/bin/sh
# Every check but the tests: shellcheck on the shell scripts, that the Python
# code is formatted (fix it with `.venv/bin/ruff format`), Ruff's linter, and
# the type checker (also as if on Windows, for code only Windows runs). Each
# runs even if one before it failed, so one run reports every failure.
#
# Uses the tools in .venv, as dev-venv.sh (which check.sh runs) or, in CI,
# setup-uv sets it up. It doesn't run dev-venv.sh itself: CI's .venv is made
# by uv, without pip.

set -eu

cd "$(dirname "$0")"

status=0
git ls-files -z '*.sh' 'git_hooks/*' | xargs -0 .venv/bin/shellcheck || status=1
.venv/bin/ruff format --check --quiet || status=1
.venv/bin/ruff check --quiet || status=1
.venv/bin/python -m mypy || status=1
.venv/bin/python -m mypy --platform win32 || status=1
exit "$status"
