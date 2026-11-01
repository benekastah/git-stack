"""How events and results become JSON."""

import enum
from dataclasses import dataclass, field

from git_stack.json_fields import json_name, to_json


class Color(enum.Enum):
    RED = "red"


@dataclass(frozen=True)
class Inner:
    color: Color
    names: tuple[str, ...]


@dataclass(frozen=True)
class Outer:
    inner: Inner
    renamed: list[Inner] = field(metadata=json_name("others"))
    missing: str | None = None


def test_to_json_converts_dataclasses_enums_and_sequences_all_the_way_down() -> None:
    value = Outer(Inner(Color.RED, ("a",)), [Inner(Color.RED, ())])

    assert to_json(value) == {
        "inner": {"color": "red", "names": ["a"]},
        "others": [{"color": "red", "names": []}],
        "missing": None,
    }
