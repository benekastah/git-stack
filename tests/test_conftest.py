"""The tests' own setup in conftest.py."""

import pathlib
import sys

import pytest

from tests.conftest import CI_WORKERS, ROOT, pytest_xdist_auto_num_workers
from tests.helpers import run_process


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


def test_hung_test_fails_saying_where_it_hangs(tmp_path: pathlib.Path) -> None:
    """Rather than hang the run (see fail_hung_test)."""
    test = tmp_path / "test_hangs.py"
    test.write_text(
        "import time\n"
        "from tests import conftest\n"
        "conftest.HANG_TIMEOUT = 1\n"
        "\n"
        "def test_hangs() -> None:\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )

    result = run_process(
        # tests/conftest.py as a plugin, since the test is outside tests.
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider", "-o", "addopts=", str(test)],
        timeout=30,
        cwd=ROOT,
    )

    assert result.returncode != 0
    assert "Timeout (0:00:01)!" in result.stderr
    assert f'File "{test}", line 6 in test_hangs' in result.stderr
