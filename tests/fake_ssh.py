"""Stands in for ssh in tests, for `ssh -G <host>`, with a config that has
`Host github-work` with `HostName github.com`. With FAKE_SSH_LOG set, it
appends each call's arguments to that file, one line per call."""

import os
import sys


def main() -> None:
    args = sys.argv[1:]
    if log := os.environ.get("FAKE_SSH_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(" ".join(args) + "\n")
    if args[:1] != ["-G"] or len(args) != 2:
        sys.exit(255)
    host = args[1]
    print(f"hostname {'github.com' if host == 'github-work' else host}")


if __name__ == "__main__":
    main()
