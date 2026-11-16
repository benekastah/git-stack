"""dev_project.py's additions to dev.py's install, run through dev.install() itself."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_install_links_the_man_page_and_sets_up_git_blame(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(ROOT))
    import dev
    import dev_project

    bin_dir = tmp_path / ".local" / "bin"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # On PATH already, so install doesn't add it to the real user's PATH on Windows.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(dev, "venv", lambda: None)
    monkeypatch.setattr(dev, "run", lambda *args: commands.append(args))
    monkeypatch.setattr(dev_project, "run", lambda *args: commands.append(args))

    dev.install()

    assert commands[-1][:3] == ("git", "config", "blame.ignoreRevsFile")
    assert commands[-1][3].endswith(".git-blame-ignore-revs")
    man_link = tmp_path / ".local" / "share" / "man" / "man1" / "git-stack.1"
    if sys.platform == "win32":
        assert not man_link.exists()
    else:
        assert man_link.readlink() == ROOT / "man" / "git-stack.1"
