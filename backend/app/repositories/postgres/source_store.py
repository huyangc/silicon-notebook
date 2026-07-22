from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from app.models.sources import (
    PaginatedSources,
    PaperAuthor,
    PaperMeta,
    SourceDetail,
    SourceElement,
    SourceSummary,
)
from app.repositories.ports import SOURCE_PAPER_META_UNSET, SourceElementWrite
from app.repositories.postgres._store_utils import (
    execute_many,
    json_value,
    jsonb,
    placeholders,
)
from app.repositories.postgres.database import PostgresDatabase


_UNSET = SOURCE_PAPER_META_UNSET


def _created_label(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            parsed = datetime.now()
    return f"{parsed.year}年{parsed.month}月{parsed.day}日"


def _metadata_compat(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False)


class SourceStore:
    """PostgreSQL source rows, elements, paper metadata, and summary hydration."""

    IN_CHUNK = 5_000

    def __init__(self, database: PostgresDatabase, *, now: Callable[[], str]) -> None:
        self.database = database
        self.now = now

    def list_sources(self, notebook_id: str) -> list[SourceSummary]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow') "
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
        where = "WHERE notebook_id=%s AND source_type NOT IN ('memory','knowhow')"
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
                "SELECT 1 FROM sources WHERE id=%s", (source_id,)
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

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(value for value in source_ids if value))
        if not ids:
            return {}
        result: dict[str, dict[str, Any]] = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), self.IN_CHUNK):
                batch = ids[offset : offset + self.IN_CHUNK]
                rows = connection.execute(
                    "SELECT id,notebook_id,title,file_name,summary,doc_type "
                    f"FROM sources WHERE id IN ({placeholders(batch)})",
                    batch,
                ).fetchall()
                result.update({row["id"]: dict(row) for row in rows})
        return result

    @staticmethod
    def retrieval_element_rows(connection, notebook_id: str):
        return connection.execute(
            "SELECT e.id,e.source_id,e.element_type,e.location_label,e.text,"
            "s.title AS source_title,m.vector AS vector "
            "FROM source_elements e JOIN sources s ON s.id=e.source_id "
            "LEFT JOIN element_embeddings m ON m.element_id=e.id "
            "WHERE s.notebook_id=%s ORDER BY e.ordinal",
            (notebook_id,),
        ).fetchall()

    def report_source_rows(self, notebook_id: str) -> list[dict[str, str]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT title FROM sources WHERE notebook_id=%s "
                "AND source_type NOT IN ('memory','knowhow') "
                "ORDER BY created_at,id COLLATE \"C\" LIMIT 20",
                (notebook_id,),
            ).fetchall()
        return [{"title": row["title"]} for row in rows]

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
        connection=None,
    ) -> None:
        statement = (
            "INSERT INTO sources"
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "source_url,file_size,file_hash,summary,doc_type,memory_id,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
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
            now,
            now,
        )
        if connection is not None:
            connection.execute(statement, values)
            return
        with self.database.write() as owned:
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

    def replace_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None:
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

    def delete_source_row(self, connection, source_id: str) -> None:
        connection.execute("DELETE FROM sources WHERE id=%s", (source_id,))

    def insert_elements(
        self,
        connection,
        source_id: str,
        elements: Sequence[SourceElementWrite],
        *,
        created_at: str,
    ) -> None:
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
        kg_extracted = bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM knowledge_objects o "
                "WHERE o.source_id=%s AND o.source_id<>'' AND COALESCE(("
                "SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=o.source_id AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1"
                "),'completed')='completed') AS ok",
                (source_id,),
            ).fetchone()["ok"]
        )
        meta = (
            self.paper_meta_for_sources(connection, [source_id]).get(source_id)
            if paper_meta is _UNSET
            else paper_meta
        )
        summary = SourceSummary(
            id=source_id,
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
            doc_type=row.get("doc_type", ""),
            source_url=row.get("source_url", ""),
            extraction_warning=self.extraction_warning(connection, source_id),
            kg_extracted=kg_extracted,
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
        for offset in range(0, len(source_ids), self.IN_CHUNK):
            batch = source_ids[offset : offset + self.IN_CHUNK]
            marker = placeholders(batch)
            for row in connection.execute(
                "SELECT source_id,COUNT(*) AS c FROM source_elements "
                f"WHERE source_id IN ({marker}) GROUP BY source_id",
                batch,
            ).fetchall():
                element_counts[row["source_id"]] = int(row["c"])
            for row in connection.execute(
                "SELECT DISTINCT o.source_id FROM knowledge_objects o "
                f"WHERE o.source_id IN ({marker}) AND o.source_id<>'' "
                "AND COALESCE((SELECT r.status FROM extraction_runs r "
                "WHERE r.source_id=o.source_id AND r.run_type='kg' "
                "ORDER BY r.created_at DESC,r.ordinal DESC LIMIT 1),'completed')='completed'",
                batch,
            ).fetchall():
                kg_extracted_ids.add(row["source_id"])
            for row in connection.execute(
                "SELECT source_id,error_message FROM extraction_runs "
                f"WHERE source_id IN ({marker}) "
                "ORDER BY source_id COLLATE \"C\",created_at DESC,ordinal DESC",
                batch,
            ).fetchall():
                latest_error.setdefault(row["source_id"], row["error_message"] or "")

        def warning(source_id: str) -> str | None:
            match = re.search(
                r"windows_failed=(\d+)/(\d+)", latest_error.get(source_id, "")
            )
            if not match or int(match.group(1)) <= 0:
                return None
            return (
                f"部分内容因网络问题未抽取（{int(match.group(1))}/"
                f"{int(match.group(2))} 段失败），建议重新上传或重试抽取。"
            )

        output: list[SourceSummary] = []
        for row in rows:
            source_id = row["id"]
            meta = paper_meta.get(source_id)
            summary = SourceSummary(
                id=source_id,
                notebook_id=row["notebook_id"],
                title=row["title"],
                type=row["source_type"],
                status=row["status"],
                summary=row["summary"],
                element_count=element_counts.get(source_id, 0),
                file_name=row["file_name"],
                file_size=row["file_size"],
                file_hash=row["file_hash"],
                parse_status=row["parse_status"],
                created_label=_created_label(row["created_at"]),
                doc_type=row.get("doc_type", ""),
                source_url=row.get("source_url", ""),
                extraction_warning=warning(source_id),
                kg_extracted=source_id in kg_extracted_ids,
                authors=[author["name"] for author in meta["authors"]] if meta else [],
                pub_year=meta["pub_year"] if meta else None,
                venue=meta["venue"] if meta else None,
            )
            summary.paper_meta_status = self._paper_meta_status_for(row, meta)
            output.append(summary)
        return output

    def extraction_warning(self, connection, source_id: str) -> str | None:
        run = connection.execute(
            "SELECT error_message FROM extraction_runs WHERE source_id=%s "
            "ORDER BY created_at DESC,ordinal DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        if run is None:
            return None
        match = re.search(r"windows_failed=(\d+)/(\d+)", run["error_message"] or "")
        if not match or int(match.group(1)) <= 0:
            return None
        return (
            f"部分内容因网络问题未抽取（{int(match.group(1))}/"
            f"{int(match.group(2))} 段失败），建议重新上传或重试抽取。"
        )

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
        if meta is not None:
            return "has_meta" if meta.get("is_paper") else "not_paper"
        if row.get("source_type", "") in ("memory", "knowhow"):
            return None
        if row.get("doc_type", "") not in ("", "academic_paper"):
            return None
        if row.get("parse_status", "") not in ("parsed", "extracting", "extracted"):
            return None
        return "missing"

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
        missing = (
            ""
            if include_existing
            else " AND NOT EXISTS(SELECT 1 FROM source_paper_meta m WHERE m.source_id=s.id)"
        )
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.id FROM sources s WHERE s.notebook_id=%s "
                "AND s.source_type NOT IN ('memory','knowhow') "
                "AND s.doc_type IN ('','academic_paper') "
                "AND s.parse_status IN ('parsed','extracting','extracted') "
                + missing
                + " ORDER BY s.created_at,s.id COLLATE \"C\"",
                (notebook_id,),
            ).fetchall()
        return [row["id"] for row in rows]
