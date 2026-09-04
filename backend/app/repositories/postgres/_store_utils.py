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


def local_datetime(value: Any) -> datetime:
    """Project an instant onto the system-local calendar and UTC offset."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.astimezone()


def local_iso_timestamp(value: Any, *, empty: str = "") -> str:
    if value is None:
        return empty
    return local_datetime(value).isoformat()


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
            result[column] = local_iso_timestamp(result[column])
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


# Page size for the offline build's whole-notebook keyset scans (batch-3 W4
# T-W4-3.1). Same order of magnitude as the embedding scans'
# ``MATRIX_FETCH_BATCH``: the bound that matters is "one page of rows in the
# driver's result buffer", not the exact number.
GRAPH_FETCH_BATCH = 10_000


def keyset_pages(db: Any, page_size: int, statement_for, cursor_of):
    """Yield successive keyset pages of one whole-notebook read.

    ``statement_for(cursor)`` returns ``(statement, params)`` for the page
    starting strictly after ``cursor`` (``None`` = first page); ``cursor_of``
    extracts the next cursor from a page's LAST row. THIS helper appends the
    ``LIMIT`` clause and its parameter, so ``page_size`` exists once: a call
    site that spelled its own ``LIMIT %s`` would carry a second copy of the
    page size that a monkeypatched/overridden ``page_size`` could silently
    disagree with. Each page is an independent, fully-exhausted statement, so
    what this bounds is (a) the rows psycopg materializes per ``fetchall`` and
    (b) the lifetime of any single statement's MVCC snapshot across a build
    that can run for hours — NOT whatever the caller then accumulates in
    Python. A short page ends the scan, exactly like
    ``EmbeddingStore.vector_pages``.

    Connection boundary: the pages are yielded against the SINGLE ``db`` the
    caller passed, so the caller's connection stays checked out for as long as
    it keeps iterating. That is deliberate and bounded for the graph-side
    reads, whose consumers do nothing between pages but fold rows into a dict
    (milliseconds per page) — unlike the ANN feed, which interleaves hnswlib
    index construction between pages and therefore acquires and releases a
    connection PER page instead (see ``embedding_pages``).

    Every call site must pass a key that is a TOTAL order over the rows its
    predicate admits, or a strict ``>`` cursor silently drops ties that
    straddle a page boundary; each site names its uniqueness argument inline.
    The cursor is asserted to advance STRICTLY on every page: a key that is
    not a total order (or a predicate whose ``>`` was written as ``>=``) would
    otherwise re-read the same rows forever, which is an unbounded-memory hang
    in the consumer rather than a loud failure — measured on the objects leg,
    a ``>=`` cursor grows to 1.5GB RSS and never terminates.
    """
    cursor = None
    while True:
        statement, params = statement_for(cursor)
        page = db.execute(
            statement + " LIMIT %s", (*params, page_size)
        ).fetchall()
        if not page:
            return
        yield page
        if len(page) < page_size:
            return
        advanced = cursor_of(page[-1])
        if cursor is not None and not advanced > cursor:
            raise RuntimeError(
                "keyset_pages cursor did not advance "
                f"({cursor!r} -> {advanced!r}); the page key is not a total "
                "order over the rows this statement admits"
            )
        cursor = advanced
