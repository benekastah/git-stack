#!/bin/sh
# Run every check: lint.sh (shellcheck, Ruff's formatter and linter, the type
# checker), then the tests.
#
# Runs dev-venv.sh first, so .venv has the dev dependencies they need.

set -eu

cd "$(dirname "$0")"

./dev-venv.sh

./lint.sh
.venv/bin/python -m pytest "$@"
