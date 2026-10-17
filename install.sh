#!/bin/sh
# Install git-stack:
#
#   curl -fsSL https://raw.githubusercontent.com/benekastah/git-stack/main/install.sh | sh
#
# Environment variables:
#   GIT_STACK_DIR  directory to install into (default: ~/.local/bin)
#   GIT_STACK_REF  branch, tag or commit to install (default: main)

set -eu

dir="${GIT_STACK_DIR:-$HOME/.local/bin}"
ref="${GIT_STACK_REF:-main}"
url="https://raw.githubusercontent.com/benekastah/git-stack/$ref/git-stack"

fail() {
    echo "git-stack install: $*" >&2
    exit 1
}

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
    || fail "Python 3.9+ is required (found $(python3 --version 2>&1))"
# `git version` prints e.g. "git version 2.38.1" or "git version 2.39.3 (Apple Git-146)".
git version | python3 -c '
import re, sys
match = re.search(r"(\d+)\.(\d+)", sys.stdin.read())
sys.exit(not match or tuple(map(int, match.groups())) < (2, 38))
' || fail "git 2.38+ is required (found $(git version))"

mkdir -p "$dir"
# Download next to the destination and rename, so an interrupted download
# never leaves a broken git-stack behind.
tmp="$(mktemp "$dir/.git-stack.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
curl -fsSL "$url" -o "$tmp" || fail "couldn't download $url"
chmod 755 "$tmp"
mv "$tmp" "$dir/git-stack"
trap - EXIT

echo "installed git-stack to $dir/git-stack"
case ":$PATH:" in
    *":$dir:"*) echo "run \`git stack -h\` to get started" ;;
    *) echo "$dir isn't on your PATH; add it so git can find \`git stack\`" ;;
esac
