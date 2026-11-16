"""An exclusive lock on a file, held across processes, on Linux, macOS and Windows."""

import errno
import os
import pathlib
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager

# How often to try again for the lock on Windows, which can't wait for it.
WINDOWS_RETRY_INTERVAL = 0.1

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        # Locks the file's first byte, whether or not it has one.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            # What another process holding the lock gives.
            if e.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True

    def _lock(fd: int) -> None:
        # msvcrt.LK_LOCK gives up after 10 seconds.
        while not _try_lock(fd):
            time.sleep(WINDOWS_RETRY_INTERVAL)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: pathlib.Path, on_wait: Callable[[], None]) -> Generator[None, None, None]:
    """Hold an exclusive lock on path (created if missing), calling on_wait
    first if another process has it and it has to wait. The system releases
    the lock when the process ends, however it ends."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            on_wait()
            _lock(fd)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
