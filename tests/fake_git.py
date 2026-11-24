"""Stands in for git in tests, as $GIT_STACK_GIT: appends the command it was
run as, its $LC_ALL and its $GIT_TEXTDOMAINDIR, to $FAKE_GIT_LOG as a JSON line, then runs
$REAL_GIT with the same arguments, stdin and output. It can also check whether
it inherited a test file's descriptor."""

import json
import os
import subprocess
import sys


def _inherited_descriptor() -> bool | None:
    descriptor = os.environ.get("FAKE_GIT_DESCRIPTOR")
    if descriptor is None:
        return None
    try:
        return os.path.samestat(os.fstat(int(descriptor)), os.stat(os.environ["FAKE_GIT_DESCRIPTOR_FILE"]))
    except OSError:
        return False


def main() -> None:
    with open(os.environ["FAKE_GIT_LOG"], "a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                {
                    "args": sys.argv[1:],
                    "inherited_descriptor": _inherited_descriptor(),
                    "LC_ALL": os.environ.get("LC_ALL"),
                    "GIT_TEXTDOMAINDIR": os.environ.get("GIT_TEXTDOMAINDIR"),
                }
            )
            + "\n"
        )
    sys.exit(subprocess.run([os.environ["REAL_GIT"], *sys.argv[1:]], check=False).returncode)


if __name__ == "__main__":
    main()
