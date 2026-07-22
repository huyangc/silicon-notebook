"""Small PostgreSQL-only row/value helpers shared by persistence stores."""
from __future__ import annotations

import json
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable, Sequence

from psycopg.types.json import Jsonb


_NOTEBOOK_JSON_COLUMNS = ("expected_questions", "source_types", "taxonomy")
_NOTEBOOK_TIMESTAMP_COLUMNS = ("created_at", "updated_at")
_TIMESTAMP_COLUMNS_BY_TABLE = {
    "chunk_embeddings": ("created_at",),
    "chunks": ("created_at",),
    "concept_clusters": ("created_at",),
    "element_embeddings": ("created_at",),
    "knowhow_cell_code": ("created_at", "updated_at"),
    "knowhow_cells": ("updated_at",),
    "knowhow_rows": ("created_at", "updated_at"),
    "knowhow_tables": ("created_at", "updated_at"),
    "knowledge_embeddings": ("created_at",),
    "knowledge_objects": ("created_at", "updated_at", "last_reviewed"),
    "knowledge_relations": ("created_at",),
    "notebook_assets": ("created_at",),
    "notebooks": ("created_at", "updated_at"),
    "relation_embeddings": ("created_at",),
    "source_authors": ("created_at",),
    "source_elements": ("created_at",),
    "source_paper_meta": ("created_at", "updated_at"),
    "sources": ("created_at", "updated_at"),
}

TimestampInput = str | datetime


def json_value(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def iso_timestamp(value: Any, *, empty: str = "") -> str:
    if value is None:
        return empty
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def normalize_timestamp(
    value: TimestampInput,
    *,
    local_timezone: tzinfo | None = None,
) -> datetime:
    """Return an aware UTC instant for every PostgreSQL timestamptz write.

    Repository compatibility seams historically emit naive local ISO strings.
    A naive value therefore means system-local wall time, not UTC.  Supplying
    ``local_timezone`` keeps this rule deterministic for direct callers/tests;
    the default delegates local-zone and DST resolution to the host runtime.
    """
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = (
            parsed.replace(tzinfo=local_timezone)
            if local_timezone is not None
            else parsed.astimezone()
        )
    return parsed.astimezone(timezone.utc)


def normalized_clock(
    clock: Callable[[], TimestampInput],
) -> Callable[[], datetime]:
    def now() -> datetime:
        return normalize_timestamp(clock())

    return now


def normalize_timestamp_row(table: str, row: dict) -> dict:
    """Normalize every copied timestamptz column before generic PG INSERT."""
    result = dict(row)
    for column in _TIMESTAMP_COLUMNS_BY_TABLE.get(table, ()):
        value = result.get(column)
        if value not in (None, ""):
            result[column] = normalize_timestamp(value)
    return result


def sqlite_compatible_row(
    row: dict | None,
    *,
    json_columns: Sequence[str] = (),
    timestamp_columns: Sequence[str] = (),
) -> dict | None:
    """Encode PG-native values at raw-row ports consumed like SQLite rows."""
    if row is None:
        return None
    result = dict(row)
    for column in json_columns:
        if column in result and not isinstance(result[column], str):
            result[column] = json.dumps(result[column], ensure_ascii=False)
    for column in timestamp_columns:
        if column in result:
            result[column] = iso_timestamp(result[column])
    return result


def sqlite_compatible_notebook_row(row: dict | None) -> dict | None:
    return sqlite_compatible_row(
        row,
        json_columns=_NOTEBOOK_JSON_COLUMNS,
        timestamp_columns=_NOTEBOOK_TIMESTAMP_COLUMNS,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def placeholders(values: Sequence[object]) -> str:
    return ",".join("%s" for _ in values)


def execute_many(connection: Any, statement: str, rows: Sequence[Sequence[Any]]) -> None:
    if not rows:
        return
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)
