"""Generate man/git-stack.1 from README.md.

The pre-commit hook in git_hooks/ runs it when README.md changes; otherwise,
run it after changing README.md. It uses the pandoc that the pypandoc_binary
dev dependency bundles, with man/git-stack.lua as a filter.
"""

import argparse
import pathlib
import sys

import pypandoc  # type: ignore[import-untyped]

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
FILTER = ROOT / "man" / "git-stack.lua"
PAGE = ROOT / "man" / "git-stack.1"


def render(readme: pathlib.Path) -> str:
    page: str = pypandoc.convert_file(
        str(readme), "man", format="gfm", extra_args=["--standalone", f"--lua-filter={FILTER}"]
    )
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true", help="don't write the page; fail if it's out of date with README.md"
    )
    parser.add_argument(
        "--readme", type=pathlib.Path, default=README, help="generate the page from this file instead of README.md"
    )
    args = parser.parse_args()
    page = render(args.readme)
    if not args.check:
        PAGE.write_text(page)
    elif PAGE.read_text() != page:
        print(f"{PAGE.relative_to(ROOT)} is out of date with README.md; run man/build.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
