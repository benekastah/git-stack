"""CheckCache, where StackCore.remembered keeps its answers."""

import pathlib

import pytest

from git_stack.check_cache import CheckCache


def test_batch_writes_all_checks_once_and_preserves_other_entries(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "git-stack-cache.json"
    cache = CheckCache(path)
    other = CheckCache(path)
    replace = pathlib.Path.replace
    writes: list[pathlib.Path] = []

    def record_write(source: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        writes.append(target)
        return replace(source, target)

    monkeypatch.setattr(pathlib.Path, "replace", record_write)
    tracked = ["first", "second", "other"]
    with cache.batch(lambda: tracked):
        cache.set("first", "status", "true", tracked)
        cache.set("second", "status", "false", tracked)
        assert cache.get("first", "status") == "true"
        assert writes == []
        other.set("other", "status", "true", tracked)

    assert writes == [path, path]
    current = CheckCache(path)
    assert [current.get(branch, "status") for branch in tracked] == ["true", "false", "true"]


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
