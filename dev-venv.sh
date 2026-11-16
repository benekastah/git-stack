#!/bin/sh
# Bring .venv up to date with uv.lock: git-stack and the dev dependencies, at
# the versions locked there (updating uv.lock first if pyproject.toml's
# dependencies have changed, and saying so). check.sh, dev-install.sh and the pre-commit hook
# run it; it's quick when there's nothing to do. Needs uv
# (https://docs.astral.sh/uv/).

set -eu

cd "$(dirname "$0")"

if ! command -v uv > /dev/null; then
    echo "dev-venv: uv isn't installed; see https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
lock_before=$(cat uv.lock 2> /dev/null || true)
uv sync --quiet
if [ "$(cat uv.lock)" != "$lock_before" ]; then
    echo "dev-venv: updated uv.lock; commit it with pyproject.toml" >&2
fi
