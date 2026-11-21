"""The tests' own setup in conftest.py."""

import pathlib
import sys
from typing import Any

import pytest

from tests.conftest import CI_WORKERS, pytest_collection_modifyitems, pytest_xdist_auto_num_workers


@pytest.mark.parametrize(
    ("environment", "workers"),
    [
        ({"CI": "true"}, CI_WORKERS),
        ({"CI": "true", "PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}, None),
        ({}, None),
    ],
)
def test_ci_runs_more_workers_than_cpus_unless_told_how_many(
    monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config, environment: dict[str, str], workers: int | None
) -> None:
    for name in ("CI", "PYTEST_XDIST_AUTO_NUM_WORKERS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert pytest_xdist_auto_num_workers(pytestconfig) == workers


class FakeItem:
    """What pytest_collection_modifyitems uses of a test: where it is, and a mark it can be given."""

    def __init__(self, name: str) -> None:
        self.path = pathlib.Path("tests") / name
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


@pytest.mark.parametrize(("platform", "skipped"), [("linux", False), ("darwin", True), ("win32", True)])
def test_the_performance_tests_run_on_linux_only(
    monkeypatch: pytest.MonkeyPatch, pytestconfig: pytest.Config, platform: str, skipped: bool
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    performance = [FakeItem("test_operations_read_only.py"), FakeItem("test_operations_github.py")]
    # The helper's own tests (test_operations.py) and every other test run everywhere.
    others = [FakeItem("test_operations.py"), FakeItem("test_restack.py")]

    pytest_collection_modifyitems(pytestconfig, [*performance, *others])  # pyright: ignore[reportArgumentType]

    assert [bool(item.markers) for item in performance] == [skipped, skipped]
    assert not any(item.markers for item in others)
