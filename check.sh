#!/bin/sh
# Run every check: shellcheck on the shell scripts, that the Python code is
# formatted (fix it with `.venv/bin/ruff format`), Ruff's linter, the type
# checker, then the tests.
#
# Runs dev-venv.sh first, so .venv has the dev dependencies they need.

set -eu

cd "$(dirname "$0")"

./dev-venv.sh

git ls-files -z '*.sh' 'git_hooks/*' | xargs -0 .venv/bin/shellcheck
.venv/bin/ruff format --check --quiet
.venv/bin/ruff check --quiet
.venv/bin/python -m mypy
.venv/bin/python -m pytest "$@"
