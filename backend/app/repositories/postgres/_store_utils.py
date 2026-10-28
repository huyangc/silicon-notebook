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
    # A6: 深拷贝携带的挂载边，created_at 由拷贝服务写成新的 ISO 串。
    "notebook_bases": ("created_at",),
    "notebooks": ("created_at", "updated_at"),
    "relation_embeddings": ("created_at",),
    "source_authors": ("created_at",),
    "source_elements": ("created_at",),
    "source_paper_cards": ("created_at", "updated_at"),
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


# 取「底」不取「值」(T4 规格评 P2-1):运维把部署超时调得比 600s 更高时
# (POSTGRES_STATEMENT_TIMEOUT_SECONDS,pool 在连接建立时按它设 session
# 值),`raise_statement_timeout_floor` 绝不能反向压低;0 = 已禁用超时,同样保持。
MAINTENANCE_STATEMENT_TIMEOUT_FLOOR_MS = 600_000


def raise_statement_timeout_floor(db: Any) -> None:
    """事务内把 statement_timeout 抬到维护底线(只抬不压,0/禁用保持)。

    ``pg_settings.setting`` 对 statement_timeout 以毫秒计;第三参 ``true`` =
    事务本地(SET LOCAL 等价),随提交/回滚自动复原,不泄漏给池化连接的
    下一个借用者——notebook_store.py:437/migrator.py:191 同款先例。必须在
    受保护语句**所在的同一个事务**里、先于它调用:``database.write()`` /
    ``database.connect()`` 借出的连接都是非 autocommit 的(``_restore_client_defaults``
    把它置 False),第一条语句就隐式开启事务,所以 ``set_config(..., true)`` 对随后
    同一个借用块内的语句(含 ``cursor().stream()``)都生效;autocommit 连接上它只管
    这一条语句,等于没放宽。

    最初为整表 DELETE 的离线维护站点而设(T-W4-4,``maintenance.py``);同一个底线现在
    也被 KG 重建的两条流式整表读复用(``UnifiedKgStore.community_graph_rows`` /
    ``source_canonical_rows``,理由见它们的 docstring),所以放在这个共享模块里,
    不各复制一份。"""
    db.execute(
        "SELECT set_config('statement_timeout',"
        " CASE WHEN cur = 0 THEN '0' ELSE GREATEST(cur, %s)::text END, true)"
        " FROM (SELECT setting::bigint AS cur FROM pg_settings"
        "       WHERE name='statement_timeout') AS s",
        (MAINTENANCE_STATEMENT_TIMEOUT_FLOOR_MS,),
    )


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
