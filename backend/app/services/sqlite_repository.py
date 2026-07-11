from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

from app.core.config import Settings
from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS
from app.core.llm import OpenAICompatibleClient, cap_kwargs
from app.models.schemas import (
    AddUrlSourcesResult,
    AnswerAnchor,
    AskRequest,
    AskResponse,
    Citation,
    DuplicateGroup,
    Evidence,
    FeedbackRequest,
    FeedbackResponse,
    KnowledgeEdge,
    KnowledgeFieldValue,
    KnowledgeGraph,
    KnowledgeNode,
    KnowledgeRecord,
    KnowledgeRef,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    ModelError,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    MergeRequest,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    PaginatedKnowledge,
    PaginatedSources,
    RejectedUrl,
    RuleCard,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UserProfile,
)
from app.services import kg_ingest
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.vector_cache import LRUProcessCache
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
from app.services.model_config import ResolvedModelConfig, ModelNotConfiguredError
from app.services.notebook_catalog import NotebookSummaryQuery
from app.services.sqlite_identity import (
    _REQUEST_USER,
    get_request_user,
    request_user_id,
    reset_request_user,
    set_request_user,
)
from app.services.repository_runtime import RepositoryRuntime, RepositoryCompatibilitySeams
from app.services.source_ingestion import SourcePipelineHooks
from app.repositories.source_files import safe_filename as _safe_filename  # noqa: F401 — compatibility export
from app.repositories.sqlite.migrations import SqliteMigrator, SCHEMA_VERSION
from app.repositories.sqlite.source_store import SourceElementWrite
from app.services.sqlite_notebook_sharing import (  # noqa: F401 — compatibility exports
    SQLiteNotebookSharingMixin,  # Task 9: no longer in the facade MRO; kept importable
    _remap_json_ids,
)
from app.services.parsers import parse_source_file, mineru_content_list_to_elements
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
from app.services.retrieval import (
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
        raise RuntimeError("SQLiteRepository is no longer available")
    return repository


def _new_id(prefix: str) -> str:
    """Collision-proof surrogate row id: prefix + full 128-bit uuid hex.
    (Historically these used only the first 10 hex chars of uuid4().hex = 40
    bits, which birthday-collides on tables with one row per item at
    multi-million-row scale — concept_clusters on a giant KG hit
    `UNIQUE constraint failed: concept_clusters.id`.)"""
    return f"{prefix}-{uuid4().hex}"


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


# Matches both one provenance marker and the comma-group form models commonly
# emit (`[k1, k3]`). A group binds only when every key exists in id_map.
_MARKER_GROUP_RE = re.compile(r"\[((?:k\d+\s*,\s*)*k\d+)\]")

# Tolerant variant that ALSO matches malformed markers with internal whitespace
# (e.g. `[ k1]`). Used only to scrub citation-shaped tokens that did NOT bind to
# a real anchor, so no fabricated/malformed marker reaches the user. Kept
# separate from _MARKER_GROUP_RE so strict anchor resolution is unchanged.
_LOOSE_MARKER_GROUP_RE = re.compile(r"\[\s*k\d+(?:\s*,\s*k\d+)*\s*\]")


def _strip_unbound_markers(answer: str, bound_keys: set) -> str:
    """Normalise the `[k…]`-shaped tokens in `answer` against `bound_keys` (the
    keys that actually resolved to an anchor):
      - key in bound_keys  → rewrite to the canonical `[key]` form (repairs a
        malformed spaced `[ k1]` so it reads as a clean citation, not a fabricated
        one, while still pointing at its real anchor);
      - key not in bound_keys → drop the token (out-of-map ids like `[k99]`, or a
        spaced id with no anchor).
    Collapses the double space a removed mid-sentence marker would leave behind."""
    def _sub(m: re.Match) -> str:
        keys = [part.strip() for part in m.group(0).strip("[]").split(",")]
        # Mixed known/unknown groups fail closed. Keeping only the known subset
        # would silently alter which premises the sentence claims to cite.
        return ("[" + ", ".join(keys) + "]"
                if keys and all(key in bound_keys for key in keys) else "")
    cleaned = _LOOSE_MARKER_GROUP_RE.sub(_sub, answer or "")
    # A stripped marker between words leaves "word  word"; normalise to one space
    # without disturbing newlines / other whitespace runs the model intended.
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


# copy_notebook's per-table chunk size (perf-audit P1-4): mirrors store_kg's
# CHUNK=1000 local constant, but module-level so tests can shrink it to force
# multiple chunk-boundary transactions without seeding thousands of rows.
_COPY_CHUNK = 1000


# Schema 版本号：每次改动表结构 → 追加一个 _migration_N 方法并把此常量 +1。
# 值 = 已定义的迁移步骤总数（步骤 1 = 全量基线 schema，历来就幂等）。
SCHEMA_VERSION = 10


@dataclass(frozen=True)
class ChunkRetrievalPlan:
    """ask_chunk 编排层的检索决策一次性只读快照（W2.2）。

    由 `_build_chunk_retrieval_plan` 一次读齐 self.settings + KG 存在探测 + rerank 配置
    产出，供 ask_chunk 从「就地内联算 overlay_on / 三分支 / 就地读 self.settings.X」改为
    「读 plan.X」——不改控制流形状，只把散落的**编排决策**收到一处、一次。

    **只收编排层的「决策」**：strategy 由 overlay_on ∧ 子查询数算出；mmr/fuse knob。
    刻意**不收**候选生成层的全局直读 flag（chunk_ann_enabled / chunk_bruteforce_max_chunks /
    scale_search_include_delta / graph_ppr_enabled）——它们在所有上下文读同一个 settings 值、
    非 per-query 决策，且 _retrieve_chunks 有多个非 ask_chunk 调用者（无此 plan），穿进去只会
    造成同一 flag 两种读法长期并存，收益边际而风险落在有生产假死史的候选级联上。
    也不收共享 flag（rrf / canonical_fold / relation_retrieval / top_n）——它们在共享
    _retrieve_scored / graph 路径，收进来即改变 reasoning/graph 语义。
    """
    strategy: str            # "mix" | "multi" | "single"（复刻 overlay_on / 子查询数三分支）
    overlay_on: bool         # chunk_kg_overlay_enabled ∧ rerank.configured ∧ (has_kg ∨ base_has_kg)
    mmr_k: int               # chunk_mmr_k（single 分支 MMR）
    mmr_lambda: float        # chunk_mmr_lambda（single 分支 MMR）
    fuse_k: int              # == chunk_mmr_k（复刻 multi 分支 quota_fuse 复用同一 knob）


class SQLiteRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root_dir = Path(__file__).resolve().parents[3]
        self._runtime = RepositoryRuntime(
            settings=self.settings,
            root_dir=self.root_dir,
            seams=RepositoryCompatibilitySeams(
                new_id=lambda prefix: _new_id(prefix),
                now=lambda: _now(),
                copy_chunk_size=lambda: _COPY_CHUNK,
                remap_json_ids=lambda value, maps: _remap_json_ids(value, maps),
            ),
        )
        self.storage_dir = self._resolve_path(settings.storage_dir)
        # Task 9: finish the sharing/deep-copy composition with facade-bound
        # late seams — the _insert_row seat and the memoized copy-stats keep
        # honouring per-instance monkeypatches, storage_dir stays live.
        self._runtime.wire_sharing(
            insert_row=lambda db, table, data: self._insert_row(db, table, data),
            copy_stats=lambda notebook_id: self.notebook_copy_stats(notebook_id),
            storage_dir=lambda: self.storage_dir,
        )
        # Task 10: vector flushes stay on the facade's `_write` seat (resolved
        # per call) so transaction-counting/failure-injection monkeypatches
        # keep observing them; the seat itself is the shared database lock.
        self._runtime.wire_persistence(write=lambda: self._write())
        # Task 11: the embed/chunk pipeline rides three facade-bound late
        # seams — the mutable embedder attribute (tests swap fakes in
        # post-construction), the _flush_object_vectors MASTER_V10 seat
        # (incremental object-vector commits stay per-instance patchable) and
        # the _mark_unified_kg_dirty KG seat (stays facade-owned until Gate 5).
        self._runtime.wire_source_pipeline(
            embedder=lambda: self.embedder,
            flush_object_vectors=lambda notebook_id, rows: self._flush_object_vectors(
                notebook_id, rows
            ),
            mark_unified_dirty=lambda notebook_id: self._mark_unified_kg_dirty(
                notebook_id
            ),
        )
        from app.services.embedding import make_embedder
        self.embedder = make_embedder(self.settings)
        self.mineru_client = MinerUClient(settings)
        self.mineru_cloud_client = MinerUCloudClient(settings)
        self.event_log = self._runtime.event_log
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Task 17: the unified-graph dict and the version-keyed VectorCache
        # are runtime-owned (RetrievalSnapshotCache constructs them eagerly);
        # `_unified_cache` / `_vector_cache` below are write-through property
        # descriptors over those SAME objects — no facade-only copies.
        self._user_model_cfg_cache = self._runtime.identity.model_config_cache
        # Bilingual-query hint: per-notebook detected corpus languages (subset of
        # ["zh","en"]), sampled cheaply and memoized. Staleness is harmless — a
        # wrong guess only over/under-generates FTS keywords, never breaks recall.
        self._notebook_langs_cache: Dict[str, List[str]] = {}
        # C7: bounded LRU (was an unbounded plain dict — every notebook ever
        # touched stayed resident for the life of the process; each entry is
        # numpy arrays + a memoized hnsw handle, tens-of-MB to GB). Eviction
        # just drops the reference — GC frees the arrays, and hnswlib.Index
        # has no explicit close() to call (see LRUProcessCache docstring).
        scale_idx_cache = LRUProcessCache(max_entries=self.settings.scale_idx_cache_max)
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
        repository_ref = weakref.ref(self)
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
            note_model_error=lambda stage, model, exc: (
                _repository_from_weakref(repository_ref)._note_model_error(
                    stage, model, exc
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
        scale_idle_queue: dict = {}   # {notebook_id: mode} 待低峰重建
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
        # `_mark_unified_kg_dirty` / `_bump_cluster_mutation_seq` wrappers
        # below delegate to it — every mutation call site keeps funnelling
        # through those wrappers, so the frozen phase matrix is unchanged.
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
            get_notebook=lambda notebook_id: self.get_notebook(notebook_id),
            invalidate_unified_cache=lambda notebook_id: (
                self._invalidate_unified_cache(notebook_id)
            ),
            mark_unified_kg_dirty=lambda notebook_id: (
                self._mark_unified_kg_dirty(notebook_id)
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
            set_source_status=lambda source_id, status: (
                self._set_source_status(source_id, status)
            ),
            run_extraction=lambda source_id: self._run_extraction(source_id),
            llm=lambda: self.llm_client,
            kg_llm=lambda: self.kg_llm_client,
            cluster_map=lambda notebook_id: self.cluster_map(notebook_id),
            annotate_edge_support=lambda notebook_id, edges: (
                self._annotate_edge_support(notebook_id, edges)
            ),
            decided_seed_pairs=lambda notebook_id: self.decided_seed_pairs(notebook_id),
            relations_for_notebook=lambda notebook_id: (
                self.relations_for_notebook(notebook_id)
            ),
            notebook_copy_stats=lambda notebook_id: self.notebook_copy_stats(notebook_id),
            note_model_error=lambda stage, model, exc: (
                self._note_model_error(stage, model, exc)
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
        # — the `_write` transaction seat, the parse/summarize/model seams
        # whose frozen patch targets live on this facade or its module
        # namespace (repo.source_elements / repo._summarize_source / module
        # parse_source_file / per-user llm & kg_llm properties) — plus DIRECT
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
            parse_file=lambda source_id, file_path, file_name, client: (
                parse_source_file(source_id, file_path, file_name, client)
            ),
            mineru_client=lambda: self.mineru_client,
            mineru_cloud_client=lambda: self.mineru_cloud_client,
            llm=lambda: self.llm_client,
            kg_llm=lambda: self.kg_llm_client,
            normalize_doc_type=_normalize_doc_type,
            default_notebook_names=_DEFAULT_NOTEBOOK_NAMES,
            clear_source_extraction_state=(
                lambda db, source_id, notebook_id, clear_embeddings: (
                    self._clear_source_extraction_state(
                        db, source_id, notebook_id, clear_embeddings=clear_embeddings
                    )
                )
            ),
            begin_extraction_run=lambda source_id, notebook_id, run_id, created_at: (
                self._begin_extraction_run(source_id, notebook_id, run_id, created_at)
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
        )
        # WS2a: 在途 ask 的 {job_id: cancel_event} 进程内注册表——cancel 端点据此
        # set 对应 worker 的 cancel_event（唯一真取消入口；断连不再取消）。
        self._ask_cancel_events: dict = {}
        self._ask_cancel_lock = threading.Lock()
        self._migrator = SqliteMigrator(self._runtime.database, self.settings)
        self._migrator.initialize()

    def current_user(self) -> UserProfile:
        return self._runtime.identity.current_user()

    def _user_profile(self, user, profile) -> UserProfile:
        return self._runtime.identity._user_profile(user, profile)

    def get_user_model_settings(self, user_id: str) -> dict:
        return self._runtime.identity.get_user_model_settings(user_id)

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        return self._runtime.identity.set_user_model_settings(user_id, settings)

    def resolve_model_config(self, user, role: str) -> ResolvedModelConfig:
        return self._runtime.identity.resolve_model_config(user, role)

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

    def list_user_usage(self) -> List[Dict[str, Any]]:
        return self._runtime.queries.list_user_usage()

    def list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]:
        return self._runtime.queries.list_user_notebooks(user_id)

    def load_notebook_scale_facts(self, notebook_id: str):
        return self._runtime.queries.load_notebook_scale_facts(notebook_id)

    def pending_actions_projection_rows(self, user_id: str) -> dict:
        return self._runtime.queries.pending_actions_projection_rows(user_id)

    def _system_llm_for(self, role: str):
        return self._runtime.models._system_llm_for(role)

    def _user_llm_cached(self, cfg: ResolvedModelConfig):
        return self._runtime.models._user_llm_cached(cfg)

    def _llm_for_role(self, role: str):
        return self._runtime.models._llm_for_role(role)

    @property
    def llm_client(self):
        return self._runtime.models.llm_client

    @llm_client.setter
    def llm_client(self, client):
        self._runtime.models.llm_client = client

    @property
    def reasoning_llm_client(self):
        return self._runtime.models.reasoning_llm_client

    @property
    def rewrite_llm_client(self):
        return self._runtime.models.rewrite_llm_client

    @property
    def kg_llm_client(self):
        return self._runtime.models.kg_llm_client

    @property
    def rerank_client(self):
        return self._runtime.models.rerank_client

    @rerank_client.setter
    def rerank_client(self, client):
        self._runtime.models.rerank_client = client

    @property
    def _system_llm_client(self):
        return self._runtime.models._system_llm_client

    @_system_llm_client.setter
    def _system_llm_client(self, client):
        self._runtime.models._system_llm_client = client

    @property
    def _reasoning_llm_client(self):
        return self._runtime.models._reasoning_llm_client

    @_reasoning_llm_client.setter
    def _reasoning_llm_client(self, client):
        self._runtime.models._reasoning_llm_client = client

    @property
    def _rewrite_llm_client(self):
        return self._runtime.models._rewrite_llm_client

    @_rewrite_llm_client.setter
    def _rewrite_llm_client(self, client):
        self._runtime.models._rewrite_llm_client = client

    @property
    def _kg_llm_client(self):
        return self._runtime.models._kg_llm_client

    @_kg_llm_client.setter
    def _kg_llm_client(self, client):
        self._runtime.models._kg_llm_client = client

    @property
    def _system_rerank_client(self):
        return self._runtime.models._system_rerank_client

    @_system_rerank_client.setter
    def _system_rerank_client(self, client):
        self._runtime.models._system_rerank_client = client

    @property
    def _user_model_cfg_cache(self):
        return self._runtime.identity.model_config_cache

    @_user_model_cfg_cache.setter
    def _user_model_cfg_cache(self, cache):
        self._runtime.model_config_cache = cache
        self._runtime.identity.model_config_cache = cache
        self._runtime.models.model_config_cache = cache

    @property
    def _user_llm_clients(self):
        return self._runtime.models._user_llm_clients

    @_user_llm_clients.setter
    def _user_llm_clients(self, clients):
        self._runtime.models._user_llm_clients = clients

    @property
    def _user_rerank_clients(self):
        return self._runtime.models._user_rerank_clients

    @_user_rerank_clients.setter
    def _user_rerank_clients(self, clients):
        self._runtime.models._user_rerank_clients = clients

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
        self._runtime.retrieval_snapshots.unified_cache = cache
        if self._runtime.knowledge_lifecycle is not None:
            self._runtime.knowledge_lifecycle.unified_cache = cache

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
        self._runtime.scale_artifacts.building = value
        self._runtime.scale_builder.building = value

    @property
    def _scale_building_lock(self):
        return self._runtime.scale_artifacts.building_lock

    @_scale_building_lock.setter
    def _scale_building_lock(self, value) -> None:
        self._runtime.scale_artifacts.building_lock = value
        self._runtime.scale_builder.building_lock = value

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
        self._runtime.scale_artifacts.scheduler_started = bool(value)

    @property
    def _auto_index_checked(self):
        return self._runtime.scale_artifacts.auto_index_checked

    @_auto_index_checked.setter
    def _auto_index_checked(self, value) -> None:
        self._runtime.scale_artifacts.auto_index_checked = value
        if self._runtime.kg_mutations is not None:
            self._runtime.kg_mutations.auto_index_checked = value

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

    @property
    def db_path(self) -> Path:
        return self._runtime.database.db_path

    @db_path.setter
    def db_path(self, value: Path) -> None:
        self._runtime.database.db_path = value

    @property
    def _write_lock(self):
        return self._runtime.database.write_lock

    @_write_lock.setter
    def _write_lock(self, value) -> None:
        self._runtime.database.write_lock = value

    def _connect(self) -> sqlite3.Connection:
        return self._runtime.database.connect()

    @contextmanager
    def _write(self):
        """串行化写事务：进程内同一时刻只有一个写者进 SQLite，并发写线程在
        Python 层排队而非裸抢 SQLite 写锁（后者即 `database is locked` 的根因）。
        纯读保持用 _connect()，不受影响（WAL 支持并发读）。"""
        with self._runtime.database.write() as db:
            yield db

    def _migrate(self) -> list[int]:
        return self._migrator.migrate()

    def _migrate_legacy(self) -> list[int]:
        return self._migrator.migrate()

    @staticmethod
    def _add_column_if_missing(db, table: str, column: str, coldef: str) -> None:
        return SqliteMigrator.add_column_if_missing(db, table, column, coldef)

    def _migration_1(self) -> None:
        return self._migrator._migration_1()
    def _migration_2(self) -> None:
        return self._migrator._migration_2()
    def _migration_3(self) -> None:
        return self._migrator._migration_3()
    def _migration_4(self) -> None:
        return self._migrator._migration_4()
    def _migration_5(self) -> None:
        return self._migrator._migration_5()
    def _migration_6(self) -> None:
        return self._migrator._migration_6()
    def _migration_7(self) -> None:
        return self._migrator._migration_7()
    def _migration_8(self) -> None:
        return self._migrator._migration_8()
    def _migration_9(self) -> None:
        return self._migrator._migration_9()

    def _migration_10(self) -> None:
        return self._migrator._migration_10()
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
        self._runtime.unified_kg.checkpoint_put(
            notebook_id, input_version, stage, rows, _now()
        )

    def _recover_interrupted_jobs(self) -> None:
        return self._migrator.recover_interrupted_jobs()

    def _recover_interrupted_jobs_legacy(self) -> None:
        return self._migrator.recover_interrupted_jobs()

    def _seed(self) -> None:
        return self._migrator.seed()

    def _seed_legacy(self) -> None:
        return self._migrator.seed()
    def _count(self, db: sqlite3.Connection, table: str, column: str, value: str) -> int:
        return self._runtime.notebook_summaries.count(db, table, column, value)

    def _count_knowledge(self, db: sqlite3.Connection, notebook_id: str, object_type: str) -> int:
        return self._runtime.knowledge.count_knowledge(
            db, notebook_id, object_type, USABLE_STATUSES
        )

    def _has_kg(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        return self._runtime.notebook_summaries.has_kg(db, notebook_id)

    def _source_has_kg(self, db: sqlite3.Connection, source_id: str) -> bool:
        """True iff the source has ≥1 knowledge_objects row with a matching source_id."""
        row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE source_id = ? AND source_id != '')",
            (source_id,),
        ).fetchone()
        return bool(row[0])

    def _count_pending_kg_sources(self, db: sqlite3.Connection, notebook_id: str) -> int:
        return self._runtime.notebook_summaries.count_pending_kg_sources(db, notebook_id)

    def _any_base_notebook_has_kg(self, db: "sqlite3.Connection | None" = None) -> bool:
        """True iff some tier='base' notebook has any knowledge_objects."""
        sql = ("SELECT EXISTS(SELECT 1 FROM knowledge_objects ko "
               "JOIN notebooks nb ON nb.id = ko.notebook_id WHERE nb.tier = 'base')")
        if db is not None:
            return bool(db.execute(sql).fetchone()[0])
        with self._connect() as conn:
            return bool(conn.execute(sql).fetchone()[0])

    def _base_notebook_info(self, db: "sqlite3.Connection | None" = None) -> "tuple[str, bool]":
        return self._runtime.notebook_summaries.base_notebook_info(db)

    @staticmethod
    def _source_ids_from_evidence(evidence_json: Optional[str]) -> set:
        """PURE parse of an evidence JSON TEXT value into its distinct
        source_ids — canonical body lives on KnowledgeStore (Task 13)."""
        from app.repositories.sqlite.knowledge_store import KnowledgeStore
        return KnowledgeStore.source_ids_from_evidence(evidence_json)

    def _upsert_knowledge_object_sources(
        self, db: sqlite3.Connection, object_id: str, notebook_id: str, evidence_json: Optional[str]
    ) -> None:
        self._runtime.knowledge.replace_object_sources(
            db, object_id, notebook_id, evidence_json
        )

    @staticmethod
    def _delete_knowledge_object_sources(db: sqlite3.Connection, object_ids: List[str]) -> None:
        from app.repositories.sqlite.knowledge_store import KnowledgeStore
        KnowledgeStore.delete_object_sources(db, object_ids)

    def _source_index_backfilled(self, db: sqlite3.Connection, notebook_id: str) -> bool:
        return self._runtime.knowledge.source_index_backfilled(db, notebook_id)

    def _mark_source_index_backfilled(self, db: sqlite3.Connection, notebook_id: str) -> None:
        self._runtime.knowledge.mark_source_index_backfilled(db, notebook_id)

    def _find_stale_knowledge_ids_for_source(
        self, db: sqlite3.Connection, source_id: str, notebook_id: str
    ) -> List[str]:
        return self._runtime.knowledge.stale_object_ids_for_source(
            db, source_id, notebook_id
        )

    def _clear_source_extraction_state(
        self,
        db: sqlite3.Connection,
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
        db: sqlite3.Connection,
        notebook_id: str,
        object_type: str,
        statuses: Optional[Iterable[str]] = USABLE_STATUSES,
        id_filter: Optional[Iterable[str]] = None,
    ) -> List[dict]:
        base_query = (
            "SELECT * FROM knowledge_objects WHERE notebook_id = ? AND object_type = ?"
        )
        base_params: List[object] = [notebook_id, object_type]
        if statuses is not None:
            status_list = list(statuses)
            placeholders = ",".join("?" for _ in status_list)
            base_query += f" AND status IN ({placeholders})"
            base_params.extend(status_list)
        if id_filter is not None:
            # 候选集(id_filter,如 _retrieve_scored 的 cand_sims keys)可能超
            # _IN_CHUNK(delta 开且候选量大)——按 _in_batches 分批查询,批间用
            # (created_at,id) 重排合并,保持与单条 IN + ORDER BY 完全一致的输出序。
            id_list = list(id_filter)
            if not id_list:
                return []
            raw_rows = []
            for batch in self._in_batches(id_list):
                phid = ",".join("?" for _ in batch)
                raw_rows.extend(db.execute(
                    base_query + f" AND id IN ({phid})",
                    (*base_params, *batch)).fetchall())
            rows = sorted(raw_rows, key=lambda row: (row["created_at"], row["id"]))
        else:
            query = base_query + " ORDER BY created_at ASC, id ASC"
            rows = db.execute(query, base_params).fetchall()
        objects: List[dict] = []
        for row in rows:
            keys = row.keys()
            objects.append(
                {
                    "id": row["id"],
                    "payload": json.loads(row["payload"] or "{}"),
                    "evidence": [
                        Evidence(**item)
                        for item in json.loads(row["evidence"] or "[]")
                    ],
                    "status": row["status"],
                    "owner": row["owner"],
                    "last_reviewed": row["last_reviewed"] if "last_reviewed" in keys else "",
                }
            )
        return objects

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

    def delete_notebook(self, notebook_id: str) -> None:
        return self._runtime.catalog.delete_notebook(notebook_id)

    # ------------------------------------------------------------------
    # Sharing / membership / deep-copy domain (Task 9): composed
    # NotebookSharingService + NotebookCopyService + SharingStore delegates.
    # Frozen public signatures; the facade keeps notebook_copy_stats' own
    # scale-profile memo (cross-domain bridge) and the _insert_row seat.
    # ------------------------------------------------------------------
    def share_notebook(self, notebook_id: str) -> dict:
        return self._runtime.sharing.share_notebook(notebook_id)

    def unshare_notebook(self, notebook_id: str) -> None:
        return self._runtime.sharing.unshare_notebook(notebook_id)

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        return self._runtime.sharing.find_notebook_by_share_token(token)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return __import__("app.services.notebook_scale", fromlist=["NotebookScaleProfile"]).NotebookScaleProfile(self.settings, self, lambda nb: tuple(self._scale_index_version(nb)), self._vector_cache).copy_stats(notebook_id)

    def shared_preview(self, notebook_id: str) -> dict:
        return self._runtime.sharing.shared_preview(notebook_id)

    def shared_by_me(self, user_id: str) -> list:
        return self._runtime.sharing.shared_by_me(user_id)

    @staticmethod
    def _insert_row(db, table: str, data: dict) -> None:
        columns = list(data.keys())
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            [data[column] for column in columns],
        )

    def _sweep_stuck_copies(self, created_by: "str | None" = None) -> int:
        return self._runtime.sharing.sweep_stuck_copies(created_by)

    def copy_notebook(
        self,
        source_notebook_id: str,
        *,
        new_owner_id: str,
        new_name: "str | None" = None,
    ) -> NotebookSummary:
        return self._runtime.sharing.copy_notebook(
            source_notebook_id, new_owner_id=new_owner_id, new_name=new_name
        )

    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.user_can_access_notebook(notebook_id, user_id)

    def is_member(self, notebook_id: str, user_id: str) -> bool:
        return self._runtime.sharing.is_member(notebook_id, user_id)

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
        self.get_notebook(notebook_id)
        with self._write() as db:
            return self._runtime.knowledge.backfill_fts(db, notebook_id)

    def backfill_chunk_fts(self, notebook_id: str) -> int:
        """从 chunks 重建 chunks_fts(DELETE+re-INSERT)。返回写入行数。

        幂等,best-effort 派生索引:先删本 notebook 的 FTS 行,再从 chunks 全量重插。
        供 copy_notebook / 刷新图谱等重建派生索引的路径调用。
        """
        with self._write() as db:
            db.execute("DELETE FROM chunks_fts WHERE notebook_id=?", (notebook_id,))
            rows = db.execute(
                "SELECT id, text FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchall()
            if rows:
                db.executemany(
                    "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                    [(r["id"], notebook_id, r["text"] or "") for r in rows])
        # Chunk set was just (re)indexed — drop the corpus-language hint so it
        # re-samples (copy_notebook / refresh-graph can bring new-language content).
        self._notebook_langs_cache.pop(notebook_id, None)
        return len(rows)

    def _semantic_search(self, notebook_id: str, q: str, k: int) -> list:
        """ANN semantic search over the notebook's scale index.

        Returns [{object_id, name:'', score, match:'semantic'}] or []
        on any failure / missing index.
        """
        try:
            if not self.settings.embedder_configured:
                return []
            idx = self._scale_index(notebook_id)
            if idx is None or not idx.ann_labels:
                return []
            qvec = self._embed_query(q)
            if qvec is None:
                return []
            import numpy as np
            dim = int(idx.manifest.get("dim", len(qvec)))
            if dim != len(qvec):
                self.event_log.emit({
                    "kind": "dim_mismatch", "notebook_id": notebook_id, "site": "kg_semantic_search",
                    "manifest_dim": dim, "query_dim": len(qvec)})
                return []
            ann = self._open_scale_ann(idx, "kg")
            if ann is None:
                return []
            ann.set_ef(max(k + 1, 50))
            actual_k = min(k, len(idx.ann_labels))
            labels, distances = ann.knn_query(np.asarray(qvec, dtype=np.float32), k=actual_k)
            hits = []
            for lab, dist in zip(labels[0], distances[0]):
                node_id = idx.ann_labels[int(lab)]
                # Skip chunk nodes and cluster hub nodes (not KG objects)
                if node_id.startswith("cluster:") or not node_id.startswith("ko-"):
                    continue
                score = max(0.0, 1.0 - float(dist))
                if score > 0:
                    hits.append({"object_id": node_id, "name": "", "score": score,
                                 "match": "semantic"})
            return hits
        except Exception:  # noqa: BLE001 — fail-open
            return []

    def _hydrate_search_hits(self, notebook_id: str, hits: list) -> list:
        """Enrich merged hits with object_type and fill missing names.

        Drops hits for objects that no longer exist or are deprecated.
        Always uses the fresh payload name when available (Fix 2: overrides
        the possibly-stale FTS-provided name so renames are reflected).
        """
        if not hits:
            return []
        ids = [h["object_id"] for h in hits]
        with self._connect() as db:
            rows = self._runtime.knowledge.object_meta_rows(db, ids)
        meta: dict = {}
        for r in rows:
            if r["status"] == "deprecated":
                continue
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            meta[r["id"]] = {"object_type": r["object_type"], "name": payload.get("name", "")}
        result = []
        for h in hits:
            m = meta.get(h["object_id"])
            if m is None:
                continue  # object gone or deprecated
            enriched = dict(h)
            enriched["object_type"] = m["object_type"]
            # Fix 2: always use fresh payload name; fall back to FTS-provided
            # name only when payload has none (e.g. newly inserted without name).
            fresh_name = m["name"]
            enriched["name"] = fresh_name if fresh_name else enriched.get("name", "")
            result.append(enriched)
        return result

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
        if not hits:
            return hits
        ids = [h["object_id"] for h in hits]
        with self._connect() as db:
            rows = self._runtime.unified_kg.cluster_fold_rows(db, notebook_id, ids)
        fold: dict[str, tuple[str, str]] = {
            r["member_object_id"]: (r["canonical_id"], r["canonical_name"])
            for r in rows
        }
        # Apply fold and dedup by canonical id (keep MAX score)
        best: dict[str, dict] = {}
        for h in hits:
            mapping = fold.get(h["object_id"])
            folded = dict(h)
            if mapping is not None:
                canon_id, canon_name = mapping
                folded["object_id"] = canon_id
                folded["name"] = canon_name  # canonical_name overrides payload name
            key = folded["object_id"]
            if key not in best or folded["score"] > best[key]["score"]:
                best[key] = folded
        result = sorted(best.values(), key=lambda x: x["score"], reverse=True)
        return result[:k]

    def kg_search(self, notebook_id: str, q: str, k: int = 30) -> list:
        """Search KG objects by name (FTS5 lexical) union ANN semantic.

        Returns [{object_id, name, object_type, score, match}] sorted by score desc.
        Raises KeyError if notebook not found.

        Fix 1: after hydration, folds raw ko-<obj> concept ids to K-<canonical>
        ids via a bounded concept_clusters lookup so search results share the same
        id space as the viz graph — enabling click-to-expand on search hits.
        """
        from app.services.kg.search import merge_search_hits
        self.get_notebook(notebook_id)
        with self._connect() as db:
            lex = self._runtime.knowledge.fts_search(db, notebook_id, q, k)
        sem = self._semantic_search(notebook_id, q, k)
        merged = merge_search_hits(lex, sem, k)
        hydrated = self._hydrate_search_hits(notebook_id, merged)
        return self._fold_hits_to_canonical(notebook_id, hydrated, k)

    def eval_insert_source_for_test(
        self, nb_id: str, name: str, text: str, tmpdir: str
    ) -> str:
        """Insert a parsed source directly for eval speed tests.
        Uses the repo's write path; avoids raw _connect access in eval scripts."""
        import pathlib
        import uuid
        from app.services.kg.parsing import parse_elements
        f = pathlib.Path(tmpdir) / f"{name}.md"
        f.write_text(text, encoding="utf-8")
        sid = _new_id("src")
        now = _now()
        els = parse_elements(text, source_file=str(f))
        with self._write() as db:
            self._runtime.source_store.insert_source(
                source_id=sid,
                notebook_id=nb_id,
                title=name,
                source_type="markdown",
                status="extracted",
                parse_status="parsed",
                file_name=f"{name}.md",
                file_path=str(f),
                file_size=0,
                file_hash="",
                summary="",
                doc_type="textbook",
                connection=db,
            )
            self._runtime.source_store.replace_elements(
                db,
                sid,
                [
                    SourceElementWrite(
                        id=_new_id("el"),
                        element_type=el.type,
                        location_label=f"L{el.line_start}-{el.line_end}",
                        text=el.text,
                        metadata={},
                    )
                    for el in els
                ],
                created_at=now,
            )
        return sid

    def list_sources(self, notebook_id: str) -> List[SourceSummary]:
        self.get_notebook(notebook_id)
        return self._runtime.source_store.list_sources(notebook_id)

    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50,
                          q: str = "") -> PaginatedSources:
        """分页 + 可选 q(按 title/file_name 服务端过滤)。万级 source 安全:只取一页 +
        一次 COUNT,不全量进内存。"""
        self.get_notebook(notebook_id)
        return self._runtime.source_store.list_sources_page(
            notebook_id, offset=offset, limit=limit, q=q
        )

    def get_source(self, source_id: str) -> SourceDetail:
        return self._runtime.source_store.get_source(source_id)

    def _source_pipeline_hooks(self) -> SourcePipelineHooks:
        """Task 12: mint FRESH hooks on every ingestion call from the facade's
        own bound seats — post-construction per-instance monkeypatches
        (_run_extraction, _mark_unified_kg_dirty, ...) keep being observed.
        Gate 5 replaces these temporary callbacks with real services.  Never
        store the hooks (not on the runtime, not on the facade)."""
        return SourcePipelineHooks(
            should_extract_kg=self._should_extract_kg,
            extract_source=self._run_extraction,
            mark_unified_dirty=self._mark_unified_kg_dirty,
            augment_notebook_metadata=lambda notebook_id, source_id: (
                self._augment_notebook_meta(
                    notebook_id, pending_source_id=source_id
                )
            ),
            maybe_enqueue_scale_fold=self._maybe_enqueue_scale_fold,
        )

    def import_sources(self, notebook_id: str, payload: SourceImportRequest) -> List[SourceSummary]:
        return self._runtime.source_ingestion.import_sources(
            notebook_id, payload, self._source_pipeline_hooks()
        )

    def add_url_sources(
        self,
        notebook_id: str,
        urls: Iterable[str],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> AddUrlSourcesResult:
        return self._runtime.source_ingestion.add_url_sources(
            notebook_id, urls, scheduler, self._source_pipeline_hooks()
        )

    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: Optional[Callable[[str], None]] = None,
    ) -> List[SourceSummary]:
        return self._runtime.source_ingestion.upload_sources(
            notebook_id, files, scheduler, self._source_pipeline_hooks()
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
        with self._connect() as db:
            return self._has_kg(db, notebook_id)

    # CJK unified-ideograph block: presence ⇒ Chinese content.
    _CJK_RE = re.compile(r"[一-鿿]")
    _LATIN_RE = re.compile(r"[A-Za-z]")

    def _notebook_langs(self, notebook_id: str) -> List[str]:
        """Cheap, cached probe of a notebook's corpus languages (subset of
        ["zh","en"]) for bilingual query expansion — used so the query rewriter
        mints keywords in each corpus language, and (via _keyword_chunk_candidates)
        so those bilingual keywords carry the 2nd language into the CHUNK FTS as
        well as the KG-name/relation FTS. Samples a bounded SPREAD (head ∪ tail by
        rowid, up to ~60 texts) — not the first N — so 2nd-language content appended
        after many first-language chunks is still caught. Detects CJK vs Latin
        letters; returns ["en"] when empty/unknown, both when mixed. This is a
        HINT: a wrong guess only over/under-generates FTS keywords, never breaks
        retrieval. Cached per-notebook; the cache is invalidated at chunk-write
        settle points (source add / re-chunk / FTS backfill) so a notebook that
        gains new-language content re-reflects it."""
        cached = self._notebook_langs_cache.get(notebook_id)
        if cached is not None:
            return cached
        has_cjk = has_latin = False
        with self._connect() as db:
            # Head ∪ tail spread by rowid (bounded, no full-table sort — never
            # ORDER BY RANDOM()): catches both the earliest and the most recently
            # added chunks cheaply.
            rows = db.execute(
                "SELECT text FROM ("
                "  SELECT rowid AS rid, text FROM chunks WHERE notebook_id=? "
                "  ORDER BY rowid LIMIT 30) "
                "UNION "
                "SELECT text FROM ("
                "  SELECT rowid AS rid, text FROM chunks WHERE notebook_id=? "
                "  ORDER BY rowid DESC LIMIT 30)",
                (notebook_id, notebook_id)).fetchall()
        for r in rows:
            t = r["text"] or ""
            if not has_cjk and self._CJK_RE.search(t):
                has_cjk = True
            if not has_latin and self._LATIN_RE.search(t):
                has_latin = True
            if has_cjk and has_latin:
                break
        langs: List[str] = []
        if has_cjk:
            langs.append("zh")
        if has_latin:
            langs.append("en")
        if not langs:
            langs = ["en"]   # empty/unknown → safe single-language default
        self._notebook_langs_cache[notebook_id] = langs
        return langs

    def _should_extract_kg(self, notebook_id: str) -> bool:
        return self._runtime.source_ingestion.should_extract_kg(notebook_id)

    def build_notebook_kg(self, notebook_id: str, *, progress=None) -> dict:
        """按需构建 KG — KnowledgeLifecycleService 拥有编排(Task 15,含
        kg_building 单飞守卫与 governance 冲突消解直连);冻结签名 delegate。"""
        return self._runtime.knowledge_lifecycle.build_notebook_kg(
            notebook_id, progress=progress
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
        return self._runtime.source_ingestion.process_source(
            source_id, self._source_pipeline_hooks()
        )

    def parse_source(self, source_id: str) -> SourceSummary:
        return self._runtime.source_ingestion.parse_source(
            source_id, self._source_pipeline_hooks()
        )

    def _augment_notebook_meta(self, notebook_id: str, pending_source_id: str = "") -> None:
        return self._runtime.source_ingestion.augment_notebook_metadata(
            notebook_id, pending_source_id
        )

    # --- TEMPORARY Task-12 catalog callbacks (Task 13/15 move this SQL into
    # --- the notebook/source stores); the ingestion service owns the
    # --- metadata-augmentation BUSINESS body and calls back through the
    # --- constructor-injected seams below.
    def _notebook_meta_row(self, notebook_id: str) -> Optional[dict]:
        with self._connect() as db:
            nb = db.execute(
                "SELECT name, purpose_auto FROM notebooks WHERE id = ?", (notebook_id,)
            ).fetchone()
        if nb is None:
            return None
        return {
            "name": nb["name"],
            "purpose_auto": ("purpose_auto" in nb.keys() and nb["purpose_auto"] == 1),
        }

    def _notebook_meta_sources(
        self, notebook_id: str, pending_source_id: str = ""
    ) -> List[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT title, doc_type, summary FROM sources "
                "WHERE notebook_id = ? AND (status = 'extracted' OR id = ?) "
                "ORDER BY created_at ASC",
                (notebook_id, pending_source_id),
            ).fetchall()
        return [
            {"title": r["title"], "doc_type": r["doc_type"], "summary": r["summary"]}
            for r in rows
        ]

    def _apply_notebook_meta(
        self, notebook_id: str, *, guard_name, name: str, purpose: str
    ) -> None:
        with self._write() as db:
            if name:
                # Optimistic guard: only overwrite if the name is still the
                # placeholder we read (no clobber of a concurrent rename).
                db.execute(
                    "UPDATE notebooks SET name = ?, updated_at = ? WHERE id = ? AND name = ?",
                    (name, _now(), notebook_id, guard_name),
                )
            if purpose:
                db.execute(
                    "UPDATE notebooks SET purpose = ?, updated_at = ? "
                    "WHERE id = ? AND purpose_auto = 1",
                    (purpose, _now(), notebook_id),
                )

    def source_elements(self, source_id: str) -> List[SourceElement]:
        return self._runtime.source_store.source_elements(source_id)

    def delete_source(self, source_id: str) -> None:
        self._runtime.source_ingestion.delete_source(
            source_id, self._source_pipeline_hooks()
        )

    def extract_source(self, source_id: str) -> None:
        """Public entry to (re-)extract a single source's KG in place. Delegates to
        _run_extraction, which deletes the source's prior KG objects/relations/
        embeddings and re-runs extraction (idempotent per source). Used by
        reextract_notebook and maintenance scripts."""
        self._run_extraction(source_id)

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
        self, source_id: str, notebook_id: str, run_id: str, created_at: str
    ) -> None:
        """Reset one source's prior KG artefacts and open its extraction_runs
        row in ONE write transaction — the exact commit boundary the inline
        _run_extraction body always had."""
        with self._write() as db:
            self._runtime.knowledge.begin_extraction_run(
                db, source_id, notebook_id, run_id, created_at
            )

    def _finish_extraction_run(self, run_id: str, status: str, message: str) -> None:
        with self._write() as db:
            self._runtime.knowledge.finish_extraction_run(
                db, run_id, status, message, _now()
            )

    def _notebook_tier(self, notebook_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
        return str(row["tier"]) if row is not None and row["tier"] else ""

    def _source_raw_text(self, source, elements) -> str:
        return self._runtime.source_files.read_source_text(
            getattr(source, "file_path", "") or "", elements
        )

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
        if not self.settings.embedder_configured:
            return
        text = _payload_text(payload).strip()
        if not text:
            return
        try:
            vector = self.embedder.embed_query(text[:2000])
        except Exception:
            return
        self._runtime.embedding_store.replace_knowledge_vectors(
            notebook_id, [(object_id, vector)], created_at=_now()
        )

    def _flush_object_vectors(self, notebook_id: str, rows: list) -> None:
        """把一批 (oid, vector) 落 knowledge_embeddings(一个写事务,幂等 REPLACE)。"""
        if not rows:
            return
        self._runtime.embedding_store.replace_knowledge_vectors(
            notebook_id, rows, created_at=_now()
        )

    def _embed_objects_batch(self, notebook_id: str, items: List[dict],
                             progress=None, commit_every: Optional[int] = None) -> None:
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

    def _knowledge_vectors(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        objects: List[dict],
    ) -> Dict[str, List[float]]:
        """⚠ 死代码(全仓无调用者,2026-07-03 核实)——若复活用于相似度计算,
        必须对 decode_vector 结果接 truncate_vec(运行时截断),否则查询/语料混空间
        静默零召回(见 tests/test_dim_invariants.py 的白名单与风险登记 R2)。

        Map object_id -> payload embedding. Lazily backfills missing vectors
        for the given objects (one-time per object) so pre-existing / seed
        knowledge also gains payload-level semantic recall."""
        from app.services.vector_index import decode_vector

        rows = db.execute(
            "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
        vectors: Dict[str, List[float]] = {}
        for row in rows:
            if not row["vector"]:
                continue
            arr = decode_vector(row["vector"])
            if arr is not None:
                vectors[row["object_id"]] = arr.tolist()
        if not self.settings.embedder_configured:
            return vectors
        pending_ids, pending_texts = [], []
        for obj in objects:
            object_id = obj["id"]
            if object_id in vectors:
                continue
            text = _payload_text(obj.get("payload", {})).strip()
            if not text:
                continue
            pending_ids.append(object_id); pending_texts.append(text[:2000])
        if not pending_texts:
            return vectors
        try:
            new_vectors = self.embedder.embed_texts(pending_texts)
        except Exception:
            return vectors  # backfill best-effort; never block search
        from app.services.vector_index import encode_vector
        now = _now()
        for object_id, vector in zip(pending_ids, new_vectors):
            vectors[object_id] = vector
            db.execute(
                """
                INSERT OR REPLACE INTO knowledge_embeddings
                (object_id, notebook_id, vector, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (object_id, notebook_id, encode_vector(vector), now),
            )
        return vectors

    def _backfill_knowledge_embeddings(self, db: sqlite3.Connection,
                                       notebook_id: str, objects: List[dict],
                                       progress=None) -> None:
        """Embed + persist any knowledge objects missing a vector, concurrently
        (reuses the concurrent _embed_objects_batch). No-op when all are embedded
        or no embedder. Lets ask() build a complete knowledge matrix without
        materializing a Python-list dict of every vector."""
        if not self.settings.embedder_configured:
            return
        have = {
            r["object_id"]
            for r in db.execute(
                "SELECT object_id FROM knowledge_embeddings "
                "WHERE notebook_id = ? AND vector IS NOT NULL",
                (notebook_id,),
            ).fetchall()
        }
        missing = [
            {"_oid": o["id"], "payload": o.get("payload", {})}
            for o in objects
            if o["id"] not in have and _payload_text(o.get("payload", {})).strip()
        ]
        if missing:
            self._embed_objects_batch(notebook_id, missing, progress=progress)
        elif progress:
            progress(0, 0)

    def _backfill_relation_embeddings(self, notebook_id: str) -> None:
        """给缺向量的关系补 relation_embeddings(幂等,只补缺失)。无 embedder 则 no-op。"""
        if not self.settings.embedder_configured:
            return
        with self._connect() as db:
            relations = self._relations_with_names(db, notebook_id)
            have = {r["relation_id"] for r in db.execute(
                "SELECT relation_id FROM relation_embeddings WHERE notebook_id=?",
                (notebook_id,)).fetchall()}
        missing = [{"_rid": r["id"], "text": r["text"]} for r in relations
                   if r["id"] not in have]
        if missing:
            self._embed_relations_batch(notebook_id, missing)

    def knowledge_types(self, notebook_id: str) -> List[KnowledgeTypeCount]:
        """All object types present in this notebook with non-deprecated counts,
        so the UI can render a tab per type — including academic/textbook types
        that have no bespoke card."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            counts, labels = self._runtime.knowledge.type_counts(db, notebook_id)
        ordered = [t for t in OBJECT_SCHEMAS if t in counts]
        ordered += [t for t in counts if t not in OBJECT_SCHEMAS]
        return [
            KnowledgeTypeCount(
                object_type=t,
                label=labels.get(t, OBJECT_TYPE_LABELS.get(t, t)),
                count=counts[t],
            )
            for t in ordered
        ]

    def _knowledge_record(
        self, object_type: str, obj: dict, schema: Optional[ObjectSchema]
    ) -> KnowledgeRecord:
        payload = obj.get("payload") or {}
        keys = (
            schema.fields
            if schema
            else [k for k in payload if not str(k).startswith("_")]
        )
        fields: List[KnowledgeFieldValue] = []
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                text = ", ".join(str(v) for v in value if str(v).strip())
            elif value is None:
                text = ""
            else:
                text = str(value)
            if text.strip():
                fields.append(KnowledgeFieldValue(key=key, value=text.strip()))
        return KnowledgeRecord(
            id=obj["id"],
            object_type=object_type,
            headline=self._knowledge_headline(object_type, payload),
            fields=fields,
            status=obj.get("status", "approved"),
            owner=obj.get("owner", ""),
            last_reviewed=obj.get("last_reviewed", ""),
            evidence=obj.get("evidence", []),
        )

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
        self.get_notebook(notebook_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        schema = self.effective_schemas().get(object_type)

        with self._connect() as db:
            total, objects = self._runtime.knowledge.list_knowledge_page(
                db, notebook_id, object_type, status, offset, limit
            )

        items = [self._knowledge_record(object_type, obj, schema) for obj in objects]
        return PaginatedKnowledge(
            items=items,
            total_count=total,
            offset=offset,
            limit=limit,
        )

    # --- Editable extraction-schema registry (Task 13: SchemaRegistryService
    # --- owns validation/sampling/LLM induction; KnowledgeStore owns rows) ---
    @staticmethod
    def _object_schema_from_row(row) -> ObjectSchemaModel:
        from app.services.schema_registry import object_schema_from_row
        return object_schema_from_row(row)

    def effective_schemas(self) -> Dict[str, ObjectSchema]:
        """Active object schemas as an ObjectSchema registry for extraction —
        DB rows overlaid on the code defaults."""
        return self._runtime.schema_registry.effective_schemas()

    def list_object_schemas(self) -> List[ObjectSchemaModel]:
        return self._runtime.schema_registry.list_object_schemas()

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
        self.get_notebook(notebook_id)
        with self._connect() as db:
            nb_count = self._runtime.knowledge.count_active_objects(db, notebook_id)
        if int(nb_count) > self.settings.viz_sync_build_max_objects:
            raise KnowledgeGraphTooLargeError(
                f"notebook {notebook_id} has {nb_count} objects "
                f"(> {self.settings.viz_sync_build_max_objects}); the legacy "
                "/graph endpoint has no bounded fallback for large notebooks — "
                "use /notebooks/{id}/unified-kg instead (bounded/paginated)."
            )
        with self._connect() as db:
            rows = self._runtime.knowledge.graph_node_rows(db, notebook_id)
        nodes = [
            KnowledgeNode(id=r["id"], object_type=r["object_type"],
                          headline=self._kg_headline(json.loads(r["payload"] or "{}")),
                          status=r["status"])
            for r in rows]
        valid = {n.id for n in nodes}
        edges = [
            KnowledgeEdge(from_id=rel["source_object_id"], to_id=rel["target_object_id"],
                          relation=rel["edge_type"], label=rel["edge_type"])
            for rel in self.relations_for_notebook(notebook_id)
            if rel["source_object_id"] in valid and rel["target_object_id"] in valid]
        return KnowledgeGraph(nodes=nodes, edges=edges)

    def _kg_headline(self, payload: dict) -> str:
        name = (payload.get("name") or "").strip()
        return name[:120] if len(name) > 120 else name

    def add_relations(self, notebook_id: str, source_id: str,
                      relations: List[dict]) -> int:
        now = _now()
        with self._write() as db:
            return self._runtime.knowledge.add_relations(
                db, notebook_id, source_id, relations, now
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

    def relations_for_notebook(self, notebook_id: str) -> List[dict]:
        with self._connect() as db:
            return self._runtime.knowledge.relations_for_notebook(db, notebook_id)

    # --- Track E: edge trust review queue + curation feedback loop ----------
    _REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

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
        from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality

        version = tuple(self._scale_index_version(notebook_id))

        def _load() -> Dict[str, float]:
            with self._connect() as db:
                max_nodes = self.settings.edge_centrality_max_nodes

                # 1. Degree ranking via SQL — bounded by distinct node count
                #    touched by a non-rejected relation (never the full
                #    knowledge_objects table).
                degree: Dict[str, int] = {}
                for r in db.execute(
                    "SELECT source_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
                    "WHERE notebook_id = ? AND review_status != 'rejected' "
                    "GROUP BY source_object_id", (notebook_id,),
                ).fetchall():
                    degree[r["n"]] = degree.get(r["n"], 0) + r["c"]
                for r in db.execute(
                    "SELECT target_object_id AS n, COUNT(*) AS c FROM knowledge_relations "
                    "WHERE notebook_id = ? AND review_status != 'rejected' "
                    "GROUP BY target_object_id", (notebook_id,),
                ).fetchall():
                    degree[r["n"]] = degree.get(r["n"], 0) + r["c"]

                bounded = len(degree) > max_nodes
                if bounded:
                    # Deterministic top-K: sort by (-degree, id) so ties break
                    # on a stable, reproducible key (unlike the old
                    # insertion-order tiebreak, which depended on dict-iteration
                    # order of a full objects load we no longer perform).
                    top_ids = [n for n, _ in sorted(
                        degree.items(), key=lambda kv: (-kv[1], kv[0])
                    )[:max_nodes]]
                    top_ids_json = json.dumps(top_ids)
                    rel_rows = db.execute(
                        "SELECT r.id, r.source_object_id, r.target_object_id, "
                        "r.edge_type, r.evidence FROM knowledge_relations r "
                        "JOIN json_each(?) s ON s.value = r.source_object_id "
                        "JOIN json_each(?) t ON t.value = r.target_object_id "
                        "WHERE r.notebook_id = ? AND r.review_status != 'rejected'",
                        (top_ids_json, top_ids_json, notebook_id),
                    ).fetchall()
                    node_ids = top_ids
                else:
                    rel_rows = db.execute(
                        "SELECT id, source_object_id, target_object_id, edge_type, "
                        "evidence FROM knowledge_relations "
                        "WHERE notebook_id = ? AND review_status != 'rejected'",
                        (notebook_id,),
                    ).fetchall()
                    obj_rows = db.execute(
                        "SELECT id FROM knowledge_objects WHERE notebook_id = ?",
                        (notebook_id,),
                    ).fetchall()
                    node_ids = [r["id"] for r in obj_rows]

            # 2. Node dict for build_rx_graph — type/name are unused by
            #    compute_edge_centrality (only `rel_id` is read back), so a
            #    minimal empty-string payload keeps graph SHAPE (node count,
            #    indices) identical without a knowledge_objects payload load.
            nodes = {oid: {"type": "", "name": ""} for oid in node_ids}

            rels = []
            for r in rel_rows:
                rels.append({
                    "id": r["id"],
                    "source_object_id": r["source_object_id"],
                    "target_object_id": r["target_object_id"],
                    "edge_type": r["edge_type"],
                    "evidence": json.loads(r["evidence"] or "[]"),
                })

            G, idx_to_oid, oid_to_idx = build_rx_graph(nodes, rels)
            return compute_edge_centrality(G)

        return self._vector_cache.get(f"{notebook_id}:edge_centrality", version, _load)

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

    def _tier2_bridge_candidates_ann(self, notebook_id: str, idx, ann, new_objs: list,
                                     cluster_map_: Dict[str, str]) -> list:
        """ANN-backed Tier2 bridge candidate detection — KnowledgeLifecycleService
        owns the body (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle._tier2_bridge_candidates_ann(
            notebook_id, idx, ann, new_objs, cluster_map_
        )
    def cluster_map(self, notebook_id: str) -> Dict[str, str]:
        """Cached {member_object_id: canonical_id} — one concept_clusters scan per
        version, reused by unified_graph/viz build/kg_neighbors fallback/answer-context
        fold/incremental_fuse_source etc. P1-2: this used to be re-scanned (ALL member
        rows, at production scale millions) on every call of every consumer; now it's
        version-cached like _vector_matrix/_ent_chunk_map/_elem_chunk_map. All known
        consumers only .get() from the returned dict (never mutate it in place), so a
        single cached dict object is safe to hand out to every caller."""
        version = tuple(self._scale_index_version(notebook_id))

        def _load():
            with self._connect() as db:
                return self._runtime.unified_kg.cluster_map_rows(db, notebook_id)

        return self._vector_cache.get(f"{notebook_id}:clustermap", version, _load)

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
        版本 = canonical_rel_seq(O(1) 行读),表重建后自动失效。"""
        with self._connect() as db:
            st = self._runtime.unified_kg.state_row(db, notebook_id)
        seq = int(st["canonical_rel_seq"]) if st else -1

        def _load():
            out: Dict[tuple, tuple] = {}
            with self._connect() as db:
                for r in self._runtime.unified_kg.edge_support_rows(db, notebook_id):
                    out[(r["canonical_src"], r["edge_type"], r["canonical_tgt"])] = (
                        int(r["support_count"]), int(r["source_count"]))
            return out

        return self._vector_cache.get(f"{notebook_id}:edge_support", ("edge_support", seq), _load)

    def _annotate_edge_support(self, notebook_id: str, edges: List[dict]) -> List[dict]:
        """给 unified/neighbors 形状的边({source_object_id,target_object_id,edge_type})
        附 support_count/source_count。查表键先过 cluster_map 折叠:unified 图只折叠
        concept 端点,claim/formula/procedure 保原始 id,而 canonical_relations 按全
        类型折叠;concept 端点已是 canonical id、不在 cluster_map 键中,get(s,s) 恒等
        通过。未命中不加字段(表空/滞后 → 前端优雅缺省)。"""
        sup = self._edge_support_map(notebook_id)
        if not sup:
            return edges
        cmap = self.cluster_map(notebook_id)
        out: List[dict] = []
        for e in edges:
            key = (cmap.get(e["source_object_id"], e["source_object_id"]),
                   e["edge_type"],
                   cmap.get(e["target_object_id"], e["target_object_id"]))
            hit = sup.get(key)
            if hit is None:
                # kg_neighbors 把边统一画成「查询节点→邻居」(既有展示行为),
                # 入边的真实方向在表里是反的——对称回退让同一条底层关系无论
                # 展示朝向都能拿到支持度;正向命中优先,A→B 与 B→A 同时存在时不串。
                hit = sup.get((key[2], key[1], key[0]))
            if hit:
                # 拷贝而非就地改:全量路径的边 dict 与 _unified_cache 共享引用,
                # 就地写字段会把注解粘进缓存(缓存须保持不含注解,避免粘住旧计数)。
                out.append({**e, "support_count": hit[0], "source_count": hit[1]})
            else:
                out.append(e)
        return out

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
    def kg_neighbors(self, notebook_id: str, object_id: str, cap: int = 50) -> dict:
        """1-hop folded neighborhood — KnowledgeLifecycleService owns the read
        orchestration (Task 15); frozen-signature delegate."""
        return self._runtime.knowledge_lifecycle.kg_neighbors(notebook_id, object_id, cap)

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
    def concept_detail(self, notebook_id: str, canonical_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            # Get cluster members and canonical name in one query
            cluster_rows = db.execute(
                "SELECT cc.member_object_id, cc.canonical_name, ko.object_type, ko.payload, ko.evidence "
                "FROM concept_clusters cc "
                "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
                "WHERE cc.notebook_id=? AND cc.canonical_id=? AND ko.status!='deprecated'",
                (notebook_id, canonical_id),
            ).fetchall()
            # canonical_name comes from the cluster table (same for all rows)
            name_row = db.execute(
                "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
                (notebook_id, canonical_id),
            ).fetchone()
            name = name_row["canonical_name"] if name_row else ""

        members = []
        member_ids = []
        for r in cluster_rows:
            obj = {
                "id": r["member_object_id"],
                "object_type": r["object_type"],
                "payload": json.loads(r["payload"] or "{}"),
                "evidence": json.loads(r["evidence"] or "[]"),
            }
            members.append(obj)
            member_ids.append(r["member_object_id"])

        mset = set(member_ids)

        if not mset:
            # No members found; still return valid shape
            return {"canonical_id": canonical_id, "canonical_name": name,
                    "members": [], "attached": [], "evidence": []}

        ph = ",".join("?" for _ in mset)
        mlist = list(mset)

        with self._connect() as db:
            # Targeted relation queries using member placeholders
            rels_out = db.execute(
                f"SELECT source_object_id, target_object_id, edge_type "
                f"FROM knowledge_relations WHERE notebook_id=? AND source_object_id IN ({ph})",
                [notebook_id] + mlist,
            ).fetchall()
            rels_in = db.execute(
                f"SELECT source_object_id, target_object_id, edge_type "
                f"FROM knowledge_relations WHERE notebook_id=? AND target_object_id IN ({ph})",
                [notebook_id] + mlist,
            ).fetchall()

            # Collect attached object ids (non-member side of relations)
            attached_ids: set[str] = set()
            rel_edges: list[dict] = []
            for rel in rels_out:
                other = rel["target_object_id"]
                if other not in mset:
                    attached_ids.add(other)
                    rel_edges.append({"other": other, "edge_type": rel["edge_type"]})
            for rel in rels_in:
                other = rel["source_object_id"]
                if other not in mset:
                    attached_ids.add(other)
                    rel_edges.append({"other": other, "edge_type": rel["edge_type"]})

            # Batch-read attached objects
            by_other: dict[str, dict] = {}
            if attached_ids:
                aph = ",".join("?" for _ in attached_ids)
                arows = db.execute(
                    f"SELECT id, object_type, payload, evidence FROM knowledge_objects "
                    f"WHERE id IN ({aph}) AND status!='deprecated'",
                    list(attached_ids),
                ).fetchall()
                by_other = {
                    r["id"]: {
                        "id": r["id"],
                        "object_type": r["object_type"],
                        "payload": json.loads(r["payload"] or "{}"),
                        "evidence": json.loads(r["evidence"] or "[]"),
                    }
                    for r in arows
                }

        attached = []
        seen_attached: set[str] = set()
        for edge in rel_edges:
            other = edge["other"]
            if other in by_other and by_other[other]["object_type"] != "concept" and other not in seen_attached:
                seen_attached.add(other)
                attached.append({**by_other[other], "edge_type": edge["edge_type"]})

        member_by_id = {m["id"]: m for m in members}
        evidence = [ev for oid in member_ids for ev in member_by_id.get(oid, {}).get("evidence", [])]
        with self._connect() as db:
            evidence = self._enrich_evidence(db, evidence)

        return {"canonical_id": canonical_id, "canonical_name": name,
                "members": [member_by_id[o] for o in member_ids if o in member_by_id],
                "attached": attached, "evidence": evidence}

    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC",
            (ids[0],),
        ).fetchall()
        ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
        return texts, ordinal

    def _enrich_evidence(self, db, evidence):
        texts, _ = self._element_texts(db, [e.get("element_id") for e in evidence])
        out = []
        for e in evidence:
            out.append({"quoted_span": e.get("quoted_span", ""),
                        "source_title": e.get("source_title", "") or e.get("source_id", ""),
                        "element_text": texts.get(e.get("element_id", ""), e.get("quoted_span", ""))})
        return out

    def node_context(self, notebook_id, object_id):
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE id=? AND notebook_id=?", (object_id, notebook_id)).fetchone()
            if row is None:
                raise KeyError(object_id)
            obj_type = row["object_type"]
            payload = json.loads(row["payload"] or "{}")
            section = payload.get("section_path", "")
            occurrences = self._enrich_evidence(db, json.loads(row["evidence"] or "[]"))
            result = {"id": object_id, "object_type": obj_type, "name": payload.get("name", ""),
                      "section_path": section, "occurrences": occurrences, "definition": None, "steps": None}
            if obj_type == "concept":
                # prefer the unified cluster's fused description when present
                crow = db.execute(
                    "SELECT canonical_description FROM concept_clusters "
                    "WHERE notebook_id=? AND member_object_id=? AND canonical_description!='' LIMIT 1",
                    (notebook_id, object_id)).fetchone()
                if crow and crow["canonical_description"]:
                    result["definition"] = crow["canonical_description"]
                else:
                    drow = db.execute(
                        "SELECT ko.payload, ko.evidence FROM knowledge_relations r JOIN knowledge_objects ko ON ko.id=r.source_object_id "
                        "WHERE r.notebook_id=? AND r.target_object_id=? AND r.edge_type='defines' LIMIT 1", (notebook_id, object_id)).fetchone()
                    if drow is not None:
                        dpay = json.loads(drow["payload"] or "{}")
                        den = self._enrich_evidence(db, json.loads(drow["evidence"] or "[]"))
                        result["definition"] = (den[0]["element_text"] if den else dpay.get("name", ""))
            if obj_type == "procedure":
                steps_payload = payload.get("steps")
                if isinstance(steps_payload, list) and steps_payload:
                    # New self-contained shape: ordered steps live in the object's payload.
                    eids = [s.get("element_id") for s in steps_payload if s.get("element_id")]
                    texts, _ord = self._element_texts(db, eids) if eids else ({}, {})
                    result["steps"] = [
                        {"name": s.get("name", ""),
                         "element_text": texts.get(s.get("element_id") or "", s.get("quote", "")),
                         "section_path": section}
                        for s in steps_payload
                    ]
                else:
                    # Legacy fallback: group sibling procedure nodes by exact section_path
                    # (precedes edges are sparse). Two distinct procedures sharing a heading
                    # would merge — acceptable for inspection.
                    #
                    # P2-3: this used to scan EVERY procedure object in the notebook
                    # (regardless of section) and filter in Python — O(procedures in
                    # notebook) per call. When the target node's own section_path is
                    # known (the common case — payload.get("section_path") above),
                    # bind the query to it directly in SQL via json_extract (JSON1,
                    # already used elsewhere in this file), so SQLite only reads
                    # matching rows. section_path is free text (not a dedicated
                    # column) so this is the only way to push the filter down without
                    # a schema change. If section_path is unavailable (rare: an old
                    # or malformed payload), fall back to a bounded LIMIT — this path
                    # is a display-only legacy fallback, not a correctness-critical
                    # query, so an arbitrary-but-bounded sample is acceptable.
                    if section:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "AND json_extract(payload,'$.section_path')=?",
                            (notebook_id, section)).fetchall()
                    else:
                        prows = db.execute(
                            "SELECT id, payload, evidence FROM knowledge_objects "
                            "WHERE notebook_id=? AND object_type='procedure' AND status!='deprecated' "
                            "LIMIT 500",
                            (notebook_id,)).fetchall()
                    candidate_steps = []
                    for pr in prows:
                        ppay = json.loads(pr["payload"] or "{}")
                        if ppay.get("section_path", "") != section:
                            continue
                        ev = json.loads(pr["evidence"] or "[]")
                        first_eid = ev[0].get("element_id") if ev else ""
                        candidate_steps.append((ppay.get("name", ""), first_eid))
                    all_step_first_eids = [eid for _, eid in candidate_steps if eid]
                    if all_step_first_eids:
                        texts, ordinal = self._element_texts(db, all_step_first_eids, with_ordinal=True)
                    else:
                        texts, ordinal = {}, {}
                    steps = []
                    for step_name, first_eid in candidate_steps:
                        steps.append({"name": step_name, "element_text": texts.get(first_eid, ""),
                                      "section_path": section, "_ord": ordinal.get(first_eid, 1_000_000)})
                    steps.sort(key=lambda s: s["_ord"])
                    for s in steps:
                        s.pop("_ord", None)
                    result["steps"] = steps
            return result

    # test-only helper; later tasks may replace it with a public insert path
    def _test_insert_object(self, notebook_id: str, object_type: str, payload: dict, source_id: str = "") -> str:
        oid = _new_id("ko")
        now = _now()
        with self._write() as db:
            db.execute(
                """INSERT INTO knowledge_objects
                   (id, notebook_id, object_type, status, owner, payload, evidence,
                    source_candidate_id, source_id, created_at, updated_at)
                   VALUES (?, ?, ?, 'approved', '', ?, '[]', NULL, ?, ?, ?)""",
                (oid, notebook_id, object_type, json.dumps(payload, ensure_ascii=False), source_id, now, now),
            )
        return oid

    # --- Governance: promotion state machine (Track F) -------------------
    # KnowledgeGovernanceService owns the domain (Task 16); the facade keeps
    # frozen-signature delegates.

    @staticmethod
    def _promotion_row_to_dict(row: sqlite3.Row, *, payload=None, evidence=None) -> dict:
        """Map a promotion_candidates row to the PromotionCandidate-shaped dict —
        canonical body lives with the governance service (Task 16)."""
        from app.services.knowledge_governance import promotion_row_to_dict
        return promotion_row_to_dict(row, payload=payload, evidence=evidence)

    def propose_promotion(self, notebook_id: str, object_id: str) -> dict:
        """Propose a personal-KG object for promotion into the base corpus —
        KnowledgeGovernanceService owns the orchestration (Task 16; idempotent
        active-proposal reuse and the base-notebook guard are unchanged).
        Frozen-signature delegate."""
        return self._runtime.knowledge_governance.propose_promotion(
            notebook_id, object_id
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

    @staticmethod
    def _seed_fn_for(object_type: str):
        """Return the kg_merge seed function for a KG object type."""
        from app.repositories.sqlite.governance_store import seed_fn_for
        return seed_fn_for(object_type)

    def _find_base_dedup_match(
        self, object_type: str, src_payload: dict, base_objs: List[sqlite3.Row]
    ) -> str:
        """Exact-seed dedup (v1) — canonical body lives with the governance
        store's promotion primitive (Task 13)."""
        from app.repositories.sqlite.governance_store import find_base_dedup_match
        return find_base_dedup_match(object_type, src_payload, base_objs)

    @staticmethod
    def _merge_evidence_lists(base_ev: list, src_ev: list) -> list:
        """Union two evidence lists, deduping on (source_id, element_id, quoted_span)."""
        from app.repositories.sqlite.governance_store import merge_evidence_lists
        return merge_evidence_lists(base_ev, src_ev)

    def reject_promotion(self, candidate_id: str, reason: str = "") -> dict:
        """Reject a promotion candidate — KnowledgeGovernanceService owns the
        orchestration (Task 16). Frozen-signature delegate."""
        return self._runtime.knowledge_governance.reject_promotion(
            candidate_id, reason
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
        from app.services.knowledge_governance import knowledge_headline
        return knowledge_headline(object_type, payload)

    def _knowledge_ref(self, obj: dict, object_type: str) -> KnowledgeRef:
        """KnowledgeRef projection — KnowledgeGovernanceService owns the body
        (Task 16); frozen-signature delegate."""
        return self._runtime.knowledge_governance._knowledge_ref(obj, object_type)

    @staticmethod
    def _payload_join(payload: dict) -> str:
        """Flatten payload text — canonical body lives with the governance
        service (Task 16)."""
        from app.services.knowledge_governance import payload_join
        return payload_join(payload)

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

    def _note_model_error(self, stage: str, model: str, exc: Exception) -> None:
        return self._runtime.models.note_model_error(stage, model, exc)

    def _embed_query(self, query: str) -> Optional[List[float]]:
        """查询 embedding(端点按原生维出向量)→ 运行时截断(EMBED_RUNTIME_DIM,
        与语料侧 build_matrix 共用 truncate_vec 同一口径)。查询/语料两侧维度
        不一致 = 静默零召回,是截断项目的头号风险 —— 勿在别处另行截断。
        P1-A:ask 作用域内(_ASK_EMBED_CACHE 非 None)按 query[:2000] 复用同文本
        的截断后向量,砍 federated 双 tier/seed/quota 对同一问题的重复 RTT;
        失败不缓存(保留每次重试语义)。default None(非 ask 路径)行为不变。"""
        if not self.settings.embedder_configured:
            return None
        cache = _ASK_EMBED_CACHE.get()
        key = query[:2000]
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                return hit
        try:
            vec = self.embedder.embed_query(query[:2000])
        except Exception as exc:
            self._note_model_error("embed", self.settings.embed_model, exc)
            return None
        from app.services.vector_index import resolve_runtime_dim, truncate_vec
        rd = resolve_runtime_dim(self.settings)
        if rd and vec is not None and len(vec) > rd:
            import numpy as np
            vec = truncate_vec(np.asarray(vec, dtype=np.float32), rd).tolist()
        if cache is not None and vec is not None:
            cache[key] = vec
        return vec

    def _runtime_dim(self) -> int:
        """本 repo 生效的运行时截断维(读 self.settings,非全局 get_settings)。
        build_matrix 内部裸调 resolve_runtime_dim() 读全局单例,生产两者同一对象,
        但测试/多实例下 self.settings 才是真相 —— 建 ANN/矩阵的 build_matrix 调用
        应显式传本值,使工件维度由本 repo 配置决定(见 T5 manifest 真相化)。"""
        from app.services.vector_index import resolve_runtime_dim
        return resolve_runtime_dim(self.settings)

    def _gather_elements(self, db: sqlite3.Connection, notebook_id: str,
                         with_vectors: bool = True) -> List[dict]:
        # 运行时截断旁路(计划 §1.2 旁路 1):element 兜底不走 build_matrix,逐向量
        # 进 retrieval.cosine 与 _embed_query(已截断,T2)比较 —— cosine 对 len 不等
        # 静默返 0.0,漏截 = 全部 element 静默零相似度。
        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        rd = resolve_runtime_dim(self.settings)
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
        elements: List[dict] = []
        for row in rows:
            vector = None
            if with_vectors and row["vector"]:
                arr = decode_vector(row["vector"])
                if arr is not None and rd:
                    arr = truncate_vec(arr, rd)
                vector = arr.tolist() if arr is not None else None
            elements.append(
                {
                    "element_id": row["id"],
                    "source_id": row["source_id"],
                    "source_title": row["source_title"],
                    "location_label": row["location_label"],
                    "element_type": row["element_type"],
                    "text": row["text"],
                    "vector": vector,
                }
            )
        return elements

    @staticmethod
    def _element_vectors(elements: List[dict]) -> dict:
        """Map element_id -> embedding vector for elements that have one."""
        return {
            element["element_id"]: element["vector"]
            for element in elements
            if element.get("vector")
        }

    def _gather_chunks(self, db: sqlite3.Connection, notebook_id: str) -> List[dict]:
        rows = db.execute(
            """
            SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids,
                   s.title AS source_title
            FROM chunks c JOIN sources s ON s.id = c.source_id
            WHERE c.notebook_id = ?
            """,
            (notebook_id,),
        ).fetchall()
        return [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]

    def _vector_matrix_version(self, db: sqlite3.Connection, notebook_id: str, table: str):
        """(table, count, max created_at) version tuple for a notebook's `table`
        embeddings — the same cheap aggregate query _vector_matrix uses to key
        its cache. Factored out so callers can `peek()` cache warmth (e.g. the
        large-notebook cold-matrix guard in _retrieve_relations_scored) without
        duplicating the SQL or paying for the (potentially GB-scale) loader.

        Includes the runtime truncation dim (T3 / 风险 R4): the cached matrix
        lives in the runtime similarity space, so flipping EMBED_RUNTIME_DIM
        without a restart must miss — serving an old-dim matrix would make
        every query_sims silently return {} (dim-mismatch guard). Warm-peek
        (_vector_matrix_warm) shares this version, so guard warmth judgments
        stay in sync with the real cache by construction."""
        from app.services.vector_index import resolve_runtime_dim
        ver = db.execute(
            f"SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
            f"FROM {table} WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchone()
        return (table, ver["c"], ver["ts"], resolve_runtime_dim(self.settings))

    def _vector_matrix(self, db: sqlite3.Connection, notebook_id: str,
                       table: str, id_col: str):
        """Cached (ids, normalized float32 matrix) for a notebook's embeddings.

        Streams JSON vectors straight into one float32 matrix (vector_index.
        build_matrix) so retrieval is a single matmul with bounded memory — vs
        materializing thousands of vectors as Python float lists (~1.3 GB on a
        large KG). Version-keyed on (count, max created_at) so it self-invalidates
        after (re)ingest. `table`/`id_col` are internal constants (not user input)."""
        from app.services.vector_index import build_matrix

        version = self._vector_matrix_version(db, notebook_id, table)
        # 键↔loader 同源:截断维取 version 元组里那份(而非各自再读 settings),
        # 缓存键声称的空间与实际加载的空间 by construction 一致(T3/R4)。
        runtime_dim = version[-1]

        def _load():
            rows = db.execute(
                f"SELECT {id_col} AS vid, vector FROM {table} WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            return build_matrix(((r["vid"], r["vector"]) for r in rows),
                                runtime_dim=runtime_dim)

        return self._vector_cache.get(f"{notebook_id}:matrix:{table}", version, _load)

    def _vector_matrix_warm(self, db: sqlite3.Connection, notebook_id: str, table: str) -> bool:
        """True 当且仅当 `table` 的向量矩阵已经暖在 _vector_cache 里(版本匹配当前
        数据)—— 不触发 loader,只是 peek。供大库场景「加载前先问值不值得」的
        守卫使用(见 _retrieve_relations_scored)。"""
        version = self._vector_matrix_version(db, notebook_id, table)
        return self._vector_cache.peek(f"{notebook_id}:matrix:{table}", version)

    def _keyword_token_sets(self, db, notebook_id: str, objects: list,
                            bounded: bool = False) -> dict:
        """Cached {object_id: frozenset(haystack_tokens)} for keyword scoring.

        Version-keyed on (COUNT, MAX(updated_at)) of knowledge_objects so any
        payload or evidence edit invalidates the cache. Evidence text is included
        so the token set is byte-equivalent to what score_knowledge builds live.

        bounded=True(ANN 门控的有界候选路径):跳过版本 COUNT 与进程缓存,直接对
        本批 objects 现场构建(与 _load 同构建逻辑,逐字节等价——为 ≤recall 个候选
        付一次百万行 COUNT 是倒挂;该缓存对每查询候选集不同的 ANN 路径也从未命中过)。"""
        from app.services.retrieval import _tokens, _payload_text

        def _build(objs):
            out = {}
            for o in objs:
                ev_text = " ".join(
                    e.quoted_span for e in o.get("evidence", [])
                )
                out[o["id"]] = frozenset(_tokens(f"{_payload_text(o['payload'])} {ev_text}"))
            return out

        if bounded:
            return _build(objects)

        ver = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,)).fetchone()
        version = ("kwtok", ver["c"], ver["ts"])
        return self._vector_cache.get(f"{notebook_id}:kwtok", version, lambda: _build(objects))

    def _federated_rx_graph(self, active_notebook_id: str):
        """Return a federated PyDiGraph merging base notebook(s) + active notebook.

        Version-keyed, per participating notebook, on BOTH the relations
        (COUNT, MAX created_at, per-review-status counts) and the objects
        (COUNT, MAX updated_at), so an ingest into ANY of them — or an
        object-only change (status flip / payload edit bumps updated_at) — or
        an edge review flip (Track E) — triggers a rebuild even without an
        explicit eviction.  Cache key: "{active_id}:fed_rxgraph".

        Track E: relations with review_status = 'rejected' are excluded — a
        curator's rejection must not flow into reasoning via the federated
        path either.

        Each relation row is tagged with its notebook_id before passing to
        build_rx_graph so per-edge tier stamping works.
        """
        from app.services.kg.graph_reason import build_rx_graph
        with self._connect() as db:
            # Participating notebooks: active + all base notebooks (excl. active
            # if active is itself base, to avoid duplication).
            base_rows = db.execute(
                "SELECT id, tier FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,),
            ).fetchall()
            active_row = db.execute(
                "SELECT id, tier FROM notebooks WHERE id=?",
                (active_notebook_id,),
            ).fetchone()
            active_tier = active_row["tier"] if active_row else "personal"

            # Build participating list: active first, then all base notebooks.
            participants = [(active_notebook_id, active_tier)] + [
                (r["id"], r["tier"]) for r in base_rows
            ]
            # Version key: per-notebook (nb_id, relations (count, max created_at,
            # per-review-status counts), objects (count, max updated_at),
            # concept_clusters (count, max created_at)). Object coverage makes
            # object-only changes (deprecate / status flip / payload edit)
            # rebuild the graph. Track E: the per-status counts (n_rej, n_ver)
            # make a single edge flip between pending/verified/rejected change
            # the version even when (COUNT, MAX created_at) is unchanged.
            # set_edge_review's explicit evict-all-fed_rxgraph remains as
            # belt-and-braces. TD2: the
            # concept_clusters part means a rebuild_unified_kg on ANY participant
            # (which rewrites clusters without touching relations) invalidates
            # the federated graph, so the cross-doc hubs are rebuilt.
            version_parts = []
            for nb_id, _ in participants:
                rel_ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
                    "COALESCE(SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END), 0) AS n_rej, "
                    "COALESCE(SUM(CASE WHEN review_status = 'verified' THEN 1 ELSE 0 END), 0) AS n_ver "
                    "FROM knowledge_relations WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                obj_ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
                    "FROM knowledge_objects WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                clu_ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
                    "FROM concept_clusters WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                version_parts.append(
                    (nb_id, rel_ver["c"], rel_ver["ts"],
                     rel_ver["n_rej"], rel_ver["n_ver"],
                     obj_ver["c"], obj_ver["ts"],
                     clu_ver["c"], clu_ver["ts"]))
            version = ("fed_rxgraph", tuple(version_parts))

            tier_map = {nb_id: nb_tier for nb_id, nb_tier in participants}

            def _load():
                nodes: dict = {}
                all_relations: list = []
                cluster_groups: dict = {}
                ph = ",".join("?" for _ in USABLE_STATUSES)
                for nb_id, _ in participants:
                    obj_rows = db.execute(
                        "SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id = ? AND status IN ({ph})",
                        (nb_id, *USABLE_STATUSES),
                    ).fetchall()
                    for r in obj_rows:
                        p = json.loads(r["payload"] or "{}")
                        nodes[r["id"]] = {
                            "type": r["object_type"],
                            "name": p.get("name", ""),
                            "tier": tier_map.get(nb_id, "personal"),
                        }
                    # Track E: rejected edges are excluded from the federated
                    # reasoning graph too (curation feedback loop) — bare
                    # filter; the column is NOT NULL DEFAULT 'pending'
                    # (migration runs in __init__), so NULL is impossible.
                    rel_rows = db.execute(
                        "SELECT id, source_object_id, target_object_id, edge_type, evidence "
                        "FROM knowledge_relations "
                        "WHERE notebook_id = ? AND review_status != 'rejected'",
                        (nb_id,),
                    ).fetchall()
                    for r in rel_rows:
                        d = dict(r)
                        d["notebook_id"] = nb_id   # tag for tier_map lookup
                        all_relations.append(d)
                    # TD2: aggregate concept-cluster membership across ALL
                    # participants. canonical_ids are name-derived and shared
                    # across tiers (same as _ppr_graph), so a base-tier member
                    # and an active-tier member of the same canonical land in
                    # one group → build_rx_graph adds a transit-only hub that
                    # bridges them cross-document. object_ids are globally unique
                    # so no collision across notebooks.
                    for r in db.execute(
                        "SELECT canonical_id, member_object_id FROM concept_clusters "
                        "WHERE notebook_id = ?",
                        (nb_id,),
                    ).fetchall():
                        cluster_groups.setdefault(r["canonical_id"], []).append(
                            r["member_object_id"])
                # `or None`: no clusters → None-path (no kind tag, no hubs),
                # byte-identical to the pre-hub federated graph shape.
                return build_rx_graph(
                    nodes, all_relations, tier="personal", tier_map=tier_map,
                    cluster_groups=cluster_groups or None)

            return self._vector_cache.get(
                f"{active_notebook_id}:fed_rxgraph", version, _load)

    def _federated_graph_is_large(self, active_notebook_id: str) -> bool:
        """Return true if *any* graph participant is above the in-memory guard.

        Federated graph loaders include the active notebook plus every base
        notebook. Guarding only the active notebook lets a tiny personal
        notebook pull an arbitrarily large base graph into rustworkx.
        """
        with self._connect() as db:
            participants = [active_notebook_id] + [
                r["id"] for r in db.execute(
                    "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                    (active_notebook_id,),
                ).fetchall()
            ]
        return any(
            not self.notebook_copy_stats(notebook_id)["copyable"]
            for notebook_id in participants
        )

    def _mention_extra_edges(self, notebook_id: str) -> List[Tuple[str, str, float]]:
        """P2 共提桥 → 图 extra_edges:每条 mention_edges 行转一条软边
        (claim_object_id ↔ f"cluster:{concept_canonical_id}", 权重 mention_edge_weight)。
        claim 是已在图内的 KG 对象节点;cluster router 由既有 cluster_groups 机制生成
        ——mention_edges 只指向跨 >=2 源的 concept canonical(构造保证),故 router 必存在;
        偶发缺失由 build_ppr_graph / _gather_kg_graph 的 key 查表静默跳过(可接受降级)。

        一次 SELECT(数万行级),不加每边 SQL;flag 关或表空返 []。fail-open:任何异常返 []
        (派生数据,绝不因共提层崩掉图构建)。"""
        if not self.settings.mention_bridge_enabled:
            return []
        try:
            w = float(self.settings.mention_edge_weight)
            with self._connect() as db:
                rows = db.execute(
                    "SELECT claim_object_id, concept_canonical_id FROM mention_edges "
                    "WHERE notebook_id=?", (notebook_id,)).fetchall()
            return [(r["claim_object_id"], f"cluster:{r['concept_canonical_id']}", w)
                    for r in rows]
        except Exception:
            return []

    def _ppr_graph(self, notebook_id: str):
        """Build (and version-cache) the graph-mode PPR graph: KG nodes + chunk
        nodes + relation/membership/synonym(+variant) edges. Always spans active
        + base-tier notebooks (object_ids / chunk_ids are globally unique;
        concept_clusters share name-derived canonical_ids so a concept in active
        and base bridges naturally).
        返回 (G, key_to_idx, chunk_idx_to_id)。"""
        from app.services.kg.ppr import build_ppr_graph
        with self._connect() as db:
            participants = [notebook_id] + [r["id"] for r in db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (notebook_id,)).fetchall()]
            version_parts = []
            for nb in participants:
                rel_ver = db.execute("SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                                     "FROM knowledge_relations WHERE notebook_id=? "
                                     "AND review_status!='rejected'", (nb,)).fetchone()
                obj_ver = db.execute("SELECT COUNT(*) AS c, COALESCE(MAX(updated_at),'') AS ts "
                                     "FROM knowledge_objects WHERE notebook_id=?", (nb,)).fetchone()
                chunk_ver = db.execute("SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                                       "FROM chunks WHERE notebook_id=?", (nb,)).fetchone()
                clu_ver = db.execute("SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                                     "FROM concept_clusters WHERE notebook_id=?", (nb,)).fetchone()
                # mention_seq(P2 共提桥):派生 mention_edges 上次重建时的 kg_mutation_seq。
                # O(1) unified_kg_state 单行读——mention 数据变更(重建/flag/权重)须让图缓存失效。
                men_ver = db.execute("SELECT COALESCE(mention_seq,-1) AS ms FROM unified_kg_state "
                                     "WHERE notebook_id=?", (nb,)).fetchone()
                version_parts.append((nb, obj_ver["c"], obj_ver["ts"], rel_ver["c"], rel_ver["ts"],
                                      chunk_ver["c"], chunk_ver["ts"], clu_ver["c"], clu_ver["ts"],
                                      men_ver["ms"] if men_ver else -1))
        # runtime_dim 入键(T3/R4):emb_synonym 边由向量矩阵派生,切
        # EMBED_RUNTIME_DIM 后旧空间算出的图不能再服役。
        from app.services.vector_index import resolve_runtime_dim
        version = ("ppr_graph", tuple(version_parts),
                   self.settings.ppr_variant_edge_weight,
                   self.settings.ppr_emb_synonym_enabled, self.settings.ppr_emb_synonym_threshold,
                   self.settings.ppr_emb_synonym_topk,
                   resolve_runtime_dim(self.settings),
                   self.settings.mention_bridge_enabled, self.settings.mention_edge_weight,)

        def _load():
            ph = ",".join("?" for _ in USABLE_STATUSES)
            kg_nodes: Dict[str, dict] = {}
            chunk_ids: list = []
            relations: list = []
            cluster_groups: Dict[str, list] = {}
            with self._connect() as db:
                for nb in participants:
                    for r in db.execute(
                            f"SELECT id, object_type, payload FROM knowledge_objects "
                            f"WHERE notebook_id=? AND status IN ({ph})",
                            (nb, *USABLE_STATUSES)).fetchall():
                        kg_nodes[r["id"]] = {"type": r["object_type"],
                                             "name": json.loads(r["payload"] or "{}").get("name", "")}
                    for r in db.execute("SELECT source_object_id, target_object_id FROM knowledge_relations "
                                        "WHERE notebook_id=? AND review_status!='rejected'", (nb,)).fetchall():
                        relations.append(dict(r))
                    for r in db.execute("SELECT id FROM chunks WHERE notebook_id=?", (nb,)).fetchall():
                        chunk_ids.append(r["id"])
                    for r in db.execute("SELECT canonical_id, member_object_id FROM concept_clusters "
                                        "WHERE notebook_id=?", (nb,)).fetchall():
                        cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])
            memberships = [(oid, cid)
                           for nb in participants
                           for oid, cids in self._ent_chunk_map(nb).items()
                           for cid in cids]
            from app.services.kg.ppr import variant_edge_pairs
            extra_edges = variant_edge_pairs(kg_nodes, self.settings.ppr_variant_edge_weight)
            if self.settings.ppr_emb_synonym_enabled:
                from app.services.kg.ppr import emb_synonym_edges
                import numpy as np
                all_ids, mats = [], []
                with self._connect() as db:
                    for nb in participants:
                        ids, mat = self._vector_matrix(db, nb, "knowledge_embeddings", "object_id")
                        if ids and mat is not None and len(mat):
                            all_ids.extend(ids)
                            mats.append(np.asarray(mat))
                if mats:
                    extra_edges = extra_edges + emb_synonym_edges(
                        all_ids, np.vstack(mats),
                        self.settings.ppr_emb_synonym_threshold,
                        self.settings.ppr_emb_synonym_topk,
                        self.settings.ppr_emb_synonym_max_entities,
                        ef_construction=self.settings.hnsw_ef_construction)
            # P2 共提桥:每 participant 一次 mention_edges SELECT,追加 claim↔cluster 软边,
            # 让 PPR 质量在 claim 与跨文档概念 router 间流动。
            for nb in participants:
                extra_edges = extra_edges + self._mention_extra_edges(nb)
            return build_ppr_graph(kg_nodes, chunk_ids, relations, memberships, cluster_groups, extra_edges=extra_edges)

        return self._vector_cache.get(f"{notebook_id}:ppr_graph", version, _load)

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
        return self._runtime.index_projections.version_facts(notebook_id) + list(settings_tail)

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
        with self._runtime.scale_artifacts.building_lock:
            return (
                self._runtime.scale_artifacts.idle_queue.pop(notebook_id, None)
                is not None
            )

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
        from app.services.kg.viz_index import arrays_from_graph

        return arrays_from_graph(self._unified_graph_full(notebook_id, "object"))

    def _viz_arrays_from_graph(self, full: dict):
        """Compatibility delegate for callers that already derived a graph."""
        from app.services.kg.viz_index import arrays_from_graph

        return arrays_from_graph(full)

    def _derive_object_graph_lite(self, notebook_id: str) -> dict:
        """Compatibility delegate to the builder's lite DB projection."""
        return self._runtime.scale_builder._derive_object_graph_lite(notebook_id)

    def _viz_index_dir(self, notebook_id: str) -> str:
        return str(self._runtime.scale_artifact_store.viz_dir(notebook_id))

    def build_viz_index(self, notebook_id: str) -> Optional[dict]:
        """Compatibility delegate to the runtime-owned viz builder."""
        return self._runtime.scale_artifacts.build_viz(notebook_id)

    def _active_kg_delta(self, notebook_id: str):
        """Gather the ACTIVE/self notebook's KG delta for splicing onto a base or
        self scale index. Delegates to _gather_kg_graph (shared with
        build_scale_index) so node/edge conventions are guaranteed identical.

        When the notebook itself is already scale-indexed, only the post-watermark
        sources (self-delta) are gathered — the index core is already represented
        by its own CSR participant, so re-splicing the whole self KG would
        double-count. Otherwise the whole notebook is gathered (unchanged).
        Returns (active_node_ids, active_edges, active_chunk_ids).
        """
        # 廉价门控早退:indexed(磁盘有 manifest)且 flag 关时,图基底只含已索引部分,
        # 直接返空——省掉 _index_delta 对 delta_sources 的分批 COUNT(生产 48,739 源、
        # 55 批,结果本会被丢弃)。gate 结果与「先 _index_delta 再判 indexed」一致:
        # _index_delta 的 indexed 恰是 manifest 是否存在。
        scale_manifest = (self._runtime.scale_artifact_store.scale_dir(notebook_id)
                          / "manifest.json")
        if (scale_manifest.exists()
                and not self.settings.scale_search_include_delta):
            # 同一原则第四处:已索引库的图基底只含已索引部分(核心 CSR 本身),水位后
            # delta 由 auto-fold 收进索引后自然可达。未索引的 active 小库(下方
            # src=None 整库 gather)是 two-tier 联邦的 active 层,不是 delta,不受门控。
            return [], [], []
        delta = self._index_delta(notebook_id)
        src = delta["delta_sources"] if delta["indexed"] else None
        node_ids, edges, chunk_ids, _kg_node_ids, _membership_counts = \
            self._gather_kg_graph(notebook_id, source_ids=src)
        return node_ids, edges, chunk_ids

    def _delta_vector_matrix(self, db: sqlite3.Connection, notebook_id: str,
                             table: str, id_col: str, node_ids: List[str]):
        """Like `_vector_matrix` but scoped to exactly `node_ids` — for hot paths
        that only need vectors for a bounded delta node set (e.g. the active KG
        delta spliced onto a base scale index), not the notebook's FULL embedding
        table. Loads via chunked `IN (...)` (SQLite variable-count safe, see
        `_IN_CHUNK`) instead of `_vector_matrix`'s `WHERE notebook_id = ?` (which
        would pull every row in the notebook — 490k×1024 when active IS the
        indexed big lib). Not version-cached (the delta itself is already
        version-scoped by the caller's cache key) — bounded by len(node_ids), so
        a fresh per-call load is cheap. Returns (ids, matrix) via the same
        `build_matrix` path, so output is bit-identical to filtering the full
        `_vector_matrix` result down to `node_ids` (same decode, same
        normalization, same skip-on-invalid semantics)."""
        from app.services.vector_index import build_matrix
        if not node_ids:
            return [], None

        def _rows():
            ids = list(dict.fromkeys(node_ids))  # de-dupe, preserve order
            for i in range(0, len(ids), self._IN_CHUNK):
                batch = ids[i:i + self._IN_CHUNK]
                ph = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT {id_col} AS vid, vector FROM {table} "
                    f"WHERE notebook_id = ? AND {id_col} IN ({ph})",
                    (notebook_id, *batch),
                ).fetchall()
                by_id = {r["vid"]: r["vector"] for r in rows}
                for nid in batch:
                    if nid in by_id:
                        yield nid, by_id[nid]

        return build_matrix(_rows())

    def _scale_xlayer_bridge_edges(self, notebook_id: str, base_indexes,
                                   active_edges, active_node_ids=None):
        """Append bounded cross-layer synonym bridge edges to active_edges.

        For each active KG node vector, query each base ANN — if cosine sim >=
        threshold and the base node id differs from the active node id (no
        duplicate of the exact-id unification splice_active already does), add
        an undirected edge (active↔base, weight=cosine). The active node lands
        in combined_ids via splice_active (new id); the base node is already in
        combined_ids — so build_transition keeps both endpoints.

        Complexity: |active_kg_nodes| × topk per base (small; no base matmul).
        NOTE: this depends only on the active node embedding matrix and the base
        ANN indexes — NOT on the query vector — so it can live inside the cached
        combined-graph loader. Returns the (possibly extended) active_edges list.

        active_node_ids: the ACTIVE DELTA node id set (from `_active_kg_delta`,
        i.e. the same nodes being spliced onto the combined graph this call).
        None = caller didn't have the delta id set → the pre-delta-scoping
        full-table load is used for EVERY participant (safety fallback).

        Iteration-domain dispatch is PER PARTICIPANT (semantic fix — the
        original delta-only scoping dropped edges in Case B below):

        - participant == self (the active notebook's own scale index,
          P0-00): domain = the DELTA node set. Core (pre-watermark) nodes'
          synonym edges were already baked into self's CSR by
          build_scale_index — re-bridging them into their own index would
          be redundant; only the not-yet-indexed delta nodes need query-time
          bridges into the self core.
        - participant != self (an EXTERNAL base notebook): domain = ALL
          active nodes (full `_vector_matrix` load — the query-path,
          version-cached loader; consistent with the pre-delta-scoping
          behavior for this participant class). Case B: when self is
          indexed AND an external base exists, self's core nodes are NOT in
          the delta, but their cross-layer bridges to the external base can
          ONLY be computed at query time — build_scale_index builds each
          library in isolation and never bakes cross-library bridges, and
          exact-id unification can't help (the two libraries mint different
          object ids for the same concept). Restricting the external-base
          domain to the delta silently severed every core-concept↔external-
          base synonym path in scale_ppr.

        Net effect: the current production shape (one big self-indexed
        library, NO external base participants) keeps the full C1 saving —
        no full-matrix load at all; layered-federation deployments get
        their cross-layer connectivity back. Both loads happen inside the
        version-cached combined-graph loader (once per version, not per
        query)."""
        import numpy as np
        if not self.settings.ppr_emb_synonym_enabled:
            return active_edges
        _syn_k = self.settings.ppr_emb_synonym_topk
        _syn_thr = self.settings.ppr_emb_synonym_threshold

        _participants = [(bid, bidx) for bid, bidx in base_indexes
                         if bidx.ann_labels]
        if not _participants:
            return active_edges
        # Which vector domains do we actually need this call?
        _need_full = (active_node_ids is None) or any(
            bid != notebook_id for bid, _ in _participants)
        _need_delta = (active_node_ids is not None and len(active_node_ids) > 0
                       and any(bid == notebook_id for bid, _ in _participants))

        _full_ids = _full_mat = None
        _delta_ids = _delta_mat = None
        with self._connect() as _db:
            if _need_full:
                _full_ids, _full_mat = self._vector_matrix(
                    _db, notebook_id, "knowledge_embeddings", "object_id")
            if _need_delta:
                _delta_ids, _delta_mat = self._delta_vector_matrix(
                    _db, notebook_id, "knowledge_embeddings", "object_id",
                    list(active_node_ids))

        _bridge_edges: List[Tuple[str, str, float]] = []
        for _bid, _bidx in _participants:
            if _bid == notebook_id and active_node_ids is not None:
                _a_ids, _a_mat = _delta_ids, _delta_mat   # self → delta domain
            else:
                _a_ids, _a_mat = _full_ids, _full_mat     # external base → full domain
            if _a_ids is None or _a_mat is None or len(_a_mat) == 0:
                continue
            _a_mat_arr = np.asarray(_a_mat, dtype=np.float32)
            _dim = int(_bidx.manifest.get("dim", _a_mat_arr.shape[1]))
            if _dim != _a_mat_arr.shape[1]:
                continue
            _ann = self._open_scale_ann(_bidx, "kg")
            if _ann is None:
                continue
            _ann.set_ef(max(_syn_k + 1, 50))
            for _ai, _a_id in enumerate(_a_ids):
                _avec = _a_mat_arr[_ai]
                try:
                    _k = min(_syn_k, len(_bidx.ann_labels))
                    _labs, _dists = _ann.knn_query(_avec, k=_k)
                except Exception as _exc:  # noqa: BLE001 — fail-open
                    self._note_model_error(
                        "scale_ppr_xbridge_query",
                        self.settings.embed_model, _exc)
                    continue
                for _lab, _dist in zip(_labs[0], _dists[0]):
                    _base_nid = _bidx.ann_labels[int(_lab)]
                    if _base_nid == _a_id:
                        continue  # exact-id: already unified by splice_active
                    _sim = max(0.0, 1.0 - float(_dist))
                    if _sim >= _syn_thr:
                        _bridge_edges.append((_a_id, _base_nid, _sim))
                        _bridge_edges.append((_base_nid, _a_id, _sim))
        if _bridge_edges:
            active_edges = list(active_edges) + _bridge_edges
        return active_edges

    def _scale_combined_graph(self, notebook_id: str, base_indexes):
        """Build (and version-cache) the query-INDEPENDENT combined base⊕active
        CSR graph used by scale_ppr. Version key = each base's manifest version +
        (conditionally) the active notebook's _scale_index_version — see the
        active_ver comment below for exactly when it is included — so the cache
        invalidates whenever a participant's KG/embeddings/settings change in a
        way that can affect the combined graph. Same _vector_cache /
        version-loader pattern as _ppr_graph.

        Returns dict: combined_ids, combined_A, combined_index,
        combined_chunk_ids, combined_idf.
        """
        import numpy as np
        from app.services.kg import scale_index as si

        base_ver = tuple(
            (bid, tuple(idx.manifest.get("version", [])))
            for bid, idx in base_indexes)
        active_indexed = (self._runtime.scale_artifact_store.scale_dir(notebook_id)
                          / "manifest.json").exists()
        # Drop the churning active_ver from the key ONLY when the active notebook is
        # ITSELF indexed and delta is gated off: then _active_kg_delta early-returns
        # empty (its gate = active manifest exists), so the combined graph is fully
        # determined by participants' on-disk manifest versions (in base_ver), and the
        # ingestion churn (kg_mutation_seq) is irrelevant. If the active notebook is
        # UN-indexed (two-tier federation: un-indexed active over a base index),
        # _active_kg_delta gathers the whole active KG into the graph, so its mutations
        # MUST stay in the key — keep active_ver.
        active_ver = (None
                      if (active_indexed and not self.settings.scale_search_include_delta)
                      else tuple(self._scale_index_version(notebook_id)))
        version = ("scale_combined", base_ver, active_ver,
                   bool(self.settings.scale_search_include_delta),
                   "f32" if self.settings.ppr_float32 else "f64")

        def _load():
            # 2. Combined graph: start from the first base index, splice remaining
            #    base indexes' nodes/edges, then splice the active delta.
            first_id, first = base_indexes[0]
            combined_ids = list(first.node_ids)
            combined_A = first.transition
            # combined_idf aligned to combined node order: base.idf for base nodes,
            # 1.0 for any node introduced later (extra base nodes / active / hubs).
            combined_idf_map: Dict[str, float] = {
                nid: float(first.idf[i]) for i, nid in enumerate(first.node_ids)
            }
            # base chunk node ids of the FIRST base (raw chunk_id convention).
            combined_chunk_ids: set = {
                first.node_ids[i] for i in first.chunk_index
                if 0 <= int(i) < len(first.node_ids)
            }
            for bid, idx in base_indexes[1:]:
                # reconstruct this base's edges from its CSR and splice as "active"-style
                extra_ids, extra_A = si.splice_active(
                    combined_ids, combined_A, list(idx.node_ids),
                    si.csr_to_edges(idx.node_ids, idx.transition))
                combined_ids, combined_A = extra_ids, extra_A
                for i, nid in enumerate(idx.node_ids):
                    combined_idf_map.setdefault(nid, float(idx.idf[i]))
                for i in idx.chunk_index:
                    if 0 <= int(i) < len(idx.node_ids):
                        combined_chunk_ids.add(idx.node_ids[int(i)])

            active_node_ids, active_edges, active_chunk_ids = \
                self._active_kg_delta(notebook_id)

            # 2b. Cross-layer synonym bridge (query-independent; see helper).
            # Delta-scoped: only active_node_ids' vectors are needed (they are
            # exactly the nodes being spliced this call) — never the full
            # notebook embedding table (see _scale_xlayer_bridge_edges docstring).
            active_edges = self._scale_xlayer_bridge_edges(
                notebook_id, base_indexes, active_edges,
                active_node_ids=active_node_ids)

            if active_node_ids or active_edges:
                combined_ids, combined_A = si.splice_active(
                    combined_ids, combined_A, active_node_ids, active_edges)
            combined_index = {nid: i for i, nid in enumerate(combined_ids)}
            combined_chunk_ids.update(active_chunk_ids)

            # combined_idf array aligned to combined_ids (1.0 for unknown nodes).
            combined_idf = np.array(
                [combined_idf_map.get(nid, 1.0) for nid in combined_ids],
                dtype=np.float64)

            # P0-B: PPR 迭代全程 float32(SpMV 带宽减半≈2x;top-k 稳定,长尾分数
            # 波动已获用户接受)。flag 已掺进上面的 version key,翻转即失效缓存。
            if self.settings.ppr_float32:
                combined_A = combined_A.astype(np.float32)

            return {
                "combined_ids": combined_ids,
                "combined_A": combined_A,
                "combined_index": combined_index,
                "combined_chunk_ids": combined_chunk_ids,
                "combined_idf": combined_idf,
            }

        return self._vector_cache.get(
            f"{notebook_id}:scale_combined", version, _load)

    def scale_ppr(self, notebook_id: str, question: str) -> List[Tuple[str, float]]:
        """规模化 PPR:base 有持久化 scale 索引时,用 ANN 取 base KG 种子(避免
        4GB 暴力 matmul)+ 把 active 增量 splice 进 base CSR 图 → personalized_ppr
        → chunk 排名。返回 [(chunk_id, 归一分 0..1), ...] 降序,与 run_ppr 同形。
        无可用 base 索引 / reset 全零 / 无 chunk 节点 → [](调用方回退 rustworkx 路径)。

        节点 id 约定与 build_scale_index 完全一致:KG 节点 key=object_id,
        chunk 节点 key=原始 chunk_id(非 "chunk:" 前缀),cluster hub key=
        "cluster:{canonical_id}"。
        """
        import numpy as np
        from app.services.kg import scale_index as si
        from app.services.vector_index import query_sims

        # 1. Base set: tier='base' notebooks (excluding active) with a valid index.
        #    v1: support the common SINGLE base case; if >1 base index exists,
        #    splice them sequentially onto the combined graph.
        with self._connect() as db:
            base_ids = [r["id"] for r in db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (notebook_id,)).fetchall()]
        base_indexes = [(bid, self._scale_index(bid, allow_stale=True)) for bid in base_ids]
        base_indexes = [(bid, idx) for bid, idx in base_indexes if idx is not None]
        # P0-00: 自身若有(含 stale)索引,把 self 也当作 participant(self CSR=substrate,
        # self ANN=种子源)。active splice 由 _active_kg_delta 自动收窄为 self-delta。
        self_idx = self._scale_index(notebook_id, allow_stale=True)
        if self_idx is not None:
            base_indexes = base_indexes + [(notebook_id, self_idx)]
        if not base_indexes:
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "no_participants",
            })
            return []

        # 2. Combined graph: base⊕active spliced CSR.  This graph is INDEPENDENT
        #    of the query (the cross-layer synonym bridge uses active node vectors,
        #    not the query vector), so it is version-cached and reused across
        #    consecutive queries against the same active notebook — the splice cost
        #    is paid once per (base versions × active version) instead of per query.
        graph = self._scale_combined_graph(notebook_id, base_indexes)
        combined_ids = graph["combined_ids"]
        combined_A = graph["combined_A"]
        combined_index = graph["combined_index"]
        combined_chunk_ids = graph["combined_chunk_ids"]
        combined_idf = graph["combined_idf"]

        # 3. Reset vector. dtype 以 combined_A 为准(而非 settings.ppr_float32 本身)——
        #    缓存里的图可能是翻转前构建的旧 dtype,对齐它才不会在 dot() 里被隐式提升。
        reset = np.zeros(len(combined_ids), dtype=combined_A.dtype)
        qvec = self._embed_query(question)

        # Bailout observability (Fix 2): 逐种子源计数,仅在 reset 全零(zero_reset)
        # bail 时才附带到诊断事件,成功路径零额外开销(计数本身是几个 int 加法,可忽略)。
        ann_seeds = 0
        ann_sources_skipped = 0

        # 3a. Base KG seeds via ANN (no 4GB matmul): query each base hnswlib index.
        if qvec is not None:
            qarr = np.asarray(qvec, dtype=np.float32)
            top_n = self.settings.ppr_kg_seed_top_n
            for bid, idx in base_indexes:
                if not idx.ann_labels:
                    continue
                dim = int(idx.manifest.get("dim", qarr.shape[0]))
                if dim != qarr.shape[0]:
                    ann_sources_skipped += 1
                    continue
                ann = self._open_scale_ann(idx, "kg")
                if ann is None:
                    ann_sources_skipped += 1
                    continue
                try:
                    ann.set_ef(max(top_n + 1, 50))
                    k = min(top_n, len(idx.ann_labels))
                    labels, distances = ann.knn_query(qarr, k=k)
                except Exception as exc:  # noqa: BLE001 — fail-open per seed source
                    self._note_model_error("scale_ppr_ann", self.settings.embed_model, exc)
                    continue
                for lab, dist in zip(labels[0], distances[0]):
                    node_id = idx.ann_labels[int(lab)]
                    ci = combined_index.get(node_id)
                    if ci is None:
                        continue
                    sim = max(0.0, 1.0 - float(dist))  # cosine distance → similarity
                    if sim > 0:
                        reset[ci] += sim * combined_idf[ci]
                        ann_seeds += 1

        # 3b. Active KG seeds — 仅当 self 没有 scale 索引(self_idx is None,即
        #     active 是真正的小 delta 库)时才做有界暴力余弦。self 已建索引的
        #     P0-00 self-participant 情形(用户直接查询已索引的大库本身),self
        #     的种子已在 3a 经它自己的 hnsw ANN 产出;这里再 _vector_matrix 全量
        #     加载同一库的 knowledge_embeddings(生产 49万×1024:未 BLOB 化 =
        #     ~36 分钟 JSON 解析 + 数 GB;BLOB 化也要 ~2GB 常驻)是纯重复,且违反
        #     成本分离不变量(base/已索引库离线 ANN,暴力只留给小 active)→ 跳过。
        active_seeds = 0
        if qvec is not None and self_idx is None:
            with self._connect() as db:
                a_ids, a_mat = self._vector_matrix(
                    db, notebook_id, "knowledge_embeddings", "object_id")
            sims = query_sims(qvec, list(a_ids) if a_ids else [], a_mat) \
                if a_ids is not None else {}
            if sims:
                top = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)[
                    : self.settings.ppr_kg_seed_top_n]
                for oid, sim in top:
                    ci = combined_index.get(oid)
                    if ci is not None and sim > 0:
                        reset[ci] += float(sim) * combined_idf[ci]
                        active_seeds += 1

        # 3c. Chunk seeds: ACTIVE notebook dense chunk seeds (raw chunk_id key —
        #     matches build's chunk node convention). NOTE (v1 follow-up): base
        #     notebook chunk dense seeds via a chunk-vector ANN are deliberately
        #     out of SP2 scope; base CHUNKS still receive PPR mass via graph
        #     propagation from base KG ANN seeds, so they remain rankable.
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, question)
        pw = self.settings.ppr_passage_node_weight
        chunk_seeds = 0
        for c in scored[: self.settings.ppr_chunk_seed_top_n]:
            ci = combined_index.get(c.chunk_id)  # raw chunk_id, no "chunk:" prefix
            if ci is not None and c.relevance > 0:
                reset[ci] += float(c.relevance) * pw
                chunk_seeds += 1

        # 4. PPR.
        if reset.sum() <= 0:
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "zero_reset",
                "ann_seeds": ann_seeds,
                "active_seeds": active_seeds,
                "chunk_seeds": chunk_seeds,
                "embed_ok": bool(qvec),
                "ann_sources_skipped": ann_sources_skipped,
            })
            return []
        t_ppr0 = time.perf_counter()
        _ppr_stats: dict = {}
        x = si.personalized_ppr(combined_A, reset, damping=self.settings.ppr_damping,
                                tol=self.settings.ppr_tol, stats=_ppr_stats)
        if x.sum() <= 0:
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "zero_ppr_mass",
            })
            return []

        # 5. Chunk rankings: collect chunk node scores, min-max normalize into
        #    [0,1] (mirror run_ppr exactly), sort desc. chunk node key is the raw
        #    chunk_id, so it IS the downstream chunk-fetch id (no prefix to strip).
        raw = []
        for cid in combined_chunk_ids:
            ci = combined_index.get(cid)
            if ci is not None:
                raw.append((cid, float(x[ci])))
        if not raw:
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "no_chunk_nodes",
            })
            return []
        vals = [s for _, s in raw]
        lo, hi = min(vals), max(vals)
        span = hi - lo
        norm = [(cid, (s - lo) / span if span > 0 else 0.0) for cid, s in raw]
        norm.sort(key=lambda kv: kv[1], reverse=True)
        self.event_log.emit({
            "kind": "scale_ppr_done", "notebook_id": notebook_id,
            "iters": _ppr_stats.get("iters", -1),
            "ppr_ms": round((time.perf_counter() - t_ppr0) * 1000),
            "nodes": len(combined_ids), "seeds": ann_seeds + active_seeds + chunk_seeds,
            "chunks_ranked": len(norm),
        })
        return norm

    def _ppr_retrieve(self, notebook_id: str, question: str) -> List["RetrievedChunk"]:
        """HippoRAG 式 PPR 检索:KG 种子 + chunk 种子 → reset 向量 → PPR →
        取 chunk 节点分数。返回前 ppr_top_chunks 的 RetrievedChunk(relevance=
        归一 PPR 分,守 [0,1])。无 KG/无 chunk 时返回 []。

        分发:若存在有效 base scale 索引,走规模化 ANN-种子 + CSR PPR 路径
        (scale_ppr);否则字节不变地回退到原 rustworkx 路径。

        大库守卫:scale_ppr 返回 [] 时,原本无条件回退 self._ppr_graph(notebook_id)
        = 全量 rustworkx 图构建(百万级节点/边,Python 边循环 → 数十分钟 + 数 GB 内存,
        曾在 1.13M 节点库上导致 reasoning 模式冻结)。大库(与分享/拷贝阈值同一「大」
        定义,not notebook_copy_stats()["copyable"])下拒绝构建该图,发
        ppr_fallback_refused 事件后返回 []——调用方(reasoning 种子/agent 动作、
        chunk 模式三路 mix、ask_graph PPR 分支)均已对 [] 容错降级。小库保留旧
        回退路径,字节不变。"""
        from app.services.kg.ppr import run_ppr
        ranked = self.scale_ppr(notebook_id, question)
        if not ranked:
            if self._federated_graph_is_large(notebook_id):
                self.event_log.emit({
                    "kind": "ppr_fallback_refused",
                    "notebook_id": notebook_id,
                    "reason": "large_notebook",
                })
                return []
            G, key_to_idx, chunk_idx_to_id = self._ppr_graph(notebook_id)
            if G.num_nodes() == 0 or not chunk_idx_to_id:
                return []
            reset = self._ppr_reset_vector(notebook_id, question, key_to_idx)
            if not reset:
                return []
            ranked = run_ppr(G, chunk_idx_to_id, reset, damping=self.settings.ppr_damping)
        ranked = ranked[: self.settings.ppr_top_chunks]
        if not ranked:
            return []

        score_map = dict(ranked)
        with self._connect() as db:
            ph = ",".join("?" for _ in score_map)
            rows = db.execute(
                f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
                f"c.notebook_id AS chunk_notebook_id, s.title AS source_title "
                f"FROM chunks c JOIN sources s ON s.id=c.source_id "
                f"WHERE c.id IN ({ph})", list(score_map)).fetchall()
        from app.services.retrieval import RetrievedChunk
        # combined_chunk_ids (scale_ppr) spans base ⊕ active — a chunk here can
        # belong to a base notebook even though this call is scoped to
        # `notebook_id`. Tag each with its real origin so citation-building can
        # resolve tier correctly (see Citation.tier).
        out = [RetrievedChunk(
            chunk_id=r["id"], source_id=r["source_id"], source_title=r["source_title"],
            section_path=r["section_path"], text=r["text"],
            element_ids=json.loads(r["element_ids"] or "[]"),
            relevance=score_map[r["id"]],
            notebook_id=r["chunk_notebook_id"]) for r in rows]
        out.sort(key=lambda c: c.relevance, reverse=True)
        return out

    def _ppr_reset_vector(self, notebook_id: str, question: str,
                          key_to_idx: Dict[str, int]) -> Dict[int, float]:
        """构造 PPR 的 reset/personalization 向量:KG 实体种子(federated_retrieve)
        + chunk 种子(dense)。返回 {vertex_idx: weight}。仅 graph 模式 PPR 路径调用。"""
        reset: Dict[int, float] = {}
        ent_chunk_map = self._ent_chunk_map(notebook_id)
        kg_hits = self.federated_retrieve(notebook_id, question)[: self.settings.ppr_kg_seed_top_n]
        if self.settings.ppr_fact_rerank_enabled:
            kg_hits = self._ppr_fact_rerank(question, kg_hits)
        for h in kg_hits:
            idx = key_to_idx.get(h.object_id)
            if idx is not None and h.relevance > 0:
                # 大众概念(出现在很多 chunk)降权,避免 Transformer/KV cache 灌满 PPR。
                w = float(h.relevance) / max(1, len(ent_chunk_map.get(h.object_id) or ()))
                reset[idx] = reset.get(idx, 0.0) + w
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, question)
        pw = self.settings.ppr_passage_node_weight
        for c in scored[: self.settings.ppr_chunk_seed_top_n]:
            idx = key_to_idx.get(f"chunk:{c.chunk_id}")
            if idx is not None and c.relevance > 0:
                reset[idx] = reset.get(idx, 0.0) + float(c.relevance) * pw
        return reset

    _PPR_RERANK_SCHEMA = '{"relevant_ids": ["..."]}'

    def _ppr_fact_rerank(self, question: str, kg_hits: list) -> list:
        """Recognition memory:LLM 过滤候选 KG 种子,只留与 question 相关的。
        fail-open:LLM 未配/报错/非法返回/过滤后为空 → 原样返回 kg_hits(绝不因
        rerank 失败而清空种子)。复用 reasoning_llm_client。"""
        client = self.reasoning_llm_client
        if not kg_hits or not getattr(client, "configured", False):
            return kg_hits
        lines = []
        for h in kg_hits:
            name = str(h.payload.get("name", "")).strip()
            snippet = h.evidence[0].quoted_span[:80] if h.evidence else ""
            lines.append(f"{h.object_id} - {name} - {snippet}")
        prompt = (
            "You are filtering knowledge-graph entries for relevance to a user question "
            "(recognition memory). Keep an entry only if it could help answer the question; "
            "when unsure, KEEP it.\n\n"
            f"Question: {question}\n\nCandidates (id - name - snippet):\n"
            + "\n".join(lines)
            + '\n\nReturn JSON only: {"relevant_ids": [ids to keep]}.'
        )
        try:
            raw = client.chat_json(
                [{"role": "user", "content": prompt}], self._PPR_RERANK_SCHEMA,
                timeout=self.settings.reasoning_timeout_seconds, max_retries=1)
            data = json.loads(raw)
            ids = data.get("relevant_ids") if isinstance(data, dict) else None
            if not isinstance(ids, list):
                return kg_hits
            keep = {str(i) for i in ids}
            kept = [h for h in kg_hits if h.object_id in keep]
            return kept or kg_hits   # 过滤后为空 → fail-open(LLM 过度过滤)
        except Exception as exc:
            self._note_model_error(
                "ppr_fact_rerank",
                self.settings.reasoning_llm_model or self.settings.openai_compat_model, exc)
            return kg_hits

    def _rule_card(self, item: RetrievedKnowledge) -> RuleCard:
        payload = item.payload
        applies_to = payload.get("applies_to")
        if isinstance(applies_to, list):
            applies_list = [str(value) for value in applies_to if str(value).strip()]
        elif applies_to:
            applies_list = [str(applies_to)]
        else:
            applies_list = []
        return RuleCard(
            id=item.object_id,
            title=str(payload.get("title", "")),
            statement=str(payload.get("statement", "")),
            applies_to=applies_list,
            recommendation=str(payload.get("recommendation", "")),
            risk_if_ignored=str(payload.get("risk_if_ignored", "")),
            severity=str(payload.get("severity", "medium")),
            status=item.status or "approved",
            owner=item.owner,
            last_reviewed=item.last_reviewed,
            evidence=item.evidence,
        )

    @staticmethod
    def _as_retrieved(obj: dict, object_type: str) -> RetrievedKnowledge:
        return RetrievedKnowledge(
            object_id=obj["id"],
            object_type=object_type,
            payload=obj["payload"],
            evidence=obj["evidence"],
            status=obj.get("status", "approved"),
            owner=obj.get("owner", ""),
            last_reviewed=obj.get("last_reviewed", ""),
        )

    def _tier_map_for(self, notebook_ids: Iterable[str]) -> Dict[str, str]:
        """Batch-resolve {notebook_id: tier} in one query (mirrors
        federated_retrieve's tier_map pass). Used by citation-building sites
        that fan across several notebooks (e.g. PPR chunks that can span
        base ⊕ active) so tier lookup is O(distinct notebooks), not O(citations).
        Missing/unknown ids default to "personal" (safe: matches Citation.tier's
        own default, never silently mislabels a source as authoritative base)."""
        ids = {nid for nid in notebook_ids if nid}
        if not ids:
            return {}
        tier_map: Dict[str, str] = {}
        with self._connect() as db:
            for batch in self._in_batches(ids):
                ph = ",".join("?" for _ in batch)
                for row in db.execute(
                        f"SELECT id, tier FROM notebooks WHERE id IN ({ph})", batch):
                    tier_map[row["id"]] = row["tier"] or "personal"
        return tier_map

    def _citations_from(
        self,
        items: List[RetrievedKnowledge],
        valid_element_ids: set,
        label: str,
    ) -> List[Citation]:
        citations: List[Citation] = []
        for item in items:
            tier = getattr(item, "tier", "personal") or "personal"
            for evidence in item.evidence:
                if evidence.element_id and evidence.element_id not in valid_element_ids:
                    continue
                citations.append(_citation(label, evidence, tier))
        return citations

    # SQLite default SQLITE_MAX_VARIABLE_NUMBER-safe chunk size for `IN (...)`
    # placeholder lists (mirrors the ~900 convention used elsewhere in this file).
    _IN_CHUNK = 900

    def _in_batches(self, ids):
        """把 id 列表切成 ≤_IN_CHUNK 的批(去重保序)。所有把 id 列表内联成
        SQL IN 占位符的 delta 位点必须经它——SQLite 3.32+ 变量上限 32,766,
        生产 48,739 delta source 已真实打爆(too many SQL variables)。"""
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), self._IN_CHUNK):
            yield ids[i:i + self._IN_CHUNK]

    def _relations_with_names(self, db: sqlite3.Connection, notebook_id: str,
                              relation_ids: Optional[List[str]] = None) -> List[dict]:
        """关系 + 两端实体名 + evidence,预构建 keyword/embed 文本。JOIN 丢弃悬空边
        (端点不在 knowledge_objects),与图节点过滤一致。

        relation_ids=None(默认): 全量(现状,仍是 _backfill_relation_embeddings 等
        维护路径的正确语义 — 全库补全向量必须看到每一条关系)。
        relation_ids=[...]: 只 JOIN 这些 id(P0-1/2 候选界定 — 热路径调用方先按
        向量 sim 定好候选集,这里只 hydrate 需要打分的那几行文本),chunk 在
        `_IN_CHUNK` 防止超 SQLite 变量数上限;空列表直接返回 []。"""
        from app.services.retrieval import relation_embed_text, _payload_text
        base_sql = (
            "SELECT r.id AS id, r.source_object_id AS s, r.target_object_id AS t, "
            "r.edge_type AS et, r.evidence AS ev, so.payload AS sp, tp.payload AS tpl "
            "FROM knowledge_relations r "
            "JOIN knowledge_objects so ON so.id = r.source_object_id "
            "JOIN knowledge_objects tp ON tp.id = r.target_object_id "
            "WHERE r.notebook_id = ?"
        )
        if relation_ids is not None:
            if not relation_ids:
                return []
            rows = []
            ids = list(relation_ids)
            for i in range(0, len(ids), self._IN_CHUNK):
                batch = ids[i:i + self._IN_CHUNK]
                ph = ",".join("?" for _ in batch)
                rows.extend(db.execute(
                    base_sql + f" AND r.id IN ({ph})",
                    (notebook_id, *batch)).fetchall())
        else:
            rows = db.execute(base_sql, (notebook_id,)).fetchall()
        out = []
        for r in rows:
            spans = [e.get("quoted_span", "") for e in json.loads(r["ev"] or "[]")
                     if isinstance(e, dict)]
            src_name = _payload_text(json.loads(r["sp"] or "{}"))[:80]
            tgt_name = _payload_text(json.loads(r["tpl"] or "{}"))[:80]
            out.append({
                "id": r["id"], "source_object_id": r["s"], "target_object_id": r["t"],
                "edge_type": r["et"],
                "text": relation_embed_text(src_name, r["et"], tgt_name, spans),
            })
        return out

    def _relation_ann_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        """ANN 核候选(idx.relation_ann_path=relation_embeddings)⊕ delta 关系暴力。
        镜像 _kg_object_candidates,relation 版。返回 {relation_id: sim∈[0,1]}。
        fail-open 返回 {} 让上层退回全量矩阵/guard 路径。"""
        import numpy as np
        from app.services.vector_index import build_matrix, query_sims
        sims: dict = {}
        labels = getattr(idx, "relation_ann_labels", None)
        if labels and query_vector is not None:
            qarr = np.asarray(query_vector, dtype=np.float32)
            dim = int(idx.manifest.get("dim", qarr.shape[0]))
            if dim == qarr.shape[0]:
                ann = self._open_scale_ann(idx, "relation")
                if ann is None:
                    return {}
                try:
                    ann.set_ef(max(recall + 1, 64))
                    k = min(recall, len(labels))
                    labs, dists = ann.knn_query(qarr, k=k)
                    for l, d in zip(labs[0], dists[0]):
                        sims[labels[int(l)]] = max(0.0, 1.0 - float(d))
                except Exception as exc:  # noqa: BLE001 — fail-open
                    self._note_model_error("relation_ann_query", self.settings.embed_model, exc)
                    return {}
        # ⊕ delta 关系(水位后 source)暴力 —— opt-in(scale_search_include_delta,
        # 默认关):与 chunk/KG对象侧同一原则「已索引的库只检索已索引部分」,delta
        # 由 scale_auto_fold_on_add 的增量 fold 收进索引(最终一致)。True 时保持
        # 强一致暴力(small id-scoped 向量加载,IN-chunked 由 build_matrix 自然
        # 承载,这里量级由 delta 决定,通常远小于全库,但仍随 delta 无界增长)。
        if self.settings.scale_search_include_delta:
            try:
                delta = self._index_delta(notebook_id)
                if delta["delta_sources"] and query_vector is not None:
                    drows = []
                    with self._connect() as db:
                        for batch in self._in_batches(delta["delta_sources"]):
                            ph_s = ",".join("?" for _ in batch)
                            drows.extend(db.execute(
                                f"SELECT relation_id AS vid, vector FROM relation_embeddings "
                                f"WHERE notebook_id=? AND relation_id IN "
                                f"(SELECT id FROM knowledge_relations WHERE notebook_id=? AND source_id IN ({ph_s}))",
                                (notebook_id, notebook_id, *batch)).fetchall())
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    for rid, s in (query_sims(query_vector, d_ids, d_mat) if d_ids else {}).items():
                        sims[rid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
                self._note_model_error("relation_ann_delta", self.settings.embed_model, exc)
        return sims

    def _retrieve_relations_scored(self, notebook_id: str, query: str) -> List["RetrievedRelation"]:
        """对 notebook 关系按 query 打分(关键词 + 关系索引语义)。镜像 _retrieve_scored;
        关系矩阵是独立索引(dual-index 分离)。

        relation-ann task:候选界定优先级从高到低——
          ① relations 表空 → 早退 [](原有语义不变)。
          ② 持久化 relation ANN(self scale index allow_stale=True 且
             has_relation_ann)存在 → ANN 核 ⊕ delta 暴力
             (_relation_ann_candidates,镜像 _kg_object_candidates),只
             hydrate top-K 候选文本做关键词+语义融合打分。knn_query 的 k 语义
             与既有 relation_recall 一致(见下)。这是大库常态路径——ANN 侧路
             让下面的冷矩阵守卫从常态退位为「无 ANN 大库」的最后兜底。
          ③ 无 ANN(或 ANN fail-open)→ 大库冷矩阵守卫(见下方「生产事故修复」
             段,语义原样保留:not copyable + _vector_matrix_warm peek 判冷 →
             skip + relation_scoring_skipped 事件 + 返回 []);小库/已热 →
             现状全量矩阵 top_k_sims 路径,字节不变。
          ④ 无向量覆盖 → 关键词全量 JOIN 分支不动。

        P0-1/2 候选界定(③ 的原注释):score_relations 混合关键词(需要 hydrate 的
        关系文本)+ 语义(向量矩阵),所以不能像 _kg_object_candidates 那样单纯按
        向量 top-K 过滤后完全跳过关键词——否则纯关键词命中(无向量覆盖/embedder
        未配置)会被静默丢弃。折中(镜像 _kg_object_candidates 的 fail-open 哲学):
          - notebook 该库 relations 表本身为空 → 早退 [](零 JOIN,COUNT(*) 探针
            比全量 JOIN 便宜几个数量级,这是空库/关系检索关闭部署的主收益)。
          - relations 非空但 relation_embeddings 整体为空(无 embedder 或未
            回填)→ 向量矩阵提供不了候选界定信号,回退全量 JOIN(现状行为,
            保关键词等价 —— 例如 test_mix_overlay.py 的 fixture 场景)。
          - relation_embeddings 非空 → 向量覆盖是真实的候选信号:走
            `top_k_sims`(matrix @ q 后 np.argpartition 直接取 top-K 索引,
            不 materialize 全量 {id: float} dict —— 生产部署关系可达百万级,
            query_sims 的全量 dict 本身就是 GB 级分配,argpartition 是 O(N)
            但只产出 K 个 (id, sim) 元组)→ 只 hydrate 这 K 个 id 的文本做
            关键词+语义融合打分。

        注:与 _kg_object_candidates 的界定条件不对称,是有意的——这里"关系表非空
        但向量表空"仍回退全量 JOIN(上面第二条),而 _kg_object_candidates 是"存在
        持久化 scale ANN 索引"才收窄,否则 fail-open 返回 {} 让上层退回全量。两者
        触发候选界定的前提不同(有向量 vs 有持久 ANN),但都遵循同一 fail-open
        哲学:信号不足时宁可付全量代价也不静默丢候选。

        生产事故修复(2026-07)——大库冷矩阵守卫:branch-3(本方法向量覆盖非空
        场景)在 top_k_sims 之前要 _vector_matrix(nb, "relation_embeddings"),
        这本身是 O(N_relations × dim) 内存(生产环境百万级关系 × 1024 维即数
        GB,矩阵未 BLOB 化时还要逐行 json.loads,是灾难级耗时)。这条加载绝不能
        在 ask 路径上对大库懒触发——保护全部调用方(reasoning
        _graph_seed_fusion、chunk overlay、graph)。守卫:大库
        (not notebook_copy_stats(nb)["copyable"]) 且矩阵未暖在 _vector_cache
        (_vector_matrix_warm 纯 peek,不触发 loader)→ 跳过语义打分,发
        relation_scoring_skipped 事件,返回 []。

        选择返回 [] 而非退化到 branch-2 的关键词专用路径:branch-2 的
        _relations_with_names(relation_ids=None) 本身是对 knowledge_relations
        的无界全量 JOIN——对大库同样是内存/耗时炸弹,只是换了张表,并不比冷
        矩阵加载更便宜。既然两条路径在大库场景下都不「有界」,选择保持
        fail-open 语义最简单、最安全的出口([] + 事件),与本方法既有的
        「关系表为空→[]」早退、以及 federated_retrieve_relations 上层对空结果
        的既有容错完全一致,不引入新的部分結果语义。真正的修复——给关系建 ANN
        索引(镜像 chunk_ann,scale index 侧)让候选界定本身有界——已由上方
        分支②(relation ANN ⊕ delta)落地:已建索引的大库常态走 ANN,不再
        触达本守卫;守卫保留为「未建索引的大库」的最后兜底,把 ask 路径钳制
        在 O(bounded)。已暖(_vector_cache 命中)或小库:字节不变,走原路径。"""
        from app.services.retrieval import score_relations
        from app.services.vector_index import top_k_sims
        with self._connect() as db:
            has_any = db.execute(
                "SELECT 1 FROM knowledge_relations WHERE notebook_id = ? LIMIT 1",
                (notebook_id,)).fetchone()
            if has_any is None:
                return []

            # ── 分支②: 持久化 relation ANN ⊕ delta(大库常态路径)─────────
            # 必须先于下面的冷矩阵守卫:ANN 候选界定本身 O(bounded)、不加载
            # 全量矩阵,已建索引的大库不该被守卫拦下。embed 只在 ANN 真实
            # 存在时才发生(守卫路径保持 master 原语义:守卫命中时零 embed)。
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None and getattr(idx, "relation_ann_labels", None):
                query_vector = self._embed_query(query)
                if query_vector is not None:
                    cand_sims = self._relation_ann_candidates(
                        notebook_id, query_vector, idx, self.settings.relation_recall)
                    if cand_sims:
                        top_ids = list(cand_sims.keys())
                        relations = self._relations_with_names(db, notebook_id, relation_ids=top_ids)
                        return score_relations(query, relations, query_vector=query_vector,
                                               relation_sims=cand_sims,
                                               downweight_edges=self.settings.kg_about_downweight_enabled)
                # fail-open(embed 失败/空候选/ANN 打开失败)→ 继续走守卫/全量路径。

            # ── 分支③: 大库冷矩阵守卫(#171 语义原样;无 ANN 时的最后兜底)──
            if (not self.notebook_copy_stats(notebook_id)["copyable"]
                    and not self._vector_matrix_warm(db, notebook_id, "relation_embeddings")):
                self.event_log.emit({
                    "kind": "relation_scoring_skipped",
                    "notebook_id": notebook_id,
                    "reason": "large_matrix_cold",
                })
                return []
            query_vector = self._embed_query(query)
            rel_ids, rel_mat = self._vector_matrix(
                db, notebook_id, "relation_embeddings", "relation_id")
            if not rel_ids:
                # 无向量覆盖(未配 embedder/未回填)→ 界定不了候选,回退全量。
                relations = self._relations_with_names(db, notebook_id)
                relation_sims = None
            else:
                top_pairs = top_k_sims(query_vector, rel_ids, rel_mat,
                                       self.settings.relation_recall) if query_vector else []
                if top_pairs:
                    top_ids = [rid for rid, _ in top_pairs]
                    relation_sims = dict(top_pairs)   # only K entries, not N
                else:
                    # no query_vector (embed_query 失败/未配置) → sim 界定不了,
                    # 但向量覆盖存在时仍以 relation_recall 为界(取矩阵前 N 个 id,
                    # 保持"有界"而非退回全量;顺序对无 sim 场景无关紧要)。
                    # 注:这是退化路径(model_error 兜底),不追求与"有 query_vector"
                    # 分支等价——矩阵内前 N 个 id 是任意切片,不是相关性排序;只保证
                    # 有界不炸内存,排序质量让位于可用性。
                    top_ids = rel_ids[: self.settings.relation_recall]
                    relation_sims = {}
                relations = self._relations_with_names(db, notebook_id, relation_ids=top_ids)
        return score_relations(query, relations, query_vector=query_vector,
                               relation_sims=relation_sims,
                               downweight_edges=self.settings.kg_about_downweight_enabled)

    def _kg_object_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        """ANN 核候选(idx.ann_path=knowledge_embeddings)⊕ delta 对象暴力。
        返回 {object_id: sim∈[0,1]}。fail-open 返回 {} 让上层退回全量。"""
        import numpy as np
        from app.services.vector_index import build_matrix, query_sims
        sims: dict = {}
        labels = getattr(idx, "ann_labels", None)
        if labels and query_vector is not None:
            qarr = np.asarray(query_vector, dtype=np.float32)
            dim = int(idx.manifest.get("dim", qarr.shape[0]))
            if dim == qarr.shape[0]:
                ann = self._open_scale_ann(idx, "kg")
                if ann is None:
                    return {}
                try:
                    ann.set_ef(max(recall + 1, 64))
                    k = min(recall, len(labels))
                    labs, dists = ann.knn_query(qarr, k=k)
                    for l, d in zip(labs[0], dists[0]):
                        sims[labels[int(l)]] = max(0.0, 1.0 - float(d))
                except Exception as exc:  # noqa: BLE001 — fail-open
                    self._note_model_error("kg_obj_ann", self.settings.embed_model, exc)
                    return {}
        # ⊕ delta 对象(水位后 source)暴力 —— opt-in(scale_search_include_delta,
        # 默认关):与 chunk 侧同一原则「已索引的库只检索已索引部分」,delta 由
        # scale_auto_fold_on_add 的增量 fold 收进索引(最终一致)。True 时保持
        # 强一致暴力(慢,且量级随 delta 无界增长)。
        if self.settings.scale_search_include_delta:
            try:
                delta = self._index_delta(notebook_id)
                if delta["delta_sources"] and query_vector is not None:
                    drows = []
                    with self._connect() as db:
                        for batch in self._in_batches(delta["delta_sources"]):
                            ph_s = ",".join("?" for _ in batch)
                            drows.extend(db.execute(
                                f"SELECT object_id AS vid, vector FROM knowledge_embeddings "
                                f"WHERE notebook_id=? AND object_id IN "
                                f"(SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id IN ({ph_s}))",
                                (notebook_id, notebook_id, *batch)).fetchall())
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    for oid, s in (query_sims(query_vector, d_ids, d_mat) if d_ids else {}).items():
                        sims[oid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
                self._note_model_error("kg_obj_delta", self.settings.embed_model, exc)
        return sims

    @property
    def retrieval(self):
        """检索原语的显式接口（W2.1）。消费者(reasoning/graph)应经此调用，
        而非穿透本类的私有 `_retrieve_*` 方法。当前委托回本类现有实现。"""
        rs = getattr(self, "_retrieval_service", None)
        if rs is None:
            from app.services.retrieval_service import RetrievalService
            rs = self._retrieval_service = RetrievalService(self)
        return rs

    def _retrieve_scored(self, notebook_id: str, query: str,
                         types: Optional[Iterable[str]] = None,
                         w_keyword: float = W_KEYWORD,
                         w_semantic: float = W_SEMANTIC) -> List[RetrievedKnowledge]:
        """Score KG objects of `types` (default all 4 _KG_TYPES) for `query`,
        returning RetrievedKnowledge sorted by fused relevance desc. Shared by
        the reasoning retriever's tools; `w_keyword`/`w_semantic` carry the
        per-sub-query `prefer` bias."""
        # ask_stage 埋点(纯观测):阶段墙钟拆解,生产诊断 20-30s 级检索用。
        t0 = time.perf_counter()
        type_list = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        query_vector = self._embed_query(query)
        t_embed = time.perf_counter()
        # indexed 时用 ANN 核 ⊕ delta 取有界候选;无索引→cand_sims=None→全量(现状)。
        cand_sims = None
        if query_vector is not None:
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is None:
                # 无 ANN 核 → 本次退回全量暴力(O(N))。O(1) once-set 兜底:大库应
                # 自动建索引,避免长期停留在暴力回退稳态。fail-open,不影响本次检索。
                try:
                    self.maybe_auto_index(notebook_id)
                except Exception:
                    pass
            elif getattr(idx, "ann_labels", None):
                cand_sims = self._kg_object_candidates(
                    notebook_id, query_vector, idx, self.settings.chunk_recall)
                if not cand_sims:
                    cand_sims = None   # fail-open → 全量
        if cand_sims is None and not self.notebook_copy_stats(notebook_id)["copyable"]:
            # 大库拿不到 ANN 候选(未建索引/ANN 打不开/维度失配/embed 失败)——
            # 一个原则:绝不全量暴力(全表 json 解析 + 全量分词 + GB 级矩阵,
            # 49 万对象生产实测数十分钟)。FTS 词法有界兜底:kg_objects_fts 覆盖
            # 全部对象(含 delta),候选的语义分仍由下方按候选 evidence 元素向量
            # 有界补充。FTS 空 → [](与 relation 侧冷矩阵守卫同一 fail-open 出口)。
            with self._connect() as db:
                lex = self._runtime.knowledge.fts_search(
                    db, notebook_id, query, k=self.settings.chunk_recall)
            self.event_log.emit({
                "kind": "kg_bruteforce_refused", "notebook_id": notebook_id,
                "site": "_retrieve_scored", "lexical_candidates": len(lex),
            })
            if not lex:
                return []
            cand_sims = {h["object_id"]: 0.0 for h in lex}
        t_ann = time.perf_counter()
        from app.services.vector_index import query_sims, build_matrix
        with self._connect() as db:
            id_filter = set(cand_sims.keys()) if cand_sims is not None else None
            kg_objs = {t: self._knowledge_objects(db, notebook_id, t, id_filter=id_filter)
                       for t in type_list}
            all_kg_objs = [o for objs in kg_objs.values() for o in objs]
            # P0-A: cand_sims is not None ⟺ we're on the bounded ANN/FTS candidate
            # path (≤chunk_recall objects) — skip the knowledge_objects COUNT probe
            # and the process-wide cache (which the candidate-set-varies-per-query
            # path never hit anyway) and build the token sets directly for this batch.
            token_sets = self._keyword_token_sets(
                db, notebook_id, all_kg_objs, bounded=cand_sims is not None)
            # candidate object ids for this retrieval call
            candidate_ids = {o["id"] for o in all_kg_objs}
            # 孤立节点降权: 有边节点集合。降权仅作用于 score(排序),不进 relevance([0,1]/tau 守恒)。
            if cand_sims is not None:
                # 有界:仅按候选对象查边(避免全表扫),element_sims 仅候选证据元素。
                # candidate_ids 量随 delta 候选无界增长——按 _in_batches 分批,每批
                # 两个 IN 位置都放该批(并集正确:一条边只要任一端点落在某批即被
                # 捕获,批间可能重复捕获同一条边,但 rel_rows 只喂 connected_ids
                # 集合,重复无害)。
                rel_rows = []
                for batch in self._in_batches(candidate_ids):
                    phc = ",".join("?" for _ in batch)
                    rel_rows.extend(db.execute(
                        f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND (source_object_id IN ({phc}) OR target_object_id IN ({phc}))",
                        (notebook_id, *batch, *batch)).fetchall())
                elem_id_set = {ev.element_id for o in all_kg_objs
                               for ev in o.get("evidence", []) if getattr(ev, "element_id", None)}
                # elem_id_set 同理分批;各批互不相交(_in_batches 去重保序切片),
                # extend 不会产生跨批重复行。
                erows = []
                for batch in self._in_batches(elem_id_set):
                    phe = ",".join("?" for _ in batch)
                    erows.extend(db.execute(
                        f"SELECT element_id AS vid, vector FROM element_embeddings "
                        f"WHERE notebook_id=? AND element_id IN ({phe})",
                        (notebook_id, *batch)).fetchall())
            else:
                rel_rows = db.execute(
                    "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id = ?",
                    (notebook_id,)).fetchall()
            connected_ids: set = set()
            for r in rel_rows:
                connected_ids.add(r["source_object_id"])
                connected_ids.add(r["target_object_id"])
            isolated_ids: set = candidate_ids - connected_ids
            if cand_sims is not None:
                knowledge_sims = cand_sims
                e_ids, e_mat = build_matrix((r["vid"], r["vector"]) for r in erows)
                element_sims = query_sims(query_vector, e_ids, e_mat) if e_ids else {}
            else:
                elem_ids, elem_mat = self._vector_matrix(db, notebook_id, "element_embeddings", "element_id")
                kn_ids, kn_mat = self._vector_matrix(db, notebook_id, "knowledge_embeddings", "object_id")
                element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
                knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None
        t_hydrate = time.perf_counter()
        penalty = self.settings.kg_isolated_rank_penalty
        if self.settings.retrieval_rrf_enabled:
            scored = self._rrf_scored(query, kg_objs, knowledge_sims, element_sims)
        else:
            scored = []
            for t in type_list:
                objs = kg_objs.get(t) or []
                if not objs:
                    continue
                scored.extend(score_knowledge(
                    query, objs, t, query_vector, None, None,
                    element_sims=element_sims, knowledge_sims=knowledge_sims,
                    w_keyword=w_keyword, w_semantic=w_semantic,
                    keyword_token_sets=token_sets,
                    isolated_ids=isolated_ids,
                    w_isolated_penalty=penalty,
                ))
            scored.sort(key=lambda it: it.score, reverse=True)
        t_score = time.perf_counter()
        if self.settings.kg_canonical_fold_enabled:
            from app.services.retrieval import fold_by_canonical
            scored = fold_by_canonical(scored, self.cluster_map(notebook_id))
        self.event_log.emit({
            "kind": "ask_stage", "site": "_retrieve_scored",
            "notebook_id": notebook_id,
            "embed_ms": round((t_embed - t0) * 1000),
            "ann_ms": round((t_ann - t_embed) * 1000),
            "hydrate_ms": round((t_hydrate - t_ann) * 1000),
            "score_ms": round((t_score - t_hydrate) * 1000),
            "fold_ms": round((time.perf_counter() - t_score) * 1000),
            "total_ms": round((time.perf_counter() - t0) * 1000),
            "candidates": len(all_kg_objs),
            "ann_gated": cand_sims is not None,
        })
        return scored

    def federated_retrieve(
        self,
        active_notebook_id: str,
        query: str,
        types: Optional[Iterable[str]] = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
    ) -> List[RetrievedKnowledge]:
        """Gather scored KG candidates from {base notebook(s)} ∪ {active personal
        notebook}, tagging each hit with .notebook_id and .tier.

        Each notebook's scoring path is IDENTICAL to _retrieve_scored — same
        _fuse, same dual-index best-of — so the [0,1]/tau and dual-index best-of
        invariants are preserved by construction. Hits are merged and sorted by
        score desc; no cross-notebook normalisation is applied (the same fused
        relevance scale applies to both tiers). Two-tier authority is a
        ZERO-MAGNITUDE ordering strategy: ranking is pure relevance (tier-blind),
        and base only wins as a tie-break on an EXACT score tie — never via any
        multiplier/quota/floor. A personal hit with higher relevance still wins.
        """
        notebook_ids: List[str] = [active_notebook_id]
        with self._connect() as db:
            # Add base notebooks (excluding the active one if it is itself base).
            base_rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,),
            ).fetchall()
            notebook_ids.extend(r["id"] for r in base_rows)
            # Tier for each notebook_id (active + base) in one pass.
            tier_map: Dict[str, str] = {}
            for nid in notebook_ids:
                row = db.execute("SELECT tier FROM notebooks WHERE id=?", (nid,)).fetchone()
                tier_map[nid] = (row["tier"] if row else "personal")

        all_hits: List[RetrievedKnowledge] = []
        for nid in notebook_ids:
            hits = self._retrieve_scored(
                nid, query, types=types, w_keyword=w_keyword, w_semantic=w_semantic)
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)

        # Pure-relevance ordering (tier-blind); base wins ONLY on an exact score
        # tie (True > False). Zero magnitude — no constant is added to any score.
        all_hits.sort(key=lambda it: (it.score, getattr(it, "tier", "") == "base"), reverse=True)
        return all_hits

    def federated_retrieve_relations(self, active_notebook_id: str,
                                     query: str) -> List["RetrievedRelation"]:
        """跨 {base notebook(s)} ∪ {active} 检索关系,逐本 .notebook_id/.tier 标注。
        每本走 _retrieve_relations_scored(同尺),合并按 score 降序。"""
        notebook_ids: List[str] = [active_notebook_id]
        with self._connect() as db:
            base_rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,)).fetchall()
            notebook_ids.extend(r["id"] for r in base_rows)
            tier_map: Dict[str, str] = {}
            for nid in notebook_ids:
                row = db.execute("SELECT tier FROM notebooks WHERE id=?", (nid,)).fetchone()
                tier_map[nid] = (row["tier"] if row else "personal")
        all_hits: List["RetrievedRelation"] = []
        for nid in notebook_ids:
            hits = self._retrieve_relations_scored(nid, query)
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)
        all_hits.sort(key=lambda it: it.score, reverse=True)
        return all_hits

    def _retrieve_neighbors(self, notebook_id: str, object_id: str,
                            edge_type: Optional[str] = None,
                            direction: str = "both") -> List[RetrievedKnowledge]:
        """1-hop graph neighbours of `object_id` as RetrievedKnowledge with
        placeholder relevance=0 (final relevance unified by run() via the
        original question). Honours edge_type filter; direction out=object as
        source, in=as target, both=either."""
        # Targeted index hits (idx_knowledge_relations_nb_source/_nb_target)
        # instead of loading every notebook edge: O(neighbours), not O(E).
        edge_clause = " AND edge_type=?" if edge_type else ""
        edge_param = [edge_type] if edge_type else []
        neighbour_ids: set = set()
        with self._connect() as db:
            if direction in ("out", "both"):
                neighbour_ids.update(
                    r["target_object_id"] for r in db.execute(
                        f"SELECT target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND source_object_id=?{edge_clause}",
                        [notebook_id, object_id, *edge_param],
                    ).fetchall()
                )
            if direction in ("in", "both"):
                neighbour_ids.update(
                    r["source_object_id"] for r in db.execute(
                        f"SELECT source_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND target_object_id=?{edge_clause}",
                        [notebook_id, object_id, *edge_param],
                    ).fetchall()
                )
            if not neighbour_ids:
                return []
            placeholders = ",".join("?" for _ in neighbour_ids)
            status_ph = ",".join("?" for _ in USABLE_STATUSES)
            rows = db.execute(
                f"SELECT * FROM knowledge_objects WHERE id IN ({placeholders}) "
                f"AND status IN ({status_ph})",
                [*neighbour_ids, *USABLE_STATUSES],
            ).fetchall()
        out: List[RetrievedKnowledge] = []
        for row in rows:
            keys = row.keys()
            out.append(RetrievedKnowledge(
                object_id=row["id"], object_type=row["object_type"],
                payload=json.loads(row["payload"] or "{}"),
                evidence=[Evidence(**e) for e in json.loads(row["evidence"] or "[]")],
                score=0.0, relevance=0.0, status=row["status"], owner=row["owner"],
                last_reviewed=row["last_reviewed"] if "last_reviewed" in keys else "",
            ))
        return out

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
        """查询期、fail-closed 的类型化两跳推理。

        只在起点实际所属的 active/base notebook 内，按已有 source/target 复合索引
        做两轮局部查询；不构建全图、不跨 notebook 虚构边、不持久化推论。返回
        FollowChainResult(inferences, nodes)，其中 nodes 是路径端点的
        RetrievedKnowledge，供 reasoning 候选池复用。
        """
        from app.services.kg.follow_chain import (
            FollowChainResult,
            TRANSITIVE_EDGE_TYPES,
            compose_two_hop_paths,
        )

        start_object_id = str(start_object_id or "").strip()
        target_object_id = str(target_object_id or "").strip()
        if not start_object_id or direction not in {"out", "in", "both"}:
            return FollowChainResult()
        allowed_types = ([edge_type] if edge_type in TRANSITIVE_EDGE_TYPES
                         else sorted(TRANSITIVE_EDGE_TYPES) if not edge_type else [])
        if not allowed_types:
            return FollowChainResult()
        max_fan_out = max(1, min(int(max_fan_out), 16))
        max_results = max(1, min(int(max_results), 16))
        status_ph = ",".join("?" for _ in USABLE_STATUSES)

        with self._connect() as db:
            # Authorization/scope gate: an action may only start from the active
            # notebook or an authoritative base notebook participating in this ask.
            start = db.execute(
                f"SELECT ko.*, n.tier AS notebook_tier "
                f"FROM knowledge_objects ko JOIN notebooks n ON n.id=ko.notebook_id "
                f"WHERE ko.id=? AND ko.status IN ({status_ph}) "
                f"AND (ko.notebook_id=? OR n.tier='base')",
                (start_object_id, *USABLE_STATUSES, active_notebook_id),
            ).fetchone()
            if start is None:
                return FollowChainResult()
            owner_notebook_id = start["notebook_id"]
            owner_tier = start["notebook_tier"] or "personal"

            # Production databases can already contain tens of millions of KG
            # rows.  follow_chain therefore adds no startup migration/index.  It
            # forces the two existing endpoint indexes and reads at most this many
            # raw rows per frontier endpoint; type/review filtering and priority
            # sorting happen only inside that bounded sample.  Missing a valid edge
            # is acceptable (fail closed); scanning an unbounded supernode is not.
            raw_scan_limit = min(256, max(32, max_fan_out * 8))
            raw_cache: Dict[tuple, tuple[List[dict], bool]] = {}
            hydrated_relation_cache: Dict[str, dict] = {}

            def raw_endpoint_rows(endpoint: str, oid: str) -> tuple[List[dict], bool]:
                key = (endpoint, oid)
                cached = raw_cache.get(key)
                if cached is not None:
                    return cached
                index_name = (
                    "idx_knowledge_relations_nb_source"
                    if endpoint == "source_object_id"
                    else "idx_knowledge_relations_nb_target"
                )
                rows = db.execute(
                    f"SELECT r.id, r.notebook_id, r.source_id, "
                    f"r.source_object_id, r.target_object_id, r.edge_type, "
                    f"r.review_status "
                    f"FROM knowledge_relations AS r INDEXED BY {index_name} "
                    f"WHERE r.notebook_id=? AND r.{endpoint}=? LIMIT ?",
                    (owner_notebook_id, oid, raw_scan_limit + 1),
                ).fetchall()
                truncated = len(rows) > raw_scan_limit
                decoded = [
                    {
                        "id": row["id"], "notebook_id": row["notebook_id"],
                        "tier": owner_tier, "source_id": row["source_id"] or "",
                        "source_object_id": row["source_object_id"],
                        "target_object_id": row["target_object_id"],
                        "edge_type": row["edge_type"],
                        "review_status": row["review_status"] or "pending",
                    }
                    for row in rows[:raw_scan_limit]
                ]
                raw_cache[key] = (decoded, truncated)
                return decoded, truncated

            def hydrate_relations(rows: List[dict]) -> List[dict]:
                missing = [row["id"] for row in rows
                           if row["id"] not in hydrated_relation_cache]
                for batch in self._in_batches(missing):
                    ph = ",".join("?" for _ in batch)
                    for row in db.execute(
                        f"SELECT r.id, r.evidence, s.title AS source_title "
                        f"FROM knowledge_relations r "
                        f"LEFT JOIN sources s ON s.id=r.source_id "
                        f"WHERE r.id IN ({ph})",
                        tuple(batch),
                    ).fetchall():
                        try:
                            evidence = json.loads(row["evidence"] or "[]")
                        except Exception:
                            evidence = []
                        hydrated_relation_cache[row["id"]] = {
                            "evidence": evidence,
                            "source_title": row["source_title"] or "",
                        }
                hydrated = []
                for row in rows:
                    detail = hydrated_relation_cache.get(row["id"])
                    if detail is not None:
                        hydrated.append({**row, **detail})
                return hydrated

            def edge_rows(endpoint: str, oid: str, et: str) -> List[dict]:
                rows, _truncated = raw_endpoint_rows(endpoint, oid)
                usable = [
                    row for row in rows
                    if row["edge_type"] == et
                    and row["review_status"] in {"verified", "pending"}
                ]
                usable.sort(key=lambda row: (
                    0 if row["review_status"] == "verified" else 1,
                    row["id"],
                ))
                return hydrate_relations(usable[:max_fan_out])

            relations_by_id: Dict[str, dict] = {}
            directions = ("out", "in") if direction == "both" else (direction,)
            for walk_dir in directions:
                endpoint = "source_object_id" if walk_dir == "out" else "target_object_id"
                for et in allowed_types:
                    first = edge_rows(endpoint, start_object_id, et)
                    for rel in first:
                        relations_by_id[rel["id"]] = rel
                    middle_ids = list(dict.fromkeys(
                        rel["target_object_id"] if walk_dir == "out"
                        else rel["source_object_id"] for rel in first
                    ))
                    for middle_id in middle_ids:
                        for rel in edge_rows(endpoint, middle_id, et):
                            relations_by_id[rel["id"]] = rel

            relations = list(relations_by_id.values())
            if not relations:
                return FollowChainResult()
            node_ids = list(dict.fromkeys(
                [start_object_id, target_object_id]
                + [oid for rel in relations for oid in
                   (rel["source_object_id"], rel["target_object_id"])]
            ))
            node_ids = [oid for oid in node_ids if oid]
            nodes: Dict[str, dict] = {}
            for batch in self._in_batches(node_ids):
                ph = ",".join("?" for _ in batch)
                for row in db.execute(
                    f"SELECT * FROM knowledge_objects WHERE notebook_id=? "
                    f"AND id IN ({ph}) AND status IN ({status_ph})",
                    (owner_notebook_id, *batch, *USABLE_STATUSES),
                ).fetchall():
                    try:
                        payload = json.loads(row["payload"] or "{}")
                    except Exception:
                        payload = {}
                    try:
                        evidence = json.loads(row["evidence"] or "[]")
                    except Exception:
                        evidence = []
                    nodes[row["id"]] = {
                        "id": row["id"], "notebook_id": owner_notebook_id,
                        "tier": owner_tier, "object_type": row["object_type"],
                        "status": row["status"], "owner": row["owner"],
                        "last_reviewed": row["last_reviewed"],
                        "payload": payload, "evidence": evidence,
                    }

        # Exact direct-edge guard over the same bounded raw endpoint sample.  If
        # that sample was truncated and a candidate's direct A→C edge was not
        # observed, absence cannot be proved; add a conservative sentinel triple
        # so compose_two_hop_paths suppresses that inference.  This intentionally
        # trades recall for a hard production bound and never scans a supernode.
        by_source: Dict[str, List[dict]] = {}
        for rel in relations:
            by_source.setdefault(rel["source_object_id"], []).append(rel)
        out_candidates: Dict[str, set] = {}
        in_candidates: Dict[str, set] = {}
        for first in relations:
            for second in by_source.get(first["target_object_id"], []):
                if first["edge_type"] != second["edge_type"]:
                    continue
                if first["source_object_id"] == start_object_id:
                    out_candidates.setdefault(first["edge_type"], set()).add(
                        second["target_object_id"])
                if second["target_object_id"] == start_object_id:
                    in_candidates.setdefault(first["edge_type"], set()).add(
                        first["source_object_id"])
        direct_triples: set = set()
        out_rows, out_truncated = raw_cache.get(
            ("source_object_id", start_object_id), ([], True))
        for et, target_ids in out_candidates.items():
            found_targets = set()
            for row in out_rows:
                if (row["edge_type"] == et
                        and row["review_status"] != "rejected"
                        and row["target_object_id"] in target_ids):
                    triple = (start_object_id, row["target_object_id"], et)
                    direct_triples.add(triple)
                    found_targets.add(row["target_object_id"])
            if out_truncated:
                direct_triples.update(
                    (start_object_id, target_id, et)
                    for target_id in target_ids - found_targets
                )

        in_rows, in_truncated = raw_cache.get(
            ("target_object_id", start_object_id), ([], True))
        for et, source_ids in in_candidates.items():
            found_sources = set()
            for row in in_rows:
                if (row["edge_type"] == et
                        and row["review_status"] != "rejected"
                        and row["source_object_id"] in source_ids):
                    triple = (row["source_object_id"], start_object_id, et)
                    direct_triples.add(triple)
                    found_sources.add(row["source_object_id"])
            if in_truncated:
                direct_triples.update(
                    (source_id, start_object_id, et)
                    for source_id in source_ids - found_sources
                )
        inferences = compose_two_hop_paths(
            nodes, relations, start_object_id,
            direction=direction, edge_type=edge_type,
            target_object_id=target_object_id,
            direct_triples=frozenset(direct_triples),
            max_results=max_results,
        )
        used_ids = list(dict.fromkeys(
            oid for chain in inferences
            for oid in (chain.source_id, chain.via_id, chain.target_id)
        ))
        hydrated: List[RetrievedKnowledge] = []
        for oid in used_ids:
            node = nodes.get(oid)
            if node is None:
                continue
            hydrated.append(RetrievedKnowledge(
                object_id=oid, object_type=node["object_type"],
                payload=node["payload"],
                evidence=[Evidence(**e) for e in node["evidence"]],
                score=0.0, relevance=0.0, status=node["status"],
                owner=node["owner"], last_reviewed=node["last_reviewed"],
                notebook_id=owner_notebook_id, tier=owner_tier,
            ))
        return FollowChainResult(inferences=inferences, nodes=hydrated)

    def _retrieve_elements(self, notebook_id: str, query: str,
                           limit: int = 8) -> List[RetrievedElement]:
        """Keyword+semantic search over raw source_elements (fallback layer 2)."""
        if not self.notebook_copy_stats(notebook_id)["copyable"]:
            # source_elements 没有索引模态,本方法=全表扫+逐行向量解码(生产
            # 17 万元素×4096 维=数 GB/次)。大库跳过并发事件,返回 [] ——
            # 调用方(reasoning search_elements、chunk 兜底层)均容错空结果。
            self.event_log.emit({
                "kind": "element_scoring_skipped", "notebook_id": notebook_id,
                "site": "_retrieve_elements", "reason": "large_notebook",
            })
            return []
        query_vector = self._embed_query(query)
        with self._connect() as db:
            elements = self._gather_elements(db, notebook_id, with_vectors=True)
        return score_elements(query, elements, query_vector, limit=limit)

    def _retrieve_chunks(self, notebook_id: str, query: str, recall: int = 0):
        """大召回 chunk 候选。返回 (scored, ids, matrix);后两者供 MMR 取两两余弦
        (matrix 行已 L2 归一化, 点积即余弦)。"""
        from app.services.retrieval import score_chunks
        from app.services.vector_index import query_sims
        recall = recall or self.settings.chunk_recall
        query_vector = self._embed_query(query)
        if self.settings.chunk_ann_enabled and query_vector is not None:
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None and getattr(idx, "chunk_ann_labels", None):
                ann = self._retrieve_chunks_ann(notebook_id, query, query_vector, idx, recall)
                if ann is not None:
                    return ann
        # ── 大库暴力守卫(镜像 #171 冷矩阵守卫哲学):走到这里 = ANN 不可用(未建
        # scale 索引 / embed 失败 query_vector=None / ANN fail-open)。超阈值的库
        # 绝不落进下面「全表拉文本 + 逐 chunk 纯 Python 分词」——生产 55 万 KG 级
        # 库曾因 .env 丢失静默走到这里,单问磨半小时「思考中」。降级为 FTS 词法
        # 候选 + 有界打分(候选内仍关键词+语义融合),发 chunk_bruteforce_skipped
        # 事件;真解=建 scale 索引(chunk ANN)。小库/关守卫(0)字节不变。
        # 大库暴力守卫(统一「大库」定义 = not copyable,与其余 5 条检索路径一把尺子):
        # 大库无论 chunk 多少都强制走索引/FTS 降级,绝不全表暴力。chunk 计数阈值
        # chunk_bruteforce_max_chunks 作叠加下限保留(小库 chunk 极多也降级)。
        large = not self.notebook_copy_stats(notebook_id)["copyable"]
        threshold = self.settings.chunk_bruteforce_max_chunks
        if large or threshold > 0:
            with self._connect() as db:
                n_chunks = db.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id = ?",
                    (notebook_id,)).fetchone()["c"]
            if large or n_chunks > threshold:
                return self._retrieve_chunks_fts_degraded(
                    notebook_id, query, query_vector, recall, n_chunks)
        # ↓ 现有暴力路径保持不变
        with self._connect() as db:
            chunks = self._gather_chunks(db, notebook_id)
            ids, mat = self._vector_matrix(db, notebook_id, "chunk_embeddings", "chunk_id")
        chunk_sims = query_sims(query_vector, ids, mat) if query_vector else None
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        return scored, ids, mat

    def _retrieve_chunks_fts_degraded(self, notebook_id, query, query_vector,
                                      recall, n_chunks):
        """大库且 chunk ANN 不可用时的有界降级:FTS5 词法候选(k=recall)→ 只对
        候选 hydrate 文本+向量,候选内做关键词+语义融合打分。绝不 _gather_chunks
        全表、不全量分词、不触发全量向量矩阵加载(与 PR#158「查询恒定成本」取向
        一致)。fail-open:FTS 异常(如旧库缺 chunks_fts)按零候选处理 →
        ([], [], None),事件携带 fts_error 供 diag_slow.py 定位。"""
        from app.services.retrieval import score_chunks
        from app.services.vector_index import query_sims
        hits, fts_error = [], ""
        try:
            with self._connect() as db:
                hits = self._runtime.knowledge.chunk_fts_search(
                    db, notebook_id, query, k=recall)
        except Exception as exc:  # noqa: BLE001 — 降级中的降级,守卫本身绝不抛
            fts_error = f"{type(exc).__name__}: {exc}"
        event = {
            "kind": "chunk_bruteforce_skipped", "notebook_id": notebook_id,
            "reason": "large_library_no_ann", "n_chunks": n_chunks,
            "threshold": self.settings.chunk_bruteforce_max_chunks,
            "fts_hits": len(hits), "embed_ok": query_vector is not None,
        }
        if fts_error:
            event["fts_error"] = fts_error[:120]
        self.event_log.emit(event)
        if not hits:
            return [], [], None
        chunks, ids, mat = self._hydrate_chunk_candidates([h["chunk_id"] for h in hits])
        chunk_sims = query_sims(query_vector, ids, mat) if query_vector else None
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        return scored, ids, mat

    def _retrieve_chunks_ann(self, notebook_id, query, query_vector, idx, recall):
        """ANN 候选版 chunk 检索:只对 top-recall 候选打分,避免全表 matmul+重分词。
        返回 (scored, ids, matrix) 同 _retrieve_chunks;失败返回 None(上层回退暴力)。"""
        import numpy as np
        from app.services.retrieval import score_chunks
        from app.services.vector_index import build_matrix, query_sims
        labels = idx.chunk_ann_labels
        if not labels:
            return None
        qarr = np.asarray(query_vector, dtype=np.float32)
        dim = int(idx.manifest.get("dim", qarr.shape[0]))
        if dim != qarr.shape[0]:
            self.event_log.emit({
                "kind": "dim_mismatch", "notebook_id": notebook_id, "site": "chunk_ann",
                "manifest_dim": dim, "query_dim": int(qarr.shape[0])})
            return None
        ann = self._open_scale_ann(idx, "chunk")
        if ann is None:
            return None
        try:
            ann.set_ef(max(recall + 1, 64))
            k = min(recall, len(labels))
            labs, dists = ann.knn_query(qarr, k=k)
        except Exception as exc:  # noqa: BLE001 — fail-open, 回退暴力
            self._note_model_error("chunk_ann_query", self.settings.embed_model, exc)
            return None
        chunk_sims = {labels[int(l)]: max(0.0, 1.0 - float(d)) for l, d in zip(labs[0], dists[0])}
        cand_ids = list(chunk_sims.keys())

        # ⊕ delta(opt-in via scale_search_include_delta):水位后新增 source 的 chunk 不在
        # 存量 ANN → 暴力补召回(delta 小)。默认关 —— 大库 delta 暴力不可扩展,改由
        # scale_auto_fold_on_add 排增量 fold 把 delta 收进索引(下方 FTS 词法块始终覆盖
        # 全部 chunk,delta 关时新内容仍词法可寻)。True 时保持强一致的暴力补召回(慢)。
        if self.settings.scale_search_include_delta:
            try:
                delta = self._index_delta(notebook_id)
                if delta["delta_sources"]:
                    drows = []
                    with self._connect() as db:
                        for batch in self._in_batches(delta["delta_sources"]):
                            ph_s = ",".join("?" for _ in batch)
                            drows.extend(db.execute(
                                f"SELECT chunk_id AS vid, vector FROM chunk_embeddings "
                                f"WHERE notebook_id=? AND chunk_id IN "
                                f"(SELECT id FROM chunks WHERE notebook_id=? AND source_id IN ({ph_s}))",
                                (notebook_id, notebook_id, *batch)).fetchall())
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    d_sims = query_sims(query_vector, d_ids, d_mat) if d_ids else {}
                    for cid, s in d_sims.items():
                        if cid not in chunk_sims:
                            cand_ids.append(cid)
                        chunk_sims[cid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮检索,退回仅核候选
                self._note_model_error("chunk_ann_delta", self.settings.embed_model, exc)

        # ∪ 词法:FTS5 命中补召回(ANN 是语义候选,纯关键词命中可能漏)
        try:
            with self._connect() as db:
                lex = self._runtime.knowledge.chunk_fts_search(
                    db, notebook_id, query, k=recall)
            for h in lex:
                cid = h["chunk_id"]
                if cid not in chunk_sims:
                    cand_ids.append(cid)
                    chunk_sims[cid] = 0.0   # 词法命中无语义分;score_chunks 的 keyword 分兜底
        except Exception as exc:  # noqa: BLE001 — 词法失败不拖垮检索
            self._note_model_error("chunk_fts", self.settings.embed_model, exc)

        if not cand_ids:
            return [], [], None
        chunks, ids, mat = self._hydrate_chunk_candidates(cand_ids)
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        return scored, ids, mat

    def _hydrate_chunk_candidates(self, cand_ids):
        """按候选 id 有界取数:chunk 文本行 + 归一化向量矩阵。候选界定之后的
        hydrate,ANN 路径与大库 FTS 降级路径共用,绝不全表。返回 (chunks, ids, mat)。
        cand_ids 随 delta⊕词法补召回可无界增长——按 _in_batches 分批,防超 SQLite
        变量上限(调用方按构造去重,_in_batches 亦去重保序,extend 不产生重复行)。"""
        from app.services.vector_index import build_matrix
        rows, vrows = [], []
        with self._connect() as db:
            for batch in self._in_batches(cand_ids):
                ph = ",".join("?" for _ in batch)
                rows.extend(db.execute(
                    f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
                    f"s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
                    f"WHERE c.id IN ({ph})", batch).fetchall())
                vrows.extend(db.execute(
                    f"SELECT chunk_id AS vid, vector FROM chunk_embeddings WHERE chunk_id IN ({ph})",
                    batch).fetchall())
        chunks = [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
        ids, mat = build_matrix((r["vid"], r["vector"]) for r in vrows)
        return chunks, ids, mat

    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        """对每个子查询并发跑 _retrieve_chunks;返回 (collected{chunk_id:best}, per_query, ids, mat)。
        ids/mat 取首个非空子查询的矩阵(同 notebook 矩阵一致,用于后续 MMR 兜底)。"""
        from concurrent.futures import ThreadPoolExecutor

        import contextvars as _cv
        tasks = [(q, _cv.copy_context()) for q in sub_queries]
        def _one(task):
            q, ctx = task
            try:
                return ctx.run(self._retrieve_chunks, notebook_id, q)
            except Exception:
                return ([], [], None)

        results = []
        if sub_queries:
            with ThreadPoolExecutor(max_workers=min(len(sub_queries), 8)) as ex:
                results = list(ex.map(_one, tasks))
        per_query, collected, ids, mat = [], {}, [], None
        for scored, qids, qmat in results:
            per_query.append({c.chunk_id: c for c in scored})
            for c in scored:
                cur = collected.get(c.chunk_id)
                if cur is None or c.relevance > cur.relevance:
                    collected[c.chunk_id] = c
            if mat is None and len(qids):
                ids, mat = qids, qmat
        return collected, per_query, ids, mat

    def _keyword_chunk_candidates(self, notebook_id: str, keywords: str,
                                  recall: int = 0):
        """Bilingual-keyword CHUNK lexical recall (the chunk-side of "FTS carries
        the 2nd language"). ONE chunk_fts_search over the combined bilingual
        keyword string, hydrated + keyword-scored into RetrievedChunk (semantic 0,
        exactly like the existing ANN∪FTS union). Empty/blank keywords → []. NO
        vector embed — purely lexical. Called ONCE per ask (not per sub-query);
        the caller merges these into whatever candidate set its branch built
        (dedup by chunk_id), so a chunk that matches only a 2nd-language keyword —
        never the question-language sub_queries — is still retrieved. fail-open:
        FTS/hydrate errors (e.g. legacy lib missing chunks_fts) → []."""
        from app.services.retrieval import score_chunks
        needle = (keywords or "").strip()
        if not needle:
            return []
        recall = recall or self.settings.chunk_recall
        try:
            with self._connect() as db:
                hits = self._runtime.knowledge.chunk_fts_search(
                    db, notebook_id, needle, k=recall)
            if not hits:
                return []
            chunks, _ids, _mat = self._hydrate_chunk_candidates([h["chunk_id"] for h in hits])
            # keyword-only score (no query_vector/chunk_sims) — mirrors the ANN∪FTS
            # union where lexical hits get keyword score and semantic 0.
            return score_chunks(needle, chunks, None, None, limit=recall)
        except Exception as exc:  # noqa: BLE001 — lexical补召回失败绝不拖垮检索
            self._note_model_error("chunk_keyword_union", self.settings.embed_model, exc)
            return []

    @staticmethod
    def _union_chunk_candidates(base: list, extra: list) -> list:
        """Append `extra` RetrievedChunks to `base`, deduped by chunk_id (keep the
        existing entry on collision — base already scored it with semantic signal).
        Order-preserving. Used to fold the bilingual-keyword lexical hits into a
        list-shaped candidate set (mix / single-subquery branches)."""
        if not extra:
            return base
        seen = {c.chunk_id for c in base}
        out = list(base)
        for c in extra:
            if c.chunk_id not in seen:
                seen.add(c.chunk_id)
                out.append(c)
        return out

    def _mmr_select_chunks(self, scored, ids, mat, k: int, lambda_: float):
        """对大召回结果做 MMR 多样性精选。沿用归一化矩阵, pair_sim=行点积。"""
        from app.services.mmr import mmr_rerank
        if len(scored) <= k:
            return list(scored)
        id_to_row = {cid: i for i, cid in enumerate(ids)}
        relevance = {c.chunk_id: c.relevance for c in scored}

        def pair_sim(a: str, b: str) -> float:
            ia, ib = id_to_row.get(a), id_to_row.get(b)
            if ia is None or ib is None:
                return 0.0
            return float(mat[ia] @ mat[ib])

        chosen = mmr_rerank([c.chunk_id for c in scored], relevance, pair_sim, k, lambda_)
        by_id = {c.chunk_id: c for c in scored}
        return [by_id[cid] for cid in chosen]

    def _chunk_answer_context(self, chunks, budget_chars: "int | None" = None,
                               notebook_id: str = "") -> tuple:
        """产出长上下文综合用的 id 标注块 + id_map。chunk.text 已含 [section] 前缀
        (P1 build_chunks),故每行直接 `k_i: <text>`。id_map 形状与 KG 版一致,
        使 _parse_answer_anchors 原样复用(object_id=chunk_id, object_type=chunk)。
        tier:与 _answer_context(KG 版)同一模式——按每 chunk 自己的 notebook_id 解析
        (PPR/概念漫游可掺 base 库 chunk,c.notebook_id 已标;单库路径留空回退调用方
        传入的 notebook_id)。批量一次查询(O(distinct notebook) 非 O(chunk)),
        循环内只查表。"""
        budget = self.settings.chunk_answer_budget_chars if budget_chars is None else budget_chars
        tier_map = self._tier_map_for(
            {getattr(c, "notebook_id", "") or notebook_id for c in chunks})
        lines, id_map = [], {}
        used = 0
        for i, c in enumerate(chunks, 1):
            if used >= budget and len(lines) >= 1:
                break
            key = f"k{i}"
            line = f"{key}: {c.text}"
            lines.append(line)
            used += len(line)
            c_nb = getattr(c, "notebook_id", "") or notebook_id
            id_map[key] = {
                "object_id": c.chunk_id, "object_type": "chunk",
                "name": c.section_path or c.source_title, "definition": None,
                "snippet": c.text[:300], "source_title": c.source_title,
                "location_label": c.section_path,
                "tier": tier_map.get(c_nb, "personal"),
            }
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _answer_chunks(
        self,
        question,
        chunks,
        history="",
        cancel_event: CancelEvent = None,
        notebook_id: str = "",
    ) -> tuple:
        """长上下文综合:把 MMR 精选的 chunk 原文喂给答案 LLM。返回
        (answer, llm_grounded, anchors)。复用 answer_prompt 的 [k] 标注协议。
        notebook_id:转发给 _chunk_answer_context 解 anchor.tier(见其 docstring);
        chunk 自带 notebook_id(跨库召回)优先,这只是单库 chunk 的回退值。"""
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        raise_if_cancelled(cancel_event)
        context_block, id_map = self._chunk_answer_context(chunks, notebook_id=notebook_id)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(self.llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

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
        """mix 长上下文综合:chunk 段(k1..kN)+ KG 段(k1001+),统一 id_map。
        chunk 段不再二次预算(选择阶段已 token 预算),故 budget_chars 给极大值。
        返回 (answer, llm_grounded, anchors)。notebook_id:转发给 _chunk_answer_context
        解 anchor.tier(见其 docstring);chunk 自带 notebook_id(跨库召回)优先,
        这只是单库 chunk 的回退值。"""
        # chunk 段编号 k1..kN,KG 段从 _MIX_KG_KEY_BASE 起;若 chunk 数逼近 base 会在
        # 合并 id_map 时撞 KG key(静默覆盖)。按 base-1 硬截(token 预算下通常远不及此)。
        chunks = chunks[: self._MIX_KG_KEY_BASE - 1]
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        raise_if_cancelled(cancel_event)
        chunk_block, chunk_id_map = self._chunk_answer_context(
            chunks, budget_chars=10**9, notebook_id=notebook_id)
        if kg_block and kg_block != "(none)":
            context_block = f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
        else:
            context_block = chunk_block
        id_map = {**chunk_id_map, **kg_id_map}
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            cancel_event=cancel_event,
            **cap_kwargs(self.llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _build_chunk_retrieval_plan(self, notebook_id: str, sub_queries: list) -> ChunkRetrievalPlan:
        """一次读齐 chunk 检索路径的 flag/knob，产出不可变快照（W2.2）。见 ChunkRetrievalPlan。
        strategy 复刻 ask_chunk 的 overlay_on 三元 AND 与 if/elif/else 顺序，逐真值等价；
        fuse_k 复刻 multi 分支 quota_fuse 复用 chunk_mmr_k 的隐式契约。"""
        s = self.settings
        overlay_on = (s.chunk_kg_overlay_enabled
                      and self.rerank_client.configured
                      and (self._notebook_has_kg(notebook_id)
                           or self._any_base_notebook_has_kg()))
        if overlay_on:
            strategy = "mix"
        elif len(sub_queries) >= 2:
            strategy = "multi"
        else:
            strategy = "single"
        return ChunkRetrievalPlan(
            strategy=strategy,
            overlay_on=overlay_on,
            mmr_k=s.chunk_mmr_k,
            mmr_lambda=s.chunk_mmr_lambda,
            fuse_k=s.chunk_mmr_k,
        )

    def ask_chunk(
        self,
        notebook_id: str,
        payload: AskRequest,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """chunk-native 通用问答:大召回 → MMR 多样性精选 → 长上下文综合 →
        引用绑回 chunk。KG 不参与(严格推理走 ask_reasoning)。"""
        import time
        from app.services.retrieval import classify_evidence
        ask_started = time.perf_counter()

        def ask_stage(name: str, started: float, **extra) -> None:
            self.event_log.emit({
                "kind": "ask_stage", "notebook_id": notebook_id, "stage": name,
                "latency_ms": round((time.perf_counter() - started) * 1000), **extra,
            })

        self.get_notebook(notebook_id)
        question = payload.question.strip()
        raise_if_cancelled(cancel_event)
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)
        raise_if_cancelled(cancel_event)
        retrieval_query = self._rewrite_followup_query(history, question, cancel_event)

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        try:
            if self.resolve_model_config(self.current_user(), "llm").source == "none":
                self._note_model_error(
                    "answer", "", ModelNotConfiguredError("请先在设置中配置你的模型服务"))
            _t = time.perf_counter()
            from app.services.query_rewrite import expand_query
            from app.services.retrieval import quota_fuse, est_tokens, truncate_by_tokens
            ex = None
            raise_if_cancelled(cancel_event)
            if self.settings.query_rewrite_enabled:
                ex = expand_query(self.rewrite_llm_client, retrieval_query, history,
                                  max_subqueries=self.settings.chunk_max_subqueries,
                                  corpus_langs=self._notebook_langs(notebook_id),
                                  cancel_event=cancel_event)
                sub_queries = [s.query for s in ex.sub_queries]
            else:
                sub_queries = [retrieval_query]
            # 对比题:焦点兄弟追加为子查询(chunk 无 agent 循环,借 expand 的 comparison
            # 字段触发)。共提优先、社区回退(resolve_comparison_peers)。无 base → 跳过。
            # 共提兄弟不应被社区层开关单独关死——P2 实测社区层对兄弟无效,操作员关
            # community_layer 时 mention 桥仍需生效(与 reasoning 分支行为对齐)。
            if ex and ex.comparison and (self.settings.community_layer_enabled
                                          or self.settings.mention_bridge_enabled):
                from app.services.communities import resolve_comparison_peers, first_base_notebook_id
                base_nb = first_base_notebook_id(self, notebook_id)
                if base_nb:
                    peers, _src = resolve_comparison_peers(
                        self, base_nb, ex.comparison["focal"], retrieval_query,
                        top_k=self.settings.community_peers_topk,
                        candidates=self.settings.community_rerank_candidates)
                    for pname in peers:
                        if pname not in sub_queries:
                            sub_queries.append(pname)
            hl = " ".join(ex.high_level_keywords) if ex else ""
            # Bilingual keyword string (high+low level, both corpus languages) for
            # the CHUNK lexical union — this is how "FTS carries the 2nd language"
            # reaches chunks (not just the KG-name/relation FTS). Computed ONCE;
            # merged into whichever candidate branch runs below (dedup by chunk_id).
            kw_str = " ".join(ex.high_level_keywords + ex.low_level_keywords) if ex else ""
            kw_hits = (self._keyword_chunk_candidates(notebook_id, kw_str)
                       if kw_str.strip() else [])
            ask_stage("expand_query", _t, n=len(sub_queries))

            # ── 检索 + 选择 ──
            # mix(overlay 开 + rerank 配齐 + 有 KG):三路并池 → rerank 排序 → token 预算截。
            # 否则走现状 chunk-only(MMR / quota_fuse),与历史字节等价。
            # W2.2:一次读齐 chunk-path flag/knob → 不可变 plan;overlay_on / strategy /
            # 各 knob 下面统一读 plan(不再就地读 self.settings)。plan.overlay_on 与旧三元
            # AND 逐字等价,答案/引用分支继续按 overlay_on 分派。
            plan = self._build_chunk_retrieval_plan(notebook_id, sub_queries)
            overlay_on = plan.overlay_on
            kg_block, kg_id_map, kg_hits = "", {}, []
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            if plan.strategy == "mix":
                candidates, kg_block, kg_id_map, kg_hits, concept_walk_n = self._mix_retrieve(
                    notebook_id, retrieval_query, hl, sub_queries)
                # ∪ bilingual-keyword chunk hits (dedup by chunk_id; keep existing on collision)
                candidates = self._union_chunk_candidates(candidates, kw_hits)
                raise_if_cancelled(cancel_event)
                order = self.rerank_client.rerank(
                    retrieval_query, [c.text for c in candidates],
                    on_error=lambda e: self._note_model_error("rerank", self.settings.rerank_model, e))
                raise_if_cancelled(cancel_event)
                ranked = [candidates[i] for i in order]
                kg_budget = self.settings.max_entity_tokens + self.settings.max_relation_tokens
                kg_block = self._truncate_kg_block(kg_block, kg_budget)
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                selected = truncate_by_tokens(ranked, lambda c: c.text, chunk_budget)
                ask_stage("mix_rerank", _t, recall=len(candidates),
                          selected=len(selected), kg_nodes=len(kg_id_map),
                          concept_walk=concept_walk_n)
            elif plan.strategy == "multi":
                collected, per_query, _ids, _mat = self._retrieve_chunks_multi(notebook_id, sub_queries)
                raise_if_cancelled(cancel_event)
                # ∪ bilingual-keyword chunk hits: merge into collected (best relevance)
                # and add as an extra per_query group so quota_fuse can surface them.
                if kw_hits:
                    for c in kw_hits:
                        cur = collected.get(c.chunk_id)
                        if cur is None or c.relevance > cur.relevance:
                            collected[c.chunk_id] = c
                    per_query = per_query + [{c.chunk_id: c for c in kw_hits}]
                selected, _counts = quota_fuse(collected, per_query, plan.fuse_k,
                                               relevance=lambda c: c.relevance)
                ask_stage("retrieve_fuse", _t, recall=len(collected), selected=len(selected))
            else:
                scored, ids, mat = self._retrieve_chunks(notebook_id, sub_queries[0])
                # ∪ bilingual-keyword chunk hits (dedup by chunk_id; keep existing on collision)
                scored = self._union_chunk_candidates(scored, kw_hits)
                raise_if_cancelled(cancel_event)
                selected = self._mmr_select_chunks(
                    scored, ids, mat, plan.mmr_k, plan.mmr_lambda)
                ask_stage("retrieve_mmr", _t, recall=len(scored), selected=len(selected))

            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            _t = time.perf_counter()
            raise_if_cancelled(cancel_event)
            if self.llm_client.configured and (selected or kg_id_map):
                # 空 content 有界重试 + 诚实降级 + 可观测(见 _answer_with_retry docstring)。
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    lambda: (self._answer_mix(
                                 question, selected, kg_block, kg_id_map, history,
                                 cancel_event=cancel_event, notebook_id=notebook_id)
                             if overlay_on else
                             self._answer_chunks(
                                 question, selected, history, cancel_event=cancel_event,
                                 notebook_id=notebook_id)),
                    getattr(self.llm_client, "model", None) or self.settings.openai_compat_model)
                synth_failed = not _ok
            ask_stage("answer_llm", _t)

            # 引用绑回 chunk。mix:绑到被答案引用的 chunk anchor(候选池大,不可全列)。
            # 非 mix:每个精选 chunk 一条(字节等价于历史)。
            # tier:selected 通常单库(personal),但概念漫游(PPR,第三路 merge 进
            # _mix_retrieve 的 candidates)可掺 base 库 chunk——那些 c.notebook_id
            # 非空;其余(单库路径)留空,回退本次 ask 的 notebook_id。一次批量
            # 查询解出 {notebook_id: tier},citations 数量再多也只查一次。
            citations: List[Citation] = []
            raise_if_cancelled(cancel_event)
            chunk_tier_map = self._tier_map_for(
                {c.notebook_id or notebook_id for c in selected})
            def _chunk_tier(c) -> str:
                return chunk_tier_map.get(c.notebook_id or notebook_id, "personal")
            if overlay_on:
                by_id = {c.chunk_id: c for c in selected}
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        citations.append(Citation(
                            label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200],
                            tier=_chunk_tier(c)))
            else:
                for c in selected:
                    eid = c.element_ids[0] if c.element_ids else ""
                    citations.append(Citation(
                        label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                        source_id=c.source_id, element_id=eid,
                        location_label=c.section_path, quoted_span=c.text[:200],
                        tier=_chunk_tier(c)))

            # grounding 在 chunk∪KG 合并集上;各项用其融合 relevance(rerank 分不参与)。
            combined_hits = list(selected) + list(kg_hits)
            evidence_level, top_relevance = classify_evidence(
                combined_hits, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            grounded = evidence_level == "grounded"

            if answer:
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                llm_mode = "grounded" if grounded else "ungrounded"
            elif synth_failed:
                # 诚实降级:LLM 跑了但没产出答案(空 content 或抛错)——不冒充成
                # "Retrieved N passage(s)" 那样的成功样子;如实说明并保留下方证据(citations)。
                llm_mode = "synthesis_failed"
                conclusion = (
                    f"已检索到 {len(selected)} 条相关内容,但本次答案合成未产出内容"
                    "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。"
                    if selected else
                    "本次答案合成未产出内容,请重试该问题。")
            else:
                # deterministic:未配 LLM(synth 未跑)→ 有内容仍如实报「检索到 N 段」。
                llm_mode = "deterministic"
                conclusion = (
                    f"Retrieved {len(selected)} relevant passage(s) for this question."
                    if selected else
                    "No indexed content matches this question yet. Upload sources or build chunks.")

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                retrieval_query=retrieval_query, top_relevance=top_relevance)
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
        response.mode = "chunk"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        ask_stage("total", ask_started)
        return response

    def ask(self, notebook_id: str, payload: AskRequest) -> AskResponse:
        """Dispatch to the retrieval handler named by payload.mode, resolved
        through the ask_modes registry. Unknown modes raise UnknownAskMode (the
        API layer returns 422) — never a silent fall-through to the legacy path."""
        from app.services.ask_modes import resolve_mode
        spec = resolve_mode(getattr(payload, "mode", None))
        return getattr(self, spec.handler)(notebook_id, payload)

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
        return self.cluster_map(notebook_id).get(object_id, object_id)

    def _rrf_scored(
        self,
        query: str,
        kg_objs: Dict[str, List[dict]],
        knowledge_sims: Optional[Dict[str, float]],
        element_sims: Optional[Dict[str, float]] = None,
    ) -> List[RetrievedKnowledge]:
        """BM25 + 语义 RRF 融合排序,产出与 score_knowledge 池化同构的列表。

        - BM25: 对所有类型的对象文本计算 Okapi BM25 分。
        - 语义: 直接使用 knowledge_sims (object_id->cosine_sim)。
        - RRF: 两组排名融合得最终分。
        - 不套 RELEVANCE_FLOOR(RRF 分量级很小,floor 会清空结果)。
        - score=RRF 分(排序用); relevance=[0,1] 融合(keyword+语义)分,供
          classify_evidence 的 tau 阈值判 grounded(RRF 微分会让全部判 inferred)。
        - weight 取 _TYPE_WEIGHT(类型权威)。
        """
        # 汇集所有类型对象,构建 (id, text) 列表及 id->obj 映射
        docs: List[tuple] = []
        id_to_obj: Dict[str, dict] = {}
        id_to_type: Dict[str, str] = {}
        for t in _KG_TYPES:
            for obj in (kg_objs.get(t) or []):
                oid = obj["id"]
                payload = obj.get("payload", {})
                evidence = obj.get("evidence", [])
                ev_text = " ".join(e.quoted_span for e in evidence)
                text = _payload_text(payload) + (" " + ev_text if ev_text else "")
                docs.append((oid, text))
                id_to_obj[oid] = obj
                id_to_type[oid] = t

        bm25 = bm25_scores(query, docs)
        sims: Dict[str, float] = knowledge_sims or {}

        fused = rrf_fuse([bm25, sims], k=self.settings.retrieval_rrf_k)
        text_by_id = dict(docs)

        result: List[RetrievedKnowledge] = []
        for oid, rrf_score in fused.items():
            if rrf_score <= 0:
                continue
            obj = id_to_obj.get(oid)
            if obj is None:
                continue
            object_type = id_to_type[oid]
            weight = _TYPE_WEIGHT.get(object_type, 0.5)
            # score = RRF (ordering); relevance = [0,1] fused keyword+semantic so
            # classify_evidence's tau thresholds stay valid (RRF micro-scores would
            # otherwise classify every answer as "inferred").
            # Best-of: object-level sim OR max(element-level sims), same as
            # score_knowledge — protects the dual-index invariant so an object
            # grounded only via an evidence-element embedding is not downgraded.
            semantic = sims.get(oid, 0.0)
            has_vec = oid in sims
            if element_sims:
                for ev in obj.get("evidence", []):
                    eid = getattr(ev, "element_id", "") or ""
                    s = element_sims.get(eid)
                    if s is not None:
                        has_vec = True
                        semantic = max(semantic, s)
            relevance = _fuse(keyword_score(query, text_by_id.get(oid, "")),
                              semantic, has_vec)
            result.append(
                RetrievedKnowledge(
                    object_id=oid,
                    object_type=object_type,
                    payload=obj.get("payload", {}),
                    evidence=obj.get("evidence", []),
                    score=rrf_score,
                    relevance=relevance,
                    weight=weight,
                    status=str(obj.get("status", "approved")),
                    owner=str(obj.get("owner", "")),
                    last_reviewed=str(obj.get("last_reviewed", "")),
                )
            )
        result.sort(key=lambda it: it.score, reverse=True)
        return result

    def _graph_seed_fusion(
        self,
        notebook_id: str,
        question: str,
        base_seeds: List[str],
        cancel_event: CancelEvent = None,
    ) -> List[str]:
        """flag 关 → 原样返回 base_seeds(等价护栏:node recall 不降由「只增不减」保证)。
        flag 开 → 用 high-level keywords 查关系索引,两端 object 并入;low-level
        keywords 额外查节点并入。去重保序:base 在前(只增不减),其后并入关系两端
        (≤2·relation_seed_top_n)与 low-level 节点命中(≤relation_seed_top_n);
        multihop 自身有 max_depth/max_fan_out 兜底,此处不再硬截。"""
        if not self.settings.relation_retrieval_enabled:
            return base_seeds
        from app.services.query_rewrite import expand_query
        raise_if_cancelled(cancel_event)
        exp = expand_query(self.rewrite_llm_client, question,
                           corpus_langs=self._notebook_langs(notebook_id),
                           cancel_event=cancel_event)
        hl = " ".join(exp.high_level_keywords) or exp.query or question
        ll = " ".join(exp.low_level_keywords)
        extra: List[str] = []
        rel_hits = self.federated_retrieve_relations(notebook_id, hl)[
            : self.settings.relation_seed_top_n]
        raise_if_cancelled(cancel_event)
        for h in rel_hits:
            extra.extend((h.source_object_id, h.target_object_id))
        if ll:
            node_hits = self.federated_retrieve(notebook_id, ll)[
                : self.settings.relation_seed_top_n]
            raise_if_cancelled(cancel_event)
            extra.extend(h.object_id for h in node_hits)
        seen, fused = set(), []
        for oid in list(base_seeds) + extra:   # base 优先保序,只增不减
            if oid and oid not in seen:
                seen.add(oid)
                fused.append(oid)
        return fused

    _MIX_NODE_SEEDS = 20
    _MIX_REL_SEEDS = 10
    _MIX_FANOUT = 8

    def _chunk_kg_overlay(self, notebook_id: str, query: str, hl: str, id_offset: int):
        """种子(节点∪关系端点)→1-hop 子图→渲染。返回 (block, id_map, kg_hits)。
        kg_hits=种子命中(带 .relevance),供 grounding。无 KG/种子 → ("", {}, [])。

        生产事故修复(2026-07):大库守卫必须在任何检索之前 —— 种子收集本身
        (federated_retrieve + federated_retrieve_relations)不是免费的:后者
        branch-3(_retrieve_relations_scored 向量覆盖非空场景)会加载
        _vector_matrix(nb, "relation_embeddings") 全量关系向量矩阵,生产环境
        百万级行 × 1024 维即数 GB(若行仍是回填中的 JSON 文本更是灾难级解析
        耗时/挂起)。守卫若留在种子收集之后,大库场景这些种子会被立即丢弃
        (直接 return ("", {}, []))——白算 + 可能挂起。挪到顶部后大库行为
        字节不变(同样的空 overlay + 同样的 graph_walk_refused 事件),只是不
        再触发任何检索;小库字节不变。真正的修复是给关系建 ANN 索引(镜像
        chunk_ann,scale index 侧)——这个守卫只是在那之前把 ask 路径钳制在
        O(bounded)。"""
        if self._federated_graph_is_large(notebook_id):
            self.event_log.emit({
                "kind": "graph_walk_refused",
                "notebook_id": notebook_id,
                "reason": "large_notebook",
                "site": "chunk_kg_overlay",
            })
            return "", {}, []
        from app.services.kg.graph_reason import multihop_subgraph, render_subgraph_context
        node_hits = self.federated_retrieve(notebook_id, query)[: self._MIX_NODE_SEEDS]
        rel_hits = self.federated_retrieve_relations(notebook_id, hl or query)[: self._MIX_REL_SEEDS]
        seeds = [h.object_id for h in node_hits]
        for r in rel_hits:
            seeds.extend((r.source_object_id, r.target_object_id))
        seeds = list(dict.fromkeys(s for s in seeds if s))
        if not seeds:
            return "", {}, []
        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
        if G is None or G.num_nodes() == 0:
            return "", {}, []
        subgraph = multihop_subgraph(G, oid_to_idx, idx_to_oid, seed_ids=seeds,
                                     edge_types=None, max_depth=1, max_fan_out=self._MIX_FANOUT)
        if not subgraph:
            return "", {}, []
        block, id_map = render_subgraph_context(subgraph, id_offset=id_offset)
        return block, id_map, node_hits

    def _elem_chunk_map(self, notebook_id: str) -> Dict[str, list]:
        """Cached {element_id: [chunk_id, ...]} — one chunks scan per version,
        reused by both _kg_source_chunks (per-query, few object_ids) and
        _ent_chunk_map (whole-notebook membership for PPR). P0-5: this used to
        be re-scanned (all chunks, per-row json.loads) on every call of either
        consumer; now it's version-cached like _vector_matrix/_keyword_token_sets."""
        version = tuple(self._scale_index_version(notebook_id))

        def _load():
            with self._connect() as db:
                chunk_rows = db.execute(
                    "SELECT id, element_ids FROM chunks WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchall()
            out: Dict[str, list] = {}
            for cr in chunk_rows:
                for el in json.loads(cr["element_ids"] or "[]"):
                    out.setdefault(el, []).append(cr["id"])
            return out

        return self._vector_cache.get(f"{notebook_id}:elemchunk", version, _load)

    def _kg_source_chunks(self, notebook_id: str, object_ids: list) -> list:
        """KG 对象 evidence 的 element_id → 含该 element 的 chunk(LightRAG 源 chunk)。
        返回 List[RetrievedChunk](relevance 占位 0.3,后续 rerank 重排)。

        P0-5: object_ids 是本次查询命中的一小撮 KG 对象(不是全库),所以 evidence
        只按 IN(...) 取这几行;element_id → chunk 的反查改走缓存的 _elem_chunk_map,
        不再对 chunks 表做全量扫描 + 逐行 json.loads + 集合交。

        输出序 = 确定性 first-seen 序:按 object_ids 顺序 → 各对象 evidence 内
        element 顺序 → _elem_chunk_map 内 chunk 列表序(即 chunks 扫描序)。
        消费方 ask_graph 的 BFS 兜底路径无 rerank,顺序直接决定
        truncate_by_tokens 的截断存活集和引用编号,所以序必须确定(旧实现的
        「chunks 全表扫描序」依赖表物理序,本就不是契约)。"""
        from app.services.retrieval import RetrievedChunk
        if not object_ids:
            return []
        with self._connect() as db:
            ph = ",".join("?" * len(object_ids))
            erows = db.execute(
                f"SELECT id, evidence FROM knowledge_objects WHERE id IN ({ph})",
                list(object_ids)).fetchall()
            # SQL IN(...) 不保证返回序 — 按 object_ids 输入序重放,evidence 内保持
            # JSON 数组序,element_id 有序去重(dict.fromkeys 语义)。
            ev_by_id = {r["id"]: r["evidence"] for r in erows}
            elem_ids: list = []
            seen_el = set()
            for oid in object_ids:
                for e in json.loads(ev_by_id.get(oid) or "[]"):
                    el = e.get("element_id") if isinstance(e, dict) else None
                    if el and el not in seen_el:
                        seen_el.add(el)
                        elem_ids.append(el)
            if not elem_ids:
                return []
            elem_map = self._elem_chunk_map(notebook_id)
            chunk_ids: list = []
            seen_cid = set()
            for el in elem_ids:
                for cid in elem_map.get(el, ()):
                    if cid not in seen_cid:
                        seen_cid.add(cid)
                        chunk_ids.append(cid)
            if not chunk_ids:
                return []
            ph2 = ",".join("?" * len(chunk_ids))
            crows = db.execute(
                f"SELECT id, source_id, text, section_path, element_ids FROM chunks WHERE id IN ({ph2})",
                chunk_ids).fetchall()
        by_id = {cr["id"]: cr for cr in crows}
        out = []
        for cid in chunk_ids:
            cr = by_id.get(cid)
            if cr is None:
                continue
            out.append(RetrievedChunk(
                chunk_id=cr["id"], source_id=cr["source_id"], source_title="",
                section_path=cr["section_path"], text=cr["text"],
                element_ids=json.loads(cr["element_ids"] or "[]"), relevance=0.3))
        return out

    def _ent_chunk_map(self, notebook_id: str) -> Dict[str, set]:
        """{object_id: set(chunk_id)} — KG 实体出现在哪些 chunk 里。
        口径同 _kg_source_chunks:evidence[].element_id ∈ chunks.element_ids[]。
        用于 PPR 的 membership 边 + (P2) specificity 权重分母。

        P0-5: version-cached like _vector_matrix — this used to full-scan ALL
        knowledge_objects.evidence + ALL chunks.element_ids (with per-row
        json.loads) on every call, uncached, on the PPR-fallback query path."""
        version = tuple(self._scale_index_version(notebook_id))

        def _load():
            with self._connect() as db:
                obj_rows = db.execute(
                    "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?",
                    (notebook_id,),
                ).fetchall()
            elem_to_chunks = self._elem_chunk_map(notebook_id)
            out: Dict[str, set] = {}
            for orow in obj_rows:
                chunks: set = set()
                for e in json.loads(orow["evidence"] or "[]"):
                    if isinstance(e, dict) and e.get("element_id"):
                        chunks |= set(elem_to_chunks.get(e["element_id"], ()))
                if chunks:
                    out[orow["id"]] = chunks
            return out

        return self._vector_cache.get(f"{notebook_id}:entchunk", version, _load)

    # ── chunk×graph mix ──────────────────────────────────────────────────────

    _MIX_KG_KEY_BASE = 1000
    _MIX_PROMPT_BUFFER_TOKENS = 2000

    def _truncate_kg_block(self, block: str, max_tokens: int) -> str:
        """按行截断 KG block 至 token 预算(整行保留)。被截掉的行其 [k] 仍留在 id_map,
        _parse_answer_anchors 解析无害(只损失上下文,不破坏引用)。镜像 truncate_by_tokens。"""
        from app.services.retrieval import est_tokens
        if not block or est_tokens(block) <= max_tokens:
            return block
        out, used = [], 0
        for ln in block.split("\n"):
            used += est_tokens(ln)
            if used > max_tokens and out:
                break
            out.append(ln)
        return "\n".join(out)

    def _gather_vector_chunks(self, notebook_id: str, sub_queries: list) -> list:
        """向量 chunk 候选(多子查询合并去重;单查询直接 scored)。返回 List[RetrievedChunk]。"""
        if len(sub_queries) >= 2:
            collected, _per, _ids, _mat = self._retrieve_chunks_multi(notebook_id, sub_queries)
            seen, out = set(), []
            for c in collected.values():
                if c.chunk_id not in seen:
                    seen.add(c.chunk_id)
                    out.append(c)
            return out
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, sub_queries[0])
        return scored

    def _mix_retrieve(self, notebook_id: str, query: str, hl: str, sub_queries: list) -> tuple:
        """三路 mix:向量 chunk + KG-overlay 源 chunk + 概念漫游(PPR)跨文档 chunk,
        round-robin 并池去重。返回 (candidates, kg_block, kg_id_map, kg_hits, ppr_count)。
        PPR 跨文档扩散的噪声由 ask_chunk 侧现成 rerank 免费压低。"""
        vector_chunks = self._gather_vector_chunks(notebook_id, sub_queries)
        kg_block, kg_id_map, kg_hits, kg_chunks = "", {}, [], []
        overlay_on = self.settings.chunk_kg_overlay_enabled and (
            self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg())
        if overlay_on:
            kg_block, kg_id_map, kg_hits = self._chunk_kg_overlay(
                notebook_id, query, hl, id_offset=self._MIX_KG_KEY_BASE)
            kg_chunks = self._kg_source_chunks(
                notebook_id, [v["object_id"] for v in kg_id_map.values()])
        # 概念漫游(PPR)第 3 路:gated GRAPH_PPR_ENABLED;无 KG/无 reset → []。
        ppr_chunks = self._ppr_retrieve(notebook_id, query) if self.settings.graph_ppr_enabled else []
        merged, seen = [], set()
        for i in range(max(len(vector_chunks), len(kg_chunks), len(ppr_chunks))):
            for src in (vector_chunks, kg_chunks, ppr_chunks):
                if i < len(src) and src[i].chunk_id not in seen:
                    seen.add(src[i].chunk_id)
                    merged.append(src[i])
        return merged, kg_block, kg_id_map, kg_hits, len(ppr_chunks)

    def _participant_notebook_ids(self, notebook_id: str) -> List[str]:
        """联邦参与库:active 在首位 + 全部 base tier(与 _ppr_graph/federated_retrieve
        的内联谓词一致;此 helper v1 只供新代码使用,存量调用点不迁移)。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (notebook_id,)).fetchall()
        return [notebook_id] + [r["id"] for r in rows]

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0) -> tuple:
        """Build the id-tagged enriched context block + id_map for the answer
        LLM. Each surviving hit gets a stable `k{i}` id; enrichment (definition /
        first-occurrence snippet / procedure steps) is pulled via node_context.
        Hits belonging to the same unified cluster (any type) are collapsed —
        the first (highest-scored) per cluster is kept, later duplicates dropped.
        Objects without a cluster entry use their own object_id as cluster key,
        so they are never erroneously deduplicated.
        Returns (context_block_str, id_map)."""
        budget = self.settings.answer_context_budget_chars
        min_items = self.settings.answer_context_min_items
        lines, id_map = [], {}
        seen_clusters: set = set()
        # Federation fold: merge every participant notebook's cluster_map so a base
        # hit and an active hit sharing a canonical id (e.g. "K-cascode") collapse
        # to one line. Concept canonical ids are name-derived (deterministic across
        # notebooks), so cross-tier same-name concepts fold correctly.
        cmap: Dict[str, str] = {}
        participants = self._participant_notebook_ids(notebook_id)
        for nb in participants:
            cmap.update(self.cluster_map(nb))
        used = 0
        i = 0
        for hit in top_hits:
            cid = cmap.get(hit.object_id, hit.object_id)
            if cid in seen_clusters:
                continue
            seen_clusters.add(cid)
            # Federation: enrich each hit against ITS OWN notebook (a base hit
            # lives in the base KG, not the active notebook). Falls back to the
            # active notebook_id for legacy/untagged hits.
            hit_nb = getattr(hit, "notebook_id", "") or notebook_id
            try:
                ctx = self.node_context(hit_nb, hit.object_id)
            except KeyError:
                continue
            # Stop once the budget is spent, but always keep at least min_items.
            if used >= budget and len(lines) >= min_items:
                break
            i += 1
            key = f"k{i + id_offset}"
            name = str(hit.payload.get("name", "")).strip()
            occ = ctx.get("occurrences") or []
            snippet = occ[0].get("element_text") if occ else ""
            definition = ctx.get("definition") or snippet
            remaining = max(0, budget - used)
            def_cap = max(0, min(300, remaining))   # per-line cap shrinks as budget fills
            extra = f" — def: {definition[:def_cap]}" if (definition and def_cap) else ""
            if ctx.get("steps") and def_cap:   # steps share the per-line budget gate
                extra += "; steps: " + " -> ".join(
                    s.get("name", "") for s in ctx["steps"][:8]
                )
            tier = getattr(hit, "tier", "personal")
            # Tier prefix surfaces authority to the LLM ([base] vs [personal]) so
            # the conflict-precedence rule in answer_prompt has something to read.
            line = f"{key}: [{hit.object_type}][{tier}] {name}{extra}"
            lines.append(line)
            used += len(line)
            id_map[key] = {
                "object_id": hit.object_id, "object_type": hit.object_type,
                "name": name, "definition": definition, "snippet": snippet,
                "source_title": (occ[0].get("source_title", "") if occ else ""),
                "location_label": (occ[0].get("section_path", "") if occ else ""),
                "tier": tier,
            }
        # In-network relations: edges whose BOTH endpoints are in the context.
        # id_map values carry unique object_ids (one entry per surviving hit;
        # concept-cluster de-dup runs above), so this inversion drops no keys.
        oid_to_key = {v["object_id"]: k for k, v in id_map.items()}
        if len(oid_to_key) >= 2:
            ids = list(oid_to_key)
            ph = ",".join("?" for _ in ids)
            rel_rows: List[tuple] = []   # (s_key, edge_type, t_key, src_nb, s_oid, t_oid)
            seen_rel = set()
            # Federation: an active-notebook hit and a base hit can both be in
            # context, so the edge linking them may live in EITHER notebook. One
            # IN query per participant (participants <= active + bases).
            with self._connect() as db:
                for nb in participants:
                    for r in db.execute(
                            f"SELECT source_object_id, target_object_id, edge_type "
                            f"FROM knowledge_relations WHERE notebook_id=? "
                            f"AND review_status!='rejected' "
                            f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                            [nb, *ids, *ids]).fetchall():
                        s = oid_to_key.get(r["source_object_id"])
                        t = oid_to_key.get(r["target_object_id"])
                        if s and t and s != t and (s, r["edge_type"], t) not in seen_rel:
                            seen_rel.add((s, r["edge_type"], t))
                            rel_rows.append((s, r["edge_type"], t, nb,
                                             r["source_object_id"], r["target_object_id"]))
            if rel_rows:
                # Rank by canonical source_count (breadth of support) so the most
                # corroborated edges survive the cap. _edge_support_map / cluster_map
                # are version-cached → these lookups are zero extra SQL over <=30 rows.
                def _support(row):
                    s_key, et, t_key, nb, s_oid, t_oid = row
                    sup = self._edge_support_map(nb)
                    cm = self.cluster_map(nb)
                    hit = sup.get((cm.get(s_oid, s_oid), et, cm.get(t_oid, t_oid)))
                    return hit[1] if hit else 1
                rel_rows.sort(key=_support, reverse=True)
                rel_lines = []
                # Cap so a dense subgraph can't blow the answer context past budget
                # (applied AFTER sorting by support, so the strongest edges win).
                for row in rel_rows[:30]:
                    s_key, et, t_key = row[0], row[1], row[2]
                    n_src = _support(row)
                    suffix = f" (×{n_src}源)" if n_src >= 2 else ""
                    rel_lines.append(f"{s_key} -[{et}]-> {t_key}{suffix}")
                lines.append("relations: " + "; ".join(rel_lines))
        return ("\n".join(lines) if lines else "(none)"), id_map

    def _rewrite_followup_query(
        self,
        history: str,
        question: str,
        cancel_event: CancelEvent = None,
    ) -> str:
        """Resolve an elliptical follow-up into a standalone retrieval query using
        prior turns. Runs whenever there IS history (any non-first turn) — the
        rewrite model itself returns the question unchanged when it's already
        standalone, so we no longer pre-gate with a brittle keyword heuristic.
        Uses the dedicated fast rewrite model (rewrite_llm_client); always falls
        back to the raw question on any failure."""
        if not history.strip():
            return question
        client = self.rewrite_llm_client
        if not getattr(client, "configured", False):
            return question
        raise_if_cancelled(cancel_event)
        try:
            raw = client.chat_json(
                [{"role": "user", "content": followup_rewrite_prompt(history, question)}],
                FOLLOWUP_REWRITE_SCHEMA_HINT,
                cancel_event=cancel_event,
            )
            data = json.loads(raw)
            if not isinstance(data, dict):
                return question
            rewritten = str(data.get("query", "")).strip()
            return rewritten or question
        except AskCancelled:
            raise
        except Exception:
            return question

    def _refine_context(
        self,
        question: str,
        context_block: str,
        client,
        cancel_event: CancelEvent = None,
    ) -> str:
        """问题感知证据精炼:把 context_block 喂给 evidence_refine LLM,抽"相关要点"
        前置成聚焦上下文(参考性,不产生 [k] 锚点)。默认开(kg_query_refine_enabled);
        client 未配/失败/无内容 → 原样返回。reasoning 传 reasoning_llm_client、graph
        传 llm_client。"""
        if not (self.settings.kg_query_refine_enabled
                and getattr(client, "configured", False)
                and context_block.strip() and context_block.strip() != "(none)"):
            return context_block
        raise_if_cancelled(cancel_event)
        from app.services.prompts import evidence_refine_prompt, EVIDENCE_REFINE_SCHEMA_HINT
        ev_block = context_block[: self.settings.query_refine_max_chars]
        try:
            raw = client.chat_json(
                [{"role": "user", "content": evidence_refine_prompt(question, ev_block)}],
                EVIDENCE_REFINE_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries,
                cancel_event=cancel_event)
            rel = json.loads(raw).get("relevant")
            if not isinstance(rel, list):
                rel = []
            rel = [str(x).strip() for x in rel if str(x).strip()]
        except AskCancelled:
            raise
        except Exception:
            rel = []
        if rel:
            context_block = ("Focused relevant evidence (for this question):\n"
                             + "\n".join(f"- {x}" for x in rel[:12])
                             + "\n\n" + context_block)
        return context_block

    def _answer_with_retry(self, synth, model_label):
        """答案合成有界重试(治思考型模型偶发空 content)。synth() 返回
        (answer, grounded, anchors);answer 空(思考型模型偶把输出预算耗在
        reasoning_content 上→content 空→chat_json 兜底 "{}"→空 answer,不抛异常、
        status=ok)或抛错 → 重试一次("{}" 不入 LLM 缓存,故重试是真·重掷);两次皆空/
        抛错 → emit 一条 model_error(空 content 本身静默不可见,补此条让"检索到却答不出"
        可追踪:前端横幅 + events.jsonl)。返回 (answer, grounded, anchors, ok)。"""
        answer, grounded, anchors = "", False, []
        for _ in range(2):
            try:
                answer, grounded, anchors = synth()
            except AskCancelled:
                raise
            except Exception as exc:
                self._note_model_error("answer", model_label, exc)
                answer, grounded, anchors = "", False, []
            if answer:
                return answer, grounded, anchors, True
        self._note_model_error("answer", model_label, RuntimeError(
            "answer synthesis produced empty content after retry "
            "(reasoning model likely spent output budget on discarded chain-of-thought)"))
        return answer, grounded, anchors, False

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
        """Synthesise the reasoning-mode answer. When PPR chunks are present they
        become first-class [k]-citable evidence: chunk segment k1..N + KG reasoning
        chain segment k1001+ (mirrors _answer_mix's keying), still via the reasoning
        client. Otherwise KG-only (legacy). search_elements passages stay
        reference-only (no [k] id). Returns (answer, llm_grounded, anchors)."""
        raise_if_cancelled(cancel_event)
        chunks = chunks or []
        chains = chains or []
        if chunks:
            # 按相关度降序(_chunk_answer_context 自带 char 预算,保留最相关;跨 PPR run
            # 的归一分仅大致可比,只影响预算边缘取舍,不破坏 [0,1]);chunk 段 k1..N + KG 段
            # k1001+,合并 id_map,两段都可 [k] 引用。无需 _answer_mix 的 base-1 截断:chunk
            # 数 ≤ ppr_top_chunks×(1 seed + _MAX_PPR_RETRIEVES) ≪ _MIX_KG_KEY_BASE(1000)。
            ordered = sorted(chunks, key=lambda c: (-c.relevance, c.chunk_id))
            chunk_block, chunk_id_map = self._chunk_answer_context(
                ordered, notebook_id=notebook_id)
            kg_block, kg_id_map = self._answer_context(
                notebook_id, top_hits, id_offset=self._MIX_KG_KEY_BASE)
            if kg_block and kg_block != "(none)":
                context_block = f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
            else:
                context_block = chunk_block
            id_map = {**chunk_id_map, **kg_id_map}
        else:
            context_block, id_map = self._answer_context(notebook_id, top_hits)
        if chains:
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_id_map = render_follow_chain_context(chains, id_offset=2000)
            if chain_block and chain_block != "(none)":
                context_block = f"{context_block}\n\n{chain_block}"
                id_map = {**id_map, **chain_id_map}
        if elements:
            extra = "\n".join(
                f"(原文 {i+1}) {el.source_title} · {el.location_label}: {el.text[:200]}"
                for i, el in enumerate(elements[:6])
            )
            context_block = f"{context_block}\n\n补充原文段落(供参考,无引用编号):\n{extra}"
        context_block = self._refine_context(
            question, context_block, self.reasoning_llm_client, cancel_event)
        raw = self.reasoning_llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT,
            timeout=self.settings.reasoning_timeout_seconds,
            max_retries=self.settings.reasoning_max_retries,
            cancel_event=cancel_event,
            **cap_kwargs(self.reasoning_llm_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors

    def _unconfigured_model_response(self, notebook_id: str, question: str,
                                     conversation_id: str, mode: str) -> AskResponse:
        """policy=required 且用户未配主 LLM 时的统一短路响应：携带 model_error 让前端
        横幅提示「请先配置」。优先于"先建 KG"等其它提示——没模型连 KG 都建不了。"""
        msg = "请先在设置中配置你的模型服务"
        response = AskResponse(
            answer_id="", conclusion=msg, conversation_id=conversation_id,
            retrieval_query=question, llm_mode="deterministic")
        response.mode = mode
        response.model_errors = [ModelError(stage="answer", model="", message=msg)]
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response

    def ask_reasoning(
        self,
        notebook_id: str,
        payload: AskRequest,
        on_trace=None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Reasoning-mode ask: agentic plan→retrieve→reflect(自由深挖)→answer。
        检索委托 ReasoningRetriever;答案/证据分档复用 fast 路径口径;响应携带
        reasoning_trace。任何阶段异常不向用户抛出(逐层容错 + 兜底空候选)。"""
        from app.services.reasoning_retrieval import ReasoningRetriever
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        raise_if_cancelled(cancel_event)
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)
        raise_if_cancelled(cancel_event)

        if self.resolve_model_config(self.current_user(), "llm").source == "none":
            return self._unconfigured_model_response(
                notebook_id, question, conversation_id, "reasoning")

        if not (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg()):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有可用的底层(tier=base)KG;"
                           "请先点『构建知识图谱』,或把一个已建图的笔记本设为底层"
                           "(POST /notebooks/{id}/tier)。",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic", kg_required=True)
            response.mode = "reasoning"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id)
            return response

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        # P1-A(本轮 scope):只挂 reasoning 模式。ask_graph/ask_chunk 同样受益,
        # 但等价性回放验证只覆盖了 reasoning——留作后续 fast-follow。
        _emb_token = _ASK_EMBED_CACHE.set({})
        try:
            def checked_trace(step):
                raise_if_cancelled(cancel_event)
                if on_trace:
                    on_trace(step)

            try:
                result = ReasoningRetriever(self, self.settings, cancel_event).run(
                    notebook_id, question, history, on_step=checked_trace)
                top_hits, elements, trace, chunks, chains = (
                    result.top_hits, result.elements, result.trace, result.chunks,
                    result.chains)
            except AskCancelled:
                raise
            except Exception:
                top_hits, elements, trace, chunks, chains = [], [], [], [], []

            registry = self.effective_schemas()
            seen_ids: set = set()
            related_knowledge: List[KnowledgeRecord] = []
            raise_if_cancelled(cancel_event)
            for item in top_hits:
                if item.object_id in seen_ids:
                    continue
                seen_ids.add(item.object_id)
                related_knowledge.append(self._knowledge_record(
                    item.object_type,
                    {"id": item.object_id, "payload": item.payload, "status": item.status,
                     "owner": getattr(item, "owner", ""),
                     "last_reviewed": getattr(item, "last_reviewed", ""),
                     "evidence": item.evidence},
                    registry.get(item.object_type)))
            related_knowledge = related_knowledge[:12]

            cited_element_ids = {ev.element_id for item in top_hits
                                 for ev in item.evidence if ev.element_id}
            citations = self._citations_from(top_hits, cited_element_ids, "KG evidence")

            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            raise_if_cancelled(cancel_event)
            if self.reasoning_llm_client.configured and (top_hits or elements or chunks or chains):
                # 空 content 有界重试 + 诚实降级 + 可观测,统一走 _answer_with_retry(见其 docstring)。
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    lambda: self._answer_reasoning(
                        notebook_id, question, top_hits, elements, history,
                        cancel_event=cancel_event, chunks=chunks, chains=chains),
                    self.settings.reasoning_llm_model or self.settings.openai_compat_model)
                synth_failed = not _ok

            # chunks 直接进证据池:RetrievedChunk.object_id 属性=chunk_id,与 chunk 锚的
            # object_id 对齐,classify_evidence 即可正确计 anchored_rel(守 tau)。
            # Relation anchors need classifier entries, but chain trust is NOT
            # query relevance.  Each chain carries only the relevance of the
            # candidate that authorized that action; unrelated high-scoring hits
            # elsewhere in the answer cannot elevate its anchors over tau.
            from types import SimpleNamespace
            from app.services.kg.follow_chain import chain_anchor_relevances
            relation_relevances = chain_anchor_relevances(chains)
            chain_evidence = [SimpleNamespace(
                object_id=relation_id, relevance=relevance,
            ) for relation_id, relevance in relation_relevances.items()]
            evidence_pool = list(top_hits) + list(chunks) + chain_evidence
            evidence_level, top_relevance = classify_evidence(
                evidence_pool, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            grounded = evidence_level == "grounded"

            if answer:
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                llm_mode = "grounded" if grounded else "ungrounded"
            elif synth_failed:
                # 诚实降级:检索成功但答案合成未产出内容 —— 绝不冒充成 "Found N objects"
                # (那读起来像"成功但偷懒")。如实说明并保留下方证据(related_knowledge/citations)。
                llm_mode = "synthesis_failed"
                conclusion = (
                    f"已检索到 {len(top_hits)} 条相关证据,但本次答案合成未产出内容"
                    "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。"
                    if top_hits else
                    "本次答案合成未产出内容,请重试该问题。")
            else:
                llm_mode = "deterministic"
                conclusion = (
                    "The notebook does not yet contain approved knowledge that matches "
                    "this question. Upload and review sources to build coverage.")

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors,
                related_knowledge=related_knowledge, citations=citations,
                llm_mode=llm_mode, conversation_id=conversation_id,
                retrieval_query=question, top_relevance=top_relevance,
                reasoning_trace=trace or None,
            )
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
            _ASK_EMBED_CACHE.reset(_emb_token)
        response.mode = "reasoning"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response

    def ask_graph(
        self,
        notebook_id: str,
        payload: "AskRequest",
        seed_ids: Optional[List[str]] = None,
        cancel_event: CancelEvent = None,
    ) -> AskResponse:
        """Multi-hop graph reasoning mode.

        1. Retrieve top seeds via federated_retrieve (active + base-tier notebooks).
        2. Build the federated rx graph via _federated_rx_graph.
        3. BFS from seed object_ids along DEFAULT_REASONING_EDGES.
        4. Render subgraph → (context_block, id_map) via render_subgraph_context.
        5. Feed context_block to the existing answer LLM + grounding path.

        The [k] anchor markers, _parse_answer_anchors, and classify_evidence are
        shared helpers reused across ask modes. There is no longer a "fast path" —
        ask_fast was retired in P4-5; _answer_kg also deleted (dead code). Context
        is now query-refined via _refine_context before being fed to the answer LLM.
        """
        from app.services.kg.graph_reason import (
            DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context,
        )
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        raise_if_cancelled(cancel_event)
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)
        raise_if_cancelled(cancel_event)

        if self.resolve_model_config(self.current_user(), "llm").source == "none":
            return self._unconfigured_model_response(
                notebook_id, question, conversation_id, "graph")

        if not (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg()):
            response = AskResponse(
                answer_id="",
                conclusion="本笔记本尚未构建知识图谱,也没有可用的底层(tier=base)KG;"
                           "请先点『构建知识图谱』,或把一个已建图的笔记本设为底层"
                           "(POST /notebooks/{id}/tier)。",
                conversation_id=conversation_id, retrieval_query=question,
                llm_mode="deterministic", kg_required=True)
            response.mode = "graph"
            raise_if_cancelled(cancel_event)
            response.answer_id = self._save_answer(
                notebook_id, question, response, conversation_id)
            return response

        _err_sink: list = []
        _err_token = _ASK_MODEL_ERRORS.set(_err_sink)
        try:
            # Seed: top-N by relevance (federated across base notebooks).
            raise_if_cancelled(cancel_event)
            top_hits = self.federated_retrieve(notebook_id, question)[:self.settings.retrieval_top_n]
            raise_if_cancelled(cancel_event)
            if not top_hits and not seed_ids:
                response = AskResponse(
                    answer_id="",
                    conclusion="The notebook does not yet contain approved knowledge "
                               "that matches this question. Upload and review sources "
                               "to build coverage.",
                    conversation_id=conversation_id, retrieval_query=question,
                    llm_mode="deterministic",
                )
                response.mode = "graph"
                response.model_errors = [ModelError(**e) for e in _err_sink]
                raise_if_cancelled(cancel_event)
                response.answer_id = self._save_answer(
                    notebook_id, question, response, conversation_id)
                return response

            # HippoRAG 式 PPR 跨文档检索(opt-in)。命中即走 chunk 答案路径:PPR 把
            # 别的文档相关 chunk 也召回,_answer_chunks 出 chunk 引用(跨多篇)。
            if self.settings.graph_ppr_enabled:
                raise_if_cancelled(cancel_event)
                ppr_chunks = self._ppr_retrieve(notebook_id, question)
                raise_if_cancelled(cancel_event)
                if ppr_chunks:
                    from app.services.retrieval import RetrievedChunk
                    reports = self.get_community_reports(notebook_id)[: self.settings.ppr_community_context_top_n]
                    community_chunks = [RetrievedChunk(
                        chunk_id=f"community:{i}", source_id="",
                        source_title="Knowledge base theme", section_path=r["title"],
                        text=f"{r['title']}. {r['summary']}", element_ids=[], relevance=1.0)
                        for i, r in enumerate(reports)]
                    ppr_chunks = community_chunks + ppr_chunks
                    answer, llm_grounded, anchors = "", False, []
                    synth_failed = False
                    if getattr(self.llm_client, "configured", False):
                        answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                            lambda: self._answer_chunks(
                                question, ppr_chunks, history, cancel_event=cancel_event,
                                notebook_id=notebook_id),
                            getattr(self.llm_client, "model", None) or self.settings.openai_compat_model)
                        synth_failed = not _ok
                    citations: List[Citation] = []
                    by_id = {c.chunk_id: c for c in ppr_chunks}
                    # ppr_chunks = 合成 community_chunks(无 notebook_id,回退本 nb)
                    # + _ppr_retrieve 结果(可掺 base 库 chunk,notebook_id 已标)。
                    ppr_tier_map = self._tier_map_for(
                        {c.notebook_id or notebook_id for c in ppr_chunks})
                    for a in anchors:
                        if a.object_type == "chunk" and a.object_id in by_id:
                            c = by_id[a.object_id]
                            eid = c.element_ids[0] if c.element_ids else ""
                            citations.append(Citation(
                                label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                                source_id=c.source_id, element_id=eid,
                                location_label=c.section_path, quoted_span=c.text[:200],
                                tier=ppr_tier_map.get(c.notebook_id or notebook_id, "personal")))
                    evidence_level, top_relevance = classify_evidence(
                        ppr_chunks, anchors, llm_grounded,
                        self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                    grounded = evidence_level == "grounded"
                    if answer:
                        conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                        llm_mode = "grounded" if grounded else "ungrounded"
                    elif synth_failed:
                        conclusion = (
                            f"已检索到 {len(ppr_chunks)} 条跨文档相关内容,但本次答案合成未产出内容"
                            "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                        llm_mode = "synthesis_failed"
                    else:
                        conclusion = f"PPR retrieved {len(ppr_chunks)} cross-document passage(s)."
                        llm_mode = "deterministic"
                    from app.models.schemas import TraceStep
                    resp = AskResponse(
                        answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                        evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                        citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                        retrieval_query=question, top_relevance=top_relevance,
                        reasoning_trace=[TraceStep(step_type="ppr",
                            summary=f"概念漫游:跨文档召回 {len(ppr_chunks)} 个 chunk",
                            detail={"chunks": len(ppr_chunks),
                                    "sources": len({c.source_id for c in ppr_chunks})})])
                    resp.mode = "graph"
                    resp.model_errors = [ModelError(**e) for e in _err_sink]
                    raise_if_cancelled(cancel_event)
                    resp.answer_id = self._save_answer(notebook_id, question, resp, conversation_id)
                    return resp

            # 大库守卫(与 _ppr_retrieve 的 Fix 1 同一「大」定义):下方
            # _federated_rx_graph 是全库 rustworkx 建图(Python 边循环),在百万级
            # 节点库上=数十分钟 + 数 GB 内存(与 1.13M 节点 reasoning 冻结同机制)。
            # 空图喂给 multihop_subgraph 的下游是 subgraph=[] → src_chunks=[] →
            # id_map={} → 不调答案 LLM → 只剩 "Graph traversal found 0 node(s)"
            # 的空壳 deterministic 文案 —— 对用户等于空答案。故大库直接早退一条
            # 带解释的降级回答(镜像上方无 KG 时的 deterministic 回答形态),
            # 并发 graph_walk_refused 事件;顺带省掉 _graph_seed_fusion 的
            # expand_query LLM 调用。放在 PPR 分支之后:大库若有 scale 索引,
            # PPR 分支仍可正常出跨文档答案,不受此守卫影响。
            if self._federated_graph_is_large(notebook_id):
                self.event_log.emit({
                    "kind": "graph_walk_refused",
                    "notebook_id": notebook_id,
                    "reason": "large_notebook",
                    "site": "ask_graph",
                })
                response = AskResponse(
                    answer_id="",
                    conclusion="该知识库规模过大,graph 模式的全图漫游在此库上不可用"
                               "(已跳过以避免长时间无响应)。请改用 chunk 或 reasoning "
                               "模式提问;若已构建规模化检索索引(scale index),"
                               "graph 模式的跨文档 PPR 检索仍可正常工作。",
                    conversation_id=conversation_id, retrieval_query=question,
                    llm_mode="deterministic",
                )
                response.mode = "graph"
                response.model_errors = [ModelError(**e) for e in _err_sink]
                raise_if_cancelled(cancel_event)
                response.answer_id = self._save_answer(
                    notebook_id, question, response, conversation_id)
                return response

            base_seeds = seed_ids if seed_ids else [h.object_id for h in top_hits[:5]]
            raise_if_cancelled(cancel_event)
            use_seeds = self._graph_seed_fusion(
                notebook_id, question, base_seeds, cancel_event)

            G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
            raise_if_cancelled(cancel_event)
            subgraph = multihop_subgraph(
                G, oid_to_idx, idx_to_oid,
                seed_ids=use_seeds,
                # TD2: include "synonym" so multihop walks THROUGH the transit-
                # only cross-doc cluster hubs (their member edges are "synonym").
                # Scoped to this call only — DEFAULT_REASONING_EDGES (a frozenset)
                # is NOT broadened globally. The hub node itself is still filtered
                # from the result/render/verify by build_rx_graph + multihop_subgraph
                # (kind="cluster" pass-through), so the LLM never cites a hub.
                edge_types=DEFAULT_REASONING_EDGES | {"synonym"},
                max_depth=getattr(self.settings, "graph_max_depth", 3),
                max_fan_out=getattr(self.settings, "graph_max_fan_out", 8),
            )
            # Render subgraph into (context_block, id_map) — same k{i} format as
            # _answer_context so grouped marker resolution works unchanged.
            context_block, id_map = render_subgraph_context(subgraph, id_offset=0)
            raise_if_cancelled(cancel_event)

            # Answer-time chain verification: an adversarial LLM check per chain edge.
            # Flagged edges get their confidence demoted to 0.05; the context is then
            # re-rendered so the demotion is visible to the answer LLM. chain_trust is
            # the weakest-link confidence over all edges (1.0 when there are no edges).
            verify_result = {"chain_trust": 1.0, "flagged": [], "edge_results": [],
                             "authority_notes": []}
            if getattr(self.reasoning_llm_client, "configured", False):
                from app.services.kg.graph_reason import verify_chain_edges
                verify_result = verify_chain_edges(
                    subgraph, self.reasoning_llm_client,
                    votes=1, timeout=self.settings.reasoning_timeout_seconds,
                    cancel_event=cancel_event,
                )
                raise_if_cancelled(cancel_event)
                if verify_result["flagged"]:
                    flagged_types = {f["edge_type"] for f in verify_result["flagged"]}
                    for _node, edge, _src in subgraph:
                        if edge and edge.get("edge_type") in flagged_types:
                            edge["confidence"] = 0.05
                    context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

            # 原文增强:子图 KG 节点的源 chunk 整段也喂模型(复用 chunk overlay 的 mix)。
            # 有源 chunk → 走 _answer_mix(KG 段 k1001+ / chunk 段 k1..N)、出 chunk 引用、直接 return;
            # 无源 chunk → 落到下方现状 KG-only 答案,行为不变。
            from app.services.retrieval import est_tokens, truncate_by_tokens
            src_chunks = self._kg_source_chunks(
                notebook_id, [n["object_id"] for n, _e, _s in subgraph])
            if src_chunks:
                mix_kg_block, mix_id_map = render_subgraph_context(
                    subgraph, id_offset=self._MIX_KG_KEY_BASE)
                mix_kg_block = self._truncate_kg_block(
                    mix_kg_block,
                    self.settings.max_entity_tokens + self.settings.max_relation_tokens)
                chunk_budget = max(0, self.settings.max_total_tokens
                                   - est_tokens(mix_kg_block) - self._MIX_PROMPT_BUFFER_TOKENS)
                src_chunks = truncate_by_tokens(src_chunks, lambda c: c.text, chunk_budget)
                # 源 chunk 的 source_title 补全(供引用标签;_kg_source_chunks 留空)
                with self._connect() as _db:
                    _sids = list({c.source_id for c in src_chunks})
                    _titles = {r["id"]: r["title"] for r in _db.execute(
                        f"SELECT id, title FROM sources WHERE id IN ({','.join('?' for _ in _sids)})",
                        _sids).fetchall()} if _sids else {}
                for c in src_chunks:
                    c.source_title = _titles.get(c.source_id, "")
                answer, llm_grounded, anchors = "", False, []
                synth_failed = False
                if getattr(self.llm_client, "configured", False):
                    answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                        lambda: self._answer_mix(
                            question, src_chunks, mix_kg_block, mix_id_map, history,
                            cancel_event=cancel_event, notebook_id=notebook_id),
                        getattr(self.llm_client, "model", None) or self.settings.openai_compat_model)
                    synth_failed = not _ok
                citations: List[Citation] = []
                by_id = {c.chunk_id: c for c in src_chunks}
                # src_chunks 来自 _kg_source_chunks(notebook_id, ...):subgraph 节点虽可能
                # 跨 base(_federated_rx_graph),但 element→chunk 反查经 _elem_chunk_map(
                # notebook_id) 单库范围,base 节点的 element 天生查不到 chunk——凡是这里
                # 真返回的 chunk 必属 notebook_id 自己,故只需查这一个 notebook 的 tier。
                src_chunk_tier = self._tier_map_for({notebook_id}).get(notebook_id, "personal")
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        citations.append(Citation(
                            label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200],
                            tier=src_chunk_tier))
                evidence_level, top_relevance = classify_evidence(
                    src_chunks, anchors, llm_grounded,
                    self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                grounded = evidence_level == "grounded"
                if answer:
                    conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                    llm_mode = "grounded" if grounded else "ungrounded"
                elif synth_failed:
                    conclusion = (
                        f"已检索到 {len(src_chunks)} 段源原文,但本次答案合成未产出内容"
                        "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                    llm_mode = "synthesis_failed"
                else:
                    conclusion = f"Graph retrieved {len(src_chunks)} source passage(s) for this question."
                    llm_mode = "deterministic"
                from app.models.schemas import TraceStep
                resp = AskResponse(
                    answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                    evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                    citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                    retrieval_query=question, top_relevance=top_relevance,
                    reasoning_trace=[TraceStep(step_type="graph_src_chunks",
                        summary=f"BFS 子图 + {len(src_chunks)} 段源原文",
                        detail={"chunks": len(src_chunks),
                                "sources": len({c.source_id for c in src_chunks})})])
                resp.mode = "graph"
                resp.model_errors = [ModelError(**e) for e in _err_sink]
                resp.answer_id = self._save_answer(notebook_id, question, resp, conversation_id)
                return resp

            # Synthesise the answer through the existing LLM + grounding path.
            context_block = self._refine_context(
                question, context_block, self.llm_client, cancel_event)
            answer, llm_grounded, anchors = "", False, []
            synth_failed = False
            raise_if_cancelled(cancel_event)
            if getattr(self.llm_client, "configured", False) and id_map:
                def _synth_kg():
                    raw = self.llm_client.chat_json(
                        [{"role": "user",
                          "content": answer_prompt(question, context_block, history)}],
                        ANSWER_SCHEMA_HINT,
                        cancel_event=cancel_event,
                        **cap_kwargs(self.llm_client, "answer_max_tokens"),
                    )
                    raise_if_cancelled(cancel_event)
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        return "", False, []
                    _ans = str(data.get("answer", "")).strip()
                    _g = bool(data.get("grounded", False))
                    _anc = self._parse_answer_anchors(_ans, id_map)
                    # Scrub citation-shaped tokens that did NOT bind to a real
                    # id_map entry (out-of-map ids like [k99], malformed [ k1]).
                    # Unlike the fast path — whose id_map IS top_hits, so the LLM
                    # rarely invents ids — graph mode shows a wider subgraph and
                    # the answer LLM occasionally emits markers the strict anchor
                    # parser can't bind; left in place they read as fabricated
                    # citations. Strip them so only resolved [k] markers ship.
                    return _strip_unbound_markers(_ans, {a.key for a in _anc}), _g, _anc
                answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                    _synth_kg,
                    getattr(self.llm_client, "model", None) or self.settings.openai_compat_model)
                synth_failed = not _ok

            # classify_evidence keys "grounded" off the relevance of the CITED hit.
            # In the fast path id_map IS built from top_hits, so every anchor is a
            # scored hit. In graph mode the cited node can be a multi-hop NEIGHBOUR
            # that is in id_map but NOT in top_hits → its relevance would read 0 and
            # the answer would be demoted to "overview" even though it cites a real,
            # chain-connected node (the q17/q18 "overview while citing specifics"
            # contradiction). Mirror the fast-path invariant: give each cited
            # neighbour a relevance inherited from the strongest seed, discounted by
            # chain_trust (the verifier's weakest-link confidence), so a trusted
            # chain can reach "grounded" while a flagged/weak one still falls back.
            hits_for_classify = list(top_hits)
            raise_if_cancelled(cancel_event)
            if anchors:
                scored_oids = {h.object_id for h in top_hits}
                seed_rel = max((h.relevance for h in top_hits), default=0.0)
                neighbour_rel = seed_rel * float(verify_result.get("chain_trust", 1.0))
                for a in anchors:
                    if a.object_id in scored_oids:
                        continue
                    scored_oids.add(a.object_id)
                    hits_for_classify.append(RetrievedKnowledge(
                        object_id=a.object_id, object_type=a.object_type,
                        payload={"name": a.name}, relevance=neighbour_rel,
                        tier=getattr(a, "tier", "personal"), notebook_id=notebook_id))

            evidence_level, top_relevance = classify_evidence(
                hits_for_classify, anchors, llm_grounded,
                self.settings.evidence_tau_low, self.settings.evidence_tau_high)
            # Report the genuine seed relevance, not the synthetic neighbour value.
            top_relevance = max((h.relevance for h in top_hits), default=top_relevance)
            grounded = evidence_level == "grounded"
            if answer:
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
                llm_mode = "grounded" if grounded else "ungrounded"
            elif synth_failed:
                conclusion = (
                    f"已检索到 {len(subgraph)} 个相关节点,但本次答案合成未产出内容"
                    "(模型可能把输出预算耗在思维链上)。请重试该问题;下方为已检索到的证据。")
                llm_mode = "synthesis_failed"
            else:
                conclusion = (
                    f"Graph traversal found {len(subgraph)} node(s) across "
                    f"{len(use_seeds)} seed(s).")
                llm_mode = "deterministic"

            from app.models.schemas import TraceStep
            graph_trace = [TraceStep(
                step_type="graph_verify",
                summary=(f"chain_trust={verify_result['chain_trust']:.2f}; "
                         f"{len(verify_result['flagged'])} edge(s) flagged; "
                         f"{len(subgraph)} node(s) traversed"),
                detail={**verify_result,
                        "authority_notes": verify_result.get("authority_notes", [])},
            )]

            response = AskResponse(
                answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                citations=[], llm_mode=llm_mode,
                conversation_id=conversation_id, retrieval_query=question,
                top_relevance=top_relevance, reasoning_trace=graph_trace,
            )
        finally:
            _ASK_MODEL_ERRORS.reset(_err_token)
        response.mode = "graph"
        response.model_errors = [ModelError(**e) for e in _err_sink]
        raise_if_cancelled(cancel_event)
        response.answer_id = self._save_answer(
            notebook_id, question, response, conversation_id)
        return response

    def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
        """Resolve the `[k_i]` markers present in `answer` into AnswerAnchor
        objects (deduped, in first-seen order). Markers not in `id_map` and
        items never cited are dropped."""
        from app.models.schemas import AnswerAnchor
        cited = []
        seen = set()
        for marker_group in _MARKER_GROUP_RE.findall(answer or ""):
            keys = [part.strip() for part in marker_group.split(",")]
            # A mixed known/unknown group is not partially trusted: binding only
            # the known subset would misrepresent which premises the model cited.
            if not keys or any(key not in id_map for key in keys):
                continue
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                ctx = id_map[key]
                name = str(ctx.get("name", ""))
                cited.append(AnswerAnchor(
                    key=key, object_id=ctx["object_id"], object_type=ctx["object_type"],
                    label=(name[:40] or key), name=name,
                    definition=ctx.get("definition"), snippet=ctx.get("snippet"),
                    source_title=ctx.get("source_title", ""), location_label=ctx.get("location_label", ""),
                    tier=ctx.get("tier", "personal"),
                ))
        return cited

    def _needs_index(self, notebook_id: str) -> bool:
        """大库且磁盘完全无 scale 索引(从未建过)→ True。用于 AskResponse.index_required:
        大库检索强制走索引,无索引时检索降级(FTS/skip/refuse),需提示用户手动建索引。
        小库(copyable=True)允许暴力、不要求索引 → False。已建索引(含 stale/有 delta)→
        False(那是恒定成本·最终一致态,由「N 源待索引」徽章覆盖,不重复提示)。
        两处判定都廉价:copystats 版本 memo;_scale_index(allow_stale) 经磁盘身份缓存 O(1)。"""
        try:
            has_index = self._scale_index(notebook_id, allow_stale=True) is not None
            return __import__("app.services.notebook_scale", fromlist=["NotebookScaleProfile"]).NotebookScaleProfile(self.settings, self, lambda nb: tuple(self._scale_index_version(nb)), self._vector_cache).requires_index(notebook_id, has_disk_index=has_index)
        except Exception:  # noqa: BLE001 — 判定失败不拖垮 ask,退化为不提示
            return False

    def _save_answer(
        self,
        notebook_id: str,
        question: str,
        response: AskResponse,
        conversation_id: Optional[str] = None,
    ) -> str:
        # 所有 ask handler 的唯一收口:在持久化/返回前给 response 打大库无索引提示位。
        # 覆盖 chunk/reasoning/graph 三 handler 的全部 return 路径(含早退),避免逐 handler
        # 多 return 点漏赋值。小库/已索引 → False(默认),无副作用。
        response.index_required = self._needs_index(notebook_id)
        answer_id = _new_id("ans")
        now = _now()
        payload = response.model_dump()
        payload["answer_id"] = answer_id
        with self._write() as db:
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    answer_id,
                    notebook_id,
                    question,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    conversation_id,
                ),
            )
        return answer_id

    def _ensure_conversation(
        self, db, notebook_id: str, conversation_id: Optional[str], question: str
    ) -> str:
        """Return the conversation id for this turn: append to an existing
        conversation in this notebook (touching `updated_at`), or create a new
        one (id `conv-<hex>`, title from the first question)."""
        now = _now()
        if conversation_id:
            # 只接续**调用者自己**的对话:共享库里成员传入 owner/他人的 conv-id 不命中,
            # 落到下面新建一条归自己的对话,杜绝跨用户注入回合(read-only 成员经 ask 触达)。
            row = db.execute(
                "SELECT id FROM conversations WHERE id = ? AND notebook_id = ? AND created_by = ?",
                (conversation_id, notebook_id, self.current_user().id),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )
                return conversation_id
        new_id = _new_id("conv")
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id, notebook_id, question[:60], self.current_user().id, now, now),
        )
        return new_id

    def begin_ask_job(self, notebook_id: str, payload, mode: str, cancel_event) -> tuple[str, str]:
        """建/接续会话 + 插入 running 的 ask_jobs 行 + 注册 cancel_event。
        就地把解析出的 conversation_id 写回 payload,使随后的 handler(_ensure_conversation)
        接续同一会话、不另建。返回 (job_id, conversation_id)。"""
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        now = _now()
        job_id = _new_id("askjob")
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            payload.conversation_id = conversation_id
            db.execute(
                "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,mode,question,"
                "status,trace_json,answer_id,error,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?, 'running','','','',?,?)",
                (job_id, notebook_id, conversation_id, self.current_user().id, mode,
                 question[:200], now, now))
        with self._ask_cancel_lock:
            self._ask_cancel_events[job_id] = cancel_event
        return job_id, conversation_id

    def finish_ask_job(self, job_id: str, status: str, *, answer_id: str = "", error: str = "") -> None:
        """终态化 ask_job + 注销 cancel_event；cancelled/failed 时清理空会话(0 答案)。"""
        with self._write() as db:
            row = db.execute("SELECT conversation_id FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
            db.execute(
                "UPDATE ask_jobs SET status=?, answer_id=?, error=?, updated_at=? WHERE id=?",
                (status, answer_id, error, _now(), job_id))
        with self._ask_cancel_lock:
            self._ask_cancel_events.pop(job_id, None)
        if status in ("cancelled", "failed") and row is not None and row["conversation_id"]:
            self._cleanup_empty_conversation(row["conversation_id"])

    def cancel_ask_job(self, job_id: str, user_id: str) -> dict:
        """显式取消(属主校验)。set 在途 worker 的 cancel_event;非属主/不存在 → KeyError。"""
        st = self.ask_job_status(job_id)   # KeyError if missing
        if st["created_by"] != user_id:
            raise KeyError(job_id)
        with self._ask_cancel_lock:
            ev = self._ask_cancel_events.get(job_id)
        if ev is not None:
            ev.set()
        return {"status": "cancelling" if ev is not None else st["status"], "job_id": job_id}

    def ask_job_status(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,status,answer_id,error "
                "FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"], "created_by": row["created_by"],
                "mode": row["mode"], "status": row["status"], "answer_id": row["answer_id"],
                "error": row["error"]}

    def append_ask_trace(self, job_id: str, step: dict) -> None:
        """把一个 trace step 追加进 ask_trace_steps 子表(append-only,O(1) 单行
        INSERT)。seq 用 `SELECT COALESCE(MAX(seq),-1)+1 WHERE job_id=?` 在同一个
        _write() 事务里取号+插入,避免与自己的下一次 append 竞态(虽单 worker 写
        单个 job、无写写竞态,取号+插同事务仍是稳妥做法)。

        perf fast-follow:取代旧版对 ask_jobs.trace_json 单列的「读整个 JSON 数组→
        append→写回」——那是 O(N^2) 累积序列化,且每次都要占用全站唯一的 _write()
        全局写锁,轨迹越长锁持有时间越长。子表把锁持有时间摊平成常数。

        仍 **fail-open**:轨迹持久化失败绝不拖垮 ask。"""
        try:
            with self._write() as db:
                exists = db.execute("SELECT 1 FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
                if exists is None:
                    return
                next_seq = db.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM ask_trace_steps WHERE job_id=?",
                    (job_id,),
                ).fetchone()["n"]
                db.execute(
                    "INSERT INTO ask_trace_steps (job_id, seq, step_json, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (job_id, next_seq, json.dumps(step, ensure_ascii=False), _now()),
                )
        except Exception:  # noqa: BLE001
            self.event_log.logger.exception("append_ask_trace failed for %s", job_id)

    @staticmethod
    def _read_ask_trace(db, job_id: str) -> list:
        """从 ask_trace_steps 子表按 seq 顺序读回一个 job 的完整轨迹,拼成 list。
        单行解析失败(损坏的 step_json)容错跳过而非整体失败——与旧版
        trace_json 列「解析失败即空列表」的粗粒度容错相比更细,但不改变
        「解析失败不抛」这条既有契约。取代直读 ask_jobs.trace_json 列
        (该列已停止写入,只为兼容旧行保留,见 append_ask_trace)。"""
        rows = db.execute(
            "SELECT step_json FROM ask_trace_steps WHERE job_id=? ORDER BY seq ASC",
            (job_id,),
        ).fetchall()
        trace = []
        for r in rows:
            try:
                trace.append(json.loads(r["step_json"]))
            except (TypeError, ValueError):
                continue
        return trace

    def ask_job_detail(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT id,notebook_id,conversation_id,created_by,mode,question,status,"
                "answer_id,error FROM ask_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            trace = self._read_ask_trace(db, job_id)
        return {"job_id": row["id"], "notebook_id": row["notebook_id"],
                "conversation_id": row["conversation_id"], "created_by": row["created_by"],
                "mode": row["mode"], "question": row["question"], "status": row["status"],
                "trace": trace, "answer_id": row["answer_id"], "error": row["error"]}

    def _cleanup_empty_conversation(self, conversation_id: str) -> None:
        """删掉没有任何 answer 的会话(取消首轮留下的空壳);有答案则保留。"""
        with self._write() as db:
            db.execute(
                "DELETE FROM conversations WHERE id=? AND NOT EXISTS "
                "(SELECT 1 FROM answers WHERE conversation_id=?)",
                (conversation_id, conversation_id))

    def _conversation_history(self, db, conversation_id: str, limit: int = 5) -> str:
        """Build the prior-turns history block (oldest->newest, last `limit`
        turns) from stored answer payloads. Uses each turn's `conclusion`
        (provenance markers already stripped). Returns "" when no prior turns."""
        rows = db.execute(
            "SELECT question, payload FROM answers WHERE conversation_id = ? "
            "ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
        rows = rows[-limit:]
        lines = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            conclusion = str(payload.get("conclusion", "")).strip()
            lines.append(f"User: {row['question']}\nAssistant: {conclusion}")
        return "\n".join(lines)

    def get_conversation(self, conversation_id: str) -> "ConversationDetail":
        """Rebuild a ConversationDetail from the conversations row + its answer
        turns. Raises KeyError if the conversation does not exist."""
        from app.models.schemas import (
            ConversationDetail,
            ConversationTurn,
        )
        with self._connect() as db:
            conv = db.execute(
                "SELECT id, notebook_id, title, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conv is None:
                raise KeyError(conversation_id)
            rows = db.execute(
                "SELECT id, question, payload, created_at FROM answers "
                "WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                (conversation_id,),
            ).fetchall()
            job = db.execute(
                "SELECT id, question, mode FROM ask_jobs "
                "WHERE conversation_id=? AND status='running' "
                "ORDER BY created_at DESC LIMIT 1", (conversation_id,)).fetchone()
            job_trace = self._read_ask_trace(db, job["id"]) if job is not None else []
        turns = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            turns.append(
                ConversationTurn(
                    answer_id=row["id"],
                    question=row["question"],
                    response=AskResponse(**payload),
                    created_at=row["created_at"],
                )
            )
        used_reasoning = bool(turns[-1].response.reasoning_trace) if turns else False
        active_job = None
        if job is not None:
            from app.models.schemas import ActiveAskJob
            active_job = ActiveAskJob(job_id=job["id"], question=job["question"] or "",
                                      mode=job["mode"] or "", trace=job_trace)
        return ConversationDetail(
            id=conv["id"],
            notebook_id=conv["notebook_id"],
            title=conv["title"] or "",
            updated_at=conv["updated_at"] or "",
            turn_count=len(turns),
            used_reasoning=used_reasoning,
            turns=turns,
            active_job=active_job,
        )

    def list_conversations(self, notebook_id: str) -> "List[ConversationSummary]":
        """List conversations for a notebook (most-recently-updated first) with
        a per-conversation turn count. Raises KeyError if the notebook is gone."""
        from app.models.schemas import ConversationSummary
        self.get_notebook(notebook_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.id, c.notebook_id, c.title, c.updated_at, "
                "(SELECT COUNT(*) FROM answers a WHERE a.conversation_id = c.id) AS turn_count, "
                "(SELECT COALESCE(json_array_length(json_extract(a.payload, '$.reasoning_trace')), 0) > 0 "
                "   FROM answers a WHERE a.conversation_id = c.id "
                "  ORDER BY a.rowid DESC LIMIT 1) AS used_reasoning "
                "FROM conversations c WHERE c.notebook_id = ? AND c.created_by = ? "
                "ORDER BY c.updated_at DESC",
                (notebook_id, self.current_user().id),
            ).fetchall()
        return [
            ConversationSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"] or "",
                updated_at=row["updated_at"] or "",
                turn_count=row["turn_count"],
                used_reasoning=bool(row["used_reasoning"]),
            )
            for row in rows
        ]

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        with self._write() as db:
            cur = db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (title, _now(), conversation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        with self._write() as db:
            cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            if cur.rowcount == 0:
                raise KeyError(conversation_id)
            db.execute("DELETE FROM answers WHERE conversation_id=?", (conversation_id,))

    def bulk_delete_conversations(self, notebook_id: str, older_than_days: int) -> int:
        """Delete the current user's conversations in `notebook_id` whose last
        activity (`updated_at`) is strictly older than `older_than_days` days,
        cascading to their answers. Returns the number deleted. Raises KeyError
        if the notebook does not exist."""
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        self.get_notebook(notebook_id)
        cutoff = (datetime.now() - timedelta(days=older_than_days)).replace(microsecond=0).isoformat()
        with self._write() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM conversations "
                    "WHERE notebook_id = ? AND created_by = ? AND updated_at < ?",
                    (notebook_id, self.current_user().id, cutoff),
                ).fetchall()
            ]
            db.executemany("DELETE FROM answers WHERE conversation_id = ?", [(cid,) for cid in ids])
            db.executemany("DELETE FROM conversations WHERE id = ?", [(cid,) for cid in ids])
        return len(ids)

    # --- 深度报告 ---
    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str:
        self.get_notebook(notebook_id)          # 不存在则 KeyError
        rid = _new_id("rep")
        now = _now()
        with self._write() as db:
            db.execute(
                "INSERT INTO reports(id, notebook_id, question, depth, created_by, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (rid, notebook_id, question, depth, self.current_user().id, now, now))
        return rid

    def update_report(self, notebook_id: str, report_id: str, *, status=None,
                      progress=None, error=None, outline=None, sections=None,
                      gaps=None, references=None, content_md=None,
                      section_status=None) -> None:
        sets, args = ["updated_at = ?"], [_now()]
        for col, val, dump in (("status", status, False), ("progress", progress, False),
                               ("error", error, False), ("content_md", content_md, False),
                               ("outline_json", outline, True),
                               ("sections_json", sections, True), ("gaps_json", gaps, True),
                               ("references_json", references, True),
                               ("section_status_json", section_status, True)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(json.dumps(val, ensure_ascii=False) if dump else val)
        args.extend([report_id, notebook_id])
        with self._write() as db:
            db.execute(f"UPDATE reports SET {', '.join(sets)} WHERE id = ? AND notebook_id = ?", args)

    def _report_row_to_dict(self, row, *, full: bool) -> dict:
        d = {"id": row["id"], "notebook_id": row["notebook_id"], "question": row["question"],
             "status": row["status"], "progress": row["progress"], "error": row["error"],
             "created_by": row["created_by"], "created_at": row["created_at"],
             "updated_at": row["updated_at"], "depth": row["depth"],
             "section_count": len(json.loads(row["outline_json"] or "[]"))}
        if full:
            d.update(outline=json.loads(row["outline_json"] or "[]"),
                     sections=json.loads(row["sections_json"] or "[]"),
                     gaps=json.loads(row["gaps_json"] or "[]"),
                     references=json.loads(row["references_json"] or "[]"),
                     section_status=json.loads(row["section_status_json"] or "[]"),
                     content_md=row["content_md"])
        return d

    def get_report(self, notebook_id: str, report_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id = ? AND notebook_id = ?",
                             (report_id, notebook_id)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self._report_row_to_dict(row, full=True)

    def list_reports(self, notebook_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM reports WHERE notebook_id = ? ORDER BY created_at DESC, id",
                (notebook_id,)).fetchall()
        return [self._report_row_to_dict(r, full=False) for r in rows]

    def delete_report(self, notebook_id: str, report_id: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM reports WHERE id = ? AND notebook_id = ?",
                       (report_id, notebook_id))

    def export_reports(self, notebook_id: str, report_ids: list) -> list:
        """批量导出:返回 [(filename, content_md)],按传入 report_ids 顺序,只取该
        notebook 下 status='done' 且 content_md 非空的报告(非 done/空/跨 notebook 的
        id 静默跳过)。文件名 = f"{_safe(question)[:40]}-{rid}.md"。

        只读走 _connect()。report_ids 数量通常极小(用户勾选的几份报告),直接构造
        占位符即可;但仍按 _in_batches 分批以防罕见的大批量超 SQLite 变量上限
        (3.32+ 上限 32,766),批间用 dict 汇总后按原顺序回放。"""
        def _safe(name: str) -> str:
            s = re.sub(r'[/\\:*?"<>|\r\n]', "_", name or "").strip()
            return s or ""

        ids = [r for r in (report_ids or []) if r]
        if not ids:
            return []
        found: dict = {}                         # rid -> (question, content_md)
        with self._connect() as db:
            for batch in self._in_batches(ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT id, question, content_md FROM reports "
                    f"WHERE notebook_id = ? AND status = 'done' "
                    f"AND content_md IS NOT NULL AND content_md != '' "
                    f"AND id IN ({placeholders})",
                    (notebook_id, *batch)).fetchall()
                for row in rows:
                    found[row["id"]] = (row["question"], row["content_md"])
        out: list = []
        seen: dict = {}                          # 文件名去重(极端同名 → 加 -N 后缀)
        for rid in ids:                          # 保持传入顺序
            if rid not in found:
                continue
            question, content_md = found[rid]
            stem = _safe(question)[:40] or rid
            fname = f"{stem}-{rid}.md"
            if fname in seen:
                seen[fname] += 1
                fname = f"{stem}-{rid}-{seen[fname]}.md"
            else:
                seen[fname] = 0
            out.append((fname, content_md))
        return out

    def pending_actions(self, user_id: str) -> dict:
        """聚合当前用户「我创建的」notebook 的三类待办(深度报告待确认/治理队列/
        索引状态),供「待确认中心」铃铛使用。REST 与后续流式端点共用同一计算核心。

        只读、无 LLM/embed 调用。严格按 notebooks.created_by = user_id 过滤 ——
        不走 list_notebooks()(它含"分享给我只读"的库,会破坏用户间隔离)。
        index 项的状态分类完全委托给已有的 scale_index_status(nb).state,不重新
        实现索引状态机;building/queued 视为进行中、不计入 count(不是待用户确认的
        动作),但仍作为 item 呈现供前端展示进度。

        「晋升候选」子项仅对 admin 呈现 —— 非 admin 也能创建晋升候选
        (propose_promotion 只受 require_notebook_access 守卫),但深链目标
        /promotion-queue 是 admin-only(403),故对非 admin 隐藏该项以免铃铛
        指向一个必 403 的动作。"""
        projection = self.pending_actions_projection_rows(user_id)
        items = projection["items"]
        nb_ids = projection["notebook_ids"]
        name_of = projection["notebook_names"]

        # ③ 索引状态(scale_index_status 自管连接,故在上面的 with 块外调用)
        for nb_id in nb_ids:
            try:
                st = self.scale_index_status(nb_id)
            except Exception:  # noqa: BLE001 — 单库状态异常不拖垮整个中心
                continue
            state = st.get("state")
            if state in ("stale", "suggested", "building", "queued"):
                item = {
                    "type": "index",
                    "state": "building" if state == "queued" else state,
                    "notebook_id": nb_id,
                    "notebook_name": name_of.get(nb_id, ""),
                }
                total = st.get("total_chunks") or 0
                delta = st.get("delta_chunks") or 0
                if state in ("building", "queued") and total:
                    item["progress"] = round(100.0 * max(0, total - delta) / total)
                items.append(item)

        actionable = sum(
            1 for it in items
            if it["type"] in ("report_outline", "governance")
            or (it["type"] == "index" and it["state"] in ("stale", "suggested"))
        )
        return {"count": actionable, "items": items}

    def submit_feedback(self, answer_id: str, payload: FeedbackRequest) -> FeedbackResponse:
        if payload.rating not in {"useful", "not_useful"}:
            raise ValueError("rating must be useful or not_useful")
        now = _now()
        feedback_id = _new_id("fb")
        with self._write() as db:
            answer = db.execute(
                "SELECT notebook_id FROM answers WHERE id = ?",
                (answer_id,),
            ).fetchone()
            if answer is None:
                raise KeyError(answer_id)
            db.execute(
                "INSERT INTO feedback (id, answer_id, notebook_id, rating, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (feedback_id, answer_id, answer["notebook_id"], payload.rating, payload.comment, now),
            )
        return FeedbackResponse(
            id=feedback_id,
            answer_id=answer_id,
            rating=payload.rating,
            comment=payload.comment,
        )

    def notebook_analytics(self, notebook_id: str) -> NotebookAnalytics:
        return self._runtime.catalog.notebook_analytics(notebook_id)

    # object_type -> counts-dict key mapping (C5 batched GROUP BY projection).
    # Canonical map lives on NotebookSummaryQuery; the facade keeps the frozen
    # compatibility name pointing at the same object.
    _NOTEBOOK_COUNT_TYPES: Dict[str, str] = NotebookSummaryQuery._NOTEBOOK_COUNT_TYPES

    def _knowledge_type_counts(self, db: sqlite3.Connection, notebook_id: str) -> Dict[str, int]:
        return self._runtime.notebook_summaries.knowledge_type_counts(db, notebook_id)

    def _notebook_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> NotebookSummary:
        return self._runtime.notebook_summaries.from_row(db, row)

    def _source_from_row(self, db: sqlite3.Connection, row: sqlite3.Row) -> SourceSummary:
        return self._runtime.source_store.source_from_row(db, row)

    def _sources_from_rows(self, db: sqlite3.Connection, rows: List[sqlite3.Row]) -> List[SourceSummary]:
        return self._runtime.source_store.sources_from_rows(db, rows)

    def _extraction_warning(self, db: sqlite3.Connection, source_id: str) -> Optional[str]:
        return self._runtime.source_store.extraction_warning(db, source_id)

    def _source_type_from_name(self, file_name: str) -> str:
        lower_name = file_name.lower()
        if lower_name.endswith(".pdf"):
            return "pdf"
        if lower_name.endswith(".md") or lower_name.endswith(".markdown"):
            return "markdown"
        if lower_name.endswith(".docx"):
            return "docx"
        if lower_name.endswith(".pptx"):
            return "pptx"
        return "other"

    def _summarize_source(self, title: str, elements: List[SourceElement]) -> str:
        text = "\n".join(element.text for element in elements[:12])
        if self.llm_client.configured and text.strip():
            try:
                raw = self.llm_client.chat_json(
                    [
                        {
                            "role": "user",
                            "content": (
                                f"Summarize this semiconductor notebook source in one concise sentence.\n"
                                f"Title: {title}\n\n{text[:6000]}"
                            ),
                        }
                    ],
                    '{"summary": "one concise sentence"}',
                )
                parsed = json.loads(raw)
                summary = str(parsed.get("summary", "")).strip()
                if summary:
                    return summary
            except Exception:
                pass
        if not text.strip():
            return "Parsed source contains no extractable text elements."
        first = " ".join(text.split())[:260]
        return f"{len(elements)} parsed text element(s). {first}"

    def _delete_file(self, file_path: str) -> None:
        return self._runtime.source_files.delete(file_path)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


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
