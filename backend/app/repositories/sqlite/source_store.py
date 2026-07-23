from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from app.models.sources import (
    PaginatedSources,
    PaperAuthor,
    PaperMeta,
    SourceDetail,
    SourceElement,
    SourceSummary,
)
from app.repositories.sqlite.database import SqliteDatabase


# Sentinel distinguishing "paper_meta not passed" (source_from_row should
# fetch it itself) from an explicit `paper_meta=None` ("caller already
# fetched — there is no meta row"). A plain `None` default couldn't tell
# those two cases apart.
_UNSET = object()


def _created_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日"


@dataclass(frozen=True)
class SourceElementWrite:
    id: str
    element_type: str
    location_label: str
    text: str
    metadata: Mapping[str, Any]


class SourceStore:
    """SQLite sources/source_elements row persistence plus the SourceSummary
    hydration (element counts, extraction warnings, KG-extracted flags — the C5
    batched projection). Row-level only — notebook existence guards, the
    parse/summarize pipeline, status events and file handling stay in the
    facade orchestration."""

    # IN(...) batching bound for the batched hydration lookups — mirrors the
    # facade's `_IN_CHUNK` (well under SQLite's default 999-variable limit).
    IN_CHUNK = 900

    def __init__(self, database: SqliteDatabase, *, now: Callable[[], str]) -> None:
        self.database = database
        self.now = now

    # ------------------------------------------------------------------ reads
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
                "AND source_type NOT IN ('memory', 'knowhow') "
                "ORDER BY created_at ASC",
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
        where = "WHERE notebook_id = ? AND source_type NOT IN ('memory', 'knowhow')"
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
                f"SELECT * FROM sources {where} ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            items = self.sources_from_rows(db, rows)
        return PaginatedSources(items=items, total_count=total, offset=offset, limit=limit)

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

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(source_id for source_id in source_ids if source_id))
        if not ids:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self.database.connect() as db:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset:offset + self.IN_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(
                    "SELECT id, notebook_id, title, file_name, summary, doc_type "
                    f"FROM sources WHERE id IN ({placeholders})",
                    batch,
                ).fetchall():
                    out[row["id"]] = dict(row)
        return out

    @staticmethod
    def retrieval_element_rows(db: sqlite3.Connection, notebook_id: str):
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

    def report_source_rows(self, notebook_id: str) -> List[Dict[str, str]]:
        """Report corpus-map recon (Task 25): source titles in creation order,
        LIMIT 20 — the deep-report engine's scout cap.  SQL frozen from the
        facade's inline query; strip/filter formatting stays engine-side.
        Excludes source_type IN ('memory', 'knowhow') so a hidden Memory- or
        knowhow-projection-derived title is never shown to the report planner
        as a source doc (their KG objects/chunks still participate via
        knowledge_objects/chunks — same "hidden container, visible content"
        split as the other memory-kg-extract sites in this file)."""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT title FROM sources WHERE notebook_id=? "
                "AND source_type NOT IN ('memory', 'knowhow') "
                "ORDER BY created_at LIMIT 20",
                (notebook_id,),
            ).fetchall()
        return [{"title": row["title"]} for row in rows]

    def source_titles(self, source_ids: List[str]) -> Dict[str, str]:
        """Batch {source_id: title} lookup (Task 24): ask_graph 的源 chunk 引用
        标签补全 — SQL frozen from the engine's inline query (one IN(...) list;
        the caller dedups and the post-truncation id count stays tiny)."""
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
        connection: "sqlite3.Connection | None" = None,
    ) -> None:
        """Insert one sources row (created_at/updated_at minted via the ``now``
        seam). Pass ``connection`` to join a caller-owned write transaction —
        batch imports keep their all-or-nothing semantics; without it the row
        commits in its own write transaction.

        ``memory_id`` links a Memory-derived synthetic source (source_type=
        'memory') back to its origin Memory row; the default "" leaves
        ordinary sources out of the partial unique index
        (idx_sources_memory_id caps this at one derived source per Memory)."""
        now = self.now()
        statement = (
            """
            INSERT INTO sources
            (id, notebook_id, title, source_type, status, parse_status, file_name,
             file_path, source_url, file_size, file_hash, summary, doc_type,
             memory_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        values = (
            source_id, notebook_id, title, source_type, status, parse_status,
            file_name, file_path, source_url, file_size, file_hash, summary,
            doc_type, memory_id, now, now,
        )
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as db:
            db.execute(statement, values)

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

    def mark_chunked(self, source_id: str, ts: str) -> None:
        """置分块完成标记 ``chunked_at = ts``(本代 elements 已成功走完分块步骤
        的时刻)。与 ``set_status`` 正交——**不碰** status/parse_status;唯一载体
        是 ``chunked_at`` 的 NULL 性(NULL = 未成功分块)。由
        ``build_chunks_for_source`` 正常返回前调用,覆盖所有分块路径(含产 0 chunk
        的纯标题 md——也算分块成功、也置值)。自带写事务,镜像 ``set_status``。"""
        with self.database.write() as db:
            db.execute(
                "UPDATE sources SET chunked_at = ? WHERE id = ?",
                (ts, source_id),
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

    def replace_elements(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None:
        """Swap a source's elements INSIDE the caller's write transaction (the
        parse pipeline clears extraction state in the same transaction)."""
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
        kg_extracted = bool(db.execute(
            "SELECT EXISTS("
            "  SELECT 1 FROM knowledge_objects ko "
            "  WHERE ko.source_id = ? AND ko.source_id != '' "
            "  AND COALESCE(("
            "    SELECT er.status FROM extraction_runs er "
            "    WHERE er.source_id=ko.source_id AND er.run_type='kg' "
            "    ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
            "  ), 'completed')='completed'"
            ")",
            (row["id"],),
        ).fetchone()[0])
        pm = (
            self.paper_meta_for_sources(db, [row["id"]]).get(row["id"])
            if paper_meta is _UNSET else paper_meta
        )
        summary = SourceSummary(
            id=row["id"],
            notebook_id=row["notebook_id"],
            title=row["title"],
            type=row["source_type"],
            status=row["status"],
            summary=row["summary"],
            element_count=element_count,
            file_name=row["file_name"],
            file_size=row["file_size"],
            file_hash=row["file_hash"],
            parse_status=row["parse_status"],
            created_label=_created_label(row["created_at"]),
            doc_type=row["doc_type"] if "doc_type" in row.keys() else "",
            source_url=row["source_url"] if "source_url" in row.keys() else "",
            extraction_warning=self.extraction_warning(db, row["id"]),
            kg_extracted=kg_extracted,
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
        knowledge_objects EXISTS, paper-meta hydration) each become batched
        `id IN (...)` queries for the whole page instead of one query per
        row — was 4*N round-trips.

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
        for i in range(0, len(source_ids), self.IN_CHUNK):
            batch = source_ids[i:i + self.IN_CHUNK]
            ph = ",".join("?" for _ in batch)
            for r in db.execute(
                f"SELECT source_id, COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({ph}) GROUP BY source_id", batch,
            ).fetchall():
                element_counts[r["source_id"]] = int(r["c"])
            for r in db.execute(
                f"SELECT DISTINCT ko.source_id FROM knowledge_objects ko "
                f"WHERE ko.source_id IN ({ph}) AND ko.source_id != '' "
                "AND COALESCE(("
                "  SELECT er.status FROM extraction_runs er "
                "  WHERE er.source_id=ko.source_id AND er.run_type='kg' "
                "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
                "), 'completed')='completed'",
                batch,
            ).fetchall():
                kg_extracted_ids.add(r["source_id"])
            for r in db.execute(
                f"SELECT source_id, error_message FROM extraction_runs "
                f"WHERE source_id IN ({ph}) ORDER BY source_id, created_at DESC",
                batch,
            ).fetchall():
                latest_error.setdefault(r["source_id"], r["error_message"] or "")

        def _warning(source_id: str) -> Optional[str]:
            if source_id not in latest_error:
                return None
            m = re.search(r"windows_failed=(\d+)/(\d+)", latest_error[source_id])
            if not m:
                return None
            fw = int(m.group(1))
            if fw <= 0:
                return None
            tw = int(m.group(2))
            return f"部分内容因网络问题未抽取（{fw}/{tw} 段失败），建议重新上传或重试抽取。"

        out: List[SourceSummary] = []
        for row in rows:
            sid = row["id"]
            pm = paper_meta.get(sid)
            summary = SourceSummary(
                id=sid,
                notebook_id=row["notebook_id"],
                title=row["title"],
                type=row["source_type"],
                status=row["status"],
                summary=row["summary"],
                element_count=element_counts.get(sid, 0),
                file_name=row["file_name"],
                file_size=row["file_size"],
                file_hash=row["file_hash"],
                parse_status=row["parse_status"],
                created_label=_created_label(row["created_at"]),
                doc_type=row["doc_type"] if "doc_type" in row.keys() else "",
                source_url=row["source_url"] if "source_url" in row.keys() else "",
                extraction_warning=_warning(sid),
                kg_extracted=sid in kg_extracted_ids,
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
        `windows_failed=N/T` token rather than stored on the source row."""
        run = db.execute(
            "SELECT error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
        if run is None:
            return None
        m = re.search(r"windows_failed=(\d+)/(\d+)", run["error_message"] or "")
        if not m:
            return None
        fw = int(m.group(1))
        if fw <= 0:
            return None
        tw = int(m.group(2))
        return f"部分内容因网络问题未抽取（{fw}/{tw} 段失败），建议重新上传或重试抽取。"

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
        """纯函数:从 sources 行 + 可选 meta 字典派生四态。零 DB 访问(调用方已经
        通过 paper_meta_for_sources 拿到了 meta,这里只做分类,不再查库)。

        meta 不为 None(source_paper_meta 行存在,含标记行)时按 is_paper 分流
        has_meta/not_paper;meta 为 None 时区分"合规但未抽取"(missing)与
        "不适用"(None) —— 后者涵盖隐藏合成源(memory/knowhow)、非论文
        doc_type、以及尚未解析完成的源,同 sources_missing_paper_meta 的候选口径。"""
        if meta is not None:
            return "has_meta" if meta.get("is_paper") else "not_paper"
        source_type = row["source_type"] if "source_type" in row.keys() else ""
        doc_type = row["doc_type"] if "doc_type" in row.keys() else ""
        parse_status = row["parse_status"] if "parse_status" in row.keys() else ""
        if source_type in ("memory", "knowhow"):
            return None
        if doc_type not in ("", "academic_paper"):
            return None
        if parse_status not in ("parsed", "extracting", "extracted"):
            return None
        return "missing"

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
        missing = (
            "" if include_existing else
            " AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)"
        )
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s "
                "WHERE s.notebook_id = ? "
                "  AND s.source_type NOT IN ('memory', 'knowhow') "
                "  AND s.doc_type IN ('', 'academic_paper') "
                "  AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
                f"{missing} ORDER BY s.created_at ASC",
                (notebook_id,),
            ).fetchall()
        return [r["id"] for r in rows]
