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
from app.services.vector_cache import VectorCache, LRUProcessCache
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


try:
    import orjson as _orjson
    def _fast_loads(s):
        """orjson for speed (5-10x on big float arrays); fall back to stdlib json
        for any value orjson rejects (e.g. NaN/Infinity in legacy vectors)."""
        try:
            return _orjson.loads(s)
        except Exception:
            return json.loads(s)
except ImportError:  # pragma: no cover
    _fast_loads = json.loads


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


def _concept_desc_sig(name: str, quotes: List[str]) -> str:
    """Deterministic signature of the concept-description LLM input. Same
    (name, quote-set) => same sig => cached description reused across rebuilds.
    Quotes are sorted here so the sig is order-insensitive on the set (the
    caller also sorts, so the prompt text stays byte-stable regardless)."""
    import hashlib
    payload = (name or "") + "\x00" + "\x00".join(sorted(quotes))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _pair_key(a: str, b: str) -> str:
    """canonical id 对的稳定归一键:排序后用 \x1f 连接,(a,b)/(b,a) 同键。"""
    x, y = sorted((a, b))
    return f"{x}\x1f{y}"


_DESC_CKPT_FLUSH = 16   # 概念描述每完成多少个 flush 一次 checkpoint(被杀最多丢这么多)


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
        # Task 12: the ingestion orchestration rides facade-bound late seams —
        # the `_write` transaction seat, the parse/summarize/model seams whose
        # frozen patch targets live on this facade or its module namespace
        # (repo.source_elements / repo._summarize_source / module
        # parse_source_file / per-user llm & kg_llm properties), and TEMPORARY
        # facade-owned KG/catalog callbacks that Gate 5 replaces with real
        # services.  Every lambda resolves at call time — post-construction
        # monkeypatches stay observed.
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
            store_kg=lambda notebook_id, source_id, objects, relations: (
                self.store_kg(notebook_id, source_id, objects, relations)
            ),
            incremental_fuse_source=lambda notebook_id, source_id: (
                self.incremental_fuse_source(notebook_id, source_id)
            ),
            maybe_auto_index=lambda notebook_id: self.maybe_auto_index(notebook_id),
            invalidate_unified_cache=lambda notebook_id: (
                self._invalidate_unified_cache(notebook_id)
            ),
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
        from app.services.embedding import make_embedder
        self.embedder = make_embedder(self.settings)
        self.mineru_client = MinerUClient(settings)
        self.mineru_cloud_client = MinerUCloudClient(settings)
        self.event_log = self._runtime.event_log
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._unified_cache: Dict[Any, Any] = {}
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
        self._scale_idx_cache = LRUProcessCache(max_entries=self.settings.scale_idx_cache_max)
        # P1-8: memoize _scale_index_version keyed on kg_mutation_seq. Maps
        # notebook_id -> (last_seq, version_list). When seq is unchanged we skip
        # the 5 COUNT/MAX aggregates and return the cached list (same format —
        # no on-disk manifest.version invalidation).
        self._scale_ver_cache: Dict[str, Any] = {}
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
        self._scale_ver_lock = threading.Lock()
        self._scale_ver_locks: Dict[str, threading.Lock] = {}
        # per-nb 单飞:allow_stale 检索路径 cold-load ScaleIndex 时,防 N 个并发查询
        # 各自 load_scale_index + hnswlib.load_index(8GB)造成 N× 内存尖峰。锁次序同
        # _scale_ver_lock:全局锁只护锁表结构,绝不在全局锁内跑 load。
        self._scale_idx_load_lock = threading.Lock()
        self._scale_idx_load_locks: Dict[str, threading.Lock] = {}
        # C7: same bounded-LRU rework as _scale_idx_cache above.
        self._viz_idx_cache = LRUProcessCache(max_entries=self.settings.scale_idx_cache_max)
        self._vector_cache = VectorCache(max_entries=self.settings.vector_cache_max_entries)
        self._scale_building: set = set()
        self._scale_building_lock = threading.Lock()
        self._scale_idle_queue: dict = {}   # {notebook_id: mode} 待低峰重建
        self._scale_scheduler_started = False
        # 大库自动建索引(maybe_auto_index)的 O(1) once-set:notebook_id 一旦被评估
        # (无论「已入队/建成」还是「判定不需要」)即加入,读路径兜底靠它避免每查询都
        # 算 notebook_copy_stats() 的 5 个 COUNT。_mark_unified_kg_dirty 在每次 KG
        # 写时 discard,使下一轮变更重新触发评估。
        self._auto_index_checked: set = set()
        # KG-view viz index background build (mirrors _scale_building exactly):
        # guarded set of notebook_ids currently being folded by _spawn_viz_build.
        self._viz_building: set = set()
        self._viz_building_lock = threading.Lock()
        # KG build/rebuild 的进行中标志（进程内；重启后天然为空=未构建，无需 reconcile）。
        # get_notebook 回填 NotebookSummary.kg_building，前端刷新后据此把「构建中」接回。
        # 集合本体归 NotebookCatalogService 所有（get_notebook 在那读成员资格）；
        # facade 别名同一个 set 对象，KG build/rebuild 路径照旧 add/discard。
        self._kg_building: set = self._runtime.catalog.kg_building
        self._kg_building_lock = threading.Lock()
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
        """Delete all KG artifacts for a notebook (objects, relations, clusters,
        merge candidates, embeddings, extraction runs, unified state) while KEEPING
        sources and source_elements so it can be re-extracted from already-parsed
        elements. Returns {table: rows_deleted}."""
        self.get_notebook(notebook_id)
        counts: dict = {}
        with self._write() as db:
            for table in ("knowledge_objects", "knowledge_relations", "concept_clusters",
                          "concept_merge_candidates", "knowledge_embeddings",
                          "extraction_runs", "unified_kg_state"):
                cur = db.execute(f"DELETE FROM {table} WHERE notebook_id = ?", (notebook_id,))
                counts[table] = cur.rowcount
            fts_cur = db.execute(
                "DELETE FROM kg_objects_fts WHERE notebook_id = ?", (notebook_id,)
            )
            counts["kg_objects_fts"] = fts_cur.rowcount
        self._invalidate_unified_cache(notebook_id)
        return counts

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
        """按需对该 notebook 下"尚无 KG"的 source 抽取(复用 _run_extraction)。
        幂等:已有 knowledge_objects 的 source 跳过。无 LLM → RuntimeError。
        跨源**并发**(提交到全局 KG job 池;窗口仍由全局 window 池封顶,两池分离防死锁);
        单 source 失败隔离,不连累其余、不回退其终态,错误入 event log。
        progress(i, n, source_id, ok):可选回调,每抽完一源调一次(批量 CLI 显示进度用)。"""
        self.get_notebook(notebook_id)  # KeyError if missing
        with self._kg_building_lock:
            self._kg_building.add(notebook_id)
        try:
            if not getattr(self.llm_client, "configured", False):
                raise RuntimeError("LLM not configured; cannot build KG")
            with self._connect() as db:
                src_ids = [r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id = ?", (notebook_id,)).fetchall()]
                # source_id is NOT NULL DEFAULT '' — use != '' to find sources that truly have KG
                kgful = {r["source_id"] for r in db.execute(
                    "SELECT DISTINCT source_id FROM knowledge_objects "
                    "WHERE notebook_id = ? AND source_id != ''", (notebook_id,)).fetchall()}
            targets = [sid for sid in src_ids if sid not in kgful]
            done, failed = [], []

            def _extract_one(sid: str) -> bool:
                try:
                    self._set_source_status(sid, "extracting")
                    self._run_extraction(sid)
                    self._set_source_status(sid, "extracted")
                    return True
                except Exception:  # noqa: BLE001 — 隔离单 source 失败
                    self.event_log.logger.exception("build_notebook_kg failed for %s", sid)
                    return False

            # 跨源并发:提交到全局 KG job 池(cap=KG_JOB_CONCURRENCY);窗口仍走全局 window
            # 池(cap=KG_EXTRACT_WORKERS)、总量封顶不打爆 LLM,两池分离防死锁。
            import concurrent.futures as _cf
            from app.services.kg import scheduler as _kg_scheduler
            futs = {_kg_scheduler.submit_job(_extract_one, sid): sid for sid in targets}
            for _i, fut in enumerate(_cf.as_completed(futs), 1):
                sid = futs[fut]
                ok = bool(fut.result())   # _extract_one 内部已吞异常,只返回布尔
                (done if ok else failed).append(sid)
                if progress is not None:
                    try:
                        progress(_i, len(targets), sid, ok)
                    except Exception:  # noqa: BLE001 — 进度回调绝不破坏构建
                        pass
            done.sort()
            failed.sort()
            try:
                self._mark_unified_kg_dirty(notebook_id)
            except Exception:
                self.event_log.logger.exception("unified-KG dirty mark failed for %s", notebook_id)
            # Conflict resolution pass — runs after ALL sources are extracted.
            # Fail-safe: any exception is logged but never breaks the build.
            if self.settings.kg_conflict_resolution_enabled:
                try:
                    self.resolve_notebook_conflicts(notebook_id)
                except Exception:  # noqa: BLE001
                    self.event_log.logger.exception(
                        "build_notebook_kg: conflict resolution failed for %s", notebook_id
                    )
            result = {"built": done, "failed": failed, "skipped": sorted(kgful)}
            # Backfill relink — reconnect any degree-0 nodes left in this notebook's
            # KG (legacy graphs, or nodes the inline path couldn't link within their
            # own source). Fail-safe: never breaks the build.
            if getattr(self.settings, "kg_relink_enabled", True):
                try:
                    result["relink"] = self.relink_notebook_kg(notebook_id)
                except Exception:  # noqa: BLE001
                    self.event_log.logger.exception(
                        "build_notebook_kg: relink failed for %s", notebook_id
                    )
            # Content-add settle point: enqueue an idle incremental fold if this
            # notebook already has a scale index, so the newly-extracted sources
            # become semantically searchable (fold only, never a fresh build).
            # Fail-safe: helper never raises.
            self._maybe_enqueue_scale_fold(notebook_id)
            return result
        finally:
            with self._kg_building_lock:
                self._kg_building.discard(notebook_id)

    def rebuild_notebook_kg(self, notebook_id: str) -> dict:
        """Full re-extract: wipe all KG artefacts, then build from scratch.

        Equivalent to delete_notebook_kg + build_notebook_kg but in a single
        call so background threads don't need to chain the two methods.
        After delete every source has no KG, so build re-extracts all of them;
        build's tail relink pass runs automatically.
        Missing-notebook KeyError is raised by delete_notebook_kg's own
        get_notebook guard (unchanged) — no separate pre-check here, so this
        wrapper adds zero new behavior on the invalid-id path."""
        # _kg_building must cover the delete phase too — large notebooks
        # (590k+ objects) can spend >6s in delete_notebook_kg alone, and the
        # frontend polls every 6s. Without this wrapper, a poll landing during
        # delete reads kg_building=False (build_notebook_kg hasn't set it yet)
        # and the UI reports "build complete" while the job is still running.
        # Nesting is safe: set.add is idempotent, so build_notebook_kg's own
        # add is a no-op; its finally-discard fires when build ends (the last
        # step of rebuild), and this outer finally-discard is then a no-op too.
        with self._kg_building_lock:
            self._kg_building.add(notebook_id)
        try:
            self.delete_notebook_kg(notebook_id)
            return self.build_notebook_kg(notebook_id)
        finally:
            with self._kg_building_lock:
                self._kg_building.discard(notebook_id)

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
        """Insert KG nodes/edges (remapping local ids to DB ids), embeds payload.

        分块写入(每块 CHUNK 行, 各自一个 _write() 事务), 避免单源 2.6万行塞一个
        事务长时间持锁。本地 id->DB id 在分块前一次性预分配, 跨块关系仍能正确
        remap。代价: 失整源原子性(崩溃可能留半本); _run_extraction 逐源自清 +
        可重跑兜底。Relations 引用不到的 local id 静默跳过。"""
        CHUNK = 1000
        now = _now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = _new_id("ko")
            obj["_oid"] = local_to_id[obj["local_id"]]   # _embed_objects_batch 依赖
        from app.services.retrieval import relation_embed_text, _payload_text
        local_to_name = {o["local_id"]: _payload_text(o["payload"])[:80] for o in objects}
        db_relations = []
        for rel in relations:
            s = local_to_id.get(rel["source_local_id"])
            t = local_to_id.get(rel["target_local_id"])
            if not s or not t:
                continue
            spans = [e.get("quoted_span", "") for e in rel.get("evidence", [])
                     if isinstance(e, dict)]
            db_relations.append({
                "_rid": _new_id("rel"),
                "source_object_id": s, "target_object_id": t,
                "edge_type": rel["edge_type"], "evidence": rel.get("evidence", []),
                "text": relation_embed_text(
                    local_to_name.get(rel["source_local_id"], "?"), rel["edge_type"],
                    local_to_name.get(rel["target_local_id"], "?"), spans),
            })

        # Base strong-review gate (Track F): objects written directly to a base
        # notebook land as 'reviewed' (still in USABLE_STATUSES, so retrievable)
        # rather than 'approved' — the curator confirms via update_knowledge
        # before they are treated as canonical. Personal notebooks keep
        # 'approved' (no behavior change).
        with self._connect() as db:
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
        auto_status = 'reviewed' if (nb_row and nb_row["tier"] == 'base') else 'approved'

        for i in range(0, len(objects), CHUNK):
            chunk = objects[i:i + CHUNK]
            with self._write() as db:
                self._runtime.knowledge.insert_object_chunk(
                    db,
                    [(o["_oid"], notebook_id, o["object_type"], auto_status,
                      json.dumps(o["payload"], ensure_ascii=False),
                      json.dumps(o["evidence"], ensure_ascii=False),
                      source_id or '', now, now) for o in chunk],
                )
                fts_rows = [
                    (o["_oid"], notebook_id, o["payload"].get("name", ""))
                    for o in chunk
                    if (o["payload"].get("name") or "").strip()
                ]
                if fts_rows:
                    self._runtime.knowledge.insert_kg_fts_rows(db, fts_rows)
                # Forward maintenance (P0-4 reverse index): fresh inserts never had
                # prior rows, so a plain batched INSERT suffices (no DELETE-first).
                kos_rows = [
                    (o["_oid"], sid, notebook_id)
                    for o in chunk
                    for sid in self._source_ids_from_evidence(
                        json.dumps(o["evidence"], ensure_ascii=False)
                    )
                ]
                if kos_rows:
                    self._runtime.knowledge.insert_object_source_rows(db, kos_rows)
        for i in range(0, len(db_relations), CHUNK):
            chunk = db_relations[i:i + CHUNK]
            with self._write() as db:
                self._runtime.knowledge.insert_relation_chunk(
                    db,
                    [(r["_rid"], notebook_id, source_id,
                      r["source_object_id"], r["target_object_id"], r["edge_type"],
                      json.dumps(r["evidence"], ensure_ascii=False), now) for r in chunk],
                )
        self._embed_objects_batch(notebook_id, objects)
        self._embed_relations_batch(notebook_id, db_relations)
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)
        return len(objects), len(db_relations)

    def relink_notebook_kg(self, notebook_id: str) -> dict:
        """Backfill: reconnect degree-0 KG nodes in an EXISTING notebook.

        For legacy / pre-relink graphs (the inline path handles new extractions).
        Loads non-deprecated objects (same node filter as knowledge_graph) with
        their evidence element_ids, asks the deterministic relink core for new
        INTRA-SOURCE edges (nodes carry real source_id ⇒ cross-source never
        linked), and inserts them into knowledge_relations EXACTLY like store_kg
        does (review_status defaults to 'pending', the same value LLM edges get).
        source_id of each new row = the SOURCE object's source_id. Idempotent:
        an edge is skipped if a row with the same
        (source_object_id, target_object_id, edge_type) already exists.

        Returns {"isolated_before", "edges_added", "isolated_after"}."""
        from app.services.kg.relink import complete_isolated_edges

        self.get_notebook(notebook_id)  # KeyError if missing
        with self._connect() as db:
            obj_rows = db.execute(
                "SELECT id, object_type, source_id, payload, evidence "
                "FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated'",
                (notebook_id,),
            ).fetchall()
            rel_rows = db.execute(
                "SELECT source_object_id, target_object_id, edge_type "
                "FROM knowledge_relations WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchall()
            # Valid source ids — knowledge_relations.source_id has an FK to
            # sources(id); a legacy/orphaned object source_id is stored as NULL
            # (the column is nullable) to avoid a FOREIGN KEY violation.
            valid_src = {
                r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id = ?", (notebook_id,)
                ).fetchall()
            }

        nodes, src_by_id = [], {}
        for r in obj_rows:
            payload = json.loads(r["payload"] or "{}")
            evidence = json.loads(r["evidence"] or "[]")
            element_ids = {
                ev.get("element_id")
                for ev in evidence
                if isinstance(ev, dict) and ev.get("element_id")
            }
            src_by_id[r["id"]] = r["source_id"] or ""
            nodes.append({
                "id": r["id"],
                "object_type": r["object_type"],
                "name": payload.get("name", ""),
                "source_id": r["source_id"] or "",
                "element_ids": element_ids,
            })

        edges = [(r["source_object_id"], r["target_object_id"]) for r in rel_rows]
        connected = {oid for pair in edges for oid in pair}
        isolated_before = sum(1 for n in nodes if n["id"] not in connected)

        proposed = complete_isolated_edges(nodes, edges)

        # Idempotency: skip any proposed edge already present (same triple).
        existing_triples = {
            (r["source_object_id"], r["target_object_id"], r["edge_type"])
            for r in rel_rows
        }
        now = _now()
        new_rows = []
        for e in proposed:
            triple = (e["source_object_id"], e["target_object_id"], e["edge_type"])
            if triple in existing_triples:
                continue
            existing_triples.add(triple)
            src = src_by_id.get(e["source_object_id"], "")
            new_rows.append((
                _new_id("rel"), notebook_id,
                src if src in valid_src else None,   # NULL if source gone (FK-safe)
                e["source_object_id"], e["target_object_id"], e["edge_type"],
                json.dumps([{"basis": e["basis"], "quote": ""}], ensure_ascii=False),
                now,
            ))

        if new_rows:
            with self._write() as db:
                self._runtime.knowledge.insert_relation_chunk(db, new_rows)
            # Match store_kg / delete_notebook_kg so the in-memory rustworkx graph
            # (and PPR/federated caches) pick up the new edges.
            self._invalidate_unified_cache(notebook_id)
            self._mark_unified_kg_dirty(notebook_id)

        now_connected = connected | {
            oid for r in new_rows for oid in (r[3], r[4])
        }
        isolated_after = sum(1 for n in nodes if n["id"] not in now_connected)
        return {
            "isolated_before": isolated_before,
            "edges_added": len(new_rows),
            "isolated_after": isolated_after,
        }

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
        """Return edges ranked by review priority = edge_centrality * (1 - trust_score).

        Only edges with review_status != 'rejected' are included (rejected edges are
        excluded from reasoning and need no further review).
        Centrality is computed over the FULL graph (including non-rejected edges),
        version-cached via _edge_centrality_map — see that method's docstring for
        the degree-top-K bounding behavior above edge_centrality_max_nodes.
        trust_score combines evidence anchoring + cross-doc corroboration + type validity.
        """
        import json as _json
        from app.services.kg.edge_trust import (
            compute_trust_score, corroboration_counts,
            corroboration_score_from_count,
        )

        self.get_notebook(notebook_id)
        with self._connect() as db:
            rel_rows = db.execute(
                "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
                "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
                "ko_s.object_type AS src_type, ko_t.object_type AS tgt_type "
                "FROM knowledge_relations kr "
                "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
                "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
                "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            # Build node types + names for trust signals
            obj_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()

        node_types: dict = {}
        node_names: dict = {}
        for r in obj_rows:
            node_types[r["id"]] = r["object_type"]
            p = _json.loads(r["payload"] or "{}")
            node_names[r["id"]] = p.get("name", "")

        rels = []
        for r in rel_rows:
            rels.append({
                "id": r["id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"],
                "edge_type": r["edge_type"],
                "evidence": _json.loads(r["evidence"] or "[]"),
                "source_id": r["source_id"],
                "review_status": r["review_status"],
                "_src_type": r["src_type"] or "",
                "_tgt_type": r["tgt_type"] or "",
                "_src_name": node_names.get(r["source_object_id"], ""),
                "_tgt_name": node_names.get(r["target_object_id"], ""),
            })

        # Corroboration counts (batched over all edges)
        corr_counts = corroboration_counts(rels, node_names)

        # Edge centrality — version-cached, see _edge_centrality_map docstring.
        edge_centrality = self._edge_centrality_map(notebook_id)

        items = []
        for rel in rels:
            rid = rel["id"]
            corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
            trust = compute_trust_score(rel, node_types, corr_score)
            ec = edge_centrality.get(rid, 0.0)
            # review_priority = high centrality × low trust
            priority = ec * (1.0 - trust)
            items.append({
                "rel_id": rid,
                "notebook_id": notebook_id,
                "edge_type": rel["edge_type"],
                "source_object_id": rel["source_object_id"],
                "target_object_id": rel["target_object_id"],
                "source_name": rel["_src_name"],
                "target_name": rel["_tgt_name"],
                "source_type": rel["_src_type"],
                "target_type": rel["_tgt_type"],
                "trust_score": trust,
                "edge_centrality": ec,
                "review_priority": priority,
                "review_status": rel["review_status"],
            })

        items.sort(key=lambda x: x["review_priority"], reverse=True)
        return items[:limit]

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation.

        Allowed statuses: 'pending', 'verified', 'rejected'.
        Raises ValueError for unknown statuses.
        Raises KeyError if the relation does not exist in this notebook.
        Invalidates the federated reasoning graph cache so the next
        graph-reasoning call sees the updated set of active edges.
        """
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"review_status must be one of {sorted(self._REVIEW_STATUSES)}, got {status!r}")
        with self._write() as db:
            self._runtime.governance.update_edge_review(db, notebook_id, rel_id, status)
        # review_status flips in place (relation COUNT unchanged) — bump the
        # monotonic seq so seq-keyed fast paths (_scale_index_version /
        # _cluster_input_version) don't serve a stale version for this edit.
        self._mark_unified_kg_dirty(notebook_id)
        # Invalidate cached graph so _federated_rx_graph rebuilds on next access
        # (belt-and-braces: its per-status-count version key would also catch
        # the flip on its own).
        self._invalidate_unified_cache(notebook_id)

    def _delete_relations_for_source(self, db, source_id: str) -> None:
        self._runtime.knowledge.delete_relations_for_source(db, source_id)

    # --- Concept-cluster / merge-candidate CRUD (Task 5) -------------------

    def write_clusters(self, notebook_id: str, rows: List[dict],
                       object_type: str = "concept") -> None:
        now = _now()
        with self._write() as db:
            self._runtime.governance.delete_clusters(db, notebook_id, object_type)
            self._runtime.governance.insert_clusters(
                db, notebook_id, object_type, rows, now
            )
            # P0-A: bump the cluster-write change signal in the SAME commit as the
            # DELETE+INSERT above — _scale_index_version's memo relies on this
            # being atomic with the content change (no window where clusters moved
            # but cseq didn't).
            self._bump_cluster_mutation_seq(db, notebook_id)
        # P1-2: this DELETE+INSERT can land on the SAME second as a prior write to
        # this notebook (COUNT/MAX(created_at) unchanged for that (notebook_id,
        # object_type) slice, or even notebook-wide if this is the only cluster
        # write) — a version-keyed cache would then serve a stale cluster_map.
        # Explicit invalidation, not the version tuple, is what's load-bearing here.
        self._invalidate_unified_cache(notebook_id)

    def append_clusters(self, notebook_id: str, rows: list, object_type: str = "concept") -> int:
        """追加写 concept_clusters(不 DELETE);member_object_id 幂等(已在则跳过)。返回新增数。"""
        now = _now()
        with self._write() as db:
            added = self._runtime.governance.insert_clusters(
                db, notebook_id, object_type, rows, now
            )
            # P0-A: only bump when a row actually landed (a no-op call — every
            # member already present — must not manufacture a fake change signal).
            if added:
                self._bump_cluster_mutation_seq(db, notebook_id)
        # P1-2: self-invalidate rather than rely on every caller remembering to.
        # incremental_fuse_source (the only production caller) already invalidates
        # at its own end too — invalidation is idempotent, so this is pure defense
        # against a future caller that forgets, and against the same-second
        # INSERT-only-COUNT-moves-not-MAX hazard (append adds a row with `now`,
        # which CAN still tie MAX(created_at) to an existing row's timestamp when
        # called twice within the same second, e.g. via incremental_fuse_source's
        # own two append_clusters calls).
        if added:
            self._invalidate_unified_cache(notebook_id)
        return added

    def incremental_fuse_source(self, notebook_id: str, source_id: str) -> None:
        """上传后增量融合该源 concept 进 concept_clusters。Tier1 名种子 append(无 LLM)。"""
        if not self.settings.kg_incremental_fusion_enabled:
            return
        # 清理 re-extraction 留下的 orphan 簇行(member 指向已删 knowledge_objects):重抽取删旧
        # ko- 以新 id 重建,旧簇成员行悬空。消费方(build_ppr_graph/unified_graph)虽已过滤,
        # 仍清以防表无界增长 + unified_kg_status 的 canonical 计数虚高。id 是 PK,子查询走索引。
        with self._write() as db:
            cur = db.execute(
                "DELETE FROM concept_clusters WHERE notebook_id=? AND member_object_id NOT IN "
                "(SELECT id FROM knowledge_objects WHERE notebook_id=?)",
                (notebook_id, notebook_id))
            # P0-A: only bump if this orphan-sweep actually deleted rows — the
            # later append_clusters calls in this method self-bump on their own
            # additions, so this guards just the DELETE branch (no double-count,
            # no fake signal on a no-op sweep).
            if cur.rowcount > 0:
                self._bump_cluster_mutation_seq(db, notebook_id)
        from app.services.kg_merge import place_new_concepts, _norm
        # P1-2: one shared cluster_map load for the whole fuse call (Tier1/Tier2
        # concept pass below + the non-concept claim/formula/procedure pass at the
        # end). cluster_map is cache-hit-cheap after the first call within this
        # method (concept_clusters isn't invalidated until _invalidate_unified_cache
        # at the very end), so this is defensive rather than a correctness
        # requirement — but it also means we never pay the cache-lookup+version-probe
        # overhead twice. Reusing the pre-Tier1-append snapshot for the non-concept
        # pass is safe: canonical ids are namespaced by type-specific prefixes
        # (K-/KL-/KF-/KP-), so place_new_concepts' collision check for claims/
        # formulas/procedures never depends on concepts Tier1 just appended.
        cmap = self.cluster_map(notebook_id)
        with self._connect() as db:
            new = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                "AND object_type='concept' AND status!='deprecated'",
                (notebook_id, source_id)).fetchall()
            cn = db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                "WHERE notebook_id=? AND object_type='concept'", (notebook_id,)).fetchall()
        new_objs = [{"object_id": r["id"],
                     "name": json.loads(r["payload"] or "{}").get("name", "")} for r in new]
        if new_objs:
            canon_names = {r["canonical_id"]: r["canonical_name"] for r in cn}
            rows = place_new_concepts(new_objs, cmap, canon_names,
                                      seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
            self.append_clusters(notebook_id, rows, object_type="concept")
            with self._connect() as db:
                ex = db.execute(
                    "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
                    "AND object_type='concept' AND status!='deprecated' AND source_id!=?",
                    (notebook_id, source_id)).fetchall()
            # Tier2 桥接候选来源三分支(P1-3,perf audit):
            #   1) 有可用 kg ANN(即使版本漂移/stale,advisory 桥接可接受)→ ANN 近邻查询,
            #      任意规模可用,恢复大库(> max_entities)上一直被静默跳过的跨文档桥接。
            #      stale 索引只缺"新↔新"对象自身(下轮重建后补),"新↔存量"这一主场景
            #      不受影响(见 _tier2_bridge_candidates_ann 文档)。
            #   2) 无索引且已有 concept 数 ≤ max_entities → 原暴力 O(new×existing) 余弦(不动)。
            #   3) 无索引且已有 concept 数 > max_entities → 跳过,但显式发 tier2_skipped 事件
            #      (P1-3 修复点:旧代码这里静默跳过,大库上 Tier2 从未真正跑过)。
            idx = self._scale_index(notebook_id, allow_stale=True)
            ann = self._open_scale_ann(idx, "kg") if (idx is not None and idx.ann_labels) else None
            cands: list = []
            if ann is not None:
                cands = self._tier2_bridge_candidates_ann(
                    notebook_id, idx, ann, new_objs, cmap)
            elif len(ex) <= self.settings.kg_incremental_tier2_max_entities:
                with self._connect() as db:
                    vrows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                                       (notebook_id,)).fetchall()
                    pend = db.execute(
                        "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                        "WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchall()
                # 按 settings.embed_dim 过滤(同 rebuild_unified_kg:丢弃旧 embedder 的异维向量)。
                # 运行时截断旁路(计划 §1.2 旁路 2):两步分离、顺序不可反 ——
                # ①先按存储原生维过滤(既有语义,异维残留出局);②通过后 truncate_vec
                # 截断到运行时空间(聚类/融合空间拍板统一 1024,与 ANN 分支同空间,
                # 否则同功能两分支 lo=0.82 语义分裂)。new_vecs 派生自 vecs,一并覆盖。
                from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                dim = self.settings.embed_dim
                rd = resolve_runtime_dim(self.settings)
                vecs = {}
                for r in vrows:
                    arr = decode_vector(r["vector"])
                    if arr is not None and arr.size == dim:
                        if rd:
                            arr = truncate_vec(arr, rd)
                        vecs[r["object_id"]] = arr.tolist()
                existing_items = [{"object_id": r["id"],
                                   "name": json.loads(r["payload"] or "{}").get("name", "")} for r in ex]
                new_vecs = {o["object_id"]: vecs[o["object_id"]] for o in new_objs if o["object_id"] in vecs}
                # 排除全部已决(confirmed+rejected+deferred)+ 已 pending,避免重复入队。
                # 直接查(含 deferred),而非 decided_pairs()(仅 confirmed/rejected)——否则
                # deferred 概念对会在新源桥接时被重新入队,违背「deferred 不回流」。
                with self._connect() as _db:
                    _decided = _db.execute(
                        "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                        "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred')",
                        (notebook_id,)).fetchall()
                exclude = {frozenset((r["canonical_a"], r["canonical_b"])) for r in _decided}
                exclude |= {frozenset((r["canonical_a"], r["canonical_b"])) for r in pend}
                from app.services.kg_merge import detect_bridge_candidates
                cands = detect_bridge_candidates(new_objs, new_vecs, existing_items, vecs, cmap, exclude)
            else:
                self.event_log.emit({
                    "kind": "tier2_skipped", "notebook_id": notebook_id,
                    "entities": len(ex), "reason": "no_index_over_threshold",
                })
            if cands:
                now = _now()
                with self._write() as db:
                    for c in cands:
                        self._runtime.governance.insert_merge_candidate(
                            db, notebook_id, c["canonical_a"], c["canonical_b"],
                            c["score"], now, id_prefix="cm")
        from app.services.kg_merge import seed_claim, seed_formula, seed_procedure
        _TYPES = {"claim": (seed_claim, "KL-"), "formula": (seed_formula, "KF-"),
                  "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPES.items():
            with self._connect() as db:
                trows = db.execute(
                    "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                    "AND object_type=? AND status!='deprecated'", (notebook_id, source_id, t)).fetchall()
                tcn = db.execute("SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                                 "WHERE notebook_id=? AND object_type=?", (notebook_id, t)).fetchall()
            tnew = [{"object_id": r["id"], "payload": json.loads(r["payload"] or "{}"),
                     "name": json.loads(r["payload"] or "{}").get("name", "")} for r in trows]
            if not tnew:
                continue
            tcanon = {r["canonical_id"]: r["canonical_name"] for r in tcn}
            trows_w = place_new_concepts(tnew, cmap, tcanon,
                                         seed_fn=lambda o, _s=sfn: _s(o), id_prefix=prefix)
            self.append_clusters(notebook_id, trows_w, object_type=t)
        self._invalidate_unified_cache(notebook_id)

    def _tier2_bridge_candidates_ann(self, notebook_id: str, idx, ann, new_objs: list,
                                     cluster_map_: Dict[str, str]) -> list:
        """ANN-backed Tier2 bridge candidate detection (P1-3, perf audit).

        Same candidate-set semantics as `kg_merge.detect_bridge_candidates`
        (lo=0.82 similarity floor, top_k=5 per new object, exclude same-canonical
        hits and already-decided/pending pairs) but sourced from the notebook's
        persisted kg hnsw ANN instead of a brute-force cosine over every existing
        concept embedding — the only way Tier2 stays usable once a library's
        concept count exceeds `kg_incremental_tier2_max_entities` (production:
        490k+ entities, where the brute-force path silently no-ops today).

        `idx` may be STALE (`_scale_index(nb, allow_stale=True)` returned a disk
        index whose version predates this source's own new objects/embeddings).
        This is acceptable: bridging is advisory (candidates only ever land in
        concept_merge_candidates as 'pending', reviewed by a human — never
        auto-merged), and a stale ANN is only missing objects newer than its
        build watermark. The query side (this source's newly-fused concepts) is
        always fresh — it's read straight from knowledge_embeddings, not from
        the index. So "new↔existing" bridges (the main incremental-upload
        scenario: a freshly uploaded concept syncing against the library's prior
        knowledge) work correctly even on a stale index; only "new↔new" bridges
        between two concepts uploaded in the same still-unindexed window are
        deferred to the next scale-index rebuild. That gap already exists today
        for anything beyond a single upload cycle (the index only refreshes on
        rebuild), so this doesn't regress the status quo.

        Thread-safety: `_open_scale_ann`'s hnswlib handle is memoized on the
        ScaleIndex instance (PR#147) and hnswlib's `knn_query` is safe for
        concurrent read-only queries against one index handle, so calling this
        from extraction job worker threads (where incremental_fuse_source runs)
        needs no extra locking.
        """
        import numpy as np
        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        topk = 5     # mirrors detect_bridge_candidates' top_k default
        lo = 0.82    # mirrors detect_bridge_candidates' lo default
        dim = self.settings.embed_dim
        # 运行时截断旁路(计划 §1.2 旁路 3):查询侧向量读自 knowledge_embeddings
        # (存储原生维),须截断到运行时空间才能进(切换后同为运行时维的)持久 kg ANN。
        rd = resolve_runtime_dim(self.settings)
        eff_dim = rd or dim   # 运行时生效维:守卫比它而非 embed_dim(否则截断后恒 [] 静默跳过)

        with self._connect() as db:
            new_rows = db.execute(
                "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=? "
                "AND object_id IN ({})".format(",".join("?" for _ in new_objs)),
                (notebook_id, *[o["object_id"] for o in new_objs])
            ).fetchall() if new_objs else []
            _decided = db.execute(
                "SELECT canonical_a, canonical_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status IN ('confirmed','rejected','deferred','pending')",
                (notebook_id,)).fetchall()
        exclude = {frozenset((r["canonical_a"], r["canonical_b"])) for r in _decided}
        new_vecs = {}
        for r in new_rows:
            arr = decode_vector(r["vector"])
            if arr is not None and arr.size == dim:   # ①先按存储维过滤(既有语义)
                if rd:
                    arr = truncate_vec(arr, rd)       # ②通过后截断(运行时空间)
                new_vecs[r["object_id"]] = arr

        idx_dim = int(idx.manifest.get("dim", eff_dim))
        if idx_dim != eff_dim or not new_vecs:
            return []

        name_by_obj = {o["object_id"]: o.get("name", "") for o in new_objs}
        out: list = []
        seen: set = set()
        n_labels = len(idx.ann_labels)
        # Legacy semantics (kg_merge.detect_bridge_candidates): rank the top_k
        # NEAREST existing concepts by raw similarity, THEN apply the lo floor /
        # same-canonical / decided-pair filters — filtering never backfills more
        # candidates to make up for a dropped one. The kg ANN index spans EVERY
        # object type (production ratio: ~310k claims vs ~70k concepts of 470k
        # vectors), so a FIXED over-fetch window can come back mostly claims —
        # squeezing concepts out entirely and systematically under-bridging.
        # Type-aware iterative over-fetch instead: start at k = top_k *
        # pad_factor; while fewer than top_k concept hits survive the filters
        # AND the raw neighbor tail is still >= the lo threshold (anything past
        # a below-lo tail is even further away and would be threshold-filtered
        # regardless — expanding cannot help) AND k < min(n_labels, hard cap),
        # double k and re-query (hnsw knn_query is cheap; a fresh query is
        # simpler and safer than incremental cursors).
        pad_factor = max(1, int(self.settings.kg_tier2_ann_pad_factor))
        hard_cap = min(n_labels, 4096)
        # Deprecated alignment: the legacy path's existing_items query filters
        # status!='deprecated'; ann_labels carries no status, so raw hits are
        # batch-validated per knn round (one small IN query, cached across new
        # objects) and deprecated/vanished objects are skipped BEFORE they can
        # consume an eligible slot — matching the legacy pool, where deprecated
        # rows never entered the ranking at all.
        status_alive: Dict[str, bool] = {}

        def _check_alive(ids: list) -> None:
            unknown = [i for i in ids if i not in status_alive]
            if not unknown:
                return
            with self._connect() as db:
                ph = ",".join("?" for _ in unknown)
                rows = db.execute(
                    f"SELECT id FROM knowledge_objects WHERE id IN ({ph}) AND status!='deprecated'",
                    unknown).fetchall()
            alive = {r["id"] for r in rows}
            for i in unknown:
                status_alive[i] = i in alive

        for oid, qvec in new_vecs.items():
            from app.services.kg_merge import _norm, seed_or_unique
            # 与 kg_merge.detect_bridge_candidates 一致的空 seed 守卫:符号-only 名
            # (_norm→"")绝不塌缩成裸 "K-"——否则该退化 canonical 会被写进
            # concept_merge_candidates,与真实簇(K-~oid)错位且互相污染。
            my_cid = "K-" + seed_or_unique(_norm(name_by_obj.get(oid, "")), oid)
            q = np.asarray(qvec, dtype=np.float32)
            k = min(max(topk * pad_factor, topk + 1), n_labels)
            eligible: list = []  # [(node_id, canonical_id, sim)] — alive concepts, not self
            query_failed = False
            while True:
                try:
                    ann.set_ef(max(k + 1, 50))
                    labels, distances = ann.knn_query(q, k=k)
                except Exception as exc:  # noqa: BLE001 — fail-open, mirrors other ANN call sites
                    self._note_model_error("tier2_bridge_ann_query", self.settings.embed_model, exc)
                    query_failed = True
                    break
                hits = sorted(zip(labels[0], distances[0]), key=lambda ld: ld[1])
                raw_ids = [idx.ann_labels[int(lab)] for lab, _ in hits]
                _check_alive([nid for nid in raw_ids if not nid.startswith("cluster:")])
                eligible = []
                for nid, (_lab, dist) in zip(raw_ids, hits):
                    if len(eligible) >= topk:
                        break
                    # Defensive invariant guard: cluster hub nodes have no
                    # knowledge_embeddings row, so by construction they never
                    # appear in ann_labels — this filter only protects against
                    # a future index-format change; it is not load-bearing.
                    if nid.startswith("cluster:") or nid == oid:
                        continue
                    if not status_alive.get(nid, False):
                        continue  # deprecated or vanished object (legacy pool parity)
                    # Type filter (mirrors legacy path, which only ever loads
                    # object_type='concept' rows into existing_items): concept
                    # canonical ids are always "K-"-prefixed (never "KL-"/"KF-"/
                    # "KP-" — claim/formula/procedure), so this string check is
                    # exact and needs no extra DB lookup per hit.
                    other_cid = cluster_map_.get(nid)
                    if not other_cid or not other_cid.startswith("K-"):
                        continue
                    eligible.append((nid, other_cid, max(0.0, 1.0 - float(dist))))
                if len(eligible) >= topk:
                    break
                if k >= hard_cap:
                    break
                tail_sim = max(0.0, 1.0 - float(hits[-1][1])) if hits else 0.0
                if tail_sim < lo:
                    break  # everything beyond the tail is below threshold anyway
                k = min(k * 2, hard_cap)
            if query_failed:
                continue
            for node_id, other_cid, sim in eligible:
                if sim < lo:
                    break  # eligible is distance-sorted ascending -> sim descending
                if other_cid == my_cid:
                    continue
                a, b = sorted((my_cid, other_cid))
                if frozenset((a, b)) in exclude or (a, b) in seen:
                    continue
                seen.add((a, b))
                out.append({"canonical_a": a, "canonical_b": b, "score": sim})
        return out

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
        now = _now()
        with self._write() as db:
            self._runtime.governance.write_merge_candidate(
                db, notebook_id, a, b, score, now
            )

    def pending_merges(self, notebook_id: str) -> List[dict]:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self._runtime.governance.pending_merges(db, notebook_id)

    def _pending_merges_batch(self, notebook_id: str, limit: int) -> List[dict]:
        """Bounded fetch of pending merge candidates, LIMITed in SQL instead of
        materializing the whole pending set and Python-slicing it (perf-audit
        P1-1)."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self._runtime.governance.pending_merges_batch(
                db, notebook_id, limit
            )

    def _has_pending_merges(self, notebook_id: str) -> bool:
        """Cheap continuation test for the merge-review drain loop — EXISTS
        instead of materializing all pending rows just to check non-emptiness
        (perf-audit P1-1)."""
        with self._connect() as db:
            return self._runtime.governance.has_pending_merges(db, notebook_id)

    def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
        if status not in ("confirmed", "rejected"):
            raise ValueError(f"invalid merge status: {status!r}")
        with self._write() as db:
            self._runtime.governance.set_merge_decision(
                db, notebook_id, candidate_id, status, _now()
            )

    def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "confirmed")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
        self.get_notebook(notebook_id)
        self.set_merge_decision(notebook_id, candidate_id, "rejected")
        self._invalidate_unified_cache(notebook_id)
        self._mark_unified_kg_dirty(notebook_id)

    # ------------------------------------------------------------------
    # kg_conflict_candidates — storage primitives (T1)
    # Mirrors the concept_merge_candidates pattern above.
    # Detection lives in conflict_detect.py (T2); adjudication in
    # conflict_review.py (T3); write-back in apply_conflict_resolution (T4);
    # orchestration in resolve_notebook_conflicts (T5).
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
        """Insert one conflict candidate into the queue and return its id.

        resolution, winner_ref, resolved_payload, confidence, and rationale
        are normally NULL at detection time and only populated after
        adjudication (set_conflict_status in T1, write-back in apply_conflict_resolution T4).
        """
        now = _now()
        with self._write() as db:
            return self._runtime.governance.write_conflict_candidate(
                db, notebook_id, kind, left_ref, right_ref,
                conflict_type, resolution, winner_ref, resolved_payload,
                confidence, rationale, now,
            )

    def pending_conflicts(self, notebook_id: str) -> List[dict]:
        """Return all conflict candidates with status='pending' for a notebook."""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            return self._runtime.governance.pending_conflicts(db, notebook_id)

    def set_conflict_status(self, notebook_id: str, candidate_id: str, status: str) -> None:
        """Update status to 'applied' or 'rejected' (+ updated_at).

        Both identifiers are required even though candidate ids are UUID-like:
        authorization is notebook-scoped, so object lookup must use the same
        scope rather than trusting a caller-controlled URL notebook id.
        """
        if status not in ("applied", "rejected"):
            raise ValueError(f"invalid conflict status: {status!r}")
        with self._write() as db:
            self._runtime.governance.set_conflict_status(
                db, notebook_id, candidate_id, status, _now()
            )

    def get_conflict_candidate(self, notebook_id: str, candidate_id: str) -> Optional[dict]:
        """Fetch one conflict candidate inside its notebook authorization scope."""
        return self._runtime.governance.get_conflict_candidate(
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
        """Execute ONE adjudicated conflict resolution against the KG.

        Mechanics only — policy (which side wins) is decided by the caller and
        passed in via ``winner_ref``.  This method just executes the decided
        outcome and keeps caches consistent.

        Parameters
        ----------
        notebook_id:
            The notebook that owns the conflicting objects / relations.
        kind:
            ``"edge"`` (refs are relation ids) or ``"node"`` (refs are
            knowledge_object ids).
        left_ref / right_ref:
            The two competing entity ids.
        resolution:
            ``"keep"`` | ``"discard"`` | ``"modify"``.
        winner_ref:
            For ``"discard"``: the ref that survives; the other is the loser.
            For ``"modify"``/``"node"``: the target object to update (falls back
            to ``left_ref`` when None or not in {left_ref, right_ref}).
            Ignored for ``"keep"``.
        resolved_payload:
            For ``"modify"``/``"node"``: the new payload dict to write.
        """
        if kind not in ("edge", "node"):
            raise ValueError(f"kind must be 'edge' or 'node', got {kind!r}")
        if resolution not in ("keep", "discard", "modify"):
            raise ValueError(
                f"resolution must be 'keep', 'discard', or 'modify', got {resolution!r}")

        # ── keep ────────────────────────────────────────────────────────────
        if resolution == "keep":
            return {"action": "keep"}

        # ── discard ─────────────────────────────────────────────────────────
        if resolution == "discard":
            if winner_ref not in (left_ref, right_ref):
                self.event_log.logger.warning(
                    "apply_conflict_resolution: discard skipped — winner_ref %r is not one of "
                    "(%r, %r) in notebook %s",
                    winner_ref, left_ref, right_ref, notebook_id,
                )
                return {"action": "skipped", "reason": "no valid winner_ref for discard"}
            loser_ref = right_ref if winner_ref == left_ref else left_ref
            if kind == "edge":
                self.set_edge_review(notebook_id, loser_ref, "rejected")
                # set_edge_review already marks dirty + invalidates cache; this
                # extra mark is a harmless belt-and-suspenders (seq is monotonic).
                self._mark_unified_kg_dirty(notebook_id)
            else:  # kind == "node"
                self.update_knowledge(
                    notebook_id, loser_ref, KnowledgeUpdate(status="conflict")
                )
                # update_knowledge already calls _invalidate_unified_cache; mark dirty too.
                self._mark_unified_kg_dirty(notebook_id)
            return {"action": "discard", "loser": loser_ref}

        # ── modify ──────────────────────────────────────────────────────────
        # resolution == "modify"
        if kind == "edge":
            self.event_log.logger.warning(
                "apply_conflict_resolution: edge modify is unsupported in v1 "
                "(notebook %s, left=%r, right=%r) — no-op",
                notebook_id, left_ref, right_ref,
            )
            return {"action": "skipped", "reason": "edge modify unsupported in v1"}

        # kind == "node"
        if not isinstance(resolved_payload, dict):
            self.event_log.logger.warning(
                "apply_conflict_resolution: modify skipped — resolved_payload is not a dict "
                "(got %r) in notebook %s",
                type(resolved_payload).__name__, notebook_id,
            )
            return {"action": "skipped", "reason": "modify without payload"}

        target = winner_ref if winner_ref in (left_ref, right_ref) else left_ref
        # Fetch the current payload so we can merge rather than replace.
        # update_knowledge replaces the entire payload column, so we must
        # preserve fields (section_path, validity_scope, steps, …) not
        # included in the adjudicator's resolved_payload.
        _row = self._runtime.knowledge.get_object_row(notebook_id, target)
        existing_payload: dict = json.loads(_row["payload"] or "{}") if _row else {}
        merged_payload = {**existing_payload, **resolved_payload}
        self.update_knowledge(
            notebook_id, target, KnowledgeUpdate(payload=merged_payload)
        )
        # update_knowledge already calls _invalidate_unified_cache; mark dirty too.
        self._mark_unified_kg_dirty(notebook_id)
        return {"action": "modify", "target": target}

    def confirm_conflict(self, notebook_id: str, candidate_id: str) -> dict:
        """Apply a pending conflict candidate and mark it as 'applied'.

        Composes existing T1/T4 primitives — no new detection or adjudication
        logic.  Raises KeyError if the candidate does not exist; raises
        ValueError if it is already decided (not 'pending').
        """
        row = self.get_conflict_candidate(notebook_id, candidate_id)
        if row is None:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"conflict candidate {candidate_id!r} is already decided "
                f"(status={row['status']!r})"
            )
        if not row.get("resolution"):
            # Detected but not yet adjudicated — nothing to apply. Clearer than
            # letting apply_conflict_resolution raise a generic ValueError.
            raise ValueError(
                f"conflict candidate {candidate_id!r} has no resolution "
                f"(not yet adjudicated)"
            )
        resolved_payload: Optional[dict] = None
        if row.get("resolved_payload") is not None:
            try:
                resolved_payload = json.loads(row["resolved_payload"])
            except (TypeError, ValueError):
                resolved_payload = None

        apply_result = self.apply_conflict_resolution(
            notebook_id,
            kind=row["kind"],
            left_ref=row["left_ref"],
            right_ref=row["right_ref"],
            resolution=row["resolution"],
            winner_ref=row["winner_ref"],
            resolved_payload=resolved_payload,
        )
        self.set_conflict_status(notebook_id, candidate_id, "applied")
        return {**apply_result, "status": "applied", "candidate_id": candidate_id}

    def reject_conflict(self, notebook_id: str, candidate_id: str) -> None:
        """Reject a pending conflict candidate (no KG mutation).

        Raises KeyError if the candidate does not exist; raises ValueError if
        it is already decided.
        """
        row = self.get_conflict_candidate(notebook_id, candidate_id)
        if row is None:
            raise KeyError(f"conflict candidate {candidate_id!r} not found")
        if row["status"] != "pending":
            raise ValueError(
                f"conflict candidate {candidate_id!r} is already decided "
                f"(status={row['status']!r})"
            )
        self.set_conflict_status(notebook_id, candidate_id, "rejected")

    # ------------------------------------------------------------------
    # resolve_notebook_conflicts — Task T5: orchestration
    # Ties detection (T2) → adjudication (T3) → write-back (T4) and
    # records everything in the queue (T1).
    # ------------------------------------------------------------------

    def resolve_notebook_conflicts(self, notebook_id: str) -> dict:
        """Detect, adjudicate, and (optionally) auto-apply KG conflicts for a notebook.

        Steps
        -----
        1. Guard: if LLM is not configured, return a summary noting skipped.
        2. Load objects + relations; build lookup dicts.
        3. Build an {object_id: vector} embeddings dict for the semantic strategy
           (reads knowledge_embeddings; passes None if unavailable).
        4. Run detect_conflict_candidates.
        5. Materialise T3 input items (text / source_text / object_type / tier).
        6. Call review_conflict_candidates (LLM adjudicator).
        7. For each verdict: record in queue; auto-apply when
           conflict_type != "none" AND resolution != "keep" AND
           confidence >= kg_conflict_auto_apply_threshold.
        8. Return summary dict.

        Cross-tier base-wins (base-notebook overrides personal-notebook claims)
        is FUTURE WORK — it belongs when cross-notebook / federated candidate
        recall is added.  In v1, all sides share one tier within the notebook
        so the LLM's winner_ref is trusted directly.
        """
        # 1. Guard — no LLM, skip gracefully
        if not getattr(self.llm_client, "configured", False):
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "skipped_llm": True,
            }

        # 2. Load objects + relations
        with self._connect() as db:
            # Fetch all non-deprecated objects for this notebook
            obj_rows = db.execute(
                "SELECT id, object_type, payload, evidence, status "
                "FROM knowledge_objects "
                "WHERE notebook_id=? AND status != 'deprecated'",
                (notebook_id,),
            ).fetchall()

            # Fetch embeddings for the semantic strategy
            vec_rows = db.execute(
                "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()

            # Fetch the notebook tier (same for all objects in v1)
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()

        # Build objects list in detect_conflict_candidates format
        objects = []
        object_map: dict = {}  # object_id → row dict
        for row in obj_rows:
            payload = json.loads(row["payload"] or "{}")
            obj = {
                "id": row["id"],
                "object_type": row["object_type"],
                "payload": payload,
                "evidence": json.loads(row["evidence"] or "[]"),
                "status": row["status"],
            }
            objects.append(obj)
            object_map[row["id"]] = obj

        relations = self.relations_for_notebook(notebook_id)

        # Build name lookup for edge-text rendering: object_id → name
        obj_name_map: dict = {
            obj["id"]: (obj["payload"].get("name", "") if isinstance(obj["payload"], dict) else "")
            for obj in objects
        }

        # 3. Build embeddings dict; log + skip on any error
        # 运行时截断旁路(计划 §1.2,conflict 同步接线):此处原先连存储维过滤都
        # 没有 —— conflict_detect._cosine_sim 虽已改混维零容忍,这里仍须①先按
        # 存储原生维过滤(异维残留出局)②通过后截断到运行时空间,保证语义策略
        # 收到的向量同维可比(而非靠下游把混维对静默判 0 丢召回)。
        embeddings: dict | None = None
        if vec_rows:
            try:
                from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec
                _dim = self.settings.embed_dim
                _rd = resolve_runtime_dim(self.settings)
                embeddings = {}
                for r in vec_rows:
                    if not r["vector"]:
                        continue
                    arr = decode_vector(r["vector"])
                    if arr is None or arr.size != _dim:
                        continue
                    if _rd:
                        arr = truncate_vec(arr, _rd)
                    embeddings[r["object_id"]] = arr.tolist()
            except Exception:  # noqa: BLE001
                self.event_log.logger.debug(
                    "resolve_notebook_conflicts: failed to load embeddings for %s; "
                    "semantic strategy will be skipped",
                    notebook_id,
                )
                embeddings = None

        # 4. Detect candidates
        from app.services.kg.conflict_detect import detect_conflict_candidates
        notebook_tier = (nb_row["tier"] if nb_row else "personal")

        candidates = detect_conflict_candidates(
            objects,
            relations,
            embeddings=embeddings,
            sim_threshold=self.settings.kg_conflict_sim_threshold,
        )

        if not candidates:
            return {
                "detected": 0,
                "auto_applied": 0,
                "queued": 0,
                "skipped_llm": False,
            }

        # 5. Materialise T3 input items
        # Build relation lookup: rel_id → relation dict
        rel_map: dict = {r["id"]: r for r in relations}

        items = []
        for cand in candidates:
            kind = cand["kind"]
            left_ref = cand["left_ref"]
            right_ref = cand["right_ref"]

            if kind == "edge":
                # text: "src_name —edge_type→ tgt_name"
                left_rel = rel_map.get(left_ref, {})
                right_rel = rel_map.get(right_ref, {})

                def _edge_text(rel: dict) -> str:
                    src_name = obj_name_map.get(rel.get("source_object_id", ""), "")
                    tgt_name = obj_name_map.get(rel.get("target_object_id", ""), "")
                    etype = rel.get("edge_type", "")
                    return f"{src_name} —{etype}→ {tgt_name}"

                def _edge_source(rel: dict) -> str:
                    ev = rel.get("evidence") or []
                    if ev and isinstance(ev, list):
                        first = ev[0]
                        if not isinstance(first, dict):
                            return ""
                        # Relations store evidence as {"quote": ...} (kg_ingest.py);
                        # nodes store evidence as {"quoted_span": ...}.  Accept both.
                        text = (first.get("quoted_span") or first.get("quote") or "")
                        return text[:400]
                    return ""

                left_item = {
                    "text": _edge_text(left_rel),
                    "source_text": _edge_source(left_rel),
                    "object_type": None,
                    "tier": notebook_tier,
                }
                right_item = {
                    "text": _edge_text(right_rel),
                    "source_text": _edge_source(right_rel),
                    "object_type": None,
                    "tier": notebook_tier,
                }
            else:
                # kind == "node"
                left_obj = object_map.get(left_ref, {})
                right_obj = object_map.get(right_ref, {})

                def _node_text(obj: dict) -> str:
                    payload = obj.get("payload") or {}
                    name = payload.get("name", "") if isinstance(payload, dict) else ""
                    return name

                def _node_source(obj: dict) -> str:
                    ev_list = obj.get("evidence") or []
                    if ev_list and isinstance(ev_list, list):
                        first = ev_list[0]
                        if isinstance(first, dict):
                            return (first.get("quoted_span") or "")[:400]
                        # Evidence may be Evidence namedtuple / dataclass
                        return (getattr(first, "quoted_span", None) or "")[:400]
                    return ""

                left_item = {
                    "text": _node_text(left_obj),
                    "source_text": _node_source(left_obj),
                    "object_type": left_obj.get("object_type"),
                    "tier": notebook_tier,
                }
                right_item = {
                    "text": _node_text(right_obj),
                    "source_text": _node_source(right_obj),
                    "object_type": right_obj.get("object_type"),
                    "tier": notebook_tier,
                }

            items.append({
                "candidate": cand,
                "left": left_item,
                "right": right_item,
            })

        # 6. Adjudicate
        from app.services.kg.conflict_review import review_conflict_candidates
        verdicts = review_conflict_candidates(self.kg_llm_client, items)

        # 7. Record + (optionally) auto-apply
        auto_applied = 0
        queued = 0
        threshold = self.settings.kg_conflict_auto_apply_threshold

        for cand, verdict in zip(candidates, verdicts):
            kind = cand["kind"]
            conflict_type = verdict["conflict_type"]
            resolution = verdict["resolution"]
            winner_ref = verdict["winner_ref"]
            resolved_payload = verdict["resolved_payload"]
            confidence = verdict["confidence"]
            rationale = verdict["rationale"]

            # Record in queue
            candidate_id = self.write_conflict_candidate(
                notebook_id,
                kind=kind,
                left_ref=cand["left_ref"],
                right_ref=cand["right_ref"],
                conflict_type=conflict_type,
                resolution=resolution,
                winner_ref=winner_ref,
                resolved_payload=(
                    json.dumps(resolved_payload) if resolved_payload is not None else None
                ),
                confidence=confidence,
                rationale=rationale,
            )

            # Auto-apply?  Only for genuine conflicts with a non-trivial resolution.
            should_apply = (
                conflict_type != "none"
                and resolution != "keep"
                and confidence >= threshold
            )
            if should_apply:
                try:
                    self.apply_conflict_resolution(
                        notebook_id,
                        kind=kind,
                        left_ref=cand["left_ref"],
                        right_ref=cand["right_ref"],
                        resolution=resolution,
                        winner_ref=winner_ref,
                        resolved_payload=resolved_payload,
                    )
                    self.set_conflict_status(notebook_id, candidate_id, "applied")
                    auto_applied += 1
                except Exception:  # noqa: BLE001
                    self.event_log.logger.exception(
                        "resolve_notebook_conflicts: auto-apply failed for candidate %s "
                        "(notebook %s, kind=%s, left=%r, right=%r)",
                        candidate_id, notebook_id, kind,
                        cand["left_ref"], cand["right_ref"],
                    )
                    queued += 1
            else:
                queued += 1

        return {
            "detected": len(candidates),
            "auto_applied": auto_applied,
            "queued": queued,
            "skipped_llm": False,
        }

    def review_pending_merges(
        self,
        notebook_id: str,
        limit: int = 50,
        confirm_threshold: Optional[float] = None,
        separate_threshold: Optional[float] = None,
    ) -> dict:
        self.get_notebook(notebook_id)
        # 非对称阈值:auto-merge 需更高置信(误并不可逆、污染图);auto-keep-separate
        # 可低些(误判仅多留一对待审)。未显式传入则取 settings 默认(0.90 / 0.80)。
        confirm = confirm_threshold if confirm_threshold is not None else self.settings.kg_merge_confirm_threshold
        separate = separate_threshold if separate_threshold is not None else self.settings.kg_merge_separate_threshold
        pending = self._pending_merges_batch(notebook_id, max(1, min(limit, 200)))
        from app.services.concept_merge_review import review_merge_candidates
        # review_merge_candidates is total (fail-open, chunked); the outer try is
        # defense-in-depth so this endpoint can never 500 on an LLM deviation (the
        # route only catches KeyError). Same batching/concurrency as the rebuild site.
        try:
            decisions = review_merge_candidates(
                self.llm_client, pending,
                batch_size=self.settings.kg_merge_review_batch_size,
                max_workers=self.settings.kg_job_concurrency,
            )
        except Exception:
            self.event_log.logger.exception(
                "merge-review adjudication failed for %s; proceeding with no decisions",
                notebook_id,
            )
            decisions = []
        confirmed = rejected = unsure = 0
        now = _now()
        with self._write() as db:
            for decision in decisions:
                candidate_id = decision["candidate_id"]
                confidence = decision["confidence"]
                status = "pending"
                if decision["decision"] == "merge" and confidence >= confirm:
                    status = "confirmed"
                    confirmed += 1
                elif decision["decision"] == "keep_separate" and confidence >= separate:
                    status = "rejected"
                    rejected += 1
                else:
                    status = "deferred"
                    unsure += 1
                self._runtime.governance.record_merge_review(
                    db, notebook_id, candidate_id, status, confidence,
                    decision["rationale"], now,
                )
        if confirmed or rejected:
            self._mark_unified_kg_dirty(notebook_id)
            self._invalidate_unified_cache(notebook_id)
        return {"reviewed": len(decisions), "confirmed": confirmed, "rejected": rejected, "unsure": unsure}

    def merge_review_job_status(self, notebook_id: str) -> dict:
        with self._connect() as db:
            row = self._runtime.governance.merge_review_job_row(db, notebook_id)
        if row is None:
            return {"status": "idle", "total": 0, "done": 0, "error": ""}
        return {"status": row["status"], "total": int(row["total"]),
                "done": int(row["done"]), "error": row["error"]}

    def run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict:
        """Drain the whole pending merge queue in batches (each batch = one
        review_pending_merges call). Single-flight per notebook. Fail-open per
        batch; a batch that reviews 0 (LLM down) counts as a stall — abort after
        2 consecutive stalls so a persistent failure can't loop forever. Since
        Task 4 makes unsure→deferred, every reviewed candidate leaves pending, so
        a healthy run strictly shrinks the queue and terminates."""
        self.get_notebook(notebook_id)
        with self._write() as db:
            total = self._runtime.governance.begin_merge_review_job(
                db, notebook_id, _now()
            )
            if total is None:
                return {"status": "running", "already": True}
        done, stalls, error, final = 0, 0, "", "done"
        max_batches = (total // max(1, batch)) + 3
        try:
            for _ in range(max_batches):
                if not self._has_pending_merges(notebook_id):
                    break
                summary = self.review_pending_merges(notebook_id, limit=batch)
                reviewed = int(summary.get("reviewed", 0))
                done += reviewed
                with self._write() as db:
                    self._runtime.governance.set_merge_review_progress(
                        db, notebook_id, done, _now())
                if reviewed == 0:
                    stalls += 1
                    if stalls >= 2:
                        error, final = "LLM 预审连续无进展,已中止", "failed"
                        break
                else:
                    stalls = 0
        except Exception as exc:  # noqa: BLE001
            error, final = f"{type(exc).__name__}: {exc}", "failed"
            self.event_log.logger.exception("merge review job failed for %s", notebook_id)
        with self._write() as db:
            self._runtime.governance.finish_merge_review_job(
                db, notebook_id, final, error, _now())
        return {"status": final, "total": total, "done": done, "error": error}

    def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
        return self._runtime.governance.decided_pairs(notebook_id)

    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        return self._runtime.governance.decided_seed_pairs(notebook_id)

    def concept_whitelist_terms(self) -> set:
        with self._connect() as db:
            return self._runtime.governance.concept_whitelist_terms(db)

    def concept_whitelist_list(self) -> List[dict]:
        with self._connect() as db:
            rows = self._runtime.governance.concept_whitelist_rows(db)
        return [{"term": r["term"], "note": r["note"], "created_at": r["created_at"]} for r in rows]

    def concept_whitelist_add(self, term: str, note: str = "") -> dict:
        from app.services.kg.filters import _norm
        t = _norm(term)
        if not t:
            raise ValueError("empty term")
        now = _now()
        with self._write() as db:
            self._runtime.governance.add_whitelist_term(db, t, note, now)
        return {"term": t, "note": note, "created_at": now}

    def concept_whitelist_remove(self, term: str) -> None:
        from app.services.kg.filters import _norm
        with self._write() as db:
            self._runtime.governance.remove_whitelist_term(db, _norm(term))

    def _invalidate_unified_cache(self, notebook_id: str) -> None:
        for key in [k for k in self._unified_cache if k[0] == notebook_id]:
            self._unified_cache.pop(key, None)
        # Matrices are stored under "{nb}:matrix:{table}" (see _vector_matrix). The old
        # "{nb}:knowledge" key never matched (dead no-op). Invalidate BOTH embedding
        # tables so an in-place re-embed (same row count + same-second created_at, i.e.
        # an unchanged version tuple) cannot serve a stale vector.
        for table in ("knowledge_embeddings", "element_embeddings"):
            self._vector_cache.invalidate(f"{notebook_id}:matrix:{table}")
        self._vector_cache.invalidate(f"{notebook_id}:kwtok")
        # Federated graph caches are keyed "{active_id}:fed_rxgraph" — the ACTIVE
        # (personal) notebook's id, NOT this notebook's. A change in THIS notebook
        # (e.g. a base notebook) may affect any federated graph that includes it,
        # so evict every fed_rxgraph entry; tracking participants per key is
        # overkill for the POC. This explicit eviction also guards against
        # same-second in-place edits that leave the version tuple unchanged.
        for key in [k for k in self._vector_cache._store if k.endswith(":fed_rxgraph")]:
            self._vector_cache.invalidate(key)
        # PPR graph (concept_clusters + knowledge_objects + chunks → HippoRAG graph) —
        # evict so a same-second KG edit with an unchanged version tuple cannot serve stale.
        self._vector_cache.invalidate(f"{notebook_id}:ppr_graph")
        # entity->chunk / element->chunk reverse maps (P0-5) — evict so a same-second
        # in-place evidence/element_ids edit with an unchanged version tuple cannot
        # serve a stale membership map to the PPR-fallback / chunk-overlay paths.
        self._vector_cache.invalidate(f"{notebook_id}:entchunk")
        self._vector_cache.invalidate(f"{notebook_id}:elemchunk")
        # review_queue's edge betweenness centrality map (P0-3) — evict so a
        # same-second in-place edit (e.g. review_status flip) with an unchanged
        # version tuple cannot serve a stale centrality map.
        self._vector_cache.invalidate(f"{notebook_id}:edge_centrality")
        # cluster_map (member_object_id -> canonical_id, P1-2) — evict so a
        # same-second concept_clusters rewrite (rename / rebuild's DELETE+INSERT,
        # which can land COUNT and MAX(created_at) on the same values as before)
        # with an unchanged version tuple cannot serve a stale membership map.
        self._vector_cache.invalidate(f"{notebook_id}:clustermap")
        # notebook_copy_stats memo (perf-audit A3) — evict so a same-second
        # in-place edit with an unchanged version tuple cannot serve a stale
        # size/copyable verdict to the ask-path guards / share paths.
        self._vector_cache.invalidate(f"{notebook_id}:copystats")

    def _cluster_input_version(self, notebook_id: str, *, exclude_emb_count: bool = False) -> str:
        """O(1) content-hash gating rebuild_unified_kg's skip-when-unchanged path.

        PRIMARY change signal = the monotonic kg_mutation_seq, bumped by
        _mark_unified_kg_dirty on EVERY KG write (the single choke point all
        mutations funnel through). It advances deterministically on ANY edit —
        adds, deletes, AND in-place edits (concept rename, confirmed<->rejected
        decision flip, re-embed) — with NO dependence on timestamp granularity. An
        earlier version used COUNT+MAX(updated_at/created_at); at _now()'s 1-second
        resolution that MISSED same-second in-place edits at fixed cardinality
        (COUNT unchanged, MAX pinned by another row) and could serve a stale
        clustering. The seq closes that hole by construction.

        BACKSTOP = three COUNTs (objects status!='deprecated'; confirmed/rejected
        decided pairs — WHERE mirrors decided_pairs() EXACTLY, pending excluded so
        rebuild's own pending-refresh doesn't move the version; embeddings) plus
        settings.embed_dim. These are pure belt-and-suspenders: they catch an
        add/delete that somehow bypassed _mark_unified_kg_dirty. No timestamps.

        Clustering SETTINGS (thresholds, rep_ann_max) are intentionally NOT here;
        the explicit 刷新图谱 / recluster paths pass force=True to pick those up.

        Stable across a rebuild: rebuild writes only concept_clusters + pending
        concept_merge_candidates and preserves kg_mutation_seq (its end-write omits
        the seq column), so this value is identical before and after a rebuild.

        exclude_emb_count: when True, OMIT the embeddings COUNT term (emb_c) from
        the hash — the result is intentionally DIFFERENT from the default (False)
        call for the same notebook/state; it is NOT a drop-in replacement, it's a
        separate "backfill-stable" version namespace. Used by rebuild_unified_kg's
        LLM-stage checkpoints (merge_review, concept_desc): the node-vector
        backfill commits INCREMENTALLY, so emb_c climbs mid-backfill while seq/
        obj_c/dec_c stay put. Keying those checkpoints on the default (emb-
        inclusive) version would make a crash-then-resume during node-vector
        backfill look like a version change and GC away a possibly hours-long
        adjudication that is still perfectly valid. item_key already provides
        fine-grained correctness within a checkpoint, so dropping emb_c here is
        safe. The skip-gate itself must keep using the DEFAULT (emb-inclusive)
        version unchanged — a new/changed embedding is a real reason to recluster.
        """
        with self._connect() as db:
            seq, obj_c, dec_c, emb_c = self._runtime.unified_kg.cluster_input_facts(
                db, notebook_id, exclude_emb_count=exclude_emb_count
            )
        # runtime_dim 追加(非替换 embed_dim;T3/R4):聚类/融合空间统一到运行时
        # 空间,切 EMBED_RUNTIME_DIM 后版本闸不得跳过重聚(旧空间的簇不再有效)。
        from app.services.vector_index import resolve_runtime_dim
        # 算法版本(纯代码分量):归一化/哨兵/护栏等 seed·聚类语义的代码改动不动任何
        # 数据派生分量(seq/COUNT/dim 全不变),但会改变聚类结果。折进版本串后,已部署
        # 库改代码 → 下次「刷新图谱」(force=False)因版本失配真重算一次,不再被闸静默
        # 跳过。bump 约定见 kg_merge.CLUSTER_ALGO_VERSION。函数内 import:测试可 patch
        # kg_merge 模块属性,调用时按 patch 后的值读取。
        from app.services.kg_merge import CLUSTER_ALGO_VERSION
        parts = [
            "v2", notebook_id,
            int(seq),
            int(obj_c),
        ]
        if not exclude_emb_count:
            parts.append(int(emb_c))     # 默认路径:与旧版位次/取值一字不差,skip-gate 哈希不变
        parts += [
            int(dec_c),
            int(self.settings.embed_dim),
            resolve_runtime_dim(self.settings),
            int(CLUSTER_ALGO_VERSION),
        ]
        return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()

    def _mark_unified_kg_dirty(self, notebook_id: str) -> None:
        # Bump the monotonic mutation counter on every KG write. This is the ONLY
        # place kg_mutation_seq advances, and every mutation funnels through here,
        # so _cluster_input_version sees a deterministic change on any edit —
        # including same-second in-place edits (rename/decision-flip/re-embed) that
        # a timestamp MAX at 1s resolution would miss. The upsert (store-owned
        # since Task 13) references the table's own current value (+1), NOT
        # excluded, so an existing row increments rather than resets to the
        # inserted literal (1). First mutation -> seq 1.
        now = _now()
        with self._write() as db:
            self._runtime.unified_kg.mark_dirty(db, notebook_id, now)
        # Re-arm maybe_auto_index's once-set: the index this nb was previously
        # judged against (fresh/absent) is now stale by construction (KG just
        # changed), so the next write-path or read-path fallback call should
        # re-evaluate rather than trust a stale "checked" verdict.
        self._auto_index_checked.discard(notebook_id)
        # Content just changed → the memoized corpus-language hint may be stale
        # (a new source could add a 2nd language). Drop it; next _notebook_langs
        # re-samples. This is the single mutation funnel, so it covers chunk adds,
        # re-chunk, re-embed and KG edits — the cheapest correct invalidation site.
        self._notebook_langs_cache.pop(notebook_id, None)

    def _bump_cluster_mutation_seq(self, db, notebook_id: str) -> None:
        """concept_clusters 写路径的单调计数器 bump。与 _mark_unified_kg_dirty 不同,
        本 helper 在调用方已持有的写事务 db 内执行(写簇+bump 同 commit,原子——
        不存在"簇写了、seq 没 bump"的窗口)。kg_mutation_seq 不在此处动:rebuild
        刻意保持它稳定(幂等,见 _cluster_input_version),clusters 的变化信号独立成列。"""
        self._runtime.unified_kg.bump_cluster_seq(db, notebook_id, _now())

    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = self._runtime.unified_kg.state_row(db, notebook_id)
            clusters = self._runtime.unified_kg.distinct_cluster_count(db, notebook_id)
        viz = self._viz_index_probe(notebook_id)
        viz_building = notebook_id in self._viz_building
        if row is None:
            return {"dirty": False, "last_rebuild_at": "", "objects": 0, "relations": 0,
                    "clusters": int(clusters), **viz, "viz_building": viz_building}
        return {
            "dirty": bool(row["dirty"]),
            "last_rebuild_at": row["last_rebuild_at"],
            "objects": int(row["object_count"]),
            "relations": int(row["relation_count"]),
            "clusters": int(row["cluster_count"] or clusters),
            **viz,
            "viz_building": viz_building,
        }

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
        """Graph data for the KG view. When `limit` is set and the full graph has
        more nodes, return only the `limit` most-connected ones (core subgraph) so
        the UI doesn't choke; `total_nodes`/`total_edges`/`truncated` let the
        frontend offer "widen range". The full graph is still derived + cached;
        the limit is a cheap slice applied after the cache.

        Large-notebook guard (checked FIRST, before any other branching): for a
        notebook whose non-deprecated object count exceeds
        settings.viz_sync_build_max_objects, _unified_graph_full must NEVER be
        called — it pulls every knowledge_objects row (full payloads) into
        Python dicts AND caches the multi-GB result in self._unified_cache,
        which is how a 490k-object production library fills 64GB RAM. This
        applies regardless of `limit`/`level`: a missing `limit` (old frontend,
        bare API calls, curl/tests) gets a server-side default cap
        (settings.viz_default_limit), and level='concept' is treated like
        'object' (the persisted folded viz graph is object-level only — the
        frontend always sends level=object, but we still defend the API for
        level=concept / no-level callers that would otherwise slip through)."""
        with self._connect() as db:
            nb_count = db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,)).fetchone()["c"]
        if int(nb_count) > self.settings.viz_sync_build_max_objects:
            effective_limit = limit if limit is not None else self.settings.viz_default_limit
            idx = self._viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(notebook_id, idx, effective_limit)
            # No index available yet: either a background build was just
            # spawned inside _viz_index, or (rare race) it isn't tracked as
            # building anymore. Either way, large notebooks never fall
            # through to _unified_graph_full below.
            return {"nodes": [], "edges": [], "total_nodes": 0,
                    "total_edges": 0, "truncated": False, "viz_building": True}

        # Bounded fast-path (small notebooks only, from here down): when a
        # limit is requested AND a valid scale index with a persisted folded
        # viz graph exists, serve the degree-top-N core straight from the
        # compact arrays (no full re-fold). EQUIVALENT to the legacy slice
        # below (same node-id set / totals / shape). Small notebooks with no
        # index fall through unchanged.
        if limit is not None and level != "concept":
            idx = self._viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(notebook_id, idx, limit)
            if idx is None and notebook_id in self._viz_building:
                # A background build was spawned inside _viz_index instead of
                # blocking this request on a minutes-long full-graph fold.
                # Surface a placeholder the frontend can poll on instead of
                # the (also expensive) _unified_graph_full fallback below.
                return {"nodes": [], "edges": [], "total_nodes": 0,
                        "total_edges": 0, "truncated": False, "viz_building": True}
        full = self._unified_graph_full(notebook_id, level)
        total_nodes, total_edges = len(full["nodes"]), len(full["edges"])
        from app.services.kg_merge import limit_graph_by_degree
        sliced = limit_graph_by_degree(full, limit) if limit is not None else full
        return {
            "nodes": sliced["nodes"],
            "edges": self._annotate_edge_support(notebook_id, sliced["edges"]),
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "truncated": len(sliced["nodes"]) < total_nodes,
        }

    def _unified_graph_full(self, notebook_id: str, level: str = "concept") -> dict:
        self.get_notebook(notebook_id)
        cached = self._unified_cache.get((notebook_id, level))
        if cached is not None:
            return cached
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = db.execute(
                "SELECT id, object_type, payload, status FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchall()
        nodes = [{"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}")} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"], "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        g = derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))
        if level == "concept":
            cids = {n["id"] for n in g["nodes"] if n["object_type"] == "concept"}
            g = {"nodes": [n for n in g["nodes"] if n["object_type"] == "concept"],
                 "edges": [e for e in g["edges"] if e["source_object_id"] in cids and e["target_object_id"] in cids]}
        self._unified_cache[(notebook_id, level)] = g
        return g

    def _viz_dict(self, idx):
        """Pack a ScaleIndex's persisted viz arrays into the dict shape the pure
        viz_neighbors function expects (1-hop topology over the folded graph)."""
        return {"viz_ids": idx.viz_ids, "viz_adj": idx.viz_adj,
                "viz_deg": idx.viz_deg, "viz_types": idx.viz_types}

    def _viz_node(self, idx, fid, name_by_id, type_by_id):
        """One folded node in the unified_graph shape {id,object_type,payload:{name}}.
        Names/types come from the persisted viz arrays (no DB round-trip)."""
        return {"id": fid, "object_type": type_by_id.get(fid, ""),
                "payload": {"name": name_by_id.get(fid, "")}}

    def _unified_graph_bounded(self, notebook_id: str, idx, limit: int) -> dict:
        """Degree-top-N core from the persisted folded viz graph — EQUIVALENT to
        limit_graph_by_degree(_unified_graph_full(nb,'object'), limit): same node-id
        set (incl. degree-tie order), same kept edges, same totals/shape.

        Selection mirrors limit_graph_by_degree EXACTLY: stable sort by degree desc
        preserving viz_ids order (which equals _unified_graph_full's node order),
        keep the first `limit`, then keep edges whose both endpoints survive."""
        import numpy as np
        ids, deg = idx.viz_ids, idx.viz_deg
        names = idx.viz_names or []
        types = idx.viz_types or []
        name_by_id = {n: nm for n, nm in zip(ids, names)}
        type_by_id = {n: t for n, t in zip(ids, types)}
        total_nodes = len(ids)
        edges_all = idx.viz_edges or []

        if limit is None or limit >= total_nodes:
            keep_ids = list(ids)
        else:
            # stable: Python's sorted is stable; deg desc with original-index tiebreak
            order = sorted(range(total_nodes), key=lambda i: -int(deg[i]))
            keep_ids = [ids[i] for i in order[:limit]]
        keep = set(keep_ids)

        nodes = [self._viz_node(idx, fid, name_by_id, type_by_id) for fid in ids if fid in keep]
        kept_edges = [{"source_object_id": s, "target_object_id": t, "edge_type": et}
                      for s, t, et in edges_all if s in keep and t in keep]
        kept_edges = self._annotate_edge_support(notebook_id, kept_edges)
        return {
            "nodes": nodes,
            "edges": kept_edges,
            "total_nodes": total_nodes,
            "total_edges": len(edges_all),
            "truncated": len(nodes) < total_nodes,
        }

    def kg_neighbors(self, notebook_id: str, object_id: str, cap: int = 50) -> dict:
        """1-hop neighborhood of `object_id` (≤cap) in the folded concept graph.

        Fast path: persisted viz graph → viz_neighbors → hydrate names/edge_types
        (same node/edge shape as unified_graph). Fallback (no index): a bounded
        1-hop query over knowledge_relations, folding endpoints via cluster_map so
        the shape matches the unified graph the frontend already renders."""
        self.get_notebook(notebook_id)
        idx = self._viz_index(notebook_id)
        if idx is not None and getattr(idx, "viz_ids", None) is not None:
            from app.services.kg.scale_index import viz_neighbors
            nb = viz_neighbors(self._viz_dict(idx), object_id, cap)
            name_by_id = {n: nm for n, nm in zip(idx.viz_ids, idx.viz_names or [])}
            type_by_id = {n: t for n, t in zip(idx.viz_ids, idx.viz_types or [])}
            nbr_ids = {n["id"] for n in nb["nodes"]}
            nodes = [self._viz_node(idx, fid, name_by_id, type_by_id) for fid in nbr_ids]
            # edge_type: look up from the persisted folded edge list (either
            # direction); default 'related' if not found (e.g. hub/membership).
            et_map = {}
            for s, t, et in (idx.viz_edges or []):
                et_map[(s, t)] = et
            edges = []
            for e in nb["edges"]:
                s, t = e["source"], e["target"]
                et = et_map.get((s, t)) or et_map.get((t, s)) or "related"
                edges.append({"source_object_id": s, "target_object_id": t, "edge_type": et})
            edges = self._annotate_edge_support(notebook_id, edges)
            return {"nodes": nodes, "edges": edges}
        return self._kg_neighbors_db(notebook_id, object_id, cap)

    def _kg_neighbors_db(self, notebook_id: str, object_id: str, cap: int) -> dict:
        """DB fallback for kg_neighbors: bounded 1-hop over knowledge_relations,
        folding concept endpoints to canonical ids (cluster_map) so the result
        matches the folded unified-graph view. `object_id` is interpreted as a
        folded id (canonical_id or raw object_id)."""
        cmap = self.cluster_map(notebook_id)
        # member -> canonical for matching folded id back to raw endpoints
        members_of = {}
        for m, c in cmap.items():
            members_of.setdefault(c, []).append(m)
        raw_ids = set(members_of.get(object_id, [object_id]))
        ph = ",".join("?" for _ in raw_ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT source_object_id, target_object_id, edge_type "
                f"FROM knowledge_relations WHERE notebook_id=? "
                f"AND (source_object_id IN ({ph}) OR target_object_id IN ({ph}))",
                (notebook_id, *raw_ids, *raw_ids),
            ).fetchall()
        # object_type + name lookup for the folded nodes we touch
        def canon(oid):
            return cmap.get(oid, oid)
        edges, nbr_ids, seen = [], set(), set()
        for r in rows:
            s, t = canon(r["source_object_id"]), canon(r["target_object_id"])
            if s == t:
                continue
            # orient relative to the queried folded node
            if s == object_id:
                other = t
            elif t == object_id:
                other = s
            else:
                continue
            if other in seen or len(seen) >= cap:
                continue
            seen.add(other)
            nbr_ids.add(other)
            edges.append({"source_object_id": object_id, "target_object_id": other,
                          "edge_type": r["edge_type"]})
        # Include the queried node itself only when it's a known canonical id or
        # has neighbours; unknown / non-canonical ids that produced no edges are
        # dropped so that both the viz fast-path and the DB path return equivalent
        # empty results for unrecognised lookups.
        canonical_ids = set(cmap.values())
        include_self = bool(nbr_ids) or object_id in canonical_ids
        all_ids = (nbr_ids | {object_id}) if include_self else nbr_ids
        meta = self._object_meta(notebook_id, all_ids, cmap)
        nodes = [{"id": oid, "object_type": meta.get(oid, ("", ""))[0],
                  "payload": {"name": meta.get(oid, ("", ""))[1]}} for oid in all_ids]
        edges = self._annotate_edge_support(notebook_id, edges)
        return {"nodes": nodes, "edges": edges}

    def _object_meta(self, notebook_id: str, folded_ids, cmap):
        """{folded_id: (object_type, name)} for a set of folded ids. A folded
        concept id is either a canonical_id (resolve via a member object) or a raw
        object id. Used by the DB neighbors fallback to hydrate node display."""
        members_of = {}
        for m, c in cmap.items():
            members_of.setdefault(c, []).append(m)
        # representative raw object id per folded id
        rep = {fid: (members_of.get(fid, [fid])[0]) for fid in folded_ids}
        rep_ids = set(rep.values())
        if not rep_ids:
            return {}
        ph = ",".join("?" for _ in rep_ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, object_type, payload FROM knowledge_objects "
                f"WHERE notebook_id=? AND id IN ({ph})",
                (notebook_id, *rep_ids),
            ).fetchall()
        by_raw = {r["id"]: (r["object_type"], json.loads(r["payload"] or "{}").get("name", ""))
                  for r in rows}
        return {fid: by_raw.get(rep[fid], ("", "")) for fid in folded_ids}

    def _stream_seed_reps(self, notebook_id: str, object_type: str, seed_fn,
                          run_id: str = "", compute_reps: bool = True):
        """Stream knowledge_objects of one type → populate kg_cluster_scratch
        (object_id → seed) and accumulate seed-level aggregates, memory-bounded
        by #unique seeds (NOT #objects). Returns (reps, members_count,
        seed_first_name):
          - reps[seed]          = mean of member vectors (dim-filtered); empty
                                  dict when compute_reps=False (skips Pass B).
          - members_count[seed] = #objects for that seed
          - seed_first_name[s]  = first-seen object name for that seed
        kg_cluster_scratch rows are scoped by (notebook_id, run_id) so concurrent
        rebuilds of the same notebook never wipe each other's scratch. The caller
        is responsible for clearing rows with the same (notebook_id, run_id) before
        calling this and after all types are processed.

        compute_reps=False skips Pass B (embeddings join) entirely and returns
        reps={}. Use for non-concept types (claim/formula/procedure) where
        cluster_seeds receives {} vectors anyway — avoids a large ANN over
        non-concept seeds."""
        import numpy as np
        from app.services.kg_merge import (build_acronym_alias_map, _seed_with_alias,
                                           _norm, seed_or_unique)

        embed_dim = self.settings.embed_dim
        # Scratch is scoped to (notebook_id, run_id); clear only this run's rows
        # for the current type before repopulating (types are processed serially
        # within a run, so a simple delete by run_id is safe).
        with self._write() as db:
            db.execute("DELETE FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
                       (notebook_id, run_id))

        # Pass A1: stream names once to build the acronym alias map.
        def _name_gen():
            with self._connect() as db:
                cur = db.execute(
                    "SELECT payload FROM knowledge_objects "
                    "WHERE notebook_id=? AND object_type=? AND status!='deprecated' "
                    "ORDER BY rowid",
                    (notebook_id, object_type))
                for r in cur:
                    yield _fast_loads(r["payload"] or "{}").get("name", "")
        alias_map = build_acronym_alias_map(_name_gen())

        # Pass A2: stream (id, name) → seed; accumulate counts/first-name; buffer
        # (notebook_id, id, seed) into scratch in batches.
        members_count: Dict[str, int] = {}
        seed_first_name: Dict[str, str] = {}
        buf: List[tuple] = []
        with self._connect() as rdb:
            # ORDER BY rowid: canonical-name selection here is first-seen per seed
            # (seed_first_name), so the stream order must be deterministic and
            # independent of which index the planner happens to pick — otherwise
            # adding/removing an index silently changes canonical names + desc-cache
            # keys. rowid = insertion order, matching the historical behaviour.
            cur = rdb.execute(
                "SELECT id, payload FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type=? AND status!='deprecated' "
                "ORDER BY rowid",
                (notebook_id, object_type))
            with self._write() as wdb:
                for r in cur:
                    pay = _fast_loads(r["payload"] or "{}")
                    name = pay.get("name", "")
                    # Pass the full payload-bearing object so seed_fn can use it
                    # (e.g. seed_procedure appends a steps signature from payload).
                    # Mirrors the legacy cluster_objects(tobjs={name,payload}, ...).
                    seed = seed_or_unique(
                        _seed_with_alias({"name": name, "payload": pay}, seed_fn, alias_map),
                        r["id"])
                    members_count[seed] = members_count.get(seed, 0) + 1
                    seed_first_name.setdefault(seed, name)
                    buf.append((notebook_id, run_id, r["id"], seed))
                    if len(buf) >= 1000:
                        wdb.executemany(
                            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
                            buf)
                        buf.clear()
                if buf:
                    wdb.executemany(
                        "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
                        buf)
                    buf.clear()

        # Pass B: stream vectors joined to seeds → rep mean per seed. Bounded by
        # #unique seeds (rep_sum/rep_cnt), NOT #objects. Dim-mismatched legacy
        # vectors are skipped (mirrors the old length filter).
        # Skipped entirely when compute_reps=False (non-concept types where
        # cluster_seeds receives {} vectors anyway — avoids an ANN over
        # millions of claim/formula/procedure seeds at scale).
        if not compute_reps:
            return {}, members_count, seed_first_name

        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        # 运行时截断旁路(计划 §1.2 旁路 4):Pass B 的 legacy-JSON 分支绕过
        # decode_vector,是点名的已知漏点 —— 两分支在共同的存储维过滤(既有语义,
        # 先过滤)之后、rep 累加之前统一截断到运行时空间(seed rep 内存亦 ÷4)。
        rd = resolve_runtime_dim(self.settings)
        rep_dim = rd if (rd and rd < embed_dim) else embed_dim   # 截断后的确定维(R9)
        rep_sum: Dict[str, "np.ndarray"] = {}
        rep_cnt: Dict[str, int] = {}
        with self._connect() as db:
            cur = db.execute(
                "SELECT s.seed AS seed, e.vector AS vector "
                "FROM knowledge_embeddings e "
                "JOIN kg_cluster_scratch s ON s.object_id=e.object_id "
                "  AND s.notebook_id=e.notebook_id AND s.run_id=? "
                "WHERE e.notebook_id=?",
                (run_id, notebook_id))
            for r in cur:
                # decode_vector: bytes(BLOB)->frombuffer zero-parse, str(legacy
                # JSON)->_fast_loads-equivalent json path. Mirrors build_matrix.
                raw = r["vector"]
                if isinstance(raw, (bytes, bytearray, memoryview)):
                    arr = decode_vector(raw)
                else:
                    v = _fast_loads(raw)
                    arr = np.asarray(v, dtype=np.float32)
                if arr is None or arr.size != embed_dim:
                    continue
                if rd:
                    arr = truncate_vec(arr, rd)
                # R9 守卫:BLOB/legacy-JSON 双分支统一维后才允许累加 ——
                # 混维 += 在 numpy 下是硬错,某分支漏截时在此现形而非静默。
                assert arr.size == rep_dim, \
                    f"seed rep dim {arr.size} != {rep_dim} (branch missed truncation?)"
                seed = r["seed"]
                if seed in rep_sum:
                    rep_sum[seed] += arr
                    rep_cnt[seed] += 1
                else:
                    rep_sum[seed] = arr.copy()
                    rep_cnt[seed] = 1
        reps: Dict[str, "np.ndarray"] = {s: rep_sum[s] / rep_cnt[s] for s in rep_sum}
        return reps, members_count, seed_first_name

    def _write_cluster_map_streamed(self, notebook_id: str, object_type: str,
                                    seed_to_canonical: Dict[str, str],
                                    canonical_names: Dict[str, str],
                                    desc_by_cid: Optional[Dict[str, str]] = None,
                                    desc_sig_by_cid: Optional[Dict[str, str]] = None,
                                    run_id: str = "") -> None:
        """Persist concept_clusters rows for one type by streaming
        kg_cluster_scratch (object_id, seed). Matches write_clusters' columns and
        DELETE scope EXACTLY (clear-by-(notebook_id, object_type), then insert one
        row per member object). Rows whose seed has no canonical are skipped.
        Only reads scratch rows matching run_id so concurrent rebuilds don't cross."""
        now = _now()
        desc_by_cid = desc_by_cid or {}
        desc_sig_by_cid = desc_sig_by_cid or {}
        with self._connect() as rdb:
            cur = rdb.execute(
                "SELECT object_id, seed FROM kg_cluster_scratch "
                "WHERE notebook_id=? AND run_id=?",
                (notebook_id, run_id))
            with self._write() as wdb:
                wdb.execute("DELETE FROM concept_clusters WHERE notebook_id=? AND object_type=?",
                            (notebook_id, object_type))
                buf: List[tuple] = []
                for r in cur:
                    cid = seed_to_canonical.get(r["seed"])
                    if cid is None:
                        continue
                    buf.append((_new_id("cc"), notebook_id, cid, r["object_id"],
                                canonical_names.get(cid, ""), object_type,
                                desc_by_cid.get(cid, ""), desc_sig_by_cid.get(cid, ""), now))
                    if len(buf) >= 1000:
                        wdb.executemany(
                            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,canonical_desc_sig,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", buf)
                        buf.clear()
                if buf:
                    wdb.executemany(
                        "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,canonical_desc_sig,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", buf)
                    buf.clear()
                # P0-A: same commit as the DELETE+INSERT above (wdb, not rdb) —
                # every rebuild pass through this streamed writer rewrites
                # concept_clusters for `object_type`, so it must bump every time
                # (unconditional, unlike append_clusters' added>0 guard: a
                # rebuild that clusters down to zero rows for this type is still
                # a real content change from whatever was there before).
                self._bump_cluster_mutation_seq(wdb, notebook_id)

    def rebuild_unified_kg(self, notebook_id: str,
                           progress: Optional[Callable[[str, int, int], None]] = None,
                           force: bool = False, fresh: bool = False) -> int:
        """Cluster the notebook's Concepts; persist concept_clusters + refresh
        pending candidates (preserving confirmed/rejected). Returns #clusters.

        Skip-when-unchanged gate: a content-hash of the clustering inputs
        (_cluster_input_version) is stored at each rebuild. When force=False and
        that version still matches AND clusters already exist, the whole recompute
        is skipped and the cached cluster count returned — nothing is deleted or
        rewritten. The 刷新图谱 button goes through this force=False path (see
        api/routes.py); the version now folds in kg_merge.CLUSTER_ALGO_VERSION, so a
        code-only change to seed/clustering SEMANTICS bumps the version and the next
        automatic rebuild truly recomputes rather than being silently skipped.
        force=True is used only by the explicit recluster CLI paths
        (scripts/recluster_kg.py, batch_ingest rebuild_only): it bypasses the gate
        and additionally picks up clustering-SETTINGS changes (thresholds,
        rep_ann_max) the data-version can't see.

        Memory-bounded (streamed): object→seed mappings live in the
        kg_cluster_scratch table; only seed-level aggregates (reps, counts,
        names) are held in RAM, so peak memory scales with #unique name-seeds,
        not #objects. Clustering semantics are identical to the legacy
        all-objects-in-memory path (guarded by tests/test_unified_kg_repository,
        test_cross_doc_merge, test_kg_merge, test_rebuild_streaming)."""
        self.get_notebook(notebook_id)
        # fresh 隐含强制重建:fresh 要清 checkpoint 并重裁,只有真正走到 rebuild 分支才有意义。
        # 两个 CLI caller 已保证 force=(rebuild_only or fresh)/force=fresh;这里在函数边界再兜一层,
        # 防将来新增 caller 传 force=False+fresh=True 时被 skip-gate 静默跳过、clear 落空(defense-in-depth)。
        force = force or fresh
        # Content-version of the clustering inputs, captured ONCE at entry. Reused
        # both for the skip gate below and for the END-write (so the stored version
        # reflects the inputs this rebuild actually consumed).
        _ver = self._cluster_input_version(notebook_id)
        if not force:
            with self._connect() as db:
                row = self._runtime.unified_kg.state_row(db, notebook_id)
                cc = self._runtime.unified_kg.concept_clusters_count(db, notebook_id)
            if row and row["cluster_input_version"] and row["cluster_input_version"] == _ver and cc > 0:
                cached = int(row["cluster_count"] or 0)
                self.event_log.logger.info(
                    "kg-rebuild[%s] skipped — inputs unchanged since last rebuild (%s clusters)",
                    notebook_id, cached)
                if progress is not None:
                    try:
                        progress(f"跳过:自上次 rebuild 起输入未变化({cached} clusters)", 0, 0)
                    except Exception:
                        pass
                # canonical 关系层(派生)同款增量:自带 seq 闸,KG 未变即秒级 no-op。
                try:
                    self.rebuild_canonical_relations(notebook_id)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                # 共提桥接层(派生)同款增量:自带 mention_seq 闸,KG 未变即秒级 no-op。
                try:
                    self.rebuild_mention_bridge(notebook_id)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "mention_bridge_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                # 增量:聚类被跳过(输入未变),但社区可能未建/过期 → rebuild_communities
                # 自带版本闸,只在需要时建(纯图、无 LLM、秒级)。这让「只需重建社区」的
                # 场景在原有刷新流程里零额外成本达成;fail-open,不拖垮跳过路径。
                try:
                    self.rebuild_communities(notebook_id, level=0)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "communities_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
                return cached
        # 断点续跑:进到这里=已确定真正要重算(force=True,或 force=False 因版本
        # 失配落空)——checkpoint GC/clear 移到这里(而非版本闸之前)才对:早前放在
        # 闸前时,force=False 且判定跳过的路径也会顺带做一次 GC 写,破坏了「跳过=零
        # 写入」的不变量。checkpoint 版本刻意排除 emb_c(节点向量增量 backfill 期间
        # emb_c 单调爬升但 seq/obj_c/dec_c 不变):这样节点向量阶段中途崩溃后,下次
        # --rebuild-only 续跑不会因 emb_c 变了就把 merge_review/concept_desc
        # checkpoint 当作「输入变了」GC 掉、被迫重新走一遍可能数小时的 LLM 裁决;
        # item_key 已经提供细粒度正确性。fresh 仍清全部 checkpoint(强制两个 LLM
        # 阶段重跑)。fail-open。
        _ck_ver = self._cluster_input_version(notebook_id, exclude_emb_count=True)
        try:
            if fresh:
                self._runtime.unified_kg.checkpoint_clear(notebook_id)
            else:
                self._runtime.unified_kg.checkpoint_gc(notebook_id, _ck_ver)
        except Exception:  # noqa: BLE001 — checkpoint 维护失败不能打断 rebuild
            self.event_log.logger.warning("rebuild checkpoint GC/clear 失败 for %s", notebook_id, exc_info=True)
        from uuid import uuid4 as _uuid4
        from app.services.kg_merge import (cluster_seeds, _norm, _discriminative_conflict,
                                           seed_claim, seed_formula, seed_procedure)
        # Each rebuild gets a unique run_id so concurrent rebuilds of the SAME
        # notebook never wipe or read each other's scratch rows.
        run_id = _uuid4().hex

        # Sub-stage instrumentation: log each stage's name+counts+elapsed on two
        # channels (event_log INFO + progress banner). Pure logging — no effect on
        # clustering results or the progress=None data path.
        import time as _time
        _t_total = _time.perf_counter()
        def _stage(msg: str) -> None:
            self.event_log.logger.info("kg-rebuild[%s] %s", notebook_id, msg)
            if progress is not None:
                try:
                    progress(msg, 0, 0)
                except Exception:
                    pass

        # --- Concepts (vector + name-seed clustering) ----------------------
        # _stream_seed_reps re-populates kg_cluster_scratch for object_type=concept
        # (object_id -> seed) and returns seed-level aggregates only.
        _t = _time.perf_counter()
        reps, members_count, seed_first_name = self._stream_seed_reps(
            notebook_id, "concept", lambda o: _norm(o["name"]), run_id=run_id)
        # IMPORTANT: include seeds with NO vector (name-only) — use members_count
        # keys, not reps keys, to match the legacy all-objects path.
        seeds = sorted(members_count)
        _stage(f"concept: streamed {sum(members_count.values())} objs → "
               f"{len(seeds)} seeds, {len(reps)} vecs "
               f"({_time.perf_counter() - _t:.1f}s)")
        decided = self.decided_seed_pairs(notebook_id)
        confirmed = {p for p, s in decided.items() if s == "confirmed"}
        rejected = {p for p, s in decided.items() if s in ("rejected", "deferred")}
        _t_cluster = _time.perf_counter()
        sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                           conflict_fn=_discriminative_conflict, id_prefix="K-",
                           rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                           ann_threads=self.settings.kg_cluster_ann_threads)
        # LLM 兜底: ≥hi 的 auto_candidates 经复核确认后并入 confirmed, 重聚一次
        from app.services.concept_merge_review import review_merge_candidates
        autoc = sd.get("auto_candidates", [])
        _t_mr = _time.perf_counter()
        if autoc and getattr(self.kg_llm_client, "configured", False):
            # Optional LLM adjudication is an enhancement — it must NEVER be able to
            # crash the rebuild. review_merge_candidates is already fail-open (chunked +
            # defensive parse); this outer guard covers any other unexpected error in the
            # block so the rebuild always proceeds to write the cluster map.
            try:
                cand_dicts = [{"id": f"ac{i}", "canonical_a": a, "canonical_b": b, "score": s}
                              for i, (a, b, s) in enumerate(autoc)]
                # ac{i} → canonical id 对的稳定键(续跑复用的锚)。
                id_to_key = {f"ac{i}": _pair_key(a, b) for i, (a, b, s) in enumerate(autoc)}
                # 已决(同 input_version)命中即跳过 LLM;只把未决候选发出去。
                cached = self._runtime.unified_kg.checkpoint_load(
                    notebook_id, _ck_ver, "merge_review")
                todo = [c for c in cand_dicts if id_to_key[c["id"]] not in cached]

                def _persist(chunk_decisions):
                    rows = [(id_to_key[d["candidate_id"]],
                             {"decision": d["decision"], "confidence": d["confidence"],
                              "canonical_name": d.get("canonical_name", "")})
                            for d in chunk_decisions if d.get("candidate_id") in id_to_key]
                    if rows:
                        self._runtime.unified_kg.checkpoint_put(
                            notebook_id, _ck_ver, "merge_review", rows, _now())

                new = review_merge_candidates(
                    self.kg_llm_client, todo,
                    batch_size=self.settings.kg_merge_review_batch_size,
                    max_workers=self.settings.kg_job_concurrency,
                    on_chunk=_persist,
                )
                # 合并 缓存 ∪ 新决策,按 pair_key 索引。
                decided = dict(cached)
                for d in new:
                    k = id_to_key.get(d.get("candidate_id"))
                    if k:
                        decided[k] = {"decision": d["decision"], "confidence": d["confidence"]}
                extra = set()
                for i, (a, b, s) in enumerate(autoc):
                    dec = decided.get(_pair_key(a, b))
                    if dec and dec.get("decision") == "merge" and \
                            float(dec.get("confidence", 0)) >= self.settings.kg_merge_confirm_threshold:
                        extra.add(frozenset((a[2:] if a.startswith("K-") else a,
                                            b[2:] if b.startswith("K-") else b)))
            except Exception:
                self.event_log.logger.exception(
                    "unified-KG merge-review adjudication failed for %s; proceeding without it",
                    notebook_id,
                )
                extra = set()
            if extra:
                confirmed = set(confirmed) | extra
                sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                                   conflict_fn=_discriminative_conflict, id_prefix="K-",
                                   rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                                   ann_threads=self.settings.kg_cluster_ann_threads)
            _stage(f"concept: merge-review {len(autoc)} candidates → "
                   f"{len(extra)} merged ({_time.perf_counter() - _t_mr:.1f}s)")
        _stage(f"concept: clustered {len(seeds)} seeds → "
               f"{len(set(sd['seed_to_canonical'].values()))} canonicals, "
               f"{len(sd.get('auto_candidates', []))} auto-cand "
               f"({_time.perf_counter() - _t_cluster:.1f}s)")
        seed_to_canonical = sd["seed_to_canonical"]
        desc_by_cid: Dict[str, str] = {}
        desc_sig_by_cid: Dict[str, str] = {}
        _t_desc = _time.perf_counter()
        _desc_ran = self.settings.kg_concept_desc_enabled and getattr(self.kg_llm_client, "configured", False)
        if _desc_ran:
            from app.services.prompts import concept_description_prompt, CONCEPT_DESC_SCHEMA_HINT
            # Previous descriptions + their input sigs, keyed by canonical id. DISTINCT
            # so this is bounded by #canonicals (not #members). Reuse fires only on an
            # exact sig match with a non-empty stored description → fail-safe: any
            # miss/mismatch just regenerates (worst case = old behavior).
            old_desc: Dict[str, tuple] = {}
            with self._connect() as db:
                for r in db.execute(
                    "SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
                    "FROM concept_clusters WHERE notebook_id=? AND object_type='concept'",
                    (notebook_id,)).fetchall():
                    old_desc[r["canonical_id"]] = (r["canonical_description"] or "", r["canonical_desc_sig"] or "")
            # 同 input_version 的 checkpoint(写簇前被杀留下的已完成描述)作第一优先复用源。
            try:
                desc_ckpt = self._runtime.unified_kg.checkpoint_load(
                    notebook_id, _ck_ver, "concept_desc")
            except Exception:  # noqa: BLE001 — checkpoint 读失败退化为全量重跑,绝不打断 rebuild
                self.event_log.logger.warning(
                    "concept_desc checkpoint load 失败 for %s;本轮全量重跑描述", notebook_id, exc_info=True)
                desc_ckpt = {}
            # Total members per canonical = Σ members_count over its seeds. Keep
            # only multi-member (cross-doc merged) canonicals — same cost bound as
            # the legacy `len(mids) < 2` gate, but computed from seed aggregates so
            # no 5M-row member list is materialized.
            total_by_cid: Dict[str, int] = {}
            seeds_by_cid: Dict[str, List[str]] = {}
            for s, cid in seed_to_canonical.items():
                total_by_cid[cid] = total_by_cid.get(cid, 0) + members_count.get(s, 0)
                seeds_by_cid.setdefault(cid, []).append(s)
            # PHASE 1 (serial, cheap DB): fetch quotes per multi-member canonical,
            # compute its input sig, and either reuse the cached description or
            # queue an LLM job. DB access stays in the main thread.
            work: List[tuple] = []
            for cid, total in total_by_cid.items():
                if total < 2:
                    continue   # only fuse cross-doc merged clusters (cost bound)
                cseeds = seeds_by_cid[cid]
                # Member evidence is fetched per canonical via the scratch join,
                # bounded by that canonical's member count (not the whole table).
                ph = ",".join("?" for _ in cseeds)
                with self._connect() as db:
                    erows = db.execute(
                        f"SELECT k.evidence AS evidence FROM knowledge_objects k "
                        f"JOIN kg_cluster_scratch s ON s.object_id=k.id "
                        f"WHERE s.notebook_id=? AND s.run_id=? AND s.seed IN ({ph})",
                        (notebook_id, run_id, *cseeds)).fetchall()
                quotes = []
                for er in erows:
                    for ev in json.loads(er["evidence"] or "[]"):
                        q = (ev.get("quoted_span") or "").strip()
                        if q:
                            quotes.append(q)
                # DETERMINISTIC dedup+order so the sig (and prompt) is stable across
                # rebuilds — scratch row order is otherwise unsorted.
                quotes = sorted(set(quotes))[:8]
                if not quotes:
                    continue
                name = sd["canonical_names"].get(cid, "")
                sig = _concept_desc_sig(name, quotes)
                ck = desc_ckpt.get(cid)
                if ck and ck.get("sig") == sig and ck.get("description"):
                    desc_by_cid[cid] = ck["description"]     # checkpoint 命中:复用,跳过 LLM
                    desc_sig_by_cid[cid] = sig
                    continue
                prev = old_desc.get(cid)
                if prev and prev[0] and prev[1] == sig:
                    desc_by_cid[cid] = prev[0]               # 跨 rebuild 缓存命中:复用
                    desc_sig_by_cid[cid] = sig
                    continue
                work.append((cid, name, quotes, sig))
            # PHASE 2 (parallel LLM): the chat_json round-trips are the bottleneck;
            # run them concurrently. kg_llm_client.chat_json is already invoked
            # concurrently elsewhere (build_notebook_kg), so per-call thread use is fine.
            # Resolve the client ONCE in the main thread: kg_llm_client is a property
            # keyed on the _REQUEST_USER ContextVar, which worker threads don't inherit
            # (per-user config would otherwise fall back to user-local inside the pool).
            import concurrent.futures as _cf
            desc_client = self.kg_llm_client
            def _gen(item):
                cid, name, quotes, sig = item
                block = "\n".join(f"- {q}" for q in quotes)
                try:
                    raw = desc_client.chat_json(
                        [{"role": "user", "content": concept_description_prompt(name, block)}],
                        CONCEPT_DESC_SCHEMA_HINT)
                    desc = (json.loads(raw).get("description") or "").strip()
                except Exception:
                    desc = ""
                return cid, desc, sig
            if work:
                workers = max(1, min(self.settings.kg_job_concurrency, len(work)))
                done_n = 0
                with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kg-desc") as pool:
                    _ck_buf: List[Tuple[str, dict]] = []
                    for fut in _cf.as_completed([pool.submit(_gen, it) for it in work]):
                        cid, desc, sig = fut.result()
                        done_n += 1
                        if desc:
                            desc_by_cid[cid] = desc
                            desc_sig_by_cid[cid] = sig
                            _ck_buf.append((cid, {"description": desc, "sig": sig}))
                            if len(_ck_buf) >= _DESC_CKPT_FLUSH:
                                try:
                                    self._runtime.unified_kg.checkpoint_put(
                                        notebook_id, _ck_ver, "concept_desc", _ck_buf, _now())
                                except Exception:  # noqa: BLE001 — checkpoint 写失败不打断 rebuild
                                    self.event_log.logger.warning(
                                        "concept_desc checkpoint put 失败 for %s", notebook_id, exc_info=True)
                                _ck_buf = []
                        if progress is not None:
                            try:
                                progress("concept_desc", done_n, len(work))
                            except Exception:
                                pass
                    if _ck_buf:
                        try:
                            self._runtime.unified_kg.checkpoint_put(
                                notebook_id, _ck_ver, "concept_desc", _ck_buf, _now())
                        except Exception:  # noqa: BLE001 — checkpoint 写失败不打断 rebuild
                            self.event_log.logger.warning(
                                "concept_desc checkpoint put 失败 for %s", notebook_id, exc_info=True)
        if _desc_ran:
            _stage(f"concept: descriptions {len(desc_by_cid)} "
                   f"({_time.perf_counter() - _t_desc:.1f}s)")
        else:
            _stage("concept: descriptions skipped")
        _t = _time.perf_counter()
        self._write_cluster_map_streamed(notebook_id, "concept", seed_to_canonical,
                                         sd["canonical_names"], desc_by_cid,
                                         desc_sig_by_cid, run_id=run_id)
        _stage(f"concept: wrote clusters ({_time.perf_counter() - _t:.1f}s)")
        # Cross-doc exact-normalized merge for non-concept types (v1: seed-based;
        # vector near-dup remains concept-only). Each type isolated by id prefix.
        # These types carry no vectors → reps_t is empty → cluster_seeds does only
        # exact-seed grouping, matching the legacy cluster_objects(tobjs, {}, ...).
        # compute_reps=False skips Pass B entirely (no embeddings join) since
        # cluster_seeds receives {} anyway — avoids wasted ANN at scale.
        _TYPE_MERGE = {"claim": (seed_claim, "KL-"),
                       "formula": (seed_formula, "KF-"),
                       "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPE_MERGE.items():
            _t = _time.perf_counter()
            reps_t, mc_t, sfn_t = self._stream_seed_reps(notebook_id, t, sfn,
                                                          run_id=run_id,
                                                          compute_reps=False)
            # Empty type → scratch empty → _write_cluster_map_streamed clears rows
            # (same as legacy write_clusters([], object_type=t)).
            sd_t = cluster_seeds(sorted(mc_t), reps_t, mc_t, sfn_t, set(), set(),
                                 conflict_fn=None, id_prefix=prefix,
                                 rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                                 ann_threads=self.settings.kg_cluster_ann_threads)
            self._write_cluster_map_streamed(notebook_id, t, sd_t["seed_to_canonical"],
                                             sd_t["canonical_names"], run_id=run_id)
            _stage(f"{t}: streamed+clustered+wrote ({_time.perf_counter() - _t:.1f}s)")
        # Refresh pending candidates in ONE transaction (per-candidate inserts
        # were the rebuild hotspot at scale). confirmed/rejected rows untouched.
        now = _now()
        _t = _time.perf_counter()
        with self._write() as db:
            self._runtime.governance.delete_pending_merges(db, notebook_id)
            self._runtime.governance.insert_pending_merge_rows(
                db,
                [(_new_id("mc"), notebook_id, ca, cb, sa, sb, score, now, now)
                 for sa, sb, ca, cb, score in sd["pending_seeds"]])
        _stage(f"pending refresh ({_time.perf_counter() - _t:.1f}s)")
        self._invalidate_unified_cache(notebook_id)
        # #distinct concept canonicals (== set of cluster_map values in the legacy
        # path: every canonical has ≥1 seed and every concept seed has a canonical).
        cluster_count = len(set(seed_to_canonical.values()))
        with self._write() as db:
            # CRITICAL: the store's finish_rebuild_state UPSERT stores
            # cluster_input_version=_ver (captured at ENTRY, reflecting the seq
            # this rebuild consumed) and clears dirty=0, but MUST NOT touch
            # kg_mutation_seq — the column is omitted from both the column list
            # and the SET so an existing row's counter is PRESERVED. Bumping it
            # here would advance the version past what was just stored (gate
            # never skips); resetting it would lose mutations that arrived
            # mid-rebuild.
            self._runtime.unified_kg.finish_rebuild_state(
                db, notebook_id, _ver, cluster_count, now
            )
        # Final cleanup: drop only THIS run's scratch rows (run_id-scoped so a
        # concurrent rebuild with a different run_id is unaffected).
        with self._write() as db:
            self._runtime.unified_kg.clear_scratch_run(db, notebook_id, run_id)
        _stage(f"DONE {cluster_count} canonicals, total {_time.perf_counter() - _t_total:.1f}s")
        # canonical 关系层(派生):聚类刚重算 → force=True(闸可能误跳)。fail-open。
        try:
            self.rebuild_canonical_relations(notebook_id, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        # 共提桥接层(派生):聚类刚重算 → force=True(闸可能误跳)。fail-open。
        try:
            self.rebuild_mention_bridge(notebook_id, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "mention_bridge_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        # Proactively refresh the viz-only index so the next KG-view open doesn't
        # pay a lazy build (and to cover same-second in-place edits the version
        # tuple can miss). Fail-open: a viz build error must never break rebuild.
        try:
            self.build_viz_index(notebook_id)
        except Exception:
            self.event_log.logger.warning(
                "build_viz_index failed after rebuild for %s", notebook_id, exc_info=True)
        # rebuild 后检索索引必然 stale(clusters/objects 已变)—— 大库自动重建/入队。
        # maybe_auto_index 自身 fail-open,这里再包一层只是双保险。
        try:
            self.maybe_auto_index(notebook_id)
        except Exception:
            self.event_log.logger.exception("maybe_auto_index failed after rebuild for %s", notebook_id)
        # 社区层:聚类刚重算 → force=True 强制重建社区(clustering 未必 bump
        # kg_mutation_seq,版本闸可能误跳)。纯图、无 LLM、fail-open——绝不拖垮 KG 重建。
        try:
            self.rebuild_communities(notebook_id, level=0, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "communities_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
        return cluster_count

    def rebuild_canonical_relations(self, notebook_id: str, force: bool = False) -> int:
        """把 knowledge_relations 端点经 concept_clusters 折叠到 canonical 空间,
        按 (canonical_src, edge_type, canonical_tgt) 聚合 support_count(原始行数)/
        source_count(非NULL source_id 去重,下限1)/sample_relation_ids(cap 5),全量
        重写 canonical_relations。方向保留;rejected 边排除;折叠后自环丢弃。

        seq 闸(同 rebuild_communities):canonical_rel_seq==kg_mutation_seq 且表非空
        → 跳过(force 绕过);重写后把入口捕获的 seq 写回。返回写入行数(跳过时返回
        现有行数)。派生数据,fail-open 由调用方负责。"""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            st = self._runtime.unified_kg.state_row(db, notebook_id)
            cnt = self._runtime.unified_kg.canonical_relations_count(db, notebook_id)
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["canonical_rel_seq"] == seq and cnt > 0):
            return int(cnt)
        agg: Dict[tuple, dict] = {}
        with self._connect() as db:
            cur = db.execute(
                "SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
                "       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
                "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
                "FROM knowledge_relations kr "
                "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
                "  AND cs.member_object_id=kr.source_object_id "
                "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
                "  AND ct.member_object_id=kr.target_object_id "
                "WHERE kr.notebook_id=? AND kr.review_status!='rejected'",
                (notebook_id,))
            for r in cur:
                s, t = r["s"], r["t"]
                if not s or not t or s == t:
                    continue
                key = (s, r["et"], t)
                ent = agg.get(key)
                if ent is None:
                    ent = agg[key] = {"n": 0, "docs": set(), "samples": []}
                ent["n"] += 1
                if r["src_doc"]:
                    ent["docs"].add(r["src_doc"])
                if len(ent["samples"]) < 5:
                    ent["samples"].append(r["rid"])
        now = _now()
        rows = [(notebook_id, s, et, t, ent["n"], max(1, len(ent["docs"])),
                 json.dumps(ent["samples"]), now)
                for (s, et, t), ent in agg.items()]
        with self._write() as db:
            self._runtime.unified_kg.replace_canonical_relations(
                db, notebook_id, rows, seq
            )
        return len(rows)

    def rebuild_mention_bridge(self, notebook_id: str, force: bool = False) -> int:
        """从 claim 文本确定性提取「claim→跨源概念」mention 边与概念共提对(零 LLM)。

        流程:跨 >=2 源的 concept 簇 → 别名表(mention_scan.build_alias_table)→
        连接私有 TEMP trigram FTS(claim 折叠名;纯内存零 WAL,不占全局写锁)→
        每别名 phrase MATCH 召回
        候选 → 统一 alnum-lookaround 边界(boundary_hit)后校验 → DF 双门(命中
        claim 数超 max(floor, cap×claims)的泛词整体丢弃 + 事件计数)→ 聚合
        claim→{canonical} 全量重写 mention_edges + 两两组合(a<b)concept_comentions。

        seq 闸同 rebuild_canonical_relations(mention_seq==kg_mutation_seq 且表非空
        → 跳过,force 绕过;重写后写回入口捕获的 seq)。flag 关时清表返回 0。
        文本/别名一律 NFKC 折叠 + lower(与 build_alias_table 的别名折叠对齐,全/半角
        互通)。派生数据,fail-open 由调用方负责。返回写入的 mention_edges 行数。"""
        self.get_notebook(notebook_id)
        if not self.settings.mention_bridge_enabled:
            with self._write() as db:
                self._runtime.unified_kg.clear_mention_bridge(db, notebook_id)
            return 0
        # seq 闸(照抄 rebuild_canonical_relations,列名换 mention_seq)。
        with self._connect() as db:
            st = self._runtime.unified_kg.state_row(db, notebook_id)
            cnt = self._runtime.unified_kg.mention_edges_count(db, notebook_id)
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["mention_seq"] == seq and cnt > 0):
            return int(cnt)

        import unicodedata as _ud
        from itertools import combinations as _combinations
        from app.services.kg.mention_scan import build_alias_table, boundary_hit

        def _fold(t: str) -> str:
            return _ud.normalize("NFKC", t or "").lower()

        # 1) 跨源 concept 簇(成员横跨 >=2 个非空 source_id)。claim 簇(KL- 前缀)
        #    是被扫描的文本、不作别名目标,故按 object_type='concept' 过滤。
        clusters: Dict[str, dict] = {}
        with self._connect() as db:
            cur = db.execute(
                "SELECT cc.canonical_id AS cid, cc.canonical_name AS cname, ko.source_id AS src "
                "FROM concept_clusters cc "
                "JOIN knowledge_objects ko ON ko.id=cc.member_object_id "
                "WHERE cc.notebook_id=? AND cc.object_type='concept'",
                (notebook_id,))
            for r in cur:
                ent = clusters.setdefault(r["cid"], {"name": r["cname"], "srcs": set()})
                if r["src"]:
                    ent["srcs"].add(r["src"])
            # 2) claim 集合(text = payload.name;NFKC+lower 折叠;len<10 跳过)。
            claim_rows = db.execute(
                "SELECT id, json_extract(payload,'$.name') AS nm FROM knowledge_objects "
                "WHERE notebook_id=? AND object_type='claim' AND status!='deprecated'",
                (notebook_id,)).fetchall()
        cross = [(cid, ent["name"]) for cid, ent in clusters.items() if len(ent["srcs"]) >= 2]
        claims: List[Tuple[str, str]] = []
        for r in claim_rows:
            folded = _fold(r["nm"])
            if len(folded) < 10:
                continue
            claims.append((r["id"], folded))
        alias_table = build_alias_table(cross)   # {canonical_id: {alias,...}}(已 NFKC+lower)
        # alias -> {canonical_id,...}(同一 alias 理论上可属多个 canonical)。
        alias_to_canons: Dict[str, Set[str]] = {}
        for cid, aliases in alias_table.items():
            for a in aliases:
                alias_to_canons.setdefault(a, set()).add(cid)

        claim_hits: Dict[str, Dict[str, str]] = {}   # claim_id -> {canonical_id: matched_alias}
        dropped = 0
        n_claims = len(claims)
        if cross and claims and alias_to_canons:
            df_gate = max(self.settings.mention_alias_df_floor,
                          self.settings.mention_alias_df_cap * n_claims)
            rowid_map = {i: claims[i - 1] for i in range(1, n_claims + 1)}
            # 3) 连接私有 TEMP trigram FTS(建+填+查同一 _connect 连接):temp schema
            #    按连接隔离——并发 rebuild 同名不相撞,无需串行化;temp_store=MEMORY
            #    (见 _connect)→ 全程纯内存,零 WAL 写入、不占 _write_lock(效率约束:
            #    部署规模 ~40万 claims 的插入+扫描绝不能挡住 ingest/其它 rebuild)。
            #    连接关闭即整表蒸发(无需 DELETE/DROP;finally close 兼释放内存)。
            #    trigram=子串语义,故每候选仍须过 boundary_hit 后校验。
            scan_db = self._connect()
            try:
                scan_db.execute("CREATE VIRTUAL TABLE temp.mention_scan_fts "
                                "USING fts5(text, tokenize='trigram')")
                scan_db.executemany(
                    "INSERT INTO temp.mention_scan_fts(rowid, text) VALUES (?,?)",
                    [(i, folded) for i, (_cid, folded) in enumerate(claims, 1)])
                # 4) 每别名 phrase MATCH 召回 → boundary_hit 校验 → DF 双门。
                for alias in sorted(alias_to_canons):
                    if len(alias) < 3:     # trigram 最短查询=3;别名门已保证,双保险
                        continue
                    canons = alias_to_canons[alias]
                    match_expr = '"' + alias.replace('"', '""') + '"'
                    hits = []
                    for row in scan_db.execute(
                            "SELECT rowid FROM temp.mention_scan_fts "
                            "WHERE mention_scan_fts MATCH ?",
                            (match_expr,)):
                        claim_id, folded = rowid_map[row["rowid"]]
                        if boundary_hit(alias, folded):
                            hits.append(claim_id)
                    if len(hits) > df_gate:    # 泛词:整体丢弃 + 计数
                        dropped += 1
                        continue
                    for claim_id in hits:
                        d = claim_hits.setdefault(claim_id, {})
                        for c in canons:
                            d.setdefault(c, alias)   # 同 canonical 多别名命中只记首个
            finally:
                scan_db.close()
        if dropped > 0:
            self.event_log.emit({"kind": "mention_alias_df_dropped",
                                 "notebook_id": notebook_id, "dropped": dropped})

        # 5) 聚合:mention_edges(每 claim×命中 canonical 一行);concept_comentions
        #    按 claim 去重后两两组合(a<b)累计 bridge_claims。全量重写 + seq 写回。
        edges = [(notebook_id, claim_id, canon, alias)
                 for claim_id, canon_map in claim_hits.items()
                 for canon, alias in canon_map.items()]
        comention: Dict[Tuple[str, str], int] = {}
        for canon_map in claim_hits.values():
            for a, b in _combinations(sorted(canon_map), 2):
                comention[(a, b)] = comention.get((a, b), 0) + 1
        cm_rows = [(notebook_id, a, b, n) for (a, b), n in comention.items()]
        with self._write() as db:
            self._runtime.unified_kg.replace_mention_bridge(
                db, notebook_id, edges, cm_rows, seq
            )
        return len(edges)

    def rebuild_communities(self, notebook_id: str, level: int = 0, force: bool = False) -> int:
        """在 canonical 实体图(关系两端经 cluster_map 映射)上跑 Louvain 社区检测,
        持久化到 communities + community_members(反向索引,存 canonical_name/centrality)。
        无 LLM、确定性(seed=42)。后端:igraph(装了即用,整数边表+C 核,10^6–10^7 边
        内存/耗时有界)优先,缺失回退 networkx;仅 networkx 回退且大库(scale-tier)无 CSR
        时拒(emit community_build_refused 返回 0,避免 OOM)。返回入库社区数。

        为何喂 canonical 图:裸 knowledge_relations 逐篇封闭(每篇的同名实体是不同
        object_id)→ 社区不跨文档。经 cluster_map 把两端映射到 canonical_id 后,不同
        文档里同一 canonical 天然合并,社区才跨文档(对比/广度题需要的"兄弟"结构)。"""
        self.get_notebook(notebook_id)
        if not self.settings.community_layer_enabled:
            return 0
        # 版本闸(增量):社区已按当前 kg_mutation_seq 建过且非空 → 跳过(除非 force)。
        # 让「刷新图谱」等重复触发在 KG 未变时秒级 no-op;首次(community_seq=-1)或 KG
        # 变动后(seq 不匹配)才重跑。无 unified_kg_state 行 → _st=None → 不跳过(安全兜底)。
        with self._connect() as _db:
            _st = self._runtime.unified_kg.state_row(_db, notebook_id)
            _cnt = self._runtime.unified_kg.communities_count(_db, notebook_id, level)
        _seq = int(_st["kg_mutation_seq"]) if _st else 0
        if (not force and _st is not None and _st["community_seq"] == _seq
                and _cnt and _cnt["c"] > 0):
            return int(_cnt["c"])
        # 社区检测后端:igraph(整数边表 + C 核,10^6–10^7 边内存/耗时有界)优先;
        # 缺失(未装)才回退 networkx(纯 Python dict-of-dicts,大库会 OOM)。
        try:
            import igraph as _ig
        except Exception:
            _ig = None
        # 大库守卫:仅在 networkx 回退(无 igraph)时才拒 scale-tier 无 CSR(避免 OOM)。
        # igraph 路径整数边表 + C 核,10^7 边安全,无需 CSR、不拒。
        if (_ig is None
                and not self.notebook_copy_stats(notebook_id)["copyable"]
                and self._scale_index(notebook_id, allow_stale=True) is None):
            self.event_log.emit({"kind": "community_build_refused",
                                 "notebook_id": notebook_id, "reason": "no_scale_index"})
            return 0
        # canonical 整数边图:SQL-join 把关系两端映射到 canonical(未聚类→自身 object_id),
        # 整数索引累加边权。避开 networkx dict-of-dicts 与全量 cluster_map dict → 10^7 边
        # 内存有界(concept_clusters.member_object_id 有索引,join 走索引)。
        can2idx: "Dict[str, int]" = {}
        ew: "Dict[tuple, int]" = {}
        with self._connect() as db:
            names = {r["canonical_id"]: r["canonical_name"] for r in db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters WHERE notebook_id=?",
                (notebook_id,))}
            for r in db.execute(
                    "SELECT COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
                    "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
                    "FROM knowledge_relations kr "
                    "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
                    "AND cs.member_object_id=kr.source_object_id "
                    "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
                    "AND ct.member_object_id=kr.target_object_id "
                    "WHERE kr.notebook_id=?", (notebook_id,)):
                s, t = r["s"], r["t"]
                if not s or not t or s == t:
                    continue
                si = can2idx.setdefault(s, len(can2idx))
                ti = can2idx.setdefault(t, len(can2idx))
                key = (si, ti) if si < ti else (ti, si)
                ew[key] = ew.get(key, 0) + 1
        idx2can = [""] * len(can2idx)
        for _c, _i in can2idx.items():
            idx2can[_i] = _c
        n_nodes = len(can2idx)
        # 社区检测 + 度中心度(deg: canonical -> degree)。comms: list[list[canonical]]。
        comms: "List[List[str]]" = []
        deg: "Dict[str, float]" = {}
        if n_nodes:
            edge_list = list(ew.keys())
            if _ig is not None:
                import random as _random
                _random.seed(42)
                try:
                    _ig.set_random_number_generator(_random)   # 确定性(seed=42)
                except Exception:
                    pass
                G = _ig.Graph(n=n_nodes, edges=edge_list)
                G.es["weight"] = list(ew.values())
                membership = G.community_multilevel(weights="weight").membership
                degs = G.degree()
                buckets: "Dict[int, List[str]]" = {}
                for _i, _m in enumerate(membership):
                    buckets.setdefault(_m, []).append(idx2can[_i])
                    deg[idx2can[_i]] = float(degs[_i])
                comms = list(buckets.values())
            else:
                import networkx as nx
                from networkx.algorithms.community import louvain_communities
                g = nx.Graph()
                for (_a, _b), _w in ew.items():
                    g.add_edge(_a, _b, weight=_w)
                comms = [[idx2can[_i] for _i in c]
                         for c in louvain_communities(g, weight="weight", seed=42)]
                for _i, _d in g.degree():
                    deg[idx2can[_i]] = float(_d)
        now = _now()
        min_size = self.settings.community_min_size
        # Policy (min-size filter + id minting + member ordering) stays here;
        # the store owns the two-table full rewrite.
        kept_rows = [(_new_id("cm"), sorted(comm))
                     for comm in comms if len(comm) >= min_size]
        kept = len(kept_rows)
        with self._write() as db:
            self._runtime.unified_kg.replace_communities(
                db, notebook_id, level, kept_rows, names, deg, now
            )
        # 记版本:社区已按 _seq 建好(无 unified_kg_state 行则 UPDATE no-op,下次仍重建)。
        with self._write() as db:
            self._runtime.unified_kg.set_community_seq(db, notebook_id, _seq)
        self.event_log.emit({"kind": "communities_rebuilt", "notebook_id": notebook_id,
                             "level": level, "communities": kept, "nodes": n_nodes})
        return kept

    def list_communities(self, notebook_id: str, level: int = 0) -> List[List[str]]:
        """Member-id lists of each detected community (for summaries / global search)."""
        with self._connect() as db:
            return self._runtime.unified_kg.community_member_ids(db, notebook_id, level)

    def summarize_communities(self, notebook_id: str, level: int = 0) -> int:
        """For each detected community, generate an LLM report (title/summary/
        findings) from its members + internal relations; persist on the community
        row. No-op (returns 0) when disabled or LLM unconfigured. Returns the
        number of communities summarized."""
        self.get_notebook(notebook_id)
        if not self.settings.kg_community_summary_enabled or not getattr(self.kg_llm_client, "configured", False):
            return 0
        from app.services.prompts import community_report_prompt, COMMUNITY_REPORT_SCHEMA_HINT
        with self._connect() as db:
            crows = self._runtime.unified_kg.community_rows_for_summary(
                db, notebook_id, level)
        done = 0
        for cr in crows:
            members = json.loads(cr["member_ids"] or "[]")
            if not members:
                continue
            ph = ",".join("?" for _ in members)
            with self._connect() as db:
                orows = db.execute(
                    f"SELECT id, object_type, payload FROM knowledge_objects WHERE id IN ({ph})", members).fetchall()
                rrows = db.execute(
                    f"SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
                    f"WHERE notebook_id=? AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                    [notebook_id, *members, *members]).fetchall()
            name_by_id = {}
            mlines = []
            for o in orows:
                nm = json.loads(o["payload"] or "{}").get("name", "")
                name_by_id[o["id"]] = nm
                mlines.append(f"- [{o['object_type']}] {nm}")
            rlines = [f"{name_by_id.get(r['source_object_id'],'?')} -[{r['edge_type']}]-> {name_by_id.get(r['target_object_id'],'?')}"
                      for r in rrows]
            members_block = "\n".join(mlines)
            relations_block = "\n".join(rlines) if rlines else "(none)"
            try:
                raw = self.kg_llm_client.chat_json(
                    [{"role": "user", "content": community_report_prompt(members_block, relations_block)}],
                    COMMUNITY_REPORT_SCHEMA_HINT)
                data = json.loads(raw)
            except Exception:
                continue
            title = str(data.get("title", "")).strip()
            summary = str(data.get("summary", "")).strip()
            findings = data.get("findings") if isinstance(data.get("findings"), list) else []
            if not summary:
                continue
            with self._write() as db:
                self._runtime.unified_kg.set_community_summary(
                    db, cr["id"], title, summary, json.dumps(findings))
            done += 1
        return done

    def get_community_reports(self, notebook_id: str, level: int = 0) -> List[dict]:
        """Persisted community reports (only those summarized). For global search."""
        with self._connect() as db:
            return self._runtime.unified_kg.community_reports(db, notebook_id, level)

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

    @staticmethod
    def _promotion_row_to_dict(row: sqlite3.Row, *, payload=None, evidence=None) -> dict:
        """Map a promotion_candidates row to the PromotionCandidate-shaped dict.
        payload/evidence are denormalised from knowledge_objects when listing."""
        return {
            "id": row["id"],
            "notebook_id": row["notebook_id"],
            "object_id": row["object_id"],
            "object_type": row["object_type"],
            "status": row["status"],
            "reason": row["reason"],
            "reviewed_by": row["reviewed_by"],
            "base_match_id": row["base_match_id"],
            "created_at": row["created_at"],
            "payload": payload if payload is not None else {},
            "evidence": evidence if evidence is not None else [],
        }

    def propose_promotion(self, notebook_id: str, object_id: str) -> dict:
        """Propose a personal-KG object for promotion into the base corpus.

        Idempotent for an already-active proposal of the same object. Raises
        KeyError if the notebook or object is missing; ValueError if the
        notebook is itself a base notebook (use the review gate there instead).
        """
        self.get_notebook(notebook_id)  # KeyError if notebook missing
        now = _now()
        with self._write() as db:
            obj = db.execute(
                "SELECT object_type FROM knowledge_objects WHERE id=? AND notebook_id=?",
                (object_id, notebook_id),
            ).fetchone()
            if obj is None:
                raise KeyError(object_id)
            nb_row = db.execute(
                "SELECT tier FROM notebooks WHERE id=?", (notebook_id,)
            ).fetchone()
            if nb_row and nb_row["tier"] == "base":
                raise ValueError("cannot propose from a base notebook — use the review gate")
            # Idempotency: return any active (non-approved, non-rejected) proposal.
            existing = self._runtime.governance.active_promotion_for_object(db, object_id)
            if existing is not None:
                return self._promotion_row_to_dict(existing)
            cand_id = _new_id("promo")
            self._runtime.governance.insert_promotion_candidate(
                db, cand_id, notebook_id, object_id, obj["object_type"], now
            )
            row = self._runtime.governance.promotion_candidate_row(db, cand_id)
        return self._promotion_row_to_dict(row)

    def list_promotion_queue(self, status_filter: Optional[str] = None) -> List[dict]:
        """List promotion candidates across all notebooks (the curator sees
        everything). Defaults to the active queue (proposed + under_review);
        pass status_filter to view a single status. Denormalises payload +
        evidence from knowledge_objects for display.

        Batched (house pattern, see _hydrate_search_hits): one `id IN (...)`
        knowledge_objects lookup for the whole queue instead of a per-row
        SELECT — was N+1 (one round-trip per candidate)."""
        with self._connect() as db:
            rows = self._runtime.governance.promotion_queue_rows(db, status_filter)
            object_ids = list(dict.fromkeys(r["object_id"] for r in rows))
            obj_by_id: Dict[str, sqlite3.Row] = {}
            for i in range(0, len(object_ids), self._IN_CHUNK):
                batch = object_ids[i:i + self._IN_CHUNK]
                ph = ",".join("?" for _ in batch)
                for r in db.execute(
                    f"SELECT id, payload, evidence FROM knowledge_objects WHERE id IN ({ph})",
                    batch,
                ).fetchall():
                    obj_by_id[r["id"]] = r
            out: List[dict] = []
            for row in rows:
                obj = obj_by_id.get(row["object_id"])
                payload = json.loads(obj["payload"] or "{}") if obj else {}
                evidence = (
                    [Evidence(**e) for e in json.loads(obj["evidence"] or "[]")]
                    if obj
                    else []
                )
                out.append(
                    self._promotion_row_to_dict(row, payload=payload, evidence=evidence)
                )
        return out

    def approve_promotion(self, candidate_id: str) -> dict:
        """Approve a promotion: copy the personal object into the base corpus,
        deduplicating against existing base objects of the same type via the
        kg_merge seed clustering. Idempotent. Raises KeyError if the candidate
        is missing; ValueError if it is rejected or there is no base notebook.
        """
        now = _now()
        with self._write() as db:
            cand = self._runtime.governance.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "rejected":
                raise ValueError("cannot approve a rejected promotion candidate")
            was_approved = cand["status"] == "approved"
            src_payload = (
                json.loads(
                    (db.execute(
                        "SELECT payload FROM knowledge_objects WHERE id=?",
                        (cand["object_id"],)).fetchone() or {"payload": None})["payload"]
                    or "{}"
                )
                if not was_approved
                else {}
            )
            approval = self._runtime.governance.approve_promotion_in_transaction(
                db, candidate_id, now
            )
            # Idempotency: an already-approved candidate returns the existing
            # base object with NO post-commit hooks — exactly the old
            # early-return-inside-the-transaction behavior.
            if was_approved:
                return {
                    "candidate_id": candidate_id,
                    "base_object_id": approval.base_object_id,
                    "merged_into": cand["base_match_id"] or "",
                }

        # Embed the new base object's payload (best-effort; outside the txn so a
        # failing embedder never blocks approval). Only for freshly-inserted ones.
        if approval.created_new_object:
            self._embed_knowledge(
                approval.base_object_id, approval.base_notebook_id, src_payload
            )
        self._invalidate_unified_cache(approval.base_notebook_id)
        self._mark_unified_kg_dirty(approval.base_notebook_id)
        return {
            "candidate_id": candidate_id,
            "base_object_id": approval.base_object_id,
            "merged_into": "" if approval.created_new_object else approval.base_object_id,
        }

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
        """Reject a promotion candidate. The personal object is left untouched.
        Raises KeyError if missing; ValueError if already approved."""
        now = _now()
        with self._write() as db:
            cand = self._runtime.governance.promotion_candidate_row(db, candidate_id)
            if cand is None:
                raise KeyError(candidate_id)
            if cand["status"] == "approved":
                raise ValueError("cannot reject an approved promotion candidate")
            self._runtime.governance.set_promotion_rejected(
                db, candidate_id, reason, now
            )
            row = self._runtime.governance.promotion_candidate_row(db, candidate_id)
        return self._promotion_row_to_dict(row)

    def update_knowledge(
        self, notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate
    ) -> RuleCard:
        now = _now()
        with self._write() as db:
            row = self._runtime.governance.update_object_in_transaction(
                db, notebook_id, knowledge_id, payload, now
            )
        # WS4: re-embed payload-level vector when the payload was edited.
        if payload.payload is not None:
            try:
                self._embed_knowledge(
                    knowledge_id, row["notebook_id"], json.loads(row["payload"] or "{}")
                )
            except Exception:
                pass
        self._invalidate_unified_cache(row["notebook_id"])
        # A node edit is a clustering input: a payload/name change moves its
        # normalized-name seed (→ cross-doc cluster membership), a re-embed changes
        # its ANN vector, and a status flip changes which objects are clustered.
        # Mark dirty so kg_mutation_seq advances and rebuild_unified_kg's skip gate
        # can't serve a stale clustering after an in-place rename/re-embed.
        self._mark_unified_kg_dirty(row["notebook_id"])
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        return self._rule_card(item)

    @staticmethod
    def _knowledge_headline(object_type: str, payload: dict) -> str:
        keys = {
            "rule": ("title", "statement"),
            "method": ("name", "use_when"),
            "risk": ("title", "description"),
            "glossary": ("term", "definition"),
            "case": ("symptom", "context"),
            "checklist": ("question",),
            # KG node types: text lives in payload["name"]
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

    def _knowledge_ref(self, obj: dict, object_type: str) -> KnowledgeRef:
        return KnowledgeRef(
            id=obj["id"],
            object_type=object_type,
            headline=self._knowledge_headline(object_type, obj["payload"]),
            status=obj.get("status", "approved"),
        )

    @staticmethod
    def _payload_join(payload: dict) -> str:
        parts: List[str] = []
        for key, value in payload.items():
            if str(key).startswith("_"):
                continue
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
        return " ".join(parts)

    def _knowledge_similarity(self, a: dict, b: dict, element_vectors: dict) -> float:
        text_a = self._payload_join(a["payload"])
        text_b = self._payload_join(b["payload"])
        keyword = max(keyword_score(text_a, text_b), keyword_score(text_b, text_a))
        semantic = 0.0
        vecs_a = [element_vectors[e.element_id] for e in a["evidence"] if e.element_id in element_vectors]
        vecs_b = [element_vectors[e.element_id] for e in b["evidence"] if e.element_id in element_vectors]
        for va in vecs_a:
            for vb in vecs_b:
                semantic = max(semantic, cosine(va, vb))
        return max(keyword, semantic * 0.95)

    def find_duplicates(self, notebook_id: str, object_type: str) -> List[DuplicateGroup]:
        """Near-duplicate detection by normalized-seed BLOCKING — the same seed the
        KG clustering uses (name/statement/formula normalization + acronym alias).
        Only objects that share a seed are compared, so this is O(N + Σ block²)
        instead of the old O(N²) all-pairs — which also loaded EVERY element vector
        of the notebook into memory and froze 查重 at 10^5+ objects. The ≥0.6 grouping
        is preserved, just scoped to each (tiny) same-seed block; keyword overlap only
        (no vectors are loaded — nothing scales with the notebook's embedding count).
        Cross-seed *semantic* near-dups (different names, similar meaning) are out of
        scope here; the clustering / emb_synonym pass merges those on KG rebuild."""
        from app.services.kg_merge import (
            build_acronym_alias_map, _seed_with_alias,
            seed_concept, seed_claim, seed_formula, seed_procedure,
        )
        seed_fn = {
            "concept": seed_concept, "claim": seed_claim,
            "formula": seed_formula, "procedure": seed_procedure,
        }.get(object_type, seed_concept)

        self.get_notebook(notebook_id)
        with self._connect() as db:
            objs = self._knowledge_objects(db, notebook_id, object_type, statuses=None)
        objs = [o for o in objs if o.get("status") != "deprecated"]

        # Block by seed: only same-normalized-name objects become candidates.
        alias_map = build_acronym_alias_map(o["payload"].get("name", "") for o in objs)
        by_seed: Dict[str, List[dict]] = {}
        for o in objs:
            seed = _seed_with_alias(
                {"name": o["payload"].get("name", ""), "payload": o["payload"]},
                seed_fn, alias_map)
            if seed:
                by_seed.setdefault(seed, []).append(o)

        groups: List[DuplicateGroup] = []
        for members in by_seed.values():
            if len(members) < 2:
                continue
            # Same seed = same normalized name/statement = the duplicate signal
            # (consistent with how the KG clustering merges variants, incl. case /
            # whitespace / acronym). similarity is a display hint only: max pairwise
            # keyword overlap within the block — capped so a pathologically large
            # same-name block stays bounded, and with {} vectors so nothing loads
            # the embedding table.
            best = 1.0
            if len(members) <= 25:
                best = 0.0
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        best = max(best, self._knowledge_similarity(members[i], members[j], {}))
            groups.append(DuplicateGroup(
                object_type=object_type,
                similarity=round(best, 3),
                members=[self._knowledge_ref(m, object_type) for m in members],
            ))
        groups.sort(key=lambda g: (-len(g.members), -g.similarity))
        return groups

    def merge_knowledge(self, notebook_id: str, source_id: str, payload: MergeRequest) -> RuleCard:
        into_id = payload.into_id
        if into_id == source_id:
            raise ValueError("cannot merge a knowledge object into itself")
        now = _now()
        with self._write() as db:
            row = self._runtime.governance.merge_objects_in_transaction(
                db, notebook_id, source_id, into_id, now
            )
        # merge deprecates one object in place (COUNT unchanged) — bump the
        # monotonic seq so _scale_index_version / _cluster_input_version fast
        # paths (keyed on kg_mutation_seq) don't miss this same-second edit.
        self._mark_unified_kg_dirty(row["notebook_id"])
        self._invalidate_unified_cache(row["notebook_id"])
        obj = {
            "id": row["id"],
            "payload": json.loads(row["payload"] or "{}"),
            "evidence": [Evidence(**item) for item in json.loads(row["evidence"] or "[]")],
            "status": row["status"],
            "owner": row["owner"],
            "last_reviewed": row["last_reviewed"] if "last_reviewed" in row.keys() else "",
        }
        item = self._as_retrieved(obj, row["object_type"])
        return self._rule_card(item)

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
        # runtime_dim 是 settings_tail 的一员(T3/R4):它经
        # _compute_scale_version_cold 流入 _scale_index_version,进而与磁盘
        # manifest.version 对照 —— 切 EMBED_RUNTIME_DIM 后旧维 scale 索引必须
        # 判 stale(而非 ANN 查询恒空)。
        from app.services.vector_index import resolve_runtime_dim
        settings_tail = (
            self.settings.ppr_variant_edge_weight,
            self.settings.ppr_emb_synonym_enabled,
            self.settings.ppr_emb_synonym_threshold,
            self.settings.ppr_emb_synonym_topk,
            resolve_runtime_dim(self.settings),
            self.settings.mention_bridge_enabled,
            self.settings.mention_edge_weight,
        )
        with self._connect() as db:
            st = db.execute(
                "SELECT kg_mutation_seq, cluster_mutation_seq, mention_seq FROM unified_kg_state "
                "WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()
            seq = int(st["kg_mutation_seq"]) if st else 0
            cseq = int(st["cluster_mutation_seq"]) if st else 0
            mseq = int(st["mention_seq"]) if (st and st["mention_seq"] is not None) else -1
        # mention_seq(P2 共提桥)折进 settings_tail:同一 unified_kg_state 单行读,零新增
        # 查询;经 settings_tail 进 memo 比较 + _compute_scale_version_cold 的 *settings_tail
        # 流入磁盘 manifest.version —— 共提桥重建(mention_edges 变)使旧 scale 索引判 stale。
        return seq, cseq, settings_tail + (mseq,)

    def _compute_scale_version_cold(self, notebook_id: str, seq: int,
                                     settings_tail: tuple) -> list:
        """冷路径:五表聚合(clusters 聚合从热路径移到这里——P0-A 后热路径只读
        unified_kg_state 单行,COUNT/MAX 只在 memo miss 时算)。version list 的
        内容与格式与 P0-A 前逐位一致(clusters 仍在第 8、9 位),磁盘
        manifest.version 兼容性不受影响。只从 _scale_index_version 的 per-nb
        单飞锁内部调用,不直接对外暴露。"""
        with self._connect() as db:
            obj_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at),'') AS ts "
                "FROM knowledge_objects WHERE notebook_id=?", (notebook_id,)).fetchone()
            rel_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_relations WHERE notebook_id=?", (notebook_id,)).fetchone()
            chunk_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchone()
            emb_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)).fetchone()
        return [
            notebook_id,
            int(obj_ver["c"]), obj_ver["ts"],
            int(rel_ver["c"]), rel_ver["ts"],
            int(chunk_ver["c"]), chunk_ver["ts"],
            int(clu_ver["c"]), clu_ver["ts"],
            int(emb_ver["c"]), emb_ver["ts"],
            *settings_tail,
        ]

    def _scale_index_version(self, notebook_id: str) -> list:
        """JSON-serializable version key for the scale index of one notebook.

        Mirrors _ppr_graph's version_parts pattern: COUNT+MAX(created_at/updated_at)
        for objects, relations, chunks, concept_clusters — all for this single notebook.

        P1-8 fast path (format-preserving memoization). This is called several
        times per query (retrieval / PPR / status) and each call used to run 5
        COUNT/MAX aggregates (10 aggregate columns). The version key FORMAT is
        unchanged, so on-disk manifest.version keeps matching — no index
        invalidation.

        P0-A change signal = TWO O(1) monotonic counters, both single-row reads
        of unified_kg_state, zero table aggregates on the hot path:
          - kg_mutation_seq (bumped by _mark_unified_kg_dirty — the single choke
            point for objects / relations / chunks / embeddings; the
            merge-knowledge and edge-review in-place edits were wired in to
            close their gaps).
          - cluster_mutation_seq (bumped by _bump_cluster_mutation_seq — the
            single choke point for concept_clusters writes: write_clusters /
            append_clusters / incremental_fuse_source's orphan sweep / the
            rebuild streamed writer _write_cluster_map_streamed).

        Why clusters needed their OWN counter instead of riding kg_mutation_seq:
        rebuild_unified_kg DELIBERATELY preserves kg_mutation_seq across a
        rebuild (its end-write omits the seq column, so _cluster_input_version
        is stable and re-running rebuild is idempotent — see that method's
        docstring). But rebuild REWRITES concept_clusters, which this version key
        must reflect (the scale index is downstream of clusters). Before P0-A
        this was covered by re-reading the real COUNT/MAX(created_at) every
        single call (2 aggregate columns, but over a concept_clusters table that
        can be millions of rows at scale); now cluster_mutation_seq carries that
        signal at O(1) instead, and the real COUNT/MAX only run in
        _compute_scale_version_cold on a memo miss. Net fast path = 1 single-row
        unified_kg_state SELECT (both seq columns) instead of 1 seq read + 2
        cluster aggregates.

        Single-flight cold path: on a memo miss (cold cache or seq/cseq/settings
        changed) this runs the FIVE-table COUNT/MAX aggregates (ten aggregate
        columns, including the cluster pair moved here from the old hot path)
        directly, so N concurrent callers for the same notebook each ran their
        own full table scan in parallel — measured 96-147s overlapping on a
        490k-object deployment when the KG page fires 3-5 concurrent requests,
        and PR#157 makes every chunk write bump kg_mutation_seq (so every upload
        re-triggers this for every concurrent viewer). Now a cold miss takes a
        per-notebook lock (VectorCache's lock-table pattern, simplified: no
        refcount eviction — the key space is #notebooks, not per-request keys)
        and double-checks the memo inside the lock, so N concurrent cold callers
        for the SAME notebook compute the five aggregates exactly once; the rest
        observe the winner's result. Loader exceptions propagate to every caller
        that raced into the cold path (the Python exception itself isn't shared
        across threads — each waiter that loses the double-check re-runs
        _compute_scale_version_cold itself after acquiring the lock, so a
        failure is retried per-caller, never cross-contaminates the memo, and a
        fixed-up retry succeeds).

        Lock ordering: _scale_ver_lock only guards structural access to the
        _scale_ver_locks table (get-or-create the per-nb Lock; entries are never
        evicted — the key space is bounded by #notebooks, unlike VectorCache's
        per-request cache keys, so an unbounded-but-notebook-scoped dict is
        acceptable); it is NEVER held while the per-nb lock is held or while the
        aggregate computation runs — so a thread can never hold the global lock
        while blocked waiting on a per-nb lock (no cycle). Audited: no existing
        caller of _scale_index_version (directly, or via _scale_index /
        _viz_index / _viz_index_probe / scale_index_status) holds _write_lock,
        _scale_building_lock, or a _vector_cache per-key lock while calling in —
        every call site is a plain read with no lock held around it (see
        callers' grep in the PR description). A loader that re-enters
        _scale_index_version for a DIFFERENT notebook while this notebook's
        per-nb lock is held is safe (different lock objects, no shared state
        touched outside the lock table); this is defensive — no current
        production path actually nests like this.
        """
        seq, cseq, settings_tail = self._probe_scale_version_signal(notebook_id)
        cached = self._scale_ver_cache.get(notebook_id)
        if (cached is not None and cached[0] == seq
                and cached[1] == cseq and cached[2] == settings_tail):
            # seq + cseq + settings unchanged → skip the five-table ten
            # aggregate-column cold path entirely (no lock, no table scans).
            return list(cached[3])

        # Cold path: get-or-create this notebook's lock (global lock held only
        # for this dict lookup/insert, never across the computation below).
        with self._scale_ver_lock:
            nb_lock = self._scale_ver_locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                self._scale_ver_locks[notebook_id] = nb_lock

        with nb_lock:
            # Double-check: another thread may have finished computing while we
            # waited for the lock. Re-probe (seq/cseq may also have moved).
            seq, cseq, settings_tail = self._probe_scale_version_signal(notebook_id)
            cached = self._scale_ver_cache.get(notebook_id)
            if (cached is not None and cached[0] == seq
                    and cached[1] == cseq and cached[2] == settings_tail):
                return list(cached[3])
            # Exceptions from here propagate uncaught (nothing cached on
            # failure); the lock releases via `with`, so a retry by this or
            # another thread re-attempts the computation cleanly.
            version = self._compute_scale_version_cold(notebook_id, seq, settings_tail)
            # Memoize keyed on (seq, cseq, settings). Store a copy so a caller
            # mutating the returned list can't corrupt the cache.
            self._scale_ver_cache[notebook_id] = (seq, cseq, settings_tail, list(version))
            return version

    def _read_manifest_version(self, out_dir: str):
        """廉价读 out_dir/manifest.json 的 version 字段(几 KB,sub-ms)。用于
        allow_stale 检索路径校验「进程缓存里的 stale 实例是否仍是当前磁盘索引」——
        磁盘索引只在 rebuild/fold 时换(新 version),与 kg_mutation_seq 无关。
        文件缺失/损坏/无 version → None(fail-soft,调用方回退到重新 load)。"""
        mpath = os.path.join(out_dir, "manifest.json")
        try:
            with open(mpath) as fh:
                return json.load(fh).get("version")
        except (OSError, ValueError):
            return None

    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        """Return a valid ScaleIndex or None.

        exact(allow_stale=False):manifest.version == 当前 DB 版本 cur 才算有效,
        否则 None——viz/status 等要求与 DB 强一致的调用方语义不变。

        allow_stale=True(检索热路径「取磁盘已索引部分」):按**磁盘索引身份**
        (manifest.json 的 version)缓存复用。磁盘索引只在 rebuild/fold 时换,与
        kg_mutation_seq(每写 bump)无关——所以摄取造成 cur 漂移时,不再每查询重建
        stale 实例 + 重载 ~10GB ANN handle,而是复用同一进程缓存实例(handle memoize
        存活)。cold-load 走 per-nb 单飞锁,防 N 个并发查询各载 8GB 造成内存尖峰。
        stale-serve 与 scale_search_include_delta 无关地正确:ANN 核=磁盘已索引部分,
        flag=ON 时 delta 新鲜度来自检索侧 ⊕delta 暴力块,不来自这个核。"""
        from app.services.kg import scale_index as si
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        cur = self._scale_index_version(notebook_id)
        cached = self._scale_idx_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        if not allow_stale:
            # version-exact:字节不变——load,manifest==cur 才 cache 并返回,否则 None。
            idx = si.load_scale_index(out_dir)
            if idx is None:
                return None
            if idx.manifest.get("version") == cur:
                self._scale_idx_cache[notebook_id] = idx
                return idx
            return None
        # allow_stale:按磁盘身份复用。cached 若仍是当前磁盘索引(其 version == 磁盘
        # manifest version)→ 直接返回(handle 存活,零重载)。
        disk_ver = self._read_manifest_version(out_dir)
        if disk_ver is None:
            return None   # 无索引
        if cached is not None and cached.manifest.get("version") == disk_ver:
            return cached
        # cold:单飞加载。全局锁只护锁表,load 在 per-nb 锁内、不持全局锁。
        with self._scale_idx_load_lock:
            nb_lock = self._scale_idx_load_locks.get(notebook_id)
            if nb_lock is None:
                nb_lock = threading.Lock()
                self._scale_idx_load_locks[notebook_id] = nb_lock
        with nb_lock:
            # double-check:等锁期间别的线程可能已加载好当前磁盘索引。
            cached = self._scale_idx_cache.get(notebook_id)
            disk_ver = self._read_manifest_version(out_dir)
            if disk_ver is None:
                return None
            if cached is not None and cached.manifest.get("version") == disk_ver:
                return cached
            idx = si.load_scale_index(out_dir)
            if idx is None:
                return None
            self._scale_idx_cache[notebook_id] = idx
            return idx

    def _open_scale_ann(self, idx, kind: str):
        """惰性 open + memoize hnswlib handle 到 ScaleIndex 实例(进程缓存,版本变→新实例→重开)。
        kind='kg'→ann.bin/ann_labels;'chunk'→chunk_ann.bin/chunk_ann_labels;
        'relation'→relation_ann.bin/relation_ann_labels。失败/无工件→None。"""
        import hnswlib
        _attr_by_kind = {"kg": "ann_handle", "chunk": "chunk_ann_handle",
                         "relation": "relation_ann_handle"}
        _path_by_kind = {"kg": "ann_path", "chunk": "chunk_ann_path",
                         "relation": "relation_ann_path"}
        _labels_by_kind = {"kg": "ann_labels", "chunk": "chunk_ann_labels",
                          "relation": "relation_ann_labels"}
        attr = _attr_by_kind[kind]
        cached = getattr(idx, attr, None)
        if cached is not None:
            return cached
        path = getattr(idx, _path_by_kind[kind], None)
        labels = getattr(idx, _labels_by_kind[kind], None)
        if not path or not labels:
            return None
        from app.services.vector_index import resolve_runtime_dim as _rrd
        dim = int(idx.manifest.get("dim", _rrd(self.settings) or self.settings.embed_dim))
        try:
            h = hnswlib.Index(space="cosine", dim=dim)
            h.load_index(path, max_elements=len(labels))
        except Exception as exc:  # noqa: BLE001 — fail-open
            self._note_model_error(f"scale_ann_open_{kind}", self.settings.embed_model, exc)
            return None
        setattr(idx, attr, h)
        return h

    def _spawn_viz_build(self, notebook_id: str) -> None:
        """Kick off a background (de-duplicated) viz-index build. Mirrors
        _run_scale_op exactly: guard-add inside the lock, discard in finally,
        exceptions only logged. build_viz_index is read-only DB + no model
        calls, so a plain daemon thread is safe (no GIL-holding native call,
        no shared mutable state beyond the cache dict it writes on success)."""
        with self._viz_building_lock:
            if notebook_id in self._viz_building:
                return
            self._viz_building.add(notebook_id)

        def _run():
            try:
                self.build_viz_index(notebook_id)
            except Exception:  # noqa: BLE001 — background task, failure only logged
                try:
                    self.event_log.logger.exception("viz index build failed for %s", notebook_id)
                except Exception:
                    pass
            finally:
                with self._viz_building_lock:
                    self._viz_building.discard(notebook_id)
        threading.Thread(target=_run, name=f"vizidx-{notebook_id}", daemon=True).start()

    def _viz_index(self, notebook_id: str):
        """Index exposing folded viz arrays for the KG-view fast paths, or None.

        Priority: (1) a valid full scale index (base library — already carries the
        viz arrays); (2) a persisted viz-only index whose version matches; (3)
        disk has a STALE viz index — serve it immediately (display staleness is
        benign) and spawn a background refresh; (4) nothing at all — small
        notebooks (≤ viz_sync_build_max_objects effective objects) still build
        synchronously (legacy behavior); large notebooks spawn a background build
        and return None (caller surfaces a "building" placeholder instead of
        blocking the request thread on a minutes-long full-graph fold)."""
        scale = self._scale_index(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            return scale
        from app.services.kg import viz_index as vi
        cur = self._scale_index_version(notebook_id)
        cached = self._viz_idx_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        idx = vi.load_viz_index(self._viz_index_dir(notebook_id))
        if idx is not None:
            if idx.manifest.get("version") == cur:
                self._viz_idx_cache[notebook_id] = idx
                return idx
            # Stale on disk: benign to serve immediately, refresh in the
            # background. Do NOT cache the stale instance as if it were fresh —
            # leaving _viz_idx_cache untouched means the version check above
            # keeps failing and we keep re-probing disk (simplest correct
            # option; mirrors _scale_index's allow_stale non-caching).
            self._spawn_viz_build(notebook_id)
            return idx
        with self._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,)).fetchone()["c"]
        if int(count) <= self.settings.viz_sync_build_max_objects:
            self.build_viz_index(notebook_id)   # sync lazy build; sets cache on success
            return self._viz_idx_cache.get(notebook_id)
        self._spawn_viz_build(notebook_id)
        return None

    def _viz_index_probe(self, notebook_id: str) -> dict:
        """Read-only viz-index status — NEVER builds. Returns
        {viz_indexed, viz_nodes, viz_edges, viz_stale}."""
        cur = self._scale_index_version(notebook_id)
        scale = self._scale_index(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            m = scale.manifest
            return {"viz_indexed": True,
                    "viz_nodes": int(m.get("n_viz_nodes", len(scale.viz_ids))),
                    "viz_edges": int(m.get("n_viz_edges", len(scale.viz_edges or []))),
                    "viz_stale": False}
        from app.services.kg import viz_index as vi
        idx = vi.load_viz_index(self._viz_index_dir(notebook_id))
        if idx is None:
            return {"viz_indexed": False, "viz_nodes": 0, "viz_edges": 0, "viz_stale": False}
        m = idx.manifest
        fresh = m.get("version") == cur
        return {"viz_indexed": fresh,
                "viz_nodes": int(m.get("n_viz_nodes", 0)),
                "viz_edges": int(m.get("n_viz_edges", 0)),
                "viz_stale": not fresh}

    def _gather_kg_graph(self, notebook_id: str, source_ids=None, synonym_edges=None,
                          as_arrays: bool = False):
        """Gather all KG nodes/relations/chunks/cluster_groups for a notebook
        and build the undirected edge set used by both build_scale_index and
        _active_kg_delta.  Single source of truth — no duplicated _add_undirected.

        source_ids : None (default) = whole notebook, byte-identical to the
        pre-scoping behaviour.  A list = only objects/relations/chunks from those
        sources (delta domain); memberships limited to gathered objects; the
        variant/synonym extra_edges are skipped (delta is small, connectivity
        comes from relations/cluster-hub/cross-layer bridges).  An empty list
        returns ([], [], [], [], {}) (as_arrays=True: ([], (empty arrays), [], [], {})).

        synonym_edges : None (default) = current behaviour — this method loads
        the KG embedding matrix itself and calls emb_synonym_edges() internally
        (whole-notebook / unscoped path only). Pass a pre-computed
        [(id_a,id_b,sim), ...] list (e.g. from build_scale_index, which already
        loaded the matrix and built the hnsw index once for both the ann.bin
        persist AND the synonym KNN) to SKIP the internal matrix load + KNN
        call entirely and merge the given edges into extra_edges instead —
        avoids doing the single most expensive build step (hnsw construction)
        twice. Only meaningful when source_ids is None; scoped/delta callers
        never pass this (variant/synonym edges are already skipped when scoped).

        as_arrays : False (default) = current behaviour, edges as a Python
        list of (str,str,float) tuples plus a `seen_undir` tuple set for
        dedup — byte-identical to before, used by every existing caller
        (_active_kg_delta, scoped/delta paths, etc). True (build_scale_index
        only) = same node_ids, but edges come back as three int32/int32/
        float32 numpy arrays (src_idx, tgt_idx, w) already encoded against
        `index = {nid: i for i, nid in enumerate(node_ids)}`, deduped via an
        encoded-pair-key np.unique instead of a Python tuple set — avoids the
        ~5GB of Python-object overhead a 10M+-edge notebook's `edges` list +
        `seen_undir` set costs. Dedup keeps the FIRST-seen weight for a given
        undirected pair, matching the string path's `if key in seen_undir:
        return` short-circuit (relations → memberships → extra_edges →
        cluster-hub insertion order, exactly as below). Both directions are
        present in the output arrays, self-loops are dropped, and cluster hub
        ids are appended to node_ids BEFORE edge assembly (so the index dict
        is complete for one array-encoding pass instead of growing mid-way).

        Returns
        -------
        (node_ids, edges, chunk_ids, kg_node_ids, membership_counts)
          node_ids          : list[str]  — kg node ids + chunk_ids + cluster hub ids
          edges             : list[(str,str,float)] — undirected (both dirs, deduped)
                               OR (as_arrays=True) (src:int32[], tgt:int32[], w:float32[])
          chunk_ids         : list[str]  — raw chunk ids (stable subset of node_ids)
          kg_node_ids       : list[str]  — KG object ids (for idf / n_kg_nodes)
          membership_counts : dict[str,int] — {object_id: len(chunks)} for IDF
        """
        from app.services.kg.ppr import variant_edge_pairs, emb_synonym_edges
        import numpy as np

        ph = ",".join("?" for _ in USABLE_STATUSES)
        scoped = source_ids is not None
        if scoped and not source_ids:
            if as_arrays:
                empty = np.empty(0, dtype=np.int32)
                return [], (empty, empty, np.empty(0, dtype=np.float32)), [], [], {}
            return [], [], [], [], {}
        clauses = [("", ())]
        if scoped:
            clauses = [
                (f" AND source_id IN ({','.join('?' for _ in b)})", tuple(b))
                for b in self._in_batches(source_ids)
            ]
        kg_nodes: Dict[str, dict] = {}
        relations: list = []
        chunk_ids: list = []
        cluster_groups: Dict[str, list] = {}

        with self._connect() as db:
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id=? AND status IN ({ph}){src_clause}",
                        (notebook_id, *USABLE_STATUSES, *src_params)).fetchall():
                    kg_nodes[r["id"]] = {
                        "type": r["object_type"],
                        "name": json.loads(r["payload"] or "{}").get("name", ""),
                    }
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=? AND review_status!='rejected'{src_clause}",
                        (notebook_id, *src_params)).fetchall():
                    relations.append(dict(r))
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id FROM chunks WHERE notebook_id=?{src_clause}",
                        (notebook_id, *src_params)).fetchall():
                    chunk_ids.append(r["id"])
            for r in db.execute(
                    "SELECT canonical_id, member_object_id FROM concept_clusters "
                    "WHERE notebook_id=?", (notebook_id,)).fetchall():
                cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])

        # Memberships: entity ↔ chunk (scoped → limit to gathered objects)
        ent_chunk_map = self._ent_chunk_map(notebook_id)
        _kg_keys = set(kg_nodes.keys())
        memberships = [(oid, cid) for oid, cids in ent_chunk_map.items()
                       if (not scoped or oid in _kg_keys) for cid in cids]
        membership_counts: Dict[str, int] = {
            oid: len(cids) for oid, cids in ent_chunk_map.items()
            if (not scoped or oid in _kg_keys)}
        del ent_chunk_map, _kg_keys

        # Extra edges: variant pairs + optional synonym pairs (whole-notebook only)
        extra_edges = []
        if not scoped:
            extra_edges = variant_edge_pairs(kg_nodes, self.settings.ppr_variant_edge_weight)
            if synonym_edges is not None:
                # Caller (build_scale_index) already computed these — reusing
                # the SAME hnsw build it also persists as ann.bin, instead of
                # this method loading the matrix + building hnsw again.
                extra_edges = extra_edges + list(synonym_edges)
            else:
                with self._connect() as db:
                    ann_ids_raw, ann_matrix_raw = self._vector_matrix(
                        db, notebook_id, "knowledge_embeddings", "object_id")
                ann_ids: list = list(ann_ids_raw) if ann_ids_raw else []
                has_vecs = bool(ann_ids) and ann_matrix_raw is not None and len(ann_matrix_raw)
                if has_vecs and self.settings.ppr_emb_synonym_enabled:
                    extra_edges = extra_edges + emb_synonym_edges(
                        ann_ids, np.asarray(ann_matrix_raw),
                        self.settings.ppr_emb_synonym_threshold,
                        self.settings.ppr_emb_synonym_topk,
                        self.settings.ppr_emb_synonym_max_entities,
                        ef_construction=self.settings.hnsw_ef_construction,
                    )
            # P2 共提桥:scale CSR 节点空间含 cluster router(下方 hub 装配),故 claim↔cluster
            # 软边同样适用;仅 whole-notebook(not scoped)路径追加,与 variant/synonym 一致。
            extra_edges = extra_edges + self._mention_extra_edges(notebook_id)

        # node_ids: kg nodes first, then chunk nodes, then cluster hubs.
        # Hubs are pre-appended HERE (before edge assembly) rather than
        # discovered while walking cluster_groups interleaved with
        # _add_undirected calls — final node_ids content/order is identical
        # either way (hub eligibility only depends on `members` vs the
        # kg+chunk node_ids_set, never on edges), but the array path needs a
        # COMPLETE id→index map before it can encode any edge, so both paths
        # share this single hub-discovery pass.
        node_ids: list = list(kg_nodes.keys()) + chunk_ids
        node_ids_set: set = set(node_ids)
        hub_members: List[Tuple[str, str]] = []  # (hub_id, member_id) pairs to wire as edges below
        for canonical_id, members in cluster_groups.items():
            present = [m for m in members if m in node_ids_set]
            if not present:
                continue
            hub_id = f"cluster:{canonical_id}"
            if hub_id not in node_ids_set:
                node_ids.append(hub_id)
                node_ids_set.add(hub_id)
            for m in present:
                hub_members.append((hub_id, m))
        del cluster_groups

        if as_arrays:
            index = {nid: i for i, nid in enumerate(node_ids)}
            n = len(node_ids)
            # Collect all directed (a,b,w) contributions in encoding order —
            # relations → memberships → extra_edges → hub_members — same
            # precedence order as the string path's _add_undirected calls,
            # so first-seen-wins dedup below picks the same winning weight.
            a_list: List[int] = []
            b_list: List[int] = []
            w_list: List[float] = []
            for rel in relations:
                sa = index.get(rel["source_object_id"])
                sb = index.get(rel["target_object_id"])
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del relations
            for oid, cid in memberships:
                sa = index.get(oid); sb = index.get(cid)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del memberships
            for a, b, w in extra_edges:
                sa = index.get(a); sb = index.get(b)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(float(w))
            del extra_edges
            for hub_id, m in hub_members:
                sa = index.get(hub_id); sb = index.get(m)
                if sa is not None and sb is not None and sa != sb:
                    a_list.append(sa); b_list.append(sb); w_list.append(1.0)
            del hub_members

            if not a_list:
                empty = np.empty(0, dtype=np.int32)
                kg_node_ids: list = list(kg_nodes.keys())
                return node_ids, (empty, empty, np.empty(0, dtype=np.float32)), chunk_ids, kg_node_ids, membership_counts

            a_arr = np.asarray(a_list, dtype=np.int64)
            b_arr = np.asarray(b_list, dtype=np.int64)
            w_arr = np.asarray(w_list, dtype=np.float64)
            del a_list, b_list, w_list
            # Undirected dedup: canonical (lo,hi) pair encoded as one int64 key
            # (lo*n + hi); np.unique(..., return_index=True) keeps the FIRST
            # occurrence of each key — matches the string path's `if key in
            # seen_undir: return` (first-wins) semantics exactly, given the
            # same relations→memberships→extra→hub encoding order above.
            lo = np.minimum(a_arr, b_arr)
            hi = np.maximum(a_arr, b_arr)
            keys = lo * n + hi
            _, first_idx = np.unique(keys, return_index=True)
            first_idx.sort()  # np.unique sorts by key value, not first-seen order — restore it
            src_u = a_arr[first_idx]
            tgt_u = b_arr[first_idx]
            w_u = w_arr[first_idx]
            del a_arr, b_arr, w_arr, lo, hi, keys, first_idx

            # Emit both directions (src->tgt and tgt->src), matching the
            # string path's edges.append((a,b,w)); edges.append((b,a,w)).
            src_final = np.concatenate([src_u, tgt_u]).astype(np.int32, copy=False)
            tgt_final = np.concatenate([tgt_u, src_u]).astype(np.int32, copy=False)
            w_final = np.concatenate([w_u, w_u]).astype(np.float32, copy=False)
            del src_u, tgt_u, w_u

            kg_node_ids = list(kg_nodes.keys())
            return node_ids, (src_final, tgt_final, w_final), chunk_ids, kg_node_ids, membership_counts

        # Build undirected edges (single _add_undirected closure, shared state)
        edges: List[Tuple[str, str, float]] = []
        seen_undir: set = set()

        def _add_undirected(a: str, b: str, w: float) -> None:
            if a == b:
                return
            key = (a, b) if a < b else (b, a)
            if key in seen_undir:
                return
            seen_undir.add(key)
            edges.append((a, b, w))
            edges.append((b, a, w))

        for rel in relations:
            _add_undirected(rel["source_object_id"], rel["target_object_id"], 1.0)
        for oid, cid in memberships:
            _add_undirected(oid, cid, 1.0)
        for a, b, w in extra_edges:
            _add_undirected(a, b, w)
        for hub_id, m in hub_members:
            _add_undirected(hub_id, m, 1.0)

        kg_node_ids: list = list(kg_nodes.keys())
        return node_ids, edges, chunk_ids, kg_node_ids, membership_counts

    def build_scale_index(self, notebook_id: str, on_stage: Optional[Callable[[str, int], None]] = None) -> dict:
        """Offline: read KG from SQLite for ONE notebook, build CSR transition +
        ANN index, write 7 files under {storage_dir}/kg_index/{notebook_id}/.
        Returns the manifest dict.

        Graph nodes: all USABLE kg-object IDs + all chunk IDs.
        Edges (undirected → both directions added):
          - relations (source↔target, weight 1.0)
          - entity↔chunk memberships (weight 1.0)
          - variant pairs (ppr_variant_edge_weight)
          - synonym pairs from embeddings (cosine weight, if emb_synonym_enabled)
        IDF[i] = 1 / membership_count for KG nodes (1.0 when 0), 1.0 for chunks.
        ANN index = only kg nodes that have a row in knowledge_embeddings.

        Perf (Task 1, 2026-07-02): the KG-embedding hnsw index used to be built
        TWICE — once inside emb_synonym_edges (for the KNN synonym-edge pass,
        discarded after) and once more here for the persisted ann.bin — hnsw
        construction is the single most expensive step in this pipeline at
        490k-object scale. Now built ONCE: load the kg matrix first, build one
        hnsw index (ef_construction configurable), derive synonym edges from it
        via emb_synonym_edges(prebuilt_index=...), feed those into
        _gather_kg_graph(synonym_edges=...) (skips its own matrix load + KNN),
        and finally hand the SAME index to save_scale_index(prebuilt_ann=...)
        which just save_index()s it (no rebuild).

        Perf (Task 2, 2026-07-02 — memory diet, real-world 490k-object build
        OOM-killed a 64GB box): four more changes here, all output-preserving:
          - Both embedding matrices (kg + chunk) load DIRECTLY via
            vector_index.build_matrix(rows, n_hint=COUNT(*)) instead of through
            _vector_matrix()/_vector_cache — a build's ~2-4GB matrices never
            enter the LRU cache (they'd just evict/outlive useful query-time
            entries and add another live copy); n_hint preallocates instead of
            the 490k-small-ndarrays-then-vstack pattern (was a 2x peak itself).
          - _gather_kg_graph(..., as_arrays=True): edges come back as int32/
            float32 numpy arrays instead of a Python (str,str,float) tuple
            list + tuple `seen_undir` set (~5GB of Python-object overhead at
            10M+ edges) — see that method's docstring for the equivalence
            argument.
          - build_transition_arrays(): CSR construction directly off those
            int arrays (no index.get() Python dict lookups per edge).
          - `del` of edge arrays / relations / memberships / ent_chunk_map as
            soon as each is consumed, plus gc.collect() between the heavy
            stages (kg matrix+ann → gather+transition → chunk matrix → viz)
            so freed numpy/hnsw memory is actually returned to the allocator
            before the next stage's peak, not just unreferenced.
          - IMPORTANT — what must stay alive: `ann_vectors` is kept alive all
            the way through save_scale_index(), because its prebuilt_ann
            fallback (misaligned/broken prebuilt hnsw handle) rebuilds the
            index from ann_vectors — dropping it early would silently corrupt
            that safety net. Everything else (edges, relations, memberships,
            ent_chunk_map, id_to_idx) is build-scoped and freed once the CSR/
            manifest fields derived from it are computed.

        `on_stage`: optional callback invoked as `(stage_name, latency_ms)`
        once per stage (the same 10 stages as the `scale_index_build` events:
        kg_matrix/ann_build/synonym/gather/transition/chunk_matrix/
        relation_matrix/viz_arrays/persist, plus a final total), right when
        that stage's timing is recorded. Lets a CLI caller (batch_ingest)
        print real-time per-stage progress on long builds without depending
        on the events logger, which
        doesn't print to the terminal. A raising callback is swallowed
        (logging-only) so it can never break the build — mirrors how
        event_log.emit isolates its own failures. Default None preserves prior
        behavior byte-for-byte (aside from the stage-name/-order changes above,
        which are observability-only and documented here).
        """
        from app.services.kg import scale_index as si
        from app.services.vector_index import build_matrix

        import gc
        import numpy as np

        # Validate notebook exists
        self.get_notebook(notebook_id)

        # Per-stage timing (observability only, no behavior change to the
        # produced artifacts): times the main internal stages and emits a
        # `scale_index_build` event per stage so a slow build on a large (e.g.
        # 490k-object) deployment can be traced to the exact bottleneck.
        # Mirrors the pipeline-stage event pattern in process_source
        # (kind/stage/status/latency_ms).
        build_started = time.perf_counter()
        timings: dict = {}

        def _notify_stage(stage_name, ms):
            if on_stage is None:
                return
            try:
                on_stage(stage_name, ms)
            except Exception:  # noqa: BLE001 — caller's callback must never break the build
                self.event_log.logger.warning(
                    "build_scale_index on_stage callback failed for stage %s", stage_name, exc_info=False)

        def _timed(stage_name, fn):
            t0 = time.perf_counter()
            out = fn()
            ms = round((time.perf_counter() - t0) * 1000)
            timings[stage_name] = ms
            self.event_log.emit({
                "kind": "scale_index_build",
                "notebook_id": notebook_id,
                "stage": stage_name,
                "status": "done",
                "latency_ms": ms,
            })
            _notify_stage(stage_name, ms)
            return out

        # ── KG embedding matrix + ONE shared hnsw build ─────────────────────
        # Loaded/built first (before gather) so the same in-memory hnswlib.Index
        # can be reused for both the synonym-edge KNN pass and the persisted
        # ann.bin — see perf note in the docstring above.
        #
        # Direct load (Task 2): bypasses _vector_matrix()/_vector_cache on
        # purpose — a build's kg/chunk matrices are multi-GB, single-use, and
        # would otherwise sit in the LRU cache outliving the build (or evict
        # useful query-time entries). n_hint=COUNT(*) lets build_matrix
        # preallocate instead of accumulating 490k+ small ndarrays pre-vstack.
        def _kg_matrix():
            with self._connect() as db:
                n_hint = db.execute(
                    "SELECT COUNT(*) AS c FROM knowledge_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
                rows = db.execute(
                    "SELECT object_id AS vid, vector FROM knowledge_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchall()
                return build_matrix(((r["vid"], r["vector"]) for r in rows), n_hint=n_hint, runtime_dim=self._runtime_dim())

        ann_ids_raw, ann_matrix_raw = _timed("kg_matrix", _kg_matrix)
        ann_ids: list = list(ann_ids_raw) if ann_ids_raw else []
        ann_matrix = ann_matrix_raw
        if ann_ids and ann_matrix is not None:
            ann_labels = ann_ids
            ann_vectors = np.asarray(ann_matrix, dtype=np.float32)
        else:
            ann_labels = []
            ann_vectors = np.empty((0, max(1, self.settings.embed_dim)), dtype=np.float32)

        def _build_kg_ann():
            # CRITICAL alignment: ann_labels IS ann_ids from the _kg_matrix()
            # load above (single source of truth — _vector_matrix returns
            # (ids, matrix) row-aligned), so labels 0..n-1 assigned here match
            # ann_labels' row order exactly, which save_scale_index later
            # verifies via get_current_count() == len(ann_labels).
            if ann_vectors.shape[0] == 0:
                return None
            import hnswlib
            idx = hnswlib.Index(space="cosine", dim=int(ann_vectors.shape[1]))
            idx.init_index(max_elements=ann_vectors.shape[0],
                           ef_construction=self.settings.hnsw_ef_construction,
                           M=16, random_seed=42)
            idx.add_items(ann_vectors, np.arange(ann_vectors.shape[0]))
            return idx

        kg_ann_index = _timed("ann_build", _build_kg_ann)
        gc.collect()  # hnsw's internal add_items copy (if any) + build scratch, before synonym/gather

        def _synonym():
            if kg_ann_index is None or not self.settings.ppr_emb_synonym_enabled:
                return []
            from app.services.kg.ppr import emb_synonym_edges
            return emb_synonym_edges(
                ann_labels, ann_vectors,
                self.settings.ppr_emb_synonym_threshold,
                self.settings.ppr_emb_synonym_topk,
                self.settings.ppr_emb_synonym_max_entities,
                prebuilt_index=kg_ann_index,
                ef_construction=self.settings.hnsw_ef_construction,
            )

        synonym_edges = _timed("synonym", _synonym)

        # as_arrays=True (Task 2): edges come back as int32/float32 numpy
        # arrays instead of a (str,str,float) tuple list — see
        # _gather_kg_graph's docstring for the equivalence argument. Only
        # build_scale_index uses this path; every other caller keeps the
        # default string-tuple path unchanged.
        node_ids, (edge_src, edge_tgt, edge_w), chunk_ids, kg_node_ids, membership_counts = \
            _timed("gather", lambda: self._gather_kg_graph(
                notebook_id, synonym_edges=synonym_edges, as_arrays=True))
        del synonym_edges

        kg_id_set = set(kg_node_ids)

        # chunk_index: indices of chunk nodes in node_ids (stable; cluster hubs
        # already appended at the end of node_ids by _gather_kg_graph)
        id_to_idx = {nid: i for i, nid in enumerate(node_ids)}
        chunk_index = [id_to_idx[cid] for cid in chunk_ids if cid in id_to_idx]

        # IDF: 1 / membership_count (1.0 when 0); chunks and hub nodes get 1.0
        idf: list = []
        for nid in node_ids:
            if nid in kg_id_set:
                cnt = membership_counts.get(nid, 0)
                idf.append(1.0 / cnt if cnt > 0 else 1.0)
            else:
                idf.append(1.0)
        del kg_id_set, membership_counts

        # Build CSR transition matrix — array fast-path (Task 2): CSR built
        # directly off the int-indexed edge arrays (no per-edge index.get()
        # Python dict round trips). id_to_idx here is recomputed inside
        # build_transition_arrays from node_ids (cheap dict comprehension);
        # not reused from the one above to keep the two call sites independent.
        transition, _ = _timed(
            "transition", lambda: si.build_transition_arrays(node_ids, edge_src, edge_tgt, edge_w))
        del edge_src, edge_tgt, edge_w
        gc.collect()  # edge arrays + CSR construction scratch, before chunk matrix load

        # Chunk-level ANN vectors/labels (Task 1): chunks that have a row in
        # chunk_embeddings. Persisted as chunk_ann.bin so query-time chunk
        # retrieval can ANN-narrow candidates on large persisted-index notebooks.
        # Direct load (Task 2): same rationale as _kg_matrix above — bypasses
        # _vector_matrix()/_vector_cache so this multi-GB matrix never becomes
        # a cache entry, and loads as late as possible (right before the ANN
        # build that consumes it) rather than living for the whole build.
        def _chunk_matrix():
            with self._connect() as db:
                n_hint = db.execute(
                    "SELECT COUNT(*) AS c FROM chunk_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
                rows = db.execute(
                    "SELECT chunk_id AS vid, vector FROM chunk_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchall()
                return build_matrix(((r["vid"], r["vector"]) for r in rows), n_hint=n_hint, runtime_dim=self._runtime_dim())

        c_ids_raw, c_mat_raw = _timed("chunk_matrix", _chunk_matrix)
        chunk_ann_labels = list(c_ids_raw) if c_ids_raw else []
        chunk_ann_vectors = (np.asarray(c_mat_raw, dtype=np.float32)
                             if chunk_ann_labels and c_mat_raw is not None else None)
        del c_ids_raw, c_mat_raw
        gc.collect()  # chunk matrix load scratch, before relation matrix stage

        # Relation-level ANN vectors/labels (relation-ann task): relations that
        # have a row in relation_embeddings. Persisted as relation_ann.bin so
        # _retrieve_relations_scored can ANN-narrow candidates on large
        # persisted-index notebooks instead of the full-matrix top_k_sims path
        # (which is the thing the #171-style cold-matrix guard exists to avoid
        # on multi-GB relation matrices). Direct load: same rationale as
        # _kg_matrix/_chunk_matrix above — bypasses _vector_matrix()/
        # _vector_cache so this matrix never becomes a cache entry.
        def _relation_matrix():
            with self._connect() as db:
                n_hint = db.execute(
                    "SELECT COUNT(*) AS c FROM relation_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
                rows = db.execute(
                    "SELECT relation_id AS vid, vector FROM relation_embeddings WHERE notebook_id=?",
                    (notebook_id,)).fetchall()
                return build_matrix(((r["vid"], r["vector"]) for r in rows), n_hint=n_hint, runtime_dim=self._runtime_dim())

        rel_ids_raw, rel_mat_raw = _timed("relation_matrix", _relation_matrix)
        relation_ann_labels = list(rel_ids_raw) if rel_ids_raw else []
        relation_ann_vectors = (np.asarray(rel_mat_raw, dtype=np.float32)
                                if relation_ann_labels and rel_mat_raw is not None else None)
        del rel_ids_raw, rel_mat_raw
        gc.collect()  # relation matrix load scratch, before viz-graph arrays stage

        # Folded concept-level viz graph (Task 4 / SP1): derive the EXACT same
        # graph _unified_graph_full(nb, "object") returns (concepts folded to
        # canonical ids via cluster_map, edges deduped) and persist it as compact
        # arrays so unified_graph(limit=N)/neighbors can serve a bounded core
        # without re-folding the full graph at request time.
        (viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload) = \
            _timed("viz_arrays", lambda: self._build_viz_graph_arrays(notebook_id))
        gc.collect()  # viz-graph build scratch, before persist (writes transition/ann/viz to disk)

        n_kg = len(kg_node_ids)
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        with self._connect() as db:
            watermark_sources = sorted(
                r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall())
        # manifest dim = 工件实际维(截断后的 ann_vectors 列数),不信配置 —— 运行时
        # 截断开启时三 ANN 均建在同一 runtime_dim 空间,manifest 记录真相以便 load/
        # 查询侧的 dim 守卫据实比较(T5;空矩阵回退到运行时生效维)。
        from app.services.vector_index import resolve_runtime_dim as _rrd
        built_dim = (int(ann_vectors.shape[1]) if getattr(ann_vectors, "size", 0)
                     else (_rrd(self.settings) or self.settings.embed_dim))
        manifest = {
            "version": self._scale_index_version(notebook_id),
            "dim": built_dim,
            "n_nodes": len(node_ids),
            "n_kg_nodes": n_kg,
            "n_chunks": len(chunk_ids),
            "n_hubs": len(node_ids) - n_kg - len(chunk_ids),
            "n_ann": len(ann_labels),
            "n_viz_nodes": len(viz_ids),
            "n_viz_edges": len(viz_payload.get("edges", [])),
            "watermark_sources": watermark_sources,
            "built_at": _now(),
            # Pre-persist stage timings only (persist/total aren't known until
            # after this dict is serialized to manifest.json by save_scale_index
            # below). The RETURNED manifest gets persist+total appended after
            # save_scale_index returns — see below. Full picture either way is
            # in the `scale_index_build` events (9 stages incl. persist/total).
            "build_ms": dict(timings),
        }
        persist_started = time.perf_counter()
        saved_manifest = si.save_scale_index(
            out_dir,
            node_ids=node_ids,
            transition=transition,
            idf=idf,
            chunk_index=chunk_index,
            ann_vectors=ann_vectors,
            ann_labels=ann_labels,
            manifest=manifest,
            viz_ids=viz_ids,
            viz_adj=viz_adj,
            viz_deg=viz_deg,
            viz_types=viz_types,
            viz_names=viz_names,
            viz_payload=viz_payload,
            chunk_ann_vectors=chunk_ann_vectors,
            chunk_ann_labels=chunk_ann_labels,
            relation_ann_vectors=relation_ann_vectors,
            relation_ann_labels=relation_ann_labels,
            prebuilt_ann=kg_ann_index,
            ef_construction=self.settings.hnsw_ef_construction,
        )
        persist_ms = round((time.perf_counter() - persist_started) * 1000)
        timings["persist"] = persist_ms
        self.event_log.emit({
            "kind": "scale_index_build",
            "notebook_id": notebook_id,
            "stage": "persist",
            "status": "done",
            "latency_ms": persist_ms,
        })
        _notify_stage("persist", persist_ms)
        total_ms = round((time.perf_counter() - build_started) * 1000)
        timings["total"] = total_ms
        self.event_log.emit({
            "kind": "scale_index_build",
            "notebook_id": notebook_id,
            "stage": "total",
            "status": "done",
            "latency_ms": total_ms,
        })
        _notify_stage("total", total_ms)
        # full rebuild 原地覆盖磁盘工件,但热进程的 _scale_idx_cache 仍持旧实例
        # (旧维/旧水位)——不失效则「重建后同进程看不见新索引」(fold 在 :8808
        # 已 pop,build 此前漏了,已核实缺陷)。pop → 下次 _scale_index 冷 reload 新工件。
        self._scale_idx_cache.pop(notebook_id, None)
        # Return a manifest dict enriched with persist+total (in-memory only;
        # the on-disk manifest.json written above intentionally only has the 7
        # pre-persist stages, since persist's own duration can't be known
        # before the file itself is written).
        return {**saved_manifest, "build_ms": dict(timings)}

    def fold_scale_index_delta(self, notebook_id: str, _assume_locked: bool = False) -> dict:
        """O(delta) 增量 fold:delta splice 进现有索引(ANN add_items、CSR splice),
        写 tmp 目录后锁内原子交换。无现有索引→全量 build;无 delta→no-op(返回旧 manifest)。
        fold 中途抛错→tmp 丢弃、旧索引不动(finally 只清 building 标记,未交换即无损)。
        _assume_locked=True 时(由 _run_scale_op 调用):调用方已持 _scale_building guard,
        本方法跳过自身 add/discard,避免嵌套去重导致空跑返回 already_building。"""
        import os
        import shutil

        import numpy as np
        import scipy.sparse as sp
        from app.services.kg import scale_index as si
        from app.services.vector_index import build_matrix

        idx = self._scale_index(notebook_id, allow_stale=True)
        if idx is None:
            return self.build_scale_index(notebook_id)
        # 运行时维守卫:旧索引建在 manifest.dim 空间,fold 的 delta 向量经 build_matrix
        # 已截断到运行时维 —— 两者不符则 add_items 会 hnswlib 硬错(被 _run_scale_op
        # 吞成一行日志,delta 积成山复现假死事故)。拒 fold + 发事件 + 升 full 重建。
        from app.services.vector_index import resolve_runtime_dim as _rrd
        _eff_dim = _rrd(self.settings) or self.settings.embed_dim
        if int(idx.manifest.get("dim", _eff_dim)) != int(_eff_dim):
            self.event_log.emit({
                "kind": "scale_fold_refused", "notebook_id": notebook_id,
                "reason": "dim_mismatch", "manifest_dim": int(idx.manifest.get("dim", 0)),
                "runtime_dim": int(_eff_dim)})
            return self.build_scale_index(notebook_id)
        delta = self._index_delta(notebook_id)
        if not delta["delta_sources"]:
            return idx.manifest
        if not _assume_locked:
            with self._scale_building_lock:
                if notebook_id in self._scale_building:
                    return {"status": "already_building"}
                self._scale_building.add(notebook_id)
        ok = False
        try:
            # 先把 delta 融进 concept_clusters(spec §4「incremental_fuse 簇」),
            # 否则 _gather_kg_graph(delta) 查不到 delta 对象的 cluster 成员 → 缺跨文档 hub 桥,
            # delta 对象在 scale_ppr 里跳不到兄弟概念(重现孤岛/对比检索坍缩)。
            # incremental_fuse_source 是 LLM-free(Tier1 名种子 append+Tier2 向量桥),daemon 线程安全、可重入。
            for _sid in delta["delta_sources"]:
                try:
                    self.incremental_fuse_source(notebook_id, _sid)
                except Exception:  # noqa: BLE001 — 融合失败不阻断 fold(退化为无 hub,仍可 ANN 召回)
                    self.event_log.logger.exception("fold incremental_fuse failed for %s", _sid)
            d_nodes, d_edges, d_chunks, d_kg_ids, d_membership = \
                self._gather_kg_graph(notebook_id, source_ids=delta["delta_sources"])
            kg_set = set(d_kg_ids)
            d_idf_map = {oid: (1.0 / c if c > 0 else 1.0)
                         for oid, c in d_membership.items()}
            node_ids, transition, idf, chunk_index = si.fold_arrays(
                list(idx.node_ids), idx.transition, idx.idf, idx.chunk_index,
                d_nodes, d_edges, d_chunks, d_idf_map)

            out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
            tmp_dir = out_dir + ".tmp"
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir)
            os.makedirs(tmp_dir, exist_ok=True)

            # 非 ANN 工件:node_ids/idf/chunk_index/graph
            sp.save_npz(os.path.join(tmp_dir, "graph.npz"), transition)
            np.save(os.path.join(tmp_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
            np.save(os.path.join(tmp_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
            np.save(os.path.join(tmp_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))

            # dim 失配已在方法入口拒 fold(转 build);到此 manifest.dim == 生效维,
            # fallback 用运行时生效维(旧 manifest 无 dim 键 + 运行时开的边角)。
            dim = int(idx.manifest.get("dim", self._runtime_dim() or self.settings.embed_dim))

            def _delta_vecs(table, col, ids):
                if not ids:
                    return [], []

                def _rows():
                    with self._connect() as db:
                        for batch in self._in_batches(ids):
                            ph = ",".join("?" for _ in batch)
                            for r in db.execute(
                                    f"SELECT {col} AS vid, vector FROM {table} "
                                    f"WHERE notebook_id=? AND {col} IN ({ph})",
                                    (notebook_id, *batch)).fetchall():
                                yield r["vid"], r["vector"]
                return build_matrix(_rows(), runtime_dim=self._runtime_dim())

            # ANN(KG 对象):增量 add delta 对象向量
            kg_vids, kg_mat = _delta_vecs("knowledge_embeddings", "object_id", list(kg_set))
            ann = si.add_items_to_ann(
                idx.ann_path, dim, kg_mat if len(kg_mat) else [], len(idx.ann_labels))
            ann.save_index(os.path.join(tmp_dir, "ann.bin"))
            ann_labels = list(idx.ann_labels) + list(kg_vids)
            np.save(os.path.join(tmp_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))

            # chunk ANN:增量 add delta chunk 向量(若原有 chunk_ann)
            manifest = dict(idx.manifest)
            manifest["built_at"] = _now()
            if idx.chunk_ann_path and idx.chunk_ann_labels is not None:
                ch_vids, ch_mat = _delta_vecs("chunk_embeddings", "chunk_id", list(d_chunks))
                cann = si.add_items_to_ann(
                    idx.chunk_ann_path, dim, ch_mat if len(ch_mat) else [],
                    len(idx.chunk_ann_labels))
                cann.save_index(os.path.join(tmp_dir, "chunk_ann.bin"))
                ch_labels = list(idx.chunk_ann_labels) + list(ch_vids)
                np.save(os.path.join(tmp_dir, "chunk_ann_labels.npy"),
                        np.asarray(ch_labels, dtype=object))
                manifest["has_chunk_ann"] = True
                manifest["n_chunk_ann"] = len(ch_labels)

            # relation ANN:增量 add delta relation 向量(若原有 relation_ann)——
            # 镜像上面 chunk ANN 的 fold 处理,delta relation id 取自水位后
            # source(与 _relation_ann_candidates 的 delta 暴力分支同一 IN 条件)。
            if idx.relation_ann_path and idx.relation_ann_labels is not None:
                d_relation_ids = []
                with self._connect() as db:
                    for batch in self._in_batches(delta["delta_sources"]):
                        ph_s = ",".join("?" for _ in batch)
                        d_relation_ids.extend(r["id"] for r in db.execute(
                            f"SELECT id FROM knowledge_relations "
                            f"WHERE notebook_id=? AND source_id IN ({ph_s})",
                            (notebook_id, *batch)).fetchall())
                rel_vids, rel_mat = _delta_vecs("relation_embeddings", "relation_id", d_relation_ids)
                rann = si.add_items_to_ann(
                    idx.relation_ann_path, dim, rel_mat if len(rel_mat) else [],
                    len(idx.relation_ann_labels))
                rann.save_index(os.path.join(tmp_dir, "relation_ann.bin"))
                rel_labels = list(idx.relation_ann_labels) + list(rel_vids)
                np.save(os.path.join(tmp_dir, "relation_ann_labels.npy"),
                        np.asarray(rel_labels, dtype=object))
                manifest["has_relation_ann"] = True
                manifest["n_relation_ann"] = len(rel_labels)

            # viz:保持旧(UI-only,可 stale)——从旧目录拷 viz 文件到 tmp(若有)
            for f in ("viz.npz", "viz_adj.npz"):
                src = os.path.join(out_dir, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(tmp_dir, f))

            # manifest:水位=当前全部 source、version bump、counts
            with self._connect() as db:
                watermark = sorted(r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall())
                total_chunks = db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
                    (notebook_id,)).fetchone()["c"]
            manifest.update({
                "version": self._scale_index_version(notebook_id),
                "watermark_sources": watermark,
                "n_nodes": len(node_ids),
                "n_chunks": int(total_chunks),
                "n_ann": len(ann_labels),
            })
            with open(os.path.join(tmp_dir, "manifest.json"), "w") as fh:
                json.dump(manifest, fh)

            # 原子交换(锁内):out_dir → .old,tmp → out_dir,rm .old
            old_dir = out_dir + ".old"
            with self._scale_building_lock:
                if os.path.exists(old_dir):
                    shutil.rmtree(old_dir)
                os.rename(out_dir, old_dir)
                os.rename(tmp_dir, out_dir)
                shutil.rmtree(old_dir, ignore_errors=True)
                self._scale_idx_cache.pop(notebook_id, None)  # 失效进程缓存 → 下次 reload
            ok = True
            return manifest
        finally:
            if not _assume_locked:
                with self._scale_building_lock:
                    self._scale_building.discard(notebook_id)
                # _assume_locked=True 时(由 _run_scale_op 调用)通知已在那边的最外层
                # 统一发一次;这里只在「独立调用 fold」(_assume_locked=False,如手动/
                # idle 调度器直调 fold)时通知,避免经 _run_scale_op 调用时重复 emit。
                if ok:
                    self._notify_index_done(notebook_id)

    def _index_delta(self, notebook_id: str) -> dict:
        """按 manifest 水位算 delta:水位后新增的 source 及其 chunk 数。
        无 manifest(未索引)→ 全部 source/chunk 视为 delta(语义:纯暴力)。"""
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        mpath = os.path.join(out_dir, "manifest.json")
        with self._connect() as db:
            cur_sources = [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
            if not os.path.exists(mpath):
                nchunks = db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
                return {"delta_sources": sorted(cur_sources),
                        "delta_chunks": int(nchunks), "indexed": False}
            with open(mpath) as fh:
                watermark = set(json.load(fh).get("watermark_sources", []))
            delta_sources = sorted(s for s in cur_sources if s not in watermark)
            if not delta_sources:
                return {"delta_sources": [], "delta_chunks": 0, "indexed": True}
            nchunks = 0
            for batch in self._in_batches(delta_sources):
                ph = ",".join("?" for _ in batch)
                nchunks += db.execute(
                    f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=? AND source_id IN ({ph})",
                    (notebook_id, *batch)).fetchone()["c"]
        return {"delta_sources": delta_sources, "delta_chunks": int(nchunks), "indexed": True}

    def _scale_index_eligible(self, notebook_id: str, *, tier: "str | None" = None,
                              exists: "bool | None" = None, total_chunks: "int | None" = None) -> bool:
        """能否构建/重建 scale 检索索引 —— **与 tier 解耦**:base-tier / 已建过 / 规模够大
        (总 chunk > index_suggest_chunk_threshold)/ 分享「大」定义(copyable=False,
        即字节或 chunks+nodes 超拷贝阈值)任一即可。检索侧已按『索引是否存在』使用
        (chunk_ann 默认开,indexed notebooks use ANN),不看 tier,故大个人库也应能建。
        可传入已算好的 tier/exists/total_chunks 复用,避免重复查询。
        短路顺序刻意把 notebook_copy_stats(5 个聚合查询)放在最后:常态路径
        (base/已建过/chunk 多)零新增开销,只有全部前置条件都不满足(非 base、未建、
        chunk-light)才付这一笔 —— 保证「大 → 可建/自动建」与 maybe_auto_index 的
        大库判定一致,chunk 少但字节/节点多的库不再被 eligibility 挡死。"""
        if tier is None:
            tier = self.get_notebook(notebook_id).tier
        if exists is None:
            exists = os.path.exists(
                os.path.join(self.settings.storage_dir, "kg_index", notebook_id, "manifest.json"))
        if tier == "base" or exists:
            return True
        if total_chunks is None:
            with self._connect() as db:
                total_chunks = db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
        if total_chunks > self.settings.index_suggest_chunk_threshold:
            return True
        return __import__("app.services.notebook_scale", fromlist=["NotebookScaleProfile"]).NotebookScaleProfile(self.settings, self, lambda nb: tuple(self._scale_index_version(nb)), self._vector_cache).index_eligible(notebook_id, tier=tier, has_disk_index=bool(exists), total_chunks=int(total_chunks))

    def scale_index_status(self, notebook_id: str) -> dict:
        """scale 索引状态(供在线重建入口 UX)。exists=磁盘有 manifest;
        stale=manifest 版本失配 或 delta chunk 超阈值;building=后台重建中;
        eligible=base-tier / 已建过 / 规模够大(与 tier 解耦,见 _scale_index_eligible)。计数取自 manifest。
        state 状态机(unindexed|suggested|building|indexed|stale):building 优先,
        未索引按总 chunk 阈值分 unindexed/suggested,已索引按版本失配/delta 阈值分 indexed/stale。"""
        nb = self.get_notebook(notebook_id)  # KeyError → 404
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        mpath = os.path.join(out_dir, "manifest.json")
        building = notebook_id in self._scale_building
        exists = os.path.exists(mpath)
        delta = self._index_delta(notebook_id)
        with self._connect() as db:
            total_chunks = db.execute(
                "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
        eligible = self._scale_index_eligible(notebook_id, tier=nb.tier, exists=exists, total_chunks=total_chunks)
        base = {"exists": exists, "building": building, "eligible": eligible,
                "delta_chunks": int(delta["delta_chunks"]), "total_chunks": int(total_chunks),
                # N sources added since the index (post-watermark); their semantic
                # vectors are searchable only if delta_searchable, else pending the
                # next fold (scale_auto_fold_on_add). Present on ALL return paths.
                "unindexed_sources": len(delta["delta_sources"]),
                "delta_searchable": bool(self.settings.scale_search_include_delta)}
        queued = notebook_id in self._scale_idle_queue
        if building:
            base["state"] = "building"
        elif queued:
            base["state"] = "queued"
        elif not exists:
            base["state"] = "suggested" if total_chunks > self.settings.index_suggest_chunk_threshold else "unindexed"
        else:
            with open(mpath) as fh:
                manifest = json.load(fh)
            version_stale = manifest.get("version") != self._scale_index_version(notebook_id)
            delta_over = delta["delta_chunks"] > self.settings.index_stale_delta_threshold
            # 运行时维切换后旧索引(manifest.dim ≠ 生效维)必须重建:ANN 建在旧空间,
            # 截断后的查询向量与之维度不符 → knn_query 硬错被吞成 fail-open 降级。
            # 判 stale 并带 reason,前端徽章据此提示重建(T6)。
            from app.services.vector_index import resolve_runtime_dim as _rrd
            eff_dim = _rrd(self.settings) or self.settings.embed_dim
            dim_stale = int(manifest.get("dim", eff_dim)) != int(eff_dim)
            base["state"] = "stale" if (version_stale or delta_over or dim_stale) else "indexed"
            if dim_stale:
                base["stale_reason"] = "dim_mismatch"
            base.update({
                "stale": bool(version_stale or delta_over or dim_stale),
                "last_built_at": str(manifest.get("built_at", "")),
                "manifest_dim": int(manifest.get("dim", 0)),
                "runtime_dim": int(eff_dim),
                "n_nodes": int(manifest.get("n_nodes", 0)),
                "n_chunks": int(manifest.get("n_chunks", 0)),
                "n_ann": int(manifest.get("n_ann", 0)),
                "n_chunk_ann": int(manifest.get("n_chunk_ann", 0)),
                "has_chunk_ann": bool(manifest.get("has_chunk_ann", False))})
            return base
        # 未建/构建中:补齐既有字段的默认值(保持 schema 稳定)
        base.update({"stale": False, "last_built_at": "", "n_nodes": 0, "n_chunks": 0,
                     "n_ann": 0, "n_chunk_ann": 0, "has_chunk_ann": False})
        return base

    def index_status(self, notebook_id: str) -> dict:
        """三系统构建状态聚合(纯只读,不触发任何 build)——供前端「索引与构建」面板
        一次拉齐,替代 4 条独立轮询。kg=抽取,unified_kg=概念合并,scale_index=检索索引。
        scale_index 原样复用 scale_index_status();unified_kg 取 dirty/building/last_rebuild_at
        子集(building 取 viz_building——unified_kg_status 内部经 _viz_index_probe 只读探测,
        从不 build)。kg 三字段直接复用 get_notebook() 算出的 NotebookSummary.kg_ready /
        kg_building / kg_pending_sources —— 与 NotebookSummary 是同一段代码(_notebook_from_row
        的 _has_kg/_count_pending_kg_sources + get_notebook 里的 _kg_building 回填,行 14086/
        14089/2064),而非另抄一份 SQL,structurally 杜绝口径与概要卡片漂移。"""
        nb = self.get_notebook(notebook_id)  # KeyError → 404
        scale = self.scale_index_status(notebook_id)
        uk = self.unified_kg_status(notebook_id)
        return {
            "kg": {
                "ready": bool(nb.kg_ready),
                "building": bool(nb.kg_building),
                "pending_sources": int(nb.kg_pending_sources),
            },
            "unified_kg": {
                "dirty": bool(uk.get("dirty", False)),
                "building": bool(uk.get("viz_building", False)),
                "last_rebuild_at": uk.get("last_rebuild_at", ""),
            },
            "scale_index": scale,
        }

    def _resolve_scale_mode(self, notebook_id: str, mode: str) -> str:
        """把 mode 解析为具体操作:fold|full。
        auto = 有(含 stale)索引 → fold,否则 → full。
        fold(显式或 auto 解析出)在 delta 源数超 scale_fold_max_delta_sources 时
        升级 full:fold 的逐源 incremental_fuse + 增量 splice 常数大(生产 48k 源
        delta 实测十几小时不可行),大 delta 全量重建反而有界(~1h)。"""
        if mode not in ("fold", "full"):
            mode = "fold" if self._scale_index(notebook_id, allow_stale=True) is not None else "full"
        if mode == "fold":
            # 运行时维切换:旧索引 manifest.dim ≠ 生效维 → fold 会把截断后的 delta
            # 向量 add 进旧空间(硬错/污染)→ 强制 full 在新空间整体重建。
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None:
                from app.services.vector_index import resolve_runtime_dim as _rrd
                eff_dim = _rrd(self.settings) or self.settings.embed_dim
                if int(idx.manifest.get("dim", eff_dim)) != int(eff_dim):
                    return "full"
                # 非源结构变更守卫:索引建成后若发生过「刷新图谱」(全量重聚类,推进
                # unified_kg_state.last_rebuild_at),fold 只把 delta 新源拼接进旧索引、
                # 不重读全量图(_gather_kg_graph 只取 delta 源)——旧节点的重聚类不会进
                # 索引,fold 却在末尾把 manifest.version 盖成当前(索引标最新、实则陈旧)。
                # 故索引 built_at 早于最近一次全量重建时升级 full,让重聚类真正收敛进索引。
                # (last_rebuild_at 仅由 rebuild 路径写,加新源的 incremental_fuse 不碰它,
                # 故不会把「纯新增来源」误判为需要 full。)
                built_at = str(idx.manifest.get("built_at", ""))
                if built_at:
                    with self._connect() as db:
                        row = db.execute(
                            "SELECT last_rebuild_at FROM unified_kg_state WHERE notebook_id=?",
                            (notebook_id,)).fetchone()
                    last_rebuild = str(row["last_rebuild_at"]) if (row and row["last_rebuild_at"]) else ""
                    if last_rebuild and last_rebuild > built_at:
                        return "full"
            try:
                delta = self._index_delta(notebook_id)
                if len(delta["delta_sources"]) > self.settings.scale_fold_max_delta_sources:
                    return "full"
            except Exception:  # noqa: BLE001 — 探测失败不挡操作,维持 fold
                pass
        return mode

    def _resolve_index_owner(self, notebook_id: str) -> "str | None":
        """索引完成通知该发给谁:优先发起请求线程的 `_REQUEST_USER`(copy_context 已
        传播,常规「刷新图谱」按钮走这条),回退查 `notebooks.created_by`(覆盖无请求
        上下文的场景,如 idle 调度器/摄取后自动 fold),都无则 None(调用方应静默跳过
        通知,而非报错)。"""
        try:
            u = _REQUEST_USER.get()
            if u is not None:
                return u.id
        except Exception:  # noqa: BLE001
            pass
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)
                ).fetchone()
                return row["created_by"] if row else None
        except Exception:  # noqa: BLE001
            return None

    def _notebook_name(self, notebook_id: str) -> str:
        """索引完成 toast 用的 notebook 展示名;查不到就空字符串(前端已按空名兜底)。"""
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT name FROM notebooks WHERE id = ?", (notebook_id,)
                ).fetchone()
                return row["name"] if row else ""
        except Exception:  # noqa: BLE001
            return ""

    def _notify_index_done(self, notebook_id: str) -> None:
        """索引成功收尾的统一通知钩子(fold/build 共用):刷新该 owner 的待确认中心
        snapshot(索引状态变化,如 building→indexed)+ 发一次瞬时 index_done 事件供
        前端弹 toast。只在**成功**路径调用——调用方负责判断成功与否;通知本身失败
        (无事件循环/DB 查询异常等)绝不能影响已经完成的索引写入,故整段 try/except
        吞掉,仅记日志。延迟 import pending_bus 避免模块加载期循环依赖。"""
        try:
            from app.services.pending_bus import pending_bus
            uid = self._resolve_index_owner(notebook_id)
            if not uid:
                return
            name = self._notebook_name(notebook_id)
            pending_bus.mark_dirty(uid)
            pending_bus.emit(uid, {
                "event": "index_done",
                "notebook_id": notebook_id,
                "notebook_name": name,
            })
        except Exception:  # noqa: BLE001 — 通知失败绝不影响已完成的索引 op
            try:
                self.event_log.logger.exception(
                    "index_done notify failed for %s", notebook_id)
            except Exception:
                pass

    def _run_scale_op(self, notebook_id: str, mode: str) -> None:
        """后台执行(guarded):按 mode 跑 fold_scale_index_delta 或 build_scale_index。
        本方法持 _scale_building guard;fold 用 _assume_locked=True 调用,避免嵌套去重空跑
        (fold 自身若再 add 会因已在集合返回 already_building)。build_scale_index 无自身
        guard,不受影响。只读 DB 向量建 ANN、不发模型调用,普通 daemon 线程即可。"""
        with self._scale_building_lock:
            if notebook_id in self._scale_building:
                return
            self._scale_building.add(notebook_id)

        def _run():
            ok = False
            try:
                op = self._resolve_scale_mode(notebook_id, mode)
                if op == "fold":
                    self.fold_scale_index_delta(notebook_id, _assume_locked=True)
                else:
                    self.build_scale_index(notebook_id)
                ok = True
            except Exception:  # noqa: BLE001 — 后台任务,失败仅记录
                try:
                    self.event_log.logger.exception("scale op failed for %s", notebook_id)
                except Exception:
                    pass
            finally:
                with self._scale_building_lock:
                    self._scale_building.discard(notebook_id)
                # 本方法是 _assume_locked=True 调用 fold 时的最外层持锁者(镜像上面
                # discard 的判定):无论走 fold 还是 full build,通知都从这里统一发一次,
                # 避免 fold 自身在 _assume_locked=True 时重复 emit(见 fold 内 not
                # _assume_locked 守卫)。仅成功(ok=True)才通知。
                if ok:
                    self._notify_index_done(notebook_id)
        threading.Thread(target=_run, name=f"scaleidx-{notebook_id}", daemon=True).start()

    def _process_idle_queue(self, force: bool = False) -> None:
        """低峰窗口(或 force)内 drain idle 队列,逐个后台重建。
        force=True 绕过时间窗(供测试/手动)。窗口按本地 datetime.now().hour 判定,
        start>end 视为跨零点。"""
        import datetime
        if not force:
            hour = datetime.datetime.now().hour
            lo = self.settings.scale_index_offpeak_start_hour
            hi = self.settings.scale_index_offpeak_end_hour
            in_window = (lo <= hour < hi) if lo <= hi else (hour >= lo or hour < hi)
            if not in_window:
                return
        with self._scale_building_lock:
            queued = dict(self._scale_idle_queue)
            self._scale_idle_queue.clear()
        for nb, mode in queued.items():
            self._run_scale_op(nb, mode)

    def _ensure_scale_scheduler(self) -> None:
        """懒启动低峰调度器 daemon(一次性):首次 idle 入队才启,避免 app-startup 接线。"""
        import time
        with self._scale_building_lock:
            if self._scale_scheduler_started:
                return
            self._scale_scheduler_started = True

        def _loop():
            while True:
                time.sleep(max(30, self.settings.scale_index_scheduler_poll_seconds))
                try:
                    self._process_idle_queue(force=False)
                except Exception:  # noqa: BLE001
                    try:
                        self.event_log.logger.exception("scale scheduler tick failed")
                    except Exception:
                        pass
        threading.Thread(target=_loop, name="scaleidx-scheduler", daemon=True).start()

    def trigger_scale_index_rebuild(self, notebook_id: str, when: str = "now",
                                    mode: str = "auto") -> dict:
        """base-tier(或已建过)才允许;不合格 → ValueError(路由转 409)。
        when="now" 立即后台重建(in-flight 去重);when="idle" 入 _scale_idle_queue、
        懒启动低峰调度器,返回 queued。mode="auto" 时由 _resolve_scale_mode 挑 fold/full。
        默认 when="now"/mode="auto" 保持既有无参调用行为不变。"""
        self.get_notebook(notebook_id)  # KeyError → 404
        if not self._scale_index_eligible(notebook_id):
            raise ValueError("notebook too small and not base-tier; scale index not applicable")
        if when == "idle":
            with self._scale_building_lock:
                self._scale_idle_queue[notebook_id] = mode
            self._ensure_scale_scheduler()
            return {"status": "queued", "notebook_id": notebook_id}
        if notebook_id in self._scale_building:
            return {"status": "already_building", "notebook_id": notebook_id}
        self._run_scale_op(notebook_id, mode)
        return {"status": "building", "notebook_id": notebook_id}

    def _dequeue_scale_idle(self, notebook_id: str) -> bool:
        """从空闲重建队列移除 notebook(加锁,幂等)。返回是否移除了一项。"""
        with self._scale_building_lock:
            return self._scale_idle_queue.pop(notebook_id, None) is not None

    def cancel_scale_index(self, notebook_id: str) -> dict:
        """取消检索索引构建:
        - state=queued(在空闲队列)→ 出队,cancelled=True。
        - state=building(后台守护线程在建)→ 无句柄不可协作打断,cancelled=False,
          reason=building_not_interruptible(前端提示「正在构建,完成后自动更新」)。
        - 其它 → 幂等 no-op,cancelled=False。
        返回 {cancelled, state(取消后的新 state), reason}。"""
        self.get_notebook(notebook_id)  # KeyError → 404
        if notebook_id in self._scale_building:
            return {"cancelled": False,
                    "state": self.scale_index_status(notebook_id)["state"],
                    "reason": "building_not_interruptible"}
        removed = self._dequeue_scale_idle(notebook_id)
        return {"cancelled": bool(removed),
                "state": self.scale_index_status(notebook_id)["state"],
                "reason": "" if removed else "not_queued"}

    def _maybe_enqueue_scale_fold(self, notebook_id: str) -> None:
        """After content is added, if the notebook ALREADY has a scale index and
        auto-fold is enabled, enqueue an idle incremental fold so the new
        (post-watermark) sources get indexed and become semantically searchable
        without a manual rebuild. Idle queue coalesces multiple adds into one
        fold. NEVER builds a fresh index (that stays a user decision above the
        suggest threshold). Fail-safe: never raises."""
        if not self.settings.scale_auto_fold_on_add:
            return
        try:
            if self._scale_index(notebook_id, allow_stale=True) is None:
                return   # not indexed yet → don't auto-build; leave to user/suggest
            self.trigger_scale_index_rebuild(notebook_id, when="idle", mode="fold")
        except Exception:
            self.event_log.logger.exception("auto scale-fold enqueue failed for %s", notebook_id)

    def maybe_auto_index(self, notebook_id: str) -> None:
        """大库自动建/重建检索索引 —— fail-open,绝不向调用方抛异常。

        「大」复用分享/拷贝的定义:notebook_copy_stats()["copyable"] is False
        (字节 > NOTEBOOK_COPY_MAX_BYTES 或 chunks+nodes > NOTEBOOK_COPY_MAX_ROWS)。

        Called from two kinds of call sites:
          - 写路径(_run_extraction 收尾、rebuild_unified_kg 收尾):每次都可能被
            调用,但仍先过 once-set,避免同一 nb 连续写入反复入队。
          - 读路径兜底(检索遇到无 ANN 回退时):必须 O(1) —— once-set 命中就直接
            return,不做任何 DB 查询。

        once-set 语义:一旦被"评估过"(无论结论是"已入队/建成"还是"不需要"),就
        加入 self._auto_index_checked,后续调用直接短路。让集合过期重新评估的唯一
        入口是 _mark_unified_kg_dirty(每次 KG 写都会 discard 该 nb) —— 这样索引
        后续因新内容变 stale 时,下一轮写入/检索会重新评估而不是被旧判定永久挡住。
        """
        if not self.settings.scale_index_auto_enabled:
            return
        if notebook_id in self._auto_index_checked:
            return
        # 批量摄取早退:多源摄取时 _mark_unified_kg_dirty 逐源 discard 该 nb 的
        # once-set 命中,导致同一批次每个源都重跑下面的 notebook_copy_stats(5 COUNT)+
        # scale_index_status(多查询+manifest 读)。已在 building/idle 排队中的 nb
        # 无需重新评估 —— O(1) 直接短路。锁语义:_scale_building 别处在
        # _scale_building_lock 下读写,这里为性能故意不加锁做成员检查;是启发式早退,
        # 漏判/误判的窗口极窄且后果轻(至多多跑一次评估),判定安全(review #4)。
        if notebook_id in self._scale_building or notebook_id in self._scale_idle_queue:
            self._auto_index_checked.add(notebook_id)
            return
        try:
            stats = self.notebook_copy_stats(notebook_id)
            if stats["copyable"]:
                return  # 小库:行为不变,不自动建索引
            status = self.scale_index_status(notebook_id)
            if status["state"] not in ("unindexed", "suggested", "stale"):
                return  # 已索引且新鲜 / 正在构建 / 已排队 —— 无需触发
            # unindexed 也触发:该分支只在 copyable=False(已判定「大」)之后才到达,
            # 「大」的定义(字节/chunks+nodes)与 index_suggest_chunk_threshold(仅看
            # total_chunks)是两把不同的尺子 —— chunk 少但字节/节点多的库会停在
            # unindexed 永不建议。产品意图是「大 → 自动建」,故此处三态一视同仁,
            # 交给 trigger_scale_index_rebuild → _scale_index_eligible 做最终把关
            # (仍不 eligible 会 ValueError,被下面 except 静默吞掉)。
            try:
                self.trigger_scale_index_rebuild(
                    notebook_id, when=self.settings.scale_index_auto_when, mode="auto")
            except Exception:  # noqa: BLE001 — 不 eligible/并发冲突等,auto 路径静默跳过
                pass
        except Exception:  # noqa: BLE001 — 读路径兜底绝不能因此拖垮请求
            try:
                self.event_log.logger.exception("maybe_auto_index failed for %s", notebook_id)
            except Exception:
                pass
        finally:
            self._auto_index_checked.add(notebook_id)

    def _build_viz_graph_arrays(self, notebook_id: str):
        """Full-payload derivation (used by build_scale_index). Delegates the
        array math to _viz_arrays_from_graph so build_viz_index can reuse it with
        a lighter (json_extract) derivation."""
        return self._viz_arrays_from_graph(self._unified_graph_full(notebook_id, "object"))

    def _viz_arrays_from_graph(self, full: dict):
        """(viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload) from a
        folded object-level graph dict {nodes, edges}. Node order = input order
        (matters for degree-tie vs limit_graph_by_degree). Only reads id /
        object_type / payload.name — payload may be full or name-only."""
        import numpy as np
        import scipy.sparse as sp

        nodes = full["nodes"]
        edges = full["edges"]
        viz_ids = [n["id"] for n in nodes]
        viz_types = [n["object_type"] for n in nodes]
        viz_names = [(n.get("payload") or {}).get("name", "") for n in nodes]
        index = {nid: i for i, nid in enumerate(viz_ids)}
        n = len(viz_ids)

        deg = np.zeros(n, dtype=np.int64)
        und_rows, und_cols, und_seen = [], [], set()
        edge_list: List[list] = []
        for e in edges:
            s, t = e["source_object_id"], e["target_object_id"]
            si_, ti = index.get(s), index.get(t)
            if si_ is None or ti is None:
                continue
            edge_list.append([s, t, e["edge_type"]])
            deg[si_] += 1
            deg[ti] += 1
            if si_ != ti:
                pair = (si_, ti) if si_ < ti else (ti, si_)
                if pair not in und_seen:
                    und_seen.add(pair)
                    und_rows += [pair[0], pair[1]]
                    und_cols += [pair[1], pair[0]]

        if und_rows:
            data = np.ones(len(und_rows), dtype=np.int8)
            viz_adj = sp.csr_matrix((data, (und_rows, und_cols)), shape=(n, n))
        else:
            viz_adj = sp.csr_matrix((n, n), dtype=np.int8)
        viz_deg = deg.astype(np.int32)
        viz_payload = {"edges": edge_list}
        return viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload

    def _derive_object_graph_lite(self, notebook_id: str) -> dict:
        """Object-level folded graph EQUIVALENT to _unified_graph_full(nb,'object')
        but WITHOUT full-payload json.loads: node names come from SQL
        json_extract(payload,'$.name'). Same table + same WHERE (no ORDER BY) →
        same scan order → same fold order → identical viz arrays."""
        self.get_notebook(notebook_id)
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = db.execute(
                "SELECT id, object_type, json_extract(payload,'$.name') AS name "
                "FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchall()
        nodes = [{"id": r["id"], "object_type": r["object_type"],
                  "payload": {"name": r["name"] or ""}} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"],
                  "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        return derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))

    def _viz_index_dir(self, notebook_id: str) -> str:
        return os.path.join(str(self.settings.storage_dir), "kg_viz", notebook_id)

    def build_viz_index(self, notebook_id: str) -> Optional[dict]:
        """Build + persist a viz-only index under {storage_dir}/kg_viz/{nb}/ so the
        KG-view fast paths light up for notebooks without a full scale index.
        json_extract names avoid the 300k-row json.loads. Returns manifest, or
        None for an empty graph (no non-deprecated objects). Caches on success."""
        from app.services.kg import viz_index as vi
        self.get_notebook(notebook_id)
        full = self._derive_object_graph_lite(notebook_id)
        if not full["nodes"]:
            return None
        viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload = \
            self._viz_arrays_from_graph(full)
        manifest = {
            "version": self._scale_index_version(notebook_id),
            "n_viz_nodes": len(viz_ids),
            "n_viz_edges": len(viz_payload.get("edges", [])),
        }
        out_dir = self._viz_index_dir(notebook_id)
        vi.save_viz_index(out_dir, viz_ids=viz_ids, viz_adj=viz_adj, viz_deg=viz_deg,
                          viz_types=viz_types, viz_names=viz_names,
                          viz_payload=viz_payload, manifest=manifest)
        self._viz_idx_cache[notebook_id] = vi.load_viz_index(out_dir)
        return manifest

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
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        if (os.path.exists(os.path.join(out_dir, "manifest.json"))
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
        active_indexed = os.path.exists(
            os.path.join(self.settings.storage_dir, "kg_index", notebook_id, "manifest.json"))
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
