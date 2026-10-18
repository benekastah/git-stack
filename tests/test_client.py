"""StackClient called directly, as a script would."""

import pytest

from git_stack import events
from git_stack.stack import BranchInfo, BranchStatus, StackClient
from tests.helpers import Repo


def test_sync_reports_through_events_and_return_value(
        repo: Repo, client: StackClient, emitted: list[events.Event],
        capsys: pytest.CaptureFixture[str]) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")

    unpushed = client.sync()

    assert unpushed == ["pr2", "pr3"]
    assert events.Untracked("pr1", "main", ("pr2",), deleted_tip=None) in emitted
    assert capsys.readouterr().out == ""


def test_info_returns_each_branch_with_its_status(repo: Repo, client: StackClient) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 review fix\n", "pr1 review fix")

    assert client.info() == [
        BranchInfo("main", None, None, current=False),
        BranchInfo("pr1", "main", BranchStatus.UP_TO_DATE, current=True),
        BranchInfo("pr2", "pr1", BranchStatus.NEEDS_RESTACK, current=False),
        BranchInfo("pr3", "pr2", BranchStatus.UP_TO_DATE, current=False),
    ]
