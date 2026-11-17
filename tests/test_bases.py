"""The plural and singular base commands."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from git_stack.stacks import StackNode, Stacks
from tests.helpers import Repo


def test_bases_prints_the_single_recorded_base(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    base = repo.git("rev-parse", "refs/heads/main")
    assert repo.stack("bases").stdout == f"{base}\n"
    assert repo.stack("base").stdout == f"{base}\n"


def test_singular_base_refuses_multiple_parents(client: StackClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stacks = Stacks([StackNode("feature", "first", additional_parents=("second",))])
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    with pytest.raises(StackError, match="multiple parents.*git stack bases"):
        client.base("feature")


def test_bases_prints_recorded_source_tips_in_order(repo: Repo, client: StackClient) -> None:
    client.branch("first")
    repo.commit("a", "first\n", "first")
    client.branch("feature")
    repo.git("switch", "--quiet", "main")
    client.branch("second")
    repo.commit("b", "second\n", "second")
    repo.git("switch", "--quiet", "feature")
    repo.stack("track", "--on", "first", "--on", "second")
    result = repo.stack("bases", expect_success=False)
    assert "git stack restack" in result.stderr

    client.restack()
    tips = [repo.git("rev-parse", branch) for branch in ("first", "second")]
    repo.git("switch", "--quiet", "second")
    repo.commit("b", "new second\n", "new second")

    assert repo.stack("bases", "feature").stdout.splitlines() == tips
