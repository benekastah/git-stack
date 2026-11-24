"""Stands in for ssh in tests, for `ssh -G <host>`, with a config that has
`Host github-work` with `HostName github.com`."""

import sys


def main() -> None:
    args = sys.argv[1:]
    if args[:1] != ["-G"] or len(args) != 2:
        sys.exit(255)
    host = args[1]
    print(f"hostname {'github.com' if host == 'github-work' else host}")


if __name__ == "__main__":
    main()
