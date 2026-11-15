"""CheckCache, where StackCore.remembered keeps its answers."""

import pathlib

import pytest

from git_stack.check_cache import CheckCache


def test_a_failed_write_leaves_no_temporary_file(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = CheckCache(tmp_path / "git-stack-cache.json")

    def refuse(self: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        # As on Windows, while another process has the file open.
        raise PermissionError(f"{target} is in use")

    monkeypatch.setattr(pathlib.Path, "replace", refuse)

    cache.set("pr1", "stackConflictCheck", "abc def false", tracked=["pr1"])

    assert list(tmp_path.iterdir()) == []
    # Failing to write only means checking again.
    assert cache.get("pr1", "stackConflictCheck") == "abc def false"
