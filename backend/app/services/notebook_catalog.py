from __future__ import annotations

import json
import shutil
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

from app.models.kg import KgBuildJobStatus
from app.models.notebooks import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookRef,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
)
from app.models.ask import NotebookSearchResponse, SearchHit
from app.core import diagnostics_runtime as diagnostics
from app.core.query_syntax import strip_accepted_quote_markers
from app.repositories.ports import (
    IdentityStorePort,
    KgBuildJobStorePort,
    NotebookStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
)
# Canonical implementation lives with the SourceFileStore (Task 11); the
# private alias keeps this module's delete_notebook cleanup call sites and
# historical importers unchanged.
from app.repositories.source_files import delete_source_file as _delete_source_file
from app.services.knowledge_contracts import USABLE_STATUSES
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
        database: RepositoryDatabasePort,
        queries: QueryStorePort,
        kg_build_jobs: "KgBuildJobStorePort | None" = None,
    ) -> None:
        self.database = database
        self.queries = queries
        self.kg_build_jobs = kg_build_jobs

    def count(
        self, db: object, table: str, column: str, value: str
    ) -> int:
        return self.queries.count_rows(db, table, column, value)

    def knowledge_type_counts(
        self, db: object, notebook_id: str
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

    def has_kg(self, db: object, notebook_id: str) -> bool:
        return self.queries.notebook_has_kg(db, notebook_id)

    def visible_source_count(
        self, db: object, notebook_id: str
    ) -> int:
        """NotebookSummary's counts["sources"] — excludes Memory-derived and
        Knowhow-table synthetic sources (source_type IN ('memory', 'knowhow')); see
        QueryStore.visible_source_count. The generic ``count`` helper above
        stays unfiltered (it is a table-agnostic primitive shared with the
        facade's ``_count`` re-export / its equivalence-oracle test)."""
        return self.queries.visible_source_count(db, notebook_id)

    def count_pending_kg_sources(
        self, db: object, notebook_id: str
    ) -> int:
        """Count every physical parsed source without a complete KG extraction."""
        return self.queries.pending_kg_source_count(db, notebook_id)

    def visible_pending_kg_sources(
        self, db: object, notebook_id: str
    ) -> int:
        """Count user-visible parsed sources without a complete KG extraction.

        Memory-derived and Knowhow-table synthetic sources are excluded.
        """
        return self.queries.visible_pending_kg_source_count(db, notebook_id)

    def mounted_bases(
        self, notebook_id: str, db: "object | None" = None
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
        connection: object,
        row: Any,
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
            kg_pending_sources=self.visible_pending_kg_sources(
                connection, row["id"]
            ),
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
            # ask_available: 该库能否在任一模式下产出有据回答(见 NotebookSummary 字段注释)。
            # 判据对齐检索口径:KG 只算**可用状态**(USABLE_STATUSES,排除 deprecated),故不
            # 直接用 kg_ready/base_kg_available(含 deprecated),而用 usable 版查询。短路:
            # has_chunk 覆盖绝大多数库(可见来源 + knowhow 格子;可用活跃 KG 必有 chunk),
            # 放最前;usable-KG 查询用已算好的 kg_ready/base_kg_available 做便宜预过滤(为假
            # 则连查都免了);confirmed-memory 查询垫底。仅此单库路径回填;列表投影保持默认。
            #
            # 四个判据里**前三个是本地证据、第四个是参考库证据**,所以拆成两步求值:先短路
            # 求出本地那半(local_evidence_available),参考库那条只在本地为假时才查。
            # ask_available 的取值与语义**逐字不变**(a or b or d) or c ≡ a or b or c or d。
            #
            # 零新增往返(效率是本仓库一等约束,这条路径就是「打开笔记本卡 5-6 秒」的现场):
            #   * 前三个分支本来就要为 ask_available 求值,结果直接复用,不重查。
            #   * confirmed-memory 那条新加了一个**已在手**的便宜预过滤 counts["memories"]
            #     —— 它是上面 memory_counts_by_owner_notebook 的结果(本函数无条件已查),
            #     按 (created_by, notebook_id) 分组的**全状态** COUNT(*),是 confirmed 的
            #     严格超集:为 0 即绝无 confirmed,直接免掉那次 EXISTS。形态与 kg_ready /
            #     base_kg_available 给 usable-KG 查询做预过滤完全一致。
            #   * 唯一被交换的是 C 与 D 的先后。逐形态对账(A=has_chunk, B=usable KG,
            #     C=usable base KG, D=confirmed memory):有 chunk 的库(绝大多数)恒 1 次查询,
            #     与改前一致;最坏情形仍是 4 次,与改前一致;「零 memory 行 + 需要走到 D」的
            #     库(如刚建的空库)由新预过滤**省下** 1 次;「c 真且 d 假」的库多付 1 次 D
            #     —— 那是为了如实回答 local_evidence_available 必须付的那一次(local 的取值
            #     由 D 决定,c 再真也代替不了它),且已被预过滤收窄到「本库对该用户有
            #     memory 行但一条都没确认」这一形态。
            local_evidence = bool(
                self.queries.notebook_has_chunk(db, notebook_id)
                or (
                    summary.kg_ready
                    and self.queries.notebook_has_usable_kg(db, notebook_id)
                )
                or (
                    int(summary.counts.get("memories", 0)) > 0
                    and user_id is not None
                    and self.queries.notebook_has_confirmed_memory(
                        db, notebook_id, user_id
                    )
                )
            )
            summary.local_evidence_available = local_evidence
            summary.ask_available = local_evidence or bool(
                summary.base_kg_available
                and self.queries.notebook_has_usable_base_kg(db, notebook_id)
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
        store: NotebookStorePort,
        summaries: NotebookSummaryQuery,
        queries: QueryStorePort,
        identity: IdentityStorePort,
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
        return self._queries.warm_open_path_caches(progress)

    def _fill_document_limit(
        self, summary: NotebookSummary, notebook_id: str
    ) -> NotebookSummary:
        """回填 owner 的「每笔记本文档数量上限」有效值(前端来源面板据此 + 来源列表
        total_count 显示「文档 X / 上限」)。仅详情/创建路径回填;列表投影保持默认。"""
        owner_id, _owner_role = self._identity.notebook_owner(notebook_id)
        summary.document_limit = self._identity.effective_document_limit(owner_id)
        return summary

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        user_id = self._identity.current_user().id
        notebook_id = self._store.create_row(payload, user_id)
        return self._fill_document_limit(
            self._summaries.get(notebook_id, user_id=user_id), notebook_id
        )

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self._fill_document_limit(
            self._summaries.get(
                notebook_id,
                kg_building=notebook_id in self.kg_building,
                paper_meta_backfilling=self._paper_meta_backfilling(notebook_id),
                user_id=self._identity.current_user().id,
            ),
            notebook_id,
        )

    def update_notebook(
        self, notebook_id: str, payload: NotebookUpdate
    ) -> NotebookSummary:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        self._store.update_row(notebook_id, payload)
        return self.get_notebook(notebook_id)

    def delete_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)  # raises KeyError if missing
        with diagnostics.diagnostic_phase("notebook_delete.db"):
            file_paths = self._store.delete_row_and_orphan_embeddings(notebook_id)
        # DB deletion is committed above; only then remove files on disk —
        # source files first, then the notebook's pasted-image asset
        # directory (knowhow-tables PR-2+3 Task 14; unconditional, from the
        # construction-injected live storage root, so the real HTTP delete
        # route — which reaches this service directly, not via the facade —
        # gets the cleanup too).
        with diagnostics.diagnostic_phase("notebook_delete.files"):
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
        # This search never tokenizes — it is already a whole-string substring
        # match, i.e. exactly what quoting asks for elsewhere. So the only thing
        # the markers can do here is make the pattern unmatchable: nothing in
        # the corpus contains the `"` the user typed. Dropping them keeps one
        # syntax across the product instead of a box where it silently finds
        # nothing.
        needle = strip_accepted_quote_markers(query)
        response = self._queries.search_notebook(notebook_id, needle)
        if self.memory_retriever is None:
            return response
        # Memory keeps the ORIGINAL query: its retriever is phrase-aware and
        # strips the markers itself for its own candidate probe. Handing it the
        # stripped needle would delete the constraint before the scorer sees it,
        # letting a memory that merely scatters those words qualify as though
        # the user had never quoted anything. (codex #410 round-2 P2)
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
