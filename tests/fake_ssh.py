"""Stand in for OpenSSH's effective configuration output."""

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-G", action="store_true")
    parser.add_argument("-p", default="22")
    parser.add_argument("-o", action="append")
    parser.add_argument("destination")
    parser.add_argument("command", nargs="?")
    args = parser.parse_args()
    if not args.G:
        sys.exit(255)
    user, sep, host = args.destination.partition("@")
    if not sep:
        host, user = user, "git"
    original = host
    if host in ("github-work", "github-proxy", "github-split", "github-protocol1"):
        host = "github.com"
    if original == "github-split" and (args.command or "").startswith("git-receive-pack"):
        host = "mirror.invalid"
    if original == "github-protocol1" and (
        args.o != ["SendEnv=GIT_PROTOCOL"]
        or os.environ.get("GIT_PROTOCOL") != "version=1"
        or "GIT_NO_REPLACE_OBJECTS" in os.environ
        or os.environ.get("GIT_EXEC_PATH") != os.environ["PATH"].split(os.pathsep, 1)[0]
        or Path.cwd() != Path(os.environ["FAKE_SSH_ROOT"]).resolve()
    ):
        host = "mirror.invalid"
    print(f"user {user}\nhostname {host}\nport {args.p}")
    if original == "github-proxy":
        print("proxycommand unverified transport")


if __name__ == "__main__":
    main()
