"""Strict SQLite-to-PostgreSQL value conversion for shadow COPY."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from app.migration.shadow.types import TableSpec


_EMPTY_TIME_SENTINELS = frozenset(
    {
        "ask_jobs.created_at",
        "ask_jobs.updated_at",
        "ask_trace_steps.created_at",
        "kg_build_jobs.finished_at",
        "knowledge_objects.last_reviewed",
        "merge_review_jobs.started_at",
        "merge_review_jobs.updated_at",
        "unified_kg_state.last_rebuild_at",
    }
)
_EMPTY_JSON_LIST_SENTINELS = frozenset(
    {
        "ask_jobs.trace_json",
        "notebooks.expected_questions",
        "notebooks.source_types",
        "notebooks.taxonomy",
    }
)


@dataclass(frozen=True)
class PostgresColumn:
    name: str
    data_type: str
    nullable: bool


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("shadow JSON contains a non-finite number")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("shadow JSON contains a non-finite number")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("shadow JSON object keys must be strings")
            _reject_non_finite(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_non_finite(child)


def canonical_json_value(value: Any) -> Any:
    """Parse SQLite JSON without losing JSON null or accepting NaN/Infinity."""
    if not isinstance(value, str):
        raise ValueError("SQLite JSON value must be canonical text")
    try:
        parsed = json.loads(
            value,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        json.JSONDecodeError,
        DecimalException,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise ValueError("SQLite JSON value is invalid") from exc
    _reject_non_finite(parsed)
    return parsed


def exact_json_dumps(value: Any) -> str:
    """Serialize Decimal-backed JSON without process-global Psycopg hooks."""

    def encode(child: Any) -> str:
        if child is None:
            return "null"
        if child is True:
            return "true"
        if child is False:
            return "false"
        if isinstance(child, str):
            return json.dumps(child, ensure_ascii=False, allow_nan=False)
        if isinstance(child, Decimal):
            if not child.is_finite():
                raise ValueError("shadow JSON contains a non-finite number")
            return str(child)
        if isinstance(child, int) and not isinstance(child, bool):
            return str(child)
        if isinstance(child, Mapping):
            if any(not isinstance(key, str) for key in child):
                raise ValueError("shadow JSON object keys must be strings")
            return (
                "{"
                + ",".join(
                    json.dumps(key, ensure_ascii=False, allow_nan=False)
                    + ":"
                    + encode(child[key])
                    for key in sorted(child)
                )
                + "}"
            )
        if isinstance(child, list):
            return "[" + ",".join(encode(item) for item in child) + "]"
        raise ValueError("shadow JSON contains an unsupported value")

    return encode(value)


def _timestamp(table: str, column: str, value: Any) -> datetime | None:
    qualified = f"{table}.{column}"
    if value == "" and qualified in _EMPTY_TIME_SENTINELS:
        return None
    if not isinstance(value, (str, datetime)):
        raise ValueError("SQLite timestamp must be ISO text")
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("SQLite timestamp is invalid") from exc
    # SQLite CURRENT_TIMESTAMP is UTC and the historical database stores naive
    # values in that convention. Explicit offsets remain exact instants.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def transform_sqlite_value(
    spec: TableSpec,
    column: PostgresColumn,
    value: Any,
) -> Any:
    if value is None:
        if not column.nullable:
            raise ValueError(f"SQLite NULL cannot populate {spec.name}.{column.name}")
        return None
    qualified = f"{spec.name}.{column.name}"
    data_type = column.data_type
    if data_type == "jsonb":
        parsed = (
            []
            if value == "" and qualified in _EMPTY_JSON_LIST_SENTINELS
            else canonical_json_value(value)
        )
        return Jsonb(parsed, dumps=exact_json_dumps)
    if data_type == "boolean":
        if isinstance(value, bool):
            return value
        if type(value) is not int or value not in (0, 1):
            raise ValueError(f"SQLite boolean is invalid: {qualified}")
        return bool(value)
    if data_type == "timestamp with time zone":
        return _timestamp(spec.name, column.name, value)
    if data_type == "bytea":
        if isinstance(value, str):
            try:
                decoded = json.loads(
                    value,
                    parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                )
                if not isinstance(decoded, list) or any(
                    isinstance(item, bool)
                    or not isinstance(item, (int, float))
                    or not math.isfinite(float(item))
                    for item in decoded
                ):
                    raise ValueError
                # Legacy SQLite fixtures may still contain JSON vectors. Only
                # that legacy text path parses numbers; an existing BLOB below
                # remains opaque bytes and is never expanded to a float array.
                return struct.pack(f"<{len(decoded)}f", *decoded)
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
                OverflowError,
                struct.error,
            ) as exc:
                raise ValueError(
                    f"SQLite legacy vector is invalid: {qualified}"
                ) from exc
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError(f"SQLite BLOB is invalid: {qualified}")
        # Keep the opaque encoded vector as bytes; never expand it to floats.
        return value if isinstance(value, bytes) else bytes(value)
    if column.name in spec.path_columns:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"SQLite filesystem path is invalid: {qualified}")
        return value
    if data_type in {"bigint", "integer", "smallint"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"SQLite integer is invalid: {qualified}")
    elif data_type in {"double precision", "real", "numeric"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"SQLite number is invalid: {qualified}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"SQLite number is non-finite: {qualified}")
        if data_type in {"double precision", "real"}:
            return float(value)
        try:
            normalized = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"SQLite numeric is invalid: {qualified}") from exc
        if not normalized.is_finite():
            raise ValueError(f"SQLite numeric is non-finite: {qualified}")
        return normalized
    elif data_type in {"text", "character", "character varying"}:
        if not isinstance(value, str):
            raise ValueError(f"SQLite text is invalid: {qualified}")
    return value


def transform_sqlite_row(
    spec: TableSpec,
    columns: Sequence[PostgresColumn],
    row: Mapping[str, Any],
) -> tuple[Any, ...]:
    return tuple(
        transform_sqlite_value(spec, column, row[column.name]) for column in columns
    )
