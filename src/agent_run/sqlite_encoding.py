"""Bounded decoding of immutable JSON text; database reads remain authoritative."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

_CONTAINER_TYPES = (dict, list)

def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@lru_cache(maxsize=4096, typed=True)
def _encoded_scalar(value: str | int | bool | None) -> str:
    return _encode(value)


@lru_cache(maxsize=4096)
def _encoded_path(value: tuple[str | int, ...]) -> str:
    return _encode(value)


def encode_json(value: Any) -> str:
    """Reuse bounded immutable encodings, preserving exact JSON scalar types."""
    if (value is None or type(value) is bool or type(value) is int and value.bit_length() <= 128
            or isinstance(value, str) and len(value) <= 256):
        return _encoded_scalar(value)
    if (isinstance(value, tuple) and len(value) <= 16
            and all(type(part) is str or type(part) is int and part.bit_length() <= 128 for part in value)
            and sum(len(part) if isinstance(part, str) else 24 for part in value) <= 256):
        return _encoded_path(value)
    return _encode(value)


def _decode_path(encoded: str) -> tuple[str | int, ...]:
    path = json.loads(encoded)
    if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
        raise ValueError("Invalid SQLite Run object path")
    return tuple(path)


@lru_cache(maxsize=4096)
def _cached_path(encoded: str) -> tuple[str | int, ...]:
    return _decode_path(encoded)


def object_path(encoded: str) -> tuple[str | int, ...]:
    # Cap both entry count and retained input length, including arbitrary keys.
    return _cached_path(encoded) if len(encoded) <= 256 else _decode_path(encoded)


def _decode_scalar(encoded: str) -> Any:
    value = json.loads(encoded)
    if isinstance(value, dict | list):
        raise ValueError("Invalid SQLite Run object value")
    return value


@lru_cache(maxsize=4096)
def _cached_scalar(encoded: str) -> Any:
    return _decode_scalar(encoded)


def scalar_value(encoded: str) -> Any:
    return _cached_scalar(encoded) if len(encoded) <= 256 else _decode_scalar(encoded)


def copy_json_tree(value: Any) -> Any:
    """Copy a decoded JSON tree's containers, sharing only immutable scalars.

    SQLite projections have already passed JSON decoding and tree assembly;
    they contain no arbitrary Python objects, mutable scalars or cycles.
    """
    if isinstance(value, dict):
        return {key: copy_json_tree(child) if isinstance(child, _CONTAINER_TYPES) else child
                for key, child in value.items()}
    if isinstance(value, list):
        return [copy_json_tree(child) if isinstance(child, _CONTAINER_TYPES) else child for child in value]
    return value


def snapshot_fields(
    state: dict[str, Any], encodings: dict[tuple[str | int, ...], str],
) -> dict[str, str]:
    """Assemble fields from proven snapshot encodings without encoding their trees again."""
    fields: dict[str, str] = {}
    for key, value in state.items():
        if (key,) in encodings:
            fields[key] = encodings[(key,)]
        elif isinstance(value, dict) and value and all((key, child) in encodings for child in value):
            fields[key] = "{" + ",".join(
                json.dumps(child, ensure_ascii=False) + ":" + encodings[(key, child)] for child in value
            ) + "}"
        elif isinstance(value, list) and value and all((key, child) in encodings for child in range(len(value))):
            fields[key] = "[" + ",".join(encodings[(key, child)] for child in range(len(value))) + "]"
    return fields


def copy_projection_fields(state: dict[str, Any], fields: dict[str, str]) -> dict[str, Any]:
    """Decode independent containers with the C codec, sharing only scalars."""
    return {key: json.loads(fields[key]) if key in fields else value for key, value in state.items()}
