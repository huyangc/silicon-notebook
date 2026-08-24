from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4

from app.core.config import Settings
from app.domain.extensions import (
    AskCompletedObserverHostPort,
    ReportCompletedObserverHostPort,
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.domain.gap_consult import GapConsultHostPort
from app.core.request_context import (
    _REQUEST_USER,
    get_request_user,
    request_user_id,
    reset_request_user,
    set_request_user,
)
from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.llm import OpenAICompatibleClient, cap_kwargs
from app.models.common import Evidence
from app.models.identity import (
    AgentPrincipal,
    AgentProfile,
    AgentTokenIssued,
    AgentTokenSummary,
    UserProfile,
)
from app.models.memory import MemoryRecord, MemoryUpdate, PaginatedMemories
from app.models.notebooks import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
)
from app.models.sources import (
    AddUrlSourcesResult,
    PaginatedSources,
    RejectedUrl,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
)
from app.models.ask import (
    AnswerAnchor,
    AskRequest,
    AskResponse,
    Citation,
    ConversationBulkDeleteResult,
    FeedbackRequest,
    FeedbackResponse,
    ModelError,
    NotebookSearchResponse,
    QueryIntentContract,
    RuleCard,
)
from app.models.knowledge import (
    DuplicateGroup,
    KnowledgeEdge,
    KnowledgeFieldValue,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeRecord,
    KnowledgeRef,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    MergeRequest,
    PaginatedKnowledge,
)
from app.domain.retrieval import ChunkRetrievalPlan
from app.services import kg_ingest
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.vector_cache import LargeAwareLRUCache, LRUProcessCache
from app.services.extraction_profiles import (
    LIST_FIELDS,
    OBJECT_SCHEMAS,
    OBJECT_TYPE_LABELS,
    PROFILES,
    ObjectSchema,
    get_profile,
    resolve_profile,
)


def _normalize_doc_type(doc_type: str) -> str:
    """Keep only known profile ids; everything else (incl. 'auto') means
    auto-detect, stored as ''."""
    value = (doc_type or "").strip().lower()
    return value if value in PROFILES else ""
from app.services.mineru_client import MinerUClient
from app.services.mineru_cloud_client import MinerUCloudClient, MinerUCloudNotConfigured
from app.services import remote_sources
from app.services.model_work import ModelNotConfiguredError
from app.services.notebook_catalog import NotebookSummaryQuery
from app.repositories.bundle import PersistenceBundleFactory
from app.services.repository_runtime import RepositoryRuntime, RepositoryCompatibilitySeams
from app.services.source_ingestion import SourcePipelineHooks
from app.repositories.source_files import safe_filename as _safe_filename  # noqa: F401 — compatibility export
from app.services.parsers import mineru_content_list_to_elements
from app.services.knowhow.assets import AssetService
from app.services.source_image_persist import make_persist_image_factory
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    DESCRIPTION_SCHEMA_HINT,
    FOLLOWUP_REWRITE_SCHEMA_HINT,
    NOTEBOOK_META_SCHEMA_HINT,
    SCHEMA_INDUCTION_HINT,
    answer_prompt,
    followup_rewrite_prompt,
    notebook_description_prompt,
    notebook_meta_prompt,
    schema_induction_prompt,
)
from app.services.repository import UploadedSourceFile
from app.services.knowledge_query import KnowledgeQueryService
from app.services.knowledge_governance import KnowledgeGovernanceService
from app.services.schema_registry import SchemaRegistryService
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval_service import RetrievalService
from app.services.retrieval import (
    NeighborExpansion,
    RetrievedKnowledge,
    RetrievedElement,
    W_KEYWORD,
    W_SEMANTIC,
    _TYPE_WEIGHT,
    _fuse,
    _payload_text,
    bm25_scores,
    cosine,
    keyword_score,
    rrf_fuse,
    score_knowledge,
    score_elements,
    type_weight,
    ensure_procedure_quota,
    classify_evidence,
)


# orjson-accelerated JSON parse + the concept-description signature. Canonical
# definitions moved to app.services.knowledge_lifecycle (Task 15, alongside
# their only internal consumers _stream_seed_reps / rebuild_unified_kg); these
# module-level names stay as the frozen compatibility exports (SAME objects).
from app.services.knowledge_lifecycle import (  # noqa: F401 — compatibility exports
    _concept_desc_sig,
    _fast_loads,
)


def _repository_from_weakref(reference):
    """Resolve a facade compatibility seam without retaining the facade."""
    repository = reference()
    if repository is None:
        raise RuntimeError("RepositoryFacade is no longer available")
    return repository


def _compatibility_value(module_name: str, name: str, fallback: object) -> object:
    """Resolve compatibility patch seats from the concrete wrapper module."""
    module = sys.modules.get(module_name)
    return getattr(module, name, fallback) if module is not None else fallback


def _new_id(prefix: str) -> str:
    """Collision-proof surrogate row id: prefix + full 128-bit uuid hex.
    (Historically these used only the first 10 hex chars of uuid4().hex = 40
    bits, which birthday-collides on tables with one row per item at
    multi-million-row scale — concept_clusters on a giant KG hit
    `UNIQUE constraint failed: concept_clusters.id`.)"""
    return f"{prefix}-{uuid4().hex}"


def _remap_json_ids(value, maps: dict):
    """Recursively rewrite copied source/element/object references in JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in {"element_id", "source_id", "object_id"} and isinstance(
                item, str
            ):
                out[key] = maps.get(key, {}).get(item, item)
            elif key == "element_ids" and isinstance(item, list):
                mapping = maps.get("element_ids", {})
                out[key] = [
                    mapping.get(child, child)
                    if isinstance(child, str)
                    else _remap_json_ids(child, maps)
                    for child in item
                ]
            else:
                out[key] = _remap_json_ids(item, maps)
        return out
    if isinstance(value, list):
        return [_remap_json_ids(item, maps) for item in value]
    return value


# Knowledge status vocabularies + the graph size guard. Canonical definitions
# moved to app.services.knowledge_contracts (Task 13); these module-level
# names stay as the frozen compatibility exports (SAME objects).
from app.services.knowledge_contracts import (  # noqa: F401 — compatibility exports
    KNOWLEDGE_STATUSES,
    KnowledgeGraphTooLargeError,
    USABLE_STATUSES,
)

# Default notebook-name placeholders the frontend creates; name auto-fill only
# overwrites these (never a user-chosen name).
_DEFAULT_NOTEBOOK_NAMES = {"", "未命名笔记本", "Untitled notebook"}

# KG object types retrieved during ask(), in priority order.
_KG_TYPES = ("claim", "formula", "procedure", "concept")


# The `[k…]` answer-marker regexes and _strip_unbound_markers moved to
# app.services.ask_service with the mode engines (Task 24).


# copy_notebook's per-table chunk size (perf-audit P1-4): mirrors store_kg's
# CHUNK=1000 local constant, but module-level so tests can shrink it to force
# multiple chunk-boundary transactions without seeding thousands of rows.
_COPY_CHUNK = 1000


def _schedule_knowhow_projection(repo: "RepositoryFacade", table_id: str) -> None:
    """knowhow-tables PR-2+3 Task 13: the ``schedule_projection`` seam
    ``wire_sharing`` hands to ``NotebookCopyService`` — deferred import (the
    same idiom ``sqlite_notebook_sharing._repository_new_id`` already uses
    for its own late-bound compatibility helper) so this facade module need
    not import ``app.services.knowhow.api`` at load time. Routes through the
    SAME per-repo debounced ``ProjectionScheduler`` every editing endpoint
    already uses (Task 3), not a bespoke synchronous call — a copy with many
    knowhow tables collapses duplicate schedule() calls exactly the same
    way a burst of edits does."""
    from app.services.knowhow.api import get_scheduler

    get_scheduler(repo).schedule(table_id)


def _make_persist_image(
    repo: "RepositoryFacade", notebook_id: str, source_id: str, created_by: str
):
    """Task 8: the ``make_persist_image`` seam ``wire_source_ingestion`` hands
    to ``SourceIngestionService`` — a module-level function (not a facade
    method) for the SAME reason ``_schedule_knowhow_projection`` above is one:
    the one-hop-delegate facade-body contract (``test_repository_facade_
    contract.py``) forbids a NAMED facade method whose body does real work
    (constructing an ``AssetService`` + binding the pure factory) rather than
    delegating to a single owning component. Threading ``repo`` (the facade)
    explicitly keeps `AssetService(repo)` valid — it needs exactly the
    facade's public ``storage_dir``/``insert_notebook_asset``/
    ``get_notebook_asset`` one-hop delegates (Task 3) — while the actual
    binding call stays inside `__init__` (the exempted composition root) via
    an inline lambda, not a class method body."""
    return make_persist_image_factory(
        repo.settings, lambda: AssetService(repo)
    )(notebook_id, source_id, created_by)


class RepositoryFacade:
    def __init__(
        self,
        settings: Settings,
        persistence_factory: PersistenceBundleFactory,
        *,
        model_provider: Any | None = None,
        retrieval_contributor_host: RetrievalContributorHostPort | None = None,
        parser_provider_chain_host: ParserProviderChainHostPort | None = None,
        ask_completed_observer_host: AskCompletedObserverHostPort | None = None,
        report_completed_observer_host: ReportCompletedObserverHostPort | None = None,
        gap_consult_host: GapConsultHostPort | None = None,
    ) -> None:
        self.settings = settings
        self.root_dir = Path(__file__).resolve().parents[3]
        repository_ref = weakref.ref(self)
        compatibility_module = type(self).__module__
        self._runtime = RepositoryRuntime(
            settings=self.settings,
            root_dir=self.root_dir,
            seams=RepositoryCompatibilitySeams(
                new_id=lambda prefix: _compatibility_value(
                    compatibility_module, "_new_id", _new_id
                )(prefix),
                now=lambda: _compatibility_value(
                    compatibility_module, "_now", _now
                )(),
                copy_chunk_size=lambda: _compatibility_value(
                    compatibility_module, "_COPY_CHUNK", _COPY_CHUNK
                ),
                remap_json_ids=lambda value, maps: _compatibility_value(
                    compatibility_module,
                    "_remap_json_ids",
                    _remap_json_ids,
                )(value, maps),
                in_chunk_size=lambda: _repository_from_weakref(
                    repository_ref
                )._IN_CHUNK,
            ),
            persistence_factory=persistence_factory,
            model_provider=model_provider,
            retrieval_contributor_host=retrieval_contributor_host,
            parser_provider_chain_host=parser_provider_chain_host,
            ask_completed_observer_host=ask_completed_observer_host,
            report_completed_observer_host=report_completed_observer_host,
            gap_consult_host=gap_consult_host,
        )
        # Task 26: the resolved storage root has ONE owner — the runtime's
        # SourceFileStore.  The facade attribute is the SAME Path object (the
        # sharing/copy composition still resolves it late, so tests may swap
        # `repo.storage_dir` post-construction).
        self.storage_dir = self._runtime.source_files.storage_dir
        # Task 9: finish the sharing/deep-copy composition with facade-bound
        # late seams — the _insert_row seat and the memoized copy-stats keep
        # honouring per-instance monkeypatches, storage_dir stays live.
        self._runtime.wire_sharing(
            insert_row=lambda db, table, data: self._insert_row(db, table, data),
            copy_stats=lambda notebook_id: self.notebook_copy_stats(notebook_id),
            storage_dir=lambda: self._runtime.storage_dir,
            # PR-2+3 Task 13: deferred import (mirrors sqlite_notebook_sharing.
            # _repository_new_id's own deferred-import comment) — knowhow.api
            # never imports sqlite_repository back, so this is not actually
            # circular, but resolving it at CALL time (not module-load time)
            # keeps this file's own import order irrelevant either way.
            schedule_projection=lambda table_id: _schedule_knowhow_projection(
                self, table_id
            ),
        )
        # Task 10: vector flushes stay on the facade's `_write` seat (resolved
        # per call) so transaction-counting/failure-injection monkeypatches
        # keep observing them; the seat itself is the shared database lock.
        self._runtime.wire_persistence(write=lambda: self._write())
        # Task 11: the embed/chunk pipeline keeps the mutable embedder and the
        # KG-dirty callback late-bound. Object-vector flushes are owned by the
        # source embedding service itself.
        self._runtime.wire_source_pipeline(
            embedder=self._runtime.models.embedding,
            mark_unified_dirty=lambda notebook_id: self._mark_unified_kg_dirty(
                notebook_id
            ),
        )
        query_embedder = self._runtime.models.embedding("retrieval_query_embedding")
        self._runtime.set_embedder(query_embedder)
        self._runtime.wire_memory(
            persistence_embedder=self._runtime.models.embedding("memory_embedding"),
            query_embedder=query_embedder,
        )
        self.mineru_client = _compatibility_value(
            compatibility_module, "MinerUClient", MinerUClient
        )(settings)
        self.mineru_cloud_client = _compatibility_value(
            compatibility_module, "MinerUCloudClient", MinerUCloudClient
        )(settings)
        self.event_log = self._runtime.event_log
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Task 17: the unified-graph dict and the version-keyed VectorCache
        # are runtime-owned (RetrievalSnapshotCache constructs them eagerly);
        # `_unified_cache` / `_vector_cache` below are write-through property
        # descriptors over those SAME objects — no facade-only copies.
        # Bilingual-query hints are runtime-owned and shared by every consumer;
        # this compatibility property never retains a facade-only copy.
        # A stale hint affects query expansion only, never storage correctness.
        self._notebook_langs_cache = self._runtime.notebook_languages
        # C7: bounded LRU (was an unbounded plain dict — every notebook ever
        # touched stayed resident for the life of the process; each entry is
        # numpy arrays + a memoized hnsw handle, tens-of-MB to GB). Eviction
        # just drops the reference — GC frees the arrays, and hnswlib.Index
        # has no explicit close() to call (see LRUProcessCache docstring).
        # PR-4: a large-library ScaleIndex is tens of GB; cap how many of those
        # stay resident (scale_idx_cache_max_large) so multi-domain base warm-up
        # can't accumulate hundreds of GB → OOM. "Large" = the estimated resident
        # bytes of its ANN matrices — the kg (n_ann), chunk (n_chunk_ann) and
        # relation (n_relation_ann) hnsw handles, each rows×runtime_dim×4 bytes, the
        # dominant footprint — over scale_idx_large_bytes. Summing all three (not
        # just n_nodes) catches a KG-node-light but chunk/relation-ANN-heavy source
        # base (codex PR#359 r1 P1). A missing count contributes 0 → an old index
        # that never wrote these keys classifies as small (never over-evicts real
        # large indexes on a missing count). Only the scale-index cache needs this —
        # viz arrays (viz_idx_cache) are far smaller than the ANN matrices.
        from app.services.kg.scale_index import estimated_ann_bytes
        from app.services.vector_index import resolve_runtime_dim

        # Capture the Settings object (already held by the runtime), NOT ``self`` —
        # this closure lives inside scale_idx_cache, so closing over the facade
        # would make the cache transitively retain the whole repository (see
        # test_retained_scale_runtime_does_not_transitively_retain_repository).
        _settings = self.settings

        def _scale_idx_is_large(idx) -> bool:
            m = getattr(idx, "manifest", None) or {}
            dim = resolve_runtime_dim(_settings) or _settings.embed_dim
            return estimated_ann_bytes(m, dim) > _settings.scale_idx_large_bytes

        scale_idx_cache = LargeAwareLRUCache(
            max_entries=self.settings.scale_idx_cache_max,
            max_large=self.settings.scale_idx_cache_max_large,
            is_large=_scale_idx_is_large,
        )
        # P1-8: memoize _scale_index_version keyed on kg_mutation_seq. Maps
        # notebook_id -> (last_seq, version_list). When seq is unchanged we skip
        # the 5 COUNT/MAX aggregates and return the cached list (same format —
        # no on-disk manifest.version invalidation).
        scale_ver_cache: Dict[str, Any] = {}
        # Single-flight for _scale_index_version's cold path (memo miss / seq
        # changed): N concurrent callers for the SAME notebook must compute the
        # four non-cluster COUNT/MAX aggregates exactly once, not N times in
        # parallel. Simpler variant of VectorCache's per-key lock table (no
        # refcount eviction — the key space here is bounded by #notebooks, not
        # per-request cache keys, so an unbounded-but-small dict is fine). Lock
        # ordering mirrors VectorCache: _scale_ver_lock only ever guards
        # structural access to _scale_ver_locks (the lock table itself) — the
        # aggregate computation runs under the per-nb lock WITHOUT holding
        # _scale_ver_lock, so a thread never holds the global lock while
        # waiting on a per-nb lock (no lock-ordering cycle).
        scale_ver_lock = threading.Lock()
        scale_ver_locks: Dict[str, threading.Lock] = {}
        # per-nb 单飞:allow_stale 检索路径 cold-load ScaleIndex 时,防 N 个并发查询
        # 各自 load_scale_index + hnswlib.load_index(8GB)造成 N× 内存尖峰。锁次序同
        # _scale_ver_lock:全局锁只护锁表结构,绝不在全局锁内跑 load。
        scale_idx_load_lock = threading.Lock()
        scale_idx_load_locks: Dict[str, threading.Lock] = {}
        # C7: same bounded-LRU rework as _scale_idx_cache above.
        viz_idx_cache = LRUProcessCache(max_entries=self.settings.scale_idx_cache_max)
        # Task 18: the scale/viz artifact READ adapters (IndexProjectionStore /
        # ScaleArtifactCatalog over the runtime-eager ScaleArtifactStore) ride
        # facade-bound late seams — the `_connect` read seat, the `_in_batches`
        # IN-chunking helper, the retrieval-owned ent-chunk/mention/vector-
        # matrix caches (they move with their domain in Gate 7), the memoized
        # `_scale_index_version` key, the LRU/lock-table state above (tests
        # reassign `_scale_idx_cache`; Task 20 transfers ownership) and the
        # model-error note. Every callback resolves at call time through a
        # weak reference — post-construction monkeypatches stay observed
        # without creating facade→runtime→adapter→facade retention cycles.
        self._runtime.wire_scale_artifacts(
            connect=lambda: _repository_from_weakref(repository_ref)._connect(),
            in_batches=lambda ids: _repository_from_weakref(
                repository_ref
            )._in_batches(ids),
            ent_chunk_map=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._ent_chunk_map(notebook_id),
            mention_extra_edges=lambda notebook_id: (
                _repository_from_weakref(repository_ref)._mention_extra_edges(
                    notebook_id
                )
            ),
            vector_matrix=lambda db, notebook_id, table, id_column: (
                _repository_from_weakref(repository_ref)._vector_matrix(
                    db, notebook_id, table, id_column
                )
            ),
            version=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._scale_index_version(notebook_id),
            scale_cache=lambda: scale_idx_cache,
            load_lock=lambda: scale_idx_load_lock,
            load_locks=lambda: scale_idx_load_locks,
            note_model_error=lambda stage, model, exc, service="", provider_failure=False, failed_fingerprint="": (
                _repository_from_weakref(repository_ref)._note_model_error(
                    stage,
                    model,
                    exc,
                    service=service,
                    provider_failure=provider_failure,
                    failed_fingerprint=failed_fingerprint,
                )
            ),
        )
        scale_building: set = set()
        scale_building_lock = threading.Lock()
        # Task 19: full/fold/viz orchestration is runtime-owned. Every
        # remaining facade collaborator is resolved late through the same
        # weak reference so monkeypatch/transaction/cache identity seams stay
        # observable without the builder retaining this facade.
        self._runtime.wire_scale_builder(
            get_notebook=lambda notebook_id: _repository_from_weakref(
                repository_ref
            ).get_notebook(notebook_id),
            version=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._scale_index_version(notebook_id),
            load_scale=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._scale_index(notebook_id, allow_stale=True),
            full_viz_graph=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._unified_graph_full(notebook_id, "object"),
            relations_for_notebook=lambda notebook_id: _repository_from_weakref(
                repository_ref
            ).relations_for_notebook(notebook_id),
            cluster_map=lambda notebook_id: _repository_from_weakref(
                repository_ref
            ).cluster_map(notebook_id),
            incremental_fuse_source=lambda notebook_id, source_id: (
                _repository_from_weakref(repository_ref).incremental_fuse_source(
                    notebook_id, source_id
                )
            ),
            invalidate_scale_cache=lambda notebook_id: scale_idx_cache.pop(
                notebook_id, None
            ),
            cache_viz=lambda notebook_id, index: viz_idx_cache.__setitem__(
                notebook_id, index
            ),
            building=scale_building,
            building_lock=scale_building_lock,
            notify_index_done=lambda notebook_id: _repository_from_weakref(
                repository_ref
            )._notify_index_done(notebook_id),
        )
        scale_idle_queue: dict = {}   # {notebook_id: (mode, queued_at_iso)} 待低峰重建
        scale_scheduler_started = False
        # 大库自动建索引(maybe_auto_index)的 O(1) once-set:notebook_id 一旦被评估
        # (无论「已入队/建成」还是「判定不需要」)即加入,读路径兜底靠它避免每查询都
        # 算 notebook_copy_stats() 的 5 个 COUNT。_mark_unified_kg_dirty 在每次 KG
        # 写时 discard,使下一轮变更重新触发评估。
        auto_index_checked: set = set()
        # Task 14→17: the KG mutation coordinator reads the unified/vector
        # caches through the runtime-owned RetrievalSnapshotCache and wraps
        # the two facade-owned state objects above BY IDENTITY (no replacement
        # copies) plus the `_write` transaction seat (resolved per call, so
        # per-instance transaction traces / failure injections keep observing
        # the dirty bump's commit boundary). The `_invalidate_unified_cache` /
        # `_mark_unified_kg_dirty` / `_mark_unified_kg_dirty_in_tx` /
        # `_bump_cluster_mutation_seq` wrappers below delegate to it — every
        # mutation call site keeps funnelling through those wrappers, so the
        # frozen phase matrix is unchanged.
        self._runtime.wire_kg_mutations(
            auto_index_checked=auto_index_checked,
            notebook_languages=self._notebook_langs_cache,
            write=lambda: self._write(),
        )
        # KG-view viz index background build (mirrors _scale_building exactly):
        # guarded set of notebook_ids currently being folded by _spawn_viz_build.
        viz_building: set = set()
        viz_building_lock = threading.Lock()
        self._runtime.wire_scale_runtime(
            scale_cache=scale_idx_cache,
            viz_cache=viz_idx_cache,
            version_memo=scale_ver_cache,
            version_lock=scale_ver_lock,
            version_locks=scale_ver_locks,
            load_lock=scale_idx_load_lock,
            load_locks=scale_idx_load_locks,
            building=scale_building,
            building_lock=scale_building_lock,
            idle_queue=scale_idle_queue,
            scheduler_started=scale_scheduler_started,
            auto_index_checked=auto_index_checked,
            viz_building=viz_building,
            viz_building_lock=viz_building_lock,
        )
        self._runtime.wire_query_services(retrieval=lambda: self.retrieval)
        # Task 15: the knowledge governance + lifecycle services ride
        # facade-bound late seams — the `_connect`/`_write` transaction seats,
        # the Task-14 coordinator wrappers (single dirty entry preserved), the
        # per-user model-client properties (class-property patches keep
        # working), the frozen embed/extraction delegate seats, and TEMPORARY
        # Gate-6 scale/viz adapters (scale-index load / ANN open / viz index /
        # probe / build-viz / auto-index / copy-stats).  Every lambda resolves
        # at call time — post-construction monkeypatches stay observed.  The
        # unified-graph memo comes from the runtime-owned retrieval_snapshots
        # (Task 17); the viz-building set is passed BY IDENTITY (no copies).
        self._runtime.wire_knowledge_lifecycle(
            connect=lambda: self._connect(),
            write=lambda: self._write(),
            get_notebook=lambda notebook_id: self.get_notebook(notebook_id), current_user_id=lambda: self.current_user().id,
            invalidate_unified_cache=lambda notebook_id: (
                self._invalidate_unified_cache(notebook_id)
            ),
            mark_unified_kg_dirty=lambda notebook_id: (
                self._mark_unified_kg_dirty(notebook_id)
            ),
            mark_unified_kg_dirty_in_tx=lambda db, notebook_id: (
                self._mark_unified_kg_dirty_in_tx(db, notebook_id)
            ),
            bump_cluster_mutation_seq=lambda db, notebook_id: (
                self._bump_cluster_mutation_seq(db, notebook_id)
            ),
            embed_objects_batch=lambda notebook_id, items: (
                self._embed_objects_batch(notebook_id, items)
            ),
            embed_relations_batch=lambda notebook_id, rel_items: (
                self._embed_relations_batch(notebook_id, rel_items)
            ),
            source_ids_from_evidence=lambda evidence_json: (
                self._source_ids_from_evidence(evidence_json)
            ),
            set_source_status=lambda source_id, status, **kwargs: self._set_source_status(
                source_id, status, **kwargs),
            run_extraction=lambda source_id, **kwargs: _call_extraction_compat(
                self._run_extraction, self._runtime.source_ingestion.run_extraction, source_id, kwargs),
            # notebook KG 构建路径的 doc_type 终态收口：复用上传流水线同一套
            # _extract_reconciling_doc_type（守卫落 'extracted' + 不一致重跑），并让它
            # 原子补发 'extracted' 事件（构建路径无 stage 前置事件、无顺序约束）。晚绑定
            # 到 source_ingestion（call-time 解析，与 run_extraction 同——它在
            # wire_source_ingestion 才建，晚于 wire_knowledge_lifecycle）。
            reconcile_extracted_terminal=lambda source_id, extract: (
                self._runtime.source_ingestion._extract_reconciling_doc_type(
                    source_id, extract, emit_terminal_status=True
                )
            ),
            cluster_map=lambda notebook_id: self.cluster_map(notebook_id),
            annotate_edge_support=lambda notebook_id, edges: (
                self._annotate_edge_support(notebook_id, edges)
            ),
            decided_seed_pairs=lambda notebook_id: self.decided_seed_pairs(notebook_id),
            relations_for_notebook=lambda notebook_id: (
                self.relations_for_notebook(notebook_id)
            ),
            notebook_copy_stats=lambda notebook_id: self.notebook_copy_stats(notebook_id),
            note_model_error=lambda stage, model, exc, service="", provider_failure=False, failed_fingerprint="": (
                self._note_model_error(
                    stage,
                    model,
                    exc,
                    service=service,
                    provider_failure=provider_failure,
                    failed_fingerprint=failed_fingerprint,
                )
            ),
            edge_centrality_map=lambda notebook_id: (
                self._edge_centrality_map(notebook_id)
            ),
            embed_knowledge=lambda object_id, notebook_id, payload: (
                self._embed_knowledge(object_id, notebook_id, payload)
            ),
            knowledge_objects=lambda db, notebook_id, object_type, **kw: (
                self._knowledge_objects(db, notebook_id, object_type, **kw)
            ),
            as_retrieved=lambda obj, object_type: self._as_retrieved(obj, object_type),
            rule_card=lambda item: self._rule_card(item),
            set_conflict_status=lambda notebook_id, candidate_id, status: (
                self.set_conflict_status(notebook_id, candidate_id, status)
            ),
        )
        # KG build/rebuild 的进行中标志（进程内；重启后天然为空=未构建，无需 reconcile）。
        # get_notebook 回填 NotebookSummary.kg_building，前端刷新后据此把「构建中」接回。
        # 集合本体归 NotebookCatalogService 所有（get_notebook 在那读成员资格）；
        # 生命周期服务持同一个 set 对象并拥有守卫锁（build/rebuild 路径在那 add/
        # discard）；facade 属性别名同一对对象，既有测试/调用照旧可见。
        self._kg_building: set = self._runtime.knowledge_lifecycle.kg_building
        self._kg_building_lock = self._runtime.knowledge_lifecycle.kg_building_lock
        # Task 12→15: the ingestion orchestration rides facade-bound late seams
        # — the `_write` transaction seat and summarize/model seams whose
        # frozen patch targets live on this facade (repo.source_elements /
        # repo._summarize_source / per-user llm & kg_llm properties) — plus DIRECT
        # KnowledgeLifecycleService/KgMutationCoordinator dependencies (the
        # Gate-4 store_kg / incremental_fuse_source / invalidate_unified_cache
        # callbacks are gone; wired AFTER wire_knowledge_lifecycle).  The
        # remaining TEMPORARY facade-owned catalog callbacks move with their
        # domains in Task 16+.  Every lambda resolves at call time —
        # post-construction monkeypatches stay observed.
        self._runtime.wire_source_ingestion(
            write=lambda: self._write(),
            source_elements=lambda source_id: self.source_elements(source_id),
            summarize_source=lambda title, elements: self._summarize_source(
                title, elements
            ),
            source_type_from_name=lambda file_name: self._source_type_from_name(
                file_name
            ),
            mineru_client=lambda: self.mineru_client,
            mineru_cloud_client=lambda: self.mineru_cloud_client,
            normalize_doc_type=_normalize_doc_type,
            default_notebook_names=_DEFAULT_NOTEBOOK_NAMES,
            clear_source_extraction_state=(
                lambda db, source_id, notebook_id, clear_embeddings: (
                    self._clear_source_extraction_state(
                        db, source_id, notebook_id, clear_embeddings=clear_embeddings
                    )
                )
            ),
            begin_extraction_run=lambda source_id, notebook_id, run_id, created_at,
            preserve_existing=False: (
                self._begin_extraction_run(
                    source_id,
                    notebook_id,
                    run_id,
                    created_at,
                    preserve_existing=preserve_existing,
                )
            ),
            finish_extraction_run=lambda run_id, status, message: (
                self._finish_extraction_run(run_id, status, message)
            ),
            notebook_tier=lambda notebook_id: self._notebook_tier(notebook_id),
            concept_whitelist_terms=lambda: self.concept_whitelist_terms(),
            notebook_has_kg=lambda notebook_id: self._notebook_has_kg(notebook_id),
            maybe_auto_index=lambda notebook_id: self.maybe_auto_index(notebook_id),
            notebook_meta_row=lambda notebook_id: self._notebook_meta_row(notebook_id),
            notebook_meta_sources=lambda notebook_id, pending_source_id: (
                self._notebook_meta_sources(notebook_id, pending_source_id)
            ),
            apply_notebook_meta=lambda notebook_id, guard_name, name, purpose: (
                self._apply_notebook_meta(
                    notebook_id, guard_name=guard_name, name=name, purpose=purpose
                )
            ),
            make_persist_image=lambda notebook_id, source_id, created_by: (
                _make_persist_image(self, notebook_id, source_id, created_by)
            ),
            delete_source_images=lambda sid: AssetService(self).delete_source_images(sid),
        )
        # WS2a: 在途 ask 的 {job_id: cancel_event} 进程内注册表本体在 runtime-owned
        # AskCancellationRegistry(Task 23;流式执行编排在 AskExecutionCoordinator,
        # 经同一注册表)。_ask_cancel_events/_ask_cancel_lock 以下方兼容属性透出
        # 同一对象——cancel 端点据此 set 对应 worker 的 cancel_event(唯一真取消
        # 入口;断连不再取消)。
        # Task 25: 深度报告脱离连接执行域——引擎工厂骑在 facade 惰性 `retrieval`
        # 属性上(embedder 绑定),job 一律经 background_jobs.submit(copy_context
        # 传播 per-user 模型);进程全局取消注册表按身份注入。
        from app.services import background_jobs
        self._runtime.wire_report_execution(
            retrieval=lambda: self.retrieval,
            job_submitter=background_jobs.submit,
        )
        # Task 24: Ask 模式引擎域——AskService 骑在同一个 facade 惰性 `retrieval`
        # 属性上(embedder 绑定;首次 ask 才组合),runtime.ask_service() 是唯一
        # 所有者;流式协调器与冻结签名的 ask_* delegate 都经它执行。
        self._runtime.wire_ask(retrieval=lambda: self.retrieval)

    def current_user(self) -> UserProfile:
        return self._runtime.identity.current_user()

    def _user_profile(self, user, profile) -> UserProfile:
        return self._runtime.identity._user_profile(user, profile)

    def create_user(self, username: str, password: str) -> UserProfile:
        return self._runtime.identity.create_user(username, password)

    def authenticate_user(self, username: str, password: str) -> "UserProfile | None":
        return self._runtime.identity.authenticate_user(username, password)

    def create_session(self, user_id: str) -> str:
        return self._runtime.identity.create_session(user_id)

    def resolve_session(self, token: str) -> "UserProfile | None":
        return self._runtime.identity.resolve_session(token)

    def delete_session(self, token: str) -> None:
        return self._runtime.identity.delete_session(token)

    def audit_labels_for_user_ids(self, user_ids: List[str]) -> dict[str, str]:
        return self._runtime.identity.audit_labels_for_user_ids(user_ids)

    def visible_document_count(self, notebook_id: str) -> int:
        return self._runtime.source_store.visible_document_count(notebook_id)

    def global_document_limit_default(self) -> int:
        return self._runtime.identity.global_document_limit_default()

    def user_document_limit_override(self, user_id: str) -> "int | None":
        return self._runtime.identity.user_document_limit_override(user_id)

    def effective_document_limit(self, user_id: "str | None") -> int:
        return self._runtime.identity.effective_document_limit(user_id)

    def notebook_owner(self, notebook_id: str) -> "tuple[str | None, str | None]":
        return self._runtime.identity.notebook_owner(notebook_id)

    def list_user_usage(self) -> List[Dict[str, Any]]:
        return self._runtime.queries.list_user_usage()

    def list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]:
        return self._runtime.queries.list_user_notebooks(user_id)

    def notebook_exists_for_owner(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.queries.notebook_exists_for_owner(notebook_id, user_id)

    def list_user_activity(
        self,
        user_id: str,
        *,
        notebook_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        before_ts: Optional[str] = None,
        before_id: Optional[str] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        return self._runtime.queries.list_user_activity(
            user_id,
            notebook_id=notebook_id,
            since=since,
            until=until,
            before_ts=before_ts,
            before_id=before_id,
            limit=limit,
        )

    def load_notebook_scale_facts(self, notebook_id: str):
        return self._runtime.queries.load_notebook_scale_facts(notebook_id)

    def pending_actions_projection_rows(self, user_id: str) -> dict:
        return self._runtime.queries.pending_actions_projection_rows(user_id)

    def chat(self, workload_id: str):
        return self._runtime.models.chat(workload_id)

    def configured(self, workload_id: str) -> bool:
        return self._runtime.models.configured(workload_id)

    def parallelism(self, workload_id: str) -> int:
        return self._runtime.models.parallelism(workload_id)

    # Task 17: the retrieval caches live on the runtime's RetrievalSnapshotCache
    # (one owner). These handles are write-through descriptors over the SAME
    # objects — never facade-only copies. A vector-cache swap propagates to
    # every consumer because the KG mutation coordinator reads through the
    # snapshot cache; a unified-cache swap also refreshes the lifecycle
    # service's held reference (it keeps the dict by identity).
    @property
    def _vector_cache(self):
        return self._runtime.retrieval_snapshots.vector_cache

    @_vector_cache.setter
    def _vector_cache(self, cache) -> None:
        self._runtime.retrieval_snapshots.vector_cache = cache

    @property
    def _unified_cache(self):
        return self._runtime.retrieval_snapshots.unified_cache

    @_unified_cache.setter
    def _unified_cache(self, cache) -> None:
        return self._runtime.set_unified_cache(cache)

    # Task 20: every scale/viz mutable object has one owner.  These descriptors
    # preserve the frozen facade attributes and test swap seams while writing
    # through to ScaleArtifactRuntime and its Task-18/19 consumers.
    @property
    def _scale_idx_cache(self):
        return self._runtime.scale_artifacts.scale_cache

    @_scale_idx_cache.setter
    def _scale_idx_cache(self, value) -> None:
        self._runtime.scale_artifacts.scale_cache = value

    @property
    def _viz_idx_cache(self):
        return self._runtime.scale_artifacts.viz_cache

    @_viz_idx_cache.setter
    def _viz_idx_cache(self, value) -> None:
        self._runtime.scale_artifacts.viz_cache = value

    @property
    def _scale_ver_cache(self):
        return self._runtime.scale_artifacts.version_memo

    @_scale_ver_cache.setter
    def _scale_ver_cache(self, value) -> None:
        self._runtime.scale_artifacts.version_memo = value

    @property
    def _scale_ver_lock(self):
        return self._runtime.scale_artifacts.version_lock

    @_scale_ver_lock.setter
    def _scale_ver_lock(self, value) -> None:
        self._runtime.scale_artifacts.version_lock = value

    @property
    def _scale_ver_locks(self):
        return self._runtime.scale_artifacts.version_locks

    @_scale_ver_locks.setter
    def _scale_ver_locks(self, value) -> None:
        self._runtime.scale_artifacts.version_locks = value

    @property
    def _scale_idx_load_lock(self):
        return self._runtime.scale_artifacts.load_lock

    @_scale_idx_load_lock.setter
    def _scale_idx_load_lock(self, value) -> None:
        self._runtime.scale_artifacts.load_lock = value

    @property
    def _scale_idx_load_locks(self):
        return self._runtime.scale_artifacts.load_locks

    @_scale_idx_load_locks.setter
    def _scale_idx_load_locks(self, value) -> None:
        self._runtime.scale_artifacts.load_locks = value

    @property
    def _scale_building(self):
        return self._runtime.scale_artifacts.building

    @_scale_building.setter
    def _scale_building(self, value) -> None:
        return self._runtime.scale_artifacts.set_building(value)

    @property
    def _scale_building_lock(self):
        return self._runtime.scale_artifacts.building_lock

    @_scale_building_lock.setter
    def _scale_building_lock(self, value) -> None:
        return self._runtime.scale_artifacts.set_building_lock(value)

    @property
    def _scale_idle_queue(self):
        return self._runtime.scale_artifacts.idle_queue

    @_scale_idle_queue.setter
    def _scale_idle_queue(self, value) -> None:
        self._runtime.scale_artifacts.idle_queue = value

    @property
    def _scale_scheduler_started(self):
        return self._runtime.scale_artifacts.scheduler_started

    @_scale_scheduler_started.setter
    def _scale_scheduler_started(self, value) -> None:
        return self._runtime.scale_artifacts.set_scheduler_started(value)

    @property
    def _auto_index_checked(self):
        return self._runtime.scale_artifacts.auto_index_checked

    @_auto_index_checked.setter
    def _auto_index_checked(self, value) -> None:
        return self._runtime.set_auto_index_checked(value)

    @property
    def _viz_building(self):
        return self._runtime.scale_artifacts.viz_building

    @_viz_building.setter
    def _viz_building(self, value) -> None:
        self._runtime.scale_artifacts.viz_building = value

    @property
    def _viz_building_lock(self):
        return self._runtime.scale_artifacts.viz_building_lock

    @_viz_building_lock.setter
    def _viz_building_lock(self, value) -> None:
        self._runtime.scale_artifacts.viz_building_lock = value

    def _resolve_path(self, value: str) -> Path:
        return self._runtime.database.resolve_path(value)





    def _connect(self) -> object:
        return self._runtime.database.connect()

    def close(self) -> None:
        """Drain process-owned model work and release this runtime once."""
        self._runtime.close()

    @contextmanager
    def _write(self):
        """串行化写事务：进程内同一时刻只有一个写者进 SQLite，并发写线程在
        Python 层排队而非裸抢 SQLite 写锁（后者即 `database is locked` 的根因）。
        纯读保持用 _connect()，不受影响（WAL 支持并发读）。"""
        with self._runtime.database.write() as db:
            yield db





    # Gate-5 migration (Task 13): the four rebuild-checkpoint helpers moved to
    # UnifiedKgStore (kg_rebuild_checkpoint row-level read/writes on the same
    # shared database lock). These frozen-signature delegates stay callable;
    # rebuild_unified_kg and the test patch seats target the store seam.
    def _rebuild_ckpt_gc(self, notebook_id: str, input_version: str) -> None:
        self._runtime.unified_kg.checkpoint_gc(notebook_id, input_version)

    def _rebuild_ckpt_clear(self, notebook_id: str) -> None:
        self._runtime.unified_kg.checkpoint_clear(notebook_id)

    def _rebuild_ckpt_load(self, notebook_id: str, input_version: str, stage: str) -> Dict[str, dict]:
        return self._runtime.unified_kg.checkpoint_load(notebook_id, input_version, stage)

    def _rebuild_ckpt_put(self, notebook_id: str, input_version: str, stage: str,
                          rows: List[Tuple[str, dict]]) -> None:
        return self._runtime.unified_kg.checkpoint_put_current(
            notebook_id, input_version, stage, rows
        )

    # KG 质量分析的只读聚合(T1) — UnifiedKgStore owns the reads; one-hop delegates.
    # 口径与产品一致(对象 USABLE_STATUSES;边 review_status != 'rejected' 且两端都是
    # 本 notebook 的可用对象),被排除的量随载荷单独返回。
    #
    # ⚠ **下面三条全是全表级的重活,而且是同一量级**(第四条 kg_community_overview 是
    # 有界读,便宜)。别只给最后一条打警告,也别因为 largest_clusters 带 LIMIT 就当它便宜
    # ——它在同形状的全扫分组之后还多一次排序,LIMIT 只截断输出。按后端分列的实测数字
    # (SQLite / PostgreSQL 各自的量级与计划形状)在 repositories/ports.py 该段头,那是真源。
    # 调用方(T3 的 KgAnalysisService)必须把它们放进按 kg_mutation_seq 记忆化的预计算
    # 路径,不得挂在在线请求上;并且**不得持有外层写事务**(store 自开连接,PG 侧会从
    # 池里另取一条,在 write() 里调用会安静地读到提交前的旧数据)。
    def kg_cluster_size_histogram(self, notebook_id: str) -> Dict[str, object]:
        """合并簇的大小分桶直方图,按 object_type 分组 + 全类型合计(收敛率分布面)。
        分组分桶在 SQL 里完成。⚠ **重活**:全扫 + 每行一次对象匹配 + 分组聚合。"""
        return self._runtime.unified_kg.cluster_size_histogram(notebook_id)

    def kg_largest_clusters(self, notebook_id: str, limit: int = 20) -> Dict[str, object]:
        """成员最多的 N 个 concept 簇;limit 有硬上限,返回带 truncated。
        ⚠ **重活,与直方图同量级**:同形状的全扫分组之后还多一次排序,LIMIT 只截断
        输出、不减少扫描与排序的输入。"""
        return self._runtime.unified_kg.largest_clusters(notebook_id, limit)

    def kg_community_overview(self, notebook_id: str, *, level: int = 0,
                              limit: int = 50, top_k: int = 5) -> Dict[str, object]:
        """主题板块列表 + 每个板块按 centrality 降序的前 K 个代表概念;
        板块数与 K 都有硬上限,返回带 total/returned/truncated,绝不静默截断。
        整块跑在一次显式只读快照事务里(并发 rebuild 不会劈出自相矛盾的数字)。"""
        return self._runtime.unified_kg.community_overview(
            notebook_id, level=level, limit=limit, top_k=top_k
        )

    def kg_relation_provenance_counts(self, notebook_id: str) -> Dict[str, object]:
        """边的出处构成(relink 补出来的 vs 未标注)。⚠ **重活:全表扫**——生产库
        800 万+ 边,每行还要匹配两个端点,不适合挂在线请求上。"""
        return self._runtime.unified_kg.relation_provenance_counts(notebook_id)

    @property
    def agent_profile(self):
        """Agentic Memory P1's per-notebook "understanding" store (T2).

        One hop to the runtime-owned ``AgentProfileStorePort`` seat, the same
        shape as ``collection_catalog`` below — deliberately NOT ten flat
        pass-through methods on the facade. The store's method names
        (``read_blocks``, ``write_block``, ``claim``, ``settle``, …) are
        generic enough that flattening them onto a 3000-line facade puts a
        bare ``claim``/``settle``/``clear_all`` into a namespace shared with
        every other domain: the next store with a ``claim`` collides silently
        (Python just keeps the later ``def``), and at a call site
        ``repository.settle(...)`` says nothing about which subsystem is being
        settled. Consumers (T3-T6) read
        ``repository.agent_profile.<method>`` instead.
        """
        return self._runtime.agent_profile

    @property
    def kg_analysis(self):
        """KG 质量分析报告的只读装配 service(T3)——one-hop 委托到中性 runtime 上的
        那**一个**实例。

        为什么是 runtime 而不是像 ``checkup`` 那样每个后端 facade 各构一份:它只吃
        database + unified_kg 两个 seam,自己不 import 任何后端,所以中性 runtime 装得下
        (checkup 装不下 —— 它依赖 sqlite/postgres 各自的 maintenance adapter)。
        单例的意义是那份按 seq 记忆化的板块列表/跨板块边缓存要跨请求存活。

        ⚠ 与上面四条的关系:本 service **只读预计算产物**,一次都不调那三条全表重活。
        """
        return self._runtime.kg_analysis

    @property
    def command_catalog(self):
        """命令目录抽取 service(方案 C·C1b)——一跳委托到中性 runtime 上的那一个实例。

        与 ``kg_analysis`` 同一条理由落在 runtime 而不是各 facade:它只吃端口与可调用,
        自己不 import 任何后端。单例在这里是**语义要求**:取消事件注册表挂在实例上,
        发起 job 的请求线程与跑 job 的后台线程必须共用同一份,分身即失联。
        """
        return self._runtime.command_catalog

    @property
    def agent_profile_jobs(self):
        """Agentic Memory P1 的巡固触发 service(T4/T5)——一跳委托到中性 runtime 上的
        那**一个**实例,与 ``command_catalog`` 同一条理由:它只吃端口/可调用,自己不
        import 任何后端;单例是语义要求(阈值闸与单飞落在持久行上,来源管线的 hook、
        Ask/报告完成的通知与 T6 手动重建按钮必须共用同一条 claim→submit 路径,分身
        即两套账)。T6 是这个 facade 属性唯一的消费方——其余触发点（source_ingestion、
        ask_execution、report_engine）已经持有 runtime 自己的 ``self.agent_profile_jobs``,
        不需要绕这一跳。
        """
        return self._runtime.agent_profile_jobs

    @property
    def retrieval_experiences(self):
        """Agentic Memory P2's deployment-GLOBAL retrieval-experience store
        (T5).

        One hop to the runtime-owned ``RetrievalExperienceStorePort`` seat,
        the same shape and the same reasoning as ``agent_profile`` above:
        method names like ``read_all``/``count``/``upsert_experience`` are far
        too generic to flatten onto a facade shared with every other domain.

        ⚠ Read that port's docstring before adding a consumer. It is the only
        store here with no tenancy column at all, which means callers get NO
        help from a predicate: what may become a row is decided one layer up,
        by ``retrieval_experience_projection``. A consumer that writes rows
        assembled from anywhere else has quietly removed the whole guarantee.
        """
        return self._runtime.retrieval_experiences

    @property
    def agent_observations(self):
        """Agentic Memory P3's per-(notebook, owner) observation log store
        (T2).

        One hop to the runtime-owned ``AgentObservationStorePort`` seat, the
        same shape and the same reasoning as ``agent_profile`` above: method
        names like ``append_observation``/``clear_observations`` are far too
        generic to flatten onto a facade shared with every other domain.
        Consumers (T3's MCP ``add_observation`` tool, T4's untrusted
        overlay-consolidation sample, T5's "my observations" API) read
        ``repository.agent_observations.<method>`` instead.
        """
        return self._runtime.agent_observations

    def _count(self, db: object, table: str, column: str, value: str) -> int:
        return self._runtime.notebook_summaries.count(db, table, column, value)

    def _count_knowledge(self, db: object, notebook_id: str, object_type: str) -> int:
        return self._runtime.knowledge.count_knowledge(
            db, notebook_id, object_type, USABLE_STATUSES
        )

    def _has_kg(self, db: object, notebook_id: str) -> bool:
        return self._runtime.notebook_summaries.has_kg(db, notebook_id)

    def _source_has_kg(self, db: object, source_id: str) -> bool:
        """True iff the source has ≥1 knowledge_objects row with a matching source_id."""
        return self._runtime.knowledge.source_has_kg(db, source_id)

    def _count_pending_kg_sources(self, db: object, notebook_id: str) -> int:
        return self._runtime.notebook_summaries.count_pending_kg_sources(db, notebook_id)

    def _any_base_notebook_has_kg(
        self, notebook_id: str, db: "object | None" = None
    ) -> bool:
        """True iff 该 notebook 挂载的参考库里有任一已建 KG。"""
        return self._runtime.knowledge.any_mounted_has_kg_compat(notebook_id, db)

    def _mounted_bases(
        self, notebook_id: str, db: "object | None" = None
    ) -> "tuple[list, list]":
        """(参考库列表, 其中已建 KG 的库 id) —— 第二项过去是 `any(has_kg)` 布尔,现在是
        那批 id;`bool(...)` 与旧值逐字等价,故按布尔消费的调用方行为不变。"""
        return self._runtime.notebook_summaries.mounted_bases(notebook_id, db)

    def _source_ids_from_evidence(self, evidence_json: Optional[str | list]) -> set:
        """PURE parse of an evidence JSON TEXT value into its distinct
        source_ids — canonical body lives on KnowledgeStore (Task 13)."""
        return self._runtime.knowledge.source_ids_from_evidence(evidence_json)

    def _upsert_knowledge_object_sources(
        self, db: object, object_id: str, notebook_id: str, evidence_json: Optional[str]
    ) -> None:
        self._runtime.knowledge.replace_object_sources(
            db, object_id, notebook_id, evidence_json
        )

    def _delete_knowledge_object_sources(
        self, db: object, object_ids: List[str]
    ) -> None:
        return self._runtime.knowledge.delete_object_sources(db, object_ids)

    def _source_index_backfilled(self, db: object, notebook_id: str) -> bool:
        return self._runtime.knowledge.source_index_backfilled(db, notebook_id)

    def _mark_source_index_backfilled(self, db: object, notebook_id: str) -> None:
        self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)

    def _find_stale_knowledge_ids_for_source(
        self, db: object, source_id: str, notebook_id: str
    ) -> List[str]:
        return self._runtime.knowledge.stale_object_ids_for_source(
            db, source_id, notebook_id
        )

    def _clear_source_extraction_state(
        self,
        db: object,
        source_id: str,
        notebook_id: str,
        *,
        clear_embeddings: bool,
    ) -> None:
        self._runtime.knowledge.clear_source_extraction_state(
            db, source_id, notebook_id, clear_embeddings=clear_embeddings
        )

    def _knowledge_objects(
        self,
        db: object,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]] = USABLE_STATUSES,
        id_filter: Optional[Iterable[str]] = None,
    ) -> List[dict]:
        # 候选集(id_filter,如 _retrieve_scored 的 cand_sims keys)可能超
        # _IN_CHUNK(delta 开且候选量大)——去重保序后按 _IN_CHUNK 分批查询,批间
        # 用 (created_at,id) 重排合并,保持与单条 IN + ORDER BY 完全一致的输出序
        # (KnowledgeStore.retrieval_objects,Task 26 delegate)。
        return self._runtime.knowledge.retrieval_objects_compat(
            db, notebook_id, object_type, statuses, id_filter
        )

    def list_notebooks(self) -> List[NotebookSummary]:
        return self._runtime.catalog.list_notebooks()

    def list_notebook_templates(self) -> List[NotebookTemplate]:
        return self._runtime.catalog.list_notebook_templates()

    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary:
        return self._runtime.catalog.create_notebook(payload)

    def get_notebook(self, notebook_id: str) -> NotebookSummary:
        return self._runtime.catalog.get_notebook(notebook_id)

    def update_notebook(self, notebook_id: str, payload: NotebookUpdate) -> NotebookSummary:
        return self._runtime.catalog.update_notebook(notebook_id, payload)

    def mark_notebook_base(self, notebook_id: str) -> None:
        return self._runtime.catalog.mark_notebook_base(notebook_id)

    def set_notebook_personal(self, notebook_id: str) -> None:
        return self._runtime.catalog.set_notebook_personal(notebook_id)

    def list_notebook_bases(self, notebook_id: str) -> list[dict]:
        return self._runtime.notebook_store.list_mount_edges_for_notebook(notebook_id)

    def mountable_notebooks(self, notebook_id: str) -> list[dict]:
        return self._runtime.notebook_store.mountable_for_notebook(notebook_id)

    def mounted_by_count(self, notebook_id: str) -> int:
        return self._runtime.notebook_store.mounted_by_count_for_notebook(notebook_id)

    def replace_notebook_bases(
        self, notebook_id: str, base_notebook_ids: list[str], created_by: str
    ) -> None:
        return self._runtime.notebook_store.replace_mounts(
            notebook_id, base_notebook_ids, created_by
        )

    def delete_notebook(self, notebook_id: str) -> None:
        return self._runtime.catalog.delete_notebook(notebook_id)

    # Owner-private Memory lifecycle.  The facade remains a one-hop
    # compatibility adapter; orchestration lives in MemoryService and SQL in
    # MemoryStore.
    def create_agent_profile(
        self, owner_id: str, name: str, description: str = ""
    ) -> AgentProfile:
        return self._runtime.memory_service.create_agent_profile(
            owner_id, name, description
        )

    def list_agent_profiles(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentProfile]:
        return self._runtime.memory_service.list_agent_profiles(
            owner_id, offset, limit
        )

    def update_agent_profile(
        self, profile_id: str, owner_id: str, patch: Any
    ) -> AgentProfile:
        return self._runtime.memory_service.update_agent_profile(
            profile_id, owner_id, patch
        )

    def issue_agent_token(
        self,
        owner_id: str,
        agent_profile_id: str,
        scopes: List[str],
        default_notebook_id: str,
        notebook_ids: List[str],
        expires_at: "str | None",
    ) -> AgentTokenIssued:
        return self._runtime.memory_service.issue_agent_token(
            owner_id,
            agent_profile_id,
            scopes,
            default_notebook_id,
            notebook_ids,
            expires_at,
        )

    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]:
        return self._runtime.memory_service.list_agent_tokens(owner_id, offset, limit)

    def revoke_agent_token(
        self, owner_id: str, token_id: str
    ) -> AgentTokenSummary:
        return self._runtime.memory_service.revoke_agent_token(owner_id, token_id)

    def resolve_agent_token(self, raw_token: str) -> "AgentPrincipal | None":
        return self._runtime.memory_service.resolve_agent_token(raw_token)

    def refresh_agent_principal(self, token_id: str) -> "AgentPrincipal | None":
        return self._runtime.memory_service.refresh_agent_principal(token_id)

    def require_agent_access(
        self, principal: AgentPrincipal, scope: str, notebook_id: str
    ) -> None:
        return self._runtime.memory_service.require_agent_access(
            principal, scope, notebook_id
        )

    def agent_memory_hits(
        self,
        user_id: str,
        notebook_id: str,
        query: str,
        include_candidates: bool,
        limit: int = 8,
    ) -> list:
        return self._runtime.memory_retriever.agent_memory_hits(
            user_id, notebook_id, query, include_candidates, limit
        )

    def answer_memory_source(self, answer_id: str) -> dict:
        return self._runtime.ask_state.answer_memory_source(answer_id)

    def create_memory_candidate(
        self,
        notebook_id: str,
        user_id: str,
        agent_profile_id: "str | None",
        client_request_id: str,
        title: str,
        content_md: str,
        tags: List[str],
        reason: str,
        task_context: "dict | None" = None,
        evidence_refs: "List[dict] | None" = None,
    ) -> MemoryRecord:
        return self._runtime.memory_service.create_candidate(
            notebook_id, user_id, agent_profile_id, client_request_id, title,
            content_md, tags, reason, task_context, evidence_refs
        )

    def create_memory_from_answer(
        self, notebook_id: str, user_id: str, answer_id: str, title: str,
        content_md: str, tags: List[str], extract_kg: bool = True,
    ) -> MemoryRecord:
        return self._runtime.memory_service.create_from_answer(
            notebook_id, user_id, answer_id, title, content_md, tags, extract_kg
        )

    def memory_kg_eligible(self, notebook_id: str) -> bool:
        return self._runtime.source_ingestion.memory_kg_eligible(notebook_id)

    def update_memory(
        self, memory_id: str, user_id: str, patch: MemoryUpdate
    ) -> MemoryRecord:
        return self._runtime.memory_service.update(memory_id, user_id, patch)

    def confirm_memory(
        self,
        memory_id: str,
        user_id: str,
        patch: "MemoryUpdate | None" = None,
    ) -> MemoryRecord:
        return self._runtime.memory_service.confirm(memory_id, user_id, patch)

    def reject_memory(self, memory_id: str, user_id: str) -> MemoryRecord:
        return self._runtime.memory_service.reject(memory_id, user_id)

    def deprecate_memory(self, memory_id: str, user_id: str) -> MemoryRecord:
        return self._runtime.memory_service.deprecate(memory_id, user_id)

    def delete_memory(self, memory_id: str, user_id: str) -> None:
        return self._runtime.memory_service.delete(memory_id, user_id)

    def bulk_delete_memories(self, user_id: str, memory_ids) -> int:
        return self._runtime.memory_service.bulk_delete(user_id, memory_ids)

    def get_memory(self, memory_id: str, user_id: str) -> MemoryRecord:
        return self._runtime.memory_service.get(memory_id, user_id)

    def list_memories(
        self,
        user_id: str,
        notebook_id: "str | None" = None,
        status: "str | None" = None,
        origin: "str | None" = None,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedMemories:
        return self._runtime.memory_service.list_memories(
            user_id, notebook_id, status, origin, query, offset, limit
        )

    def answer_memory_links(
        self, notebook_id: str, user_id: str, answer_ids: List[str]
    ) -> dict[str, str]:
        return self._runtime.memory_service.answer_memory_links(
            notebook_id, user_id, answer_ids
        )

    def memory_revisions(self, memory_id: str, user_id: str) -> list:
        return self._runtime.memory_service.revisions(memory_id, user_id)

    def propose_memory_promotion(
        self, memory_id: str, user_id: str, *, target_base_id: str = ""
    ) -> dict:
        return self._runtime.memory_service.propose_promotion(
            memory_id, user_id, target_base_id=target_base_id
        )

    # ------------------------------------------------------------------
    # Sharing / membership / deep-copy domain (Task 9): composed
    # NotebookSharingService + NotebookCopyService + SharingStore delegates.
    # Frozen public signatures; the facade keeps notebook_copy_stats' own
    # scale-profile memo (cross-domain bridge) and the _insert_row seat.
    # ------------------------------------------------------------------
    def share_notebook(self, notebook_id: str) -> dict:
        return self._runtime.sharing.share_notebook(notebook_id)

    def share_state(self, notebook_id: str) -> dict:
        return self._runtime.sharing.share_state(notebook_id)

    def unshare_notebook(self, notebook_id: str) -> None:
        return self._runtime.sharing.unshare_notebook(notebook_id)

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        return self._runtime.sharing.find_notebook_by_share_token(token)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return self._runtime.scale_artifacts.notebook_copy_stats(notebook_id)

    def shared_preview(self, notebook_id: str) -> dict:
        return self._runtime.sharing.shared_preview(notebook_id)

    def shared_by_me(self, user_id: str) -> list:
        return self._runtime.sharing.shared_by_me(user_id)

    def _insert_row(self, db, table: str, data: dict) -> None:
        return self._runtime.sharing_store.insert_row_values(db, table, data)

    def _sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._runtime.sharing.sweep_stuck_copies(created_by)

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        actor_label: "str | None" = None,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        return self._runtime.sharing.copy_notebook(
            source_notebook_id, new_owner_id=new_owner_id,
            actor_label=actor_label, new_name=new_name,
        )

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_access_notebook(notebook_id, user_id)

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.is_member(notebook_id, user_id)

    def user_can_admin_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_admin_notebook(notebook_id, user_id)

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_read_notebook(notebook_id, user_id)

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_read_source(source_id, user_id)

    def add_member(self, notebook_id: str, user_id: str) -> None:
        return self._runtime.sharing.add_member(notebook_id, user_id)

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        return self._runtime.sharing.remove_member(notebook_id, user_id)

    def kick_all_members(self, notebook_id: str) -> None:
        return self._runtime.sharing.kick_all_members(notebook_id)

    def list_members(self, notebook_id: str) -> list:
        return self._runtime.sharing.list_members(notebook_id)

    def join_shared(self, notebook_id: str, user_id: str) -> NotebookSummary:
        return self._runtime.sharing.join_shared(notebook_id, user_id)

    def leave_notebook(self, notebook_id: str, user_id: str) -> None:
        return self._runtime.sharing.leave_notebook(notebook_id, user_id)

    def source_owner(self, source_id: str) -> "str | None":
        return self._runtime.sharing.source_owner(source_id)

    def source_notebook_id(self, source_id: str) -> "str | None":
        return self._runtime.sharing.source_notebook_id(source_id)

    def conversation_owner(self, conversation_id: str) -> "str | None":
        return self._runtime.sharing.conversation_owner(conversation_id)

    def answer_owner(self, answer_id: str) -> "str | None":
        return self._runtime.sharing.answer_owner(answer_id)

    def user_can_read_answer(self, answer_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_read_answer(answer_id, user_id)

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        """Delete all KG artifacts for a notebook — KnowledgeLifecycleService
        owns the orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.delete_notebook_kg(notebook_id)

    # ------------------------------------------------------------------
    # KG search: FTS5 (lexical) + ANN (semantic) + hydration
    # ------------------------------------------------------------------

    def backfill_kg_fts(self, notebook_id: str) -> int:
        """Re-populate kg_objects_fts from knowledge_objects for this notebook.

        Idempotent: deletes existing FTS rows first, then re-inserts from
        knowledge_objects (non-deprecated, non-empty name).  Returns the
        number of rows inserted.
        """
        return self._runtime.knowledge_query.backfill_kg_fts(notebook_id)

    def backfill_chunk_fts(self, notebook_id: str) -> int:
        """从 chunks 重建 chunks_fts(DELETE+re-INSERT)。返回写入行数。

        幂等,best-effort 派生索引:先删本 notebook 的 FTS 行,再从 chunks 全量重插。
        供 copy_notebook / 刷新图谱等重建派生索引的路径调用。
        """
        return self._runtime.knowledge_query.backfill_chunk_fts(notebook_id)

    def _semantic_search(self, notebook_id: str, q: str, k: int) -> list:
        """ANN semantic search over the notebook's scale index.

        Returns [{object_id, name:'', score, match:'semantic'}] or []
        on any failure / missing index.
        """
        return self._runtime.knowledge_query.semantic_search(notebook_id, q, k)

    def _hydrate_search_hits(self, notebook_id: str, hits: list) -> list:
        """Enrich merged hits with object_type and fill missing names.

        Drops hits for objects that no longer exist or are deprecated.
        Always uses the fresh payload name when available (Fix 2: overrides
        the possibly-stale FTS-provided name so renames are reflected).
        """
        return self._runtime.knowledge_query.hydrate_search_hits(notebook_id, hits)

    def _fold_hits_to_canonical(self, notebook_id: str, hits: list, k: int) -> list:
        """Fix 1: fold raw ko-<obj> ids in hits to K-<canonical> ids.

        Uses a BOUNDED query (only the ≤k hit ids) against concept_clusters —
        does NOT load the full cluster_map which can be 5M entries at scale.
        For each hit that has a cluster row: replace object_id with canonical_id
        and set name to canonical_name (so it matches the viz node label).
        Hits without a cluster row (non-concept types, or pre-rebuild state) are
        kept as-is. After folding, dedup by object_id keeping MAX score, re-sort
        by score desc, truncate to k.
        """
        return self._runtime.knowledge_query.fold_hits_to_canonical(notebook_id, hits, k)

    def kg_search(self, notebook_id: str, q: str, k: int = 30) -> list:
        """Search KG objects by name (FTS5 lexical) union ANN semantic.

        Returns [{object_id, name, object_type, score, match}] sorted by score desc.
        Raises KeyError if notebook not found.

        Fix 1: after hydration, folds raw ko-<obj> concept ids to K-<canonical>
        ids via a bounded concept_clusters lookup so search results share the same
        id space as the viz graph — enabling click-to-expand on search hits.
        """
        return self._runtime.knowledge_query.search(notebook_id, q, k)


    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        return self._runtime.source_ingestion.list_sources(notebook_id)

    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50,
                          q: str = "") -> PaginatedSources:
        """分页 + 可选 q(按 title/file_name 服务端过滤)。万级 source 安全:只取一页 +
        一次 COUNT,不全量进内存。"""
        return self._runtime.source_ingestion.list_sources_page(
            notebook_id, offset, limit, q
        )

    def visible_source_ids(self, notebook_id: str, source_ids: List[str]) -> List[str]:
        """Return the requested ids that are visible imported sources of notebook."""
        return self._runtime.index_projections.visible_source_ids(notebook_id, source_ids)

    def all_visible_source_ids(self, notebook_id: str) -> List[str]:
        """Return the current notebook's complete user-visible source id set."""
        return self._runtime.source_store.all_visible_source_ids(notebook_id)

    def hidden_source_ids(self, notebook_id: str, owner_id: str) -> List[str]:
        """Hidden Memory/Knowhow projection participants for ONE user.
        Knowhow projections are notebook-wide; a Memory projection belongs
        only to the user who created it, filtered in the adapter's SQL."""
        return self._runtime.source_store.hidden_source_ids(notebook_id, owner_id)

    def visible_source_count(self, notebook_id: str) -> int:
        return self._runtime.source_store.visible_document_count(notebook_id)

    def visible_source_scope_snapshot(
        self, notebook_id: str, source_ids: List[str]
    ) -> tuple[List[str], int]:
        """Validate a compact include scope without materializing the universe."""
        return self._runtime.source_store.visible_source_scope_snapshot(
            notebook_id, source_ids
        )

    def get_source(self, source_id: str) -> SourceDetail:
        return self._runtime.source_store.get_source(source_id)

    def report_source_rows(
        self, notebook_id: str, *, representative_limit: int = 20,
        distribution_limit: int = 32,
    ) -> dict[str, Any]:
        return self._runtime.source_store.report_source_rows(
            notebook_id,
            representative_limit=representative_limit,
            distribution_limit=distribution_limit,
        )

    def report_source_identity_rows(
        self, source_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        return self._runtime.source_store.report_source_identity_rows(source_ids)

    def _source_pipeline_hooks(self) -> SourcePipelineHooks:
        """Task 12: mint FRESH hooks on every ingestion call from the facade's
        own bound seats — post-construction per-instance monkeypatches
        (_run_extraction, _mark_unified_kg_dirty, ...) keep being observed.
        Gate 5 replaces these temporary callbacks with real services.  Never
        store the hooks (not on the runtime, not on the facade)."""
        return self._runtime.source_ingestion.pipeline_hooks()

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]:
        return self._runtime.source_ingestion.import_sources_compat(notebook_id, payload)

    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
        capacity_limit: Optional[int] = None,
        agent_profile_id: str = "",
    ) -> AddUrlSourcesResult:
        return self._runtime.source_ingestion.add_url_sources_compat(
            notebook_id, urls, scheduler, capacity_limit, agent_profile_id
        )

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: Optional[Callable[[str], None]] = None,
        agent_profile_id: str = "",
        capacity_limit: Optional[int] = None,
    ) -> List[UploadedSourceSummary]:
        return self._runtime.source_ingestion.upload_sources_compat(
            notebook_id, files, scheduler, agent_profile_id, capacity_limit
        )

    def _set_source_status(
        self,
        source_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        error_message: str = "",
    ) -> None:
        self._runtime.source_ingestion.set_source_status(
            source_id, status, summary=summary, error_message=error_message
        )

    def _notebook_has_kg(self, notebook_id: str) -> bool:
        """True iff this notebook has any knowledge_objects row."""
        return self._runtime.knowledge.has_kg(notebook_id)

    # CJK unified-ideograph block: presence ⇒ Chinese content.
    _CJK_RE = CandidateRetrievalService._CJK_RE
    _LATIN_RE = CandidateRetrievalService._LATIN_RE

    def _notebook_langs(self, notebook_id: str) -> List[str]:
        return self.retrieval.candidates._notebook_langs(notebook_id)

    def _should_extract_kg(self, notebook_id: str) -> bool:
        return self._runtime.source_ingestion.should_extract_kg(notebook_id)

    def build_notebook_kg(
        self,
        notebook_id: str,
        *,
        progress=None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize=None,
        job_id: str | None = None,
        skip_policy=None,
    ) -> dict:
        """按需构建 KG — KnowledgeLifecycleService 拥有编排(Task 15,含
        kg_building 单飞守卫、目标策略与 governance 冲突消解直连)。

        skip_policy 只由离线批量的显式跳过模式传入(见 ModelSkipPolicy);
        API 路径一律省略,保持"模型不可用即熔断整个任务"的既有契约。"""
        return self._runtime.knowledge_lifecycle.build_notebook_kg(
            notebook_id,
            progress=progress,
            target_limit=target_limit,
            retry_partial=retry_partial,
            finalize=finalize,
            job_id=job_id,
            skip_policy=skip_policy,
        )

    def rebuild_notebook_kg(self, notebook_id: str) -> dict:
        """Full re-extract (delete+build, kg_building 覆盖 delete 阶段) —
        KnowledgeLifecycleService owns the orchestration (Task 15)."""
        return self._runtime.knowledge_lifecycle.rebuild_notebook_kg(notebook_id)

    def _parse_url_via_local(
        self, source_id: str, url: str, file_name: str
    ) -> List[SourceElement]:
        return self._runtime.source_ingestion.parse_url_via_local(
            source_id, url, file_name
        )

    def process_source(self, source_id: str) -> SourceSummary:
        return self._runtime.source_ingestion.process_source_compat(source_id)

    def source_parse_busy(self, source_id: str, *, timeout: float = 0.0) -> bool:
        """Is this source's parse pipeline running right now?

        A bounded PROBE of the ingestion service's own per-source chunk lock —
        the same mutex ``process_source`` holds continuously from
        ``replace_elements`` through ``build_chunks``, and the same one
        ``CommandCatalogService._source_write_barrier`` takes to serialize
        against it. Taking it and immediately dropping it answers "is a parse
        in flight" without inventing a second status signal (``parse_status``
        is written at stage boundaries and would report ``parsed`` in the
        middle of chunk building).

        Exposed as a boolean rather than by re-exporting the context manager
        because the one caller (MCP's ``reparse_source``) cannot hold the lock
        across its own submit anyway: it hands the work to the background
        scheduler and returns. The residual race — idle at probe time, busy by
        the time the job thread starts — is accepted and documented on that
        tool; ``process_source`` serializes on this very lock, so the worst
        case is duplicated work, never interleaved writes.

        ``timeout`` is the bounded acquire wait, in seconds, forwarded verbatim
        to ``try_hold_source_chunk_lock``. The default 0.0 makes this a
        non-blocking probe.
        """
        with self._runtime.source_ingestion.try_hold_source_chunk_lock(
            source_id, timeout=timeout
        ) as acquired:
            return not acquired

    def parse_source(self, source_id: str) -> SourceSummary:
        return self._runtime.source_ingestion.parse_source_compat(source_id)

    def _augment_notebook_meta(self, notebook_id: str, pending_source_id: str = "") -> None:
        return self._runtime.source_ingestion.augment_notebook_metadata(
            notebook_id, pending_source_id
        )

    # --- Task-12 catalog callbacks — SQL now lives on the notebook/source
    # --- stores (Task 26); the ingestion service owns the metadata-
    # --- augmentation BUSINESS body and calls back through the constructor-
    # --- injected seams below (the facade keeps its connection boundaries).
    def _notebook_meta_row(self, notebook_id: str) -> Optional[dict]:
        return self._runtime.notebook_store.meta_for_notebook(notebook_id)

    def _notebook_meta_sources(
        self, notebook_id: str, pending_source_id: str = ""
    ) -> List[dict]:
        return self._runtime.source_store.meta_sources(notebook_id, pending_source_id)

    def _apply_notebook_meta(
        self, notebook_id: str, *, guard_name, name: str, purpose: str
    ) -> None:
        return self._runtime.notebook_store.apply_meta_for_notebook(
            notebook_id, guard_name=guard_name, name=name, purpose=purpose
        )

    def source_elements(self, source_id: str) -> List[SourceElement]:
        return self._runtime.source_store.source_elements(source_id)

    def source_elements_page(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 40,
        anchor_element_id: str = "",
    ):
        return self._runtime.source_store.source_elements_page(
            source_id, offset, limit, anchor_element_id
        )

    def source_id_by_hash(self, notebook_id: str, digest: str) -> Optional[str]:
        """The existing same-content source in this notebook, if any — the very
        lookup ``upload_sources`` uses for its dedup short-circuit.

        Exposed on the facade so a caller can ASK whether an upload would be a
        reuse before committing to one. MCP's ``add_source_text`` needs that to
        order two independent rules correctly: the per-notebook document ceiling
        must not refuse a call that is about to add no document at all. Reusing
        this method rather than re-deriving "have I seen these bytes" keeps one
        dedup rule (its docstring on the store is the authority: empty digest
        never matches, hidden synthetic rows excluded, oldest row wins).
        """
        return self._runtime.source_store.source_id_by_hash(notebook_id, digest)

    def source_metadata(
        self, source_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """The narrow source-card projection (one SQL, no paper-author/element
        -count/KG hydration), keyed by source id.

        Exposed on the facade for MCP's ``get_cited_element``: a citation
        point-read needs the owning notebook, the source type and the display
        -title columns, and nothing else — ``get_source`` would fan out into
        paper authors, an element ``COUNT(*)``, a KG ``EXISTS`` and the private
        ``error_message``, every one of which that caller discards. Agents call
        the point-read in a loop, so the discarded work is the whole cost.
        """
        return self._runtime.source_store.source_metadata(source_ids)

    def evidence_elements(
        self, element_ids: Sequence[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Raw ``source_elements`` rows by id (one primary-key SQL), the same
        reader ``EvidenceContextService`` hydrates citations from.

        ``metadata`` arrives as the stored JSON TEXT on both backends, which is
        exactly the carrier ``evidence_context._knowhow_ref`` parses — so a
        consumer that needs both the element and its knowhow pointer reuses
        that one judgement instead of restating it.
        """
        return self._runtime.source_store.evidence_elements(element_ids)

    def delete_source(self, source_id: str) -> None:
        return self._runtime.source_ingestion.delete_source_compat(source_id)

    def extract_source(self, source_id: str) -> None:
        """Public entry to (re-)extract a single source's KG in place. Delegates to
        _run_extraction, which deletes the source's prior KG objects/relations/
        embeddings and re-runs extraction (idempotent per source). Used by
        reextract_notebook and maintenance scripts."""
        return self._runtime.source_ingestion.run_extraction(source_id)

    def _relink_extra_relations(
        self, objects: List[dict], relations: List[dict], source_id: str
    ) -> List[dict]:
        return self._runtime.source_ingestion.relink_extra_relations(
            objects, relations, source_id
        )

    def _run_extraction(self, source_id: str) -> None:
        return self._runtime.source_ingestion.run_extraction(source_id)

    # --- TEMPORARY Task-12 KG callbacks (Task 13/15 targets: KnowledgeStore /
    # --- KnowledgeLifecycleService).  The ingestion service owns the
    # --- extraction ORCHESTRATION and calls back through these seams so no
    # --- SQL leaks into the application service.
    def _begin_extraction_run(
        self,
        source_id: str,
        notebook_id: str,
        run_id: str,
        created_at: str,
        *,
        preserve_existing: bool = False,
    ) -> None:
        """Open one source run, optionally retaining its graph for safe retry."""
        return self._runtime.knowledge.begin_extraction(
            source_id,
            notebook_id,
            run_id,
            created_at,
            preserve_existing=preserve_existing,
        )

    def _finish_extraction_run(self, run_id: str, status: str, message: str) -> None:
        return self._runtime.knowledge.finish_extraction(run_id, status, message)

    def _notebook_tier(self, notebook_id: str) -> str:
        return self._runtime.notebook_store.tier(notebook_id)

    def _source_raw_text(self, source, elements) -> str:
        return self._runtime.source_files.read_source(source, elements)

    def _embed_source(self, source_id: str) -> None:
        return self._runtime.source_embedding.embed_source(source_id)

    def _embed_knowledge(
        self,
        object_id: str,
        notebook_id: str,
        payload: Dict[str, object],
    ) -> None:
        """Embed a knowledge object's own payload text (WS4: payload-level
        vectors, not just evidence-element vectors). No-op without embeddings."""
        return self._runtime.source_embedding.embed_knowledge(
            object_id, notebook_id, payload
        )

    def _flush_object_vectors(self, notebook_id: str, rows: list) -> None:
        """把一批 (oid, vector) 落 knowledge_embeddings(一个写事务,幂等 REPLACE)。"""
        return self._runtime.source_embedding.flush_object_vectors(notebook_id, rows)

    def _embed_objects_batch(self, notebook_id: str, items: List[dict],
                             progress=None, commit_every: Optional[int] = None) -> int:
        return self._runtime.source_embedding.embed_objects_batch(
            notebook_id, items, progress=progress, commit_every=commit_every
        )

    def _embed_relations_batch(self, notebook_id: str, rel_items: List[dict]) -> None:
        return self._runtime.source_embedding.embed_relations_batch(
            notebook_id, rel_items
        )

    def _build_chunks_for_source(self, source_id: str) -> None:
        return self._runtime.source_chunking.build_chunks_for_source(source_id)

    def _embed_chunks_for_source(self, source_id: str) -> None:
        return self._runtime.source_embedding.embed_chunks_for_source(source_id)

    def _chunk_and_embed_source(self, source_id: str) -> None:
        return self._runtime.source_chunking.chunk_and_embed_source(source_id)

    def _embed_chunks_batch(self, notebook_id: str, items: List[dict]) -> None:
        return self._runtime.source_embedding.embed_chunks_batch(notebook_id, items)

    def _backfill_knowledge_embeddings(self, db: object,
                                       notebook_id: str, objects: List[dict],
                                       progress=None) -> None:
        """Embed + persist any knowledge objects missing a vector, concurrently
        (reuses the concurrent embed_objects_batch). No-op when all are embedded
        or no embedder. Lets ask() build a complete knowledge matrix without
        materializing a Python-list dict of every vector.  SourceEmbeddingService
        owns the orchestration (Task 26); frozen-signature delegate."""
        return self._runtime.source_embedding.backfill_knowledge_embeddings(
            db, notebook_id, objects, progress=progress
        )


    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]:
        """All object types present in this notebook with non-deprecated counts,
        so the UI can render a tab per type — including academic/textbook types
        that have no bespoke card."""
        return self._runtime.knowledge_query.knowledge_types(notebook_id)

    def _knowledge_record(
        self, object_type: str, obj: dict, schema: Optional[ObjectSchema]
    ) -> KnowledgeRecord:
        """KG 对象 → KnowledgeRecord 展示投影 — canonical body 随 ask_reasoning
        的 related_knowledge 消费点移入 ask_service (Task 24);list_knowledge
        继续经本冻结签名 delegate 使用同一投影。"""
        return self._runtime.knowledge_query.knowledge_record(object_type, obj, schema)

    def list_knowledge(
        self,
        notebook_id: str,
        object_type: str,
        status: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> PaginatedKnowledge:
        """Generic, type-agnostic paginated listing for any object type.

        status=None preserves the original behaviour: no status filter (all
        statuses returned), matching the old ``statuses=None`` call to
        ``_knowledge_objects``.  Pass a non-empty string to filter to that
        one status.
        """
        return self._runtime.knowledge_query.list_knowledge(
            notebook_id, object_type, status, offset, limit
        )

    # --- Editable extraction-schema registry (Task 13: SchemaRegistryService
    # --- owns validation/sampling/LLM induction; KnowledgeStore owns rows) ---
    @staticmethod
    def _object_schema_from_row(row) -> ObjectSchemaModel:
        return SchemaRegistryService.from_row(row)

    def effective_schemas(self, notebook_id: str = "") -> Dict[str, ObjectSchema]:
        """Active object schemas as an ObjectSchema registry for extraction —
        DB rows overlaid on the code defaults."""
        return self._runtime.schema_registry.effective_schemas(notebook_id)

    def list_object_schemas(self, *, can_edit: bool = False) -> List[ObjectSchemaModel]:
        return self._runtime.schema_registry.list_object_schemas(can_edit=can_edit)

    def list_notebook_object_schemas(
        self, notebook_id: str, *, can_edit: bool = False
    ) -> List[ObjectSchemaModel]:
        return self._runtime.schema_registry.list_notebook_object_schemas(
            notebook_id, can_edit=can_edit
        )

    def create_notebook_object_schema(
        self, notebook_id: str, payload: ObjectSchemaCreate, *, created_by: str
    ) -> ObjectSchemaModel:
        return self._runtime.schema_registry.create_notebook_object_schema(
            notebook_id, payload, created_by=created_by
        )

    def update_notebook_object_schema(
        self,
        notebook_id: str,
        object_type: str,
        payload: ObjectSchemaUpdate,
        *,
        created_by: str,
    ) -> ObjectSchemaModel:
        return self._runtime.schema_registry.update_notebook_object_schema(
            notebook_id, object_type, payload, created_by=created_by
        )

    def delete_notebook_object_schema(
        self, notebook_id: str, object_type: str
    ) -> str:
        return self._runtime.schema_registry.delete_notebook_object_schema(
            notebook_id, object_type
        )

    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel:
        return self._runtime.schema_registry.create_object_schema(payload)

    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel:
        return self._runtime.schema_registry.update_object_schema(object_type, payload)

    def delete_object_schema(self, object_type: str) -> None:
        self._runtime.schema_registry.delete_object_schema(object_type)

    def propose_schemas(self, notebook_id: str) -> List[ObjectSchemaModel]:
        """Schema induction (suggestion mode): inspect the notebook's content and
        propose NEW object types the current schema does not cover. Proposals are
        stored with status='proposed' for curator approval; never auto-activated.
        Requires the LLM; offline this is a no-op that returns existing proposals."""
        return self._runtime.schema_registry.propose_schemas(notebook_id)

    def knowledge_graph(self, notebook_id: str) -> KnowledgeGraph:
        """KG-native graph: nodes = non-deprecated knowledge objects (4 KG types),
        edges = knowledge_relations rows.

        Legacy endpoint (GET /notebooks/{id}/graph) — superseded by
        /unified-kg's bounded/paginated graph view; no frontend caller uses
        this route anymore. It never had a large-notebook guard: unlike
        unified_graph (which falls back to the persisted, bounded viz index
        above settings.viz_sync_build_max_objects), this method always pulls
        EVERY non-deprecated knowledge_objects row (full payload, for
        _kg_headline) into Python objects with no cap — a 490k-object
        deployment would materialize the whole KG in memory synchronously on
        the request thread. Since there's no bounded variant for this legacy
        shape to fall back to (unlike unified_graph), guard by outright
        rejecting large notebooks with a clear pointer to the real endpoint,
        rather than silently truncating a "complete" graph response into a
        misleadingly-partial one."""
        return self._runtime.knowledge_query.graph(notebook_id)

    def _kg_headline(self, payload: dict) -> str:
        return self._runtime.knowledge_query.kg_headline(payload)

    def add_relations(self, notebook_id: str, source_id: str,
                      relations: List[dict]) -> int:
        return self._runtime.knowledge.add_relations_current(
            notebook_id, source_id, relations
        )

    def store_kg(self, notebook_id: str, source_id: Optional[str],
                 objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
        """Insert KG nodes/edges — KnowledgeLifecycleService owns the chunked
        write orchestration (Task 15; the frozen phase order object chunks →
        relation chunks → embeds → invalidate → dirty is unchanged)."""
        return self._runtime.knowledge_lifecycle.store_kg(
            notebook_id, source_id, objects, relations
        )

    def relink_notebook_kg(self, notebook_id: str) -> dict:
        """Backfill relink of degree-0 KG nodes — KnowledgeLifecycleService
        owns the orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.relink_notebook_kg(notebook_id)

    def start_notebook_relink(self, notebook_id: str) -> dict:
        """Claim the notebook's relink slot (409 source) — Task-15 delegate."""
        return self._runtime.knowledge_lifecycle.start_notebook_relink(notebook_id)

    def notebook_relink_status(self, notebook_id: str) -> dict:
        """Latest relink state for this notebook — Task-15 delegate."""
        return self._runtime.knowledge_lifecycle.notebook_relink_status(notebook_id)

    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict:
        """Background relink entry point — Task-15 delegate."""
        return self._runtime.knowledge_lifecycle.run_notebook_relink_job(
            notebook_id, job_id
        )

    def fail_notebook_relink_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        """Release the relink claim when the worker never started — Task-15."""
        return self._runtime.knowledge_lifecycle.fail_notebook_relink_submission(
            notebook_id, job_id
        )

    def conflict_resolution_admitted(self, notebook_id: str) -> bool:
        """Size admission for conflict detection — shared by endpoint and build tail."""
        return self._runtime.knowledge_lifecycle.conflict_resolution_admitted(
            notebook_id
        )

    def start_conflict_resolution(self, notebook_id: str) -> dict:
        """Claim the notebook's conflict-detection slot (409 source).

        A slot of its own, separate from relink/rebuild — see
        ``KgMaintenanceJobs``' module docstring.
        """
        return self._runtime.knowledge_lifecycle.start_conflict_resolution(
            notebook_id
        )

    def run_conflict_resolution_job(self, notebook_id: str, job_id: str) -> dict:
        """Background conflict-detection entry point (settles on every exit)."""
        return self._runtime.knowledge_lifecycle.run_conflict_resolution_job(
            notebook_id, job_id
        )

    def fail_conflict_resolution_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        """Release the conflict claim when the worker never started."""
        return (
            self._runtime.knowledge_lifecycle
            .fail_conflict_resolution_submission(notebook_id, job_id)
        )

    def relations_for_notebook(self, notebook_id: str) -> List[dict]:
        return self._runtime.knowledge_query.relations_for_notebook(notebook_id)

    # --- Track E: edge trust review queue + curation feedback loop ----------
    _REVIEW_STATUSES = KnowledgeGovernanceService._REVIEW_STATUSES

    def _edge_centrality_map(self, notebook_id: str) -> Dict[str, float]:
        """Cached {rel_id: edge_betweenness_centrality} over the live (non-rejected)
        graph, keyed on tuple(_scale_index_version(nb)) — version-cached like
        _vector_matrix/_ent_chunk_map so review_queue's O(V·E) Brandes run
        (rustworkx digraph_edge_betweenness_centrality) pays once per KG version
        instead of once per HTTP request. At 490k-node scale this used to be
        minutes of synchronous CPU on the request thread, every request.

        Bounding (P0-3, reworked): when the FULL node count exceeds
        settings.edge_centrality_max_nodes, the loader itself stays bounded —
        it never materializes the full objects/relations graph before cutting
        it down. Instead:
          1. Degree ranking via SQL: `GROUP BY source_object_id` and `GROUP BY
             target_object_id` COUNT(*) over knowledge_relations (non-rejected),
             merged in a Python dict — bounded by the distinct node count
             touched by an edge (objects with zero relations never rank and are
             irrelevant to edge betweenness anyway: an isolated node cannot be
             an edge endpoint). This entirely avoids loading knowledge_objects'
             payload (name/type — unused by compute_edge_centrality, which only
             reads back `rel_id`) for the ranking step.
          2. Only relations with BOTH endpoints in the top-K id set are loaded
             (edges to/from out-of-top-K nodes are dropped from the graph
             entirely, same "centrality 0.0 for those edges" semantics as the
             old post-hoc subgraph cut). Uses `json_each(?)` — SQLite's
             built-in table-valued function that turns a single bound JSON
             array parameter into a virtual row set — JOINed against
             knowledge_relations, instead of either (a) a many-thousand-
             placeholder `IN (...)` list, which risks SQLITE_MAX_VARIABLE_
             NUMBER-adjacent slowness/limits at K's default of 20000, or
             (b) a `CREATE TEMP TABLE` + INSERT, which is a real SQL write
             this repo's write-serialization convention requires funneling
             through `_write()` (see test_all_writes_go_through_write_lock) —
             json_each needs neither DDL nor an INSERT, it is a pure read-side
             join, so the read-only `_connect()` path stays correct.
          3. build_rx_graph + compute_edge_centrality run exactly as before, on
             the now-already-bounded (top-K-only) node/edge set — no post-hoc
             subgraph cut needed since the load itself is scoped.

        Under-K graphs: identical result to before (no bounding kicks in — the
        full node set IS the "top-K" set, SQL ranking is order-preserving
        wrt/betweenness since every node participates either way). Ties in the
        SQL degree ranking are broken by `id` (deterministic, unlike the old
        node-insertion-order tiebreak — see the equivalence test for the
        exact-K-boundary case, which is not order-sensitive: nodes past the
        cut contribute 0 either way since they're not edge endpoints for any
        edge that survives, or the graph is under K and no cut happens at all).
        """
        return self._runtime.knowledge_query.edge_centrality_map(notebook_id)

    def review_queue(self, notebook_id: str, limit: int = 200) -> List[dict]:
        """Edge trust review queue — KnowledgeGovernanceService owns the
        orchestration (Task 16; centrality stays version-cached through the
        facade's _edge_centrality_map port). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.review_queue(notebook_id, limit)

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation —
        KnowledgeGovernanceService owns the orchestration (Task 16; the frozen
        commit→dirty→invalidate phase order is unchanged). Frozen-signature
        delegate."""
        return self._runtime.knowledge_governance.set_edge_review(
            notebook_id, rel_id, status
        )

    def _delete_relations_for_source(self, db, source_id: str) -> None:
        self._runtime.knowledge.delete_relations_for_source(db, source_id)

    # --- Concept-cluster / merge-candidate CRUD (Task 5) -------------------

    def write_clusters(self, notebook_id: str, rows: List[dict],
                       object_type: str = "concept") -> None:
        """concept_clusters 全量重写 — KnowledgeLifecycleService owns the
        orchestration (Task 15; replace + cluster-seq bump stay ONE commit)."""
        return self._runtime.knowledge_lifecycle.write_clusters(
            notebook_id, rows, object_type
        )

    def append_clusters(self, notebook_id: str, rows: list, object_type: str = "concept") -> int:
        """追加写 concept_clusters — KnowledgeLifecycleService owns the
        orchestration (Task 15; append + bump one commit, invalidate 仅在新增)。"""
        return self._runtime.knowledge_lifecycle.append_clusters(
            notebook_id, rows, object_type
        )

    def incremental_fuse_source(self, notebook_id: str, source_id: str) -> None:
        """上传后增量融合该源 concept 进 concept_clusters — KnowledgeLifecycleService
        owns the orchestration (Task 15; Tier1 名种子 append + Tier2 向量桥不变)。"""
        return self._runtime.knowledge_lifecycle.incremental_fuse_source(
            notebook_id, source_id
        )

    def _tier2_bridge_candidates_ann(self, notebook_id: str, idx, ann,
                                     new_objs: list,
                                     frozen_canonical: Dict[str, str]) -> list:
        """ANN-backed Tier2 bridge candidate detection — KnowledgeLifecycleService
        owns the body (Task 15); frozen-signature delegate.

        PR-C:整表 ``cluster_map_`` 形参换成了 ``frozen_canonical`` —— ANN 命中的
        canonical 折叠改走有界 ``cluster_fold_rows``,但**本批新对象**那几个 id 的
        取值必须由调用方在 ``append_clusters`` 之前冻结好交进来(合同与理由见服务端
        方法的 docstring)。"""
        return self._runtime.knowledge_lifecycle._tier2_bridge_candidates_ann(
            notebook_id, idx, ann, new_objs, frozen_canonical
        )
    def cluster_map(self, notebook_id: str) -> Dict[str, str]:
        """Cached {member_object_id: canonical_id} — RetrievalCandidateService owns
        the loader + version-cache body (Task 21); frozen-signature delegate。单一
        owner：此前 facade 与服务各留一份等价实现、共享同一缓存键（"{nb}:clustermap"），
        属双所有权漂移地雷（review 抓出），降为委托后 loader 只此一家。"""
        return self.retrieval.candidates.cluster_map(notebook_id)

    def write_merge_candidate(self, notebook_id: str, a: str, b: str, score: float) -> None:
        """Merge-candidate insert — KnowledgeGovernanceService owns the body
        (Task 16); frozen-signature delegate."""
        return self._runtime.knowledge_governance.write_merge_candidate(
            notebook_id, a, b, score
        )

    def pending_merges(self, notebook_id: str) -> List[dict]:
        return self._runtime.knowledge_governance.pending_merges(notebook_id)

    def _pending_merges_batch(self, notebook_id: str, limit: int) -> List[dict]:
        """Bounded SQL-LIMIT fetch of pending merge candidates —
        KnowledgeGovernanceService owns the body (Task 16); frozen-signature
        delegate."""
        return self._runtime.knowledge_governance._pending_merges_batch(
            notebook_id, limit
        )

    def _has_pending_merges(self, notebook_id: str) -> bool:
        """Cheap EXISTS continuation test for the merge-review drain loop —
        KnowledgeGovernanceService owns the body (Task 16); frozen-signature
        delegate."""
        return self._runtime.knowledge_governance._has_pending_merges(notebook_id)

    def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
        return self._runtime.knowledge_governance.set_merge_decision(
            notebook_id, candidate_id, status
        )

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        """Confirm a merge candidate — KnowledgeGovernanceService owns the
        orchestration (Task 16; the frozen commit→invalidate→dirty order is
        unchanged). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.confirm_merge(
            notebook_id, candidate_id
        )

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        """Reject a merge candidate — KnowledgeGovernanceService owns the
        orchestration (Task 16). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.reject_merge(
            notebook_id, candidate_id
        )

    # ------------------------------------------------------------------
    # kg_conflict_candidates — KnowledgeGovernanceService owns the domain
    # (Task 16). Detection lives in conflict_detect.py (T2); adjudication in
    # conflict_review.py (T3); write-back in apply_conflict_resolution (T4);
    # orchestration in resolve_notebook_conflicts (T5). The facade keeps
    # frozen-signature delegates; the compound flows keep routing the
    # candidate-status transaction through THIS facade's set_conflict_status
    # wrapper (the frozen phase contracts patch it).
    # ------------------------------------------------------------------

    def write_conflict_candidate(
        self,
        notebook_id: str,
        kind: str,
        left_ref: str,
        right_ref: str,
        conflict_type: Optional[str] = None,
        resolution: Optional[str] = None,
        winner_ref: Optional[str] = None,
        resolved_payload: Optional[str] = None,
        confidence: Optional[float] = None,
        rationale: Optional[str] = None,
    ) -> str:
        """Insert one conflict candidate — KnowledgeGovernanceService owns the
        body (Task 16); frozen-signature delegate."""
        return self._runtime.knowledge_governance.write_conflict_candidate(
            notebook_id, kind, left_ref, right_ref,
            conflict_type, resolution, winner_ref, resolved_payload,
            confidence, rationale,
        )

    def pending_conflicts(self, notebook_id: str) -> List[dict]:
        """Return all conflict candidates with status='pending' for a notebook."""
        return self._runtime.knowledge_governance.pending_conflicts(notebook_id)

    def set_conflict_status(self, notebook_id: str, candidate_id: str, status: str) -> None:
        """Update status to 'applied' or 'rejected' (+ updated_at) —
        KnowledgeGovernanceService owns the body (Task 16). This wrapper stays
        the compound flows' candidate-status seat (confirm/reject/resolve ride
        it late-bound, so the frozen phase contracts keep intercepting here).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.set_conflict_status(
            notebook_id, candidate_id, status
        )

    def get_conflict_candidate(self, notebook_id: str, candidate_id: str) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        return self._runtime.knowledge_governance.get_conflict_candidate(
            notebook_id, candidate_id
        )

    def apply_conflict_resolution(
        self,
        notebook_id: str,
        *,
        kind: str,
        left_ref: str,
        right_ref: str,
        resolution: str,
        winner_ref: Optional[str] = None,
        resolved_payload: Optional[dict] = None,
    ) -> dict:
        """Execute ONE adjudicated conflict resolution against the KG —
        KnowledgeGovernanceService owns the multi-step orchestration (Task 16;
        the frozen conflict double dirty bumps are unchanged).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.apply_conflict_resolution(
            notebook_id,
            kind=kind,
            left_ref=left_ref,
            right_ref=right_ref,
            resolution=resolution,
            winner_ref=winner_ref,
            resolved_payload=resolved_payload,
        )

    def confirm_conflict(self, notebook_id: str, candidate_id: str) -> dict:
        """Apply a pending conflict candidate and mark it as 'applied' —
        KnowledgeGovernanceService owns the orchestration (Task 16; the
        mutation-commits-before-candidate-status boundary is unchanged and the
        status transaction still rides this facade's set_conflict_status).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.confirm_conflict(
            notebook_id, candidate_id
        )

    def reject_conflict(self, notebook_id: str, candidate_id: str) -> None:
        """Reject a pending conflict candidate (no KG mutation) —
        KnowledgeGovernanceService owns the orchestration (Task 16).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.reject_conflict(
            notebook_id, candidate_id
        )

    def resolve_notebook_conflicts(self, notebook_id: str) -> dict:
        """Detect → adjudicate → (optionally) auto-apply KG conflicts —
        KnowledgeGovernanceService owns the orchestration (Task 15 seed,
        Task 16 full surface). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.resolve_notebook_conflicts(notebook_id)

    def review_pending_merges(
        self,
        notebook_id: str,
        limit: int = 50,
        confirm_threshold: Optional[float] = None,
        separate_threshold: Optional[float] = None,
    ) -> dict:
        """LLM merge-candidate adjudication batch — KnowledgeGovernanceService
        owns the orchestration (Task 16; asymmetric thresholds, deferred 终态,
        fail-open adjudication and the dirty-then-invalidate order are
        unchanged). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.review_pending_merges(
            notebook_id, limit, confirm_threshold, separate_threshold
        )

    def merge_review_job_status(self, notebook_id: str) -> dict:
        return self._runtime.knowledge_governance.merge_review_job_status(notebook_id)

    def run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict:
        """Drain the pending merge queue in batches — KnowledgeGovernanceService
        owns the job loop (Task 16; single-flight, stall abort and per-batch
        fail-open are unchanged). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.run_merge_review_job(
            notebook_id, batch=batch
        )

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        return self._runtime.knowledge_governance.decided_pairs(notebook_id)

    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        return self._runtime.knowledge_governance.decided_seed_pairs(notebook_id)

    def concept_whitelist_terms(self) -> set:
        return self._runtime.knowledge_governance.concept_whitelist_terms()

    def concept_whitelist_list(self) -> List[dict]:
        return self._runtime.knowledge_governance.concept_whitelist_list()

    def concept_whitelist_add(self, term: str, note: str = "") -> dict:
        return self._runtime.knowledge_governance.concept_whitelist_add(term, note)

    def concept_whitelist_remove(self, term: str) -> None:
        return self._runtime.knowledge_governance.concept_whitelist_remove(term)

    def _invalidate_unified_cache(self, notebook_id: str) -> None:
        # Task 14: unified/vector cache eviction is coordinator-owned (the
        # coordinator holds THIS facade's cache objects by identity). Every
        # mutation call site keeps funnelling through this wrapper.
        self._runtime.kg_mutations.invalidate_unified_cache(notebook_id)

    def _cluster_input_version(self, notebook_id: str, *, exclude_emb_count: bool = False) -> str:
        """O(1) clustering-input content hash (v2, exclude_emb_count checkpoint
        namespace) — lifecycle-owned (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle._cluster_input_version(
            notebook_id, exclude_emb_count=exclude_emb_count
        )
    def _mark_unified_kg_dirty(self, notebook_id: str) -> None:
        # Task 14: the kg_mutation_seq dirty bump is coordinator-owned. This
        # wrapper stays the single funnel every KG write goes through (and the
        # frozen per-instance patch seat); the coordinator stays the single
        # place kg_mutation_seq advances — its write transaction rides the
        # facade `_write` seat, so begin/commit phase traces are unchanged.
        self._runtime.kg_mutations.mark_unified_kg_dirty(notebook_id)

    def _mark_unified_kg_dirty_in_tx(self, db, notebook_id: str) -> None:
        # In-transaction twin of `_mark_unified_kg_dirty`: rides a write
        # transaction the CALLER already holds open (relink_notebook_kg's
        # per-source commit) instead of opening its own — the dirty bump
        # commits atomically with the edges it accompanies, so a kill -9
        # between that commit and the run's finally cannot leave committed
        # edges invisible to kg_mutation_seq.
        self._runtime.kg_mutations.mark_unified_kg_dirty_in_tx(db, notebook_id)

    def _bump_cluster_mutation_seq(self, db, notebook_id: str) -> None:
        # Task 14: coordinator-owned in-transaction primitive (写簇+bump 同
        # commit,原子;kg_mutation_seq 不在此处动 — rebuild 刻意保持它稳定)。
        self._runtime.kg_mutations.bump_cluster_mutation_seq(db, notebook_id)

    def unified_kg_status(self, notebook_id: str) -> dict:
        """Unified-KG state summary — KnowledgeLifecycleService owns the read
        (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.unified_kg_status(notebook_id)
    def _edge_support_map(self, notebook_id: str) -> Dict[tuple, tuple]:
        """{(canonical_src, edge_type, canonical_tgt): (support_count, source_count)}。
        GraphRetrievalService owns the loader + version-cache body (Task 21);
        frozen-signature delegate（同 cluster_map：消除共享缓存键 "{nb}:edge_support"
        的双所有权，loader 只此一家）。"""
        return self.retrieval.graph._edge_support_map(notebook_id)

    def _annotate_edge_support(self, notebook_id: str, edges: List[dict]) -> List[dict]:
        """给 unified/neighbors 形状的边({source_object_id,target_object_id,edge_type})
        附 support_count/source_count。查表键先过 cluster_map 折叠:unified 图只折叠
        concept 端点,claim/formula/procedure 保原始 id,而 canonical_relations 按全
        类型折叠;concept 端点已是 canonical id、不在 cluster_map 键中,get(s,s) 恒等
        通过。未命中不加字段(表空/滞后 → 前端优雅缺省)。"""
        return self._runtime.knowledge_query.annotate_edge_support(notebook_id, edges)

    def unified_graph(self, notebook_id: str, level: str = "concept",
                      limit: Optional[int] = None) -> dict:
        """KG-view graph read (large-notebook guard + bounded fast-path) —
        KnowledgeLifecycleService owns the orchestration (Task 15)."""
        return self._runtime.knowledge_lifecycle.unified_graph(notebook_id, level, limit)

    def _unified_graph_full(self, notebook_id: str, level: str = "concept") -> dict:
        """Full derived unified graph (unified_cache-backed) — lifecycle-owned
        (Task 15; the canonical patch seat is repo._runtime.knowledge_lifecycle)."""
        return self._runtime.knowledge_lifecycle._unified_graph_full(notebook_id, level)

    def _viz_dict(self, idx):
        """viz arrays → viz_neighbors dict shape — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle._viz_dict(idx)

    def _viz_node(self, idx, fid, name_by_id, type_by_id):
        """One folded viz node in unified_graph shape — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle._viz_node(idx, fid, name_by_id, type_by_id)

    def _unified_graph_bounded(self, notebook_id: str, idx, limit: int) -> dict:
        """Degree-top-N core from the persisted folded viz graph —
        lifecycle-owned (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle._unified_graph_bounded(
            notebook_id, idx, limit
        )
    def kg_neighbors(
        self,
        notebook_id: str,
        object_id: str,
        cap: int = 50,
        *,
        source_notebook_id: str = "",
    ) -> dict:
        """1-hop folded neighborhood — KnowledgeLifecycleService owns the read
        orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.kg_neighbors(
            notebook_id,
            object_id,
            cap,
            source_notebook_id=source_notebook_id,
        )

    def _kg_neighbors_db(self, notebook_id: str, object_id: str, cap: int) -> dict:
        """DB fallback for kg_neighbors — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle._kg_neighbors_db(
            notebook_id, object_id, cap
        )

    def _object_meta(self, notebook_id: str, folded_ids, cmap):
        """{folded_id: (object_type, name)} hydration — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle._object_meta(
            notebook_id, folded_ids, cmap
        )
    def _stream_seed_reps(self, notebook_id: str, object_type: str, seed_fn,
                          run_id: str = "", compute_reps: bool = True):
        """Streamed seed aggregation (ORDER BY rowid anchored) — lifecycle-owned
        (Task 15; the canonical patch seat is repo._runtime.knowledge_lifecycle)."""
        return self._runtime.knowledge_lifecycle._stream_seed_reps(
            notebook_id, object_type, seed_fn, run_id=run_id, compute_reps=compute_reps
        )
    def _write_cluster_map_streamed(self, notebook_id: str, object_type: str,
                                    seed_to_canonical: Dict[str, str],
                                    canonical_names: Dict[str, str],
                                    desc_by_cid: Optional[Dict[str, str]] = None,
                                    desc_sig_by_cid: Optional[Dict[str, str]] = None,
                                    run_id: str = "") -> None:
        """Streamed concept_clusters writer — lifecycle-owned (Task 15; the
        canonical patch seat is repo._runtime.knowledge_lifecycle)."""
        return self._runtime.knowledge_lifecycle._write_cluster_map_streamed(
            notebook_id, object_type, seed_to_canonical, canonical_names,
            desc_by_cid, desc_sig_by_cid, run_id
        )
    def rebuild_unified_kg(self, notebook_id: str,
                           progress: Optional[Callable[[str, int, int], None]] = None,
                           force: bool = False, fresh: bool = False) -> int:
        """Streamed unified rebuild — KnowledgeLifecycleService owns the
        orchestration (Task 15). The cluster_input_version skip gate,
        force=force or fresh boundary self-defense, checkpoint GC after the
        gate and the merge-review/concept-desc/node-vector resume semantics
        are preserved verbatim in the service."""
        return self._runtime.knowledge_lifecycle.rebuild_unified_kg(
            notebook_id, progress, force, fresh
        )
    def start_unified_kg_rebuild(self, notebook_id: str) -> dict:
        """Claim the shared KG-maintenance slot for a rebuild (409 source)."""
        return self._runtime.knowledge_lifecycle.start_unified_kg_rebuild(notebook_id)

    def unified_kg_rebuild_status(self, notebook_id: str) -> dict:
        """Latest unified-rebuild state for this notebook."""
        return self._runtime.knowledge_lifecycle.unified_kg_rebuild_status(notebook_id)

    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int:
        """Background unified-rebuild entry point."""
        return self._runtime.knowledge_lifecycle.run_unified_kg_rebuild_job(
            notebook_id, job_id
        )

    def fail_unified_kg_rebuild_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        """Release the rebuild claim when the worker never started."""
        return self._runtime.knowledge_lifecycle.fail_unified_kg_rebuild_submission(
            notebook_id, job_id
        )

    def rebuild_canonical_relations(self, notebook_id: str, force: bool = False) -> int:
        """canonical_relations 全量重写(seq 闸) — KnowledgeLifecycleService owns
        the orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.rebuild_canonical_relations(
            notebook_id, force
        )

    def rebuild_mention_bridge(self, notebook_id: str, force: bool = False) -> int:
        """mention_edges/concept_comentions 全量重写(seq 闸) —
        KnowledgeLifecycleService owns the orchestration (Task 15)."""
        return self._runtime.knowledge_lifecycle.rebuild_mention_bridge(
            notebook_id, force
        )

    def rebuild_communities(self, notebook_id: str, level: int = 0, force: bool = False) -> int:
        """canonical 图 Louvain 社区检测(版本闸) — KnowledgeLifecycleService owns
        the orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.rebuild_communities(
            notebook_id, level, force
        )
    def list_communities(self, notebook_id: str, level: int = 0) -> List[List[str]]:
        """Member-id lists of each detected community — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle.list_communities(notebook_id, level)

    def summarize_communities(self, notebook_id: str, level: int = 0) -> int:
        """Per-community LLM reports — KnowledgeLifecycleService owns the
        orchestration (Task 15; unconfigured no-op + per-community fail-open)."""
        return self._runtime.knowledge_lifecycle.summarize_communities(notebook_id, level)

    def get_community_reports(self, notebook_id: str, level: int = 0) -> List[dict]:
        """Persisted community reports — lifecycle-owned (Task 15)."""
        return self._runtime.knowledge_lifecycle.get_community_reports(notebook_id, level)
    def concept_detail(
        self,
        notebook_id: str,
        canonical_id: str,
        *,
        source_notebook_id: str = "",
    ) -> dict:
        return self._runtime.knowledge_query.concept_detail(
            notebook_id,
            canonical_id,
            source_notebook_id=source_notebook_id,
        )

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        return self._runtime.knowledge._element_texts(db, element_ids, with_ordinal=with_ordinal)

    def _enrich_evidence(self, db, evidence):
        return self._runtime.knowledge._enrich_evidence(db, evidence)

    def node_context(
        self,
        notebook_id,
        object_id,
        *,
        source_notebook_id: str = "",
    ):
        return self._runtime.knowledge_query.node_context(
            notebook_id,
            object_id,
            source_notebook_id=source_notebook_id,
        )

    # test-only helper; later tasks may replace it with a public insert path
    def _test_insert_object(self, notebook_id: str, object_type: str, payload: dict, source_id: str = "") -> str:
        return self._runtime.knowledge_query.insert_test_object(
            notebook_id, object_type, payload, source_id
        )

    # --- Governance: promotion state machine (Track F) -------------------
    # KnowledgeGovernanceService owns the domain (Task 16); the facade keeps
    # frozen-signature delegates.

    @staticmethod
    def _promotion_row_to_dict(row: object, *, payload=None, evidence=None) -> dict:
        """Map a promotion_candidates row to the PromotionCandidate-shaped dict —
        canonical body lives with the governance service (Task 16)."""
        return KnowledgeGovernanceService.promotion_dict(
            row, payload=payload, evidence=evidence
        )

    def propose_promotion(
        self, notebook_id: str, object_id: str, *, target_base_id: str = ""
    ) -> dict:
        """Propose a personal-KG object for promotion into the base corpus —
        KnowledgeGovernanceService owns the orchestration (Task 16; idempotent
        active-proposal reuse and the base-notebook guard are unchanged).
        Task 7 (multi-domain base libraries) adds target_base_id: which
        mounted public reference library to promote into (required only when
        more than one is mounted). Frozen for the original two positional
        params; target_base_id is additive and backward compatible."""
        return self._runtime.knowledge_governance.propose_promotion(
            notebook_id, object_id, target_base_id=target_base_id
        )

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks —
        KnowledgeGovernanceService owns the batched read (Task 16; the single
        `id IN (...)` knowledge_objects lookup is unchanged). Frozen-signature
        delegate."""
        return self._runtime.knowledge_governance.list_promotion_queue(status_filter)

    def approve_promotion(self, candidate_id: str) -> dict:
        """Approve a promotion into the base corpus —
        KnowledgeGovernanceService owns the orchestration (Task 16; the frozen
        commit→embed→invalidate→dirty order and idempotent early return are
        unchanged). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.approve_promotion(candidate_id)

    def approve_promotion_as_reviewer(
        self, candidate_id: str, reviewer_id: str
    ) -> dict:
        """Authenticated adapter; preserves the frozen legacy facade signature."""
        return self._runtime.knowledge_governance.approve_promotion(
            candidate_id, reviewer_id=reviewer_id
        )

    def _seed_fn_for(self, object_type: str):
        """Return the kg_merge seed function for a KG object type."""
        return self._runtime.governance.seed_for(object_type)

    def _find_base_dedup_match(
        self, object_type: str, src_payload: dict, base_objs: List[object]
    ) -> str:
        """Exact-seed dedup (v1) — canonical body lives with the governance
        store's promotion primitive (Task 13)."""
        return self._runtime.governance.find_base_match(
            object_type, src_payload, base_objs
        )

    def _merge_evidence_lists(self, base_ev: list, src_ev: list) -> list:
        """Union two evidence lists, deduping on (source_id, element_id, quoted_span)."""
        return self._runtime.governance.merge_evidence(base_ev, src_ev)

    def reject_promotion(self, candidate_id: str, reason: str = "") -> dict:
        """Reject a promotion candidate — KnowledgeGovernanceService owns the
        orchestration (Task 16). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.reject_promotion(
            candidate_id, reason
        )

    def reject_promotion_as_reviewer(
        self, candidate_id: str, reason: str, reviewer_id: str
    ) -> dict:
        """Authenticated adapter; preserves the frozen legacy facade signature."""
        return self._runtime.knowledge_governance.reject_promotion(
            candidate_id, reason, reviewer_id=reviewer_id
        )

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard:
        """Update a knowledge object — KnowledgeGovernanceService owns the
        orchestration (Task 16; the frozen commit→best-effort-embed→invalidate
        →dirty order is unchanged). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.update_knowledge(
            notebook_id, knowledge_id, payload
        )

    @staticmethod
    def _knowledge_headline(object_type: str, payload: dict) -> str:
        """Headline extraction for a KG payload — canonical body lives with the
        governance service (Task 16); shared with list_knowledge's record
        projection."""
        return KnowledgeGovernanceService.headline(object_type, payload)

    def _knowledge_ref(self, obj: dict, object_type: str) -> KnowledgeRef:
        """KnowledgeRef projection — KnowledgeGovernanceService owns the body
        (Task 16); frozen-signature delegate."""
        return self._runtime.knowledge_governance._knowledge_ref(obj, object_type)

    @staticmethod
    def _payload_join(payload: dict) -> str:
        """Flatten payload text — canonical body lives with the governance
        service (Task 16)."""
        return KnowledgeGovernanceService.joined_payload(payload)

    def _knowledge_similarity(self, a: dict, b: dict, element_vectors: dict) -> float:
        """Keyword ∨ evidence-vector similarity — KnowledgeGovernanceService
        owns the body (Task 16); frozen-signature delegate."""
        return self._runtime.knowledge_governance._knowledge_similarity(
            a, b, element_vectors
        )

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]:
        """Near-duplicate detection by normalized-seed blocking —
        KnowledgeGovernanceService owns the orchestration (Task 16; the
        O(N + Σ block²) seed blocking and ≥0.6 grouping are unchanged).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.find_duplicates(
            notebook_id, object_type
        )

    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard:
        """Merge one knowledge object into another —
        KnowledgeGovernanceService owns the orchestration (Task 16; the frozen
        commit→dirty→invalidate mirror-image order is unchanged).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.merge_knowledge(
            notebook_id, source_id, payload
        )

    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse:
        return self._runtime.catalog.search_notebook(notebook_id, query)

    def _note_model_error(
        self,
        stage: str,
        model: str,
        exc: Exception,
        service: str = "",
        *,
        provider_failure: bool = False,
        failed_fingerprint: str = "",
    ) -> None:
        return self._runtime.models.note_model_error(
            stage,
            model,
            exc,
            service=service,
            provider_failure=provider_failure,
            failed_fingerprint=failed_fingerprint,
        )

    def _embed_query(self, query: str) -> Optional[List[float]]:
        return self.retrieval.candidates._embed_query(query)

    def _runtime_dim(self) -> int:
        """本 repo 生效的运行时截断维(读 self.settings,非全局 get_settings)。
        build_matrix 内部裸调 resolve_runtime_dim() 读全局单例,生产两者同一对象,
        但测试/多实例下 self.settings 才是真相 —— 建 ANN/矩阵的 build_matrix 调用
        应显式传本值,使工件维度由本 repo 配置决定(见 T5 manifest 真相化)。"""
        return self.retrieval.runtime_dim()

    def _gather_elements(self, db: object, notebook_id: str,
                         with_vectors: bool = True) -> List[dict]:
        # 运行时截断旁路(计划 §1.2 旁路 1):element 兜底不走 build_matrix,逐向量
        # 进 retrieval.cosine 与 _embed_query(已截断,T2)比较 —— cosine 对 len 不等
        # 静默返 0.0,漏截 = 全部 element 静默零相似度。
        return self.retrieval.candidates._gather_elements(db, notebook_id, with_vectors)

    @staticmethod
    def _element_vectors(elements: List[dict]) -> dict:
        return CandidateRetrievalService._element_vectors(elements)

    def _gather_chunks(self, db: object, notebook_id: str) -> List[dict]:
        return self.retrieval.candidates._gather_chunks(db, notebook_id)

    def _vector_matrix_version(self, db: object, notebook_id: str, table: str):
        return self.retrieval.candidates._vector_matrix_version(db, notebook_id, table)

    def _vector_matrix(self, db: object, notebook_id: str,
                       table: str, id_col: str):
        return self.retrieval.candidates._vector_matrix(db, notebook_id, table, id_col)

    def _vector_matrix_warm(self, db: object, notebook_id: str, table: str) -> bool:
        return self.retrieval.candidates._vector_matrix_warm(db, notebook_id, table)

    def _keyword_token_sets(self, db, notebook_id: str, objects: list,
                            bounded: bool = False) -> dict:
        return self.retrieval.candidates._keyword_token_sets(db, notebook_id, objects, bounded)

    def _federated_rx_graph(self, active_notebook_id: str):
        return self.retrieval.graph._federated_rx_graph(active_notebook_id)

    def _federated_graph_is_large(self, active_notebook_id: str) -> bool:
        return self.retrieval.candidates._federated_graph_is_large(active_notebook_id)

    def _mention_extra_edges(self, notebook_id: str) -> List[Tuple[str, str, float]]:
        return self.retrieval.graph._mention_extra_edges(notebook_id)

    def _ppr_graph(self, notebook_id: str):
        return self.retrieval.graph._ppr_graph(notebook_id)

    # ── scale index (offline build) ──────────────────────────────────────────

    def _probe_scale_version_signal(self, notebook_id: str):
        """Cheap probe: (seq, cseq, settings_tail) for `notebook_id`. O(1) — a
        single unified_kg_state row read, no table aggregates — always run,
        never single-flighted (it's the signal used to decide whether the
        expensive cold path is needed at all, so it must never itself block on
        another thread's cold compute).

        P0-A: clusters used to be re-read here every call via a concept_clusters
        COUNT/MAX(created_at) (millions of rows at scale) because rebuild
        deliberately keeps kg_mutation_seq stable across a rebuild (idempotency)
        while still rewriting concept_clusters — seq alone couldn't see that
        rewrite. Now cluster_mutation_seq (bumped in the SAME commit as every
        concept_clusters write — write_clusters / append_clusters /
        incremental_fuse_source's orphan sweep / the rebuild streamed writer)
        carries that signal at O(1), so this probe never touches concept_clusters
        at all; the real COUNT/MAX only run in _compute_scale_version_cold on a
        memo miss."""
        # runtime_dim 是 settings_tail 的一员(T3/R4);mention_seq(P2 共提桥)
        # 折进 settings_tail —— 两者经 _compute_scale_version_cold 流入
        # _scale_index_version,进而与磁盘 manifest.version 对照。Task 18:单行
        # 探针读移入 IndexProjectionStore(经 _connect 席位,spy/单飞注入照常观测)。
        return self._runtime.index_projections.version_signal(notebook_id)

    def _compute_scale_version_cold(self, notebook_id: str, seq: int,
                                     settings_tail: tuple) -> list:
        """冷路径:五表聚合(clusters 聚合从热路径移到这里——P0-A 后热路径只读
        unified_kg_state 单行,COUNT/MAX 只在 memo miss 时算)。version list 的
        内容与格式与 P0-A 前逐位一致(clusters 仍在第 8、9 位),磁盘
        manifest.version 兼容性不受影响。只从 _scale_index_version 的 per-nb
        单飞锁内部调用,不直接对外暴露。Task 18:五表聚合读移入
        IndexProjectionStore.version_facts(经 _connect 席位);settings_tail
        仍由本方法追加 —— 与 memo 键取自同一次探针,list 格式逐位不变。"""
        return self._runtime.index_projections.version_with_settings(
            notebook_id, settings_tail
        )

    def _scale_index_version(self, notebook_id: str) -> list:
        """Compatibility delegate to the runtime-owned version memo."""
        return self._runtime.scale_artifacts.version(notebook_id)

    def _read_manifest_version(self, out_dir: str):
        """廉价读 out_dir/manifest.json 的 version 字段(几 KB,sub-ms)。
        文件缺失/损坏/无 version → None(fail-soft)。Task 18:移入
        ScaleArtifactStore(磁盘 manifest 探针 O(1) 语义不变)。"""
        return self._runtime.scale_artifact_store.read_manifest_version(out_dir)

    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        """Return a valid ScaleIndex or None.  Task 18:exact/allow_stale 语义
        (磁盘身份缓存复用 + per-nb 单飞 cold-load)整体移入
        ScaleArtifactCatalog.load —— catalog 只读、不含 builder,读取永不触发
        重建(base 离线 ANN / active 暴力的成本分离不变量)。"""
        return self._runtime.scale_artifacts.load(
            notebook_id, allow_stale=allow_stale
        )

    def _open_scale_ann(self, idx, kind: str):
        """惰性 open + memoize hnswlib handle 到 ScaleIndex 实例。失败/无工件→None。
        Task 18:移入 ScaleArtifactCatalog.open_ann(manifest dim 探针 + fail-open
        回退语义不变)。"""
        return self._runtime.scale_artifacts.open_ann(idx, kind)

    def _spawn_viz_build(self, notebook_id: str) -> None:
        """Compatibility delegate to the runtime-owned viz scheduler."""
        return self._runtime.scale_artifacts._spawn_viz_build(notebook_id)

    def _viz_index(self, notebook_id: str):
        """Compatibility delegate to runtime viz artifact selection."""
        return self._runtime.scale_artifacts.viz_index(notebook_id)

    def _viz_index_probe(self, notebook_id: str) -> dict:
        """Compatibility delegate to the read-only runtime viz probe."""
        return self._runtime.scale_artifacts.viz_probe(notebook_id)

    def _gather_kg_graph(self, notebook_id: str, source_ids=None, synonym_edges=None,
                          as_arrays: bool = False):
        """Compatibility delegate to the runtime-owned scale builder."""
        return self._runtime.scale_builder.gather_graph(
            notebook_id,
            source_ids=source_ids,
            synonym_edges=synonym_edges,
            as_arrays=as_arrays,
        )

    def build_scale_index(
        self,
        notebook_id: str,
        on_stage: Optional[Callable[[str, int], None]] = None,
    ) -> dict:
        """Compatibility delegate to the runtime-owned full builder."""
        return self._runtime.scale_artifacts.build(
            notebook_id, on_stage=on_stage
        )

    def fold_scale_index_delta(
        self, notebook_id: str, _assume_locked: bool = False
    ) -> dict:
        """Compatibility delegate to the runtime-owned delta folder."""
        return self._runtime.scale_artifacts.fold(
            notebook_id, assume_locked=_assume_locked
        )

    def _index_delta(self, notebook_id: str) -> dict:
        """Compatibility delegate to the runtime-owned scale builder."""
        return self._runtime.scale_builder._index_delta(notebook_id)

    def _scale_index_eligible(self, notebook_id: str, *, tier: "str | None" = None,
                              exists: "bool | None" = None,
                              total_chunks: "int | None" = None) -> bool:
        """Compatibility delegate to runtime scale eligibility."""
        return self._runtime.scale_artifacts.eligible(
            notebook_id, tier=tier, exists=exists, total_chunks=total_chunks
        )

    def scale_index_status(self, notebook_id: str) -> dict:
        """Compatibility delegate to runtime scale status."""
        return self._runtime.scale_artifacts.status(notebook_id)

    def index_status(self, notebook_id: str) -> dict:
        """Compatibility delegate to the runtime's read-only combined status."""
        return self._runtime.scale_artifacts.index_status(notebook_id)

    def _resolve_scale_mode(self, notebook_id: str, mode: str) -> str:
        """Compatibility delegate to runtime fold/full selection."""
        return self._runtime.scale_artifacts._resolve_mode(notebook_id, mode)

    def _resolve_index_owner(self, notebook_id: str) -> "str | None":
        """Compatibility delegate to runtime notification ownership."""
        return self._runtime.scale_artifacts._resolve_index_owner(notebook_id)

    def _notebook_name(self, notebook_id: str) -> str:
        """Compatibility delegate to runtime notification naming."""
        return self._runtime.scale_artifacts._notebook_name(notebook_id)

    def _notify_index_done(self, notebook_id: str) -> None:
        """Compatibility delegate to runtime index-completion notification."""
        return self._runtime.scale_artifacts.notify_index_done(notebook_id)

    def _run_scale_op(self, notebook_id: str, mode: str) -> None:
        """Compatibility delegate to the runtime daemon launcher."""
        return self._runtime.scale_artifacts._run_scale_op(notebook_id, mode)

    def _process_idle_queue(self, force: bool = False) -> None:
        """Compatibility delegate to runtime idle-queue draining."""
        return self._runtime.scale_artifacts._process_idle_queue(force=force)

    def _ensure_scale_scheduler(self) -> None:
        """Compatibility delegate to the runtime scheduler."""
        return self._runtime.scale_artifacts._ensure_scheduler()

    def trigger_scale_index_rebuild(self, notebook_id: str, when: str = "now",
                                    mode: str = "auto") -> dict:
        """Compatibility delegate to runtime rebuild scheduling."""
        return self._runtime.scale_artifacts.trigger(
            notebook_id, when=when, mode=mode
        )

    def _dequeue_scale_idle(self, notebook_id: str) -> bool:
        """Compatibility helper for the runtime-owned idle queue."""
        return self._runtime.scale_artifacts.dequeue_idle(notebook_id)

    def cancel_scale_index(self, notebook_id: str) -> dict:
        """Compatibility delegate to runtime cancellation."""
        return self._runtime.scale_artifacts.cancel(notebook_id)

    def _maybe_enqueue_scale_fold(self, notebook_id: str) -> None:
        """Compatibility delegate to runtime auto-fold policy."""
        return self._runtime.scale_artifacts.maybe_enqueue_fold(notebook_id)

    def maybe_auto_index(self, notebook_id: str) -> None:
        """Compatibility delegate to runtime automatic indexing."""
        return self._runtime.scale_artifacts.maybe_auto_index(notebook_id)

    def _build_viz_graph_arrays(self, notebook_id: str):
        """Compatibility helper over the runtime builder's pure viz math."""
        return self._runtime.scale_artifacts.build_viz_graph_arrays(notebook_id)

    def _viz_arrays_from_graph(self, full: dict):
        """Compatibility delegate for callers that already derived a graph."""
        return self._runtime.scale_artifacts.viz_arrays_from_graph(full)

    def _derive_object_graph_lite(self, notebook_id: str) -> dict:
        """Compatibility delegate to the builder's lite DB projection."""
        return self._runtime.scale_builder._derive_object_graph_lite(notebook_id)

    def _viz_index_dir(self, notebook_id: str) -> str:
        return str(self._runtime.scale_artifact_store.viz_dir(notebook_id))

    def build_viz_index(self, notebook_id: str) -> Optional[dict]:
        """Compatibility delegate to the runtime-owned viz builder."""
        return self._runtime.scale_artifacts.build_viz(notebook_id)

    def _active_kg_delta(self, notebook_id: str):
        return self.retrieval.graph._active_kg_delta(notebook_id)

    def _delta_vector_matrix(self, db: object, notebook_id: str,
                             table: str, id_col: str, node_ids: List[str]):
        return self.retrieval.graph._delta_vector_matrix(db, notebook_id, table, id_col, node_ids)

    def _scale_xlayer_bridge_edges(self, notebook_id: str, base_indexes,
                                   active_edges, active_node_ids=None):
        return self.retrieval.graph._scale_xlayer_bridge_edges(notebook_id, base_indexes, active_edges, active_node_ids)

    def _scale_combined_graph(self, notebook_id: str, base_indexes):
        return self.retrieval.graph._scale_combined_graph(notebook_id, base_indexes)

    def scale_ppr(self, notebook_id: str, question: str) -> List[Tuple[str, float]]:
        return self.retrieval.graph.scale_ppr(notebook_id, question)

    def _ppr_retrieve(self, notebook_id: str, question: str) -> List["RetrievedChunk"]:
        return self.retrieval.graph.ppr_retrieve(notebook_id, question)

    def _ppr_reset_vector(self, notebook_id: str, question: str,
                          key_to_idx: Dict[str, int]) -> Dict[int, float]:
        return self.retrieval.graph._ppr_reset_vector(notebook_id, question, key_to_idx)

    _PPR_RERANK_SCHEMA = '{"relevant_ids": ["..."]}'

    def _ppr_fact_rerank(self, question: str, kg_hits: list) -> list:
        return self.retrieval.graph._ppr_fact_rerank(question, kg_hits)

    def _rule_card(self, item: RetrievedKnowledge) -> RuleCard:
        return self._runtime.knowledge_query.rule_card(item)

    @staticmethod
    def _as_retrieved(obj: dict, object_type: str) -> RetrievedKnowledge:
        return KnowledgeQueryService.as_retrieved(obj, object_type)

    def _tier_map_for(self, notebook_ids: Iterable[str]) -> Dict[str, str]:
        """Batch-resolve {notebook_id: tier} in one query (mirrors
        federated_retrieve's tier_map pass). Used by citation-building sites
        that fan across several notebooks (e.g. PPR chunks that can span
        base ⊕ active) so tier lookup is O(distinct notebooks), not O(citations).
        Missing/unknown ids default to "personal" (safe: matches Citation.tier's
        own default, never silently mislabels a source as authoritative base)."""
        return self._runtime.evidence_context_component.tier_map(notebook_ids)

    def _citations_from(
        self,
        items: List[RetrievedKnowledge],
        valid_element_ids: set,
        label: str,
        *,
        notebook_id: str,
    ) -> List[Citation]:
        return self._runtime.evidence_context_component.citations_from(
            items, valid_element_ids, label, notebook_id=notebook_id
        )

    # SQLite default SQLITE_MAX_VARIABLE_NUMBER-safe chunk size for `IN (...)`
    # placeholder lists (mirrors the ~900 convention used elsewhere in this file).
    _IN_CHUNK = 900

    def _in_batches(self, ids):
        """把 id 列表切成 ≤_IN_CHUNK 的批(去重保序)。所有把 id 列表内联成
        SQL IN 占位符的 delta 位点必须经它——SQLite 3.32+ 变量上限 32,766,
        生产 48,739 delta source 已真实打爆(too many SQL variables)。"""
        return self.retrieval.in_batches(ids, self._IN_CHUNK)

    def _relations_with_names(self, db: object, notebook_id: str,
                              relation_ids: Optional[List[str]] = None) -> List[dict]:
        return self.retrieval.candidates._relations_with_names(db, notebook_id, relation_ids)

    def _relation_ann_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        return self.retrieval.candidates._relation_ann_candidates(notebook_id, query_vector, idx, recall)

    def _retrieve_relations_scored(self, notebook_id: str, query: str) -> List["RetrievedRelation"]:
        return self.retrieval.candidates._retrieve_relations_scored(notebook_id, query)

    def _kg_object_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        return self.retrieval.candidates._kg_object_candidates(notebook_id, query_vector, idx, recall)

    @property
    def retrieval(self):
        """Explicit candidate + graph retrieval port with no facade backreference."""
        return self._runtime.retrieval_component

    @property
    def collection_catalog(self):
        """Typed-collection counts (the enumeration tools' map layer).

        One hop to the runtime-owned ``CollectionCatalogService`` — it holds
        the bounded per-source / per-notebook count caches, so every consumer
        must observe THIS instance rather than construct its own.
        """
        return self._runtime.collection_catalog

    @property
    def collection_enumeration(self):
        """Typed-collection listing (the enumeration tools' list layer).

        One hop to the runtime-owned ``CollectionEnumerationService``, which
        is wired to the same catalog instance above — a second wiring would
        let the map and the list count different things.
        """
        return self._runtime.collection_enumeration

    def _retrieve_scored(self, notebook_id: str, query: str,
                         types: Optional[Iterable[str]] = None,
                         w_keyword: float = W_KEYWORD,
                         w_semantic: float = W_SEMANTIC, *,
                         allowed_source_ids=None) -> List[RetrievedKnowledge]:
        if allowed_source_ids is None:
            return self.retrieval.candidates.retrieve_scored(
                notebook_id, query, types, w_keyword, w_semantic
            )
        return self.retrieval.candidates.retrieve_scored(
            notebook_id, query, types, w_keyword, w_semantic,
            allowed_source_ids=allowed_source_ids,
        )

    def federated_retrieve(
        self,
        active_notebook_id: str,
        query: str,
        types: Optional[Iterable[str]] = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
        *,
        allowed_source_keys=None,
    ) -> List[RetrievedKnowledge]:
        if allowed_source_keys is None:
            return self.retrieval.candidates.federated_retrieve(
                active_notebook_id, query, types, w_keyword, w_semantic
            )
        return self.retrieval.candidates.federated_retrieve(
            active_notebook_id, query, types, w_keyword, w_semantic,
            allowed_source_keys=allowed_source_keys,
        )

    def federated_retrieve_relations(self, active_notebook_id: str,
                                     query: str) -> List["RetrievedRelation"]:
        return self.retrieval.candidates.federated_retrieve_relations(active_notebook_id, query)

    def _retrieve_neighbors(self, notebook_id: str, object_id: str,
                            edge_type: Optional[str] = None,
                            direction: str = "both") -> NeighborExpansion:
        return self.retrieval.candidates.retrieve_neighbors(notebook_id, object_id, edge_type, direction)

    def _follow_chain(
        self,
        active_notebook_id: str,
        start_object_id: str,
        edge_type: Optional[str] = None,
        target_object_id: str = "",
        direction: str = "out",
        max_fan_out: int = 8,
        max_results: int = 4,
    ):
        return self.retrieval.graph.follow_chain(active_notebook_id, start_object_id, edge_type, target_object_id, direction, max_fan_out, max_results)

    def _retrieve_elements(self, notebook_id: str, query: str,
                           limit: int = 8, *,
                           allowed_source_ids=None) -> List[RetrievedElement]:
        if allowed_source_ids is None:
            return self.retrieval.candidates.retrieve_elements(
                notebook_id, query, limit
            )
        return self.retrieval.candidates.retrieve_elements(
            notebook_id, query, limit,
            allowed_source_ids=allowed_source_ids,
        )

    def _retrieve_chunks(self, notebook_id: str, query: str, recall: int = 0):
        return self.retrieval.candidates._retrieve_chunks(notebook_id, query, recall)

    def _retrieve_chunks_fts_degraded(self, notebook_id, query, query_vector,
                                      recall, n_chunks):
        return self.retrieval.candidates._retrieve_chunks_fts_degraded(notebook_id, query, query_vector, recall, n_chunks)

    def _retrieve_chunks_ann(
        self, notebook_id, query, query_vector, idx, recall, *,
        allowed_source_ids=None,
    ):
        if allowed_source_ids is None:
            return self.retrieval.candidates._retrieve_chunks_ann(
                notebook_id, query, query_vector, idx, recall
            )
        return self.retrieval.candidates._retrieve_chunks_ann(
            notebook_id, query, query_vector, idx, recall,
            allowed_source_ids=allowed_source_ids,
        )

    def _hydrate_chunk_candidates(self, cand_ids):
        return self.retrieval.candidates._hydrate_chunk_candidates(cand_ids)

    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        return self.retrieval.candidates._retrieve_chunks_multi(notebook_id, sub_queries)

    def _keyword_chunk_candidates(self, notebook_id: str, keywords: str,
                                  recall: int = 0):
        return self.retrieval.candidates._keyword_chunk_candidates(notebook_id, keywords, recall)

    @staticmethod
    def _union_chunk_candidates(base: list, extra: list) -> list:
        return RetrievalService.merge_chunk_candidates(base, extra)

    def _mmr_select_chunks(self, scored, ids, mat, k: int, lambda_: float):
        return self.retrieval.candidates._mmr_select_chunks(scored, ids, mat, k, lambda_)

    def _chunk_answer_context(self, chunks, budget_chars: "int | None" = None,
                               notebook_id: str = "") -> tuple:
        """产出长上下文综合用的 id 标注块 + id_map。chunk.text 已含 [section] 前缀
        (P1 build_chunks),故每行直接 `k_i: <text>`。id_map 形状与 KG 版一致,
        使 _parse_answer_anchors 原样复用(object_id=chunk_id, object_type=chunk)。
        tier:与 _answer_context(KG 版)同一模式——按每 chunk 自己的 notebook_id 解析
        (PPR/概念漫游可掺 base 库 chunk,c.notebook_id 已标;单库路径留空回退调用方
        传入的 notebook_id)。批量一次查询(O(distinct notebook) 非 O(chunk)),
        循环内只查表。"""
        return self._runtime.evidence_context_component.chunk_context(
            chunks, notebook_id=notebook_id, budget_chars=budget_chars
        )

    def _answer_chunks(
        self,
        question,
        chunks,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
    ) -> tuple:
        """长上下文综合 — 冻结签名 delegate(engine body 在 AskService,Task 24)。"""
        return self._runtime.ask_component._answer_chunks(
            question, chunks, history, cancel_event=cancel_event,
            notebook_id=notebook_id)

    def _answer_mix(
        self,
        question,
        chunks,
        kg_block,
        kg_id_map,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
    ) -> tuple:
        """mix 长上下文综合 — 冻结签名 delegate(engine body 在 AskService,Task 24)。"""
        return self._runtime.ask_component._answer_mix(
            question, chunks, kg_block, kg_id_map, history,
            cancel_event=cancel_event, notebook_id=notebook_id)

    def _build_chunk_retrieval_plan(self, notebook_id: str, sub_queries: list) -> ChunkRetrievalPlan:
        return self.retrieval.candidates._build_chunk_retrieval_plan(notebook_id, sub_queries)

    def ask_chunk(
        self,
        notebook_id: str,
        payload: AskRequest,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """chunk-native 通用问答 — 冻结签名 delegate:engine body 在
        AskService.ask_chunk (Task 24),身份适配 current_user().id。"""
        return self._runtime.ask_component.ask_chunk_current(
            notebook_id, payload, cancel_event)

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Dispatch to the retrieval handler named by payload.mode, resolved
        through the ask_modes registry. Unknown modes raise UnknownAskMode (the
        API layer returns 422) — never a silent fall-through to the legacy
        path. The blocking surface uses the same internal durable-job and
        atomic-final-save lifecycle as the streaming coordinator; its job id
        is not added to the AskResponse protocol."""
        return self._runtime.ask_component.ask_current(notebook_id, payload)

    def preview_reasoning_intent(
        self, notebook_id: str, question: str, history: str = "",
        cancel_event: CancelEvent = None
    ) -> QueryIntentContract:
        """Corpus-blind preflight used before a reasoning Ask creates a job."""
        return self._runtime.ask_component.preview_reasoning_intent(
            notebook_id, question, history, cancel_event
        )

    def validate_reasoning_submission(
        self, notebook_id: str, payload: AskRequest
    ) -> None:
        """Preflight a selected scope before API code creates a durable Ask."""
        return self._runtime.ask_component.validate_reasoning_submission(
            notebook_id, payload
        )

    # ask_fast (legacy KG-native, P4-5退役) 和 _ask_global (GraphRAG map-reduce, P4-5退役)
    # 已删除。旧会话/书签中的 mode="fast"/"global" 通过 ask_modes._RETIRED_MODES 映射到
    # "chunk"，不会触发 422。
    # 随之删除的还有：is_process_query import（原 ask_fast 独占调用）。
    # 保留的 helper（_rrf_scored 等）仍被其他 ask_* 路径或测试直接调用，尚未可删。
    # 旧 LLM 打分重排 helper 已删——被 qwen3-rerank(RerankClient)取代。
    # _answer_kg 已删(P4-5死码)，query-refine 移入 _refine_context。

    def _concept_cluster_id(self, notebook_id: str, object_id: str) -> str:
        """Canonical unified-cluster id for a concept `object_id`, reusing the
        same `concept_clusters` membership map `concept_detail` relies on
        (`cluster_map` -> {member_object_id: canonical_id}). When clustering is
        not populated for the notebook (no cluster row for this object), fall
        back to `object_id` so dedup degrades gracefully (no merge, no crash)."""
        return self.retrieval.concept_cluster_id(notebook_id, object_id)

    def _rrf_scored(
        self,
        query: str,
        kg_objs: Dict[str, List[dict]],
        knowledge_sims: Optional[Dict[str, float]],
        element_sims: Optional[Dict[str, float]] = None,
    ) -> List[RetrievedKnowledge]:
        return self.retrieval.candidates._rrf_scored(query, kg_objs, knowledge_sims, element_sims)

    def _graph_seed_fusion(
        self,
        notebook_id: str,
        question: str,
        base_seeds: List[str],
        cancel_event: CancelEvent = None,
    ) -> List[str]:
        return self.retrieval.candidates._graph_seed_fusion(notebook_id, question, base_seeds, cancel_event)

    def _chunk_kg_overlay(self, notebook_id: str, query: str, hl: str, id_offset: int):
        return self.retrieval.candidates._chunk_kg_overlay(notebook_id, query, hl, id_offset)

    def _elem_chunk_map(self, notebook_id: str) -> Dict[str, list]:
        return self.retrieval.graph._elem_chunk_map(notebook_id)

    def _kg_source_chunks(
        self, notebook_id: str, object_ids: list, *, support_by_object=None
    ) -> list:
        return self.retrieval.graph._kg_source_chunks(
            notebook_id, object_ids, support_by_object=support_by_object
        )

    def _ent_chunk_map(self, notebook_id: str) -> Dict[str, set]:
        return self.retrieval.graph._ent_chunk_map(notebook_id)

    # ── chunk×graph mix ──────────────────────────────────────────────────────

    _MIX_KG_KEY_BASE = 1000
    _MIX_PROMPT_BUFFER_TOKENS = 2000

    def _truncate_kg_block(self, block: str, max_tokens: int) -> str:
        """按行截断 KG block 至 token 预算(整行保留)。被截掉的行其 [k] 仍留在 id_map,
        _parse_answer_anchors 解析无害(只损失上下文,不破坏引用)。镜像 truncate_by_tokens。"""
        return self._runtime.evidence_context_component.truncate_kg_block(
            block, max_tokens
        )

    def _gather_vector_chunks(self, notebook_id: str, sub_queries: list) -> list:
        return self.retrieval.candidates._gather_vector_chunks(notebook_id, sub_queries)

    def _mix_retrieve(self, notebook_id: str, query: str, hl: str, sub_queries: list) -> tuple:
        return self.retrieval.candidates._mix_retrieve(notebook_id, query, hl, sub_queries)

    def participant_notebook_ids(self, notebook_id: str) -> List[str]:
        """联邦参与库:active 在首位 + 全部 base tier(与 _ppr_graph/federated_retrieve
        的内联谓词一致;此 helper v1 只供新代码使用,存量调用点不迁移)。

        公开(v1 落地时叫 `_participant_notebook_ids`,零调用点)是因为它现在有了
        facade **外部**的消费方:MCP 的 `get_cited_element` 拿注入的 repository 去做
        「引用点查」的参与集校验——deps 里的 `notebook_store_port()` 是浏览器侧
        (FastAPI 依赖)的取数口,MCP 不经过它。唯一定义点仍是 mount_sql.py。"""
        return self._runtime.notebook_store.participant_notebook_ids(notebook_id)

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0) -> tuple:
        return self._runtime.evidence_context_component.knowledge_context(
            notebook_id, top_hits, id_offset=id_offset
        )

    def _rewrite_followup_query(
        self,
        history: str,
        question: str,
        cancel_event: CancelEvent = None,
    ) -> str:
        """Follow-up 检索改写 — 冻结签名 delegate(engine body 在 AskService,Task 24)。"""
        return self._runtime.ask_component._rewrite_followup_query(
            history, question, cancel_event)

    def _refine_context(
        self,
        question: str,
        context_block: str,
        client,
        cancel_event: CancelEvent = None,
    ) -> str:
        """问题感知证据精炼 — 冻结签名 delegate(engine body 在 AskService,Task 24)。"""
        return self._runtime.ask_component._refine_context(
            question, context_block, client, cancel_event)

    def _answer_with_retry(self, synth, model_label):
        """答案合成有界重试 — 冻结签名 delegate(engine body 在 AskService,Task 24;
        空 content 重试一次 + 诚实降级 + 补 model_error,见服务端 docstring)。"""
        return self._runtime.ask_component._answer_with_retry(synth, model_label)

    def _answer_reasoning(
        self,
        notebook_id,
        question,
        top_hits,
        elements,
        history="",
        cancel_event: CancelEvent = None,
        chunks=None,
        chains=None,
    ):
        """reasoning 答案合成 — 冻结签名 delegate(engine body 在 AskService,Task 24)。"""
        return self._runtime.ask_component._answer_reasoning(
            notebook_id, question, top_hits, elements, history,
            cancel_event=cancel_event, chunks=chunks, chains=chains)

    def _unconfigured_model_response(self, notebook_id: str, question: str,
                                     conversation_id: str, mode: str) -> AskResponse:
        """未配主 LLM 的统一短路响应 — 冻结签名 delegate(engine body 在
        AskService,Task 24),身份适配 current_user().id。"""
        return self._runtime.ask_component.unconfigured_model_response_current(
            notebook_id, question, conversation_id, mode)

    def ask_reasoning(
        self,
        notebook_id: str,
        payload: AskRequest,
        on_trace=None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Reasoning-mode ask — 冻结签名 delegate:engine body 在
        AskService.ask_reasoning (Task 24),身份适配 current_user().id。"""
        return self._runtime.ask_component.ask_reasoning_current(
            notebook_id, payload, on_trace, cancel_event)

    def ask_graph(
        self,
        notebook_id: str,
        payload: "AskRequest",
        seed_ids: Optional[List[str]] = None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Multi-hop graph reasoning mode — 冻结签名 delegate:engine body 在
        AskService.ask_graph (Task 24),身份适配 current_user().id。"""
        return self._runtime.ask_component.ask_graph_current(
            notebook_id, payload, seed_ids, cancel_event)

    def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
        """Resolve the `[k_i]` markers present in `answer` into AnswerAnchor
        objects (deduped, in first-seen order). Markers not in `id_map` and
        items never cited are dropped."""
        return self._runtime.evidence_context_component.parse_anchors(answer, id_map)

    def _needs_index(self, notebook_id: str) -> bool:
        """大库无索引提示位判定 — 冻结签名 delegate(index-required 计算随
        _save_answer 收口移入 AskService,Task 24;判定失败退化 False 不拖垮 ask)。"""
        return self._runtime.ask_component._needs_index(notebook_id)

    def _save_answer(
        self,
        notebook_id: str,
        question: str,
        response: AskResponse,
        conversation_id: Optional[str] = None,
    ) -> str:
        """所有 ask handler 的答案落存收口 — 冻结签名 delegate(index_required
        装饰 + 行持久化在 AskService._save_answer → AskStateStore,Task 24),
        身份适配 current_user().id。"""
        return self._runtime.ask_component.save_answer_current(
            notebook_id, question, response, conversation_id)

    def _ensure_conversation(
        self, db, notebook_id: str, conversation_id: Optional[str], question: str
    ) -> str:
        """Return the conversation id for this turn: append to an existing
        conversation in this notebook (touching `updated_at`), or create a new
        one (id `conv-<hex>`, title from the first question).  Rides the
        CALLER's connection; identity resolves here — the store takes the
        explicit user_id and preserves the created_by scoping verbatim."""
        return self._runtime.ask_component.ensure_conversation_current(
            db, notebook_id, conversation_id, question
        )

    def begin_ask_job(self, notebook_id: str, payload, mode: str, cancel_event) -> tuple[str, str]:
        """建/接续会话 + 插入 running 的 ask_jobs 行 + 注册 cancel_event。
        就地把解析出的 conversation_id 写回 payload,使随后的 handler(_ensure_conversation)
        接续同一会话、不另建。返回 (job_id, conversation_id)。
        持久化(单写事务原子建会话+job 行)在 AskStateStore;cancel-event 注册表在
        runtime-owned AskCancellationRegistry(Task 23——流式执行编排在
        AskExecutionCoordinator,经同一注册表);notebook 存在性守卫留在 facade。"""
        return self._runtime.ask_component.begin_job_current(
            notebook_id, payload, mode, cancel_event
        )

    def finish_ask_job(self, job_id: str, status: str, *, answer_id: str = "", error: str = "") -> None:
        """终态化 ask_job + 注销 cancel_event；cancelled/failed 时清理空会话(0 答案)。
        冻结次序:终态 job 行(store 里的一个事务)→ 注销 → 空会话清理保持为
        之后的另一个事务。"""
        return self._runtime.ask_component.finish_job(
            job_id, status, answer_id=answer_id, error=error
        )

    def cancel_ask_job(self, job_id: str, user_id: str) -> dict:
        """显式取消(属主校验)。set 在途 worker 的 cancel_event;非属主/不存在 → KeyError。"""
        return self._runtime.ask_component.cancel_job(job_id, user_id)

    @property
    def _ask_cancel_events(self) -> dict:
        """WS2a 兼容座:在途 ask 的 {job_id: cancel_event} 注册表 dict 本体
        (真身在 runtime-owned AskCancellationRegistry,Task 23)。冻结的测试
        断言(`repo._ask_cancel_events.get(job_id) is ev`)继续观察同一对象;
        显式 cancel 端点是唯一真取消入口,断连绝不 set。"""
        return self._runtime.ask_cancellations.events

    @property
    def _ask_cancel_lock(self) -> threading.Lock:
        """WS2a 兼容座:注册表守卫锁(真身随注册表在 runtime,一个所有者)。"""
        return self._runtime.ask_cancellations.lock

    def ask_job_status(self, job_id: str) -> dict:
        return self._runtime.ask_state.ask_job_status(job_id)

    def append_ask_trace(self, job_id: str, step: dict) -> None:
        """把一个 trace step 追加进 ask_trace_steps 子表(append-only,O(1) 单行
        INSERT;取号+插入同事务,细节在 AskStateStore.append_trace)。

        本 facade 协调层保有既有契约:**fail-open**——raw store 上抛的任何持久化
        失败在此记日志吞掉,轨迹持久化失败绝不拖垮 ask(error_policies.json:
        append_ask_trace)。notebook_id/user_id 是 port 签名参数,store SQL 只按
        job_id 落行;基线本就不解析用户,这里传占位保持零额外开销(Task 24 的
        引擎调用会带真实值)。"""
        return self._runtime.ask_component.append_trace_fail_open(job_id, step)

    def _read_ask_trace(self, db, job_id: str) -> list:
        """从 ask_trace_steps 子表按 seq 顺序读回一个 job 的完整轨迹(容错细节
        在 AskStateStore.read_trace;冻结签名 delegate,骑调用者连接)。"""
        return self._runtime.ask_state.read_trace(db, job_id)

    def ask_job_detail(self, job_id: str) -> dict:
        return self._runtime.ask_state.ask_job_detail(job_id)

    def ask_answer_detail(self, answer_id: str) -> "dict | None":
        """按 answer_id 直查单条答案(一条主键查询,不加载整个会话)。见
        ``AskStateStore.ask_answer_detail`` docstring。"""
        return self._runtime.ask_state.ask_answer_detail(answer_id)

    def _cleanup_empty_conversation(self, conversation_id: str) -> None:
        """删掉没有任何 answer 的会话(取消首轮留下的空壳);有答案则保留。"""
        return self._runtime.ask_state.cleanup_empty_conversation(conversation_id)

    def _conversation_history(self, db, conversation_id: str, limit: int = 5) -> str:
        """Build the prior-turns history block (oldest->newest, last `limit`
        turns) from stored answer payloads.  Frozen-signature delegate riding
        the caller's connection (the mode engines own the transaction)."""
        return self._runtime.ask_state.conversation_history(db, conversation_id, limit)

    def get_conversation(self, conversation_id: str) -> "ConversationDetail":
        """Rebuild a ConversationDetail from the conversations row + its answer
        turns. Raises KeyError if the conversation does not exist."""
        return self._runtime.ask_state.get_conversation(conversation_id)

    def list_conversations(self, notebook_id: str) -> "List[ConversationSummary]":
        """List conversations for a notebook (most-recently-updated first) with
        a per-conversation turn count. Raises KeyError if the notebook is gone."""
        return self._runtime.ask_component.list_conversations_current(notebook_id)

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        return self._runtime.ask_state.rename_conversation(conversation_id, title)

    def delete_conversation(self, conversation_id: str) -> None:
        return self._runtime.ask_state.delete_conversation(conversation_id)

    def bulk_delete_conversations(
        self, notebook_id: str, older_than_days: int
    ) -> ConversationBulkDeleteResult:
        """Delete the current user's conversations in `notebook_id` whose last
        activity (`updated_at`) is strictly older than `older_than_days` days,
        cascading to their answers and terminal durable Ask state. Returns the
        authoritative count and ids actually deleted; running conversations
        are skipped. Raises KeyError if the notebook does not exist."""
        return self._runtime.ask_component.bulk_delete_conversations_current(
            notebook_id, older_than_days
        )

    # --- 会话公开分享(T2:发放/回读/撤销 + 水位推进)---
    # 直接委托给 AskStateStore 的方法(镜像 report_store 分享方法的委托形态,见下面
    # share_report 等)。这些方法声明在 ports.py 的 AskStateRepository 协议
    # `--- Public share links ---` 段,与报告面 ReportRepository 的同名段同款
    # (codex T2 评审:早先这里的注释说反了,称"报告面也不进协议",实则报告分享方法
    # 确在协议里——现已补齐会话侧声明并改正此注释)。
    def conversation_creator(
        self, notebook_id: str, conversation_id: str
    ) -> "str | None":
        """One notebook-scoped read of a conversation's creator, for the
        authenticated share endpoints' row-level gate. ``None`` when the
        conversation is not in this notebook. See
        ``AskStateStore.conversation_creator``."""
        return self._runtime.ask_state.conversation_creator(
            notebook_id, conversation_id
        )

    def share_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
        expected_through_id: str | None = None,
    ) -> dict:
        """Issue (or reuse) the public token and pin the read watermark. When
        ``expected_through_id`` is given it pins to exactly that answer (the
        disclosure TOCTOU fix); empty falls back to the current latest. See
        ``AskStateStore.share_conversation``."""
        return self._runtime.ask_state.share_conversation(
            notebook_id, conversation_id, expected_through_id
        )

    def conversation_share_state(
        self, notebook_id: str, conversation_id: str
    ) -> dict:
        """Issued token + watermark for the write-guarded read-back endpoint.
        See ``AskStateStore.conversation_share_state``."""
        return self._runtime.ask_state.conversation_share_state(
            notebook_id, conversation_id
        )

    def unshare_conversation(self, notebook_id: str, conversation_id: str) -> None:
        """Revoke the public link. See ``AskStateStore.unshare_conversation``."""
        return self._runtime.ask_state.unshare_conversation(
            notebook_id, conversation_id
        )

    def public_conversation_by_token(self, token: str) -> "dict | None":
        """Resolve one shared conversation by token alone — the ONLY
        session-free conversation read (T3, mirrors
        ``public_report_by_token``). Takes nothing but the token: the caller is
        the anonymous router, which binds no request user, so this must never
        consult the current-user ContextVar. See
        ``AskStateStore.public_conversation_by_token``."""
        return self._runtime.ask_state.public_conversation_by_token(token)

    # --- 深度报告 ---
    # Task 25: reports 表行级持久化移入 runtime 所有的 ReportStore;此处保持
    # 冻结签名委托(notebook 存在性守卫留在 create_report 委托里)。脱离连接
    # 执行编排见 report_execution 属性(ReportExecutionCoordinator)。
    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str:
        return self._runtime.report_application.create_report(
            notebook_id, question, depth
        )

    def update_report(self, notebook_id: str, report_id: str, *, status=None,
                      progress=None, error=None, outline=None, sections=None,
                      gaps=None, references=None, content_md=None,
                      section_status=None, understanding=None) -> None:
        return self._runtime.report_store.update_report(
            notebook_id, report_id, status=status, progress=progress,
            error=error, outline=outline, sections=sections, gaps=gaps,
            references=references, content_md=content_md,
            section_status=section_status, understanding=understanding)

    def claim_report_intent(
        self, notebook_id: str, report_id: str, understanding: dict
    ) -> bool:
        return self._runtime.report_store.claim_report_intent(
            notebook_id, report_id, understanding
        )

    def claim_report_generation(
        self,
        notebook_id: str,
        report_id: str,
        understanding: dict | None = None,
    ) -> bool:
        if understanding is None:
            return self._runtime.report_store.claim_report_generation(
                notebook_id, report_id
            )
        return self._runtime.report_store.claim_report_generation(
            notebook_id, report_id, understanding
        )

    def _report_row_to_dict(self, row, *, full: bool) -> dict:
        return self._runtime.report_store.row_to_dict(row, full=full)

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        return self._runtime.report_store.get_report(notebook_id, report_id)

    def list_reports(self, notebook_id: str, *, created_by: str | None) -> list:
        return self._runtime.report_store.list_reports(
            notebook_id, created_by=created_by
        )

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        return self._runtime.report_store.delete_report(notebook_id, report_id)

    def export_reports(self, notebook_id: str, report_ids: list, *,
                       created_by: str | None) -> list:
        return self._runtime.report_store.export_reports(
            notebook_id, report_ids, created_by=created_by
        )

    def share_report(self, notebook_id: str, report_id: str) -> str:
        return self._runtime.report_store.share_report(notebook_id, report_id)

    def unshare_report(self, notebook_id: str, report_id: str) -> None:
        return self._runtime.report_store.unshare_report(notebook_id, report_id)

    def report_share_token(self, notebook_id: str, report_id: str) -> str:
        return self._runtime.report_store.report_share_token(notebook_id, report_id)

    def public_report_by_token(self, token: str) -> dict | None:
        return self._runtime.report_store.public_report_by_token(token)

    @property
    def report_execution(self):
        """Explicit deep-report execution coordinator (Task 25); routes'
        _launch_plan_job/_launch_generate_job delegate here."""
        return self._runtime.report_execution


    def pending_actions(self, user_id: str) -> dict:
        """聚合当前用户「我创建的」notebook 的三类待办(深度报告待确认/治理队列/
        索引状态),供「待确认中心」铃铛使用。REST 与后续流式端点共用同一计算核心。

        只读、无 LLM/embed 调用。严格按 notebooks.created_by = user_id 过滤 ——
        不走 list_notebooks()(它含"分享给我只读"的库,会破坏用户间隔离)。
        index 项的状态分类完全委托给已有的 scale_index_status(nb).state,不重新
        实现索引状态机;building/queued 视为进行中、不计入 count(不是待用户确认的
        动作),但仍作为 item 呈现供前端展示进度。

        「晋升候选」子项仅对 admin 呈现 —— 非 admin 也能创建晋升候选
        (propose_promotion 只受 require_notebook_capability("knowledge:write")
        守卫,P0 阶段解析为 owner-only),但深链目标
        /promotion-queue 是 admin-only(403),故对非 admin 隐藏该项以免铃铛
        指向一个必 403 的动作。"""
        return self._runtime.pending_actions_service.list_for_user(user_id)

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse:
        return self._runtime.ask_state.submit_feedback(answer_id, payload)

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        return self._runtime.catalog.notebook_analytics(notebook_id)

    def warm_open_path_caches(
        self, progress: Optional[Callable[[int, int], None]] = None
    ) -> int:
        return self._runtime.catalog.warm_open_path_caches(progress)

    def _preload_scale_retrieval_artifacts(
        self, progress: Optional[Callable[[int, int], None]] = None
    ) -> dict[str, int]:
        """Startup-only strict preload behind the process readiness gate."""
        return self.retrieval.preload_scale_artifacts(progress)

    # object_type -> counts-dict key mapping (C5 batched GROUP BY projection).
    # Canonical map lives on NotebookSummaryQuery; the facade keeps the frozen
    # compatibility name pointing at the same object.
    _NOTEBOOK_COUNT_TYPES: Dict[str, str] = NotebookSummaryQuery._NOTEBOOK_COUNT_TYPES

    def _knowledge_type_counts(self, db: object, notebook_id: str) -> Dict[str, int]:
        return self._runtime.notebook_summaries.knowledge_type_counts(db, notebook_id)

    def _notebook_from_row(self, db: object, row: object) -> NotebookSummary:
        return self._runtime.notebook_summaries.from_row(db, row)

    def _source_from_row(self, db: object, row: object) -> SourceSummary:
        return self._runtime.source_store.source_from_row(db, row)

    def _sources_from_rows(self, db: object, rows: List[object]) -> List[SourceSummary]:
        return self._runtime.source_store.sources_from_rows(db, rows)

    def _extraction_warning(self, db: object, source_id: str) -> Optional[str]:
        return self._runtime.source_store.extraction_warning(db, source_id)

    def _source_type_from_name(self, file_name: str) -> str:
        return self._runtime.source_ingestion.source_type(file_name)

    def _summarize_source(self, title: str, elements: List[SourceElement]) -> str:
        return self._runtime.source_ingestion.summarize(title, elements)

    def _delete_file(self, file_path: str) -> None:
        return self._runtime.source_files.delete(file_path)

    @property
    def storage_dir(self):
        return self._runtime.storage_dir

    @storage_dir.setter
    def storage_dir(self, value) -> None:
        self._runtime.storage_dir = value

    # 查询侧 embedder 不在 facade 暴露:模型客户端一律经 self._runtime.models
    # 访问,embedder 的替换走 self._runtime.set_embedder()(会连带重连下游)。
    # 见 test_retired_model_client_facade_attributes_do_not_exist。
    @property
    def _notebook_langs_cache(self):
        return self._runtime.notebook_languages

    @_notebook_langs_cache.setter
    def _notebook_langs_cache(self, value) -> None:
        self._runtime.notebook_languages = value

    def start_ask_stream(self, notebook_id: str, payload: AskRequest, mode,
                         *, user_id: str):
        """Start detached Ask execution through the runtime-owned coordinator."""
        return self._runtime.ask_execution.start(
            notebook_id, payload, mode, user_id=user_id
        )

    # --- knowhow-tables PR-1 Task 2: one-hop delegates onto the runtime-
    # --- owned KnowhowStore. Task 5 (projector) and Task 6 (import/table
    # --- API) build directly on these exact names/signatures.
    def create_knowhow_table(
        self, notebook_id: str, title: str, description: str, columns: list,
        created_by: str = "", actor: str = "", origin: str = "user",
    ) -> str:
        return self._runtime.knowhow_store.create_knowhow_table(
            notebook_id, title, description, columns, created_by,
            actor=actor, origin=origin,
        )

    def create_knowhow_table_with_rows(
        self,
        notebook_id: str,
        title: str,
        description: str,
        columns: list,
        rows,
        created_by: str = "",
        actor: str = "",
        origin: str = "user",
    ) -> str:
        return self._runtime.knowhow_store.create_knowhow_table_with_rows(
            notebook_id, title, description, columns, rows, created_by,
            actor=actor, origin=origin,
        )

    def list_knowhow_tables(self, notebook_id: str) -> list:
        return self._runtime.knowhow_store.list_knowhow_tables(notebook_id)

    def get_knowhow_table(self, table_id: str) -> dict:
        return self._runtime.knowhow_store.get_knowhow_table(table_id)

    def add_knowhow_row(
        self, table_id: str, cells: dict, position: Optional[int] = None,
        actor: str = "", origin: str = "user",
    ) -> str:
        return self._runtime.knowhow_store.add_knowhow_row(
            table_id, cells, position, actor=actor, origin=origin,
        )

    def append_knowhow_rows(
        self, table_id: str, rows, actor: str = "", origin: str = "user",
    ) -> list[str]:
        return self._runtime.knowhow_store.append_knowhow_rows(
            table_id, rows, actor=actor, origin=origin,
        )

    def update_knowhow_cell(
        self, row_id: str, column_id: str, content_md: str, require_assets=(),
        actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.update_knowhow_cell(
            row_id, column_id, content_md, require_assets,
            actor=actor, origin=origin,
        )

    def update_knowhow_cells(
        self, row_ids: list, column_id: str, content_md: str, require_assets=(),
        actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.update_knowhow_cells(
            row_ids, column_id, content_md, require_assets,
            actor=actor, origin=origin,
        )

    def update_knowhow_cells_bulk_guarded(
        self, notebook_id: str, updates: list,
        actor: str = "", origin: str = "user",
    ) -> dict:
        return self._runtime.knowhow_store.update_knowhow_cells_bulk_guarded(
            notebook_id, updates, actor=actor, origin=origin,
        )

    def update_knowhow_cells_guarded_atomic(
        self,
        notebook_id: str,
        updates: list,
        require_assets=(),
        anchor_column_id=None,
        expected_anchor=None,
        actor: str = "",
        origin: str = "user",
    ) -> dict:
        return self._runtime.knowhow_store.update_knowhow_cells_guarded_atomic(
            notebook_id, updates, require_assets, anchor_column_id, expected_anchor,
            actor=actor, origin=origin,
        )

    def delete_knowhow_table(self, table_id: str) -> dict:
        return self._runtime.knowhow_store.delete_knowhow_table(table_id)

    def set_knowhow_hidden_source(self, table_id: str, source_id: str) -> None:
        return self._runtime.knowhow_store.set_knowhow_hidden_source(
            table_id, source_id
        )

    def bump_knowhow_mutation_seq(self, table_id: str) -> int:
        return self._runtime.knowhow_store.bump_knowhow_mutation_seq(table_id)

    def insert_notebook_asset(
        self,
        notebook_id: str,
        filename: str,
        mime: str,
        size: int,
        created_by: str,
        source_id: Optional[str] = None,
    ) -> str:
        return self._runtime.knowhow_store.insert_notebook_asset(
            notebook_id, filename, mime, size, created_by, source_id
        )

    def get_notebook_asset(self, asset_id: str) -> Optional[dict]:
        return self._runtime.knowhow_store.get_notebook_asset(asset_id)

    def source_asset_ids(self, source_id: str) -> List[str]:
        return self._runtime.knowhow_store.source_asset_ids(source_id)

    def delete_source_asset_rows(self, source_id: str) -> List[dict]:
        return self._runtime.knowhow_store.delete_source_asset_rows(source_id)

    # ---------------------------------------------------- paper metadata
    def get_paper_meta(self, source_id: str) -> Optional[dict]:
        return self._runtime.source_store.get_paper_meta(source_id)

    def sources_missing_paper_meta(
        self, notebook_id: str, include_existing: bool = False
    ) -> List[str]:
        return self._runtime.source_store.sources_missing_paper_meta(
            notebook_id, include_existing
        )

    def backfill_paper_metadata(
        self, notebook_id: str, force: bool = False, progress=None
    ) -> dict:
        return self._runtime.source_ingestion.backfill_paper_metadata(
            notebook_id, force=force, progress=progress
        )

    # --- knowhow-tables PR-2+3 Task 1: editing/code-attachment one-hop
    # --- delegates onto the same runtime-owned KnowhowStore (editing API,
    # --- projection scheduler, and the agent surface build on these).
    def update_knowhow_table_meta(
        self, table_id: str, title: Optional[str] = None,
        description: Optional[str] = None,
        actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.update_knowhow_table_meta(
            table_id, title, description, actor=actor, origin=origin,
        )

    def set_knowhow_anchor_column(
        self, table_id: str, column_id: Optional[str],
        actor: str = "", origin: str = "user",
    ) -> Optional[str]:
        return self._runtime.knowhow_store.set_knowhow_anchor_column(
            table_id, column_id, actor=actor, origin=origin,
        )

    def add_knowhow_column(
        self, table_id: str, name: str, kind: str, position: Optional[int] = None,
        actor: str = "", origin: str = "user",
    ) -> str:
        return self._runtime.knowhow_store.add_knowhow_column(
            table_id, name, kind, position, actor=actor, origin=origin,
        )

    def rename_knowhow_column(
        self, column_id: str, name: str, actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.rename_knowhow_column(
            column_id, name, actor=actor, origin=origin,
        )

    def set_knowhow_column_kind(
        self, column_id: str, kind: str, actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.set_knowhow_column_kind(
            column_id, kind, actor=actor, origin=origin,
        )

    def delete_knowhow_column(
        self, column_id: str, actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.delete_knowhow_column(
            column_id, actor=actor, origin=origin,
        )

    def delete_knowhow_row(
        self, row_id: str, actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.delete_knowhow_row(
            row_id, actor=actor, origin=origin,
        )

    def validate_cell_target(self, row_id: str, column_id: str) -> None:
        return self._runtime.knowhow_store.validate_cell_target(row_id, column_id)

    def upsert_knowhow_cell_code(
        self, row_id: str, column_id: str, code_text: str, language: str,
        updated_by: str, cell_content_hash: str,
        actor: str = "", origin: str = "user",
    ) -> str:
        return self._runtime.knowhow_store.upsert_knowhow_cell_code(
            row_id, column_id, code_text, language, updated_by, cell_content_hash,
            actor=actor, origin=origin,
        )

    def get_knowhow_cell_code(self, row_id: str, column_id: str) -> Optional[dict]:
        return self._runtime.knowhow_store.get_knowhow_cell_code(row_id, column_id)

    def delete_knowhow_cell_code(
        self, row_id: str, column_id: str,
        actor: str = "", origin: str = "user",
    ) -> None:
        return self._runtime.knowhow_store.delete_knowhow_cell_code(
            row_id, column_id, actor=actor, origin=origin,
        )

    def list_knowhow_cell_code(self, table_id: str) -> list:
        return self._runtime.knowhow_store.list_knowhow_cell_code(table_id)

    # --- knowhow-tables PR-2+3 Task 10: agent surface one-hop delegate ----
    def get_knowhow_row_location(self, row_id: str) -> Optional[dict]:
        return self._runtime.knowhow_store.get_knowhow_row_location(row_id)

    def knowhow_history_head_seq(self, table_id: str) -> int:
        return self._runtime.knowhow_history_store.head_seq(table_id)

    def list_knowhow_changes(
        self, table_id: str, limit: int = 50, before_seq: "int | None" = None
    ) -> list:
        return self._runtime.knowhow_history_store.list_changes(
            table_id, limit, before_seq
        )

    def knowhow_history_page(
        self, table_id: str, limit: int = 50, before_seq: "int | None" = None
    ) -> dict:
        return self._runtime.knowhow_history_store.history_page(
            table_id, limit, before_seq
        )

    def get_knowhow_change(self, table_id: str, seq: int) -> "dict | None":
        return self._runtime.knowhow_history_store.get_change(table_id, seq)

    def knowhow_changes_between(
        self, table_id: str, from_seq: int, to_seq: int
    ) -> list:
        return self._runtime.knowhow_history_store.changes_between(
            table_id, from_seq, to_seq
        )

    def knowhow_cell_history(
        self, table_id: str, row_id: str, column_id: str, limit: int = 50
    ) -> list:
        return self._runtime.knowhow_history_store.cell_history(
            table_id, row_id, column_id, limit
        )

    def revert_knowhow_table(
        self, table_id: str, target_seq: int, expected_head_seq: int,
        actor: str = "",
    ) -> dict:
        return self._runtime.knowhow_history_store.revert_to(
            table_id, target_seq, expected_head_seq, actor
        )

    def create_knowhow_milestone(
        self, table_id: str, seq: int, name: str, note: str, created_by: str
    ) -> dict:
        return self._runtime.knowhow_history_store.create_milestone(
            table_id, seq, name, note, created_by
        )

    def delete_knowhow_milestone(self, table_id: str, milestone_id: str) -> None:
        return self._runtime.knowhow_history_store.delete_milestone(
            table_id, milestone_id
        )

    def list_knowhow_milestones(self, table_id: str) -> list:
        return self._runtime.knowhow_history_store.list_milestones(table_id)

    def prune_knowhow_history(self, table_id: str, before_iso: str) -> dict:
        return self._runtime.knowhow_history_store.prune(table_id, before_iso)

    # --- paper-meta status: backfill progress one-hop delegates ----------
    def paper_meta_backfilling(self, notebook_id: str) -> bool:
        """O(1) 内存 membership；重启后天然为 False（未在跑）。"""
        return self._runtime.source_ingestion.paper_meta_backfilling(notebook_id)

    def paper_meta_backfill_progress(self, notebook_id: str) -> Optional[dict]:
        """返回 {"total","done"} 的浅拷贝或 None（未在跑）。锁内取快照。"""
        return self._runtime.source_ingestion.paper_meta_backfill_progress(notebook_id)
    # --- memory cross-notebook copy/move Task B2: one-hop delegate --------
    def transfer_memories(
        self,
        user_id: str,
        memory_ids: List[str],
        target_notebook_id: str,
        mode: str,
        extract_kg: bool = True,
    ) -> list[dict]:
        return self._runtime.memory_service.transfer(
            user_id, memory_ids, target_notebook_id, mode, extract_kg
        )

    # --- durable KG build jobs: task-scoped entry points -----------------
    def prepare_notebook_kg_job(
        self,
        notebook_id: str,
        mode: str,
        *,
        target_limit: int | None = None,
        retry_partial: bool = False,
    ) -> dict:
        return self._runtime.knowledge_lifecycle.prepare_notebook_kg_job(
            notebook_id,
            mode,
            target_limit=target_limit,
            retry_partial=retry_partial,
        )

    def fail_notebook_kg_job_submission(self, job_id: str) -> bool:
        return self._runtime.knowledge_lifecycle.fail_notebook_kg_job_submission(
            job_id
        )

    def execute_notebook_kg_job(
        self,
        notebook_id: str,
        job_id: str,
        mode: str,
        *,
        progress=None,
        target_limit: int | None = None,
        retry_partial: bool = False,
        finalize=None,
    ) -> dict:
        return self._runtime.knowledge_lifecycle.execute_notebook_kg_job(
            notebook_id,
            job_id,
            mode,
            progress=progress,
            target_limit=target_limit,
            retry_partial=retry_partial,
            finalize=finalize,
        )


def _now() -> str:
    # Ask history is ordered by conversation.updated_at.  Preserve sub-second
    # precision so two conversations touched inside one wall-clock second still
    # have a meaningful recency order (UUID/id tie-breakers are not activity).
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _citation(label: str, evidence: Evidence, tier: str = "personal") -> Citation:
    return Citation(
        label=label,
        source_id=evidence.source_id,
        element_id=evidence.element_id,
        location_label=evidence.location_label,
        quoted_span=evidence.quoted_span,
        tier=tier,
    )


def _as_str_list(value: object) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value:
        return [str(value).strip()]
    return []


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


def _call_extraction_compat(
    callback, task_callback, source_id: str, kwargs: dict
) -> None:
    """Keep legacy one-argument monkeypatch seats while forwarding task state."""
    if getattr(callback, "__func__", None) is RepositoryFacade._run_extraction:
        return task_callback(source_id, **kwargs)
    return callback(source_id)
