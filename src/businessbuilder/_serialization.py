from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
import json
from typing import Any


def encode_record(value: Any) -> str:
    return json.dumps(_encode(value), sort_keys=True, separators=(",", ":"))


def decode_record(value: str, registry: dict[str, type]) -> Any:
    return _decode(json.loads(value), registry)


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return {"__enum__": value.__class__.__name__, "value": value.value}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if is_dataclass(value):
        return {
            "__dataclass__": value.__class__.__name__,
            "fields": {item.name: _encode(getattr(value, item.name)) for item in fields(value)},
        }
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(item) for item in value]}
    if isinstance(value, frozenset):
        return {"__frozenset__": [_encode(item) for item in sorted(value, key=str)]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any, registry: dict[str, type]) -> Any:
    if isinstance(value, list):
        return [_decode(item, registry) for item in value]
    if not isinstance(value, dict):
        return value
    if "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    if "__enum__" in value:
        return registry[value["__enum__"]](value["value"])
    if "__tuple__" in value:
        return tuple(_decode(item, registry) for item in value["__tuple__"])
    if "__frozenset__" in value:
        return frozenset(_decode(item, registry) for item in value["__frozenset__"])
    if "__dataclass__" in value:
        cls = registry[value["__dataclass__"]]
        return cls(**{key: _decode(item, registry) for key, item in value["fields"].items()})
    return {key: _decode(item, registry) for key, item in value.items()}
