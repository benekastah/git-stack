"""Stands in for git in tests, as $GIT_STACK_GIT: appends the command it was
run as, and its $LC_ALL, to $FAKE_GIT_LOG as a JSON line, then runs
$REAL_GIT with the same arguments, stdin and output."""

import json
import os
import subprocess
import sys


def main() -> None:
    with open(os.environ["FAKE_GIT_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"args": sys.argv[1:], "LC_ALL": os.environ.get("LC_ALL")}) + "\n")
    sys.exit(subprocess.run([os.environ["REAL_GIT"], *sys.argv[1:]], check=False).returncode)


if __name__ == "__main__":
    main()
