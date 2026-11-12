"""How the CLI prints events."""

import dataclasses
import typing

import pytest

from git_stack import events
from git_stack.output import event_json, message, print_event


def test_warning_goes_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    print_event(events.TrunkDiverged("main", "origin/main"))

    assert capsys.readouterr() == ("", "warning: main has diverged from origin/main; not updating it\n")


def test_progress_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    print_event(events.Restacking("pr2", "pr1"))

    assert capsys.readouterr() == ("restacking pr2 onto pr1\n", "")


def test_stack_mismatch_suggests_rebuild(capsys: pytest.CaptureFixture[str]) -> None:
    print_event(events.GitHubStackMismatch((1, 2), (42,)))

    assert "git stack prs --rebuild-stack" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (events.Untracked("pr1", "main", (), deleted_tip=None), "pr1 was merged into main; stopped tracking it"),
        (
            events.Untracked("pr1", "main", ("pr2", "pr2b"), deleted_tip=None),
            "pr1 was merged into main; stopped tracking it and moved pr2, pr2b onto main",
        ),
        (
            events.Untracked("pr1", "main", ("pr2",), deleted_tip="0123456789abcdef"),
            "pr1 was merged into main; deleted it (was 0123456789ab) and moved pr2 onto main",
        ),
    ],
)
def test_untracked_says_what_happened_to_branch_and_children(event: events.Event, expected: str) -> None:
    assert message(event) == expected


def test_every_event_has_its_own_type() -> None:
    types = [event.event_type for event in typing.get_args(events.Event)]

    assert len(set(types)) == len(types)
    # The types of the lines --json ends with.
    assert not {"result", "error"} & set(types)


def test_no_event_field_hides_a_key_event_json_adds() -> None:
    for event in typing.get_args(events.Event):
        assert not {f.name for f in dataclasses.fields(event)} & {"type", "message", "warning"}


def test_event_json_has_type_message_warning_and_fields() -> None:
    event = events.Untracked("pr1", "main", ("pr2",), deleted_tip="0123456789abcdef")

    assert event_json(event) == {
        "type": "untracked",
        "message": "pr1 was merged into main; deleted it (was 0123456789ab) and moved pr2 onto main",
        "warning": False,
        "branch": "pr1",
        "parent": "main",
        "children": ["pr2"],
        "deleted_tip": "0123456789abcdef",
    }
    assert event_json(events.TrunkDiverged("main", "origin/main"))["warning"] is True


@pytest.mark.parametrize(
    ("children", "expected"),
    [
        (
            ("pr2", "pr2b"),
            (
                "skipping pr1: it was merged into main, then main reverted some of its changes. To bring them back "
                "with pr2, pr2b on them, run `git stack unrevert pr1-unrevert 0123456789ab fedcba987654 --from main "
                "--stack pr2 --stack pr2b`, or if that wasn't a revert, `git stack clean --ignore-revert pr1`"
            ),
        ),
        (
            (),
            (
                "skipping pr1: it was merged into main, then main reverted some of its changes. To bring them back, "
                "run `git stack unrevert pr1-unrevert 0123456789ab fedcba987654 --from main`, or if that wasn't a "
                "revert, `git stack clean --ignore-revert pr1`"
            ),
        ),
    ],
)
def test_reverted_gives_the_unrevert_command(children: tuple[str, ...], expected: str) -> None:
    event = events.Reverted("pr1", "main", ("0123456789abcdef", "fedcba9876543210"), children)

    assert message(event) == expected
