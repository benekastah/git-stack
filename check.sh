#!/bin/sh
# Run every check: the type checker, then the tests.
#
# The first run creates .venv with the dev dependencies from pyproject.toml,
# and later runs reinstall them whenever pyproject.toml changes.

set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if [ ! -f .venv/installed ] || [ pyproject.toml -nt .venv/installed ]; then
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet --group dev
    touch .venv/installed
fi

.venv/bin/python -m mypy
.venv/bin/python -m pytest "$@"
