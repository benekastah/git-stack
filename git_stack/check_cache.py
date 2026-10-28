"""The answers StackCore.remembered remembers, kept in a file of their own."""

import json
import os
import tempfile
import threading
from collections.abc import Collection


class CheckCache:
    """{branch: {key: value}}, as JSON in path.

    Kept out of git config, since writing that takes .git/config's lock, and
    any other git config write that comes at that moment (e.g. a sync in
    another terminal, while an editor's status bar refreshes info) fails.
    Each write replaces the file, so readers never see half of one. Processes
    writing at once can drop each other's answers, which only means checking
    again. Safe to use from several threads, as info does.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, str]] | None = None

    def get(self, branch: str, key: str) -> str | None:
        with self._lock:
            if self._entries is None:
                self._entries = self._read()
            return self._entries.get(branch, {}).get(key)

    def set(self, branch: str, key: str, value: str, tracked: Collection[str]) -> None:
        """Remember value for branch's key, and forget the branches not in
        tracked, so the file doesn't grow with every branch ever checked."""
        with self._lock:
            # Read again, to keep what other processes wrote since.
            entries = {b: keys for b, keys in self._read().items() if b in tracked}
            entries.setdefault(branch, {})[key] = value
            self._write(entries)

    def forget(self, branch: str, keys: Collection[str] | None = None) -> None:
        """Forget keys (default: all) of branch."""
        with self._lock:
            entries = self._read()
            if branch not in entries:
                self._entries = entries
                return
            if keys is None:
                del entries[branch]
            else:
                entries[branch] = {k: v for k, v in entries[branch].items() if k not in keys}
            self._write(entries)

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                entries = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(entries, dict):
            return {}
        return {
            b: {k: v for k, v in keys.items() if isinstance(v, str)}
            for b, keys in entries.items()
            if isinstance(keys, dict)
        }

    def _write(self, entries: dict[str, dict[str, str]]) -> None:
        """Replace the file with entries. Failing to only means checking again."""
        self._entries = entries
        try:
            fd, temp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".git-stack-cache-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            os.replace(temp, self.path)
        except OSError:
            pass
