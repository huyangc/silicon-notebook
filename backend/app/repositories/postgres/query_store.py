from __future__ import annotations

from typing import Any

from psycopg import Error, sql

from app.core.config import Settings
from app.models.notebooks import NotebookAnalytics
from app.models.ask import (
    NotebookSearchResponse,
    SearchHit,
)
from app.repositories.postgres._store_utils import (
    iso_timestamp,
    json_value,
    sqlite_compatible_notebook_row,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.mount_sql import MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID
from app.repositories.postgres.search import (
    notebook_element_rows,
    notebook_knowledge_rows,
    notebook_source_rows,
)
from app.services.extraction_profiles import OBJECT_TYPE_LABELS
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.notebook_scale import NotebookScaleFacts


_COUNT_IDENTIFIERS = {
    "sources": frozenset({"notebook_id"}),
    "knowledge_objects": frozenset({"notebook_id", "object_type"}),
    "conversations": frozenset({"notebook_id", "created_by"}),
    "reports": frozenset({"notebook_id", "created_by"}),
    "memory_items": frozenset({"notebook_id", "created_by"}),
    # Collection catalog: "how many knowhow tables are in scope" — a plain
    # index count over idx_knowhow_tables_nb, deliberately reusing this generic
    # primitive instead of adding a store method for one COUNT(*).
    "knowhow_tables": frozenset({"notebook_id"}),
}


def _compat_notebook_rows(rows):
    return [sqlite_compatible_notebook_row(row) for row in rows]


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
    def __init__(
        self, database: PostgresDatabase, settings: Settings | None = None
    ) -> None:
        self.database = database
        self.settings = settings

    def warm_open_path_caches(self, progress=None) -> int:
        """Warm PostgreSQL shared buffers for the same notebook-open queries."""
        with self.database.connect() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM notebooks WHERE status<>'copying' ORDER BY id"
                ).fetchall()
            ]
            total = len(ids)
            for index, notebook_id in enumerate(ids, start=1):
                try:
                    self.knowledge_type_count_rows(
                        db,
                        notebook_id,
                        ("approved", "reviewed", "project_specific", "conflict"),
                    )
                    self.pending_kg_source_count(db, notebook_id)
                    self.visible_pending_kg_source_count(db, notebook_id)
                    db.execute(
                        "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
                        (notebook_id,),
                    ).fetchone()
                except Error:
                    # A warm miss affects latency only; later reads remain exact.
                    db.rollback()
                finally:
                    if progress is not None:
                        progress(index, total)
            return total

    def invalidate_knowledge_counts(self, notebook_id: str) -> None:
        # pending-source 计数现有 seq-gated 进程缓存(codex 第4轮 P2)——安全阀:写已落但其
        # seq bump 尚未提交时清掉,与 sqlite invalidate 同款语义(非正确性必需,seq gate 自失效)。
        from app.repositories.postgres import knowledge_counts_cache
        knowledge_counts_cache.invalidate(notebook_id)

    # NotebookSummary projection primitives.  The caller deliberately retains
    # the connection so one summary is hydrated from one read snapshot.
    @staticmethod
    def count_rows(
        db: Any, table: str, column: str, value: str
    ) -> int:
        if table not in _COUNT_IDENTIFIERS or column not in _COUNT_IDENTIFIERS[table]:
            raise ValueError("unsupported PostgreSQL count identifier")
        row = db.execute(
            sql.SQL("SELECT COUNT(*) AS count FROM {} WHERE {}=%s").format(
                sql.Identifier(table), sql.Identifier(column)
            ),
            (value,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def knowledge_type_count_rows(
        db: Any, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        values = list(statuses)
        if not values:
            return []
        return db.execute(
            "SELECT object_type,COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=%s AND status=ANY(%s) GROUP BY object_type",
            (notebook_id, values),
        ).fetchall()

    @staticmethod
    def knowledge_type_count_rows_for_sources(
        db: Any,
        notebook_id: str,
        source_ids: "list[str]",
        statuses: tuple[str, ...],
    ) -> "list[dict]":
        """``[{object_type, c}]`` for objects owned by the GIVEN sources; see
        the SQLite adapter for why this takes ids rather than re-spelling the
        Memory predicate.  ``=ANY(%s)`` keeps one bound parameter per list, so
        no batching is needed here."""
        values = list(dict.fromkeys(value for value in source_ids if value))
        allowed = list(statuses)
        if not values or not allowed:
            return []
        return db.execute(
            "SELECT object_type,COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=%s AND source_id=ANY(%s) AND status=ANY(%s) "
            "GROUP BY object_type",
            (notebook_id, values, allowed),
        ).fetchall()

    @staticmethod
    def knowhow_knowledge_type_rows(
        db: Any, notebook_id: str, statuses: tuple[str, ...]
    ) -> "list[dict]":
        """Return only KO types owned by a table's hidden Knowhow source."""
        values = list(statuses)
        if not values:
            return []
        return db.execute(
            "SELECT ko.object_type,COUNT(*) AS c "
            "FROM knowhow_tables kt JOIN knowledge_objects ko "
            "ON ko.source_id=kt.hidden_source_id "
            "WHERE kt.notebook_id=%s "
            "AND ko.notebook_id=%s AND ko.status=ANY(%s) "
            "GROUP BY ko.object_type ORDER BY ko.object_type",
            (notebook_id, notebook_id, values),
        ).fetchall()

    @staticmethod
    def notebook_has_kg(db: Any, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
            "WHERE notebook_id=%s) AS exists",
            (notebook_id,),
        ).fetchone()
        return bool(row["exists"])

    @staticmethod
    def notebook_has_usable_kg(db: Any, notebook_id: str) -> bool:
        """本库是否有**可用状态**的 knowledge_objects(USABLE_STATUSES,排除 deprecated)。
        与 sqlite 同义,驱动 NotebookSummary.ask_available(见 PR#334)。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
            "WHERE notebook_id=%s AND status=ANY(%s)) AS exists",
            (notebook_id, list(USABLE_STATUSES)),
        ).fetchone()
        return bool(row["exists"])

    @staticmethod
    def notebook_has_usable_base_kg(db: Any, notebook_id: str) -> bool:
        """本库挂载的参考库中是否有任一含**可用状态** KG。与 sqlite 同义(PR#334)。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko "
            "WHERE ko.notebook_id = b.id AND ko.status=ANY(%s))) AS exists",
            (notebook_id, list(USABLE_STATUSES)),
        ).fetchone()
        return bool(row["exists"])

    @staticmethod
    def notebook_has_chunk(db: Any, notebook_id: str) -> bool:
        """本库是否有任意 chunk(文档 + knowhow 格子同表)。驱动 ask_available:
        knowhow-only 库无可见来源、无 KG 却可检索(PR#334)。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM chunks WHERE notebook_id=%s) AS exists",
            (notebook_id,),
        ).fetchone()
        return bool(row["exists"])

    @staticmethod
    def notebook_has_confirmed_memory(
        db: Any, notebook_id: str, user_id: str
    ) -> bool:
        """本库对该用户是否有**已确认** memory(confirmed-only + user-scoped,与检索侧
        及 sqlite 一致)。驱动 ask_available(PR#334)。"""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM memory_items "
            "WHERE notebook_id=%s AND created_by=%s AND status='confirmed') AS exists",
            (notebook_id, user_id),
        ).fetchone()
        return bool(row["exists"])

    @staticmethod
    def pending_kg_source_count(db: Any, notebook_id: str) -> int:
        # seq-gated 进程缓存(kg_mutation_seq),对齐 sqlite;查询判据见 cache 模块 _pending_query。
        from app.repositories.postgres import knowledge_counts_cache
        return knowledge_counts_cache.pending_source_count(db, notebook_id)

    @staticmethod
    def visible_pending_kg_source_count(db: Any, notebook_id: str) -> int:
        # checkup H6:seq-gated 缓存,前端自动拉 checkup 时大库不再每次全扫(codex 第4轮 P2)。
        from app.repositories.postgres import knowledge_counts_cache
        return knowledge_counts_cache.visible_pending_source_count(db, notebook_id)

    @staticmethod
    def sources_missing_chunks(db: Any, notebook_id: str) -> set:
        """H3(缺分块)候选集的 postgres 镜像——判据与 sqlite QueryStore.sources_missing_chunks
        逐字一致(有 elements、chunked_at IS NULL、排除 memory/knowhow 隐藏合成源);减活跃租约
        由 CheckupService 在 service 层做。见 sqlite 版 docstring 的完整设计说明。"""
        return {
            row["id"] for row in db.execute(
                "SELECT s.id AS id FROM sources s "
                "WHERE s.notebook_id = %s "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND s.chunked_at IS NULL "
                "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def sources_without_elements(db: Any, notebook_id: str) -> set:
        """H2(空源)候选集的 postgres 镜像——判据与 sqlite QueryStore.sources_without_elements
        逐字一致(parse_status 白名单 parsed/extracting/extracted、无 source_elements、排除
        memory/knowhow);减活跃租约由 CheckupService 在 service 层做。见 sqlite 版 docstring。"""
        return {
            row["id"] for row in db.execute(
                "SELECT s.id AS id FROM sources s "
                "WHERE s.notebook_id = %s "
                "AND s.source_type NOT IN ('memory', 'knowhow') "
                "AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
                "AND NOT EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)",
                (notebook_id,),
            ).fetchall()
        }

    @staticmethod
    def visible_source_count(db: Any, notebook_id: str) -> int:
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
            "WHERE notebook_id = %s AND source_type NOT IN ('memory', 'knowhow')",
            (notebook_id,),
        ).fetchone()
        return int(row["count"])

    @staticmethod
    def mounted_bases_row(db: Any, notebook_id: str):
        """本库挂载的有效参考库 + 各自是否有 KG —— 一次查询同时供 NotebookSummary 的
        base_notebooks 与 base_kg_available。"""
        return db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            "EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id) AS has_kg "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()

    @staticmethod
    def summary_notebook_row(db: Any, notebook_id: str):
        return sqlite_compatible_notebook_row(db.execute(
            "SELECT * FROM notebooks WHERE id = %s AND status != 'copying'",
            (notebook_id,),
        ).fetchone())

    @staticmethod
    def owned_notebook_rows(db: Any, user_id: str):
        return _compat_notebook_rows(db.execute(
            "SELECT * FROM notebooks WHERE created_by = %s AND status != 'copying' "
            "ORDER BY created_at ASC",
            (user_id,),
        ).fetchall())

    @staticmethod
    def joined_notebook_rows(db: Any, user_id: str):
        return _compat_notebook_rows(db.execute(
            "SELECT nb.*, u.username AS _owner_username FROM notebook_members m "
            "JOIN notebooks nb ON nb.id = m.notebook_id "
            "LEFT JOIN users u ON u.id = nb.created_by "
            "WHERE m.user_id = %s AND nb.status != 'copying' "
            "ORDER BY m.added_at ASC",
            (user_id,),
        ).fetchall())

    @staticmethod
    def memory_counts_by_owner_notebook(
        db: Any, user_id: str
    ) -> dict[tuple[str, str], int]:
        """One owner-scoped grouped query for every notebook card.

        Memory is private to ``created_by`` even when the notebook is shared;
        grouping by both privacy key and notebook id makes that scope explicit
        and avoids a per-card query.
        """
        rows = db.execute(
            "SELECT created_by, notebook_id, COUNT(*) AS c FROM memory_items "
            "WHERE created_by=%s GROUP BY created_by, notebook_id",
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
            questions = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM ask_jobs GROUP BY created_by"
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
            overrides = {
                row["k"]: int(row["v"])
                for row in db.execute(
                    "SELECT user_id AS k,upload_document_limit AS v "
                    "FROM user_profiles WHERE upload_document_limit IS NOT NULL"
                ).fetchall()
            }
            default_row = db.execute(
                "SELECT value FROM app_settings "
                "WHERE key='upload_document_limit_default'"
            ).fetchone()
        raw_default = default_row["value"] if default_row is not None else None
        try:
            global_default = int(raw_default)
        except (TypeError, ValueError):
            global_default = int(
                self.settings.user_upload_document_limit if self.settings else 20
            )
        return [
            {
                "id": user["id"],
                "username": user["username"] or user["display_name"] or user["id"],
                "role": user["role"],
                "created_at": iso_timestamp(user["created_at"]),
                "notebooks": notebooks.get(user["id"], 0),
                "sources": sources.get(user["id"], 0),
                "conversations": conversations.get(user["id"], 0),
                "questions": questions.get(user["id"], 0),
                "reports": reports.get(user["id"], 0),
                "last_active": iso_timestamp(active.get(user["id"])),
                "upload_limit": overrides.get(user["id"], global_default),
                "upload_limit_overridden": user["id"] in overrides,
            }
            for user in users
        ]

    def list_user_notebooks(self, user_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            notebooks = db.execute(
                "SELECT id, name, status, created_at, updated_at FROM notebooks "
                "WHERE created_by = %s AND status != 'copying' ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            ids = [row["id"] for row in notebooks]
            sources: dict[str, int] = {}
            conversations: dict[str, int] = {}
            questions: dict[str, int] = {}
            reports: dict[str, int] = {}
            if ids:
                placeholders = ",".join("%s" for _ in ids)
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
                questions = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM ask_jobs "
                        f"WHERE notebook_id IN ({placeholders}) AND created_by = %s "
                        f"GROUP BY notebook_id",
                        [*ids, user_id],
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
                "created_at": iso_timestamp(row["created_at"]),
                "updated_at": iso_timestamp(row["updated_at"]),
                "sources": sources.get(row["id"], 0),
                "conversations": conversations.get(row["id"], 0),
                "questions": questions.get(row["id"], 0),
                "reports": reports.get(row["id"], 0),
            }
            for row in notebooks
        ]

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        with self.database.connect() as db:
            exists = db.execute(
                "SELECT 1 FROM notebooks WHERE id = %s AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(notebook_id)
            answers_total = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM answers WHERE notebook_id = %s",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = %s AND rating = 'useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            not_useful = int(
                db.execute(
                    "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = %s AND rating = 'not_useful'",
                    (notebook_id,),
                ).fetchone()["c"]
            )
            low_rated = [
                row["question"]
                for row in db.execute(
                    "SELECT a.question FROM feedback f "
                    "JOIN answers a ON a.id = f.answer_id "
                    "WHERE f.notebook_id = %s AND f.rating = 'not_useful' "
                    "GROUP BY a.question "
                    "ORDER BY MAX(f.created_at) DESC, a.question COLLATE \"C\" ASC "
                    "LIMIT 10",
                    (notebook_id,),
                ).fetchall()
            ]
            knowledge_counts = {
                row["object_type"]: int(row["c"])
                for row in db.execute(
                    "SELECT object_type,COUNT(*) AS c FROM knowledge_objects "
                    "WHERE notebook_id=%s AND status!='deprecated' GROUP BY object_type",
                    (notebook_id,),
                ).fetchall()
            }
            # Memory-derived AND knowhow-projection hidden synthetic sources
            # (source_type IN ('memory', 'knowhow')) are excluded — this feeds
            # the /analytics 看板 parse_status distribution, a user-facing
            # surface; see visible_source_count.
            source_status_counts = {
                row["parse_status"]: int(row["c"])
                for row in db.execute(
                    "SELECT parse_status, COUNT(*) AS c FROM sources "
                    "WHERE notebook_id = %s AND source_type NOT IN ('memory', 'knowhow') "
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
                    "WHERE notebook_id = %s GROUP BY is_paper",
                    (notebook_id,),
                ).fetchall()
            }
            # missing 计数是 SourceStore.sources_missing_paper_meta 的 COUNT 镜像
            # (WHERE 子句逐字保持一致,口径漂移会立刻体现为两处不一致),走
            # idx_sources_nb_parse_status_type(notebook_id, parse_status, source_type)。
            missing = int(db.execute(
                "SELECT COUNT(*) AS c FROM sources s "
                "WHERE s.notebook_id = %s "
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
                "SELECT id, name FROM notebooks WHERE created_by = %s AND status != 'copying'",
                (user_id,),
            ).fetchall()
            name_of = {row["id"]: row["name"] for row in mine}
            notebook_ids = list(name_of)
            if notebook_ids:
                role_row = db.execute(
                    "SELECT role FROM users WHERE id = %s", (user_id,)
                ).fetchone()
                is_admin = bool(role_row) and role_row["role"] == "admin"
                reports = db.execute(
                    "SELECT id, question, notebook_id, created_at FROM reports "
                    "WHERE status IN ('intent_ready','outline_ready') "
                    "AND created_by = %s ORDER BY updated_at DESC",
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
                            "created_at": iso_timestamp(row["created_at"]),
                        }
                    )
                placeholders = ",".join("%s" for _ in notebook_ids)
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
                        sql.SQL(
                            "SELECT notebook_id,COUNT(*) AS c FROM {} "
                            "WHERE notebook_id IN ("
                        )
                        .format(sql.Identifier(table))
                        + sql.SQL(placeholders)
                        + sql.SQL(") AND {} GROUP BY notebook_id").format(
                            sql.SQL(predicate)
                        ),
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
                "SELECT * FROM notebooks WHERE id = %s AND status != 'copying'",
                (notebook_id,),
            ).fetchone()
            if notebook is None:
                raise KeyError(notebook_id)
            if not needle:
                return NotebookSearchResponse(query=query, hits=[])
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
            source_rows = notebook_source_rows(db, notebook_id, needle, cap)
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
            element_rows = notebook_element_rows(db, notebook_id, needle, cap)
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
            knowledge_rows = notebook_knowledge_rows(db, notebook_id, needle, cap)
            for row in knowledge_rows:
                payload = json_value(row["payload"], {})
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
                return int(db.execute(sql, (notebook_id,)).fetchone()["value"])

            return NotebookScaleFacts(
                one(
                    "SELECT COALESCE(SUM(file_size),0) AS value FROM sources "
                    "WHERE notebook_id=%s"
                ),
                one("SELECT COUNT(*) AS value FROM sources WHERE notebook_id=%s"),
                one("SELECT COUNT(*) AS value FROM chunks WHERE notebook_id=%s"),
                one("SELECT COUNT(*) AS value FROM knowledge_objects WHERE notebook_id=%s"),
                one("SELECT COUNT(*) AS value FROM knowledge_relations WHERE notebook_id=%s"),
            )

    def is_mounted_by_anyone(self, notebook_id: str) -> bool:
        """被任何笔记本当作参考库挂着(Task 6)—— NotebookScaleProfile.index_eligible
        的挂载分支消费。刻意不区分挂载边是否仍然「有效」(不走 mount_sql.py 的
        MOUNT_VALID_EXPR):那是解析参与集用的谓词,边失效通常是临时的,不该让
        索引跟着过期。IndexProjectionStore.is_mounted_by_anyone 是
        ScaleArtifactRuntime.eligible 侧的镜像实现,两处必须保持同一判定。"""
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM notebook_bases "
                "WHERE base_notebook_id=%s) AS exists",
                (notebook_id,),
            ).fetchone()["exists"])
