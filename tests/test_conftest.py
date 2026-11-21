"""The tests' own setup in conftest.py."""

import pytest

from tests.conftest import CI_WORKERS, pytest_xdist_auto_num_workers


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
