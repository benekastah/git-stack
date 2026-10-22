"""What StackClient promises scripts beyond each operation's behavior."""

import pytest

from git_stack import events
from git_stack.stack import StackClient
from tests.helpers import Repo


def test_client_reports_through_events_without_printing(
    repo: Repo, client: StackClient, emitted: list[events.Event], capsys: pytest.CaptureFixture[str]
) -> None:
    repo.make_stack()
    client.push()
    repo.squash_merge_on_forge("pr1")

    client.sync()

    assert events.Untracked("pr1", "main", ("pr2",), deleted_tip=None) in emitted
    assert capsys.readouterr() == ("", "")
