"""The test helpers in tests/helpers.py that tests rely on to fail rather than hang."""

import pathlib
import sys
import time

import pytest

from git_stack.file_lock import file_lock
from tests.helpers import run_process


class StillLocked(Exception):
    pass


def raise_still_locked() -> None:
    raise StillLocked


def test_process_past_its_timeout_is_killed_with_what_it_started(tmp_path: pathlib.Path) -> None:
    """A grandchild left running (e.g. one deadlocked with git-stack's lock)
    would hold the lock, or the output's pipes, for good."""
    lock = tmp_path / "lock"
    locked = tmp_path / "locked"
    # Holds lock until it's killed.
    grandchild = (
        "import pathlib, time\n"
        "from git_stack.file_lock import file_lock\n"
        f"with file_lock(pathlib.Path({str(lock)!r}), lambda: None):\n"
        f"    pathlib.Path({str(locked)!r}).touch()\n"
        "    time.sleep(600)\n"
    )
    # Starts it, and waits for it to hold the lock and for its own end.
    child = (
        "import pathlib, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"while not pathlib.Path({str(locked)!r}).exists():\n"
        "    time.sleep(0.05)\n"
        "print('started', flush=True)\n"
        "time.sleep(600)\n"
    )

    with pytest.raises(pytest.fail.Exception, match="didn't finish in 3 seconds:\nstarted\n"):
        run_process([sys.executable, "-c", child], timeout=3)

    # Once it's gone, which the system may take a moment to see.
    deadline = time.monotonic() + 10
    while True:
        try:
            with file_lock(lock, raise_still_locked):
                break
        except StillLocked:
            assert time.monotonic() < deadline, "what the process started still runs"
            time.sleep(0.1)
