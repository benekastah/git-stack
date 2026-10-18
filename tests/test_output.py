"""How the CLI prints events."""

import pytest

from git_stack import events
from git_stack.output import message, print_event


def test_warning_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    print_event(events.TrunkDiverged("main", "origin/main"))

    assert capsys.readouterr() == ("", "warning: main has diverged from origin/main; not updating it\n")


def test_progress_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    print_event(events.Restacking("pr2", "pr1"))

    assert capsys.readouterr() == ("restacking pr2 onto pr1\n", "")


@pytest.mark.parametrize(("event", "expected"), [
    (events.Untracked("pr1", "main", (), deleted_tip=None),
     "pr1 was merged into main; stopped tracking it"),
    (events.Untracked("pr1", "main", ("pr2", "pr2b"), deleted_tip=None),
     "pr1 was merged into main; stopped tracking it and moved pr2, pr2b onto main"),
    (events.Untracked("pr1", "main", ("pr2",), deleted_tip="0123456789abcdef"),
     "pr1 was merged into main; deleted it (was 0123456789ab) and moved pr2 onto main"),
])
def test_untracked_says_what_happened_to_branch_and_children(event: events.Event, expected: str) -> None:
    assert message(event) == expected
