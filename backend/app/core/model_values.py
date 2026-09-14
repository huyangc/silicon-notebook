"""Narrowing helpers for fields read out of a delivered model reply.

The JSON-contract boundary (``core.model_json``) delivers off-type fields
and reports them instead of rejecting the reply, so a consumer that wants
prose must ask for a string rather than call ``str()`` on whatever arrived:
``str([])`` is the non-empty text ``"[]"``, ``str(None)`` is ``"None"``, and
both slip past every emptiness check and get persisted as content.
"""
from __future__ import annotations

from typing import Any


def as_text(value: Any) -> str:
    """``value`` stripped when it is a string; "" for anything else."""
    return value.strip() if isinstance(value, str) else ""


def as_text_list(value: Any) -> list[str]:
    """The non-empty string items of a list; a bare string or any other
    container shape is empty rather than iterated character by character."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
