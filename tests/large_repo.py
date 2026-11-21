"""A large repository, for the tests marked `large`: a long history, thousands of active branches
and a busy main, built once with `git fast-import` (500,000 commits through `git commit` would
take hours) and copied for each test.

    origin.git   bare; main with `history` commits, and `branches` branches (feature/...) each
                 forked from main in the last `branch_window_days` days, with 1 to 3 commits
    local        a clone of it, with a local branch for each of them too, and a commit graph

main gets `commits_per_day` commits a day, so a stack forked N days ago has N * commits_per_day
commits on main to be restacked onto.
"""

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass

START = 1_577_836_800
"""2020-01-01: the time of main's first commit."""
DAY = 86_400


@dataclass(frozen=True)
class Recipe:
    """How big the repository is."""

    history: int = 500_000
    branches: int = 5_000
    commits_per_day: int = 500
    branch_window_days: int = 60

    def key(self) -> str:
        """A name for this repository, which changes with the recipe and with how it's built, so a
        cached one is never used for another."""
        source = pathlib.Path(__file__).read_bytes()
        digest = hashlib.sha256(source + repr(self).encode()).hexdigest()
        return f"{self.history}c-{self.branches}b-{digest[:12]}"

    def commit_time(self, number: int) -> int:
        return START + number * DAY // self.commits_per_day


def _commit(ref: str, mark: int | None, parent: int | None, time: int, message: str, path: str, text: str) -> bytes:
    """One `commit` command of git fast-import's stream: it makes a commit on `ref` that sets
    `path` to `text` (a file of its own, inline, so there's no separate blob to name).

    `mark` numbers the commit so a later one can name it as its `parent` (`from :n`); a commit
    with no `parent` continues what `ref` already has, which is how a branch's later commits, and
    main's, follow the one before."""
    lines = [f"commit {ref}"]
    if mark is not None:
        lines.append(f"mark :{mark}")
    lines.append(f"committer Test <test@example.com> {time} +0000")
    lines.append(f"data {len(message)}\n{message}")
    if parent is not None:
        lines.append(f"from :{parent}")
    lines.append(f"M 100644 inline {path}\ndata {len(text)}\n{text}\n")
    return ("\n".join(lines) + "\n").encode()


def stream(recipe: Recipe) -> Iterator[bytes]:
    """The commands `git fast-import` reads to build the repository."""
    for number in range(1, recipe.history + 1):
        # Each commit changes one of 3,000 files in 100 directories, which gives the trees a
        # realistic size and depth: neither one file every time, nor every file every time.
        # Every main commit is marked with its number, for the branches below to fork from.
        path = f"src/pkg{number % 100:03d}/module{number % 3000:04d}.py"
        yield _commit(
            "refs/heads/main", number, None, recipe.commit_time(number), f"Change {number}", path, f"VALUE = {number}\n"
        )
    # The active branches were forked recently: in the last `branch_window_days` days of main.
    first_forkable = max(1, recipe.history - recipe.branch_window_days * recipe.commits_per_day)
    span = recipe.history - first_forkable
    for branch in range(recipe.branches):
        # 7919 is prime, so stepping by it spreads the fork points over the whole window, the same
        # on every build (a random one would make each build, and the cached repository, differ).
        fork = first_forkable + (branch * 7919) % (span + 1)
        name = f"refs/heads/feature/user{branch % 50:02d}/topic-{branch:05d}"
        for own in range(1 + branch % 3):
            yield _commit(
                name,
                None,
                fork if own == 0 else None,
                recipe.commit_time(fork) + 60 * (own + 1),
                f"Topic {branch}, part {own + 1}",
                f"topics/topic-{branch:05d}/part{own + 1}.txt",
                f"topic {branch} part {own + 1}\n",
            )


def _git(*args: str, cwd: pathlib.Path, input: bytes | None = None) -> None:
    """Run git in cwd, failing if it does, with no config of the user's or the system's and with
    an author, so the repository is the same wherever it's built."""
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, input=input, env=environment, check=True, capture_output=True)


def build(directory: pathlib.Path, recipe: Recipe, progress: Callable[[str], None] = lambda message: None) -> None:
    """Make origin.git and local in directory, which is new. progress is told what it's doing, since
    it takes about a minute."""
    origin, local = directory / "origin.git", directory / "local"
    directory.mkdir(parents=True)
    _git("init", "--quiet", "--bare", "--initial-branch=main", str(origin), cwd=directory)
    progress("importing history and branches")
    importer = subprocess.Popen(
        ["git", "fast-import", "--quiet", "--done"],
        cwd=origin,
        stdin=subprocess.PIPE,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )
    assert importer.stdin is not None
    for chunk in stream(recipe):
        importer.stdin.write(chunk)
    importer.stdin.write(b"done\n")
    importer.stdin.close()
    if importer.wait() != 0:
        raise RuntimeError("git fast-import failed")
    # A repository people use has its refs packed, not 5,000 files, and a commit graph, which git
    # writes itself in the background (`git maintenance`): it speeds up walking history, such as
    # for a merge base. --changed-paths adds the Bloom filters that speed up a path's history.
    progress("packing refs and writing the commit graph")
    _git("pack-refs", "--all", cwd=origin)
    _git("commit-graph", "write", "--reachable", "--changed-paths", cwd=origin)
    # `git clone` gives the clone origin/<branch> for each branch, and a local one for main only.
    # People who work on thousands of active branches have many local ones too, and one
    # `update-ref --stdin` makes them all in a process, where `git branch` would start 5,000.
    progress("cloning, with a local branch for each")
    _git("clone", "--quiet", str(origin), str(local), cwd=directory)
    listing = subprocess.run(
        [
            "git",
            "for-each-ref",
            "--format=create refs/heads/%(refname:lstrip=3) %(objectname)",
            "refs/remotes/origin/feature",
        ],
        cwd=local,
        capture_output=True,
        check=True,
    ).stdout
    _git("update-ref", "--stdin", cwd=local, input=listing)
    _git("commit-graph", "write", "--reachable", "--changed-paths", cwd=local)


def is_git_object(relative: pathlib.PurePath) -> bool:
    """Whether the file at relative, a path in the git directory (see `copy`), is a pack or a loose
    object, which git never changes once it's written."""
    parts = relative.parts
    return parts[:2] == ("objects", "pack") or (
        len(parts) == 3 and parts[0] == "objects" and re.fullmatch("[0-9a-f]{2}", parts[1]) is not None
    )


def copy(built: pathlib.Path, destination: pathlib.Path) -> None:
    """A copy of a repository `build` made, quick because its objects are linked, not copied.

    The objects are hard-linked: a pack, or a loose object, is never changed once written. Everything
    else (a working tree file, the config, the refs, the index) is copied, since a test changes
    those in place, and a link would change the cached repository too. Which files are objects is
    judged by their path in the git directory (`.git`, or the repository itself if it's bare), not
    wherever the repository is: a checkout under a directory called `objects` is no different.

    A link that can't be made is an error, not a copy: the cached repository and its copy must be
    on one filesystem, or each test would write the whole repository (and fill a RAM-backed
    temporary directory)."""
    git_directory = built / ".git" if (built / ".git").is_dir() else built

    def link_or_copy(source: str, target: str) -> str:
        path = pathlib.Path(source)
        if not path.is_relative_to(git_directory) or not is_git_object(path.relative_to(git_directory)):
            shutil.copy2(source, target)
            return target
        try:
            os.link(source, target)
        except OSError as error:
            raise RuntimeError(
                f"can't link {source} to {target} ({error}): the cached repository and its copy "
                "have to be on the same filesystem"
            ) from error
        return target

    shutil.copytree(built, destination, copy_function=link_or_copy, symlinks=True)


def ensure_one_process() -> None:
    """Fail in a pytest-xdist worker: workers would each build the repository, and delete one
    another's half-built one. (`dev.py tests -m large` gets here with pyproject's `--numprocesses
    auto`; `dev.py large-tests` runs one.)"""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        raise RuntimeError("the large repository tests run in one process: use `dev.py large-tests`, or `-n 0`")


def prune(parent: pathlib.Path, keep: str) -> None:
    """Remove what's in parent, where repositories are built, besides the one named keep: builds
    made by an older recipe or version of this file, and the copies (`keep/work`) that a run
    which was killed left (the repository's other files are kept for the next). So one run at a
    time in a clone: another that started meanwhile would lose the copies it's using."""
    for entry in parent.iterdir():
        if entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)
    shutil.rmtree(parent / keep / "work", ignore_errors=True)
