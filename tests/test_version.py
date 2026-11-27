"""The package version: version.txt, which setuptools and `git_stack._version` read."""

import tomllib
from pathlib import Path

import pytest

from git_stack import _version

ROOT = Path(__file__).resolve().parents[1]


def test_version_txt_has_nothing_but_a_line_with_the_version() -> None:
    text = (ROOT / "version.txt").read_text("utf-8")

    assert text == text.strip() + "\n"
    assert "\n" not in text.strip()


def test_the_version_is_the_one_in_version_txt() -> None:
    assert _version.__version__ == (ROOT / "version.txt").read_text("utf-8").strip()


def test_setuptools_takes_the_version_from_version_txt() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))

    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {"file": "version.txt"}


def test_an_installed_package_with_no_version_txt_next_to_it_has_the_version_of_its_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_version, "installed_version", lambda name: "1.2.3" if name == "git-stack" else "?")

    assert _version.read_version(tmp_path) == "1.2.3"


def test_a_checkout_has_the_version_in_its_version_txt_even_if_the_install_is_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "version.txt").write_text("2.0.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(_version, "installed_version", lambda name: "1.2.3")

    assert _version.read_version(tmp_path) == "2.0.0"


def test_a_version_txt_that_belongs_to_something_else_is_not_the_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In an installed package the directory above is site-packages, where another package may leave a
    version.txt: it's the checkout's only with a pyproject.toml beside it."""
    (tmp_path / "version.txt").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(_version, "installed_version", lambda name: "1.2.3")

    assert _version.read_version(tmp_path) == "1.2.3"
