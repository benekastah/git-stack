"""The plural and singular parent commands."""

import pytest

from git_stack.errors import StackError
from git_stack.stack import StackClient
from git_stack.stacks import StackNode, Stacks
from tests.helpers import Repo


def test_parents_prints_the_single_base(repo: Repo, client: StackClient) -> None:
    client.branch("feature")
    assert repo.stack("parents").stdout == "main\n"
    assert repo.stack("parent").stdout == "main\n"


def test_parents_lists_all_bases_and_parent_refuses_ambiguity(
    client: StackClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    stacks = Stacks([StackNode("feature", "first", additional_parents=("second", "third"))])
    monkeypatch.setattr(client, "settled_stacks", lambda: stacks)

    assert client.parents("feature") == ["first", "second", "third"]
    with pytest.raises(StackError, match="multiple parents.*git stack parents"):
        client.parent("feature")
