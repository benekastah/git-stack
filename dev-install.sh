#!/bin/sh
# Install git-stack from this clone, for using it while working on it.
#
# Installs it in editable mode into a virtualenv, so changes to the clone take
# effect without reinstalling, links the `git-stack` command into ~/.local/bin
# and the man page into ~/.local/share/man/man1, makes this repository use the
# hooks in git_hooks/, and makes `git blame` in it skip the commits listed in
# .git-blame-ignore-revs. It also sets up the clone's .venv, which the hooks
# use, with dev-venv.sh. Safe to rerun, e.g. after pyproject.toml changes.

set -eu

clone=$(cd "$(dirname "$0")" && pwd)
venv="$HOME/.local/share/git-stack/venv"
bin_dir="$HOME/.local/bin"
man_dir="$HOME/.local/share/man/man1"

# Link target to link, replacing an earlier link but not anything else.
link() {
    if [ -e "$2" ] && [ ! -L "$2" ]; then
        echo "dev-install: $2 exists and isn't a link; move it out of the way and rerun" >&2
        exit 1
    fi
    ln -sfn "$1" "$2"
    echo "linked $2 -> $1"
}

if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --quiet --editable "$clone"
echo "installed git-stack from $clone into $venv"

mkdir -p "$bin_dir" "$man_dir"
link "$venv/bin/git-stack" "$bin_dir/git-stack"
link "$clone/man/git-stack.1" "$man_dir/git-stack.1"

"$clone/dev-venv.sh"
echo "set up $clone/.venv with the dev dependencies"
git -C "$clone" config core.hooksPath git_hooks
echo "set this repository's hooks to git_hooks/"

# `git blame` fails if the file is missing (e.g. with an older commit checked
# out), unless its git supports marking the path :(optional).
if git -C "$clone" -c blame.ignoreRevsFile=":(optional)$clone/no-such-file" \
        blame -L 1,1 -- dev-install.sh > /dev/null 2>&1; then
    git -C "$clone" config blame.ignoreRevsFile ':(optional).git-blame-ignore-revs'
else
    git -C "$clone" config blame.ignoreRevsFile .git-blame-ignore-revs
fi
echo "made git blame skip the commits in .git-blame-ignore-revs"

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "warning: $bin_dir isn't on your PATH, so git won't find git stack; add it in" \
            "your shell's startup file" >&2 ;;
esac
if ! man -w git-stack > /dev/null 2>&1; then
    echo "warning: man doesn't search ${man_dir%/man1}, so git stack --help won't find the man page;" \
         "add export MANPATH=\"\$HOME/.local/share/man:\" to your shell's startup file" >&2
fi
