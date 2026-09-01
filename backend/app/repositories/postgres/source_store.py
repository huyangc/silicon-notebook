from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from app.models.sources import (
    PaginatedSourceElements,
    PaginatedSources,
    PaperAuthor,
    PaperMeta,
    SourceDetail,
    SourceElement,
    SourceSummary,
    extraction_warning_text,
    kg_analyzed_without_objects,
    INDEXING_CHUNK_FALLBACK_WARNING_PREFIX,
    has_indexing_chunk_fallback_warning,
    has_pdf_python_fallback_warning,
    paper_meta_status,
)
from app.domain.source_display import summary_display_title
from app.repositories.ports import (
    SOURCE_PAPER_META_UNSET,
    DocumentCapacityExceeded,
    SourceElementWrite,
)
from app.repositories.postgres._store_utils import (
    TimestampInput,
    execute_many,
    iso_timestamp,
    json_value,
    jsonb,
    local_datetime,
    normalized_clock,
    normalize_timestamp,
    placeholders,
)
from app.repositories.postgres.database import PostgresDatabase


_UNSET = SOURCE_PAPER_META_UNSET


# 「用户可见文档」的判定谓词，与 SQLite 侧 ``source_store.
# VISIBLE_SOURCE_TYPES_PREDICATE`` 逐字同义:排除 Memory 派生源与 knowhow 表隐藏
# 投影源。此前这段谓词在本文件里被 list_sources / list_sources_page /
# visible_document_count 各写一遍;来源集合枚举(design doc §6.2)必须与来源页签
# 同口径，所以先把它收成一个常量，再由 ``source_change_signal_rows`` 把它作为
# **投影列**求值（`user_visible`）——否则「哪些来源用户看得见」在这一个文件里就有
# 四份拼写，任何一份漂移都会让答案里出现一张用户在来源页签上根本看不到的卡片。
# 求值放在投影里而不是另开一条 id 查询，是因为 `source_type` 上没有索引：那条查询
# 只能整表扫这个 notebook 的全部源行，而且就发生在刚扫过同一批行之后。SQL 文本
# 逐字不变。
VISIBLE_SOURCE_TYPES_PREDICATE = "source_type NOT IN ('memory','knowhow')"


# 「这一行是私有 Memory 的合成来源」的 SQL 谓词,与 SQLite 侧
# ``source_store.MEMORY_SOURCE_TYPE_PREDICATE`` 逐字同义;两侧的理由与用法边界写在
# 那一份注释里(简述:底座聚合把 Memory 排除压进语句内,跨查询的相减/排除清单在
# READ COMMITTED 下会被并发的 Memory 增删漏掉,而漏掉的东西里包含概念名称)。
MEMORY_SOURCE_TYPE_PREDICATE = "source_type = 'memory'"


# 论文元数据补抽候选谓词(接在 ``FROM sources s`` 且已按 ``s.notebook_id`` 过滤之后)。
# 与 SQLite 侧 ``sqlite.source_store.PAPER_META_ELIGIBLE_SQL`` 同义;三个消费方
# (sources_missing_paper_meta / notebook_analytics 的 missing 计数 /
# NotebookSummary.paper_meta_missing 的 EXISTS 探针)共用这一份保证口径不漂移。
PAPER_META_ELIGIBLE_SQL = (
    " AND s.source_type NOT IN ('memory','knowhow')"
    " AND s.doc_type IN ('','academic_paper')"
    " AND s.parse_status IN ('parsed','extracting','extracted')"
)
PAPER_META_NO_META_SQL = (
    " AND NOT EXISTS(SELECT 1 FROM source_paper_meta m WHERE m.source_id=s.id)"
)


def _sort_key(value: object) -> str:
    """A timestamp column as ORDER-BY-compatible text.

    Driver datetimes become ISO text; text columns pass through; NULL becomes
    ``""``.

    **Aware datetimes are converted to UTC first**, and that is the load-bearing
    line.  ``timestamptz`` values arrive with a UTC offset, and offsets are not
    constant within one column: across a DST transition two rows an hour apart
    can read ``…01:30:00+02:00`` and ``…01:30:00+01:00``.  Comparing those
    strings lexicographically ranks them by the wall-clock digits and then by
    the offset text, which is not chronological — ``+01:00`` sorts before
    ``+02:00`` even though it is the LATER instant.  PostgreSQL's ``ORDER BY``
    compares instants, so the roster would come out in an order the source tab
    never shows, silently, and only around a DST fold.  Normalizing to UTC makes
    every key share one offset, which is the condition under which lexicographic
    ISO comparison equals instant comparison.  (Naive datetimes carry no offset
    and are left alone: they are already single-convention, and inventing a
    timezone for them would be a guess.)

    The one asymmetry worth naming: SQL sorts NULLs last by default on
    ascending order, while ``""`` sorts first here.  ``sources.created_at`` is
    written on every insert path and has never been NULL, so this branch is
    defensive only; putting an impossible row first is preferable to raising
    mid-enumeration, and the roster's coverage would disclose nothing wrong
    either way.
    """
    if value is None:
        return ""
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return str(value)
    if getattr(value, "tzinfo", None) is not None:
        try:
            return value.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            # A datetime whose offset cannot be applied is not worth failing an
            # enumeration over; the unconverted form is still a stable key.
            return isoformat()
    return isoformat()


def _created_label(value: object) -> str:
    try:
        parsed = local_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.now().astimezone()
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _metadata_compat(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False)


class SourceStore:
    """PostgreSQL source rows, elements, paper metadata, and summary hydration."""

    IN_CHUNK = 5_000

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        now: Callable[[], TimestampInput],
        current_user_id: Callable[[], str] = lambda: "",
    ) -> None:
        self.database = database
        self.now = normalized_clock(now)
        self.current_user_id = current_user_id

    def all_visible_source_ids(self, notebook_id: str) -> list[str]:
        """Return the current visible-source universe for graph drift checks."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} ORDER BY id",
                (notebook_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def hidden_source_ids(self, notebook_id: str, owner_id: str) -> list[str]:
        """Hidden Memory/Knowhow projection participants **for one user**, in
        stable id order — see the SQLite adapter for why the Memory owner
        filter lives in the SQL rather than on the result, and why this stays
        a separate read (Knowhow projections are notebook-wide, a Memory
        projection belongs to its ``memory_items.created_by``)."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type IN ('memory','knowhow') "
                "AND (s.source_type <> 'memory' OR EXISTS ("
                "SELECT 1 FROM memory_items m "
                "WHERE m.id = s.memory_id AND m.created_by = %s)) "
                "ORDER BY s.id",
                (notebook_id, owner_id),
            ).fetchall()
        return [row["id"] for row in rows]

    def visible_source_scope_snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple[list[str], int]:
        """Validate compact selected ids and count the universe in one statement."""
        requested = list(dict.fromkeys(str(value) for value in source_ids if value))
        with self.database.connect() as connection:
            if not requested:
                total = int(connection.execute(
                    "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s "
                    f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
                    (notebook_id,),
                ).fetchone()["c"])
                return [], total
            requested_json = json.dumps(requested, ensure_ascii=False)
            rows = connection.execute(
                "WITH requested(id, ordinal) AS ("
                "SELECT value, ordinal FROM "
                "jsonb_array_elements_text(%s::jsonb) WITH ORDINALITY AS r(value, ordinal)"
                "), visible AS ("
                "SELECT requested.id, requested.ordinal FROM requested "
                "JOIN sources ON sources.id=requested.id "
                f"WHERE sources.notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                "), stats(visible_count) AS ("
                "SELECT COUNT(*) FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                ") SELECT visible.id, stats.visible_count FROM stats "
                "LEFT JOIN visible ON TRUE ORDER BY visible.ordinal",
                (requested_json, notebook_id, notebook_id),
            ).fetchall()
            visible = [str(row["id"]) for row in rows if row["id"] is not None]
            total = int(rows[0]["visible_count"])
        return visible, total

    def list_sources(self, notebook_id: str) -> list[SourceSummary]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE notebook_id=%s "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                "ORDER BY created_at,id COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
            return self.sources_from_rows(connection, rows)

    def list_sources_page(
        self,
        notebook_id: str,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
    ) -> PaginatedSources:
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        needle = (q or "").strip().lower()
        where = f"WHERE notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
        params: list[object] = [notebook_id]
        if needle:
            where += (
                " AND (LOWER(title) LIKE %s OR LOWER(file_name) LIKE %s "
                "OR EXISTS(SELECT 1 FROM source_authors a "
                "WHERE a.source_id=sources.id AND LOWER(a.name) LIKE %s) "
                "OR EXISTS(SELECT 1 FROM source_paper_meta m "
                "WHERE m.source_id=sources.id AND LOWER(m.paper_title) LIKE %s))"
            )
            like = f"%{needle}%"
            params.extend((like, like, like, like))
        with self.database.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS c FROM sources {where}", params
            ).fetchone()["c"]
            rows = connection.execute(
                f"SELECT * FROM sources {where} "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s OFFSET %s",
                (*params, limit, offset),
            ).fetchall()
            items = self.sources_from_rows(connection, rows)
        return PaginatedSources(
            items=items, total_count=total, offset=offset, limit=limit
        )

    def visible_document_count(self, notebook_id: str) -> int:
        with self.database.connect() as connection:
            return self._visible_document_count_on(connection, notebook_id)

    @staticmethod
    def _visible_document_count_on(connection, notebook_id: str) -> int:
        """The visible-document COUNT on a caller-provided connection — shared
        by the read path and the atomic capacity gate inside the creation write
        transactions (mirrors the SQLite twin)."""
        row = connection.execute(
            "SELECT COUNT(*) AS c FROM sources WHERE notebook_id=%s "
            f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
            (notebook_id,),
        ).fetchone()
        return int(row["c"])

    @staticmethod
    def _lock_notebook_row_for_capacity(connection, notebook_id: str) -> None:
        """Serialize capacity-checked source creators on the notebook row.

        PostgreSQL's ``write()`` deliberately has no process-wide lock and runs
        READ COMMITTED, so two concurrent transactions could each COUNT, each
        see one free slot, and each INSERT — the transaction boundary alone
        closes nothing. ``FOR NO KEY UPDATE`` makes the second capacity-checked
        creator wait until the first commits; its COUNT (a fresh READ COMMITTED
        snapshot per statement) then includes the winner's row.

        Lock-mode conflict edges, stated precisely (do not paraphrase):

        * NOT blocked: the ``FOR KEY SHARE`` a plain FK insert takes — exempt
          writers (offline ``batch_ingest``, Memory/knowhow projection row
          inserts) never queue behind a capacity-checked upload's INSERT.
        * Blocked both ways, and NEW conflict edges this lock introduces:
          ordinary ``UPDATE notebooks SET …`` (rename, tier/status flips,
          ``updated_at`` bumps) takes FOR NO KEY UPDATE itself, and
          ``memory_store``'s ``FOR SHARE`` ownership probes conflict with it
          too. Both sides hold their locks for millisecond transactions, so
          this is brief queueing, not a correctness issue.
        * No deadlock by construction: this lock is the FIRST statement of its
          transaction, and everything after it (one COUNT, one INSERT whose FK
          check takes KEY SHARE on this very row, already held stronger) waits
          on nothing — a cycle needs a transaction that waits while holding,
          which this one never does. If ``postgres_lock_timeout_seconds``
          still trips (a long-lived holder elsewhere), the failure shape is
          psycopg's LockNotAvailable surfacing as a 500 — the pre-existing
          shape of every notebook-row lock timeout, not a new path.

        A vanished notebook raises ``KeyError`` (mapped to the existing 404).
        This existence probe is a PG-only bonus — the lock has to read the row
        anyway. The SQLite twin takes no row lock and deliberately adds no
        probe: both routes 404 earlier via ``get_row``/capability guards, so
        the divergence (SQLite would hit the FK on INSERT instead) is a
        registered backend difference on a production-unreachable path, not a
        parity contract."""
        row = connection.execute(
            "SELECT 1 FROM notebooks WHERE id=%s FOR NO KEY UPDATE",
            (notebook_id,),
        ).fetchone()
        if row is None:
            raise KeyError(notebook_id)

    def mark_indexing_chunk_fallback(self, source_id: str, warning_code: str) -> None:
        """见 SourceStorePort:非空写稳定前缀诊断(不覆盖既有 MinerU 诊断),空只清自己。"""
        prefix = INDEXING_CHUNK_FALLBACK_WARNING_PREFIX
        with self.database.write() as connection:
            if warning_code:
                connection.execute(
                    "UPDATE sources SET error_message=%s WHERE id=%s AND "
                    "(error_message IS NULL OR error_message='' OR error_message LIKE %s)",
                    (f"{prefix} {warning_code}", source_id, f"{prefix}%"),
                )
            else:
                connection.execute(
                    "UPDATE sources SET error_message='' WHERE id=%s "
                    "AND error_message LIKE %s",
                    (source_id, f"{prefix}%"),
                )

    @staticmethod
    def clear_chunked_at(connection, source_id: str) -> None:
        connection.execute(
            "UPDATE sources SET chunked_at=NULL WHERE id=%s", (source_id,)
        )

    def get_source(self, source_id: str) -> SourceDetail:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sources WHERE id=%s", (source_id,)
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
            paper_meta = self.paper_meta_for_sources(connection, [source_id]).get(
                source_id
            )
            summary = self.source_from_row(
                connection, row, paper_meta=paper_meta
            )
        return SourceDetail(
            **summary.model_dump(),
            file_path=row["file_path"],
            error_message=row["error_message"],
            paper_meta=self.paper_meta_model(paper_meta),
        )

    def source_exists(self, source_id: str) -> bool:
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM sources WHERE id=%s", (source_id,)
                ).fetchone()
                is not None
            )

    @staticmethod
    def source_exists_tx(connection, source_id: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sources WHERE id=%s FOR KEY SHARE", (source_id,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def source_exists_for_update_tx(
        connection, source_id: str, notebook_id: str | None = None
    ) -> bool:
        """Take the aggregate lock before deleting projection children.

        Project completion takes ``FOR KEY SHARE`` on this row.  Teardown
        takes the conflicting lock first, before touching graph rows, which
        gives both paths one source -> derived-row lock order and prevents a
        deadlock or a projection resurrecting children after deletion.
        """
        if notebook_id is not None:
            SourceStore._lock_notebook_row_for_capacity(connection, notebook_id)
        return (
            connection.execute(
                "SELECT 1 FROM sources WHERE id=%s FOR UPDATE", (source_id,)
            ).fetchone()
            is not None
        )

    def source_elements(self, source_id: str) -> list[SourceElement]:
        self.get_source(source_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_elements WHERE source_id=%s "
                "ORDER BY created_at,id COLLATE \"C\"",
                (source_id,),
            ).fetchall()
        return [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json_value(row["metadata"], {}),
            )
            for row in rows
        ]

    def source_elements_after(
        self,
        source_id: str,
        after: "tuple[Any, str] | None",
        limit: int,
    ) -> "tuple[list[SourceElement], tuple[Any, str] | None]":
        """PostgreSQL twin of the whole-source keyset walk; see the SQLite
        adapter for why ``source_elements_page`` is not reused for it.

        ``created_at`` is ``timestamptz`` here, so the cursor carries the
        ``datetime`` this adapter returned — never a re-rendered string (same
        reason as ``source_element_type_page``).  The ordering matches
        ``source_elements`` verbatim, ``COLLATE "C"`` included.
        """
        limit = max(1, int(limit))
        params: list[Any] = [source_id]
        clause = ""
        if after is not None:
            clause = "AND (created_at, id COLLATE \"C\") > (%s, %s) "
            params.extend([after[0], after[1]])
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id,source_id,element_type,location_label,text,"
                "metadata,created_at FROM source_elements WHERE source_id=%s "
                f"{clause}"
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s",
                tuple(params),
            ).fetchall()
        items = [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json_value(row["metadata"], {}),
            )
            for row in rows
        ]
        next_after = (
            (rows[-1]["created_at"], rows[-1]["id"]) if len(rows) == limit else None
        )
        return items, next_after

    def source_elements_page(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 40,
        anchor_element_id: str = "",
    ) -> PaginatedSourceElements:
        """PostgreSQL twin of the bounded source-detail element reader."""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        with self.database.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sources WHERE id=%s", (source_id,)
            ).fetchone() is None:
                raise KeyError(source_id)
            total = int(connection.execute(
                "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()["c"])
            if anchor_element_id:
                anchor = connection.execute(
                    "SELECT created_at,id FROM source_elements "
                    "WHERE source_id=%s AND id=%s",
                    (source_id, anchor_element_id),
                ).fetchone()
                if anchor is not None:
                    before = int(connection.execute(
                        "SELECT COUNT(*) AS c FROM source_elements "
                        "WHERE source_id=%s AND (created_at<%s OR "
                        "(created_at=%s AND id COLLATE \"C\"<%s))",
                        (source_id, anchor["created_at"], anchor["created_at"], anchor["id"]),
                    ).fetchone()["c"])
                    offset = (before // limit) * limit
            rows = connection.execute(
                "SELECT * FROM source_elements WHERE source_id=%s "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT %s OFFSET %s",
                (source_id, limit, offset),
            ).fetchall()
        return PaginatedSourceElements(
            items=[
                SourceElement(
                    id=row["id"],
                    source_id=row["source_id"],
                    element_type=row["element_type"],
                    location_label=row["location_label"],
                    text=row["text"],
                    metadata=json_value(row["metadata"], {}),
                )
                for row in rows
            ],
            total_count=total,
            offset=offset,
            limit=limit,
        )

    # ------------------------------------------- typed-collection catalog
    # Backend twin of the SQLite primitives; see that adapter's docstrings for
    # WHY the signal is (updated_at, parse_status, chunked_at).
    def source_change_signal_rows(
        self, connection: Any, notebook_id: str
    ) -> list[tuple[str, str, str, bool]]:
        """``[(source_id, opaque change signal, created_at sort key,
        user_visible)]`` for physical source rows, EXCLUDING the private Memory
        synthetic rows (see the SQLite adapter for why the exclusion is
        unconditional).

        The token is formatted here rather than in the service so the caller
        never has to know that PostgreSQL hands back ``datetime`` objects
        where SQLite hands back ISO text: both backends produce a string that
        is only ever compared for equality against a token from the same
        store.

        The created-at key is normalized by ``_sort_key`` for the same reason,
        but for ORDERING rather than equality: every row in one notebook goes
        through the same formatting, so lexicographic comparison over these
        strings reproduces this backend's ``ORDER BY created_at, id`` — the
        order ``list_sources`` returns and the source tab shows.

        ``user_visible`` is evaluated in SQL from this module's
        ``VISIBLE_SOURCE_TYPES_PREDICATE`` — the same constant ``list_sources``
        filters on — on a row this query already visits.  It exists so the
        catalog does NOT need a second ``sources`` read per notebook: nothing
        indexes ``source_type``, so a "which ids are hidden" query has to scan
        every source row of the notebook, and it would do that on the request
        path immediately after this query walked the same rows.
        """
        rows = connection.execute(
            "SELECT id,updated_at,parse_status,chunked_at,created_at,"
            f"({VISIBLE_SOURCE_TYPES_PREDICATE}) AS user_visible FROM sources "
            "WHERE notebook_id=%s AND source_type<>'memory'",
            (notebook_id,),
        ).fetchall()
        return [
            (
                row["id"],
                "{}|{}|{}".format(
                    row["updated_at"] if row["updated_at"] is not None else "",
                    row["parse_status"] or "",
                    row["chunked_at"] if row["chunked_at"] is not None else "",
                ),
                _sort_key(row["created_at"]),
                bool(row["user_visible"]),
            )
            for row in rows
        ]

    def memory_source_ids(self, connection: Any, notebook_id: str) -> list[str]:
        """The notebook's private Memory synthetic source ids — the exact
        complement of the exclusion above; see the SQLite adapter for the
        single-definition argument and the cost shape."""
        return [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM sources "
                f"WHERE notebook_id=%s AND {MEMORY_SOURCE_TYPE_PREDICATE}",
                (notebook_id,),
            ).fetchall()
        ]

    def visible_parse_status_counts(
        self, connection: Any, notebook_id: str
    ) -> list[tuple[str, int]]:
        """``[(parse_status, count)]`` over this notebook's user-visible sources;
        see the SQLite adapter for why this is its own query rather than a
        widening of the (contractually opaque) change signal."""
        return [
            (str(row["parse_status"] or ""), int(row["c"]))
            for row in connection.execute(
                "SELECT parse_status, COUNT(*) AS c FROM sources "
                f"WHERE notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                "GROUP BY parse_status",
                (notebook_id,),
            ).fetchall()
        ]

    # Batch width for the typed-collection count, deliberately narrower than
    # the class-wide ``IN_CHUNK`` (5 000, tuned for id hydration that returns
    # one row per id).  This query returns up to len(element_types) rows PER
    # source, and the planner materializes the whole group before we see it,
    # so the batch bounds intermediate rows, not just bound parameters.  A
    # local constant rather than a change to ``IN_CHUNK``: that attribute is
    # shared with unrelated hydration paths this reasoning does not cover.
    COUNT_IN_CHUNK = 1024

    def element_type_count_rows(
        self,
        connection: Any,
        source_ids: Sequence[str],
        element_types: Sequence[str],
    ) -> list[tuple[str, str, int]]:
        """``[(source_id, element_type, count)]``, bounded and index-assisted
        via ``idx_source_elements_source_type``.

        ``= ANY(%s)`` keeps one bound parameter per list (no placeholder
        explosion).  The planner still chooses the access path — an index scan
        for a selective batch, a bitmap scan when the batch covers much of the
        table — and that choice is deliberately NOT part of the contract; what
        is fixed is that the element-type restriction stays in the query, so
        a source's prose is never read to count its formulas.
        """
        ids = list(dict.fromkeys(value for value in source_ids if value))
        types = list(dict.fromkeys(value for value in element_types if value))
        if not ids or not types:
            return []
        out: list[tuple[str, str, int]] = []
        for offset in range(0, len(ids), self.COUNT_IN_CHUNK):
            batch = ids[offset : offset + self.COUNT_IN_CHUNK]
            rows = connection.execute(
                "SELECT source_id,element_type,COUNT(*) AS c FROM source_elements "
                "WHERE source_id=ANY(%s) AND element_type=ANY(%s) "
                "GROUP BY source_id,element_type",
                (batch, types),
            ).fetchall()
            out.extend(
                (row["source_id"], row["element_type"], int(row["c"])) for row in rows
            )
        return out

    # --------------------------------------- typed-collection enumeration
    def element_page_rows(
        self,
        connection: Any,
        source_id: str,
        element_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> list[Any]:
        """Backend twin of the SQLite keyset page; see that adapter for why the
        cursor value is passed back unparsed.

        Here it matters more than on SQLite: ``created_at`` is ``timestamptz``,
        so the value handed back is a ``datetime``.  Re-rendering it as text
        and re-parsing it would risk a microsecond/offset round-trip that
        silently skips or repeats a row at a page boundary.  Row values
        ``(created_at, id) > (%s, %s)`` are index-comparable, keeping this on
        ``idx_source_elements_source_type``.
        """
        params: list[Any] = [source_id, element_type]
        clause = ""
        if after is not None:
            clause = "AND (created_at,id) > (%s,%s) "
            params.extend([after[0], after[1]])
        params.append(max(1, int(limit)))
        return connection.execute(
            "SELECT id,source_id,element_type,location_label,text,created_at,"
            "metadata->>'asset_id' AS asset_id "
            "FROM source_elements WHERE source_id=%s AND element_type=%s "
            f"{clause}"
            "ORDER BY created_at,id LIMIT %s",
            tuple(params),
        ).fetchall()

    def source_display_rows(
        self, connection: Any, source_ids: Sequence[str]
    ) -> list[Any]:
        """Labels (and the owning notebook) for enumerated items, on the
        caller's connection."""
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return []
        out: list[Any] = []
        for offset in range(0, len(ids), self.COUNT_IN_CHUNK):
            batch = ids[offset : offset + self.COUNT_IN_CHUNK]
            out.extend(connection.execute(
                "SELECT s.id,s.notebook_id,s.title,s.file_name,"
                "m.is_paper,m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "WHERE s.id=ANY(%s)",
                (batch,),
            ).fetchall())
        return out

    def visible_source_identity_rows_bounded(
        self, connection: Any, notebook_id: str, limit: int
    ) -> list[Any]:
        if int(limit) <= 0:
            return []
        return connection.execute(
            "SELECT s.id,s.notebook_id,s.title,s.file_name,"
            "m.is_paper,m.paper_title "
            "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
            f"WHERE s.notebook_id=%s AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
            "ORDER BY s.created_at,s.id LIMIT %s",
            (notebook_id, int(limit)),
        ).fetchall()

    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(value for value in element_ids if value))
        if not ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset : offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,source_id,element_type,location_label,text,metadata "
                    f"FROM source_elements WHERE id IN ({placeholders(batch)})",
                    batch,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["metadata"] = _metadata_compat(item["metadata"])
                    result[row["id"]] = item
        return result

    def image_asset_rows(
        self, element_ids: Sequence[str]
    ) -> list[tuple[str, Any]]:
        """``(id, metadata)`` for the image elements among ``element_ids``.

        The narrow sibling of ``evidence_elements`` — see ``SourceStorePort``
        for why the citation-image path may not reuse the wide reader: both
        predicates are pushed into SQL and ``text`` (the whole reason the wide
        read is expensive) is never selected.  ``metadata`` still goes through
        ``_metadata_compat`` so both backends hand the consumer the same JSON
        TEXT carrier.
        """
        ids = list(dict.fromkeys(value for value in element_ids if value))
        if not ids:
            return []
        out: list[tuple[str, Any]] = []
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset : offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,metadata FROM source_elements "
                    f"WHERE id IN ({placeholders(batch)}) AND element_type='image'",
                    batch,
                ).fetchall()
                out.extend(
                    (row["id"], _metadata_compat(row["metadata"])) for row in rows
                )
        return out

    def source_listing_rows(
        self, connection: Any, source_ids: Sequence[str]
    ) -> list[Any]:
        """The source-card projection (title / type / stored summary), on the
        caller's connection; ``source_metadata`` below is this method plus its
        own connection.  See the SQLite adapter for why they share one SQL."""
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return []
        out: list[Any] = []
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset : offset + self.IN_CHUNK]
            out.extend(connection.execute(
                "SELECT s.id,s.notebook_id,s.title,s.file_name,s.summary,s.doc_type,"
                "s.source_type,m.is_paper,m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                f"WHERE s.id IN ({placeholders(batch)})",
                batch,
            ).fetchall())
        return out

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return {}
        with self.database.connect() as connection:
            return {
                row["id"]: dict(row)
                for row in self.source_listing_rows(connection, ids)
            }

    @staticmethod
    def retrieval_element_rows(
        connection,
        notebook_id: str,
        allowed_source_ids: Sequence[str] | None = None,
    ):
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            return connection.execute(
                "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
                "s.title AS source_title,m.vector AS vector "
                "FROM source_elements e JOIN sources s ON s.id=e.source_id "
                "LEFT JOIN element_embeddings m ON m.element_id=e.id "
                "WHERE s.notebook_id=%s AND e.source_id=ANY(%s) ORDER BY e.ordinal",
                (notebook_id, source_ids),
            ).fetchall()
        return connection.execute(
            "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
            "s.title AS source_title,m.vector AS vector "
            "FROM source_elements e JOIN sources s ON s.id=e.source_id "
            "LEFT JOIN element_embeddings m ON m.element_id=e.id "
            "WHERE s.notebook_id=%s ORDER BY e.ordinal",
            (notebook_id,),
        ).fetchall()

    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> dict[str, object]:
        """Bounded corpus profile: SQL aggregates plus representative sources."""
        representative_limit = max(1, min(int(representative_limit), 64))
        distribution_limit = max(1, min(int(distribution_limit), 64))
        with self.database.connect() as connection:
            totals = dict(connection.execute(
                "SELECT COUNT(*) AS total_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND BTRIM(COALESCE(m.paper_title,''))<>'' THEN 1 ELSE 0 END) AS metadata_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND m.pub_year BETWEEN 1000 AND 9999 THEN 1 ELSE 0 END) AS known_year_sources,"
                "SUM(CASE WHEN COALESCE(s.file_hash,'')='' AND NOT "
                "(COALESCE(m.is_paper,0)=1 AND BTRIM(COALESCE(m.paper_title,''))<>'') "
                "THEN 1 ELSE 0 END) AS identity_uncertain_sources "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id "
                "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow')",
                (notebook_id,),
            ).fetchone())
            type_rows = [dict(row) for row in connection.execute(
                "SELECT type,count FROM (SELECT "
                "COALESCE(NULLIF(BTRIM(s.doc_type),''),"
                "NULLIF(BTRIM(s.source_type),''),'unknown') AS type,COUNT(*) AS count "
                "FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') GROUP BY 1) grouped "
                "ORDER BY count DESC,type COLLATE \"C\" LIMIT %s",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            year_rows = [dict(row) for row in connection.execute(
                "SELECT m.pub_year AS year,COUNT(*) AS count FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 GROUP BY m.pub_year "
                "ORDER BY m.pub_year DESC LIMIT %s",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            duplicate_row = dict(connection.execute(
                "SELECT "
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "WHERE s.notebook_id=%s AND s.source_type NOT IN ('memory','knowhow') "
                "AND COALESCE(s.file_hash,'')<>'' GROUP BY s.file_hash HAVING COUNT(*)>1) d),0) "
                "AS hash_duplicate_excess,"
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') AND m.is_paper=1 "
                "AND BTRIM(COALESCE(m.paper_title,''))<>'' "
                "GROUP BY LOWER(BTRIM(m.paper_title)) HAVING COUNT(*)>1) d),0) "
                "AS title_duplicate_excess",
                (notebook_id, notebook_id),
            ).fetchone())
            representatives = [dict(row) for row in connection.execute(
                "WITH base AS (SELECT s.id,s.title,s.file_name,s.source_type,s.doc_type,"
                "m.paper_title,CASE WHEN m.is_paper=1 THEN m.pub_year ELSE NULL END AS pub_year,"
                "m.is_paper,s.created_at,"
                "COALESCE(NULLIF(BTRIM(s.doc_type),''),"
                "NULLIF(BTRIM(s.source_type),''),'unknown') AS type_key,"
                "CASE WHEN m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 "
                "THEN m.pub_year ELSE NULL END AS year_key FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow')),"
                "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY type_key "
                "ORDER BY created_at,id COLLATE \"C\") AS type_rank,"
                "ROW_NUMBER() OVER (PARTITION BY year_key "
                "ORDER BY created_at,id COLLATE \"C\") AS year_rank FROM base) "
                "SELECT id,title,file_name,source_type,doc_type,paper_title,pub_year,is_paper "
                "FROM ranked ORDER BY CASE WHEN type_rank=1 THEN 0 WHEN year_rank=1 THEN 1 ELSE 2 END,"
                "type_key COLLATE \"C\",year_key DESC NULLS LAST,created_at,"
                "id COLLATE \"C\" LIMIT %s",
                (notebook_id, representative_limit),
            ).fetchall()]
        return {
            **totals,
            **duplicate_row,
            "type_distribution": type_rows[:distribution_limit],
            "type_distribution_truncated": len(type_rows) > distribution_limit,
            "year_distribution": year_rows[:distribution_limit],
            "year_distribution_truncated": len(year_rows) > distribution_limit,
            "representatives": representatives,
        }

    def report_source_identity_rows(
        self, source_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        ids = list(dict.fromkeys(str(value) for value in source_ids if value))[:1024]
        if not ids:
            return []
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id,s.file_hash,m.paper_title,m.is_paper FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.id=ANY(%s)",
                (ids,),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_titles(self, source_ids: list[str]) -> dict[str, str]:
        ids = [str(value) for value in source_ids if value]
        if not ids:
            return {}
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT id,title FROM sources WHERE id IN ({placeholders(ids)})",
                ids,
            ).fetchall()
        return {row["id"]: row["title"] for row in rows}

    def notebook_element_sample(
        self, notebook_id: str, *, max_chars: int = 8000
    ) -> list[dict]:
        budget = max(0, int(max_chars))
        if budget == 0:
            return []
        result: list[dict] = []
        rendered = 0
        after_ordinal = 0
        with self.database.connect() as connection:
            while rendered < budget:
                rows = connection.execute(
                    "SELECT e.ordinal AS _ordinal,e.location_label,"
                    "substring(e.text FROM 1 FOR %s) AS text "
                    "FROM source_elements e JOIN sources s ON s.id=e.source_id "
                    "WHERE s.notebook_id=%s AND e.ordinal>%s "
                    "ORDER BY e.ordinal LIMIT %s",
                    (budget, notebook_id, after_ordinal, 32),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    after_ordinal = int(row["_ordinal"])
                    location = str(row["location_label"] or "")
                    prefix = f"[{location}] "
                    separator = 1 if result else 0
                    available = budget - rendered - separator - len(prefix)
                    if available <= 0:
                        return result
                    text = str(row["text"] or "")[:available]
                    result.append({"location_label": location, "text": text})
                    rendered += separator + len(prefix) + len(text)
                    if rendered >= budget:
                        return result
        return result

    def insert_source(
        self,
        *,
        source_id: str,
        notebook_id: str,
        title: str,
        source_type: str,
        status: str,
        parse_status: str,
        file_name: str,
        file_path: str,
        file_size: int,
        file_hash: str,
        summary: str,
        doc_type: str,
        source_url: str = "",
        memory_id: str = "",
        agent_profile_id: str = "",
        connection=None,
        capacity_limit: "int | None" = None,
    ) -> None:
        # ``capacity_limit`` (None = exempt): enforce the notebook's
        # visible-document ceiling atomically with this insert — notebook-row
        # lock, then COUNT, then INSERT, all in one owned transaction (see
        # ``_lock_notebook_row_for_capacity`` for why the lock is the
        # serialization). Own-transaction shape only, same rule and reason as
        # the SQLite twin's docstring.
        if capacity_limit is not None and connection is not None:
            raise ValueError(
                "capacity_limit requires an owned write transaction; "
                "it cannot join a caller-provided connection"
            )
        statement = (
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "source_url,file_size,file_hash,summary,doc_type,memory_id,"
            "agent_profile_id,created_at,updated_at,uploaded_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        now = self.now()
        values = (
            source_id,
            notebook_id,
            title,
            source_type,
            status,
            parse_status,
            file_name,
            file_path,
            source_url,
            file_size,
            file_hash,
            summary,
            doc_type,
            memory_id or None,
            # "" -> NULL, the same sentinel-free shape memory_id uses: NULL is
            # what every pre-v48 row carries and what the Agent-facing delete
            # check reads as "a person added this, never removable by an Agent".
            # Whitespace-only is the same statement as "": strip before the NULL
            # fold, or "   " lands a non-NULL row that reads as "an Agent added
            # this" while naming no agent. Same expression as the SQLite twin.
            (agent_profile_id or "").strip() or None,
            now,
            now,
            ((self.current_user_id() or "").strip() or None)
            if source_type not in {"memory", "knowhow"}
            else None,
        )
        visible = source_type not in {"memory", "knowhow"}
        if connection is not None:
            if visible:
                self._lock_notebook_row_for_capacity(connection, notebook_id)
            connection.execute(statement, values)
            return
        with self.database.write() as owned:
            if visible:
                self._lock_notebook_row_for_capacity(owned, notebook_id)
            if capacity_limit is not None:
                current = self._visible_document_count_on(owned, notebook_id)
                if current >= capacity_limit:
                    raise DocumentCapacityExceeded(current, capacity_limit)
            owned.execute(statement, values)

    def source_id_for_memory(self, memory_id: str) -> str | None:
        if not memory_id:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE memory_id=%s", (memory_id,)
            ).fetchone()
        return str(row["id"]) if row else None

    def set_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: str | None = None,
        error_message: str = "",
    ) -> None:
        fields = ["status=%s", "parse_status=%s", "error_message=%s", "updated_at=%s"]
        params: list[object] = [status, status, error_message, self.now()]
        if summary is not None:
            fields.insert(2, "summary=%s")
            params.insert(2, summary)
        with self.database.write() as connection:
            connection.execute(
                f"UPDATE sources SET {','.join(fields)} WHERE id=%s",
                (*params, source_id),
            )

    def update_file_hash(
        self,
        source_id: str,
        file_hash: str,
        *,
        title: str | None = None,
        connection=None,
    ) -> None:
        fields = ["file_hash=%s", "updated_at=%s"]
        params: list[object] = [file_hash, self.now()]
        if title is not None:
            fields.insert(0, "title=%s")
            params.insert(0, title)
        statement = f"UPDATE sources SET {','.join(fields)} WHERE id=%s"
        values = (*params, source_id)
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as owned:
            owned.execute(statement, values)

    def source_id_by_hash(self, notebook_id: str, digest: str) -> str | None:
        """Existing source in this notebook whose content hash matches ``digest``
        (upload/batch dedup). Empty digest never matches (URL / metadata-only
        rows store ``file_hash=''``); memory/knowhow hidden-synthetic rows are
        excluded; oldest row wins (``ORDER BY created_at, id``). Mirrors the
        SQLite ``SourceStore.source_id_by_hash`` rule so UI and CLI dedup alike."""
        if not digest:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM sources WHERE notebook_id=%s AND file_hash=%s "
                "AND source_type NOT IN ('memory', 'knowhow') "
                "ORDER BY created_at, id",
                (notebook_id, digest),
            ).fetchone()
        return str(row["id"]) if row else None

    def set_doc_type(self, source_id: str, doc_type: str) -> None:
        """Persist a corrected extraction-profile id on ONE source row (the
        upload path's 'same file, different doc_type' correction). Caller passes
        an already-normalized value; storage only, no vocabulary re-judging."""
        with self.database.write() as connection:
            connection.execute(
                "UPDATE sources SET doc_type=%s, updated_at=%s WHERE id=%s",
                (doc_type, self.now(), source_id),
            )

    def clear_paper_meta(self, source_id: str) -> None:
        """Reverse of upsert_paper_meta: drop this source's paper-meta marker row
        and author rows (retype to a non-paper doc_type). Idempotent."""
        with self.database.write() as connection:
            connection.execute(
                "DELETE FROM source_paper_meta WHERE source_id=%s", (source_id,)
            )
            connection.execute(
                "DELETE FROM source_authors WHERE source_id=%s", (source_id,)
            )

    def claim_failed_for_retry(self, source_id: str) -> bool:
        """Atomically flip a FAILED source to 'queued' to claim a retry; returns
        whether THIS caller won (rowcount==1). One WHERE-guarded UPDATE — the
        loser sees parse_status no longer 'failed' and must not reschedule."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET parse_status='queued', status='queued', "
                "error_message='', updated_at=%s "
                "WHERE id=%s AND parse_status='failed'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def claim_reextract_if_extracted(self, source_id: str) -> bool:
        """Atomically flip a settled 'extracted' source to 'extracting' to claim a
        doc-type re-extraction; returns whether THIS caller won (rowcount==1)."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET parse_status='extracting', status='extracting', "
                "error_message='', updated_at=%s "
                "WHERE id=%s AND parse_status='extracted'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def mark_extracted_if_doc_type(
        self, source_id: str, expected_doc_type: str, *, error_message: str = ""
    ) -> bool:
        """Atomically mark a source 'extracted' IFF its stored doc_type still
        equals ``expected_doc_type`` (what the just-finished extraction used);
        returns whether the terminal transition landed (rowcount==1). rowcount 0
        means a concurrent retype changed the type — the caller re-extracts."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE sources SET status='extracted', parse_status='extracted', "
                "error_message=%s, updated_at=%s "
                "WHERE id=%s AND doc_type=%s",
                (error_message, self.now(), source_id, expected_doc_type),
            )
        return cursor.rowcount == 1

    def insert_source_if_absent(
        self,
        *,
        source_id: str,
        notebook_id: str,
        digest: str,
        title: str,
        source_type: str,
        status: str,
        parse_status: str,
        file_name: str,
        file_path: str,
        file_size: int,
        summary: str,
        doc_type: str,
        agent_profile_id: str = "",
        capacity_limit: "int | None" = None,
    ) -> str | None:
        """Atomic content-dedup insert (postgres): the whole ``write()`` block is
        one transaction, so the dedup re-check and the insert commit together —
        two concurrent identical first-uploads can't both create a row. Returns an
        existing same-content visible source's id (caller reuses it, no row
        created) or None (a new row was inserted with ``file_hash=digest``).
        Mirrors the SQLite SourceStore rule (same dedup SELECT + insert_source
        shape); the SQLite side takes RESERVED via BEGIN IMMEDIATE, here the
        transaction boundary is the atomicity.

        ``agent_profile_id`` reaches the insert branch only — a reused row keeps
        the provenance of whoever created it (see the SQLite twin).

        ``capacity_limit`` (None = exempt) enforces the visible-document
        ceiling in this same transaction: the notebook-row lock is taken FIRST
        (before even the dedup re-check, so both re-checks run after the
        serialization point — see ``_lock_notebook_row_for_capacity``), the
        COUNT runs on this connection, and an at-or-over-limit notebook raises
        ``DocumentCapacityExceeded``. The dedup re-check still decides first:
        re-uploaded existing bytes reuse their row even in a full notebook."""
        with self.database.write() as connection:
            if source_type not in {"memory", "knowhow"}:
                self._lock_notebook_row_for_capacity(connection, notebook_id)
            if digest:
                row = connection.execute(
                    "SELECT id FROM sources WHERE notebook_id=%s AND file_hash=%s "
                    "AND source_type NOT IN ('memory', 'knowhow') "
                    "ORDER BY created_at, id",
                    (notebook_id, digest),
                ).fetchone()
                if row is not None:
                    return str(row["id"])
            if capacity_limit is not None:
                current = self._visible_document_count_on(connection, notebook_id)
                if current >= capacity_limit:
                    raise DocumentCapacityExceeded(current, capacity_limit)
            self.insert_source(
                source_id=source_id,
                notebook_id=notebook_id,
                title=title,
                source_type=source_type,
                status=status,
                parse_status=parse_status,
                file_name=file_name,
                file_path=file_path,
                file_size=file_size,
                file_hash=digest,
                summary=summary,
                doc_type=doc_type,
                agent_profile_id=agent_profile_id,
                connection=connection,
            )
        return None

    def replace_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        """Backend twin of the SQLite swap — including the ``updated_at`` bump
        that flips the change signal atomically with the new element
        generation (see the SQLite docstring for why it exists and why it is
        harmless)."""
        created_at = normalize_timestamp(created_at)
        source = connection.execute(
            "SELECT notebook_id,source_type FROM sources WHERE id=%s", (source_id,)
        ).fetchone()
        if source is None:
            raise KeyError(source_id)
        if source["source_type"] not in {"memory", "knowhow"}:
            self._lock_notebook_row_for_capacity(
                connection, str(source["notebook_id"])
            )
        # Fixed writer order is notebook -> source, matching the generation
        # publisher and closing both phantom-source and element-swap races.
        locked = connection.execute(
            "SELECT id FROM sources WHERE id=%s FOR UPDATE", (source_id,)
        ).fetchone()
        if locked is None:
            raise KeyError(source_id)
        connection.execute("DELETE FROM source_elements WHERE source_id=%s", (source_id,))
        execute_many(
            connection,
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    item.id,
                    source_id,
                    item.element_type,
                    item.location_label,
                    item.text,
                    jsonb(dict(item.metadata)),
                    created_at,
                )
                for item in elements
            ],
        )
        connection.execute(
            "UPDATE sources SET updated_at=%s WHERE id=%s", (created_at, source_id)
        )

    def delete_source_row(self, connection, source_id: str) -> None:
        connection.execute("DELETE FROM sources WHERE id=%s", (source_id,))

    def insert_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: TimestampInput,
    ) -> None:
        created_at = normalize_timestamp(created_at)
        execute_many(
            connection,
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [
                (
                    item.id,
                    source_id,
                    item.element_type,
                    item.location_label,
                    item.text,
                    jsonb(dict(item.metadata)),
                    created_at,
                )
                for item in elements
            ],
        )

    def delete_elements_by_knowhow_row(
        self, connection, source_id: str, row_id: str
    ) -> None:
        connection.execute(
            "DELETE FROM source_elements WHERE source_id=%s "
            "AND metadata #>> '{knowhow,row_id}'=%s",
            (source_id, row_id),
        )

    def source_from_row(
        self,
        connection,
        row: Mapping[str, Any],
        *,
        paper_meta: dict | None | object = _UNSET,
    ) -> SourceSummary:
        source_id = row["id"]
        element_count = int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM source_elements WHERE source_id=%s",
                (source_id,),
            ).fetchone()["count"]
        )
        # 三个 run 子查询原本相关到 ``o.source_id``,写在 EXISTS 内部,于是每扫到一行
        # knowledge_objects 就重算一遍;而它们只依赖 source_id,对这次调用是**常量**。
        # 提到 EXISTS 外面直接绑定 %s 之后:KO 存在性是一次半连接(idx_knowledge_objects_source
        # 首行即停),run 判定是 ≤3 次索引探针(idx_extraction_runs_source_created)。
        # ``o.source_id<>''`` 保留在 EXISTS 内:等值连接把它传递到绑定参数上,与原查询
        # 逐字等价(绑定的 id 为空串时两版都判 false)。批量兄弟见 sources_from_rows。
        kg_extracted = bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects o "
                "WHERE o.source_id=%s AND o.source_id<>'') AND COALESCE(("
                "SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=%s AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1"
                "),'completed')='completed' AND NOT ("
                "COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=%s AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'') "
                " ~ 'windows_failed=[1-9][0-9]*/[0-9]+' OR "
                "strpos(COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=%s AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),''),"
                " 'retry_incomplete=1')>0) AS ok",
                (source_id, source_id, source_id, source_id),
            ).fetchone()["ok"]
        )
        meta = (
            self.paper_meta_for_sources(connection, [source_id]).get(source_id)
            if paper_meta is _UNSET
            else paper_meta
        )
        # 一次读取喂两个派生字段(告警 + 「已分析但零产出」);见 SQLite 侧同名方法。
        latest_run_status, latest_run_error = self.latest_run_state(
            connection, source_id
        )
        summary = SourceSummary(
            id=source_id,
            notebook_id=row["notebook_id"],
            title=row["title"],
            display_title=summary_display_title(row, meta),
            type=row["source_type"],
            status=row["status"],
            summary=row["summary"],
            element_count=element_count,
            file_name=row["file_name"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parse_status=row["parse_status"],
            created_at=iso_timestamp(row["created_at"]),
            created_label=_created_label(row["created_at"]),
            doc_type=row.get("doc_type", ""),
            source_url=row.get("source_url", ""),
            # v48 provenance: "the column is not NULL", never the raw agent id.
            agent_created=row.get("agent_profile_id") is not None,
            extraction_warning=extraction_warning_text(latest_run_error),
            parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
            kg_extracted=kg_extracted,
            kg_analyzed_empty=kg_analyzed_without_objects(
                latest_run_status, latest_run_error
            ),
            authors=[author["name"] for author in meta["authors"]] if meta else [],
            pub_year=meta["pub_year"] if meta else None,
            venue=meta["venue"] if meta else None,
        )
        summary.paper_meta_status = self._paper_meta_status_for(row, meta)
        return summary

    def sources_from_rows(
        self, connection, rows: list[Mapping[str, Any]]
    ) -> list[SourceSummary]:
        if not rows:
            return []
        source_ids = [row["id"] for row in rows]
        paper_meta = self.paper_meta_for_sources(connection, source_ids)
        element_counts: dict[str, int] = {}
        kg_extracted_ids: set[str] = set()
        latest_error: dict[str, str] = {}
        latest_status: dict[str, str] = {}
        for offset in range(0, len(source_ids), self.IN_CHUNK):
            batch = source_ids[offset : offset + self.IN_CHUNK]
            marker = placeholders(batch)
            for row in connection.execute(
                "SELECT source_id,COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({marker}) GROUP BY source_id",
                batch,
            ).fetchall():
                element_counts[row["source_id"]] = int(row["c"])
            # kg_extracted 的驱动集是**页内的 source id**,不是 knowledge_objects 行。
            #
            # 原查询 ``SELECT DISTINCT o.source_id FROM knowledge_objects o WHERE ...``
            # 把三个 run 子查询挂在每一行 KO 上;它们只依赖 ``o.source_id``,对固定
            # source 是常量,而 DISTINCT 又把结果折回 source 粒度——所以「每行算一次」
            # 的开销纯属浪费。现场数据:4.9 万 source 的 notebook 取一页 50 条,页内
            # 共 33.9 万 KO 行,这一条查询 3650ms、101 万次子查询执行、508 万 shared
            # buffers,是来源页签 3.3s 墙钟的大头。
            #
            # 改写后每个 source 至多 3 次 run 索引探针(idx_extraction_runs_source_created)
            # 加 1 次 KO 半连接(idx_knowledge_objects_source,首行即停),一页 50 个 id
            # 约 200 次探测取代百万次。``o.source_id<>''`` 留在 EXISTS 内:等值连接把它
            # 传递到 page_sources.source_id 上,与原查询逐字等价。
            #
            # 这不依赖「每个 source 只有一条 run」这一生产观测:latest-run 的取法
            # (同一条 ORDER BY + LIMIT 1)一字未动,多条 run 时语义与原查询一致。
            #
            # CTE 用 ``WITH x(col) AS (VALUES ...)`` 而不是 LATERAL 或 ``AS t(col)``
            # 表别名列名:前者两端都支持,后两者 SQLite 没有,而这条查询与 SQLite 侧
            # 同名方法是同构的一对(见那边同一处注释)。
            page_values = ",".join("(%s)" for _ in batch)
            for row in connection.execute(
                f"WITH page_sources(source_id) AS (VALUES {page_values}) "
                "SELECT source_id FROM page_sources "
                "WHERE EXISTS(SELECT 1 FROM knowledge_objects o "
                " WHERE o.source_id=page_sources.source_id AND o.source_id<>'') "
                "AND COALESCE((SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'completed')='completed' "
                "AND NOT (COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'') "
                " ~ 'windows_failed=[1-9][0-9]*/[0-9]+' OR "
                "strpos(COALESCE((SELECT r.error_message FROM extraction_runs r "
                " WHERE r.source_id=page_sources.source_id AND r.run_type='kg' "
                " ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),''),"
                " 'retry_incomplete=1')>0)",
                batch,
            ).fetchall():
                kg_extracted_ids.add(row["source_id"])
            for row in connection.execute(
                "SELECT source_id,status,error_message FROM extraction_runs "
                f"WHERE source_id IN ({marker}) "
                "ORDER BY source_id COLLATE \"C\",created_at DESC,ordinal DESC",
                batch,
            ).fetchall():
                # 同一行的两列一起取:选中的是哪一行完全由上面的 ORDER BY 决定,
                # status 只是搭同一趟车,不新增查询。
                if row["source_id"] in latest_error:
                    continue
                latest_error[row["source_id"]] = row["error_message"] or ""
                latest_status[row["source_id"]] = row["status"] or ""

        def warning(source_id: str) -> str | None:
            # 派生规则只有一份(app.models.sources),这里只负责取到「最近一次抽取的
            # error_message」这个输入。
            return extraction_warning_text(latest_error.get(source_id, ""))

        output: list[SourceSummary] = []
        for row in rows:
            source_id = row["id"]
            meta = paper_meta.get(source_id)
            summary = SourceSummary(
                id=source_id,
                notebook_id=row["notebook_id"],
                title=row["title"],
                display_title=summary_display_title(row, meta),
                type=row["source_type"],
                status=row["status"],
                summary=row["summary"],
                element_count=element_counts.get(source_id, 0),
                file_name=row["file_name"],
                file_size=row["file_size"],
                file_hash=row["file_hash"],
                parse_status=row["parse_status"],
                created_at=iso_timestamp(row["created_at"]),
                created_label=_created_label(row["created_at"]),
                doc_type=row.get("doc_type", ""),
                source_url=row.get("source_url", ""),
                # v48 provenance: "the column is not NULL", never the raw agent id.
                agent_created=row.get("agent_profile_id") is not None,
                extraction_warning=warning(source_id),
                parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
                kg_extracted=source_id in kg_extracted_ids,
                kg_analyzed_empty=kg_analyzed_without_objects(
                    latest_status.get(source_id, ""),
                    latest_error.get(source_id, ""),
                ),
                authors=[author["name"] for author in meta["authors"]] if meta else [],
                pub_year=meta["pub_year"] if meta else None,
                venue=meta["venue"] if meta else None,
            )
            summary.paper_meta_status = self._paper_meta_status_for(row, meta)
            output.append(summary)
        return output

    def extraction_warning(self, connection, source_id: str) -> str | None:
        return extraction_warning_text(
            self.latest_run_state(connection, source_id)[1]
        )

    @staticmethod
    def latest_run_state(connection, source_id: str) -> "tuple[str, str]":
        """最近一次抽取记录的 ``(status, error_message)``——两个派生字段的共同输入。
        与 SQLite 侧同名方法逐条对应(含「刻意不加 run_type 过滤」那条理由)。"""
        run = connection.execute(
            "SELECT status,error_message FROM extraction_runs WHERE source_id=%s "
            "ORDER BY created_at DESC,ordinal DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if run is None:
            return ("", "")
        return (str(run["status"] or ""), str(run["error_message"] or ""))

    @staticmethod
    def meta_source_rows(
        connection, notebook_id: str, pending_source_id: str = ""
    ) -> list[dict]:
        rows = connection.execute(
            "SELECT title,doc_type,summary FROM sources WHERE notebook_id=%s "
            "AND source_type NOT IN ('memory','knowhow') "
            "AND (status='extracted' OR id=%s) "
            "ORDER BY created_at,id COLLATE \"C\"",
            (notebook_id, pending_source_id),
        ).fetchall()
        return [
            {"title": row["title"], "doc_type": row["doc_type"], "summary": row["summary"]}
            for row in rows
        ]

    def meta_sources(self, notebook_id: str, pending_source_id: str = "") -> list[dict]:
        with self.database.connect() as connection:
            return self.meta_source_rows(connection, notebook_id, pending_source_id)

    def upsert_paper_meta(self, source_id: str, notebook_id: str, meta: dict) -> None:
        now = self.now()
        raw_json = json_value(meta.get("raw_json") or {}, {})
        with self.database.write() as connection:
            notebook = connection.execute(
                "SELECT id FROM notebooks WHERE id=%s FOR KEY SHARE",
                (notebook_id,),
            ).fetchone()
            if notebook is None:
                raise KeyError(notebook_id)
            source = connection.execute(
                "SELECT id FROM sources WHERE id=%s AND notebook_id=%s "
                "FOR KEY SHARE",
                (source_id, notebook_id),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            connection.execute(
                "INSERT INTO source_paper_meta"
                "(source_id,notebook_id,is_paper,paper_title,venue,pub_year,doi,keywords,"
                "raw_json,model,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(source_id) DO UPDATE SET "
                "is_paper=excluded.is_paper,paper_title=excluded.paper_title,"
                "venue=excluded.venue,pub_year=excluded.pub_year,doi=excluded.doi,"
                "keywords=excluded.keywords,raw_json=excluded.raw_json,model=excluded.model,"
                "updated_at=excluded.updated_at",
                (
                    source_id,
                    notebook_id,
                    1 if meta.get("is_paper") else 0,
                    meta.get("paper_title"),
                    meta.get("venue"),
                    meta.get("pub_year"),
                    meta.get("doi"),
                    jsonb(meta.get("keywords") or []),
                    jsonb(raw_json),
                    str(meta.get("model") or ""),
                    now,
                    now,
                ),
            )
            connection.execute("DELETE FROM source_authors WHERE source_id=%s", (source_id,))
            execute_many(
                connection,
                "INSERT INTO source_authors"
                "(id,source_id,notebook_id,position,name,affiliation,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (
                        f"{source_id}:auth:{int(author.get('position', 0)):03d}",
                        source_id,
                        notebook_id,
                        int(author.get("position", 0)),
                        str(author.get("name") or "").strip(),
                        str(author.get("affiliation") or "").strip(),
                        now,
                    )
                    for author in meta.get("authors") or []
                ],
            )

    @staticmethod
    def _paper_meta_dict(row: Mapping[str, Any], authors: list[Mapping[str, Any]]) -> dict:
        return {
            "source_id": row["source_id"],
            "is_paper": bool(row["is_paper"]),
            "paper_title": row["paper_title"],
            "venue": row["venue"],
            "pub_year": row["pub_year"],
            "doi": row["doi"],
            "keywords": json_value(row["keywords"], []),
            "model": row["model"],
            "authors": [
                {
                    "position": author["position"],
                    "name": author["name"],
                    "affiliation": author["affiliation"],
                }
                for author in authors
            ],
        }

    @staticmethod
    def _paper_meta_status_for(
        row: Mapping[str, Any], meta: dict | None
    ) -> str | None:
        """SQLite 孪生方法的对等实现:分类规则本身只有一份
        (`app.models.sources.paper_meta_status`),这里只翻译入参。"""
        return paper_meta_status(
            None if meta is None else bool(meta.get("is_paper")),
            row.get("source_type", ""),
            row.get("doc_type", ""),
            row.get("parse_status", ""),
        )

    @staticmethod
    def paper_meta_model(meta: dict | None) -> PaperMeta | None:
        if meta is None:
            return None
        return PaperMeta(
            is_paper=meta["is_paper"],
            title=meta["paper_title"],
            venue=meta["venue"],
            year=meta["pub_year"],
            doi=meta["doi"],
            keywords=list(meta["keywords"]),
            authors=[
                PaperAuthor(name=author["name"], affiliation=author["affiliation"])
                for author in meta["authors"]
            ],
        )

    def get_paper_meta(self, source_id: str) -> dict | None:
        with self.database.connect() as connection:
            return self.paper_meta_for_sources(connection, [source_id]).get(source_id)

    def paper_meta_for_sources(
        self, connection, source_ids: Sequence[str]
    ) -> dict[str, dict]:
        meta_rows: dict[str, Mapping[str, Any]] = {}
        author_rows: dict[str, list[Mapping[str, Any]]] = {}
        ids = list(source_ids)
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset : offset + self.IN_CHUNK]
            marker = placeholders(batch)
            for row in connection.execute(
                f"SELECT * FROM source_paper_meta WHERE source_id IN ({marker})", batch
            ).fetchall():
                meta_rows[row["source_id"]] = row
            for row in connection.execute(
                "SELECT source_id,position,name,affiliation FROM source_authors "
                f"WHERE source_id IN ({marker}) "
                "ORDER BY source_id COLLATE \"C\",position",
                batch,
            ).fetchall():
                author_rows.setdefault(row["source_id"], []).append(row)
        return {
            source_id: self._paper_meta_dict(row, author_rows.get(source_id, []))
            for source_id, row in meta_rows.items()
        }

    def sources_missing_paper_meta(
        self, notebook_id: str, include_existing: bool = False
    ) -> list[str]:
        missing = "" if include_existing else PAPER_META_NO_META_SQL
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s"
                + PAPER_META_ELIGIBLE_SQL
                + missing
                + " ORDER BY s.created_at,s.id COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
        return [row["id"] for row in rows]
