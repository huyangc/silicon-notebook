from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

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
from app.repositories.sqlite.database import SqliteDatabase


# Sentinel distinguishing "paper_meta not passed" (source_from_row should
# fetch it itself) from an explicit `paper_meta=None` ("caller already
# fetched — there is no meta row"). A plain `None` default couldn't tell
# those two cases apart.
_UNSET = SOURCE_PAPER_META_UNSET


# 「用户可见文档」的判定谓词 —— 排除 Memory 派生源与 knowhow 表隐藏投影源
# (source_type IN ('memory', 'knowhow')):两者都是无独立用户可见内容的内部派生链接。
# 这是 list_sources_page 的 total_count 与文档数量上限强制(visible_document_count)
# 的**单一真源**:两处口径逐字一致才不会出现「面板显示 19 篇但配额算到 18」。
# 内部真集路径(get_source/pending_kg/copy/scale-index)刻意不用它,保留完整集合。
VISIBLE_SOURCE_TYPES_PREDICATE = "source_type NOT IN ('memory', 'knowhow')"


# 「这一行是私有 Memory 的合成来源」的 SQL 谓词——`memory_source_ids` 取的正是这批
# 行,而 `source_change_signal_rows` 排掉的也正是这批行(一条谓词,两个方向)。
#
# 提升成模块级常量而不是继续在每条语句里裸写,是因为它已经不只有「取 id 清单」一个
# 消费方了:`query_store` 的两条底座聚合(知识对象计数、Top 概念名)现在把这条排除
# **压进它们自己的语句内**——先读 id 清单再相减/传排除清单,两次读之间没有共享快照,
# PG 的 READ COMMITTED 下一次并发的 Memory 增删就会让相减漏减、让排除清单漏项,
# 而后一种漏项泄漏的是**概念名称**本身。唯一定义点因此从 Python 方法下移到这条谓词。
#
# ⚠ 用在子查询里时保持不加限定词(`SELECT 1 FROM sources WHERE sources.id = o.
# source_id AND source_type = 'memory'`):`knowledge_objects` / `concept_clusters`
# 都没有 `source_type` 列,所以最内层的 `sources` 是它唯一能解析到的表。
MEMORY_SOURCE_TYPE_PREDICATE = "source_type = 'memory'"


# 论文元数据补抽候选的 SQL 谓词(接在 ``FROM sources s`` 且已按 ``s.notebook_id``
# 过滤之后)。三个消费方共用:sources_missing_paper_meta(补抽排队)、
# query_store.notebook_analytics 的 missing 计数、以及 NotebookSummary.
# paper_meta_missing 的 EXISTS 探针(「补全论文信息」按钮的显示门)——口径由这
# 一份保证不漂移。合规条件与 models.sources 的 PAPER_META_ELIGIBLE_* 常量同义。
PAPER_META_ELIGIBLE_SQL = (
    " AND s.source_type NOT IN ('memory', 'knowhow')"
    " AND s.doc_type IN ('', 'academic_paper')"
    " AND s.parse_status IN ('parsed', 'extracting', 'extracted')"
)
# 「尚无 meta 行」半句单独成段:sources_missing_paper_meta 的 --force
# (include_existing=True)要能把它拿掉。
PAPER_META_NO_META_SQL = (
    " AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)"
)


def _created_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日"


class SourceStore:
    """SQLite sources/source_elements row persistence plus the SourceSummary
    hydration (element counts, extraction warnings, KG-extracted flags — the C5
    batched projection). Row-level only — notebook existence guards, the
    parse/summarize pipeline, status events and file handling stay in the
    facade orchestration."""

    # IN(...) batching bound for the batched hydration lookups — mirrors the
    # facade's `_IN_CHUNK` (well under SQLite's default 999-variable limit).
    IN_CHUNK = 900

    def __init__(
        self,
        database: SqliteDatabase,
        *,
        now: Callable[[], str],
        current_user_id: Callable[[], str] = lambda: "",
    ) -> None:
        self.database = database
        self.now = now
        self.current_user_id = current_user_id

    # ------------------------------------------------------------------ reads
    def all_visible_source_ids(self, notebook_id: str) -> list[str]:
        """Return the current visible-source universe for graph drift checks."""
        with self.database.connect() as db:
            return [row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id=? "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} ORDER BY id",
                (notebook_id,),
            ).fetchall()]

    def hidden_source_ids(self, notebook_id: str, owner_id: str) -> list[str]:
        """Hidden Memory/Knowhow projection participants **for one user**, in
        stable id order.

        The hidden half is NOT uniform.  **Knowhow** projection sources are
        notebook-wide content — every member reads the table itself, so every
        member's ceiling admits them.  A **Memory** projection source belongs
        to ONE user: ``memory_items.created_by`` is the predicate every other
        Memory path already uses (``MemoryStore.memory_retrieval_rows``'
        ``m.created_by=?``, ``memory_for_user``, and
        ``source_change_signal_rows``' unconditional exclusion of Memory from
        typed-collection listings).  Without this filter, a shared notebook's
        default non-narrowed request froze EVERY member's Memory projection
        source into the requesting user's ceiling, and the Memory-derived
        elements and KG objects those sources own then became reachable
        through ordinary candidate retrieval and through the whole-graph/PPR
        channels a non-narrowed run re-enables.

        The filter is in the SQL, not on the result, for two reasons: another
        member's Memory source ids never enter this process at all, and a
        later edit cannot drop a result-side ``if`` and silently reopen the
        leak.  It costs no extra round trip and no extra access path either:
        ``s.source_type <> 'memory'`` short-circuits the ``EXISTS`` for every
        Knowhow row, so the primary-key probe into ``memory_items`` fires only
        for rows that are Memory projections (bounded by the notebook's
        confirmed-Memory count via ``idx_sources_memory_id``).  A Memory source
        whose origin row is gone fails the ``EXISTS`` and drops out — fail
        closed on an orphan.

        Both consumers must pass the SAME identity: the boundary takes it from
        ``current_user()`` and carries it on the frozen scope, so the retrieval
        drift probe re-reads this partition in the frame the freeze was taken
        in.  An owner-scoped freeze compared against an unfiltered live read
        would report drift forever in any shared notebook that holds another
        member's Memory, silently disabling the whole-graph/PPR/relation/exact
        channels for good.

        Deliberately its own query rather than folded into the visible-half
        read: the compact ``include`` freeze reads only the requested ids plus
        a count, and merging the two would put every source row back on that
        hot path to answer a question bounded by the projection count.
        """
        with self.database.connect() as db:
            return [row["id"] for row in db.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=? "
                "AND s.source_type IN ('memory','knowhow') "
                "AND (s.source_type <> 'memory' OR EXISTS ("
                "SELECT 1 FROM memory_items m "
                "WHERE m.id = s.memory_id AND m.created_by = ?)) "
                "ORDER BY s.id",
                (notebook_id, owner_id),
            ).fetchall()]

    def visible_source_scope_snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> tuple[list[str], int]:
        """Validate selected ids and count the visible universe in one snapshot.

        The returned id rows are bounded by ``source_ids``; the scalar count is
        used only to distinguish a genuinely narrowed include-list from an
        explicit all-selected snapshot.  The compact path uses one SQL
        statement, so a concurrent mutation cannot produce a false
        all-selected classification.
        """
        requested = list(dict.fromkeys(str(value) for value in source_ids if value))
        with self.database.connect() as db:
            if not requested:
                total = int(db.execute(
                    "SELECT COUNT(*) AS c FROM sources "
                    f"WHERE notebook_id=? AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
                    (notebook_id,),
                ).fetchone()["c"])
                return [], total
            requested_json = json.dumps(requested, ensure_ascii=False)
            rows = db.execute(
                "WITH requested(id, ordinal) AS ("
                "SELECT CAST(value AS TEXT), CAST(key AS INTEGER) FROM json_each(?)"
                "), visible AS ("
                "SELECT requested.id, requested.ordinal FROM requested "
                "CROSS JOIN sources ON sources.id=requested.id "
                f"WHERE sources.notebook_id=? AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                "), stats(visible_count) AS ("
                "SELECT COUNT(*) FROM sources WHERE notebook_id=? "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
                ") SELECT visible.id, stats.visible_count FROM stats "
                "LEFT JOIN visible ON 1=1 ORDER BY visible.ordinal",
                (requested_json, notebook_id, notebook_id),
            ).fetchall()
            visible = [str(row["id"]) for row in rows if row["id"] is not None]
            total = int(rows[0]["visible_count"])
        return visible, total

    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        """User-facing source list — excludes Memory-derived AND knowhow-table
        hidden synthetic rows (source_type IN ('memory', 'knowhow')): both are
        internal derivation links with no independent user-visible content
        (memory-kg-extract Task 3; knowhow-tables PR-1 Task 5's hidden
        projection source, mirroring the SAME mechanism rather than inventing
        a second one), and would otherwise double-count right next to the
        Memory panel / show a phantom "Knowhow 表：…" source card. Internal
        paths (get_source, pending_kg_source_count, copy materialization,
        scale-index scans) deliberately keep the true full set — do not add
        this filter there."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                # `, id` 是必需的次键,不是装饰:`created_at` 在同一次批量导入里
                # 会出现并列值,而 SQLite 对并列行不保证稳定顺序。来源清单枚举
                # 按 `(created_at, id)` 走,两处一旦分叉,「前 N 篇」在页签与目录
                # 里就是不同的 N 篇——而那正是模型据以逐篇深挖的那份前缀。
                # PostgreSQL 侧本来就带(`ORDER BY created_at,id COLLATE "C"`)。
                "ORDER BY created_at ASC, id ASC",
                (notebook_id,),
            ).fetchall()
            return self.sources_from_rows(db, rows)

    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50,
                          q: str = "") -> PaginatedSources:
        """分页 + 可选 q(按 title/file_name/作者名/论文标题 服务端过滤)。万级 source
        安全:只取一页 + 一次 COUNT,不全量进内存。同 list_sources 排除 source_type
        IN ('memory', 'knowhow') 的隐藏合成源(含 total_count),内部真集路径
        (get_source/pending_kg/copy/scale-index)不受影响。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        needle = (q or "").strip().lower()
        where = f"WHERE notebook_id = ? AND {VISIBLE_SOURCE_TYPES_PREDICATE}"
        params: List[object] = [notebook_id]
        if needle:
            where += (
                " AND (LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?"
                " OR EXISTS(SELECT 1 FROM source_authors a"
                "    WHERE a.source_id = sources.id AND LOWER(a.name) LIKE ?)"
                " OR EXISTS(SELECT 1 FROM source_paper_meta m"
                "    WHERE m.source_id = sources.id AND LOWER(m.paper_title) LIKE ?))"
            )
            like = f"%{needle}%"
            params += [like, like, like, like]
        with self.database.connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) c FROM sources {where}", params).fetchone()["c"]
            rows = db.execute(
                f"SELECT * FROM sources {where} "
                # 同上:并列 created_at 下没有次键,翻页会重复/漏行,而且与来源
                # 清单的 `(created_at, id)` 序分叉。
                "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            items = self.sources_from_rows(db, rows)
        return PaginatedSources(items=items, total_count=total, offset=offset, limit=limit)

    def visible_document_count(self, notebook_id: str) -> int:
        """本笔记本「用户可见文档」数量 —— 与 list_sources_page 的 total_count 逐字
        同口径(共用 VISIBLE_SOURCE_TYPES_PREDICATE),是文档数量上限强制的当前计数。
        排除 Memory 派生源与 knowhow 隐藏投影源;内部真集路径不走此方法。"""
        with self.database.connect() as db:
            return self._visible_document_count_on(db, notebook_id)

    @staticmethod
    def _visible_document_count_on(db: sqlite3.Connection, notebook_id: str) -> int:
        """The visible-document COUNT on a caller-provided connection — shared by
        the read path (``visible_document_count``) and the atomic capacity gate
        inside the creation write transactions (``insert_source`` /
        ``insert_source_if_absent``), so the ceiling can never be enforced
        against a different count than the one the UI reports."""
        row = db.execute(
            "SELECT COUNT(*) AS c FROM sources "
            f"WHERE notebook_id = ? AND {VISIBLE_SOURCE_TYPES_PREDICATE}",
            (notebook_id,),
        ).fetchone()
        return int(row["c"])

    def get_source(self, source_id: str) -> SourceDetail:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row is None:
                raise KeyError(source_id)
            # Fetch paper-meta ONCE and thread it into source_from_row (which
            # also needs it for authors/pub_year/venue) instead of letting it
            # fetch its own copy — SourceDetail needs the raw dict here too
            # (for paper_meta_model), so a shared local avoids a second
            # paper_meta_for_sources round trip for the same source_id.
            pm = self.paper_meta_for_sources(db, [source_id]).get(source_id)
            summary = self.source_from_row(db, row, paper_meta=pm)
            return SourceDetail(
                **summary.model_dump(),
                file_path=row["file_path"],
                error_message=row["error_message"],
                paper_meta=self.paper_meta_model(pm),
            )

    def source_exists(self, source_id: str) -> bool:
        """Cheap existence probe (a single ``SELECT 1``, not the full row +
        paper-meta hydration ``get_source`` does) — for a caller that only
        needs to know whether the row is still there, e.g.
        ``KnowhowProjector.project_table``'s pre-terminal-write re-check
        guarding against a concurrent ``delete_table_projection`` (a move or
        a plain delete) landing mid-pass."""
        with self.database.connect() as db:
            return db.execute(
                "SELECT 1 FROM sources WHERE id = ?", (source_id,)
            ).fetchone() is not None

    @staticmethod
    def source_exists_tx(connection: sqlite3.Connection, source_id: str) -> bool:
        """Tx-scoped variant of ``source_exists`` — see
        ``KnowhowStore.table_exists_tx`` for why this exists (PR review round
        2 P1-1: the caller must run this on the SAME connection/transaction
        as the write it gates, not on a separately-opened one, or the
        check-then-write pair is still a TOCTOU gap)."""
        return connection.execute(
            "SELECT 1 FROM sources WHERE id = ?", (source_id,)
        ).fetchone() is not None

    @staticmethod
    def source_exists_for_update_tx(
        connection: sqlite3.Connection,
        source_id: str,
        notebook_id: str | None = None,
    ) -> bool:
        """Lock-order seam shared with PostgreSQL projection teardown.

        SQLite's caller has already acquired ``BEGIN IMMEDIATE``, so a plain
        existence probe runs under the database-wide write reservation.
        """
        del notebook_id
        return connection.execute(
            "SELECT 1 FROM sources WHERE id = ?", (source_id,)
        ).fetchone() is not None

    def source_elements(self, source_id: str) -> List[SourceElement]:
        self.get_source(source_id)          # KeyError guard, same as the facade did
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM source_elements WHERE source_id = ? ORDER BY created_at ASC, id ASC",
                (source_id,),
            ).fetchall()
        return [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json.loads(row["metadata"] or "{}"),
            )
            for row in rows
        ]

    def source_elements_after(
        self,
        source_id: str,
        after: "tuple[object, str] | None",
        limit: int,
    ) -> "tuple[List[SourceElement], tuple[object, str] | None]":
        """One keyset page of this source's elements + the next cursor.

        Same global order as ``source_elements`` (``created_at, id``), so a
        whole-source walk visits exactly the same elements in exactly the same
        sequence — only bounded. ``source_elements_page`` deliberately is NOT
        reused for that: it pages by ``OFFSET`` (and clamps ``limit`` to 100
        for the detail view), which makes a full-source walk quadratic.

        The row-value comparison is the same deliberate spelling as
        ``source_element_type_page``: SQLite optimizes ``(a, b) > (?, ?)``
        against the multi-column ``idx_source_elements_source_created``, while
        the ``created_at > ? OR (created_at = ? AND id > ?)`` form degrades
        into a scan of the whole source range on every page.

        ``after`` values are handed straight back unparsed. ``None`` is
        returned as the next cursor once the page came up short — the caller
        stops without a final empty query.
        """
        limit = max(1, int(limit))
        params: List[object] = [source_id]
        clause = ""
        if after is not None:
            clause = "AND (created_at, id) > (?, ?) "
            params.extend([after[0], after[1]])
        params.append(limit)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT id, source_id, element_type, location_label, text, "
                "metadata, created_at FROM source_elements WHERE source_id = ? "
                f"{clause}"
                "ORDER BY created_at, id LIMIT ?",
                params,
            ).fetchall()
        items = [
            SourceElement(
                id=row["id"],
                source_id=row["source_id"],
                element_type=row["element_type"],
                location_label=row["location_label"],
                text=row["text"],
                metadata=json.loads(row["metadata"] or "{}"),
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
        """Read one stable source-element window instead of hydrating a book.

        The existing full-list method remains for ingestion and internal
        callers.  Source detail uses this bounded reader; an optional anchor
        resolves to the page containing a citation target.
        """
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 100))
        with self.database.connect() as db:
            if db.execute(
                "SELECT 1 FROM sources WHERE id=?", (source_id,)
            ).fetchone() is None:
                raise KeyError(source_id)
            total = int(db.execute(
                "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=?",
                (source_id,),
            ).fetchone()["c"])
            if anchor_element_id:
                anchor = db.execute(
                    "SELECT created_at,id FROM source_elements "
                    "WHERE source_id=? AND id=?",
                    (source_id, anchor_element_id),
                ).fetchone()
                if anchor is not None:
                    before = int(db.execute(
                        "SELECT COUNT(*) AS c FROM source_elements "
                        "WHERE source_id=? AND (created_at<? OR "
                        "(created_at=? AND id<?))",
                        (source_id, anchor["created_at"], anchor["created_at"], anchor["id"]),
                    ).fetchone()["c"])
                    offset = (before // limit) * limit
            rows = db.execute(
                "SELECT * FROM source_elements WHERE source_id=? "
                "ORDER BY created_at ASC,id ASC LIMIT ? OFFSET ?",
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
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                for row in rows
            ],
            total_count=total,
            offset=offset,
            limit=limit,
        )

    # ------------------------------------------- typed-collection catalog
    # (reasoning enumeration tools PR-2): the two primitives behind
    # app.services.collection_catalog.  Both take the CALLER's connection so a
    # whole scope (active notebook + mounted bases) is read from one snapshot.
    def source_change_signal_rows(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> List[tuple[str, str, str, bool]]:
        """``[(source_id, opaque change signal, created_at sort key,
        user_visible)]`` for the notebook's PHYSICAL sources, EXCLUDING the
        private Memory synthetic rows.

        Knowhow's hidden projection source stays in (it is notebook-wide
        content, visible to every member through the table itself); a
        ``source_type='memory'`` row does not.  A confirmed Memory belongs to
        one user, and every other channel treats it that way — Ask reaches it
        only through the owner-scoped Memory retriever, Knowhow completion
        excludes it from its candidate set by contract.  A typed-collection
        listing has no owner filter of its own: it is built from a notebook's
        participant scope, so any member of a shared notebook would otherwise
        read another member's confirmed Memory through its formulas, tables,
        images and code blocks.  The exclusion is unconditional — the same
        listing means the same thing in a single-user notebook as in a shared
        one — and its complement is ``memory_source_ids`` below (one predicate,
        two directions; ``test_memory_source_ids_complement_the_signal_rows``
        pins them together).

        The signal is ``updated_at | parse_status | chunked_at`` because those
        three are what the element writers touch:
          * EVERY element swap: ``replace_elements`` advances ``updated_at`` to
            the instant its new elements carry, inside the caller's write
            transaction.  That alone makes the signal flip atomically with the
            element generation, on a first parse as well as on a reparse.  The
            two clauses below are older, narrower guarantees that still hold
            and cost nothing to keep — this one is the load-bearing rule;
          * REPARSE of an already-chunked source: ``replace_elements`` and
            ``clear_chunked_at`` run in the SAME write transaction
            (``SourceIngestionService.process_source``), and ``chunked_at``
            held a value, so nulling it flips the signal too;
          * the Memory-derived reparse folds ``update_file_hash``
            (``updated_at``) into the same transaction as its
            ``replace_elements``;
          * ``set_status`` moves both ``parse_status`` and ``updated_at`` on
            every lifecycle transition;
          * the Knowhow projector is the only other element writer and it only
            ever writes ``element_type='knowhow_cell'``, which no enumerable
            kind whitelist contains — it cannot change a counted number.
        A deleted source simply drops out of this list.  ONE index-seeked
        query per notebook (a ``notebook_id``-prefixed index); deliberately
        NOT a timestamp comparison — the catalog only ever tests tokens for
        equality.  The ``source_type`` restriction rides along for free: the
        three signal columns are not carried by any index, so every candidate
        row is visited anyway and the predicate is a residual filter on rows
        already in hand, never a second access path.
        """
        rows = db.execute(
            "SELECT id, updated_at, parse_status, chunked_at, created_at, "
            # The user-visible flag is EVALUATED HERE, from the same constant
            # ``list_sources`` filters on, on a row this query already visits.
            # It replaces a second ``sources`` read per notebook: the old
            # ``hidden_source_ids`` query could not seek on ``source_type``
            # (no index carries it), so it re-scanned every source row of the
            # notebook to find the one or two hidden ones — on the request path,
            # right after this query had just walked the same rows.
            f"({VISIBLE_SOURCE_TYPES_PREDICATE}) AS user_visible "
            "FROM sources WHERE notebook_id = ? AND source_type <> 'memory'",
            (notebook_id,),
        ).fetchall()
        return [
            (
                row["id"],
                "{}|{}|{}".format(
                    row["updated_at"] or "",
                    row["parse_status"] or "",
                    row["chunked_at"] or "",
                ),
                # Sort key, passed through as stored: ``list_sources`` orders by
                # this very column with SQLite's default BINARY collation, and
                # Python's codepoint comparison over the same ASCII timestamp
                # text is that same ordering.  So the roster the sources
                # collection walks is byte-for-byte the source tab's order —
                # not a second interpretation of it.
                str(row["created_at"] or ""),
                bool(row["user_visible"]),
            )
            for row in rows
        ]

    def memory_source_ids(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> List[str]:
        """The notebook's private Memory synthetic source ids.

        The exact complement of ``source_change_signal_rows``' exclusion, and
        the ONE place the services layer learns which sources are Memory.  The
        element side can filter in SQL because it already reads every source
        row; the KG side cannot — ``knowledge_objects`` carries no source type
        — so it filters the rows it reads against this set instead, exactly the
        way it already filters them against ``USABLE_STATUSES``.

        Bounded by the notebook's Memory count (one synthetic source per
        confirmed Memory, enforced by ``idx_sources_memory_id``), and returned
        as ids rather than as a count so the caller can both filter rows and
        subtract the objects those sources own from the enumeration
        denominator, without a second spelling of "which sources are Memory".
        One query per notebook, same ``notebook_id``-prefixed index seek as the
        signal query, projecting the primary key only.
        """
        return [
            row["id"]
            for row in db.execute(
                "SELECT id FROM sources "
                f"WHERE notebook_id = ? AND {MEMORY_SOURCE_TYPE_PREDICATE}",
                (notebook_id,),
            ).fetchall()
        ]

    def visible_parse_status_counts(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> List[tuple[str, int]]:
        """``[(parse_status, count)]`` over this notebook's user-visible sources.

        A second read of the same rows ``source_change_signal_rows`` walks, and
        deliberately so: that row's change signal is contractually opaque, so
        the ``parse_status`` inside it cannot be consumed even though the bytes
        are there.  Same ``notebook_id``-prefixed seek, same residual
        visible-source predicate; the result is one row per distinct status.
        """
        return [
            (str(row["parse_status"] or ""), int(row["c"]))
            for row in db.execute(
                "SELECT parse_status, COUNT(*) AS c FROM sources "
                f"WHERE notebook_id = ? AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
                "GROUP BY parse_status",
                (notebook_id,),
            ).fetchall()
        ]

    def element_type_count_rows(
        self,
        db: sqlite3.Connection,
        source_ids: Sequence[str],
        element_types: Sequence[str],
    ) -> List[tuple[str, str, int]]:
        """``[(source_id, element_type, count)]`` — covered by
        ``idx_source_elements_source_type`` (source_id, element_type,
        created_at, id): the whitelist keeps each source to one seek per
        requested type instead of scanning its whole element range.  Both
        lists are deduplicated; the source batch leaves room for the type
        placeholders under SQLite's variable limit."""
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        types = list(
            dict.fromkeys(element_type for element_type in element_types if element_type)
        )
        if not ids or not types:
            return []
        type_marks = ",".join("?" for _ in types)
        batch_size = max(1, self.IN_CHUNK - len(types))
        out: List[tuple[str, str, int]] = []
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset:offset + batch_size]
            id_marks = ",".join("?" for _ in batch)
            for row in db.execute(
                "SELECT source_id, element_type, COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({id_marks}) AND element_type IN ({type_marks}) "
                "GROUP BY source_id, element_type",
                (*batch, *types),
            ).fetchall():
                out.append((row["source_id"], row["element_type"], int(row["c"])))
        return out

    # --------------------------------------- typed-collection enumeration
    def element_page_rows(
        self,
        db: sqlite3.Connection,
        source_id: str,
        element_type: str,
        after: tuple[object, str] | None,
        limit: int,
    ) -> List[sqlite3.Row]:
        """One keyset page of ``(source_id, element_type)`` ordered by
        ``(created_at, id)``.

        The row-value comparison is deliberate: SQLite optimizes
        ``(a, b) > (?, ?)`` against a multi-column index (since 3.15), so this
        stays an index range seek on ``idx_source_elements_source_type``.  The
        equivalent ``created_at > ? OR (created_at = ? AND id > ?)`` spelling
        does NOT — it degrades into a scan of the whole (source, type) range
        on every page, which is exactly the cost this cursor exists to avoid.

        ``after`` carries values this adapter returned earlier; nothing is
        reparsed or reformatted, so a cursor round trip cannot skip or repeat
        a row through a timestamp-formatting difference.

        ``asset_id`` is projected with JSON1 ``json_extract`` rather than by
        selecting ``metadata``: an image needs its short asset id, while a
        table's metadata carries the whole rendered HTML that this listing
        deliberately does not transfer.
        """
        params: List[object] = [source_id, element_type]
        clause = ""
        if after is not None:
            clause = "AND (created_at, id) > (?, ?) "
            params.extend([after[0], after[1]])
        params.append(max(1, int(limit)))
        return db.execute(
            "SELECT id, source_id, element_type, location_label, text, created_at, "
            "json_extract(metadata, '$.asset_id') AS asset_id "
            "FROM source_elements WHERE source_id = ? AND element_type = ? "
            f"{clause}"
            "ORDER BY created_at, id LIMIT ?",
            params,
        ).fetchall()

    def source_display_rows(
        self, db: sqlite3.Connection, source_ids: Sequence[str]
    ) -> List[sqlite3.Row]:
        """Labels for enumerated items, on the caller's connection.

        ``source_paper_meta`` is joined here rather than queried separately so
        a grounded paper title costs no extra round trip; the join side is 1:1
        on the source primary key.
        """
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        if not ids:
            return []
        out: List[sqlite3.Row] = []
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset:offset + self.IN_CHUNK]
            marks = ",".join("?" for _ in batch)
            out.extend(db.execute(
                "SELECT s.id, s.notebook_id, s.title, s.file_name, "
                "m.is_paper, m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id = s.id "
                f"WHERE s.id IN ({marks})",
                batch,
            ).fetchall())
        return out

    def visible_source_identity_rows_bounded(
        self, db: sqlite3.Connection, notebook_id: str, limit: int
    ) -> List[sqlite3.Row]:
        if int(limit) <= 0:
            return []
        return db.execute(
            "SELECT s.id, s.notebook_id, s.title, s.file_name, "
            "m.is_paper, m.paper_title "
            "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
            f"WHERE s.notebook_id=? AND {VISIBLE_SOURCE_TYPES_PREDICATE} "
            "ORDER BY s.created_at, s.id LIMIT ?",
            (notebook_id, int(limit)),
        ).fetchall()

    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(element_id for element_id in element_ids if element_id))
        if not ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self.database.connect() as db:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset:offset + self.IN_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(
                    "SELECT id, source_id, element_type, location_label, text, metadata "
                    f"FROM source_elements WHERE id IN ({placeholders})",
                    batch,
                ).fetchall():
                    out[row["id"]] = dict(row)
        return out

    def image_asset_rows(
        self, element_ids: Sequence[str]
    ) -> List[tuple[str, Any]]:
        """``(id, metadata)`` for the image elements among ``element_ids``.

        The narrow sibling of ``evidence_elements`` — see ``SourceStorePort``
        for why the citation-image path may not reuse the wide reader: both
        predicates are pushed into SQL and ``text`` (the whole reason the wide
        read is expensive) is never selected.
        """
        ids = list(dict.fromkeys(element_id for element_id in element_ids if element_id))
        if not ids:
            return []
        out: List[tuple[str, Any]] = []
        with self.database.connect() as db:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset:offset + self.IN_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                out.extend(
                    (row["id"], row["metadata"])
                    for row in db.execute(
                        "SELECT id, metadata FROM source_elements "
                        f"WHERE id IN ({placeholders}) AND element_type='image'",
                        batch,
                    ).fetchall()
                )
        return out

    def source_listing_rows(
        self, db: sqlite3.Connection, source_ids: Sequence[str]
    ) -> List[sqlite3.Row]:
        """The source-card projection (title / type / stored summary), on the
        caller's connection.

        One SQL text, two entry points: ``source_metadata`` below is this method
        plus its own connection.  The sources collection needs the caller's
        connection (an enumeration keeps its whole walk on one), and it needs
        exactly these columns — so making them two spellings of one projection
        would only create room for them to disagree.
        """
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        if not ids:
            return []
        out: List[sqlite3.Row] = []
        for offset in range(0, len(ids), self.IN_CHUNK):
            batch = ids[offset:offset + self.IN_CHUNK]
            placeholders = ",".join("?" for _ in batch)
            out.extend(db.execute(
                "SELECT s.id, s.notebook_id, s.title, s.file_name, s.summary, "
                "s.doc_type, s.source_type, m.is_paper, m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                f"WHERE s.id IN ({placeholders})",
                batch,
            ).fetchall())
        return out

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        if not ids:
            return {}
        with self.database.connect() as db:
            return {
                row["id"]: dict(row)
                for row in self.source_listing_rows(db, ids)
            }

    @staticmethod
    def retrieval_element_rows(
        db: sqlite3.Connection,
        notebook_id: str,
        allowed_source_ids: Sequence[str] | None = None,
    ):
        if allowed_source_ids is not None:
            source_ids = list(dict.fromkeys(allowed_source_ids))
            if not source_ids:
                return []
            placeholders = ",".join("?" for _ in source_ids)
            return db.execute(
                "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
                "s.title AS source_title,em.vector AS vector "
                "FROM source_elements e JOIN sources s ON s.id=e.source_id "
                "LEFT JOIN element_embeddings em ON em.element_id=e.id "
                f"WHERE s.notebook_id=? AND e.source_id IN ({placeholders})",
                (notebook_id, *source_ids),
            ).fetchall()
        return db.execute(
            """
            SELECT e.id, e.source_id, e.element_type, e.location_label, e.text,
                   s.title AS source_title, em.vector AS vector
            FROM source_elements e
            JOIN sources s ON s.id = e.source_id
            LEFT JOIN element_embeddings em ON em.element_id = e.id
            WHERE s.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()

    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> Dict[str, object]:
        """Bounded corpus profile: SQL aggregates plus a small representative set.

        The database may scan/group the visible collection, but Python never
        materialises every source identity.  Exact family resolution is a
        separate bounded lookup over evidence source ids.
        """
        representative_limit = max(1, min(int(representative_limit), 64))
        distribution_limit = max(1, min(int(distribution_limit), 64))
        visible = "s.notebook_id=? AND s.source_type NOT IN ('memory','knowhow')"
        with self.database.connect() as db:
            totals = dict(db.execute(
                "SELECT COUNT(*) AS total_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND TRIM(COALESCE(m.paper_title,''))<>'' THEN 1 ELSE 0 END) AS metadata_sources,"
                "SUM(CASE WHEN COALESCE(m.is_paper,0)=1 "
                "AND m.pub_year BETWEEN 1000 AND 9999 THEN 1 ELSE 0 END) AS known_year_sources,"
                "SUM(CASE WHEN COALESCE(s.file_hash,'')='' AND NOT "
                "(COALESCE(m.is_paper,0)=1 AND TRIM(COALESCE(m.paper_title,''))<>'') "
                "THEN 1 ELSE 0 END) AS identity_uncertain_sources "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id "
                f"WHERE {visible}",
                (notebook_id,),
            ).fetchone())
            type_rows = [dict(row) for row in db.execute(
                "SELECT COALESCE(NULLIF(TRIM(s.doc_type),''),"
                "NULLIF(TRIM(s.source_type),''),'unknown') AS type,COUNT(*) AS count "
                "FROM sources s WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory','knowhow') GROUP BY 1 "
                "ORDER BY count DESC,type LIMIT ?",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            year_rows = [dict(row) for row in db.execute(
                "SELECT m.pub_year AS year,COUNT(*) AS count FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 GROUP BY m.pub_year "
                "ORDER BY m.pub_year DESC LIMIT ?",
                (notebook_id, distribution_limit + 1),
            ).fetchall()]
            duplicate_row = dict(db.execute(
                "SELECT "
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "WHERE s.notebook_id=? AND s.source_type NOT IN ('memory','knowhow') "
                "AND COALESCE(s.file_hash,'')<>'' GROUP BY s.file_hash HAVING COUNT(*)>1)),0) "
                "AS hash_duplicate_excess,"
                "COALESCE((SELECT SUM(n-1) FROM (SELECT COUNT(*) AS n FROM sources s "
                "JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory','knowhow') AND m.is_paper=1 "
                "AND TRIM(COALESCE(m.paper_title,''))<>'' "
                "GROUP BY LOWER(TRIM(m.paper_title)) HAVING COUNT(*)>1)),0) "
                "AS title_duplicate_excess",
                (notebook_id, notebook_id),
            ).fetchone())
            representatives = [dict(row) for row in db.execute(
                "WITH base AS (SELECT s.id,s.title,s.file_name,s.source_type,s.doc_type,"
                "m.paper_title,CASE WHEN m.is_paper=1 THEN m.pub_year ELSE NULL END AS pub_year,"
                "m.is_paper,s.created_at,"
                "COALESCE(NULLIF(TRIM(s.doc_type),''),"
                "NULLIF(TRIM(s.source_type),''),'unknown') AS type_key,"
                "CASE WHEN m.is_paper=1 AND m.pub_year BETWEEN 1000 AND 9999 "
                "THEN m.pub_year ELSE NULL END AS year_key FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id WHERE s.notebook_id=? "
                "AND s.source_type NOT IN ('memory','knowhow')),"
                "ranked AS (SELECT *,ROW_NUMBER() OVER (PARTITION BY type_key "
                "ORDER BY created_at,id) AS type_rank,"
                "ROW_NUMBER() OVER (PARTITION BY year_key "
                "ORDER BY created_at,id) AS year_rank FROM base) "
                "SELECT id,title,file_name,source_type,doc_type,paper_title,pub_year,is_paper "
                "FROM ranked ORDER BY CASE WHEN type_rank=1 THEN 0 WHEN year_rank=1 THEN 1 ELSE 2 END,"
                "type_key,year_key DESC,created_at,id LIMIT ?",
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
    ) -> List[Dict[str, object]]:
        ids = list(dict.fromkeys(str(value) for value in source_ids if value))[:1024]
        if not ids:
            return []
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT s.id,s.file_hash,m.paper_title,m.is_paper FROM sources s "
                "LEFT JOIN source_paper_meta m ON m.source_id=s.id "
                "AND m.notebook_id=s.notebook_id "
                f"WHERE s.id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def source_titles(self, source_ids: List[str]) -> Dict[str, str]:
        """Batch {source_id: title} lookup (Task 24): 源 chunk 引用标签补全
        (selected-source-graph activation, shared by ask_chunk/ask_reasoning)
        — SQL frozen from the engine's inline query (one IN(...) list; the
        caller dedups and the post-truncation id count stays tiny)."""
        ids = [str(s) for s in source_ids if s]
        if not ids:
            return {}
        with self.database.connect() as db:
            rows = db.execute(
                f"SELECT id, title FROM sources WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        return {row["id"]: row["title"] for row in rows}

    def notebook_element_sample(
        self, notebook_id: str, *, max_chars: int = 8000
    ) -> List[dict]:
        """Return a deterministic, character-bounded schema-induction sample.

        Read only the text columns the prompt consumes and stop paging as soon
        as the rendered ``[location] text`` budget is full.  This keeps both
        SQLite and Python work bounded for large notebooks and avoids loading
        the unrelated embedding BLOBs that the old ``_gather_elements`` join
        selected before truncating in the service.
        """
        budget = max(0, int(max_chars))
        if budget == 0:
            return []

        out: List[dict] = []
        rendered_chars = 0
        after_rowid = 0
        page_size = 32
        with self.database.connect() as db:
            while rendered_chars < budget:
                rows = db.execute(
                    """
                    SELECT e.rowid AS _rowid, e.location_label,
                           substr(e.text, 1, ?) AS text
                    FROM source_elements e
                    JOIN sources s ON s.id = e.source_id
                    WHERE s.notebook_id = ? AND e.rowid > ?
                    ORDER BY e.rowid ASC
                    LIMIT ?
                    """,
                    (budget, notebook_id, after_rowid, page_size),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    after_rowid = int(row["_rowid"])
                    location = str(row["location_label"] or "")
                    prefix = f"[{location}] "
                    separator_chars = 1 if out else 0
                    available = budget - rendered_chars - separator_chars - len(prefix)
                    if available <= 0:
                        return out
                    text = str(row["text"] or "")[:available]
                    out.append({"location_label": location, "text": text})
                    rendered_chars += separator_chars + len(prefix) + len(text)
                    if rendered_chars >= budget:
                        return out
        return out

    # ----------------------------------------------------------------- writes
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
        connection: "sqlite3.Connection | None" = None,
        capacity_limit: "int | None" = None,
    ) -> None:
        """Insert one sources row (created_at/updated_at minted via the ``now``
        seam). Pass ``connection`` to join a caller-owned write transaction —
        batch imports keep their all-or-nothing semantics; without it the row
        commits in its own write transaction.

        ``memory_id`` links a Memory-derived synthetic source (source_type=
        'memory') back to its origin Memory row; the default "" leaves
        ordinary sources out of the partial unique index
        (idx_sources_memory_id caps this at one derived source per Memory).

        ``agent_profile_id`` (v48) records that an Agent — not a person — added
        this source. The default "" is stored as SQL NULL, not as "": NULL is
        the value every pre-v48 row already carries and the one the delete
        permission check reads as "user-added, an Agent may never remove it",
        so a caller that omits the argument lands on exactly the same row shape
        as the whole existing corpus.

        ``capacity_limit`` (URL imports; None = exempt) enforces the notebook's
        visible-document ceiling ATOMICALLY with this insert: COUNT and INSERT
        run in one write transaction under BEGIN IMMEDIATE, so two concurrent
        capacity-checked creators cannot both snapshot "one slot left" and both
        land (the check-then-insert race the API pre-flight alone cannot
        close). Over the ceiling raises ``DocumentCapacityExceeded`` and
        inserts nothing. Only the own-transaction shape is supported: a joined
        ``connection`` gives this method no say over when the transaction took
        its write lock, so the count could predate it — refuse loudly rather
        than enforce a limit that does not actually hold."""
        if capacity_limit is not None and connection is not None:
            raise ValueError(
                "capacity_limit requires an owned write transaction; "
                "it cannot join a caller-provided connection"
            )
        now = self.now()
        statement = (
            """
            INSERT INTO sources
            (id, notebook_id, title, source_type, status, parse_status, file_name,
             file_path, source_url, file_size, file_hash, summary, doc_type,
             memory_id, agent_profile_id, created_at, updated_at, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        values = (
            source_id, notebook_id, title, source_type, status, parse_status,
            file_name, file_path, source_url, file_size, file_hash, summary,
            # Whitespace-only is the same statement as "": strip before the NULL
            # fold, or a caller that passes "   " lands a non-NULL row that reads
            # as "an Agent added this" while naming no agent at all. Both backends
            # normalize with this exact expression.
            doc_type, memory_id, (agent_profile_id or "").strip() or None, now, now,
            ((self.current_user_id() or "").strip() or None)
            if source_type not in {"memory", "knowhow"}
            else None,
        )
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as db:
            if capacity_limit is not None:
                # BEGIN IMMEDIATE before the COUNT: the RESERVED lock (plus the
                # process-global write lock) makes count-then-insert one atomic
                # step, and the fresh write connection reads the latest
                # committed state — a concurrent creator's row is in the count.
                self.database.begin_immediate(db)
                current = self._visible_document_count_on(db, notebook_id)
                if current >= capacity_limit:
                    raise DocumentCapacityExceeded(current, capacity_limit)
            db.execute(statement, values)

    def source_id_by_hash(self, notebook_id: str, digest: str) -> Optional[str]:
        """Existing source in this notebook whose content hash matches
        ``digest``, if any (Task 7: backs ``upload_sources``' same-notebook
        dedup short-circuit). One-hop query over ``idx_sources_notebook_
        file_hash`` (migration 24). Sole owner of this lookup:
        ``maintenance.source_id_by_hash`` (the offline ``batch_ingest``
        composition root's ``already_ingested`` check) delegates here, so the
        CLI and the UI can never dedup by different rules.

        Empty ``digest`` never matches: URL sources and metadata-only imports
        store ``file_hash=''``, and a bare equality lookup would otherwise
        report an unrelated source instead of "no match" (same "" sentinel
        hazard as ``source_id_for_memory``).

        ``ORDER BY created_at, id`` is load-bearing, not decoration: databases
        deployed before migration 24 can already hold several rows with the
        same hash (that is the mess this dedup exists to stop growing), and
        without it SQLite may return either one — so the same re-upload could
        land on a different source between calls. Oldest row wins: it is the
        one existing notes/citations already point at.

        Scoped to ``notebook_id`` on purpose: cross-notebook content is NEVER
        matched here. A user re-uploading the same file into a different
        notebook usually means they want it there too, and sharing one
        ``sources`` row across notebooks would tangle ownership/ACLs and
        delete cascades — see test_upload_dedup.py's cross-notebook case.

        ``source_type NOT IN ('memory', 'knowhow')`` — the same hidden-synthetic
        exclusion list ``list_sources`` uses — is a correctness guard, not
        housekeeping. Memory-derived rows carry a NON-empty ``file_hash``
        (sha256 of ``title + "\\n" + content``), so a file whose bytes happen to
        equal that exact text would otherwise be "deduped" onto a hidden row:
        the upload would report success, briefly show a source the visible list
        then drops, and quietly graft the user's file onto a Memory projection.
        Excluding the known hidden types rather than allow-listing the importable
        ones is deliberate: upload's ``source_type`` comes from
        ``source_type_from_name`` and grows with every new supported format, so
        an allow-list would silently stop deduping the next format added.
        ⚠ A NEW hidden/derived source_type must be added here too (grep
        ``NOT IN ('memory', 'knowhow')`` for the sibling sites)."""
        with self.database.connect() as db:
            return self._source_id_by_hash_on(db, notebook_id, digest)

    @staticmethod
    def _source_id_by_hash_on(
        db: sqlite3.Connection, notebook_id: str, digest: str
    ) -> Optional[str]:
        """The dedup SELECT on a caller-provided connection — shared by the read
        path (``source_id_by_hash``) and the atomic write path
        (``insert_source_if_absent``) so the two can never dedup by different
        rules. Empty ``digest`` never matches (URL / metadata-only rows store
        ``file_hash=''``); the memory/knowhow exclusion and the ``ORDER BY
        created_at, id`` are the load-bearing guards documented on
        ``source_id_by_hash`` — kept here as the single implementation."""
        if not digest:
            return None
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND file_hash=? "
            "AND source_type NOT IN ('memory', 'knowhow') "
            "ORDER BY created_at, id",
            (notebook_id, digest),
        ).fetchone()
        return row["id"] if row else None

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
    ) -> Optional[str]:
        """Atomic content-dedup insert: if a same-content VISIBLE source already
        exists in this notebook return its id (the caller reuses it, no row is
        created); otherwise insert the new row (``file_hash=digest``) and return
        None.

        ``capacity_limit`` (None = exempt) additionally enforces the notebook's
        visible-document ceiling inside the SAME ``BEGIN IMMEDIATE`` write
        transaction: after the dedup re-check misses, the visible-document
        COUNT runs on this very connection and an at-or-over-limit notebook
        raises ``DocumentCapacityExceeded`` instead of inserting — closing the
        check-then-insert window in which two concurrent uploads both saw one
        free slot (PR #584 codex R6). Order matters and is deliberate: the
        dedup re-check runs FIRST, so re-uploading existing bytes into a full
        notebook still reuses the row (a reuse adds no document and must never
        be refused by the ceiling).

        ``agent_profile_id`` is written ONLY on the insert branch. Reusing an
        existing row must never restamp its provenance: a user-added source
        that an Agent later re-uploads stays user-added, which is what keeps it
        undeletable through the Agent surface.

        upload_sources' former "``source_id_by_hash`` (read) then
        ``insert_source`` (write)" was two steps: two concurrent FIRST uploads of
        identical bytes both saw "no match" and each inserted, and migration
        24/25's ``(notebook_id, file_hash)`` index is NON-unique, so nothing
        stopped the duplicate (Codex round 4 P2). Folding the re-check and the
        insert into ONE write transaction closes it. ``BEGIN IMMEDIATE`` takes
        SQLite's RESERVED lock as the first statement, making the
        re-check-then-insert atomic even against a SECOND OS process — the
        offline ``batch_ingest`` CLI shares this product DB in real deployments,
        the identical cross-process reasoning as ``KnowhowTransferStore.
        delete_table_if_unchanged`` / ``snapshot_table``; the process-global
        write lock covers same-process concurrency. The re-check reuses the SAME
        dedup SELECT as ``source_id_by_hash`` (``_source_id_by_hash_on``) and the
        insert reuses ``insert_source`` (one row-shape owner).

        A UNIQUE index is DELIBERATELY not used instead: already-deployed DBs can
        ALREADY hold duplicate ``(notebook_id, file_hash)`` rows — the very mess
        this feature exists to stop growing — so a UNIQUE migration would fail to
        apply on exactly those DBs. The atomicity lives in the transaction, not a
        constraint."""
        with self.database.write() as db:
            self.database.begin_immediate(db)
            existing = self._source_id_by_hash_on(db, notebook_id, digest)
            if existing is not None:
                return existing
            if capacity_limit is not None:
                current = self._visible_document_count_on(db, notebook_id)
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
                connection=db,
            )
            return None

    def source_id_for_memory(self, memory_id: str) -> Optional[str]:
        """Id of the synthetic source derived from a Memory, if any (at most
        one per the idx_sources_memory_id partial unique index). Guards the
        "" sentinel explicitly — ordinary (non-memory) sources all default
        their memory_id column to "", so a bare equality lookup would
        otherwise match an unrelated source instead of reporting no link."""
        if not memory_id:
            return None
        with self.database.connect() as db:
            row = db.execute(
                "SELECT id FROM sources WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        return str(row["id"]) if row else None

    def set_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: "str | None" = None,
        error_message: str = "",
    ) -> None:
        fields = ["status = ?", "parse_status = ?", "error_message = ?", "updated_at = ?"]
        params: List[object] = [status, status, error_message, self.now()]
        if summary is not None:
            fields.insert(2, "summary = ?")
            params.insert(2, summary)
        with self.database.write() as db:
            db.execute(
                f"UPDATE sources SET {', '.join(fields)} WHERE id = ?",
                (*params, source_id),
            )


    def mark_indexing_chunk_fallback(self, source_id: str, warning_code: str) -> None:
        """见 SourceStorePort:非空写稳定前缀诊断(不覆盖既有 MinerU 诊断),空只清自己。"""
        prefix = INDEXING_CHUNK_FALLBACK_WARNING_PREFIX
        with self.database.write() as db:
            if warning_code:
                db.execute(
                    "UPDATE sources SET error_message=? WHERE id=? AND "
                    "(error_message IS NULL OR error_message='' OR error_message LIKE ?)",
                    (f"{prefix} {warning_code}", source_id, f"{prefix}%"),
                )
            else:
                db.execute(
                    "UPDATE sources SET error_message='' WHERE id=? "
                    "AND error_message LIKE ?",
                    (source_id, f"{prefix}%"),
                )

    def clear_chunked_at(
        self, connection: sqlite3.Connection, source_id: str
    ) -> None:
        """把 ``chunked_at`` 归零(NULL),**在调用方的写事务内**(镜像
        ``replace_elements`` 的 connection 约定)——写新代 elements 时调用,新代
        elements 落库即令旧分块完成标记失效,无崩溃窗口。**刻意就地一条、不折进
        ``clear_source_extraction_state``**:后者也被 KG 抽取的
        ``begin_extraction_run`` 复用,而抽取发生在分块之后,折进去会把分块刚置好
        的 ``chunked_at`` 又清掉。"""
        connection.execute(
            "UPDATE sources SET chunked_at = NULL WHERE id = ?", (source_id,)
        )

    def claim_failed_for_retry(self, source_id: str) -> bool:
        """Atomically flip a FAILED source to 'queued' to claim a retry, returning
        whether THIS caller won the claim.

        Backs the upload path's failed-source retry (SourceIngestionService.
        reuse_uploaded_source): two concurrent re-uploads of the same failed row
        both read parse_status=='failed' before either writes, and would both
        schedule process_source on the one row — two pipelines that
        replace_elements / clear each other's extraction state and KG products,
        plus a wasted parse/LLM pass. The guarded UPDATE closes it: a single
        conditional statement is atomic under the process-global write lock, so of
        two concurrent callers exactly one sees rowcount==1 (it flipped
        failed->queued and schedules); the loser's WHERE finds parse_status is no
        longer 'failed' -> rowcount 0 -> False (it must NOT reschedule; the winner
        already owns the rerun). Clears error_message like set_status('queued')
        did: last round's failure no longer describes the row. Mirrors
        kg_build_job_store's rowcount claims; no BEGIN IMMEDIATE needed — one
        WHERE-guarded UPDATE is atomic on its own."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE sources SET parse_status='queued', status='queued', "
                "error_message='', updated_at=? "
                "WHERE id=? AND parse_status='failed'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def claim_reextract_if_extracted(self, source_id: str) -> bool:
        """Atomically flip a SETTLED 'extracted' source to 'extracting' to claim a
        doc-type re-extraction, returning whether THIS caller won.

        Backs reuse_uploaded_source's retype branch. Reading parse_status and THEN
        deciding to schedule is a TOCTOU: an in-flight pipeline can finalize
        'extracted' between reuse_uploaded_source's top-of-method read and the
        branch, so a retype that saw 'extracting' would decline to schedule while
        the pipeline (which already read the OLD doc_type) finalizes with it —
        and nobody reconciles the new type. The conditional flip closes it: won
        (row was 'extracted') -> this caller owns the re-extract with the new
        type; lost (still queued/parsing/extracting, or failed) -> False, do NOT
        schedule, because the running pipeline's terminal doc_type recheck
        (mark_extracted_if_doc_type) re-extracts with the new type and a second
        pipeline on one row clears the first's KG products. Single WHERE-guarded
        UPDATE, atomic under the write lock; clears error_message like
        set_status('extracting') did."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE sources SET parse_status='extracting', status='extracting', "
                "error_message='', updated_at=? "
                "WHERE id=? AND parse_status='extracted'",
                (self.now(), source_id),
            )
        return cursor.rowcount == 1

    def mark_extracted_if_doc_type(
        self, source_id: str, expected_doc_type: str, *, error_message: str = ""
    ) -> bool:
        """Atomically mark a source 'extracted' IFF its stored doc_type still
        equals ``expected_doc_type`` (what the just-finished extraction used),
        returning whether the terminal transition landed.

        Backs SourceIngestionService._extract_reconciling_doc_type. doc_type is
        read at the START of run_extraction (it selects the extraction profile and
        rides the prompt, hence the LLM cache key); a concurrent retype landing
        between the reconcile loop's final doc_type read and the 'extracted' mark
        used to leave the stored type disagreeing with the profile actually used,
        with nothing to correct it (reuse_uploaded_source declines to schedule on
        an 'extracting' row). Folding the recheck INTO the terminal write — one
        WHERE-guarded UPDATE, atomic under the write lock — removes that window:
        rowcount 1 -> finalized consistently; rowcount 0 -> the type changed under
        us, the caller re-extracts with the new type (bounded rounds) instead of
        finalizing an inconsistent source. ``expected_doc_type`` is already
        normalized by the caller, and every writer stores a normalized doc_type,
        so the raw column == expected comparison is exact. Does not touch summary
        (set at 'parsed'); error_message mirrors set_status('extracted', ...)."""
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE sources SET status='extracted', parse_status='extracted', "
                "error_message=?, updated_at=? "
                "WHERE id=? AND doc_type=?",
                (error_message, self.now(), source_id, expected_doc_type),
            )
        return cursor.rowcount == 1

    def update_file_hash(
        self,
        source_id: str,
        file_hash: str,
        *,
        title: "str | None" = None,
        connection: "sqlite3.Connection | None" = None,
    ) -> None:
        """Persist a recomputed fingerprint (Memory-derived source reparse:
        insert_source already carries the fingerprint for a brand-new row;
        this is the update half for an existing row whose content changed —
        and the failure path's clear-to-'' so a broken row is never
        fingerprint-skipped into on retry).

        ``title``: the memory fingerprint covers sha256(title+content), so a
        title change re-lands here too — pass it to refresh sources.title in
        the same UPDATE (None leaves the stored title untouched).
        Pass ``connection`` to ride the caller's write transaction (the
        memory reparse path folds this into the same commit as
        clear_source_extraction_state + replace_elements)."""
        fields = ["file_hash = ?", "updated_at = ?"]
        params: List[object] = [file_hash, self.now()]
        if title is not None:
            fields.insert(0, "title = ?")
            params.insert(0, title)
        statement = f"UPDATE sources SET {', '.join(fields)} WHERE id = ?"
        values = (*params, source_id)
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as db:
            db.execute(statement, values)

    def set_doc_type(self, source_id: str, doc_type: str) -> None:
        """Persist a corrected extraction-profile id on ONE source row.

        Written for the upload path's "same file, different doc_type" case:
        content dedup hands the existing row back, but the doc_type the user
        just picked is a real correction — it selects the extraction profile
        and rides the extraction prompt (hence the LLM cache key), so dropping
        it silently would leave the source extracted under the wrong profile
        forever (see SourceIngestionService.reuse_uploaded_source).

        The caller passes an ALREADY normalized value (_normalize_doc_type:
        known profile id, or '' for auto-detect) — this method is storage, it
        does not re-judge the vocabulary. Deliberately NOT reusing
        maintenance.set_sources_doc_type: that one rewrites every source of a
        notebook (offline seeding), which is the opposite blast radius."""
        with self.database.write() as db:
            db.execute(
                "UPDATE sources SET doc_type = ?, updated_at = ? WHERE id = ?",
                (doc_type, self.now(), source_id),
            )

    def replace_elements(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None:
        """Swap a source's elements INSIDE the caller's write transaction (the
        parse pipeline clears extraction state in the same transaction).

        The source's ``updated_at`` is advanced to the SAME instant the new
        elements carry, in this same transaction.  That is what makes the
        change signal (see ``source_change_signal_rows``) flip atomically with
        the element generation, so a count cached against the previous
        generation becomes unreachable the moment the new one commits.
        Without it a FIRST parse landed elements while every component of the
        signal stayed put — ``chunked_at`` was already NULL so nulling it was a
        no-op — and the signal only moved at the next ``set_status``, an LLM
        call later.  Anything counting in that window (the collection map, and
        therefore the enumeration plan built from it) read the source as empty.

        It is a bump the source was going to receive anyway: every lifecycle
        transition writes ``updated_at``, and the parse always ends in one.
        This only moves it earlier, to the instant the data actually changed.
        ``sources.updated_at`` is written by the status/hash/doc-type paths and
        read in exactly one place — the change-signal query — so nothing
        orders, displays or keys on it (source listings order by
        ``created_at``, and the API model does not carry the column).
        """
        connection.execute(
            "DELETE FROM source_elements WHERE source_id = ?", (source_id,)
        )
        connection.executemany(
            """
            INSERT INTO source_elements
            (id, source_id, element_type, location_label, text, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    element.id,
                    source_id,
                    element.element_type,
                    element.location_label,
                    element.text,
                    json.dumps(dict(element.metadata)),
                    created_at,
                )
                for element in elements
            ],
        )
        # ``created_at`` is the caller's own ``now()`` (microsecond precision,
        # the repository-wide convention), so the signal moves to a value that
        # belongs to THIS element generation rather than to a second clock read.
        connection.execute(
            "UPDATE sources SET updated_at = ? WHERE id = ?",
            (created_at, source_id),
        )

    def delete_source_row(
        self, connection: sqlite3.Connection, source_id: str
    ) -> None:
        connection.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # ------------------------------------------------- knowhow projection
    # (Task 5, knowhow-tables PR-1): the deterministic projector writes one
    # source_elements row per non-empty cell of a knowhow-table row, tagged
    # with metadata.knowhow.row_id — these two primitives are ROW-scoped
    # (unlike replace_elements above, which wipes an entire source), so
    # reprojecting one row never disturbs sibling rows' elements sharing the
    # same hidden source.
    def insert_elements(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None:
        """Insert-only half of ``replace_elements`` (no delete-all first) —
        the projector does its own row-scoped delete via
        ``delete_elements_by_knowhow_row`` beforehand."""
        connection.executemany(
            """
            INSERT INTO source_elements
            (id, source_id, element_type, location_label, text, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    element.id,
                    source_id,
                    element.element_type,
                    element.location_label,
                    element.text,
                    json.dumps(dict(element.metadata), ensure_ascii=False),
                    created_at,
                )
                for element in elements
            ],
        )

    def delete_elements_by_knowhow_row(
        self, connection: sqlite3.Connection, source_id: str, row_id: str
    ) -> None:
        """Delete this row's PRIOR knowhow-cell elements (any column), keyed
        by ``metadata.knowhow.row_id`` — not by id prefix, since element ids
        are ``el-kh-{hash(row_id, column_id)}`` and carry no shared per-row
        substring. Row-scoped (not source-scoped) so it never touches
        sibling rows' elements under the same hidden source. json_extract on
        an un-indexed TEXT column is a per-source_id scan, acceptable at this
        feature's bounded scale (single knowhow table, ~100s of elements)."""
        connection.execute(
            "DELETE FROM source_elements WHERE source_id = ? "
            "AND json_extract(metadata, '$.knowhow.row_id') = ?",
            (source_id, row_id),
        )

    # -------------------------------------------------------------- hydration
    def source_from_row(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        paper_meta: Optional[dict] = _UNSET,  # type: ignore[assignment]
    ) -> SourceSummary:
        """Single-row path (get_source) — 4 point queries (source_elements
        COUNT, knowledge_objects EXISTS, extraction_runs latest
        error_message, paper-meta hydration). list_sources / list_sources_page
        use the batched sources_from_rows sibling instead (C5: was 3 queries *
        N rows per page — now 3 total per page; the paper-meta lookup landed
        later as the 4th).

        ``paper_meta``: get_source already fetches this dict for
        SourceDetail.paper_meta, so it passes the SAME dict here instead of
        letting this method fetch its own copy of the same source_id — the
        ``_UNSET`` sentinel default (as opposed to a plain ``None``, which
        means "already fetched, no meta row exists") is what makes every
        other caller still fetch it here."""
        element_count = int(db.execute(
            "SELECT COUNT(*) AS count FROM source_elements WHERE source_id = ?",
            (row["id"],),
        ).fetchone()["count"])
        # 三个 run 子查询原本相关到 ``ko.source_id`` 并写在 EXISTS 内部,于是每扫到
        # 一行 knowledge_objects 就重算一遍;而它们只依赖 source_id,对这次调用是
        # **常量**。提到 EXISTS 外面直接绑定 ? 之后:KO 存在性是一次半连接
        # (idx_knowledge_objects_source,首行即停),run 判定是 ≤3 次索引探针
        # (idx_extraction_runs_source_created)。``ko.source_id != ''`` 保留在 EXISTS
        # 内:等值连接把它传递到绑定参数上,与原查询逐字等价。PG 侧同名方法同构。
        kg_extracted = bool(db.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM knowledge_objects ko "
            "  WHERE ko.source_id = ? AND ko.source_id != ''"
            ")"
            "  AND COALESCE(("
            "    SELECT er.status FROM extraction_runs er "
            "    WHERE er.source_id=? AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
            "  ), 'completed')='completed'"
            "  AND COALESCE((SELECT er.error_message FROM extraction_runs er "
            "    WHERE er.source_id=? AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
            "    NOT GLOB '*windows_failed=[1-9]*/*'"
            "  AND instr(COALESCE((SELECT er.error_message FROM extraction_runs er "
            "    WHERE er.source_id=? AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),''),"
            "    'retry_incomplete=1')=0",
            (row["id"], row["id"], row["id"], row["id"]),
        ).fetchone()[0])
        pm = (
            self.paper_meta_for_sources(db, [row["id"]]).get(row["id"])
            if paper_meta is _UNSET else paper_meta
        )
        # 一次读取喂两个派生字段(告警 + 「已分析但零产出」),不是两次:两者的输入
        # 就是同一行「最近一次抽取记录」。query 数因此与改动前一致。
        latest_run_status, latest_run_error = self.latest_run_state(db, row["id"])
        summary = SourceSummary(
            id=row["id"],
            notebook_id=row["notebook_id"],
            title=row["title"],
            display_title=summary_display_title(row, pm),
            type=row["source_type"],
            status=row["status"],
            summary=row["summary"],
            element_count=element_count,
            file_name=row["file_name"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parse_status=row["parse_status"],
            created_at=row["created_at"] or "",
            created_label=_created_label(row["created_at"]),
            doc_type=row["doc_type"] if "doc_type" in row.keys() else "",
            source_url=row["source_url"] if "source_url" in row.keys() else "",
            # v48 provenance, derived from "the column is not NULL" — the raw
            # agent id stays server-side. Guarded like doc_type/source_url
            # above so a caller-supplied narrow row still projects.
            agent_created=(
                row["agent_profile_id"] is not None
                if "agent_profile_id" in row.keys() else False
            ),
            extraction_warning=extraction_warning_text(latest_run_error),
            parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
            kg_extracted=kg_extracted,
            kg_analyzed_empty=kg_analyzed_without_objects(
                latest_run_status, latest_run_error
            ),
            authors=[a["name"] for a in pm["authors"]] if pm else [],
            pub_year=pm["pub_year"] if pm else None,
            venue=pm["venue"] if pm else None,
        )
        summary.paper_meta_status = self._paper_meta_status_for(row, pm)
        return summary

    def sources_from_rows(self, db: sqlite3.Connection, rows: List[sqlite3.Row]) -> List[SourceSummary]:
        """Batched sibling of source_from_row for a PAGE of source rows (house
        pattern, see _hydrate_search_hits): the 4 per-row lookups
        (source_elements COUNT, latest extraction_runs error_message,
        kg_extracted 判定, paper-meta hydration) each become ONE query for the
        whole page instead of one query per row — was 4*N round-trips.

        三条是 `source_id IN (...)`;kg_extracted 那条不是,它的驱动集是页内的
        source id 本身,用 `WITH page_sources(source_id) AS (VALUES ...)` 承载
        (理由与等价论证见循环体内那处注释)。

        extraction_warning tie-break equivalence: when two extraction_runs
        rows for the same source share `created_at` (rare but possible at
        second-granularity timestamps), a per-row "ORDER BY created_at DESC
        LIMIT 1" and this batched "ORDER BY source_id, created_at DESC" over
        the SAME idx_extraction_runs_source_created index resolve the tie
        identically — both walk the same physical index order, so the first
        row seen per source_id in the batched scan is the same row LIMIT 1
        would have picked per-id (verified: both orderings are driven by the
        same btree, whose tie order is deterministic and independent of
        whether the WHERE clause scopes one id or many via IN)."""
        if not rows:
            return []
        source_ids = [r["id"] for r in rows]
        paper_meta = self.paper_meta_for_sources(db, source_ids)
        element_counts: Dict[str, int] = {}
        kg_extracted_ids: set = set()
        latest_error: Dict[str, str] = {}
        latest_status: Dict[str, str] = {}
        for i in range(0, len(source_ids), self.IN_CHUNK):
            batch = source_ids[i:i + self.IN_CHUNK]
            ph = ",".join("?" for _ in batch)
            for r in db.execute(
                f"SELECT source_id, COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({ph}) GROUP BY source_id", batch,
            ).fetchall():
                element_counts[r["source_id"]] = int(r["c"])
            # kg_extracted 的驱动集是**页内的 source id**,不是 knowledge_objects 行。
            #
            # 原查询 ``SELECT DISTINCT ko.source_id FROM knowledge_objects ko WHERE ...``
            # 把三个 run 子查询挂在每一行 KO 上;它们只依赖 ``ko.source_id``,对固定
            # source 是常量,而 DISTINCT 又把结果折回 source 粒度——「每行算一次」纯属
            # 浪费。PG 侧现场数据:4.9 万 source 的 notebook 取一页 50 条、页内共 33.9 万
            # KO 行,这一条查询单次 3650ms、101 万次子查询执行。
            #
            # 改写后每个 source 至多 3 次 run 索引探针(idx_extraction_runs_source_created)
            # 加 1 次 KO 半连接(idx_knowledge_objects_source,首行即停)。
            # ``ko.source_id != ''`` 留在 EXISTS 内:等值连接把它传递到
            # page_sources.source_id 上,与原查询逐字等价。改写不依赖「每个 source 只有
            # 一条 run」:latest-run 的取法(同一条 ORDER BY + LIMIT 1)一字未动。
            #
            # CTE 用 ``WITH x(col) AS (VALUES ...)``——两端都支持的唯一写法,PG 侧同名
            # 方法用的是同一个形状(那边不用 LATERAL,正是为了保住这份同构)。
            page_values = ",".join("(?)" for _ in batch)
            for r in db.execute(
                f"WITH page_sources(source_id) AS (VALUES {page_values}) "
                "SELECT source_id FROM page_sources "
                "WHERE EXISTS("
                "  SELECT 1 FROM knowledge_objects ko "
                "  WHERE ko.source_id=page_sources.source_id AND ko.source_id != ''"
                ") "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=page_sources.source_id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
                "), 'completed')='completed' "
                "AND COALESCE((SELECT er.error_message FROM extraction_runs er "
                " WHERE er.source_id=page_sources.source_id AND er.run_type='kg' "
                " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
                " NOT GLOB '*windows_failed=[1-9]*/*' "
                "AND instr(COALESCE((SELECT er.error_message FROM extraction_runs er "
                " WHERE er.source_id=page_sources.source_id AND er.run_type='kg' "
                " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),''),"
                " 'retry_incomplete=1')=0",
                batch,
            ).fetchall():
                kg_extracted_ids.add(r["source_id"])
            for r in db.execute(
                f"SELECT source_id, status, error_message FROM extraction_runs "
                f"WHERE source_id IN ({ph}) ORDER BY source_id, created_at DESC",
                batch,
            ).fetchall():
                # 同一行的两列一起 setdefault:哪一行算「最近一次」由上面的 ORDER BY
                # 决定(tie-break 等价性见本方法 docstring),status 只是搭同一趟车,
                # 不新增查询、也不改变选中的是哪一行。
                if r["source_id"] in latest_error:
                    continue
                latest_error[r["source_id"]] = r["error_message"] or ""
                latest_status[r["source_id"]] = r["status"] or ""

        def _warning(source_id: str) -> Optional[str]:
            # 派生规则只有一份(app.models.sources),这里只负责取到「最近一次抽取的
            # error_message」这个输入——见该函数 docstring。
            return extraction_warning_text(latest_error.get(source_id))

        out: List[SourceSummary] = []
        for row in rows:
            sid = row["id"]
            pm = paper_meta.get(sid)
            summary = SourceSummary(
                id=sid,
                notebook_id=row["notebook_id"],
                title=row["title"],
                display_title=summary_display_title(row, pm),
                type=row["source_type"],
                status=row["status"],
                summary=row["summary"],
                element_count=element_counts.get(sid, 0),
                file_name=row["file_name"],
                file_size=row["file_size"],
                file_hash=row["file_hash"],
                parse_status=row["parse_status"],
                created_at=row["created_at"] or "",
                created_label=_created_label(row["created_at"]),
                doc_type=row["doc_type"] if "doc_type" in row.keys() else "",
                source_url=row["source_url"] if "source_url" in row.keys() else "",
                # v48 provenance, derived from "the column is not NULL" — the raw
                # agent id stays server-side. Guarded like doc_type/source_url
                # above so a caller-supplied narrow row still projects.
                agent_created=(
                    row["agent_profile_id"] is not None
                    if "agent_profile_id" in row.keys() else False
                ),
                extraction_warning=_warning(sid),
                parse_quality_warning=has_pdf_python_fallback_warning(row["error_message"]),
            indexing_chunk_fallback=has_indexing_chunk_fallback_warning(row["error_message"]),
                kg_extracted=sid in kg_extracted_ids,
                kg_analyzed_empty=kg_analyzed_without_objects(
                    latest_status.get(sid, ""), latest_error.get(sid, "")
                ),
                authors=[a["name"] for a in pm["authors"]] if pm else [],
                pub_year=pm["pub_year"] if pm else None,
                venue=pm["venue"] if pm else None,
            )
            summary.paper_meta_status = self._paper_meta_status_for(row, pm)
            out.append(summary)
        return out

    def extraction_warning(self, db: sqlite3.Connection, source_id: str) -> Optional[str]:
        """Surface a user-facing warning when the latest KG extraction left
        network-failed windows (degraded run). Parsed from the run's
        `windows_failed=N/T` token rather than stored on the source row —
        the parsing itself lives in `app.models.sources.extraction_warning_text`
        (single implementation); this method only fetches its input."""
        return extraction_warning_text(self.latest_run_state(db, source_id)[1])

    @staticmethod
    def latest_run_state(
        db: sqlite3.Connection, source_id: str
    ) -> "tuple[str, str]":
        """最近一次抽取记录的 ``(status, error_message)``,两个派生字段的共同输入。

        抽出来是因为 ``extraction_warning`` 与 ``kg_analyzed_empty`` 问的是**同一行**:
        各读一次就是把 get_source 的点查从 4 条变成 5 条,而它们的答案本来就来自同一次
        读取。返回值一律是字符串(无记录 → ``("", "")``),让两个派生谓词都不必再判 None。

        刻意不加 ``run_type='kg'`` 过滤:``extraction_runs`` 只有一个写者、且写死
        ``'kg'``,而批量兄弟 ``sources_from_rows`` 的 tie-break 等价性论证(见其
        docstring)建立在两侧走同一条索引、同一份 WHERE 之上——只在一侧加过滤会让
        那段论证失效。将来真出现第二种 run_type 时,两处必须一起加。"""
        run = db.execute(
            "SELECT status, error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
        if run is None:
            return ("", "")
        return (str(run["status"] or ""), str(run["error_message"] or ""))

    @staticmethod
    def meta_source_rows(
        db: sqlite3.Connection, notebook_id: str, pending_source_id: str = ""
    ) -> List[dict]:
        """Title/doc_type/summary rows feeding notebook metadata augmentation
        (Task 26: moved verbatim from the facade's `_notebook_meta_sources`).
        Excludes source_type IN ('memory', 'knowhow') so a hidden Memory- or
        knowhow-projection-derived source never contributes its title or
        inflates the count baked into the auto-generated notebook
        name/description."""
        rows = db.execute(
            "SELECT title, doc_type, summary FROM sources WHERE notebook_id = ? "
            "AND source_type NOT IN ('memory', 'knowhow') "
            "AND (status = 'extracted' OR id = ?) "
            "ORDER BY created_at ASC",
            (notebook_id, pending_source_id),
        ).fetchall()
        return [
            {"title": r["title"], "doc_type": r["doc_type"], "summary": r["summary"]}
            for r in rows
        ]

    def meta_sources(
        self, notebook_id: str, pending_source_id: str = ""
    ) -> List[dict]:
        with self.database.connect() as db:
            return self.meta_source_rows(db, notebook_id, pending_source_id)

    # ------------------------------------------------------- paper metadata
    def upsert_paper_meta(self, source_id: str, notebook_id: str, meta: dict) -> None:
        """写入/覆盖论文元数据(单写事务):source_paper_meta upsert + source_authors
        整组 delete+insert。meta 形状 = paper_meta.verify_paper_meta 的返回(已接地
        校验);行存在即「已尝试」,is_paper=0 是「已判定非论文」标记(幂等防重调 LLM)。
        作者行 id 取 source_id 限定的确定性复合键(重抽稳定,无碰撞面)。"""
        now = self.now()
        with self.database.write() as db:
            db.execute(
                """
                INSERT INTO source_paper_meta
                  (source_id, notebook_id, is_paper, paper_title, venue, pub_year,
                   doi, keywords, raw_json, model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                  is_paper=excluded.is_paper, paper_title=excluded.paper_title,
                  venue=excluded.venue, pub_year=excluded.pub_year, doi=excluded.doi,
                  keywords=excluded.keywords, raw_json=excluded.raw_json,
                  model=excluded.model, updated_at=excluded.updated_at
                """,
                (
                    source_id, notebook_id, 1 if meta.get("is_paper") else 0,
                    meta.get("paper_title"), meta.get("venue"), meta.get("pub_year"),
                    meta.get("doi"),
                    json.dumps(meta.get("keywords") or [], ensure_ascii=False),
                    str(meta.get("raw_json") or "{}"), str(meta.get("model") or ""),
                    now, now,
                ),
            )
            db.execute("DELETE FROM source_authors WHERE source_id = ?", (source_id,))
            for author in meta.get("authors") or []:
                position = int(author.get("position", 0))
                db.execute(
                    "INSERT INTO source_authors "
                    "(id, source_id, notebook_id, position, name, affiliation, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{source_id}:auth:{position:03d}", source_id, notebook_id,
                        position, str(author.get("name") or "").strip(),
                        str(author.get("affiliation") or "").strip(), now,
                    ),
                )

    def clear_paper_meta(self, source_id: str) -> None:
        """删除某源的论文元数据:source_paper_meta 标记行 + source_authors 作者行,
        单写事务。是 upsert_paper_meta 的反向(它写这两张表)。

        用途:一条源被**显式**改成非论文类型(如 academic→textbook)后,元数据抽取
        gate 会跳过它、不再刷新,但旧的论文标题/作者若不清,SourceStore 会继续把它
        当论文展示/搜索/计数——所以 retype 到不合格类型时清掉(见
        SourceIngestionService.reuse_uploaded_source)。幂等:无行时静默 no-op。"""
        with self.database.write() as db:
            db.execute("DELETE FROM source_paper_meta WHERE source_id = ?", (source_id,))
            db.execute("DELETE FROM source_authors WHERE source_id = ?", (source_id,))

    @staticmethod
    def _paper_meta_dict(row: sqlite3.Row, authors: List[sqlite3.Row]) -> dict:
        return {
            "source_id": row["source_id"],
            "is_paper": bool(row["is_paper"]),
            "paper_title": row["paper_title"],
            "venue": row["venue"],
            "pub_year": row["pub_year"],
            "doi": row["doi"],
            "keywords": json.loads(row["keywords"] or "[]"),
            "model": row["model"],
            "authors": [
                {"position": a["position"], "name": a["name"],
                 "affiliation": a["affiliation"]}
                for a in authors
            ],
        }

    @staticmethod
    def _paper_meta_status_for(row: sqlite3.Row, meta: Optional[dict]) -> Optional[str]:
        """从 sources 行 + 可选 meta 字典派生四态。零 DB 访问(调用方已经通过
        paper_meta_for_sources 拿到了 meta,这里只做分类,不再查库)。

        分类规则本身只有一份实现(`app.models.sources.paper_meta_status`);这里只
        负责把 sqlite3.Row 的可选列与「有没有 meta 行」翻译成它的入参。"""
        return paper_meta_status(
            None if meta is None else bool(meta.get("is_paper")),
            row["source_type"] if "source_type" in row.keys() else "",
            row["doc_type"] if "doc_type" in row.keys() else "",
            row["parse_status"] if "parse_status" in row.keys() else "",
        )

    @staticmethod
    def paper_meta_model(meta: Optional[dict]) -> Optional[PaperMeta]:
        """store dict → API 模型(SourceDetail.paper_meta)。标记行(is_paper=0)也
        返回对象(is_paper False),前端按 is_paper 门控显示。"""
        if meta is None:
            return None
        return PaperMeta(
            is_paper=meta["is_paper"], title=meta["paper_title"],
            venue=meta["venue"], year=meta["pub_year"], doi=meta["doi"],
            keywords=list(meta["keywords"]),
            authors=[
                PaperAuthor(name=a["name"], affiliation=a["affiliation"])
                for a in meta["authors"]
            ],
        )

    def get_paper_meta(self, source_id: str) -> Optional[dict]:
        with self.database.connect() as db:
            return self.paper_meta_for_sources(db, [source_id]).get(source_id)

    def paper_meta_for_sources(self, db: sqlite3.Connection,
                               source_ids: Sequence[str]) -> Dict[str, dict]:
        """批量水合(IN 分批守 999 变量上限,同 sources_from_rows 惯例)。
        无 meta 行的源不在返回里。"""
        meta_rows: Dict[str, sqlite3.Row] = {}
        author_rows: Dict[str, List[sqlite3.Row]] = {}
        ids = list(source_ids)
        for i in range(0, len(ids), self.IN_CHUNK):
            batch = ids[i:i + self.IN_CHUNK]
            ph = ",".join("?" for _ in batch)
            for row in db.execute(
                f"SELECT * FROM source_paper_meta WHERE source_id IN ({ph})", batch,
            ).fetchall():
                meta_rows[row["source_id"]] = row
            for a in db.execute(
                f"SELECT source_id, position, name, affiliation FROM source_authors "
                f"WHERE source_id IN ({ph}) ORDER BY source_id, position ASC", batch,
            ).fetchall():
                author_rows.setdefault(a["source_id"], []).append(a)
        return {
            sid: self._paper_meta_dict(row, author_rows.get(sid, []))
            for sid, row in meta_rows.items()
        }

    def sources_missing_paper_meta(self, notebook_id: str,
                                   include_existing: bool = False) -> List[str]:
        """补抽目标源:doc_type 为 academic_paper(含 ''=默认,与 run_extraction 的
        `or "academic_paper"` 语义一致)、已有解析产物(parsed 及之后)、非 memory/
        knowhow 合成源;默认排除已有 meta 行(幂等续跑),include_existing=True
        (--force)全量。"""
        missing = "" if include_existing else PAPER_META_NO_META_SQL
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s "
                "WHERE s.notebook_id = ?"
                f"{PAPER_META_ELIGIBLE_SQL}{missing} ORDER BY s.created_at ASC",
                (notebook_id,),
            ).fetchall()
        return [r["id"] for r in rows]
