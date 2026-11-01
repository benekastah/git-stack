"""How events and results become the JSON --json prints.

A dataclass becomes an object of its fields, under their own names unless
json_name gives another. Scripts rely on these names, so they don't change once
released.
"""

import dataclasses
import enum
from typing import Any

JSON_NAME = "json_name"


def json_name(name: str) -> dict[str, str]:
    """Metadata for a dataclass field that --json calls name, e.g.
    field(metadata=json_name("pr"))."""
    return {JSON_NAME: name}


def to_json(value: Any) -> Any:
    """value as JSON: a dataclass as an object of its fields, an enum as its
    value, and a list or tuple as a list of its items."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.metadata.get(JSON_NAME, field.name): to_json(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list | tuple):
        return [to_json(item) for item in value]
    return value
