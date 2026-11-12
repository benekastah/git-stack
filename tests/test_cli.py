"""The CLI: how each command maps its arguments onto StackClient, and prints
what it returns. StackClient is a mock; tests/test_end_to_end.py runs the
real thing."""

import json
import sys
from typing import Any
from unittest.mock import create_autospec

import pytest

from git_stack import events
from git_stack.adapters.git import PullRequestLink, PullRequestState
from git_stack.cli import main
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.json_fields import to_json
from git_stack.stack import (
    BranchInfo,
    BranchStatus,
    ImportAction,
    ImportResult,
    ImportStep,
    ImportStepWithoutPr,
    PrsResult,
    PullRequestAction,
    PullRequestStep,
    PushResult,
    Renamed,
    StackClient,
    SyncResult,
    Tracked,
    Unreverted,
    Untracked,
)

# A stack, and how print_tree draws it without color.
STACK = [
    BranchInfo("main", None, None, current=False),
    BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=True),
]
STACK_TREE = "main\n└── pr1 *\n"


@pytest.fixture
def stack() -> Any:
    """A mock StackClient whose methods check their arguments against the real ones."""
    stack = create_autospec(StackClient, instance=True)
    stack.git = create_autospec(GitClient, instance=True)
    stack.git.run.return_value = ""  # e.g. git config --get of an unset setting
    stack.branch.return_value = "main"
    stack.track.return_value = []
    stack.import_stacks.return_value = ImportResult([], [])
    stack.untrack.return_value = [Untracked("pr1", children=())]
    stack.rename.return_value = Renamed("old", "new", children=(), upstream=None)
    stack.info.return_value = []
    stack.sync.return_value = SyncResult([], [])
    stack.push.return_value = PushResult(["pr1"], [])
    stack.clean.return_value = []
    stack.prs.return_value = PrsResult([], [])
    stack.next.return_value = "pr2"
    stack.prev.return_value = "main"
    stack.parent.return_value = "main"
    stack.base.return_value = "a" * 40
    stack.show_stack.return_value = ["pr1", "pr2"]
    stack.group_add.return_value = ["pr1"]
    stack.group_remove.return_value = []
    stack.group_rename.return_value = ("old", [])
    return stack


@pytest.mark.parametrize(
    ("argv", "method", "args", "kwargs"),
    [
        (["branch", "pr1"], "branch", ("pr1",), {"group": None}),
        (["branch", "pr1", "--group", "feat"], "branch", ("pr1",), {"group": "feat"}),
        (["track", "main"], "track", ("main", []), {"base": None}),
        (["track", "main", "pr1", "pr2", "--base", "abc123"], "track", ("main", ["pr1", "pr2"]), {"base": "abc123"}),
        (
            ["import"],
            "import_stacks",
            (),
            {"remote": "origin", "dry_run": False, "force": False, "author": None, "branches": [], "prs": []},
        ),
        (
            ["import", "-n", "--remote", "upstream", "--author", "alice", "api", "12", "--pr", "3", "--pr", "4"],
            "import_stacks",
            (),
            {
                "remote": "upstream",
                "dry_run": True,
                "force": False,
                "author": "alice",
                "branches": ["api", "12"],
                "prs": ["3", "4"],
            },
        ),
        (
            ["autocreate", "-f"],
            "import_stacks",
            (),
            {"remote": "origin", "dry_run": False, "force": True, "author": None, "branches": [], "prs": []},
        ),
        (["untrack"], "untrack", ([],), {}),
        (["untrack", "pr1", "pr2"], "untrack", (["pr1", "pr2"],), {}),
        (["rename", "new"], "rename", ("new",), {"old": None}),
        (["rename", "old", "new"], "rename", ("new",), {"old": "old"}),
        (["info"], "info", ([], []), {"offline": False}),
        (["info", "fix-*", "-g", "search", "--offline"], "info", (["fix-*"], ["search"]), {"offline": True}),
        (["info", "--json"], "info", ([], []), {"offline": False}),
        (["next"], "next", (), {}),
        (["prev"], "prev", (), {}),
        (["restack"], "restack", ([], []), {"onto": None, "squash": False, "latest": False}),
        (["restack", "--allow-shared"], "restack", ([], []), {"onto": None, "squash": False, "latest": False}),
        (
            ["restack", "--onto", "main", "--squash", "--latest"],
            "restack",
            ([], []),
            {"onto": "main", "squash": True, "latest": True},
        ),
        (
            ["sync"],
            "sync",
            ([], []),
            {"remote": "origin", "delete": None, "latest": False, "push": True, "ignore_reverts": []},
        ),
        (
            ["sync", "--no-push"],
            "sync",
            ([], []),
            {"remote": "origin", "delete": None, "latest": False, "push": False, "ignore_reverts": []},
        ),
        (
            ["sync", "--allow-shared"],
            "sync",
            ([], []),
            {"remote": "origin", "delete": None, "latest": False, "push": True, "ignore_reverts": []},
        ),
        (
            ["sync", "b", "-g", "one", "--group", "two", "--remote", "upstream", "-d", "local", "--latest"],
            "sync",
            (["b"], ["one", "two"]),
            {"remote": "upstream", "delete": "local", "latest": True, "push": True, "ignore_reverts": []},
        ),
        (["clean"], "clean", (), {"remote": "origin", "delete": None, "ignore_reverts": []}),
        (
            ["clean", "--delete", "both", "--ignore-revert", "a", "--ignore-revert", "b"],
            "clean",
            (),
            {"remote": "origin", "delete": "both", "ignore_reverts": ["a", "b"]},
        ),
        (["push"], "push", ([], []), {"remote": "origin", "force": False, "prs": False}),
        (
            ["push", "pr1", "b-*", "--remote", "upstream"],
            "push",
            (["pr1", "b-*"], []),
            {"remote": "upstream", "force": False, "prs": False},
        ),
        (["push", "--force"], "push", ([], []), {"remote": "origin", "force": True, "prs": False}),
        (["push", "--allow-shared"], "push", ([], []), {"remote": "origin", "force": False, "prs": False}),
        (["push", "--prs"], "push", ([], []), {"remote": "origin", "force": False, "prs": True}),
        (["push", "--pr"], "push", ([], []), {"remote": "origin", "force": False, "prs": True}),
        (["group", "add", "feat", "feat-*", "-g", "old"], "group_add", ("feat", ["feat-*"], ["old"]), {"move": False}),
        (["group", "add", "--move", "feat"], "group_add", ("feat", [], []), {"move": True}),
        (["group", "remove"], "group_remove", ([], []), {}),
        (["ungroup", "pr1", "-g", "feat"], "group_remove", (["pr1"], ["feat"]), {}),
        (["group", "rename", "new"], "group_rename", ("new",), {"old": None}),
        (["group", "rename", "old", "new"], "group_rename", ("new",), {"old": "old"}),
        (["ownership", "shared"], "set_ownership", ("shared", [], []), {}),
        (["prs"], "prs", ([], []), {"remote": "origin", "dry_run": False, "rebuild_stack": False}),
        (["pr", "--rebuild-stack"], "prs", ([], []), {"remote": "origin", "dry_run": False, "rebuild_stack": True}),
        (
            ["prs", "pr1", "-n", "--remote", "upstream"],
            "prs",
            (["pr1"], []),
            {"remote": "upstream", "dry_run": True, "rebuild_stack": False},
        ),
        (["pr"], "prs", ([], []), {"remote": "origin", "dry_run": False, "rebuild_stack": False}),
        (["unrevert", "pr1-unrevert", "abc"], "unrevert", ("pr1-unrevert", ["abc"], None, []), {"remote": "origin"}),
        (
            ["unrevert", "new", "abc", "def", "--from", "release", "--stack", "pr2", "--stack", "pr2b"],
            "unrevert",
            ("new", ["abc", "def"], "release", ["pr2", "pr2b"]),
            {"remote": "origin"},
        ),
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

    if method in ("restack", "sync", "push"):
        kwargs = {**kwargs, "allow_shared": "--allow-shared" in argv}
    if method == "sync":
        kwargs = {**kwargs, "pull_tracked_upstream": "--pull-tracked-upstream" in argv}
    getattr(stack, method).assert_called_once_with(*args, **kwargs)


IMPORTED = [
    ImportStep("pr1", "main", 1, ImportAction.CREATE, base="a" * 40),
    ImportStep("pr2", "pr1", 2, ImportAction.TRACK, base="b" * 40),
    ImportStep("pr3", "pr2", 3, ImportAction.ALREADY_TRACKED),
    ImportStep("pr4", "pr2", 4, ImportAction.PARENT_DIFFERS, tracked_parent="main"),
    ImportStep("pr5", "theirs", 5, ImportAction.SKIP, reason="theirs isn't a local branch"),
    ImportStep("pr6", "main", 6, ImportAction.CREATE, base="d" * 40, other_authors=["alice"]),
]
WITHOUT_PRS = [
    ImportStepWithoutPr("base", "main", ImportAction.CREATE, base="c" * 40),
    ImportStepWithoutPr("hers", "base", ImportAction.CREATE, base="e" * 40, other_authors=["alice@example.com"]),
    ImportStepWithoutPr("theirs", "main", ImportAction.SKIP, reason="it has commits by someone@example.com"),
]
PARENT_DIFFERS = (
    "pr4 is tracked on main, but #4 merges it into pr2; leaving it (to follow GitHub, run `git stack track pr2 pr4`)"
)


def test_import_dry_run_says_what_it_would_do_for_each_pull_request(
    stack: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.import_stacks.return_value = ImportResult(IMPORTED, [], WITHOUT_PRS)

    main(["import", "--dry-run"], stack=stack)

    assert capsys.readouterr().out.splitlines() == [
        "would create base from origin/base, tracking it on main (no pull request)",
        "would create hers from origin/hers, tracking it on base (no pull request, commits by alice@example.com, needs -f)",
        "would create pr1 from origin/pr1, tracking it on main (#1)",
        "would track pr2 on pr1 (#2)",
        "pr3 is already tracked on pr2 (#3)",
        PARENT_DIFFERS,
        "skipping pr5 (#5): theirs isn't a local branch",
        "would create pr6 from origin/pr6, tracking it on main (#6, by alice, needs -f)",
    ]


def test_import_shows_the_stacks_with_what_it_tracked_in_green(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "true" if "--get-colorbool" in args else ""

    def info(name: str, parent: str, number: int) -> BranchInfo:
        url = f"https://github.com/acme/widgets/pull/{number}"
        link = PullRequestLink(number, url, PullRequestState.OPEN)
        return BranchInfo(name, parent, BranchStatus.UP_TO_DATE, current=False, pull_request=link)

    stack.import_stacks.return_value = ImportResult(
        IMPORTED,
        [BranchInfo("main", None, None, current=False), info("pr1", "main", 1), info("pr2", "pr1", 2)],
    )

    main(["import"], stack=stack)

    assert capsys.readouterr().out.splitlines() == [
        PARENT_DIFFERS,
        "skipping pr5 (#5): theirs isn't a local branch",
        "\033[1mmain\033[m",
        "\033[2m└── \033[mpr1      \033[32mhttps://github.com/acme/widgets/pull/1\033[m",
        "\033[2m    └── \033[mpr2  \033[32mhttps://github.com/acme/widgets/pull/2\033[m",
    ]


def test_import_without_pull_requests_says_so(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["import"], stack=stack)

    assert capsys.readouterr().out == "you have no open pull requests from branches in this repository\n"


def test_import_without_another_authors_pull_requests_says_so(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["import", "--author", "alice"], stack=stack)

    assert capsys.readouterr().out == "alice has no open pull requests from branches in this repository\n"


def test_import_takes_dry_run_or_force_but_not_both(stack: Any) -> None:
    with pytest.raises(SystemExit):
        main(["import", "-n", "-f"], stack=stack)

    stack.import_stacks.assert_not_called()


@pytest.mark.parametrize("dry_run", [False, True])
def test_prs_shows_the_stack_with_new_pull_requests_in_green(
    stack: Any, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "true" if "--get-colorbool" in args else ""
    url = "https://github.com/acme/widgets/pull/1"
    existing = PullRequestLink(7, "https://github.com/acme/widgets/pull/7", PullRequestState.OPEN)
    stack.prs.return_value = PrsResult(
        [
            PullRequestStep(
                "pr1", "main", PullRequestAction.OPEN, "Add the API", *((None, None) if dry_run else (1, url))
            ),
            PullRequestStep("pr2", "pr1", PullRequestAction.EXISTS, number=7),
            PullRequestStep("pr3", "pr2", PullRequestAction.NO_COMMITS),
        ],
        [
            BranchInfo("main", None, None, current=False),
            BranchInfo(
                "pr1",
                "main",
                BranchStatus.UP_TO_DATE,
                current=False,
                pull_request=None if dry_run else PullRequestLink(1, url, PullRequestState.DRAFT),
            ),
            BranchInfo("pr2", "pr1", BranchStatus.UP_TO_DATE, current=False, pull_request=existing),
            BranchInfo("pr3", "pr2", BranchStatus.UP_TO_DATE, current=False),
        ],
    )

    main(["prs", *(["--dry-run"] if dry_run else [])], stack=stack)

    new = "would open: Add the API" if dry_run else url
    width = max(len(new), len(existing.url))
    assert capsys.readouterr().out.splitlines() == [
        "skipping pr3: it has no commits of its own",
        "\033[1mmain\033[m",
        f"\033[2m└── \033[mpr1          \033[32m{new.rjust(width)}\033[m",
        f"\033[2m    └── \033[mpr2      \033[34m{existing.url.rjust(width)}\033[m",
        "\033[2m        └── \033[mpr3",
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
        BranchInfo("undone", "main", BranchStatus.REVERTED, current=False),
        BranchInfo("release", None, None, current=False),
        BranchInfo("hotfix", "release", BranchStatus.PARENT_MISSING, current=False),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == (
        "main\n"
        "├── pr1                                 https://github.com/acme/widgets/pull/9\n"
        "│   ├── pr2 (needs restack)            https://github.com/acme/widgets/pull/13\n"
        "│   └── pr2b (merged)\n"
        "├── solo * (merged, with new commits)\n"
        "└── undone (merged, then reverted)\n"
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

    lines = capsys.readouterr().out.splitlines()
    assert lines[:4] == [
        "Key: \033[33mbranch\033[m ! needs restack; * current branch",
        "     status: \033[36mcyan\033[m merged; \033[33myellow\033[m needs attention; \033[31mred\033[m missing parent",
        "     PR: \033[34mblue\033[m open; \033[90mgrey\033[m draft; \033[35mmagenta\033[m merged",
        "",
    ]
    assert lines[4] == "\033[1mmain\033[m"
    assert "pr1 \033[36m(merged)\033[m" in lines[5]
    assert "\033[1;33mpr2 *\033[m !" in lines[6]
    assert "(needs restack)" not in lines[6]
    assert "\033[35mhttps://github.com/acme/widgets/pull/1\033[m" in lines[5]
    assert "\033[34mhttps://github.com/acme/widgets/pull/2\033[m" in lines[6]
    assert "\033[90mhttps://github.com/acme/widgets/pull/3\033[m" in lines[7]


def test_info_colors_non_current_branch_needing_restack(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.git.run.side_effect = lambda *args, check=True: "true" if "--get-colorbool" in args else ""
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.NEEDS_RESTACK, current=False),
    ]

    main(["info"], stack=stack)

    assert "\033[33mpr1\033[m !" in capsys.readouterr().out


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


def test_sync_shows_the_stacks_then_lists_branches_left_to_push(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.sync.return_value = SyncResult(["pr2", "pr3"], STACK)

    main(["sync"], stack=stack)

    assert capsys.readouterr().out == STACK_TREE + (
        "note: run `git stack push` to update the remote branches and pull requests of:\n  pr2\n  pr3\n"
    )


def test_sync_with_nothing_to_push_prints_nothing(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["sync"], stack=stack)

    assert capsys.readouterr().out == ""


def test_push_shows_the_stacks(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.push.return_value = PushResult(["pr1"], STACK)

    main(["push"], stack=stack)

    assert capsys.readouterr().out == STACK_TREE


def test_push_with_prs_shows_what_it_skipped_then_the_stacks(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.push.return_value = PushResult(["pr1"], STACK, [PullRequestStep("pr1", "main", PullRequestAction.NO_COMMITS)])

    main(["push", "--prs"], stack=stack)

    assert capsys.readouterr().out == f"skipping pr1: it has no commits of its own\n{STACK_TREE}"


PUSHED_WITHOUT_PRS = PushResult(["pr1"], STACK, error="pushed, but didn't open or update any pull requests: no gh")


def test_push_with_prs_shows_the_stacks_then_fails_if_it_opened_none(
    stack: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.push.return_value = PUSHED_WITHOUT_PRS

    assert main(["push", "--prs"], stack=stack) == 1

    out, err = capsys.readouterr()
    assert out == STACK_TREE
    assert err == "git stack: pushed, but didn't open or update any pull requests: no gh\n"


def test_push_with_prs_json_has_the_result_then_fails_if_it_opened_none(
    stack: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.push.return_value = PUSHED_WITHOUT_PRS

    assert main(["push", "--prs", "--json"], stack=stack) == 1

    line = json.loads(capsys.readouterr().out)
    assert (line["type"], line["pushed"], line["error"]) == ("result", ["pr1"], PUSHED_WITHOUT_PRS.error)


def test_clean_shows_the_stacks(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.clean.return_value = STACK

    main(["clean"], stack=stack)

    assert capsys.readouterr().out == STACK_TREE


def test_clean_json_prints_each_event_then_the_result(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    def clean(**kwargs: Any) -> list[BranchInfo]:
        stack.on_event(events.Fetching("origin"))
        return STACK

    stack.clean.side_effect = clean

    assert main(["clean", "--json"], stack=stack) == 0

    out, err = capsys.readouterr()
    fetching = {
        "type": "fetching",
        "message": "fetching origin, and pruning its deleted branches",
        "warning": False,
        "remote": "origin",
    }
    # The last line repeats every event, for consumers that only read it.
    assert [json.loads(line) for line in out.splitlines()] == [
        fetching,
        {"type": "result", "stacks": to_json(STACK), "events": [fetching]},
    ]
    assert err == ""
    assert stack.git.passthrough_stdout is sys.stderr


def test_clean_json_ends_with_the_error_when_it_fails(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    def clean(**kwargs: Any) -> list[BranchInfo]:
        stack.on_event(events.Fetching("origin"))
        raise StackError("fetching origin failed")

    stack.clean.side_effect = clean

    assert main(["clean", "--json"], stack=stack) == 1

    out, err = capsys.readouterr()
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["type"] for line in lines] == ["fetching", "error"]
    assert lines[-1] == {"type": "error", "message": "fetching origin failed", "events": [lines[0]]}
    assert err == "git stack: fetching origin failed\n"


@pytest.mark.parametrize(
    ("argv", "method", "returned", "result"),
    [
        (["ownership", "shared"], "set_ownership", ["pr1"], {"ownership": "shared", "branches": ["pr1"]}),
        (["sync"], "sync", SyncResult(["pr1"], STACK), {"stacks": to_json(STACK), "unpushed": ["pr1"]}),
        (
            ["push"],
            "push",
            PushResult(["pr1"], STACK),
            {"stacks": to_json(STACK), "pushed": ["pr1"], "steps": None, "error": None},
        ),
        (
            ["push", "--prs"],
            "push",
            PushResult(["pr1"], STACK, [PullRequestStep("pr1", "main", PullRequestAction.EXISTS, number=12)]),
            {
                "stacks": to_json(STACK),
                "pushed": ["pr1"],
                "steps": [
                    {"branch": "pr1", "parent": "main", "action": "exists", "title": None, "number": 12, "url": None}
                ],
                "error": None,
            },
        ),
        (
            ["prs"],
            "prs",
            PrsResult([PullRequestStep("pr1", "main", PullRequestAction.OPEN, "Add pr1", 12, "https://x/12")], STACK),
            {
                "stacks": to_json(STACK),
                "steps": [
                    {
                        "branch": "pr1",
                        "parent": "main",
                        "action": "open",
                        "title": "Add pr1",
                        "number": 12,
                        "url": "https://x/12",
                    }
                ],
            },
        ),
        (["restack"], "restack", None, {}),
        (
            ["unrevert", "pr1-unrevert", "abc", "--stack", "pr2"],
            "unrevert",
            Unreverted("pr1-unrevert", "main", ("abc",), ("pr2",)),
            {"branch": "pr1-unrevert", "parent": "main", "reverts": ["abc"], "children": ["pr2"]},
        ),
        (
            ["track", "main"],
            "track",
            [Tracked("pr1", "main", "abc")],
            {"tracked": [{"branch": "pr1", "parent": "main", "base": "abc"}]},
        ),
        (["untrack"], "untrack", [Untracked("pr1", ("pr2",))], {"untracked": [{"branch": "pr1", "children": ["pr2"]}]}),
        (
            ["rename", "new"],
            "rename",
            Renamed("old", "new", ("pr2",), "origin/old"),
            {"old": "old", "new": "new", "children": ["pr2"], "upstream": "origin/old"},
        ),
        (["branch", "pr2", "--group", "g"], "branch", "pr1", {"branch": "pr2", "parent": "pr1", "group": "g"}),
        (["group", "add", "g", "pr1"], "group_add", ["pr1"], {"group": "g", "bottoms": ["pr1"]}),
        (["group", "remove"], "group_remove", [("pr1", "g")], {"removed": [{"bottom": "pr1", "group": "g"}]}),
        (["ungroup"], "group_remove", [], {"removed": []}),
        (
            ["group", "rename", "new"],
            "group_rename",
            ("old", ["pr1"]),
            {"old": "old", "new": "new", "branches": ["pr1"]},
        ),
        (["parent"], "parent", "main", {"parent": "main"}),
        (["base"], "base", "abc", {"base": "abc"}),
        (["show-stack"], "show_stack", ["pr1", "pr2"], {"branches": ["pr1", "pr2"]}),
        (["next"], "next", "pr2", {"branch": "pr2"}),
        (["prev"], "prev", "main", {"branch": "main"}),
        (
            ["import"],
            "import_stacks",
            ImportResult(
                [ImportStep("pr1", "main", 12, ImportAction.TRACK, base="a" * 40)],
                STACK,
                [ImportStepWithoutPr("base", "main", ImportAction.CREATE, base="b" * 40)],
            ),
            {
                "stacks": to_json(STACK),
                "without_prs": [
                    {
                        "branch": "base",
                        "parent": "main",
                        "action": "create",
                        "base": "b" * 40,
                        "reason": None,
                        "other_authors": [],
                    }
                ],
                "steps": [
                    {
                        "branch": "pr1",
                        "parent": "main",
                        "number": 12,
                        "action": "track",
                        "base": "a" * 40,
                        "tracked_parent": None,
                        "reason": None,
                        "other_authors": [],
                    }
                ],
            },
        ),
    ],
)
def test_json_prints_the_result_on_stdout(
    stack: Any, capsys: pytest.CaptureFixture[str], argv: list[str], method: str, returned: Any, result: Any
) -> None:
    getattr(stack, method).return_value = returned

    assert main([*argv, "--json"], stack=stack) == 0

    assert json.loads(capsys.readouterr().out) == {"type": "result", **result, "events": []}


def test_push_says_when_everything_looks_merged(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.push.return_value = PushResult([], [])
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


def test_info_draws_a_header_above_the_stacks_in_a_group(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo("b", "main", BranchStatus.UP_TO_DATE, current=False, group="a"),
        BranchInfo("b2", "b", BranchStatus.UP_TO_DATE, current=False),
        BranchInfo("c", "main", BranchStatus.UP_TO_DATE, current=True, group="a"),
        BranchInfo("solo", "main", BranchStatus.UP_TO_DATE, current=False),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == ("main\n├── group a\n│   ├── b\n│   │   └── b2\n│   └── c *\n└── solo\n")


def test_info_draws_a_group_header_on_each_root_it_has_stacks_on(
    stack: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo("b", "main", BranchStatus.UP_TO_DATE, current=False, group="a"),
        BranchInfo("release", None, None, current=False),
        BranchInfo("c", "release", BranchStatus.UP_TO_DATE, current=False, group="a"),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == ("main\n└── group a\n    └── b\nrelease\n└── group a\n    └── c\n")


def test_info_json_lists_every_branch(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo(
            "pr1",
            "main",
            BranchStatus.NEEDS_RESTACK,
            current=True,
            pull_request=PullRequestLink(12, "https://github.com/acme/widgets/pull/12", PullRequestState.OPEN),
            group="pr0",
        ),
    ]

    main(["info", "--json"], stack=stack)

    assert json.loads(capsys.readouterr().out) == [
        {
            "name": "main",
            "parent": None,
            "status": None,
            "current": False,
            "group": None,
            "pr": None,
        },
        {
            "name": "pr1",
            "parent": "main",
            "status": "needs-restack",
            "current": True,
            "group": "pr0",
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


def test_show_stack_takes_quiet_or_json_but_not_both(stack: Any) -> None:
    with pytest.raises(SystemExit):
        main(["show-stack", "--quiet", "--json"], stack=stack)
