"""Fixtures of the large-repository tests: copies of the repository tests/large_repo.py builds,
and a report of how long each declared block took (reported, never asserted: timing is noisy)."""

import os
import pathlib
import shutil
import sys
import uuid
from collections.abc import Generator

import pytest

from tests import large_repo, operations
from tests.conftest import GIT_EXEC_PATH, ROOT
from tests.helpers import Repo

RECIPE = large_repo.Recipe()
TIMINGS: list[tuple[str, int, int, float]] = []
"""(test, git processes, gh commands, seconds) of each block that finished."""


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    large = pytest.mark.large
    for item in items:
        if pathlib.Path(item.fspath).is_relative_to(pathlib.Path(__file__).parent):
            item.add_marker(large)


@pytest.fixture(scope="session")
def large_repository(request: pytest.FixtureRequest) -> pathlib.Path:
    """The repository, built once and kept in pytest's cache (so the next run, and CI's cache
    of it, reuse it) until its recipe, or how it's built, changes. Run with `-n 0`: workers
    would each build it."""
    parent = pathlib.Path(request.config.cache.mkdir("large-repositories"))
    directory = parent / RECIPE.key()
    # `built` is made last: a build that was interrupted, or failed, has no such file, and is
    # thrown away and done again rather than used half-made.
    if not (directory / "built").exists():
        shutil.rmtree(directory, ignore_errors=True)
        large_repo.build(directory, RECIPE, lambda message: print(f"large repository: {message}", file=sys.stderr))
        (directory / "built").touch()
    return directory


@pytest.fixture
def repo(
    request: pytest.FixtureRequest,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    large_repository: pathlib.Path,
) -> Repo:
    """The large repository, in place of tests/conftest.py's small one, so the tests that use that
    one run here as they are: a copy of origin.git and a clone of it with a local branch for each
    of the thousands, and a clone of it standing in for the forge."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(GIT_EXEC_PATH), os.environ["PATH"]]))
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    # Beside the cached repository, not in tmp_path, which can be a RAM disk on another filesystem
    # that the objects can't be linked across (copied instead, each test would write the whole
    # 365 MB repository to RAM, which filled a 16 GB /tmp). Removed when the test is done, since the
    # objects it linked stay with the cached repository, and this directory is its own files.
    repos = large_repository / "work" / uuid.uuid4().hex
    repos.mkdir(parents=True)
    request.addfinalizer(lambda: shutil.rmtree(repos, ignore_errors=True))
    large_repo.copy(large_repository / "origin.git", repos / "origin.git")
    large_repo.copy(large_repository / "local", repos / "local")
    repo = Repo(path=repos / "local", origin=repos / "origin.git", forge=repos / "forge", tmp=repos)
    repo.git("config", "remote.origin.url", str(repo.origin))
    repo.git("clone", "--quiet", "--local", str(repo.origin), str(repo.forge), cwd=repos)
    return repo


@pytest.fixture(autouse=True)
def report_timings(request: pytest.FixtureRequest) -> Generator[None]:
    # What expect_operations finished during this test, and no earlier one's.
    operations.FINISHED.clear()
    yield
    name = request.node.nodeid.split("::", 1)[1]
    TIMINGS.extend((name, len(ran.git), len(ran.gh), ran.seconds) for ran in operations.FINISHED)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if not TIMINGS:
        return
    rows = [f"| {name} | {git} | {gh} | {seconds:.2f} |" for name, git, gh, seconds in TIMINGS]
    table = "\n".join(
        [f"### Large repository: {RECIPE.history:,} commits, {RECIPE.branches:,} branches", ""]
        + ["| scenario | git | gh | seconds |", "| --- | ---: | ---: | ---: |", *rows]
    )
    terminalreporter.write_sep("-", "time of each declared block (reported, not asserted)")
    terminalreporter.write_line(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(table + "\n")
