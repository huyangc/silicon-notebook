from __future__ import annotations

from typing import Any

from psycopg import Error, sql

from app.core.activity_time import (
    UNRESOLVED_INSTANT,
    UNRESOLVED_INSTANT_ISO,
    cursor_instant_text,
    parse_activity_instant,
)
from app.core.config import Settings
from app.models.notebooks import NotebookAnalytics
from app.models.ask import (
    NotebookSearchResponse,
    SearchHit,
)
from app.models.sources import (
    extraction_warning_text,
    has_pdf_python_fallback_warning,
    paper_meta_status,
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
from app.repositories.postgres.source_store import VISIBLE_SOURCE_TYPES_PREDICATE
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


def _absolute_instant(column: str) -> str:
    """PostgreSQL 侧「绝对时刻」的比较形态,用在 ORDER BY / 范围 / 游标三处——
    ``sqlite/query_store.py::_absolute_instant`` 的孪生实现。

    这里的 ``created_at`` 是原生 ``timestamptz``,不需要 SQLite 那侧的 ``julianday()``
    折算;``COALESCE`` 这一半却**同样不可省**:``ask_jobs.created_at`` 在 PG schema 里
    是**可空**的(``0001_initial.sql``,该列无 NOT NULL,而 ``sources`` / ``reports``
    两张表有),而停机 importer 的 ``_parse_timestamp("") -> None`` 会把 SQLite 那侧
    ``TEXT NOT NULL DEFAULT ''`` 的空串行迁成 NULL。

    少了它有两处会炸,而且是同一行同时炸两次:
    * ``ORDER BY created_at DESC`` 在 PG 默认 **NULLS FIRST**,这行必定排在第 1 页;
    * Python 归并键 ``pool.sort`` 里混进 ``None`` 直接
      ``TypeError: '<' not supported between instances of 'NoneType' and 'datetime'``。

    于是该用户的活动流每次请求都 500。兜底值与 SQLite 侧**共用同一个哨兵**
    (``UNRESOLVED_INSTANT_ISO``),两边因此把这类行排到同一个位置(最末),游标也回传
    同一个可往返的串。刻意不用 ``'-infinity'``:它排序对、却写不成 ISO 游标。
    """
    return f"COALESCE({column}, TIMESTAMPTZ '{UNRESOLVED_INSTANT_ISO}')"


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
                # VISIBLE_SOURCE_TYPES_PREDICATE 不可省(与 SQLite 侧逐字同一条理由):
                # 它是「面向用户可见来源数」的单一真源,少了它 memory / knowhow 投影行
                # 会被算进表头,而展开的清单带谓词、只列可见来源。
                sources = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM sources "
                        f"WHERE notebook_id IN ({placeholders}) "
                        f"AND {VISIBLE_SOURCE_TYPES_PREDICATE} GROUP BY notebook_id",
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

    def notebook_exists_for_owner(self, notebook_id: str, user_id: str) -> bool:
        """SQLite 孪生实现,谓词逐字相同(见那边的说明)。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebooks WHERE id = %s AND created_by = %s "
                "AND status != 'copying'",
                (notebook_id, user_id),
            ).fetchone()
        return row is not None

    def list_user_activity(
        self,
        user_id: str,
        *,
        notebook_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        before_ts: str | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """PostgreSQL twin of the SQLite ``list_user_activity`` — same fields,
        same (created_at DESC, id DESC) keyset semantics, same per-type bounded
        + in-Python merge (no three-way UNION; see the SQLite implementation's
        docstring and
        docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md
        §4.1 for the rationale).

        ``created_at`` is ``timestamptz`` here (text in SQLite), so the cursor
        and the ``since``/``until`` range bounds are each normalized once to an
        aware UTC ``datetime`` and bound natively rather than compared as text.
        That normalization is the **shared** ``parse_activity_instant`` — the
        very same function the SQLite backend calls before handing its bound to
        ``julianday()``. Both backends therefore agree on what
        ``"2026-07-31T16:00:00.000Z"`` means, and both raise ``ValueError`` on a
        malformed bound instead of one silently returning a page while the other
        500s (see ``app.core.activity_time`` for the contract, including why a
        naive bound is read as UTC).

        The in-Python merge sorts on the native per-row value (``datetime``
        here, SQLite's own ``julianday()`` output there — in each case exactly
        the value that backend's ``ORDER BY`` used) and only stringifies via
        ``iso_timestamp`` when building the output dict, so no comparison ever
        depends on ``.isoformat()``'s variable-width fractional-second
        rendering.

        Scope is owner-only for all three types (same as the SQLite twin):
        rows the user produced inside *someone else's* shared notebook are
        deliberately absent — the usage totals count them, this expanded feed
        does not.
        """
        limit = max(1, min(200, int(limit)))
        fetch_limit = limit + 1
        empty: dict[str, Any] = {"items": [], "has_more": False, "next_cursor": None}
        cursor_active = before_ts is not None and before_id is not None
        before_dt = (
            parse_activity_instant(before_ts, field="before_ts")
            if cursor_active else None
        )
        since_dt = (
            parse_activity_instant(since, field="since") if since is not None else None
        )
        until_dt = (
            parse_activity_instant(until, field="until") if until is not None else None
        )

        def _range_and_cursor_clause(prefix: str) -> tuple[str, list[Any]]:
            instant = _absolute_instant(f"{prefix}created_at")
            clause = ""
            params: list[Any] = []
            if since_dt is not None:
                clause += f" AND {instant} >= %s"
                params.append(since_dt)
            if until_dt is not None:
                clause += f" AND {instant} < %s"
                params.append(until_dt)
            if cursor_active:
                clause += (
                    f" AND ({instant} < %s OR "
                    f"({instant} = %s AND {prefix}id COLLATE \"C\" < %s))"
                )
                params.extend([before_dt, before_dt, before_id])
            return clause, params

        with self.database.connect() as db:
            if notebook_id is not None:
                owned = db.execute(
                    "SELECT 1 FROM notebooks WHERE id = %s AND created_by = %s "
                    "AND status != 'copying'",
                    (notebook_id, user_id),
                ).fetchone()
                if owned is None:
                    return empty
                owned_notebook_ids = [notebook_id]
            else:
                owned_notebook_ids = [
                    row["id"]
                    for row in db.execute(
                        "SELECT id FROM notebooks WHERE created_by = %s "
                        "AND status != 'copying'",
                        (user_id,),
                    ).fetchall()
                ]
            if not owned_notebook_ids:
                # 三类都按自有笔记本收窄,一个都没有就没有任何活动可列。
                return empty
            owned_placeholders = ",".join("%s" for _ in owned_notebook_ids)

            # 1. 提问:created_by 之外还收窄到自有笔记本(owner-only,理由同 SQLite 侧)。
            # notebook_id 已在上面解析成 owned_notebook_ids == [notebook_id],这条 IN
            # 同时兑现了「按库过滤」和「归属校验」,不需要第二条谓词。
            ask_params: list[Any] = [user_id, *owned_notebook_ids]
            ask_range_clause, ask_range_params = _range_and_cursor_clause("")
            ask_params.extend(ask_range_params)
            ask_rows = db.execute(
                "SELECT id, notebook_id, created_at, asked_at, conversation_id, "
                "question, mode, status, answer_id, error FROM ask_jobs "
                f"WHERE created_by = %s AND notebook_id IN ({owned_placeholders})"
                f"{ask_range_clause} "
                f"ORDER BY {_absolute_instant('created_at')} DESC, "
                "id COLLATE \"C\" DESC LIMIT %s",
                [*ask_params, fetch_limit],
            ).fetchall()

            # 2. 来源:sources 没有 created_by 列,靠"该用户自己的笔记本 id"限定范围
            # (与 list_user_notebooks 同口径),VISIBLE_SOURCE_TYPES_PREDICATE 排除
            # 隐藏合成源,source_paper_meta LEFT JOIN 一次带出 is_paper/paper_title。
            # s.doc_type / s.error_message 一并带出仅用于 Python 侧派生
            # paper_meta_status / parse_quality_warning,两者都不作为返回字段。
            source_params: list[Any] = list(owned_notebook_ids)
            source_range_clause, source_range_params = _range_and_cursor_clause("s.")
            source_params.extend(source_range_params)
            source_rows = db.execute(
                "SELECT s.id, s.notebook_id, s.created_at, s.title, s.file_name, "
                "s.source_type, s.parse_status, s.status, s.error_message, "
                "s.doc_type, m.is_paper, m.paper_title "
                "FROM sources s LEFT JOIN source_paper_meta m ON m.source_id = s.id "
                f"WHERE s.notebook_id IN ({owned_placeholders}) "
                f"AND {VISIBLE_SOURCE_TYPES_PREDICATE}{source_range_clause} "
                f"ORDER BY {_absolute_instant('s.created_at')} DESC, "
                "s.id COLLATE \"C\" DESC LIMIT %s",
                [*source_params, fetch_limit],
            ).fetchall()

            # 2b. extraction_warning 是「最近一次 extraction_runs.error_message 里的
            # windows_failed=N/T 标记」派生态,单独一条批量查询(不是逐行 N+1),只对
            # 本页已取回的 source id 集合取一次。ORDER BY 与 SourceStore.
            # sources_from_rows 逐字相同(source_id COLLATE "C", created_at DESC,
            # ordinal DESC):ordinal 存在的唯一理由就是给并列 created_at 提供确定序,
            # 少了它「最近一次」在同秒两条记录下就不确定。
            latest_extraction_error: dict[str, str] = {}
            source_ids_on_page = [row["id"] for row in source_rows]
            if source_ids_on_page:
                ph2 = ",".join("%s" for _ in source_ids_on_page)
                for r in db.execute(
                    "SELECT source_id, error_message FROM extraction_runs "
                    f"WHERE source_id IN ({ph2}) "
                    "ORDER BY source_id COLLATE \"C\", created_at DESC, ordinal DESC",
                    source_ids_on_page,
                ).fetchall():
                    latest_extraction_error.setdefault(
                        r["source_id"], r["error_message"] or ""
                    )

            # 3. 报告:与提问同口径(created_by + 自有笔记本)。understanding_json 一并
            # 带出仅用于 Python 侧提取 generation_started_at(镜像 ReportStore.
            # row_to_dict 同一套 jsonb 提取,不发明第二套写法),本身不作为返回字段。
            report_params: list[Any] = [user_id, *owned_notebook_ids]
            report_range_clause, report_range_params = _range_and_cursor_clause("")
            report_params.extend(report_range_params)
            report_rows = db.execute(
                "SELECT id, notebook_id, created_at, updated_at, question, depth, "
                "status, understanding_json FROM reports "
                f"WHERE created_by = %s AND notebook_id IN ({owned_placeholders})"
                f"{report_range_clause} "
                f"ORDER BY {_absolute_instant('created_at')} DESC, "
                "id COLLATE \"C\" DESC LIMIT %s",
                [*report_params, fetch_limit],
            ).fetchall()

        pool: list[dict[str, Any]] = []
        for row in ask_rows:
            pool.append(
                {
                    "type": "ask",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "asked_at": row["asked_at"],
                    "conversation_id": row["conversation_id"],
                    "question": row["question"],
                    "mode": row["mode"],
                    "status": row["status"],
                    "answer_id": row["answer_id"],
                    "error": row["error"],
                }
            )
        for row in source_rows:
            pool.append(
                {
                    "type": "source",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "title": row["title"],
                    "file_name": row["file_name"],
                    "source_type": row["source_type"],
                    "parse_status": row["parse_status"],
                    "status": row["status"],
                    # 刻意**不**返回 sources.error_message(可能带服务端绝对路径);
                    # 安全事实按 ScopedSourceDetail 的既有做法换成 parse_failed 布尔。
                    # 与 SQLite 侧逐字同形,理由见那边的注释。
                    "parse_failed": row["parse_status"] == "failed",
                    "is_paper": bool(row["is_paper"]),
                    "paper_title": row["paper_title"] or "",
                    "extraction_warning": extraction_warning_text(
                        latest_extraction_error.get(row["id"])
                    ),
                    "parse_quality_warning": has_pdf_python_fallback_warning(
                        row["error_message"]
                    ),
                    "paper_meta_status": paper_meta_status(
                        row["is_paper"], row["source_type"], row["doc_type"],
                        row["parse_status"],
                    ),
                }
            )
        for row in report_rows:
            understanding = json_value(row["understanding_json"], {})
            generation_started_at = str(
                understanding.get("_generation_started_at", "") or ""
            )
            pool.append(
                {
                    "type": "report",
                    "id": row["id"],
                    "notebook_id": row["notebook_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "question": row["question"],
                    "depth": int(row["depth"]),
                    "status": row["status"],
                    "generation_started_at": generation_started_at,
                }
            )

        # created_at 在归并期间保持原生 datetime(与 SQLite 侧字符串同构:两者都在各
        # 自后端的结果集里同质、可直接比较),只在写出字段前经 iso_timestamp 转字符串,
        # 避免依赖 isoformat() 的变长小数位表示做字符串比较。
        #
        # ⚠ 归并键必须与 SQL 的 ORDER BY 用**同一个** COALESCE 兜底,否则可空的
        # ask_jobs.created_at 会让 None 混进比较元组、直接 TypeError(SQL 已经把这类行
        # 排到最末,Python 这一半漏掉就白排了)。哨兵只进排序键,不进输出字段——
        # 输出仍走下面的 iso_timestamp,NULL 照旧渲染成空串。
        pool.sort(
            key=lambda item: (item["created_at"] or UNRESOLVED_INSTANT, item["id"]),
            reverse=True,
        )
        has_more = (
            len(ask_rows) > limit
            or len(source_rows) > limit
            or len(report_rows) > limit
            or len(pool) > limit
        )
        page = pool[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            # 与 SQLite 侧同一条:游标只发**自己解析得回来**的值。NULL created_at 经
            # iso_timestamp 会变成空串,原样发出去会让下一页在 parse_activity_instant
            # 抛 ValueError;cursor_instant_text 对它回传哨兵——正是该行在
            # _absolute_instant 里实际参与排序的那个值,游标因此仍指向同一个位置。
            next_cursor = {
                "ts": cursor_instant_text(last["created_at"]),
                "id": last["id"],
            }
        for item in page:
            # created_at 之外,只有 report 的 updated_at 是 timestamptz;asked_at 在
            # PostgreSQL 侧本就是 text COLLATE "C"(见 migration 0013),无需转换。
            item["created_at"] = iso_timestamp(item["created_at"])
            if item["type"] == "report":
                item["updated_at"] = iso_timestamp(item["updated_at"])
        return {"items": page, "has_more": has_more, "next_cursor": next_cursor}

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
