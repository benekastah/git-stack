"""The CLI: how each command maps its arguments onto StackClient, and prints
what it returns. StackClient is a mock; tests/test_end_to_end.py runs the
real thing."""

from typing import Any
from unittest.mock import create_autospec

import pytest

from git_stack.cli import main
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.stack import BranchInfo, BranchStatus, Renamed, StackClient, Tracked


@pytest.fixture
def stack() -> Any:
    """A mock StackClient whose methods check their arguments against the real ones."""
    stack = create_autospec(StackClient, instance=True)
    stack.git = create_autospec(GitClient, instance=True)
    stack.branch.return_value = "main"
    stack.track.return_value = []
    stack.rename.return_value = Renamed("old", "new", children=(), upstream=None)
    stack.info.return_value = []
    stack.sync.return_value = []
    stack.push.return_value = ["pr1"]
    return stack


@pytest.mark.parametrize(("argv", "method", "args", "kwargs"), [
    (["branch", "pr1"], "branch", ("pr1",), {}),
    (["track", "main"], "track", ("main", []), {"base": None}),
    (["track", "main", "pr1", "pr2", "--base", "abc123"], "track", ("main", ["pr1", "pr2"]),
     {"base": "abc123"}),
    (["rename", "new"], "rename", ("new",), {"old": None}),
    (["rename", "old", "new"], "rename", ("new",), {"old": "old"}),
    (["info"], "info", (), {}),
    (["restack"], "restack", (), {"onto": None, "squash": False}),
    (["restack", "--onto", "main", "--squash"], "restack", (), {"onto": "main", "squash": True}),
    (["sync"], "sync", (), {"remote": "origin", "delete": None}),
    (["sync", "--remote", "upstream", "-d", "local"], "sync", (), {"remote": "upstream", "delete": "local"}),
    (["clean"], "clean", (), {"remote": "origin", "delete": None}),
    (["clean", "--delete", "both"], "clean", (), {"remote": "origin", "delete": "both"}),
    (["push"], "push", (None,), {"remote": "origin", "whole_stack": False}),
    (["push", "pr1", "--remote", "upstream"], "push", ("pr1",), {"remote": "upstream", "whole_stack": False}),
    (["push", "--all"], "push", (None,), {"remote": "origin", "whole_stack": True}),
])
def test_command_calls_its_client_method(
        stack: Any, argv: list[str], method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    assert main(argv, stack=stack) == 0

    getattr(stack, method).assert_called_once_with(*args, **kwargs)


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
        f"tracking pr1 on top of main (base {'a' * 12})\n"
        f"tracking pr2 on top of pr1 (base {'b' * 12})\n")


def test_rename_prints_moved_children_and_old_upstream(
        stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.rename.return_value = Renamed("pr1", "first", children=("pr2", "pr2b"), upstream="origin/pr1")

    main(["rename", "pr1", "first"], stack=stack)

    out = capsys.readouterr().out
    assert out.startswith("renamed pr1 to first and moved pr2, pr2b onto it\n")
    assert "note: origin/pr1 wasn't renamed" in out


def test_info_draws_each_stack_as_a_tree(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.info.return_value = [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=False),
        BranchInfo("pr2", "pr1", BranchStatus.NEEDS_RESTACK, current=False),
        BranchInfo("pr2b", "pr1", BranchStatus.MERGED, current=False),
        BranchInfo("solo", "main", BranchStatus.MERGED_WITH_NEW_COMMITS, current=True),
        BranchInfo("release", None, None, current=False),
        BranchInfo("hotfix", "release", BranchStatus.PARENT_MISSING, current=False),
    ]

    main(["info"], stack=stack)

    assert capsys.readouterr().out == (
        "main\n"
        "├── pr1\n"
        "│   ├── pr2 (needs restack)\n"
        "│   └── pr2b (merged)\n"
        "└── solo * (merged, with new commits)\n"
        "release\n"
        "└── hotfix (parent release missing)\n")


def test_info_without_stacks(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["info"], stack=stack)

    assert capsys.readouterr().out == "no tracked branches\n"


def test_sync_lists_branches_left_to_push(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.sync.return_value = ["pr2", "pr3"]

    main(["sync"], stack=stack)

    assert capsys.readouterr().out == (
        "note: run `git stack push` to update the remote branches and pull requests of:\n"
        "  pr2\n"
        "  pr3\n")


def test_sync_with_nothing_to_push_prints_nothing(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    main(["sync"], stack=stack)

    assert capsys.readouterr().out == ""


def test_push_refuses_branch_with_all(stack: Any) -> None:
    with pytest.raises(SystemExit):
        main(["push", "pr1", "--all"], stack=stack)

    stack.push.assert_not_called()


def test_push_says_when_everything_looks_merged(stack: Any, capsys: pytest.CaptureFixture[str]) -> None:
    stack.push.return_value = []
    stack.git.current_branch.return_value = "pr1"

    main(["push"], stack=stack)

    assert capsys.readouterr().out == "nothing to push: every branch in pr1's stack looks merged\n"
