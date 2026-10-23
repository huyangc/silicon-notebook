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
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import uuid4

from app.core.config import Settings
from app.domain.cancellation import CoreCancellation
from app.domain.extensions import (
    AskCompletedObserverHostPort,
    ElementAssetLocation,
    ElementEnricherHostPort,
    ReportCompletedObserverHostPort,
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.domain.gap_consult import GapConsultHostPort
from app.domain.reflect_action import ReflectActionHostPort
from app.domain.ask_engine import AskEngineHostPort
from app.domain.knowledge_contracts import CONCEPT_DETAIL_PAGE_MAX
from app.domain.indexing_pipeline import (
    IndexingPipelineHostPort,
    IndexingPipelineStalePlanError,
)
from app.core.request_context import (
    _REQUEST_USER,
    get_request_user,
    request_user_id,
    reset_request_user,
    set_request_user,
)
from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.llm import OpenAICompatibleClient, cap_kwargs
from app.models.identity import (
    AgentPrincipal,
    AgentProfile,
    AgentTokenAccess,
    AgentTokenIssued,
    AgentTokenSummary,
    UserProfile,
)
from app.models.memory import MemoryRecord, MemoryUpdate, PaginatedMemories
from app.models.notebooks import (
    NotebookAnalytics,
    NotebookCreate,
    NotebookSummary,
    NotebookUpdate,
)
from app.models.sources import (
    AddUrlSourcesResult,
    PaginatedSources,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
)
from app.models.ask import (
    AnswerAnchor,
    AskEvidence,
    AskOutput,
    AskRequest,
    AskResponse,
    ConversationBulkDeleteResult,
    ConversationDetail,
    ConversationSummary,
    FeedbackRequest,
    FeedbackResponse,
    ModelError,
    NotebookSearchResponse,
    QueryIntentContract,
    RuleCard,
    StoredSubmittedVia,
)
from app.models.knowledge import (
    DuplicateGroup,
    KnowledgeGraph,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    MergeRequest,
    PaginatedKnowledge,
)
from app.domain.retrieval import RetrievedChunk
from app.services import kg_ingest
from app.services.ask_followup import FollowupResolution
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.vector_cache import LargeAwareLRUCache, LRUProcessCache
from app.services.extraction_profiles import (
    PROFILES,
    ObjectSchema,
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
from app.repositories.source_files import safe_filename as _safe_filename  # noqa: F401 — compatibility export
from app.services.parsers import mineru_content_list_to_elements
from app.services.knowhow.assets import AssetService
from app.services.source_image_persist import make_persist_image_factory
from app.services.prompts import (
    ANSWER_SCHEMA_HINT,
    FOLLOWUP_REWRITE_SCHEMA_HINT,
    NOTEBOOK_META_SCHEMA_HINT,
    SCHEMA_INDUCTION_HINT,
    answer_prompt,
    followup_rewrite_prompt,
    notebook_meta_prompt,
    schema_induction_prompt,
)
from app.services.repository import UploadedSourceFile
from app.services.knowledge_query import KnowledgeQueryService
from app.services.knowledge_governance import KnowledgeGovernanceService
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.retrieval import (
    RetrievedKnowledge,
    RetrievedElement,
    W_KEYWORD,
    W_SEMANTIC,
    _fuse,
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


# The `[k…]` answer-marker regexes moved to app.services.ask_service with the
# mode engines (Task 24).


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


def _project_knowhow_table(repo: "RepositoryFacade", table_id: str, embed: bool) -> None:
    """The ``project_table`` seam ``wire_sharing`` hands to the notebook
    importer: one synchronous projection pass (not the debounced scheduler --
    the import must know the pass is over before its re-embedding legs run).
    Module-level and deferred-import for the same reasons as
    ``_schedule_knowhow_projection``."""
    from app.services.knowhow.api import build_projector

    build_projector(repo).project_table(table_id, embed=embed)


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


def _resolve_element_assets(
    repo: "RepositoryFacade", notebook_id: str, asset_ids
) -> dict[str, ElementAssetLocation]:
    """The ``resolve_element_assets`` seam ``wire_source_ingestion`` hands to
    ``SourceIngestionService`` — a module-level function (not a facade method)
    for exactly the reason ``_make_persist_image`` above is one: the
    one-hop-delegate facade-body contract (``test_repository_facade_
    contract.py``) forbids a NAMED facade method whose body does real work
    (a loop over ids, an ``AssetService`` construction, a filesystem stat)
    rather than delegating to a single owning component.

    This resolution runs on the CALLING thread, before any plugin starts, and
    that is the whole point: the extension host serves image bytes to a worker
    thread that must perform no database access, so the row lookup and the
    path derivation have to be finished by the time it is handed over.

    Only assets that belong to ``notebook_id`` and whose file is actually on
    disk are returned.  A cross-notebook row is not "missing", it is a
    boundary violation, and a row whose file never landed would make the
    SDK's "non-empty asset id means there ARE bytes" promise false.  Every
    per-asset failure — an unknown id, a malformed row, an unreadable path —
    skips that one asset rather than failing the parse: the enrichment point
    is best-effort in every other direction too.  Cancellation is the sole
    exception: the parse job is being torn down, and swallowing that into
    "this asset has no image" would keep the point running past it.
    """
    located: dict[str, ElementAssetLocation] = {}
    if not notebook_id:
        return located
    assets = AssetService(repo)
    for asset_id in asset_ids:
        if type(asset_id) is not str or not asset_id or asset_id in located:
            continue
        try:
            asset = repo.get_notebook_asset(asset_id)
            if asset is None or asset["notebook_id"] != notebook_id:
                continue
            path = assets.path_for(asset)
            if not path.is_file():
                continue
            located[asset_id] = ElementAssetLocation(str(path), str(asset["mime"]))
        except CoreCancellation:
            raise
        except Exception:  # noqa: BLE001 — one unresolvable asset, not a parse
            continue
    return located


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
        ask_engine_host: AskEngineHostPort | None = None,
        indexing_pipeline_host: IndexingPipelineHostPort | None = None,
        gap_consult_host: GapConsultHostPort | None = None,
        element_enricher_host: ElementEnricherHostPort | None = None,
        reflect_action_host: ReflectActionHostPort | None = None,
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
            ask_engine_host=ask_engine_host,
            indexing_pipeline_host=indexing_pipeline_host,
            gap_consult_host=gap_consult_host,
            element_enricher_host=element_enricher_host,
            reflect_action_host=reflect_action_host,
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
            project_table=lambda table_id, embed: _project_knowhow_table(self, table_id, embed),
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
        # 体检 H4/H5 事件失效插槽(竞态论证见 _invalidate_checkup_missing_vector_counts)。
        self._runtime.on_source_vectors_written = (
            self._invalidate_checkup_missing_vector_counts
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
            ent_chunk_map=lambda notebook_id, paged=False: (
                _repository_from_weakref(repository_ref)
            )._ent_chunk_map(notebook_id, paged=paged),
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
            cluster_map=lambda notebook_id: _repository_from_weakref(
                repository_ref
            ).cluster_map(notebook_id),
            incremental_fuse_source=lambda notebook_id, source_id: (
                _repository_from_weakref(repository_ref)._runtime
                .knowledge_lifecycle.incremental_fuse_source(notebook_id, source_id)
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
            # 同款晚绑定:发布器等在飞导入收敛(codex #602 R13 P1)。
            wait_ingestion_drain=lambda nb, t: (
                self._runtime.source_ingestion.wait_for_ingestion_drain(nb, t)),
            cluster_map=lambda notebook_id: self.cluster_map(notebook_id),
            annotate_edge_support=lambda notebook_id, edges: (
                self._annotate_edge_support(notebook_id, edges)
            ),
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
            embed_knowledge=lambda object_id, notebook_id, payload, mark_dirty_in_tx=None: (
                self._embed_knowledge(object_id, notebook_id, payload, mark_dirty_in_tx=mark_dirty_in_tx)
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
            preserve_existing=False, indexing_pipeline_id="",
            indexing_pipeline_version="builtin.chunk.v1": (
                self._begin_extraction_run(
                    source_id,
                    notebook_id,
                    run_id,
                    created_at,
                    preserve_existing=preserve_existing,
                    indexing_pipeline_id=indexing_pipeline_id,
                    indexing_pipeline_version=indexing_pipeline_version,
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
            apply_notebook_meta=lambda notebook_id, **values: (
                self._apply_notebook_meta(notebook_id, **values)
            ),
            make_persist_image=lambda notebook_id, source_id, created_by: (
                _make_persist_image(self, notebook_id, source_id, created_by)
            ),
            delete_source_images=lambda sid: AssetService(self).delete_source_images(sid),
            resolve_element_assets=partial(_resolve_element_assets, self),
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

    def resolve_session(self, token: str) -> "UserProfile | None":
        return self._runtime.identity.resolve_session(token)

    def audit_labels_for_user_ids(self, user_ids: List[str]) -> dict[str, str]:
        return self._runtime.identity.audit_labels_for_user_ids(user_ids)

    def visible_document_count(self, notebook_id: str) -> int:
        return self._runtime.source_store.visible_document_count(notebook_id)

    def effective_document_limit(self, user_id: "str | None") -> int:
        return self._runtime.identity.effective_document_limit(user_id)

    def notebook_owner(self, notebook_id: str) -> "tuple[str | None, str | None]":
        return self._runtime.identity.notebook_owner(notebook_id)

    def load_notebook_scale_facts(self, notebook_id: str):
        return self._runtime.queries.load_notebook_scale_facts(notebook_id)

    def chat(self, workload_id: str):
        return self._runtime.models.chat(workload_id)

    def configured(self, workload_id: str) -> bool:
        return self._runtime.models.configured(workload_id)

    def parallelism(self, workload_id: str) -> int:
        return self._runtime.models.parallelism(workload_id)

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
        """Agentic Memory P2's retrieval-experience store (T5), partitioned by
        notebook since SQLite v79 / PostgreSQL 0059.

        One hop to the runtime-owned ``RetrievalExperienceStorePort`` seat,
        the same shape and the same reasoning as ``agent_profile`` above:
        method names like ``read_partition``/``count``/``upsert_experience``
        are far too generic to flatten onto a facade shared with every other
        domain.

        ⚠ Read that port's docstring before adding a consumer. ``notebook_id``
        here is a PARTITION key, not the tenancy predicate the rest of this
        facade's stores give you: it narrows who sees an entry, but what may
        become a row at all is still decided one layer up, by
        ``retrieval_experience_projection``. A consumer that writes rows
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

    def _source_ids_from_evidence(self, evidence_json: Optional[str | list]) -> set:
        """PURE parse of an evidence JSON TEXT value into its distinct
        source_ids — canonical body lives on KnowledgeStore (Task 13)."""
        return self._runtime.knowledge.source_ids_from_evidence(evidence_json)

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

    def list_notebook_bases(
        self, notebook_id: str, viewer_id: str | None = None
    ) -> list[dict]:
        return self._runtime.notebook_store.list_mount_edges_for_notebook(
            notebook_id, viewer_id
        )

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
        self, owner_id: str, offset: int = 0, limit: int = 100,
        *, include_deleted: bool = False,
    ) -> list[AgentProfile]:
        return self._runtime.memory_service.list_agent_profiles(
            owner_id, offset, limit, include_deleted=include_deleted
        )

    def update_agent_profile(
        self, profile_id: str, owner_id: str, patch: Any
    ) -> AgentProfile:
        return self._runtime.memory_service.update_agent_profile(
            profile_id, owner_id, patch
        )

    def delete_agent_profile(self, profile_id: str, owner_id: str) -> None:
        self._runtime.memory_service.delete_agent_profile(profile_id, owner_id)

    def issue_agent_token(
        self,
        owner_id: str,
        agent_profile_id: str,
        scopes: List[str],
        default_notebook_id: str,
        notebook_ids: List[str],
        expires_at: "str | None",
        allow_answer_links: bool = False,
    ) -> AgentTokenIssued:
        return self._runtime.memory_service.issue_agent_token(
            owner_id,
            agent_profile_id,
            scopes,
            default_notebook_id,
            notebook_ids,
            expires_at,
            allow_answer_links,
        )

    def list_agent_tokens(
        self, owner_id: str, offset: int = 0, limit: int = 100
    ) -> list[AgentTokenSummary]:
        return self._runtime.memory_service.list_agent_tokens(owner_id, offset, limit)

    def revoke_agent_token(
        self, owner_id: str, token_id: str
    ) -> AgentTokenSummary:
        return self._runtime.memory_service.revoke_agent_token(owner_id, token_id)

    def update_agent_token_access(
        self,
        owner_id: str,
        token_id: str,
        scopes: List[str],
        default_notebook_id: str,
        notebook_ids: List[str],
        expires_at: "str | None",
        expected: AgentTokenAccess | None = None,
        allow_answer_links: bool = False,
    ) -> AgentTokenSummary:
        return self._runtime.memory_service.update_agent_token_access(
            owner_id,
            token_id,
            scopes,
            default_notebook_id,
            notebook_ids,
            expires_at,
            expected,
            allow_answer_links,
        )

    def agent_token_secret(self, owner_id: str, token_id: str) -> str:
        return self._runtime.memory_service.agent_token_secret(owner_id, token_id)

    def resolve_agent_token(self, raw_token: str) -> "AgentPrincipal | None":
        return self._runtime.memory_service.resolve_agent_token(raw_token)

    def resolve_agent_token_status(
        self, raw_token: str
    ) -> "tuple[AgentPrincipal | None, str]":
        return self._runtime.memory_service.resolve_agent_token_status(raw_token)

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
    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return self._runtime.scale_artifacts.notebook_copy_stats(notebook_id)

    def shared_preview(self, notebook_id: str) -> dict:
        return self._runtime.sharing.shared_preview(notebook_id)

    def shared_by_me(self, user_id: str) -> list:
        return self._runtime.sharing.shared_by_me(user_id)

    def _insert_row(self, db, table: str, data: dict) -> None:
        return self._runtime.sharing_store.insert_row_values(db, table, data)

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_access_notebook(notebook_id, user_id)

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_read_notebook(notebook_id, user_id)

    def source_notebook_id(
        self,
        source_id: str,
        *,
        viewer_id: "str | None" = None,
        visible_only: bool = False,
    ) -> "str | None":
        return self._runtime.sharing.source_notebook_id(
            source_id, viewer_id=viewer_id, visible_only=visible_only
        )

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        """Delete all KG artifacts for a notebook — KnowledgeLifecycleService
        owns the orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.delete_notebook_kg(notebook_id)

    # ------------------------------------------------------------------
    # KG search: FTS5 (lexical) + ANN (semantic) + hydration
    # ------------------------------------------------------------------

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

    def all_visible_source_ids(self, notebook_id: str) -> List[str]:
        """Return the current notebook's complete user-visible source id set."""
        return self._runtime.source_store.all_visible_source_ids(notebook_id)

    def indexing_pipeline_options(self, notebook_id: str) -> dict:
        """Project the frozen registry and this notebook's published choice."""
        return self._runtime.indexing_pipeline.projection(notebook_id)

    def set_indexing_pipeline(self, notebook_id: str, pipeline_id: str | None) -> dict:
        """Persist desired intent, claim one durable rebuild job, then return."""
        intent = self._runtime.indexing_pipeline.begin(notebook_id, pipeline_id)
        if not intent["changed"]:
            return {**intent, "rebuild_status": "idle", "job_id": None}
        pipeline_id = str(intent.pop("_pipeline_id"))
        pipeline_version = str(intent.pop("_pipeline_version"))
        pipeline_generation = str(intent.pop("_pipeline_generation"))
        # Reuse the existing durable notebook rebuild row/single-flight even
        # when this deployment has no KG model. In that case the worker skips
        # only PR-2's model-dependent middle stage; chunk + scale remain durable.
        job = self._runtime.knowledge_lifecycle.prepare_notebook_kg_job(
            notebook_id,
            "rebuild",
            allow_without_model=True,
        )
        if not self._runtime.notebook_store.attach_indexing_pipeline_job(
            notebook_id, pipeline_generation, job["id"]
        ):
            self._runtime.knowledge_lifecycle.fail_notebook_kg_job_submission(
                job["id"]
            )
            raise IndexingPipelineStalePlanError(notebook_id)
        from app.services import background_jobs

        try:
            background_jobs.submit(
                self.execute_indexing_pipeline_rebuild,
                notebook_id,
                job["id"],
                pipeline_id,
                pipeline_version,
                pipeline_generation,
                name=f"index-pipeline-{notebook_id}",
                notify_pending=True,
            )
        except Exception:
            self._runtime.knowledge_lifecycle.fail_notebook_kg_job_submission(
                job["id"]
            )
            raise
        return {
            **intent,
            "rebuild_status": "pending",
            "job_id": job["id"],
        }

    def execute_indexing_pipeline_rebuild(
        self,
        notebook_id: str,
        job_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
    ) -> dict:
        """Authorized durable worker: chunks → optional KG → atomic success tail."""
        lifecycle = self._runtime.knowledge_lifecycle
        try:
            rebuilt = self._runtime.indexing_pipeline.rebuild(
                notebook_id,
                job_id=job_id,
                pipeline_id=pipeline_id,
                pipeline_version=pipeline_version,
                pipeline_generation=pipeline_generation,
            )
        except BaseException:
            lifecycle.finish_indexing_pipeline_job(
                notebook_id, job_id, succeeded=False
            )
            raise

        def finalize(_result: dict) -> dict:
            return {
                "indexing_pipeline_rebuilt": True,
                "indexing_pipeline_warning_count": int(
                    rebuilt.get("warning_count", 0)
                ),
            }

        if self._runtime.models.configured("kg_extract"):
            result = lifecycle.execute_notebook_kg_job(
                notebook_id,
                job_id,
                "rebuild",
                finalize=finalize,
                preserve_existing_rebuild=True,
                indexing_pipeline_identity=(
                    pipeline_id,
                    pipeline_version,
                    pipeline_generation,
                ),
            )
        else:
            option = (
                self._runtime.indexing_pipelines.option(pipeline_id)
                if pipeline_id and self._runtime.indexing_pipelines is not None
                else None
            )
            if option is not None and option.overrides_kg_extraction:
                lifecycle.finish_indexing_pipeline_job(
                    notebook_id, job_id, succeeded=False
                )
                raise RuntimeError(
                    "selected indexing pipeline requires the KG model"
                )
            try:
                result = finalize({})
            except BaseException:
                lifecycle.finish_indexing_pipeline_job(
                    notebook_id, job_id, succeeded=False
                )
                raise
            lifecycle.finish_indexing_pipeline_job(
                notebook_id,
                job_id,
                succeeded=True,
                pipeline_identity=(
                    pipeline_id,
                    pipeline_version,
                    pipeline_generation,
                ),
            )
        if self._runtime.scale_artifacts.eligible(notebook_id):
            try:
                # Full scale artifacts share one staging directory per notebook.
                # Enter through the runtime claim instead of calling the builder
                # directly so concurrent rebuilds cannot corrupt that staging
                # area or race their atomic publication.
                self._runtime.scale_artifacts.rebuild_after_publication(notebook_id)
            except Exception:
                self._runtime.event_log.logger.exception(
                    "post-publication scale rebuild failed for indexing pipeline"
                )
        return {**rebuilt, **result, "job_id": job_id}

    def require_indexing_pipeline_write(self, notebook_id: str) -> None:
        """Fail closed before an operation can create a new index artifact."""
        self._runtime.indexing_pipeline.require_write_admission(notebook_id)

    def hidden_source_ids(self, notebook_id: str, owner_id: str) -> List[str]:
        """Hidden Memory/Knowhow projection participants for ONE user.
        Knowhow projections are notebook-wide; a Memory projection belongs
        only to the user who created it, filtered in the adapter's SQL."""
        return self._runtime.source_store.hidden_source_ids(notebook_id, owner_id)

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

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]:
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.source_ingestion.import_sources_compat(notebook_id, payload)

    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
        capacity_limit: Optional[int] = None,
        agent_profile_id: str = "",
        trusted_proxy_origins: "frozenset[str] | None" = None,
    ) -> AddUrlSourcesResult:
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.source_ingestion.add_url_sources_compat(
            notebook_id, urls, scheduler, capacity_limit, agent_profile_id,
            trusted_proxy_origins=trusted_proxy_origins,
        )

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: Optional[Callable[[str], None]] = None,
        agent_profile_id: str = "",
        capacity_limit: Optional[int] = None,
    ) -> List[UploadedSourceSummary]:
        self.require_indexing_pipeline_write(notebook_id)
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
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.build_notebook_kg(
            notebook_id,
            progress=progress,
            target_limit=target_limit,
            retry_partial=retry_partial,
            finalize=finalize,
            job_id=job_id,
            skip_policy=skip_policy,
        )

    # 测试观察点：facade 这层的索引流水线写闸是被测对象（S2 批 B 裁决保留）。
    def rebuild_notebook_kg(self, notebook_id: str) -> dict:
        """Full re-extract (delete+build, kg_building 覆盖 delete 阶段) —
        KnowledgeLifecycleService owns the orchestration (Task 15)."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.rebuild_notebook_kg(notebook_id)

    def process_source(self, source_id: str) -> SourceSummary:
        source = self._runtime.source_store.get_source(source_id)
        self.require_indexing_pipeline_write(source.notebook_id)
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
        source = self._runtime.source_store.get_source(source_id)
        self.require_indexing_pipeline_write(source.notebook_id)
        return self._runtime.source_ingestion.parse_source_compat(source_id)

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
        self, notebook_id: str, *, guard_name, guard_generation: int, name: str, purpose: str
    ) -> None:
        return self._runtime.notebook_store.apply_meta_for_notebook(
            notebook_id, guard_name=guard_name, guard_generation=guard_generation,
            name=name, purpose=purpose
        )

    def source_elements(self, source_id: str) -> List[SourceElement]:
        return self._runtime.source_store.source_elements(source_id)

    def source_elements_page(
        self,
        source_id: str,
        offset: int = 0,
        limit: int = 40,
        anchor_element_id: str = "",
        *,
        positions: Optional[Sequence[int]] = None,
    ):
        return self._runtime.source_store.source_elements_page(
            source_id, offset, limit, anchor_element_id, positions=positions
        )

    def source_id_by_hash(self, notebook_id: str, digest: str) -> Optional[str]:
        """The existing same-content source in this notebook, if any — the very
        lookup ``upload_sources`` uses for its dedup short-circuit.

        Exposed on the facade so a caller can ASK whether an upload would be a
        reuse before committing to one. MCP's ``add_source (Markdown)`` needs that to
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

        Exposed on the facade for MCP's ``read_reference``: a citation
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
        source = self._runtime.source_store.get_source(source_id)
        self.require_indexing_pipeline_write(source.notebook_id)
        return self._runtime.source_ingestion.run_extraction(source_id)

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
        indexing_pipeline_id: str = "",
        indexing_pipeline_version: str = "builtin.chunk.v1",
    ) -> None:
        """Open one source run, optionally retaining its graph for safe retry.

        codex #638 R4 P2: when ``preserve_existing`` is False the store-level
        ``begin_extraction_run`` clears this source's ``knowledge_relations``/
        ``knowledge_objects`` up front (``clear_source_graph_state``) — a real
        graph mutation that must advance ``kg_mutation_seq``, or a warm
        ``review_queue_memo`` in ANOTHER process sharing this DB keeps serving
        the just-deleted edges indefinitely (seq-gated memos only invalidate
        on a seq change; this repository's own process-local invalidate call
        — ``SourceIngestionService._invalidate_corpus_scale_memos`` — cannot
        reach a sibling process). The bump therefore rides the SAME write
        transaction as the clear (``mark_unified_kg_dirty_in_tx``, the
        ``set_edge_review`` R2 precedent): opened here at the facade instead
        of inside the store's self-contained ``begin_extraction`` (which used
        to own this transaction and had no coordinator access — stores don't
        import services), so a crash between the clear committing and the
        bump committing is impossible; either both land or neither does.
        Conditioned on ``not preserve_existing`` — a preserved-graph retry
        deletes nothing, so bumping there would just be a gratuitous
        cache-wide invalidation with no matching content change."""
        with self._write() as db:
            self._runtime.knowledge.begin_extraction_run(
                db,
                source_id,
                notebook_id,
                run_id,
                created_at,
                preserve_existing=preserve_existing,
                indexing_pipeline_id=indexing_pipeline_id,
                indexing_pipeline_version=indexing_pipeline_version,
            )
            if not preserve_existing:
                self._mark_unified_kg_dirty_in_tx(db, notebook_id)

    def _finish_extraction_run(self, run_id: str, status: str, message: str) -> None:
        return self._runtime.knowledge.finish_extraction(run_id, status, message)

    def _notebook_tier(self, notebook_id: str) -> str:
        return self._runtime.notebook_store.tier(notebook_id)

    def _invalidate_checkup_missing_vector_counts(self, notebook_id: str) -> None:
        """element/chunk 向量写事件 → 体检 H4/H5 memo 失效的转发终点(构造期挂上
        ``runtime.on_source_vectors_written``,见 __init__)。按**调用时刻**的
        ``__dict__`` 解析 checkup:未构造即 no-op(没有缓存可失效)。为什么不在 checkup
        懒构造的 property 里直接绑实例(codex 质量评审 P1):property 的 check-then-set
        无锁,并发首访会把回调永久接到被覆盖丢弃的那个实例上、事件通道静默哑掉,H4/H5
        退化成纯背底 TTL;本转发器按调用时刻解析,双实例竞态的任一胜者都收得到通知,
        与改动前「双构造无害」的形态一致。"""
        checkup = self.__dict__.get("_checkup")
        if checkup is not None:
            checkup.invalidate_missing_vector_counts(notebook_id)

    def _embed_knowledge(
        self,
        object_id: str,
        notebook_id: str,
        payload: Dict[str, object],
        *,
        mark_dirty_in_tx: "Callable[[Any, str], int] | None" = None,
    ) -> None:
        """Embed a knowledge object's own payload text (WS4: payload-level
        vectors, not just evidence-element vectors). No-op without embeddings.

        ``mark_dirty_in_tx``: threaded straight through to
        ``SourceEmbeddingService.embed_knowledge`` — see its docstring (codex
        #638 R6 P1) for when a caller must pass it (a payload REPLACE on an
        already-embedded object) versus leave it ``None`` (a first embed of a
        brand-new object)."""
        return self._runtime.source_embedding.embed_knowledge(
            object_id, notebook_id, payload, mark_dirty_in_tx=mark_dirty_in_tx
        )

    def _embed_objects_batch(self, notebook_id: str, items: List[dict],
                             progress=None, commit_every: Optional[int] = None) -> int:
        return self._runtime.source_embedding.embed_objects_batch(
            notebook_id, items, progress=progress, commit_every=commit_every
        )

    def _embed_relations_batch(self, notebook_id: str, rel_items: List[dict]) -> int:
        return self._runtime.source_embedding.embed_relations_batch(
            notebook_id, rel_items
        )

    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]:
        """All object types present in this notebook with non-deprecated counts,
        so the UI can render a tab per type — including academic/textbook types
        that have no bespoke card."""
        return self._runtime.knowledge_query.knowledge_types(notebook_id)

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

    def store_kg(self, notebook_id: str, source_id: Optional[str],
                 objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
        """Insert KG nodes/edges — KnowledgeLifecycleService owns the chunked
        write orchestration (Task 15; the frozen phase order object chunks →
        relation chunks → embeds → invalidate → dirty is unchanged)."""
        return self._runtime.knowledge_lifecycle.store_kg(
            notebook_id, source_id, objects, relations
        )

    # 测试观察点：facade 这层的索引流水线写闸是被测对象（S2 批 B 裁决保留）。
    def relink_notebook_kg(self, notebook_id: str) -> dict:
        """Backfill relink of degree-0 KG nodes — KnowledgeLifecycleService
        owns the orchestration (Task 15); frozen-signature delegate."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.relink_notebook_kg(notebook_id)

    def start_notebook_relink(self, notebook_id: str) -> dict:
        """Claim the notebook's relink slot (409 source) — Task-15 delegate."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.start_notebook_relink(notebook_id)

    def notebook_relink_status(self, notebook_id: str) -> dict:
        """Latest relink state for this notebook — Task-15 delegate."""
        return self._runtime.knowledge_lifecycle.notebook_relink_status(notebook_id)

    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict:
        """Background relink entry point — Task-15 delegate."""
        self.require_indexing_pipeline_write(notebook_id)
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
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.start_conflict_resolution(
            notebook_id
        )

    def run_conflict_resolution_job(self, notebook_id: str, job_id: str) -> dict:
        """Background conflict-detection entry point (settles on every exit)."""
        self.require_indexing_pipeline_write(notebook_id)
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

    def review_queue_page(self, notebook_id: str, limit: int = 100) -> Dict[str, Any]:
        """``{"items": [...], "total": n}`` in ONE call (R3 T-A3 v4, codex
        #638 R1) — both halves come from the SAME seq-gated ranking-memo entry
        (or the same single cold scan), so they can never describe two
        different KG versions the way two independent v3 calls could.
        KnowledgeGovernanceService owns the orchestration; frozen-signature
        delegate."""
        return self._runtime.knowledge_governance.review_queue_page(
            notebook_id, limit
        )

    def set_edge_review(
        self, notebook_id: str, rel_id: str, status: str,
        *, actor_id: Optional[str] = None,
    ) -> None:
        """Persist review_status on a knowledge_relation —
        KnowledgeGovernanceService owns the orchestration (Task 16; the frozen
        commit→dirty→invalidate phase order is unchanged). Frozen-signature
        delegate; ``actor_id`` (M1) is additive: a relation derived from
        someone else's Memory answers like a missing id."""
        return self._runtime.knowledge_governance.set_edge_review(
            notebook_id, rel_id, status, actor_id=actor_id
        )

    # --- Concept-cluster / merge-candidate CRUD (Task 5) -------------------

    def cluster_map(self, notebook_id: str) -> Dict[str, str]:
        """Cached {member_object_id: canonical_id} — RetrievalCandidateService owns
        the loader + version-cache body (Task 21); frozen-signature delegate。单一
        owner：此前 facade 与服务各留一份等价实现、共享同一缓存键（"{nb}:clustermap"），
        属双所有权漂移地雷（review 抓出），降为委托后 loader 只此一家。"""
        return self.retrieval.candidates.cluster_map(notebook_id)

    def pending_merges(self, notebook_id: str) -> List[dict]:
        return self._runtime.knowledge_governance.pending_merges(notebook_id)

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

    def _mark_unified_kg_dirty(self, notebook_id: str) -> None:
        # Task 14: the kg_mutation_seq dirty bump is coordinator-owned. This
        # wrapper stays the single funnel every KG write goes through (and the
        # frozen per-instance patch seat); the coordinator stays the single
        # place kg_mutation_seq advances — its write transaction rides the
        # facade `_write` seat, so begin/commit phase traces are unchanged.
        self._runtime.kg_mutations.mark_unified_kg_dirty(notebook_id)

    def _mark_unified_kg_dirty_in_tx(self, db, notebook_id: str) -> "tuple[int, int]":
        # In-transaction twin of `_mark_unified_kg_dirty`: rides a write
        # transaction the CALLER already holds open (relink_notebook_kg's
        # per-source commit, and — R2 P2 fix, codex #638 R2 — set_edge_review's
        # own UPDATE) instead of opening its own — the dirty bump commits
        # atomically with the write it accompanies, so a kill -9 between that
        # commit and the run's finally cannot leave committed edges invisible
        # to kg_mutation_seq. Returns (kg_mutation_seq, kg_reset_epoch), both
        # read back on the SAME connection, before commit (batch-3-W1 PR-2
        # widened this from a bare seq int) so a caller that needs its own
        # bump's value atomically paired with its own write (set_edge_review's
        # memo carry) never has to re-open a connection after commit.
        return self._runtime.kg_mutations.mark_unified_kg_dirty_in_tx(db, notebook_id)

    def _bump_cluster_mutation_seq(self, db, notebook_id: str) -> None:
        # Task 14: coordinator-owned in-transaction primitive (写簇+bump 同
        # commit,原子;kg_mutation_seq 不在此处动 — rebuild 刻意保持它稳定)。
        self._runtime.kg_mutations.bump_cluster_mutation_seq(db, notebook_id)

    def unified_kg_status(self, notebook_id: str) -> dict:
        """Unified-KG state summary — KnowledgeLifecycleService owns the read
        (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.unified_kg_status(notebook_id)
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

    def rebuild_unified_kg(self, notebook_id: str,
                           progress: Optional[Callable[[str, int, int], None]] = None,
                           force: bool = False, fresh: bool = False) -> int:
        """Streamed unified rebuild — KnowledgeLifecycleService owns the
        orchestration (Task 15). The cluster_input_version skip gate,
        force=force or fresh boundary self-defense, checkpoint GC after the
        gate and the merge-review/concept-desc/node-vector resume semantics
        are preserved verbatim in the service."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.rebuild_unified_kg(
            notebook_id, progress, force, fresh
        )
    def request_cold_community_partition(self, notebook_id: str) -> None:
        """``recluster_kg --cold-partition``: the next board partition starts cold
        (KnowledgeLifecycleService.request_cold_community_partition)."""
        self.require_indexing_pipeline_write(notebook_id)
        self._runtime.knowledge_lifecycle.request_cold_community_partition(notebook_id)

    def community_partition_state(self, notebook_id: str) -> dict:
        """Published board partition ``{"version", "mode"}``
        (KnowledgeLifecycleService.community_partition_state)."""
        return self._runtime.knowledge_lifecycle.community_partition_state(notebook_id)

    def start_unified_kg_rebuild(self, notebook_id: str) -> dict:
        """Claim the shared KG-maintenance slot for a rebuild (409 source)."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.knowledge_lifecycle.start_unified_kg_rebuild(notebook_id)

    def unified_kg_rebuild_status(self, notebook_id: str) -> dict:
        """Latest unified-rebuild state for this notebook."""
        return self._runtime.knowledge_lifecycle.unified_kg_rebuild_status(notebook_id)

    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int:
        """Background unified-rebuild entry point."""
        self.require_indexing_pipeline_write(notebook_id)
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

    # 「删除知识图谱」不走 require_indexing_pipeline_write:那道闸拦的是「会产出
    # 新索引产物」的写,删除一件都不产出;管线不可用时反而正是需要能删的时候。
    def start_kg_delete(self, notebook_id: str) -> dict:
        """Claim the shared KG-maintenance slot for a KG delete (409 source)."""
        return self._runtime.knowledge_lifecycle.start_kg_delete(notebook_id)

    def kg_delete_status(self, notebook_id: str) -> dict:
        """Latest KG-delete state for this notebook."""
        return self._runtime.knowledge_lifecycle.kg_delete_status(notebook_id)

    def run_kg_delete_job(self, notebook_id: str, job_id: str) -> dict:
        """Background KG-delete entry point (settles on every exit)."""
        return self._runtime.knowledge_lifecycle.run_kg_delete_job(
            notebook_id, job_id
        )

    def fail_kg_delete_submission(self, notebook_id: str, job_id: str) -> None:
        """Release the KG-delete claim when the worker never started."""
        return self._runtime.knowledge_lifecycle.fail_kg_delete_submission(
            notebook_id, job_id
        )

    def concept_detail(
        self,
        notebook_id: str,
        canonical_id: str,
        *,
        source_notebook_id: str = "",
        limit: int | None = CONCEPT_DETAIL_PAGE_MAX,
        after: str = "",
    ) -> dict:
        return self._runtime.knowledge_query.concept_detail(
            notebook_id,
            canonical_id,
            source_notebook_id=source_notebook_id,
            limit=limit,
            after=after,
        )

    def _enrich_evidence(
        self, db, evidence, *, owner_notebook_id: Optional[str] = None,
        sources_only: bool = False,
    ):
        return self._runtime.knowledge._enrich_evidence(
            db, evidence, owner_notebook_id=owner_notebook_id,
            sources_only=sources_only,
        )

    def node_context(
        self,
        notebook_id,
        object_id,
        *,
        source_notebook_id: str = "",
        allowed_source_ids: Optional[Sequence[str]] = None,
    ):
        return self._runtime.knowledge_query.node_context(
            notebook_id,
            object_id,
            source_notebook_id=source_notebook_id,
            allowed_source_ids=allowed_source_ids,
        )

    # --- Governance: promotion state machine (Track F) -------------------
    # KnowledgeGovernanceService owns the domain (Task 16); the facade keeps
    # frozen-signature delegates.

    def propose_promotion(
        self, notebook_id: str, object_id: str, *, target_base_id: str = "",
        actor_id: Optional[str] = None,
    ) -> dict:
        """Propose a personal-KG object for promotion into the base corpus —
        KnowledgeGovernanceService owns the orchestration (Task 16; idempotent
        active-proposal reuse and the base-notebook guard are unchanged).
        Task 7 (multi-domain base libraries) adds target_base_id: which
        mounted public reference library to promote into (required only when
        more than one is mounted). Frozen for the original two positional
        params; target_base_id is additive and backward compatible, and so
        is ``actor_id`` (M1: the proposer, for the Memory ownership check)."""
        return self._runtime.knowledge_governance.propose_promotion(
            notebook_id, object_id, target_base_id=target_base_id,
            actor_id=actor_id,
        )

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks —
        KnowledgeGovernanceService owns the batched read (Task 16; the single
        `id IN (...)` knowledge_objects lookup is unchanged). Frozen-signature
        delegate."""
        return self._runtime.knowledge_governance.list_promotion_queue(status_filter)

    def approve_promotion_as_reviewer(
        self, candidate_id: str, reviewer_id: str
    ) -> dict:
        """Authenticated adapter; preserves the frozen legacy facade signature."""
        return self._runtime.knowledge_governance.approve_promotion(
            candidate_id, reviewer_id=reviewer_id
        )

    def reject_promotion_as_reviewer(
        self, candidate_id: str, reason: str, reviewer_id: str
    ) -> dict:
        """Authenticated adapter; preserves the frozen legacy facade signature."""
        return self._runtime.knowledge_governance.reject_promotion(
            candidate_id, reason, reviewer_id=reviewer_id
        )

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate,
        *, actor_id: Optional[str] = None,
    ) -> RuleCard:
        """Update a knowledge object — KnowledgeGovernanceService owns the
        orchestration (Task 16; the frozen commit→best-effort-embed→invalidate
        →dirty order is unchanged). Frozen-signature delegate; ``actor_id``
        (M1) is additive."""
        return self._runtime.knowledge_governance.update_knowledge(
            notebook_id, knowledge_id, payload, actor_id=actor_id
        )

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]:
        """Near-duplicate detection by normalized-seed blocking —
        KnowledgeGovernanceService owns the orchestration (Task 16; the
        O(N + Σ block²) seed blocking and ≥0.6 grouping are unchanged).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.find_duplicates(
            notebook_id, object_type
        )

    def merge_knowledge(
        self, notebook_id: str, source_id: str, payload: MergeRequest,
        *, actor_id: Optional[str] = None,
    ) -> RuleCard:
        """Merge one knowledge object into another —
        KnowledgeGovernanceService owns the orchestration (Task 16; the frozen
        commit→dirty→invalidate mirror-image order is unchanged).
        Frozen-signature delegate; ``actor_id`` (M1) is additive."""
        return self._runtime.knowledge_governance.merge_knowledge(
            notebook_id, source_id, payload, actor_id=actor_id
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

    def _vector_matrix(self, db: object, notebook_id: str,
                       table: str, id_col: str):
        return self.retrieval.candidates._vector_matrix(db, notebook_id, table, id_col)

    def _federated_rx_graph(self, active_notebook_id: str):
        return self.retrieval.graph._federated_rx_graph(active_notebook_id)

    def _federated_graph_is_large(self, active_notebook_id: str) -> bool:
        return self.retrieval.candidates._federated_graph_is_large(active_notebook_id)

    def _mention_extra_edges(self, notebook_id: str) -> List[Tuple[str, str, float]]:
        return self.retrieval.graph._mention_extra_edges(notebook_id)

    # ── scale index (offline build) ──────────────────────────────────────────

    def _scale_index_version(self, notebook_id: str) -> list:
        """Compatibility delegate to the runtime-owned version memo."""
        return self._runtime.scale_artifacts.version(notebook_id)

    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        """Return a valid ScaleIndex or None.  Task 18:exact/allow_stale 语义
        (磁盘身份缓存复用 + per-nb 单飞 cold-load)整体移入
        ScaleArtifactCatalog.load —— catalog 只读、不含 builder,读取永不触发
        重建(base 离线 ANN / active 暴力的成本分离不变量)。"""
        return self._runtime.scale_artifacts.load(
            notebook_id, allow_stale=allow_stale
        )

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
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.scale_artifacts.build(
            notebook_id, on_stage=on_stage
        )

    def fold_scale_index_delta(
        self, notebook_id: str, _assume_locked: bool = False
    ) -> dict:
        """Compatibility delegate to the runtime-owned delta folder."""
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.scale_artifacts.fold(
            notebook_id, assume_locked=_assume_locked
        )

    def _index_delta(self, notebook_id: str) -> dict:
        """Compatibility delegate to the runtime-owned scale builder."""
        return self._runtime.scale_builder._index_delta(notebook_id)

    def scale_index_status(self, notebook_id: str) -> dict:
        """Compatibility delegate to runtime scale status."""
        return self._runtime.scale_artifacts.status(notebook_id)

    def index_status(self, notebook_id: str) -> dict:
        """Compatibility delegate to the runtime's read-only combined status."""
        return self._runtime.scale_artifacts.index_status(notebook_id)

    def _notebook_name(self, notebook_id: str) -> str:
        """Compatibility delegate to runtime notification naming."""
        return self._runtime.scale_artifacts._notebook_name(notebook_id)

    def _notify_index_done(self, notebook_id: str) -> None:
        """Compatibility delegate to runtime index-completion notification."""
        return self._runtime.scale_artifacts.notify_index_done(notebook_id)

    def trigger_scale_index_rebuild(self, notebook_id: str, when: str = "now",
                                    mode: str = "auto") -> dict:
        """Compatibility delegate to runtime rebuild scheduling.

        Both current consumers (the HTTP rebuild endpoint and the MCP build
        tool) are explicit, deliberate one-off requests — never an internal
        automatic retry — so this is unconditionally a ``manual`` trigger and
        exempt from the Z5 failure backoff (see
        ``ScaleArtifactRuntime.trigger``).
        """
        self.require_indexing_pipeline_write(notebook_id)
        return self._runtime.scale_artifacts.trigger(
            notebook_id, when=when, mode=mode, manual=True
        )

    def cancel_scale_index(self, notebook_id: str) -> dict:
        """Compatibility delegate to runtime cancellation."""
        return self._runtime.scale_artifacts.cancel(notebook_id)

    def maybe_auto_index(self, notebook_id: str) -> None:
        """Compatibility delegate to runtime automatic indexing."""
        return self._runtime.scale_artifacts.maybe_auto_index(notebook_id)

    def _ppr_retrieve(self, notebook_id: str, question: str) -> List["RetrievedChunk"]:
        return self.retrieval.graph.ppr_retrieve(notebook_id, question)

    _PPR_RERANK_SCHEMA = '{"relevant_ids": [""]}'

    def _rule_card(self, item: RetrievedKnowledge) -> RuleCard:
        return self._runtime.knowledge_query.rule_card(item)

    @staticmethod
    def _as_retrieved(obj: dict, object_type: str) -> RetrievedKnowledge:
        return KnowledgeQueryService.as_retrieved(obj, object_type)

    # SQLite default SQLITE_MAX_VARIABLE_NUMBER-safe chunk size for `IN (...)`
    # placeholder lists (mirrors the ~900 convention used elsewhere in this file).
    _IN_CHUNK = 900

    def _in_batches(self, ids):
        """把 id 列表切成 ≤_IN_CHUNK 的批(去重保序)。所有把 id 列表内联成
        SQL IN 占位符的 delta 位点必须经它——SQLite 3.32+ 变量上限 32,766,
        生产 48,739 delta source 已真实打爆(too many SQL variables)。"""
        return self.retrieval.in_batches(ids, self._IN_CHUNK)

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

    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        return self.retrieval.candidates._retrieve_chunks_multi(notebook_id, sub_queries)

    def ask(
        self,
        notebook_id: str,
        payload: AskRequest,
        *,
        submitted_via: StoredSubmittedVia = "",
        output: AskOutput = "answer",
    ) -> AskResponse | AskEvidence:
        """Dispatch to the retrieval handler named by payload.mode, resolved
        through the ask_modes registry. Unknown modes raise UnknownAskMode (the
        API layer returns 422) — never a silent fall-through to the legacy
        path. The blocking surface uses the same internal durable-job and
        atomic-final-save lifecycle as the streaming coordinator; its job id
        is not added to the AskResponse protocol. ``output="evidence"``
        (built-in modes only) stops before synthesis and returns the
        ``AskEvidence`` the answer would have been written from, saving no
        conversation and no answer."""
        return self._runtime.ask_component.ask_current(
            notebook_id, payload, submitted_via=submitted_via, output=output
        )

    def ask_with_job(
        self, notebook_id: str, payload: AskRequest, *,
        submitted_via: StoredSubmittedVia = "", attach_only: bool = False,
        stop: Any = None, output: AskOutput = "answer",
    ) -> "tuple[AskResponse | AskEvidence | None, str] | None":
        """``ask`` plus its durable job id, honouring ``client_request_id``
        (attach to the job the key already created; ``stop`` abandons a pure
        wait). The MCP ``ask`` tool's seat; the HTTP protocol is unchanged.
        ``output="evidence"`` is ``ask``'s retrieval-only output (never
        keyed). See ``AskService.ask_current_with_job``."""
        return self._runtime.ask_component.ask_current_with_job(
            notebook_id, payload, submitted_via=submitted_via,
            attach_only=attach_only, stop=stop, output=output,
        )

    def preview_reasoning_intent(
        self, notebook_id: str, question: str, history: str = "",
        cancel_event: CancelEvent = None, *,
        source_scope: Any = None, base_scope: Any = None,
        user_id: str | None = None, report_id: str | None = None,
    ) -> QueryIntentContract:
        """Corpus-blind preflight used before a reasoning Ask creates a job,
        under the same default ceiling the ask will install; ``report_id`` is
        the deep report the request attaches (its outline reaches the
        understanding)."""
        return self._runtime.ask_component.preview_reasoning_intent(
            notebook_id, question, history, cancel_event,
            source_scope=source_scope, base_scope=base_scope, user_id=user_id,
            report_id=report_id,
        )

    def validate_reasoning_submission(
        self, notebook_id: str, payload: AskRequest
    ) -> None:
        """Preflight a selected scope before API code creates a durable Ask."""
        return self._runtime.ask_component.validate_reasoning_submission(
            notebook_id, payload
        )

    def resolve_reasoning_followup(
        self, notebook_id: str, payload: AskRequest
    ) -> FollowupResolution:
        """PR-C: resolve an elliptical follow-up (or decide it cannot be)
        before API code creates a durable Ask. Clear questions and requests
        that already carry a reviewed intent cost nothing here."""
        return self._runtime.ask_component.resolve_reasoning_followup(
            notebook_id, payload
        )

    # ask_fast (legacy KG-native, P4-5退役)、_ask_global (GraphRAG map-reduce, P4-5退役)
    # 与全图 BFS + PPR 的 graph 问答引擎(本轮退役)已删除。旧会话/书签中的
    # mode="fast"/"global"/"graph" 通过 ask_modes._RETIRED_MODES 映射到
    # "chunk"，不会触发 422。
    # 随该 graph 引擎一并删除的还有：它专属的检索端口协议、大库判定与图种子融合
    # 两个端口方法、AskService 构造参数里只服务于它的图端口与社区报告座位；
    # kg/graph_reason.py 的答案期链路验证函数与 _federated_rx_graph/
    # _kg_source_chunks 等底层基础设施保留(follow_chain/chunk mix 分支仍消费)。
    # 随之删除的还有：is_process_query import（原 ask_fast 独占调用）。
    # 旧 LLM 打分重排 helper 已删——被 qwen3-rerank(RerankClient)取代。
    # _answer_kg 已删(P4-5死码)，query-refine 移入 _refine_context。

    def _kg_source_chunks(
        self, notebook_id: str, object_ids: list, *, support_by_object=None
    ) -> list:
        return self.retrieval.graph._kg_source_chunks(
            notebook_id, object_ids, support_by_object=support_by_object
        )

    def _ent_chunk_map(
        self, notebook_id: str, *, paged: bool = False
    ) -> Dict[str, set]:
        return self.retrieval.graph._ent_chunk_map(notebook_id, paged=paged)

    # ── chunk×graph mix ──────────────────────────────────────────────────────

    _MIX_KG_KEY_BASE = 1000
    _MIX_PROMPT_BUFFER_TOKENS = 2000

    def participant_notebook_ids(
        self, notebook_id: str, *, viewer_id: str | None
    ) -> List[str]:
        """联邦参与库:active 在首位 + 全部 base tier(与 _ppr_graph/federated_retrieve
        的内联谓词一致;此 helper v1 只供新代码使用,存量调用点不迁移)。

        公开(v1 落地时叫 `_participant_notebook_ids`,零调用点)是因为它现在有了
        facade **外部**的消费方:MCP 的 `read_reference` 拿注入的 repository 去做
        「引用点查」的参与集校验——deps 里的 `notebook_store_port()` 是浏览器侧
        (FastAPI 依赖)的取数口,MCP 不经过它。唯一定义点仍是 mount_sql.py。

        ``viewer_id`` 是必填关键字(M3:挂载只对挂载人或自己能读被挂库的人生效):
        调用方必须说出「替谁取」——MCP 传令牌所属用户,公开页传分享创建者。"""
        return self._runtime.notebook_store.participant_notebook_ids(
            notebook_id, viewer_id=viewer_id
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

    def append_ask_trace(self, job_id: str, step: dict) -> None:
        """把一个 trace step 追加进 ask_trace_steps 子表(append-only,O(1) 单行
        INSERT;取号+插入同事务,细节在 AskStateStore.append_trace)。

        本 facade 协调层保有既有契约:**fail-open**——raw store 上抛的任何持久化
        失败在此记日志吞掉,轨迹持久化失败绝不拖垮 ask(error_policies.json:
        append_ask_trace)。notebook_id/user_id 是 port 签名参数,store SQL 只按
        job_id 落行;基线本就不解析用户,这里传占位保持零额外开销(Task 24 的
        引擎调用会带真实值)。"""
        return self._runtime.ask_component.append_trace_fail_open(job_id, step)

    def ask_job_origin(self, job_id: str) -> "dict | None":
        """Creator, notebook, submission surface, question, mode,
        conversation and Memory-channel flag of one Ask job (``None`` if
        absent): what the MCP ``get_ask`` checks before reading a job."""
        return self._runtime.ask_state.ask_job_origin(job_id)

    def ask_job_detail(self, job_id: str) -> dict:
        """Return a live ask job only; retained summaries are admin-guarded."""
        return self._runtime.ask_state.ask_job_detail(job_id)

    def ask_answer_detail(self, answer_id: str) -> "dict | None":
        """按 answer_id 直查单条答案(一条主键查询,不加载整个会话)。见
        ``AskStateStore.ask_answer_detail`` docstring。"""
        return self._runtime.ask_state.ask_answer_detail(answer_id)

    def guarded_ask_detail(
        self, job_id: str, *, actor_id: str, reader_id: "str | None"
    ):
        """Keep the notebook deletion fence through API response assembly."""
        return self._runtime.ask_state.guarded_ask_detail(
            job_id, actor_id=actor_id, reader_id=reader_id
        )

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
    def create_report(
        self,
        notebook_id: str,
        question: str,
        depth: int = 2,
        *,
        submitted_via: StoredSubmittedVia = "",
    ) -> str:
        return self._runtime.report_application.create_report(
            notebook_id, question, depth, submitted_via=submitted_via
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

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        return self._runtime.report_store.get_report(notebook_id, report_id)

    def guarded_report_detail(
        self, report_id: str, *, actor_id: str, reader_id: "str | None"
    ):
        """Keep the notebook deletion fence through API response assembly."""
        return self._runtime.report_store.guarded_report_detail(
            report_id, actor_id=actor_id, reader_id=reader_id
        )

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

    def share_report(
        self, notebook_id: str, report_id: str, *, memory_guard=None
    ) -> str:
        return self._runtime.report_store.share_report(
            notebook_id, report_id, memory_guard=memory_guard
        )

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

    def _source_type_from_name(self, file_name: str) -> str:
        return self._runtime.source_ingestion.source_type(file_name)

    def _summarize_source(self, title: str, elements: List[SourceElement]) -> str:
        return self._runtime.source_ingestion.summarize(title, elements)

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
                         *, user_id: str, attach_only=False,
                         submitted_via: StoredSubmittedVia = ""):
        """Start detached Ask execution through the runtime-owned coordinator.

        ``attach_only`` (a keyed re-submission probe): only attach to the job
        ``payload.client_request_id`` already created, returning ``None`` when
        there is none — the route then runs its normal preflight and start."""
        if attach_only:
            return self._runtime.ask_execution.attach_existing(
                notebook_id, payload, user_id
            )
        return self._runtime.ask_execution.start(
            notebook_id, payload, mode, user_id=user_id, submitted_via=submitted_via
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

    def delete_source_asset_rows(self, source_id: str) -> List[dict]:
        return self._runtime.knowhow_store.delete_source_asset_rows(source_id)

    # ---------------------------------------------------- paper metadata
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

    def prune_knowhow_history(self, table_id: str, before_iso: str) -> dict:
        return self._runtime.knowhow_history_store.prune(table_id, before_iso)

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
        self.require_indexing_pipeline_write(notebook_id)
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
