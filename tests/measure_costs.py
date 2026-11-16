"""Time what the tests spend their time on, one operation at a time: starting
git, sh and Python, git commands that write, pushing and fetching between
repositories, and writing small files. Run by .github/workflows/measure.yml on
each CI system, to see where macOS and Windows are slower than Linux.

    python -m tests.measure_costs [<directory>]

Works in a new directory under <directory> (default: the system's temporary
directory), and prints a Markdown table of the median time of each, also
appending it to $GITHUB_STEP_SUMMARY if that's set.
"""

import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNS = 30


def git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def median_ms(operation: Callable[[], object], runs: int = RUNS) -> float:
    times: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        operation()
        times.append(time.perf_counter() - started)
    return statistics.median(times) * 1000


def measure(directory: pathlib.Path) -> list[tuple[str, float]]:
    origin, clone = directory / "origin.git", directory / "clone"
    config = directory / "gitconfig"
    config.write_text("[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n", encoding="utf-8")
    os.environ.update(
        GIT_CONFIG_GLOBAL=str(config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
        PYTHONPATH=str(ROOT),
    )
    git("init", "--quiet", "--bare", str(origin), cwd=directory)
    git("clone", "--quiet", str(origin), str(clone), cwd=directory)
    git("commit", "--quiet", "--allow-empty", "-m", "first", cwd=clone)
    git("push", "--quiet", "origin", "HEAD:main", cwd=clone)

    def push() -> None:
        git("commit", "--quiet", "--allow-empty", "-m", "next", cwd=clone)
        git("push", "--quiet", "origin", "HEAD:main", cwd=clone)

    def write_files() -> None:
        files = directory / "files"
        files.mkdir()
        for i in range(100):
            (files / str(i)).write_text("x" * 100, encoding="utf-8")
        shutil.rmtree(files)

    sh = shutil.which("sh")
    results = [
        ("git --version", median_ms(lambda: git("--version", cwd=clone))),
        ("git rev-parse HEAD", median_ms(lambda: git("rev-parse", "HEAD", cwd=clone))),
        ("git commit (empty)", median_ms(lambda: git("commit", "--quiet", "--allow-empty", "-m", "x", cwd=clone))),
        ("git commit + push to a local bare repository", median_ms(push)),
        ("git fetch from it", median_ms(lambda: git("fetch", "--quiet", "origin", cwd=clone))),
        ("python -c pass", median_ms(lambda: subprocess.run([sys.executable, "-c", "pass"], check=True))),
        (
            "python -c 'import git_stack.cli'",
            median_ms(lambda: subprocess.run([sys.executable, "-c", "import git_stack.cli"], check=True)),
        ),
        ("write and delete 100 small files", median_ms(write_files, runs=10)),
    ]
    if sh is not None:
        results.insert(2, ("sh -c true", median_ms(lambda: subprocess.run([sh, "-c", "true"], check=True))))
    return results


def main() -> None:
    parent = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None
    with tempfile.TemporaryDirectory(dir=parent, prefix="measure-") as directory:
        results = measure(pathlib.Path(directory))
    lines = [f"Median of each, in `{parent or tempfile.gettempdir()}`:", "", "| Operation | ms |", "|---|---:|"]
    lines += [f"| {name} | {ms:.1f} |" for name, ms in results]
    table = "\n".join(lines) + "\n"
    print(table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(table + "\n")


if __name__ == "__main__":
    main()
