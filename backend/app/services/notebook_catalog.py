from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, List

from app.models.schemas import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
)
# Canonical implementation lives with the SourceFileStore (Task 11); the
# private alias keeps this module's delete_notebook cleanup call sites and
# historical importers unchanged.
from app.repositories.source_files import delete_source_file as _delete_source_file
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.identity_store import IdentityStore
from app.repositories.sqlite.notebook_store import NotebookStore, USABLE_STATUSES
from app.repositories.sqlite.query_store import QueryStore
from app.services.notebook_templates import NOTEBOOK_TEMPLATES


def _created_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        dt = datetime.now()
    return f"{dt.year}年{dt.month}月{dt.day}日"


class NotebookSummaryQuery:
    """Cross-table NotebookSummary projection: knowledge-type counts, base-KG
    availability and pending-source aggregation over an open connection."""

    # object_type -> counts-dict key mapping shared by from_row's GROUP BY
    # aggregation (C5: N+1 fix — was 6 separate COUNT(*) queries per notebook,
    # one per object_type; a single GROUP BY object_type query gets all 6 in
    # one round trip, restricted to USABLE_STATUSES same as the old per-type
    # queries).  The facade re-exports this map as `_NOTEBOOK_COUNT_TYPES`.
    _NOTEBOOK_COUNT_TYPES: Dict[str, str] = {
        "rule": "rules", "case": "cases", "checklist": "checklist_items",
        "method": "methods", "risk": "risks", "glossary": "glossary",
    }

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database

    def count(
        self, db: sqlite3.Connection, table: str, column: str, value: str
    ) -> int:
        row = db.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",
            (value,),
        ).fetchone()
        return int(row["count"])

    def knowledge_type_counts(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> Dict[str, int]:
        """{counts-dict key: count} for the 6 knowledge object_types
        from_row surfaces, via ONE GROUP BY query instead of 6 separate
        per-type COUNT(*) calls. Same USABLE_STATUSES filter and same
        zero-default for absent types as the old per-type COUNT(*) calls."""
        placeholders = ",".join("?" for _ in USABLE_STATUSES)
        rows = db.execute(
            f"SELECT object_type, COUNT(*) AS c FROM knowledge_objects "
            f"WHERE notebook_id = ? AND status IN ({placeholders}) "
            f"GROUP BY object_type",
            (notebook_id, *USABLE_STATUSES),
        ).fetchall()
        by_type = {r["object_type"]: int(r["c"]) for r in rows}
        return {
            key: by_type.get(otype, 0)
            for otype, key in self._NOTEBOOK_COUNT_TYPES.items()
        }

    def has_kg(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)",
            (notebook_id,),
        ).fetchone()
        return bool(row[0])

    def count_pending_kg_sources(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> int:
        """Count sources in the notebook that are PARSED (have ≥1 source_elements row)
        but have NO KG (no knowledge_objects row with that source_id)."""
        row = db.execute(
            """
            SELECT COUNT(*) FROM sources s
            WHERE s.notebook_id = ?
              AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id)
              AND NOT EXISTS (SELECT 1 FROM knowledge_objects k WHERE k.source_id = s.id AND k.source_id != '')
            """,
            (notebook_id,),
        ).fetchone()
        return int(row[0])

    def base_notebook_info(
        self, db: "sqlite3.Connection | None" = None
    ) -> "tuple[str, bool]":
        """(基准库名, 是否有 KG) —— 一次查询同时供 NotebookSummary 的 base_notebook_name
        与 base_kg_available,避免每条 summary 各查一次(net-zero 于原 base_kg_available)。
        基准库全局唯一(mark_notebook_base 会降级其它 tier='base'),取最早创建者;has_kg
        沿用 _any_base_notebook_has_kg 相同的 EXISTS 语义,保证 base_kg_available 值不变。
        无基准库 → ("", False)。"""
        sql = ("SELECT nb.name, "
               "EXISTS(SELECT 1 FROM knowledge_objects ko "
               "JOIN notebooks b ON b.id = ko.notebook_id WHERE b.tier = 'base') "
               "FROM notebooks nb WHERE nb.tier = 'base' "
               "ORDER BY nb.created_at ASC LIMIT 1")
        if db is not None:
            row = db.execute(sql).fetchone()
        else:
            with self.database.connect() as conn:
                row = conn.execute(sql).fetchone()
        if not row:
            return ("", False)
        return (row[0] or "", bool(row[1]))

    def from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> NotebookSummary:
        # 注意:kg_building 仅经 get(kg_building=...) 回填为真值;list_for_user 等走
        # from_row 的路径恒为 False（当前无消费方读列表里的该字段）。
        counts = {
            "sources": self.count(connection, "sources", "notebook_id", row["id"]),
            **self.knowledge_type_counts(connection, row["id"]),
        }
        keys = row.keys()

        def _list(field: str) -> List[str]:
            if field not in keys or not row[field]:
                return []
            try:
                value = json.loads(row[field])
                return [str(v) for v in value] if isinstance(value, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        base_name, base_has_kg = self.base_notebook_info(connection)
        return NotebookSummary(
            id=row["id"],
            name=row["name"],
            purpose=row["purpose"],
            primary_domain=row["primary_domain"],
            status=row["status"],
            counts=counts,
            created_label=_created_label(row["created_at"]),
            target_users=row["target_users"] if "target_users" in keys else "",
            expected_questions=_list("expected_questions"),
            source_types=_list("source_types"),
            taxonomy=_list("taxonomy"),
            access_scope=row["access_scope"] if "access_scope" in keys else "",
            tier=row["tier"] if "tier" in keys else "personal",
            kg_ready=self.has_kg(connection, row["id"]),
            base_kg_available=base_has_kg,
            base_notebook_name=base_name,
            kg_pending_sources=self.count_pending_kg_sources(connection, row["id"]),
        )

    def get(
        self, notebook_id: str, *, kg_building: bool = False
    ) -> NotebookSummary:
        """status='copying' rows (copy_notebook's in-progress sentinel, P1-4)
        are treated as not-yet-existing: every catalog mutation guards with
        get(...) before acting, and a half-copied notebook must not be usable
        by any of them until the copy finishes."""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM notebooks WHERE id = ? AND status != 'copying'",
                (notebook_id,)).fetchone()
            if row is None:
                raise KeyError(notebook_id)
            summary = self.from_row(db, row)
        summary.kg_building = kg_building
        return summary

    def list_for_user(self, user_id: str) -> list[NotebookSummary]:
        """自有库(access=owner)∪ 经只读共享加入的库(access=reader)。

        status='copying' 是 copy_notebook 分批写入期间的哨兵状态(P1-4),半拷贝
        的副本必须排除,不然用户能看到/点进一个字段还没写全的空壳 notebook。
        """
        out: List[NotebookSummary] = []
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM notebooks WHERE created_by = ? AND status != 'copying' "
                "ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            for row in rows:
                nb = self.from_row(db, row)
                nb.access = "owner"
                out.append(nb)
            joined = db.execute(
                "SELECT nb.*, u.username AS _owner_username FROM notebook_members m "
                "JOIN notebooks nb ON nb.id = m.notebook_id "
                "LEFT JOIN users u ON u.id = nb.created_by "
                "WHERE m.user_id = ? AND nb.status != 'copying' "
                "ORDER BY m.added_at ASC", (user_id,)).fetchall()
            for row in joined:
                nb = self.from_row(db, row)
                nb.access = "reader"
                nb.shared_from = row["_owner_username"] or ""
                out.append(nb)
        return out


class NotebookCatalogService:
    """Notebook catalog orchestration over the row store, the summary
    projection and the Task-7 query adapter.  Owns the in-process
    kg_building flag set (进程内; 重启后天然为空=未构建, 无需 reconcile) that
    get_notebook reflects into NotebookSummary.kg_building."""

    def __init__(
        self,
        store: NotebookStore,
        summaries: NotebookSummaryQuery,
        queries: QueryStore,
        identity: IdentityStore,
    ) -> None:
        self._store = store
        self._summaries = summaries
        self._queries = queries
        self._identity = identity
        self.kg_building: set = set()

    def list_notebook_templates(self) -> list[NotebookTemplate]:
        return [NotebookTemplate(**t) for t in NOTEBOOK_TEMPLATES]

    def list_notebooks(self) -> list[NotebookSummary]:
        return self._summaries.list_for_user(self._identity.current_user().id)

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        notebook_id = self._store.create_row(
            payload, self._identity.current_user().id
        )
        return self._summaries.get(notebook_id)

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self._summaries.get(
            notebook_id, kg_building=notebook_id in self.kg_building
        )

    def update_notebook(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> NotebookSummary:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.update_row(notebook_id, payload)
        return self.get_notebook(notebook_id)

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        file_paths = self._store.delete_row_and_orphan_embeddings(notebook_id)
        # DB deletion is committed above; only then remove files on disk.
        for file_path in file_paths:
            _delete_source_file(file_path)

    def mark_notebook_base(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.set_tier(notebook_id, "base")

    def set_notebook_personal(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.set_tier(notebook_id, "personal")

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        return self._queries.notebook_analytics(notebook_id)

    def search_notebook(
        self, notebook_id: str, query: str
    ) -> NotebookSearchResponse:
        return self._queries.search_notebook(notebook_id, query)
