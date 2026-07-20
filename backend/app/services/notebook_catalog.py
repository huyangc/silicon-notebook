from __future__ import annotations

import json
import shutil
import sqlite3
import weakref
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

from app.models.schemas import (
    KgBuildJobStatus,
    NotebookAnalytics,
    NotebookCreate,
    NotebookRef,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    SearchHit,
)
from app.repositories.ports import KgBuildJobStorePort
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


def _delete_notebook_asset_dir(storage_dir: Path, notebook_id: str) -> None:
    """Remove the whole per-notebook pasted-image-asset directory
    (``storage_dir/assets/<notebook_id>/`` — see
    ``app.services.knowhow.assets.AssetService.path_for`` for the same path
    formula).

    ``notebook_assets`` ROWS need no explicit delete here: the column is
    ``notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE``
    (migration 16) and every connection runs with ``PRAGMA foreign_keys = ON``
    (``SqliteDatabase._new_connection``), so they disappear automatically the
    moment ``delete_row_and_orphan_embeddings`` deletes the ``notebooks`` row
    above — verified empirically (knowhow-tables PR-2+3 Task 14): unlike
    ``knowledge_embeddings`` (which has no FK and needs the explicit delete a
    few lines up), ``notebook_assets`` cascades cleanly. Only the on-disk
    FILES need this explicit cleanup (cascade never touches the filesystem).
    Mirrors ``_delete_source_file``'s exists-before-touch tolerance so a
    notebook that never had an asset uploaded (directory never created)
    deletes cleanly."""
    asset_dir = Path(storage_dir) / "assets" / notebook_id
    if asset_dir.exists():
        shutil.rmtree(asset_dir, ignore_errors=True)


def kg_build_status(row) -> KgBuildJobStatus | None:
    if row is None:
        return None
    return KgBuildJobStatus(
        job_id=row["id"],
        mode=row["mode"],
        status=row["status"],
        stage=row["stage"],
        total_sources=int(row["total_sources"]),
        completed_sources=int(row["completed_sources"]),
        failed_sources=int(row["failed_sources"]),
        error_code=row["error_code"],
        user_message=row["error_message"],
        updated_at=row["updated_at"],
    )


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

    def __init__(
        self,
        database: SqliteDatabase,
        queries: "QueryStore | None" = None,
        kg_build_jobs: "KgBuildJobStorePort | None" = None,
    ) -> None:
        self.database = database
        self.queries = queries or QueryStore(database)
        self.kg_build_jobs = kg_build_jobs

    def count(
        self, db: sqlite3.Connection, table: str, column: str, value: str
    ) -> int:
        return self.queries.count_rows(db, table, column, value)

    def knowledge_type_counts(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> Dict[str, int]:
        """{counts-dict key: count} for the 6 knowledge object_types
        from_row surfaces, via ONE GROUP BY query instead of 6 separate
        per-type COUNT(*) calls. Same USABLE_STATUSES filter and same
        zero-default for absent types as the old per-type COUNT(*) calls."""
        rows = self.queries.knowledge_type_count_rows(
            db, notebook_id, USABLE_STATUSES
        )
        by_type = {r["object_type"]: int(r["c"]) for r in rows}
        return {
            key: by_type.get(otype, 0)
            for otype, key in self._NOTEBOOK_COUNT_TYPES.items()
        }

    def has_kg(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        return self.queries.notebook_has_kg(db, notebook_id)

    def visible_source_count(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> int:
        """NotebookSummary's counts["sources"] — excludes Memory-derived
        synthetic sources (source_type='memory'); see
        QueryStore.visible_source_count. The generic ``count`` helper above
        stays unfiltered (it is a table-agnostic primitive shared with the
        facade's ``_count`` re-export / its equivalence-oracle test)."""
        return self.queries.visible_source_count(db, notebook_id)

    def count_pending_kg_sources(
        self, db: sqlite3.Connection, notebook_id: str
    ) -> int:
        """Count parsed sources that do not have a complete KG extraction."""
        return self.queries.pending_kg_source_count(db, notebook_id)

    def mounted_bases(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> "tuple[list[NotebookRef], bool]":
        """(参考库列表, 是否任一有 KG) —— 一次查询同时供 NotebookSummary 的
        base_notebooks 与 base_kg_available,避免每条 summary 各查一次。
        未挂载 → ([], False)。"""
        if db is not None:
            rows = self.queries.mounted_bases_row(db, notebook_id)
        else:
            with self.database.connect() as conn:
                rows = self.queries.mounted_bases_row(conn, notebook_id)
        refs = [
            NotebookRef(id=r["id"], name=r["name"], tier=r["tier"] or "personal")
            for r in rows
        ]
        return (refs, any(bool(r["has_kg"]) for r in rows))

    def from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        memory_count: int = 0,
    ) -> NotebookSummary:
        # 注意:kg_building/paper_meta_backfilling 仅经 get(kg_building=...,
        # paper_meta_backfilling=...) 回填为真值;list_for_user 等走 from_row 的
        # 路径恒为 False（当前无消费方读列表里的该字段）。
        counts = {
            "sources": self.visible_source_count(connection, row["id"]),
            "memories": memory_count,
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

        base_refs, base_has_kg = self.mounted_bases(row["id"], connection)
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
            base_notebooks=base_refs,
            kg_pending_sources=self.count_pending_kg_sources(connection, row["id"]),
            is_shared=bool(row["is_shared"]) if "is_shared" in keys else False,
        )

    def get(
        self,
        notebook_id: str,
        *,
        kg_building: bool = False,
        paper_meta_backfilling: bool = False,
        user_id: str | None = None,
    ) -> NotebookSummary:
        """status='copying' rows (copy_notebook's in-progress sentinel, P1-4)
        are treated as not-yet-existing: every catalog mutation guards with
        get(...) before acting, and a half-copied notebook must not be usable
        by any of them until the copy finishes."""
        with self.database.connect() as db:
            row = self.queries.summary_notebook_row(db, notebook_id)
            if row is None:
                raise KeyError(notebook_id)
            memory_counts = (
                self.queries.memory_counts_by_owner_notebook(db, user_id)
                if user_id is not None
                else {}
            )
            summary = self.from_row(
                db,
                row,
                memory_count=memory_counts.get((user_id, notebook_id), 0),
            )
            job_row = (
                self.kg_build_jobs.latest_on(db, notebook_id)
                if self.kg_build_jobs is not None
                else None
            )
        summary.kg_build = kg_build_status(job_row)
        summary.kg_building = kg_building or (
            summary.kg_build is not None
            and summary.kg_build.status == "running"
        )
        summary.paper_meta_backfilling = paper_meta_backfilling
        return summary

    def list_for_user(self, user_id: str) -> list[NotebookSummary]:
        """自有库(access=owner)∪ 经只读共享加入的库(access=reader)。

        status='copying' 是 copy_notebook 分批写入期间的哨兵状态(P1-4),半拷贝
        的副本必须排除,不然用户能看到/点进一个字段还没写全的空壳 notebook。
        """
        out: List[NotebookSummary] = []
        with self.database.connect() as db:
            memory_counts = self.queries.memory_counts_by_owner_notebook(db, user_id)
            rows = self.queries.owned_notebook_rows(db, user_id)
            for row in rows:
                nb = self.from_row(
                    db,
                    row,
                    memory_count=memory_counts.get((user_id, row["id"]), 0),
                )
                nb.access = "owner"
                out.append(nb)
            joined = self.queries.joined_notebook_rows(db, user_id)
            for row in joined:
                nb = self.from_row(
                    db,
                    row,
                    memory_count=memory_counts.get((user_id, row["id"]), 0),
                )
                nb.access = "reader"
                nb.shared_from = row["_owner_username"] or ""
                out.append(nb)
        return out


class NotebookCatalogService:
    """Notebook catalog orchestration over the row store, the summary
    projection and the Task-7 query adapter.  Owns the in-process
    kg_building flag set (进程内; 重启后天然为空=未构建, 无需 reconcile) that
    get_notebook reflects into NotebookSummary.kg_building. Mirrors the same
    reflect-into-summary wiring for paper_meta_backfilling, sourced from the
    injected source_ingestion service's own in-process dict (see
    NotebookSummary.paper_meta_backfilling)."""

    def __init__(
        self,
        store: NotebookStore,
        summaries: NotebookSummaryQuery,
        queries: QueryStore,
        identity: IdentityStore,
        storage_dir: Callable[[], Path],
    ) -> None:
        """``storage_dir`` is a zero-arg callable resolving the LIVE storage
        root (knowhow-tables PR-2+3 Task 14) — a callable rather than a Path
        snapshot because the facade's ``storage_dir`` is a mutable property
        (tests monkeypatch it per instance), exactly the convention
        ``NotebookCopyService`` already uses for the same collaborator.
        ``delete_notebook`` reads it to sweep the notebook's pasted-image
        asset directory; injected at construction so EVERY caller gets the
        cleanup — routes reach this service directly via
        ``deps.notebook_catalog_repository()`` (``repo._runtime.catalog``),
        never through the facade delegate."""
        self._store = store
        self._summaries = summaries
        self._queries = queries
        self._identity = identity
        self._storage_dir = storage_dir
        self.kg_building: set = set()
        # Injected post-construction by RepositoryRuntime.wire_source_ingestion()
        # once SourceIngestionService exists (mirrors memory_retriever below —
        # this class is constructed before source ingestion is wired). Held as
        # a WEAKREF, not a strong ref: SourceIngestionService closes over the
        # facade (`self._write`, `self.source_elements`, ...) for its own
        # wiring, so a strong ref here would let anything that holds `catalog`
        # (e.g. ScaleArtifactRuntime, which callbacks into
        # catalog.get_notebook) transitively keep the whole facade alive —
        # exactly what test_scale_artifact_runtime's retention tests guard
        # against. The service outlives every real request, so the weakref
        # is always live in practice; it only reads as dead in that same
        # deliberate GC/retention test.
        self.source_ingestion: "weakref.ReferenceType | None" = None
        self.memory_retriever = None

    def _paper_meta_backfilling(self, notebook_id: str) -> bool:
        service = self.source_ingestion() if self.source_ingestion is not None else None
        return False if service is None else service.paper_meta_backfilling(notebook_id)

    def list_notebook_templates(self) -> list[NotebookTemplate]:
        return [NotebookTemplate(**t) for t in NOTEBOOK_TEMPLATES]

    def list_notebooks(self) -> list[NotebookSummary]:
        return self._summaries.list_for_user(self._identity.current_user().id)

    def warm_open_path_caches(self, progress=None) -> int:
        """Prime the per-process open-path count caches (``knowledge_counts_cache``
        — the per-type GROUP BY, the pending-source correlated count and the chunk
        count) for every notebook so the first login after a restart is served
        warm instead of paying the cold recompute. Called by the startup-readiness
        warm-up; best-effort per notebook inside ``warm_all``. Returns the count."""
        from app.repositories.sqlite import knowledge_counts_cache
        with self._summaries.database.connect() as db:
            return knowledge_counts_cache.warm_all(db, progress)

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        user_id = self._identity.current_user().id
        notebook_id = self._store.create_row(payload, user_id)
        return self._summaries.get(notebook_id, user_id=user_id)

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self._summaries.get(
            notebook_id,
            kg_building=notebook_id in self.kg_building,
            paper_meta_backfilling=self._paper_meta_backfilling(notebook_id),
            user_id=self._identity.current_user().id,
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
        # DB deletion is committed above; only then remove files on disk —
        # source files first, then the notebook's pasted-image asset
        # directory (knowhow-tables PR-2+3 Task 14; unconditional, from the
        # construction-injected live storage root, so the real HTTP delete
        # route — which reaches this service directly, not via the facade —
        # gets the cleanup too).
        for file_path in file_paths:
            _delete_source_file(file_path)
        _delete_notebook_asset_dir(self._storage_dir(), notebook_id)

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
        response = self._queries.search_notebook(notebook_id, query)
        if self.memory_retriever is None:
            return response
        memories = self.memory_retriever.notebook_memory_hits(
            self._identity.current_user().id, notebook_id, query, 8
        )
        memory_hits = [SearchHit(
            scope="Memory",
            notebook_id=notebook_id,
            label=item.title,
            text=item.text[:400],
            source_id="",
            element_id="",
            memory_id=item.memory_id,
            provenance=dict(item.provenance),
        ) for item in memories]
        if not memory_hits:
            return response
        return NotebookSearchResponse(
            query=response.query,
            hits=[*response.hits[: max(0, 20 - len(memory_hits))], *memory_hits][:20],
        )
