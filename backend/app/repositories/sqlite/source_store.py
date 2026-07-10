from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from app.models.schemas import (
    PaginatedSources,
    SourceDetail,
    SourceElement,
    SourceSummary,
)
from app.repositories.sqlite.database import SqliteDatabase


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
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? ORDER BY created_at ASC",
                (notebook_id,),
            ).fetchall()
            return self.sources_from_rows(db, rows)

    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50,
                          q: str = "") -> PaginatedSources:
        """分页 + 可选 q(按 title/file_name 服务端过滤)。万级 source 安全:只取一页 +
        一次 COUNT,不全量进内存。"""
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        needle = (q or "").strip().lower()
        where = "WHERE notebook_id = ?"
        params: List[object] = [notebook_id]
        if needle:
            where += " AND (LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?)"
            like = f"%{needle}%"
            params += [like, like]
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
            summary = self.source_from_row(db, row)
            return SourceDetail(
                **summary.model_dump(),
                file_path=row["file_path"],
                error_message=row["error_message"],
            )

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

    def notebook_element_sample(self, notebook_id: str) -> List[dict]:
        """Notebook-wide element sample for schema induction (Task 13). Runs
        the SAME join _gather_elements always ran (identical row order — the
        LEFT JOIN keeps the query plan byte-stable) but skips the Python-side
        vector decode: propose_schemas only reads location_label/text."""
        with self.database.connect() as db:
            rows = db.execute(
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
        return [
            {"location_label": row["location_label"], "text": row["text"]}
            for row in rows
        ]

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
        connection: "sqlite3.Connection | None" = None,
    ) -> None:
        """Insert one sources row (created_at/updated_at minted via the ``now``
        seam). Pass ``connection`` to join a caller-owned write transaction —
        batch imports keep their all-or-nothing semantics; without it the row
        commits in its own write transaction."""
        now = self.now()
        statement = (
            """
            INSERT INTO sources
            (id, notebook_id, title, source_type, status, parse_status, file_name,
             file_path, source_url, file_size, file_hash, summary, doc_type,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        )
        values = (
            source_id, notebook_id, title, source_type, status, parse_status,
            file_name, file_path, source_url, file_size, file_hash, summary,
            doc_type, now, now,
        )
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as db:
            db.execute(statement, values)

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

    # -------------------------------------------------------------- hydration
    def source_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> SourceSummary:
        """Single-row path (get_source) — 3 point queries. list_sources /
        list_sources_page use the batched sources_from_rows sibling instead
        (C5: was 3 queries * N rows per page — now 3 total per page)."""
        element_count = int(db.execute(
            "SELECT COUNT(*) AS count FROM source_elements WHERE source_id = ?",
            (row["id"],),
        ).fetchone()["count"])
        kg_extracted = bool(db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE source_id = ? AND source_id != '')",
            (row["id"],),
        ).fetchone()[0])
        return SourceSummary(
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
        )

    def sources_from_rows(self, db: sqlite3.Connection, rows: List[sqlite3.Row]) -> List[SourceSummary]:
        """Batched sibling of source_from_row for a PAGE of source rows (house
        pattern, see _hydrate_search_hits): the 3 per-row lookups
        (source_elements COUNT, latest extraction_runs error_message,
        knowledge_objects EXISTS) each become ONE `id IN (...)` query for the
        whole page instead of one query per row — was 3*N round-trips.

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
                f"SELECT DISTINCT source_id FROM knowledge_objects "
                f"WHERE source_id IN ({ph}) AND source_id != ''", batch,
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
            out.append(SourceSummary(
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
            ))
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
