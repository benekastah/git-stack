#!/bin/sh
# Create .venv with the dev dependencies from pyproject.toml, or bring it up to
# date with them: they're reinstalled whenever pyproject.toml has changed since
# they were last installed. check.sh, dev-install.sh and the pre-commit hook
# run it; it's quick when there's nothing to do.

set -eu

cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "dev-venv: creating .venv" >&2
    python3 -m venv .venv
fi
if [ ! -f .venv/installed ] || [ pyproject.toml -nt .venv/installed ]; then
    echo "dev-venv: installing the dev dependencies from pyproject.toml into .venv" >&2
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet --group dev
    touch .venv/installed
fi
