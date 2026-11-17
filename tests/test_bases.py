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


def test_bases_refuses_until_all_boundaries_are_recorded(client: StackClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stacks = Stacks([StackNode("feature", "first", additional_parents=("second",))])
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    with pytest.raises(StackError, match="multiple bases.*not supported"):
        client.bases("feature")
