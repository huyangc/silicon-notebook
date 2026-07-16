from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.models.schemas import (
    NotebookAnalytics,
    NotebookSearchResponse,
    SearchHit,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.services.extraction_profiles import OBJECT_TYPE_LABELS
from app.services.notebook_scale import NotebookScaleFacts


def _snippet(text: str, needle: str) -> str:
    clean = " ".join(text.split())
    lower = clean.lower()
    index = lower.find(needle)
    if index < 0:
        return clean[:180]
    start = max(0, index - 48)
    end = min(len(clean), index + len(needle) + 120)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


class QueryStore:
    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    # NotebookSummary projection primitives.  The caller deliberately retains
    # the connection so one summary is hydrated from one read snapshot.
    @staticmethod
    def count_rows(
        db: sqlite3.Connection, table: str, column: str, value: str
    ) -> int:
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?", (value,)
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def knowledge_type_count_rows(
        db: sqlite3.Connection, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        # Served from the seq-gated count cache (one GROUP BY per kg_mutation_seq
        # instead of per open/list/poll). Returns row-like dicts with the same
        # ["object_type"] / ["c"] keys the callers read.
        from app.repositories.sqlite import knowledge_counts_cache
        counts = knowledge_counts_cache.type_counts(db, notebook_id, statuses)
        return [{"object_type": ot, "c": c} for ot, c in counts.items()]

    @staticmethod
    def notebook_has_kg(db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def pending_kg_source_count(db: sqlite3.Connection, notebook_id: str) -> int:
        # Served from the seq-gated count cache (one correlated scan per
        # kg_mutation_seq instead of ~2s per open at 48k sources).
        from app.repositories.sqlite import knowledge_counts_cache
        return knowledge_counts_cache.pending_source_count(db, notebook_id)

    @staticmethod
    def visible_source_count(db: sqlite3.Connection, notebook_id: str) -> int:
        """NotebookSummary's user-facing source count — excludes Memory-derived
        AND knowhow-table hidden synthetic sources (source_type IN ('memory',
        'knowhow')): both are internal derivation links with no independent
        user-visible content, which would otherwise double-count next to the
        Memory panel's own count / inflate the count past what list_sources
        shows (SourceStore.list_sources carries the SAME exclusion — see its
        docstring). Internal paths (pending_kg_source_count above, copy
        materialization, scale-index scans) keep counting the true full set
        and must NOT reuse this method."""
        row = db.execute(
            "SELECT COUNT(*) AS count FROM sources "
            "WHERE notebook_id = ? AND source_type NOT IN ('memory', 'knowhow')",
            (notebook_id,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def base_notebook_info_row(db: sqlite3.Connection):
        return db.execute(
            "SELECT nb.name, "
            "EXISTS(SELECT 1 FROM knowledge_objects ko "
            "JOIN notebooks b ON b.id = ko.notebook_id WHERE b.tier = 'base') "
            "FROM notebooks nb WHERE nb.tier = 'base' "
            "ORDER BY nb.created_at ASC LIMIT 1"
        ).fetchone()

    @staticmethod
    def summary_notebook_row(db: sqlite3.Connection, notebook_id: str):
        return db.execute(
            "SELECT * FROM notebooks WHERE id = ? AND status != 'copying'",
            (notebook_id,),
        ).fetchone()

    @staticmethod
    def owned_notebook_rows(db: sqlite3.Connection, user_id: str):
        return db.execute(
            "SELECT * FROM notebooks WHERE created_by = ? AND status != 'copying' "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()

    @staticmethod
    def joined_notebook_rows(db: sqlite3.Connection, user_id: str):
        return db.execute(
            "SELECT nb.*, u.username AS _owner_username FROM notebook_members m "
            "JOIN notebooks nb ON nb.id = m.notebook_id "
            "LEFT JOIN users u ON u.id = nb.created_by "
            "WHERE m.user_id = ? AND nb.status != 'copying' "
            "ORDER BY m.added_at ASC",
            (user_id,),
        ).fetchall()

    @staticmethod
    def memory_counts_by_owner_notebook(
        db: sqlite3.Connection, user_id: str
    ) -> dict[tuple[str, str], int]:
        """One owner-scoped grouped query for every notebook card.

        Memory is private to ``created_by`` even when the notebook is shared;
        grouping by both privacy key and notebook id makes that scope explicit
        and avoids a per-card query.
        """
        rows = db.execute(
            "SELECT created_by, notebook_id, COUNT(*) AS c FROM memory_items "
            "WHERE created_by=? GROUP BY created_by, notebook_id",
            (user_id,),
        ).fetchall()
        return {
            (row["created_by"], row["notebook_id"]): int(row["c"])
            for row in rows
        }

    def list_user_usage(self) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            users = db.execute(
                "SELECT id, username, display_name, role, created_at "
                "FROM users ORDER BY created_at, id"
            ).fetchall()
            notebooks = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM notebooks "
                    "WHERE status != 'copying' GROUP BY created_by"
                ).fetchall()
            }
            sources = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT nb.created_by AS k, COUNT(*) AS c FROM sources s "
                    "JOIN notebooks nb ON nb.id = s.notebook_id GROUP BY nb.created_by"
                ).fetchall()
            }
            conversations = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM conversations GROUP BY created_by"
                ).fetchall()
            }
            reports = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT nb.created_by AS k, COUNT(*) AS c FROM reports r "
                    "JOIN notebooks nb ON nb.id = r.notebook_id GROUP BY nb.created_by"
                ).fetchall()
            }
            active = {
                row["k"]: row["m"]
                for row in db.execute(
                    "SELECT created_by AS k, MAX(updated_at) AS m FROM conversations "
                    "GROUP BY created_by"
                ).fetchall()
            }
        return [
            {
                "id": user["id"],
                "username": user["username"] or user["display_name"] or user["id"],
                "role": user["role"],
                "created_at": user["created_at"],
                "notebooks": notebooks.get(user["id"], 0),
                "sources": sources.get(user["id"], 0),
                "conversations": conversations.get(user["id"], 0),
                "reports": reports.get(user["id"], 0),
                "last_active": active.get(user["id"]),
            }
            for user in users
        ]

    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            notebooks = db.execute(
                "SELECT id, name, status, created_at, updated_at FROM notebooks "
                "WHERE created_by = ? AND status != 'copying' ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            ids = [row["id"] for row in notebooks]
            sources: dict[str, int] = {}
            conversations: dict[str, int] = {}
            reports: dict[str, int] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                sources = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM sources "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                conversations = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM conversations "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                reports = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM reports "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "sources": sources.get(row["id"], 0),
                "conversations": conversations.get(row["id"], 0),
                "reports": reports.get(row["id"], 0),
            }
            for row in notebooks
        ]

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        with self.database.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM notebooks WHERE id = ? AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(notebook_id)
            answers_total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM answers WHERE notebook_id = ?",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            not_useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'not_useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            low_rated = [
                row["question"]
                for row in db.execute(
                    "SELECT DISTINCT a.question FROM feedback f "
                    "JOIN answers a ON a.id = f.answer_id "
                    "WHERE f.notebook_id = ? AND f.rating = 'not_useful' "
                    "ORDER BY f.created_at DESC LIMIT 10",
                    (notebook_id,),
                ).fetchall()
            ]
            # seq-gated count cache (non-deprecated) — same GROUP BY, memoized.
            from app.repositories.sqlite import knowledge_counts_cache
            knowledge_counts = knowledge_counts_cache.type_counts(db, notebook_id)
            # Memory-derived AND knowhow-projection hidden synthetic sources
            # (source_type IN ('memory', 'knowhow')) are excluded — this feeds
            # the /analytics 看板 parse_status distribution, a user-facing
            # surface; see visible_source_count.
            source_status_counts = {
                row["parse_status"]: int(row["c"])
                for row in db.execute(
                    "SELECT parse_status, COUNT(*) AS c FROM sources "
                    "WHERE notebook_id = ? AND source_type NOT IN ('memory', 'knowhow') "
                    "GROUP BY parse_status",
                    (notebook_id,),
                ).fetchall()
            }
            # paper-meta 三态计数(看板;paper-metadata Task 4)。paper_meta 写入
            # (create_source/upsert_paper_meta)不 bump kg_mutation_seq,故这两条
            # GROUP BY 必须未缓存直读——不能像 knowledge_counts 那样走
            # knowledge_counts_cache 的 seq 门(会读到陈旧值,见该模块 docstring
            # 对 sources COUNT 的同款排除说明)。is_paper 计数走
            # idx_source_paper_meta_nb(notebook_id)。
            by_is_paper = {
                int(row["is_paper"]): int(row["c"])
                for row in db.execute(
                    "SELECT is_paper, COUNT(*) AS c FROM source_paper_meta "
                    "WHERE notebook_id = ? GROUP BY is_paper",
                    (notebook_id,),
                ).fetchall()
            }
            # missing 计数是 SourceStore.sources_missing_paper_meta 的 COUNT 镜像
            # (WHERE 子句逐字保持一致,口径漂移会立刻体现为两处不一致),走
            # idx_sources_nb_parse_status_type(notebook_id, parse_status, source_type)。
            missing = int(db.execute(
                "SELECT COUNT(*) AS c FROM sources s "
                "WHERE s.notebook_id = ? "
                "  AND s.source_type NOT IN ('memory', 'knowhow') "
                "  AND s.doc_type IN ('', 'academic_paper') "
                "  AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
                "  AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)",
                (notebook_id,),
            ).fetchone()["c"])
            paper_meta_counts = {
                "has_meta": by_is_paper.get(1, 0),
                "marker": by_is_paper.get(0, 0),
                "missing": missing,
            }
        rated = useful + not_useful
        return NotebookAnalytics(
            answers_total=answers_total,
            feedback_useful=useful,
            feedback_not_useful=not_useful,
            usefulness_rate=round(useful / rated, 3) if rated else 0.0,
            low_rated_questions=low_rated,
            knowledge_counts=knowledge_counts,
            source_status_counts=source_status_counts,
            paper_meta_counts=paper_meta_counts,
        )

    def pending_actions_projection_rows(self, user_id: str) -> dict:
        items: list[dict[str, Any]] = []
        with self.database.connect() as db:
            mine = db.execute(
                "SELECT id, name FROM notebooks WHERE created_by = ? AND status != 'copying'",
                (user_id,),
            ).fetchall()
            name_of = {row["id"]: row["name"] for row in mine}
            notebook_ids = list(name_of)
            if notebook_ids:
                role_row = db.execute(
                    "SELECT role FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                is_admin = bool(role_row) and role_row["role"] == "admin"
                reports = db.execute(
                    "SELECT id, question, notebook_id, created_at FROM reports "
                    "WHERE status = 'outline_ready' AND created_by = ? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
                for row in reports:
                    items.append(
                        {
                            "type": "report_outline",
                            "notebook_id": row["notebook_id"],
                            "notebook_name": name_of.get(row["notebook_id"], ""),
                            "report_id": row["id"],
                            "title": (row["question"] or "")[:60],
                            "created_at": row["created_at"],
                        }
                    )
                placeholders = ",".join("?" for _ in notebook_ids)
                governance = [
                    ("merge", "concept_merge_candidates", "status = 'pending'"),
                    ("edge", "knowledge_relations", "review_status = 'pending'"),
                ]
                if is_admin:
                    governance.append(
                        (
                            "promotion",
                            "promotion_candidates",
                            "status IN ('proposed','under_review')",
                        )
                    )
                for subtype, table, predicate in governance:
                    grouped = db.execute(
                        f"SELECT notebook_id, COUNT(*) AS c FROM {table} "
                        f"WHERE notebook_id IN ({placeholders}) AND {predicate} GROUP BY notebook_id",
                        notebook_ids,
                    ).fetchall()
                    for row in grouped:
                        if row["c"] > 0:
                            items.append(
                                {
                                    "type": "governance",
                                    "subtype": subtype,
                                    "notebook_id": row["notebook_id"],
                                    "notebook_name": name_of.get(row["notebook_id"], ""),
                                    "count": row["c"],
                                }
                            )
        return {
            "notebook_ids": notebook_ids,
            "notebook_names": name_of,
            "items": items,
        }

    @staticmethod
    def _knowledge_headline(object_type: str, payload: dict) -> str:
        keys = {
            "rule": ("title", "statement"),
            "method": ("name", "use_when"),
            "risk": ("title", "description"),
            "glossary": ("term", "definition"),
            "case": ("symptom", "context"),
            "checklist": ("question",),
            "claim": ("name", "statement"),
            "formula": ("name", "statement"),
            "procedure": ("name", "title"),
            "concept": ("name", "term", "definition"),
            "finding": ("name", "statement", "metric"),
            "principle": ("statement", "rationale"),
            "example": ("title", "problem"),
        }.get(object_type, ("name", "title", "statement", "term", "question"))
        for key in keys:
            value = str(payload.get(key, "")).strip()
            if value:
                return value[:120]
        return object_type

    @staticmethod
    def _payload_join(payload: dict) -> str:
        parts: list[str] = []
        for key, value in payload.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
        return " ".join(parts)

    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse:
        needle = query.strip().lower()
        with self.database.connect() as db:
            notebook = db.execute(
                "SELECT * FROM notebooks WHERE id = ? AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if notebook is None:
                raise KeyError(notebook_id)
            if not needle:
                return NotebookSearchResponse(query=query, hits=[])
            like = f"%{needle}%"
            cap = 20
            hits: list[SearchHit] = []
            for scope, value in (
                ("Notebook", notebook["name"]),
                ("Domain", notebook["primary_domain"]),
            ):
                if needle in f"{scope} {value}".lower():
                    hits.append(
                        SearchHit(
                            scope=scope,
                            notebook_id=notebook_id,
                            label=scope,
                            text=_snippet(value or scope, needle),
                            source_id="",
                            element_id="",
                        )
                    )
            # source_type NOT IN ('memory', 'knowhow') keeps Memory-derived AND
            # knowhow-projection hidden synthetic sources out of the search box
            # (GET /notebooks/{id}/search) — same user-facing hide as
            # list_sources; a knowhow hidden source's title ("Knowhow 表：…")
            # would otherwise surface as a dead-end "Source" hit with no
            # coherent source view to jump to (citation-jump to the row detail
            # drawer is PR-2 scope, per the design spec).
            source_rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? "
                "AND source_type NOT IN ('memory', 'knowhow') AND "
                "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(file_name) LIKE ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (notebook_id, like, like, like, cap),
            ).fetchall()
            for row in source_rows:
                label = row["title"] or row["file_name"]
                body = row["summary"] or row["file_name"] or row["title"]
                hits.append(
                    SearchHit(
                        scope="Source",
                        notebook_id=notebook_id,
                        label=label,
                        text=_snippet(body, needle),
                        source_id=row["id"],
                        element_id="",
                    )
                )
            # Same hide on the element leg: a memory source's or knowhow
            # hidden source's element text must not leak in as a
            # scope="Element" hit either (a knowhow cell's own element would
            # otherwise show up here with the same dead-end-navigation issue
            # as the "Source" leg above).
            element_rows = db.execute(
                "SELECT se.*, s.title AS source_title FROM source_elements se "
                "JOIN sources s ON s.id = se.source_id "
                "WHERE s.notebook_id = ? AND s.source_type NOT IN ('memory', 'knowhow') AND "
                "(LOWER(se.text) LIKE ? OR LOWER(se.location_label) LIKE ? OR LOWER(s.title) LIKE ?) "
                "LIMIT ?",
                (notebook_id, like, like, like, cap),
            ).fetchall()
            for row in element_rows:
                label = f"{row['source_title']} · {row['location_label']}"
                hits.append(
                    SearchHit(
                        scope="Element",
                        notebook_id=notebook_id,
                        label=label,
                        text=_snippet(row["text"] or label, needle),
                        source_id=row["source_id"],
                        element_id=row["id"],
                    )
                )
            knowledge_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated' AND LOWER(payload) LIKE ? LIMIT ?",
                (notebook_id, like, cap),
            ).fetchall()
            for row in knowledge_rows:
                payload = json.loads(row["payload"] or "{}")
                label = OBJECT_TYPE_LABELS.get(row["object_type"], row["object_type"])
                headline = self._knowledge_headline(row["object_type"], payload)
                body = self._payload_join(payload)
                if needle not in f"{label} {headline} {body}".lower():
                    continue
                hits.append(
                    SearchHit(
                        scope=label,
                        notebook_id=notebook_id,
                        label=headline,
                        text=_snippet(body or headline, needle),
                        source_id="",
                        element_id="",
                    )
                )
        return NotebookSearchResponse(query=query, hits=hits[:20])

    def load_notebook_scale_facts(
        self, notebook_id: str
    ) -> NotebookScaleFacts:
        with self.database.connect() as db:
            def one(sql: str) -> int:
                return int(db.execute(sql, (notebook_id,)).fetchone()[0])

            return NotebookScaleFacts(
                one(
                    "SELECT COALESCE(SUM(file_size), 0) FROM sources WHERE notebook_id = ?"
                ),
                one("SELECT COUNT(*) FROM sources WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM chunks WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id = ?"),
                one("SELECT COUNT(*) FROM knowledge_relations WHERE notebook_id = ?"),
            )
