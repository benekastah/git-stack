#!/bin/sh
# Bring .venv up to date with uv.lock: git-stack and the dev dependencies, at
# the versions locked there (updating uv.lock first if pyproject.toml's
# dependencies have changed). check.sh, dev-install.sh and the pre-commit hook
# run it; it's quick when there's nothing to do. Needs uv
# (https://docs.astral.sh/uv/).

set -eu

cd "$(dirname "$0")"

if ! command -v uv > /dev/null; then
    echo "dev-venv: uv isn't installed; see https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
uv sync --quiet
