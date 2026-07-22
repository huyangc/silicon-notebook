"""Small PostgreSQL-only row/value helpers shared by persistence stores."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from psycopg.types.json import Jsonb


_NOTEBOOK_JSON_COLUMNS = ("expected_questions", "source_types", "taxonomy")
_NOTEBOOK_TIMESTAMP_COLUMNS = ("created_at", "updated_at")


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
