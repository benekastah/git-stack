"""The CLI: how each command maps its arguments onto StackClient, and prints
what it returns. StackClient is a mock; tests/test_end_to_end.py runs the
real thing."""

import json
from typing import Any
from unittest.mock import create_autospec

import pytest

from git_stack.adapters.git import PullRequestLink, PullRequestState
from git_stack.cli import main
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import (
    AutocreateAction,
    Autocreated,
    BranchInfo,
    BranchStatus,
    PullRequestAction,
    PullRequestStep,
    Renamed,
    StackClient,
    Tracked,
    Untracked,
)


@pytest.fixture
def stack() -> Any:
    """A mock StackClient whose methods check their arguments against the real ones."""
    stack = create_autospec(StackClient, instance=True)
    stack.git = create_autospec(GitClient, instance=True)
    stack.git.run.return_value = ""  # e.g. git config --get of an unset setting
    stack.branch.return_value = "main"
    stack.track.return_value = []
    stack.autocreate.return_value = []
    stack.untrack.return_value = [Untracked("pr1", children=())]
    stack.rename.return_value = Renamed("old", "new", children=(), upstream=None)
    stack.info.return_value = []
    stack.sync.return_value = []
    stack.push.return_value = ["pr1"]
    stack.prs.return_value = []
    stack.next.return_value = "pr2"
    stack.prev.return_value = "main"
    stack.parent.return_value = "main"
    stack.base.return_value = "a" * 40
    stack.show_stack.return_value = ["pr1", "pr2"]
    return stack


@pytest.mark.parametrize(
    ("argv", "method", "args", "kwargs"),
    [
        (["branch", "pr1"], "branch", ("pr1",), {}),
        (["track", "main"], "track", ("main", []), {"base": None}),
        (["track", "main", "pr1", "pr2", "--base", "abc123"], "track", ("main", ["pr1", "pr2"]), {"base": "abc123"}),
        (["autocreate"], "autocreate", (), {"remote": "origin", "dry_run": False}),
        (["autocreate", "-n", "--remote", "upstream"], "autocreate", (), {"remote": "upstream", "dry_run": True}),
        (["untrack"], "untrack", ([],), {}),
        (["untrack", "pr1", "pr2"], "untrack", (["pr1", "pr2"],), {}),
        (["rename", "new"], "rename", ("new",), {"old": None}),
        (["rename", "old", "new"], "rename", ("new",), {"old": "old"}),
        (["info"], "info", (), {"offline": False}),
        (["info", "--offline"], "info", (), {"offline": True}),
        (["info", "--json"], "info", (), {"offline": False}),
        (["next"], "next", (), {}),
        (["prev"], "prev", (), {}),
        (["restack"], "restack", (), {"onto": None, "squash": False, "latest": False}),
        (
            ["restack", "--onto", "main", "--squash", "--latest"],
            "restack",
            (),
            {"onto": "main", "squash": True, "latest": True},
        ),
        (["sync"], "sync", (), {"remote": "origin", "delete": None, "latest": False}),
        (
            ["sync", "--remote", "upstream", "-d", "local", "--latest"],
            "sync",
            (),
            {"remote": "upstream", "delete": "local", "latest": True},
        ),
        (["clean"], "clean", (), {"remote": "origin", "delete": None}),
        (["clean", "--delete", "both"], "clean", (), {"remote": "origin", "delete": "both"}),
        (["push"], "push", (None,), {"remote": "origin", "force": False}),
        (["push", "pr1", "--remote", "upstream"], "push", ("pr1",), {"remote": "upstream", "force": False}),
        (["push", "--force"], "push", (None,), {"remote": "origin", "force": True}),
        (["prs"], "prs", (), {"remote": "origin", "dry_run": False}),
        (["prs", "-n", "--remote", "upstream"], "prs", (), {"remote": "upstream", "dry_run": True}),
        (["pr"], "prs", (), {"remote": "origin", "dry_run": False}),
        (["parent"], "parent", (None,), {}),
        (["parent", "pr1"], "parent", ("pr1",), {}),
        (["base"], "base", (None,), {}),
        (["base", "pr1"], "base", ("pr1",), {}),
        (["show-stack"], "show_stack", (None,), {}),
        (["show-stack", "-q", "pr1"], "show_stack", ("pr1",), {}),
    ],
)
def test_command_calls_its_client_method(
    stack: Any, argv: list[str], method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    assert main(argv, stack=stack) == 0

    getattr(stack, method).assert_called_once_with(*args, **kwargs)


@pytest.mark.parametrize("dry_run", [False, True])
def test_autocreate_says_what_it_did_for_each_pull_request(
    stack: Any, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    stack.autocreate.return_value = [
        Autocreated("pr1", "main", 1, AutocreateAction.CREATE, base="a" * 40),
        Autocreated("pr2", "pr1", 2, AutocreateAction.TRACK, base="b" * 40),
        Autocreated("pr3", "pr2", 3, AutocreateAction.ALREADY_TRACKED),
        Autocreated("pr4", "pr2", 4, AutocreateAction.PARENT_DIFFERS, tracked_parent="main"),
        Autocreated("pr5", "theirs", 5, AutocreateAction.SKIP, reason="theirs isn't a local branch"),
    ]

    main(["autocreate", *(["--dry-run"] if dry_run else [])], stack=stack)

    created, tracking = ("would create", "would track") if dry_run else ("created", "tracking")
    assert capsys.readouterr().out.splitlines() == [
        f"{created} pr1 from origin/pr1, tracking it on main (#1)",
        f"{tracking} pr2 on pr1 (#2)",
        "pr3 is already tracked on pr2 (#3)",
        "pr4 is tracked on main, but #4 merges it into pr2; leaving it (to follow GitHub, run "
        "`git stack track pr2 pr4`)",
        "skipping pr5 (#5): theirs isn't a local branch",
    ]


def test_autocreate_without_pull_requests_says_so(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["autocreate"], stack=stack)

    assert capsys.readouterr().out == "you have no open pull requests from branches in this repository\n"


@pytest.mark.parametrize("dry_run", [False, True])
def test_prs_says_what_it_did_for_each_branch(stack: Any, capsys: pytest.CaptureFixture[str], dry_run: bool) -> None:
    url = "https://github.com/acme/widgets/pull/1"
    stack.prs.return_value = [
        PullRequestStep("pr1", "main", PullRequestAction.OPEN, "Add the API", *((None, None) if dry_run else (1, url))),
        PullRequestStep("pr2", "pr1", PullRequestAction.EXISTS, number=7),
        PullRequestStep("pr3", "pr2", PullRequestAction.NO_COMMITS),
    ]

    main(["prs", *(["--dry-run"] if dry_run else [])], stack=stack)

    assert capsys.readouterr().out.splitlines() == [
        "would open a draft pull request for pr1 into main: Add the API"
        if dry_run
        else f"opened draft pull request #1 for pr1 into main: {url}",
        "pr2 already has pull request #7",
        "skipping pr3: it has no commits of its own",
    ]


def test_prs_says_when_everything_looks_merged(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["prs"], stack=stack)

    assert capsys.readouterr().out == "nothing to open: every branch in the current stack looks merged\n"


def test_stack_error_is_printed_and_fails(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.restack.side_effect = StackError("main isn't in a stack")

    assert main(["restack"], stack=stack) == 1

    assert capsys.readouterr() == ("", "git stack: main isn't in a stack\n")


def test_branch_prints_parent(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["branch", "pr1"], stack=stack)

    assert capsys.readouterr().out == "created pr1 on top of main\n"


def test_track_prints_each_branch(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.track.return_value = [Tracked("pr1", "main", "a" * 40), Tracked("pr2", "pr1", "b" * 40)]

    main(["track", "main", "pr1", "pr2"], stack=stack)

    assert capsys.readouterr().out == (
        f"tracking pr1 on top of main (base {'a' * 12})\ntracking pr2 on top of pr1 (base {'b' * 12})\n"
    )


def test_rename_prints_moved_children_and_old_upstream(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.rename.return_value = Renamed("pr1", "first", children=("pr2", "pr2b"), upstream="origin/pr1")

    main(["rename", "pr1", "first"], stack=stack)

    out = capsys.readouterr().out
    assert out.startswith("renamed pr1 to first and moved pr2, pr2b onto it\n")
    assert "note: origin/pr1 wasn't renamed" in out


def test_info_draws_each_stack_as_a_tree(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo(
            "pr1",
            "main",
            BranchStatus.UP_TO_DATE,
            current=False,
            pull_request=PullRequestLink(9, "https://github.com/acme/widgets/pull/9", PullRequestState.OPEN),
        ),
        BranchInfo(
            "pr2",
            "pr1",
            BranchStatus.NEEDS_RESTACK,
            current=False,
            pull_request=PullRequestLink(13, "https://github.com/acme/widgets/pull/13", PullRequestState.OPEN),
        ),
        BranchInfo("pr2b", "pr1", BranchStatus.MERGED, current=False),
        BranchInfo("solo", "main", BranchStatus.MERGED_WITH_NEW_COMMITS, current=True),
        BranchInfo("release", None, None, current=False),
        BranchInfo("hotfix", "release", BranchStatus.PARENT_MISSING, current=False),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == (
        "main\n"
        "├── pr1                                 https://github.com/acme/widgets/pull/9\n"
        "│   ├── pr2 (needs restack)            https://github.com/acme/widgets/pull/13\n"
        "│   └── pr2b (merged)\n"
        "└── solo * (merged, with new commits)\n"
        "release\n"
        "└── hotfix (parent release missing)\n"
    )


def test_info_colors_each_part(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "true" if "--get-colorbool" in args else ""
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo(
            "pr1",
            "main",
            BranchStatus.MERGED,
            current=False,
            pull_request=PullRequestLink(1, "https://github.com/acme/widgets/pull/1", PullRequestState.MERGED),
        ),
        BranchInfo(
            "pr2",
            "pr1",
            BranchStatus.NEEDS_RESTACK,
            current=True,
            pull_request=PullRequestLink(2, "https://github.com/acme/widgets/pull/2", PullRequestState.OPEN),
        ),
        BranchInfo(
            "pr3",
            "pr2",
            BranchStatus.UP_TO_DATE,
            current=False,
            pull_request=PullRequestLink(3, "https://github.com/acme/widgets/pull/3", PullRequestState.DRAFT),
        ),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == (
        "\033[1mmain\033[m\n"
        "\033[2m└── \033[mpr1 \033[36m(merged)\033[m"
        + " "
        * 15
        + "\033[35mhttps://github.com/acme/widgets/pull/1\033[m\n"
        "\033[2m    └── \033[m\033[1;32mpr2 *\033[m \033[33m(needs restack)\033[m"
        "  \033[34mhttps://github.com/acme/widgets/pull/2\033[m\n"
        "\033[2m        └── \033[mpr3" + " " * 16 + "\033[90mhttps://github.com/acme/widgets/pull/3\033[m\n"
    )


def test_info_can_draw_each_stack_with_its_root_at_the_bottom(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "root-at-bottom" if "stack.orientation" in args else ""
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=False),
        BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=True),
        BranchInfo("pr2b", "pr1", BranchStatus.UP_TO_DATE, current=False),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == ("    ┌── pr2b\n    ├── pr2 *\n┌── pr1\nmain\n")


def test_info_refuses_an_unknown_orientation(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "sideways" if "stack.orientation" in args else ""
    stack.info.return_value = [BranchInfo("main", None, None, current=False)]

    assert main(["info"], stack=stack) == 1

    assert "stack.orientation must be root-at-top or root-at-bottom, not sideways" in capsys.readouterr().err


def test_info_without_stacks(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["info"], stack=stack)

    assert capsys.readouterr().out == "no tracked branches\n"


def test_sync_lists_branches_left_to_push(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.sync.return_value = ["pr2", "pr3"]

    main(["sync"], stack=stack)

    assert capsys.readouterr().out == (
        "note: run `git stack push` to update the remote branches and pull requests of:\n  pr2\n  pr3\n"
    )


def test_sync_with_nothing_to_push_prints_nothing(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["sync"], stack=stack)

    assert capsys.readouterr().out == ""


def test_push_says_when_everything_looks_merged(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.push.return_value = []
    stack.git.current_branch.return_value = "pr1"

    main(["push"], stack=stack)

    assert capsys.readouterr().out == "nothing to push: every branch in pr1's stack looks merged\n"


def test_untrack_prints_branches_left_on_it(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.untrack.return_value = [Untracked("pr1", children=("pr2", "pr2b"))]

    main(["untrack", "pr1"], stack=stack)

    assert capsys.readouterr().out == (
        "stopped tracking pr1\n"
        "note: pr2, pr2b still stacked on pr1, which is now the bottom of their stack. "
        "Move them with `git stack restack --onto <parent>`\n"
    )


def test_info_json_lists_every_branch(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo(
            "pr1",
            "main",
            BranchStatus.NEEDS_RESTACK,
            current=True,
            pull_request=PullRequestLink(12, "https://github.com/acme/widgets/pull/12", PullRequestState.OPEN),
        ),
    ]

    main(["info", "--json"], stack=stack)

    assert json.loads(capsys.readouterr().out) == [
        {"name": "main", "parent": None, "status": None, "current": False, "pr": None},
        {
            "name": "pr1",
            "parent": "main",
            "status": "needs-restack",
            "current": True,
            "pr": {"number": 12, "url": "https://github.com/acme/widgets/pull/12", "state": "open"},
        },
    ]


@pytest.mark.parametrize(
    ("argv", "out"),
    [
        (["next"], "switched to pr2\n"),
        (["prev"], "switched to main\n"),
        (["parent"], "main\n"),
        (["base"], "a" * 40 + "\n"),
        (["show-stack"], "pr1\npr2\n"),
    ],
)
def test_command_prints_its_result(stack: Any, capsys: pytest.CaptureFixture[str], argv: list[str], out: str) -> None:
    assert main(argv, stack=stack) == 0

    assert capsys.readouterr().out == out


def test_show_stack_quiet_prints_nothing_for_a_stacked_branch(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show-stack", "--quiet"], stack=stack) == 0

    assert capsys.readouterr() == ("", "")


def test_show_stack_quiet_fails_silently_outside_a_stack(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.show_stack.side_effect = StackError("main isn't in a stack")

    assert main(["show-stack", "--quiet"], stack=stack) == 1

    assert capsys.readouterr() == ("", "")


def test_show_stack_outside_a_stack_says_so(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.show_stack.side_effect = StackError("main isn't in a stack")

    assert main(["show-stack"], stack=stack) == 1

    assert capsys.readouterr() == ("", "git stack: main isn't in a stack\n")
