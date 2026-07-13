"""Neutral fail-closed helpers for standard JSON owned by the application."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


class JsonSafetyError(ValueError):
    """A value cannot be represented as finite, standards-compliant JSON."""


def validate_finite_json(
    value: Any, *, field: str, _active: set[int] | None = None
) -> None:
    """Recursively reject non-finite floats and cyclic JSON containers."""
    if isinstance(value, float) and not math.isfinite(value):
        raise JsonSafetyError(f"{field} must not contain non-finite numbers")
    is_mapping = isinstance(value, Mapping)
    is_sequence = isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )
    if not is_mapping and not is_sequence:
        return
    active = _active if _active is not None else set()
    identity = id(value)
    if identity in active:
        raise JsonSafetyError(f"{field} must be JSON serializable")
    active.add(identity)
    try:
        children = value.values() if is_mapping else value
        for child in children:
            validate_finite_json(child, field=field, _active=active)
    finally:
        active.remove(identity)


def strict_json_dumps(
    value: Any,
    *,
    field: str,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
) -> str:
    """Serialize without Python's non-standard NaN/Infinity extensions."""
    validate_finite_json(value, field=field)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=sort_keys,
            separators=separators,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise JsonSafetyError(f"{field} must be JSON serializable") from exc


def strict_json_size_bytes(value: Any, *, field: str) -> int:
    """Return the canonical compact UTF-8 size used by input limits."""
    encoded = strict_json_dumps(
        value, field=field, sort_keys=True, separators=(",", ":")
    )
    return len(encoded.encode("utf-8"))
