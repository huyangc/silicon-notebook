"""Backend-neutral projection of ``chunks.element_ids`` into reverse rows.

Both adapters store the forward direction the same way (a JSON array of
element ids on the chunk row), so the offline backfill's row-shaping logic is
identical on either side.  It lives here — next to ``like_pattern`` — rather
than in one adapter, because adapters must never import each other.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence


def decode_element_ids(value: Any) -> list[str]:
    """Element ids from a stored ``chunks.element_ids`` value.

    SQLite stores JSON text; PostgreSQL stores ``jsonb`` and hands back a list.
    Malformed or non-list payloads yield no rows rather than raising: the
    backfill must not abort a whole notebook over one corrupt legacy row (that
    row simply keeps the legacy read path's behaviour, which also skips it).
    """
    if isinstance(value, (list, tuple)):
        parsed: Any = value
    elif isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value or "[]")
        except (ValueError, TypeError):
            return []
    elif value is None:
        return []
    else:
        return []
    if not isinstance(parsed, (list, tuple)):
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item]


def reverse_rows(
    notebook_id: str, chunk_rows: Iterable[Mapping[str, Any]]
) -> list[tuple[str, str, str]]:
    """``(notebook_id, element_id, chunk_id)`` rows for a page of chunk rows.

    De-duplicated across the whole page: one chunk may list the same element id
    twice, and the composite primary key would otherwise reject the repeat
    mid-batch and abort the page's transaction.
    """
    seen: set[tuple[str, str, str]] = set()
    rows: list[tuple[str, str, str]] = []
    for chunk_row in chunk_rows:
        chunk_id = chunk_row["id"]
        for element_id in decode_element_ids(chunk_row["element_ids"]):
            row = (notebook_id, element_id, str(chunk_id))
            if row in seen:
                continue
            seen.add(row)
            rows.append(row)
    return rows


def reverse_rows_for_writes(
    notebook_id: str, chunk_id_and_element_ids: Sequence[tuple[str, Sequence[str]]]
) -> list[tuple[str, str, str]]:
    """Same shaping for the write path, where element ids are already a list."""
    return reverse_rows(
        notebook_id,
        [
            {"id": chunk_id, "element_ids": list(element_ids)}
            for chunk_id, element_ids in chunk_id_and_element_ids
        ],
    )
