"""Frozen dataclasses to and from JSON-compatible values, driven by their type hints."""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


def to_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [to_json(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    return value


def from_json[T](cls: type[T], data: Mapping[str, Any]) -> T:
    hints = get_type_hints(cls)
    return cls(**{name: _decode(hints[name], value) for name, value in data.items() if name in hints})


def _decode(hint: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        options = [a for a in get_args(hint) if a is not type(None)]
        return _decode(options[0], value) if len(options) == 1 else value
    if origin is tuple:
        return tuple(_decode(get_args(hint)[0], v) for v in value)
    if isinstance(hint, type) and is_dataclass(hint):
        return from_json(hint, value)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    return value
