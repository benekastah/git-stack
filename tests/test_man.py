"""The man page, man/git-stack.1, which man/build.py generates from README.md."""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_man_page_is_up_to_date_with_readme() -> None:
    result = subprocess.run([sys.executable, ROOT / "man" / "build.py", "--check"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
