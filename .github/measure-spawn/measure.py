"""Throwaway: time each way of starting git, from a small Python process and
from a large one (like a pytest worker), and print a Markdown report.

Run with no arguments. It reruns itself once per parent size, so each size
gets a fresh process.
"""

import json
import os
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

RUNS = {"version": 200, "rev-parse": 100}
WARMUP = 10
BIG_MB = 600

GIT = shutil.which("git") or "git"
GIT_CORE = os.path.join(
    subprocess.run([GIT, "--exec-path"], capture_output=True, text=True, check=True).stdout.strip(),
    "git.exe" if os.name == "nt" else "git",
)

HELPER = r"""
import os, struct, subprocess, sys, json
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
while True:
    header = stdin.read(4)
    if not header:
        break
    argv = json.loads(stdin.read(struct.unpack("<I", header)[0]))
    result = subprocess.run(argv, capture_output=True, close_fds=False)
    stdout.write(struct.pack("<iII", result.returncode, len(result.stdout), len(result.stderr)))
    stdout.write(result.stdout + result.stderr)
    stdout.flush()
"""


class Helper:
    """A small Python process that starts git for us, one request at a time."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", HELPER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

    def run(self, argv: list[str]) -> bytes:
        assert self.process.stdin and self.process.stdout
        request = json.dumps(argv).encode()
        self.process.stdin.write(struct.pack("<I", len(request)) + request)
        self.process.stdin.flush()
        code, out_len, err_len = struct.unpack("<iII", self.process.stdout.read(12))
        data = self.process.stdout.read(out_len + err_len)
        assert code == 0, data
        return data[:out_len]

    def close(self) -> None:
        assert self.process.stdin
        self.process.stdin.close()
        self.process.wait()


def posix_spawn_direct(argv: list[str]) -> bytes:
    """os.posix_spawn with a pipe for stdout, as low-level as Python gets."""
    read_end, write_end = os.pipe()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        pid = os.posix_spawn(
            argv[0],
            argv,
            os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, write_end, 1),
                (os.POSIX_SPAWN_DUP2, devnull, 2),
                (os.POSIX_SPAWN_CLOSE, read_end),
            ],
        )
    finally:
        os.close(write_end)
        os.close(devnull)
    chunks = []
    while chunk := os.read(read_end, 65536):
        chunks.append(chunk)
    os.close(read_end)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return b"".join(chunks)


def checked(**kwargs: object) -> Callable[[list[str]], bytes]:
    def run(argv: list[str]) -> bytes:
        return subprocess.run(argv, capture_output=True, check=True, **kwargs).stdout  # type: ignore[call-overload,no-any-return]

    return run


spawned = 0
real_posix_spawn = getattr(os, "posix_spawn", None)


def counting_posix_spawn(*args: object, **kwargs: object) -> int:
    global spawned
    spawned += 1
    return real_posix_spawn(*args, **kwargs)  # type: ignore[misc,no-any-return]


def methods(helper: Helper) -> list[tuple[str, str, Callable[[list[str]], bytes]]]:
    """(name, the git it runs, how)."""
    found = [
        ("subprocess.run, `git` by name", "git", checked()),
        ("subprocess.run, full path", GIT, checked()),
        ("subprocess.run, full path, close_fds=False", GIT, checked(close_fds=False)),
        ("subprocess.run, git-core's git, close_fds=False", GIT_CORE, checked(close_fds=False)),
        ("helper process (full path, close_fds=False inside)", GIT, helper.run),
    ]
    if real_posix_spawn is not None:
        found.append(("os.posix_spawn directly", GIT, posix_spawn_direct))
    return found


def time_method(run: Callable[[list[str]], bytes], argv: list[str], runs: int) -> tuple[float, float, int]:
    global spawned
    for _ in range(WARMUP):
        run(argv)
    spawned = 0
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        run(argv)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), statistics.mean(times), spawned


def measure(size: str, repo: str) -> list[dict[str, object]]:
    if real_posix_spawn is not None:
        os.posix_spawn = counting_posix_spawn  # type: ignore[assignment]
    helper = Helper()
    if size == "big":
        # Touch every page, so the memory is really mapped, as a pytest
        # worker's is, and import a good deal, as one has.
        ballast = bytearray(BIG_MB * 1024 * 1024)
        for i in range(0, len(ballast), 4096):
            ballast[i] = 1
        import asyncio, email.parser, http.server, json.decoder, unittest, xml.dom.minidom  # noqa: F401, I001
    rows = []
    for command, argv_tail in {"version": ["--version"], "rev-parse": ["-C", repo, "rev-parse", "HEAD"]}.items():
        for name, git, run in methods(helper):
            median, mean, used_spawn = time_method(run, [git, *argv_tail], RUNS[command])
            rows.append(
                {
                    "size": size,
                    "command": command,
                    "method": name,
                    "median": median,
                    "mean": mean,
                    "posix_spawn": used_spawn > 0 if name.startswith("subprocess") else None,
                }
            )
    helper.close()
    return rows


def main() -> None:
    if len(sys.argv) == 3:
        print(json.dumps(measure(sys.argv[1], sys.argv[2])))
        return
    with tempfile.TemporaryDirectory() as temp:
        repo = os.path.join(temp, "repo")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@a",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@a",
        }
        subprocess.run([GIT, "init", "-q", repo], check=True, env=env)
        subprocess.run([GIT, "-C", repo, "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
        rows = []
        for size in ("small", "big"):
            out = subprocess.run([sys.executable, __file__, size, repo], capture_output=True, text=True, check=True)
            rows += json.loads(out.stdout)

    git_version = subprocess.run([GIT, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    print(f"## {sys.platform}, Python {sys.version.split()[0]}, {os.cpu_count()} CPUs, {git_version}\n")
    print(f"- `git` on PATH: `{GIT}`; git-core's: `{GIT_CORE}`")
    print(
        f"- subprocess: _USE_POSIX_SPAWN={getattr(subprocess, '_USE_POSIX_SPAWN', None)},"
        f" _HAVE_POSIX_SPAWN_CLOSEFROM={getattr(subprocess, '_HAVE_POSIX_SPAWN_CLOSEFROM', None)},"
        f" _USE_VFORK={getattr(subprocess, '_USE_VFORK', None)}"
    )
    print(f"- big parent: {BIG_MB} MB touched, plus imports; runs: {RUNS}, after {WARMUP} warmup\n")
    print("| command | method | small parent: median / mean ms | big parent: median / mean ms | posix_spawn used |")
    print("|---|---|---|---|---|")
    by_key = {(r["size"], r["command"], r["method"]): r for r in rows}
    for r in rows:
        if r["size"] != "small":
            continue
        big = by_key[("big", r["command"], r["method"])]
        used = {True: "yes", False: "no", None: "n/a"}[r["posix_spawn"]]  # type: ignore[index]
        print(
            f"| {r['command']} | {r['method']} | {r['median']:.2f} / {r['mean']:.2f}"
            f" | {big['median']:.2f} / {big['mean']:.2f} | {used} |"
        )


if __name__ == "__main__":
    main()
