"""Candidate retrieval owner.

The service is composed from narrow stores/runtime collaborators.  It retains
no repository facade; compatibility operation functions are unbound and run
against this facade-independent state object. All query families are hosted
by their owning stores.
"""
from __future__ import annotations

import json
import itertools
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from app.core.ask_context import _ASK_EMBED_CACHE
from app.core.config import (
    DEFAULT_CHUNK_KG_FAN_OUT,
    DEFAULT_CHUNK_KG_MAX_DEPTH,
    DEFAULT_CHUNK_KG_NODE_SEED_TOP_N,
    DEFAULT_CHUNK_KG_RELATION_SEED_TOP_N,
)
from app.models.common import Evidence
from app.domain.retrieval import ChunkRetrievalPlan
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.retrieval import (
    RELEVANCE_FLOOR,
    W_KEYWORD,
    W_SEMANTIC,
    GapRelationRow,
    NeighborExpansion,
    RetrievedElement,
    RetrievedKnowledge,
    RetrievedRelation,
    _TYPE_WEIGHT,
    _fuse,
    _payload_text,
    _tokens,
    bm25_scores,
    ensure_procedure_quota,
    is_process_query,
    keyword_basis,
    partition_generated_question_chunks,
    rrf_fuse,
    score_elements,
    score_knowledge,
    type_weight,
)
from app.services.retrieval_run import (
    current_retrieval_run,
    memoized_query_embedding,
)
from app.services.source_display import source_display_title


_KG_TYPES = ("claim", "formula", "procedure", "concept")

# 邻居展开(`_retrieve_neighbors`)的行读取放大系数。`REASONING_NEIGHBOR_EXPAND_LIMIT`
# 约束的是**唯一合格邻居数**,而 SQL 的 LIMIT 只会数**关系行**——两者之间隔着
# 「同一对端点的多条重复/佐证关系」与「被 `is_queryable_edge_pair` 判掉的行」。
# 按行截断会让这些行吃掉预算,展开数远少于配置值。因此读取界放大到
# `(limit+1) × 4`(仓库既有的「有界过扫描」做法,见集合枚举的 `max_rows × 4`):
# 读取仍是索引序、仍然有界,只是把重复与不可查边的消耗吸收掉。放大系数不做成
# 部署变量:它是实现细节(取决于抽取产生多少重复边),不是用户该调的预算。
_NEIGHBOR_ROW_OVERSCAN = 4

# --------------------------------------- KG 弱支撑边探测(设计文档 §3.3)
# 「支撑薄弱」= 这条 canonical 边只有 1-2 篇**文档**撑着(`source_count`),不是
# 「只有 1-2 条原始关系」(`support_count`)。综述类问题里它恰好是最该补证的方向,
# 所以作为提示喂给模型;阈值放到 3 就把「有一定支撑」的边也算进来,提示会被稀释
# 成噪声。
_KG_GAP_SOURCE_MAX = 2
# 一次探测最多取回几条边。这是**成本闸**:它同时封住了随后名字解析那条查询的
# 输入规模,也封住了回喂账目在内存里能攒多大。
_KG_GAP_PROBE_LIMIT = 24
# 一次探测最多接受几个源端 id。调用方(大纲绑定)按构造只会给 ≤12 节×8 键,这里
# 是防御性的第二道:IN 列表的长度必须由服务端说了算,不能由上游的一个笔误决定。
_KG_GAP_MAX_SEEDS = 96

_GENERATED_QUESTION_QUERY_EVENT_FIELDS = frozenset({
    "kind",
    "mode",
    "status",
    "baseline_hits",
    "scan_limit",
    "question_rows",
    "matched_chunks",
    "added_chunks",
})


def _first_relation_sample(raw: object) -> str:
    """`canonical_relations.sample_relation_ids` 的第一条样本关系 id。

    两个适配器都以 JSON **文本**出参(PostgreSQL 侧显式 `::text`),所以这里只认
    文本 —— 服务层不判断 dialect 是架构红线,而「顺手也接受 list」正是把那条判断
    偷偷搬进服务层的写法(一侧改成出 list 时它会静默继续工作,parity 用例反而
    测不出两侧已经不同形)。
    """
    if not isinstance(raw, str):
        return ""
    try:
        samples = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(samples, list) and samples:
        return str(samples[0])
    return ""


class _RetrievalState:
    def __init__(
        self,
        *,
        database,
        snapshots,
        queries,
        scale_runtime,
        model_clients,
        model_error_sink,
        notebook_languages,
        settings,
        event_log,
        embedder,
    ) -> None:
        self.database = database
        self.snapshots = snapshots
        self.queries = queries
        self.scale_runtime = scale_runtime
        self.model_clients = model_clients
        self.model_error_sink = model_error_sink
        self._notebook_langs_cache = notebook_languages
        self.settings = settings
        self.event_log = event_log
        self.embedder = embedder
        self._peer = None
        self._retrieval = None

    def bind(self, *, peer, retrieval) -> None:
        self._peer = peer
        self._retrieval = retrieval

    @property
    def retrieval(self):
        return self._retrieval

    @property
    def _vector_cache(self):
        return self.snapshots.vector_cache

    @property
    def _unified_cache(self):
        return self.snapshots.unified_cache

    @property
    def _MIX_KG_KEY_BASE(self):
        return 1000

    def _connect(self):
        return self.database.connect()

    def _note_model_error(
        self,
        stage: str,
        model_or_error,
        exc: Exception | None = None,
        service: str = "",
        *,
        provider_failure: bool = False,
        failed_fingerprint: str = "",
        workload_id: str = "",
    ) -> None:
        if workload_id:
            error = exc or model_or_error
            self.model_error_sink.note_model_error(
                stage, error, workload_id=workload_id
            )
            return
        self.model_error_sink.note_model_error(
            stage,
            model_or_error,
            exc,
            service=service,
            provider_failure=provider_failure,
            failed_fingerprint=failed_fingerprint,
        )

    def _in_batches(self, ids):
        values = list(dict.fromkeys(ids))
        for offset in range(0, len(values), self._IN_CHUNK):
            yield values[offset:offset + self._IN_CHUNK]

    def _scale_index(self, notebook_id: str, allow_stale: bool = False):
        return self.scale_runtime.load(notebook_id, allow_stale=allow_stale)

    def _open_scale_ann(self, index, kind: str):
        return self.scale_runtime.open_ann(index, kind)

    def _index_delta(self, notebook_id: str) -> dict:
        return self.scale_runtime.builder._index_delta(notebook_id)

    def _scale_index_version(self, notebook_id: str) -> list:
        return self.scale_runtime.version(notebook_id)

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        return self.scale_runtime.notebook_copy_stats(notebook_id)

    def maybe_auto_index(self, notebook_id: str) -> None:
        self.scale_runtime.maybe_auto_index(notebook_id)

    def _federated_graph_is_large(self, active_notebook_id: str) -> bool:
        return any(
            not self.notebook_copy_stats(notebook_id)["copyable"]
            for notebook_id in self.notebooks.participant_notebook_ids(active_notebook_id)
        )

    def cluster_map(self, notebook_id: str) -> dict[str, str]:
        version = tuple(self._scale_index_version(notebook_id))
        return self._vector_cache.get(
            f"{notebook_id}:clustermap",
            version,
            lambda: self._load_cluster_map(notebook_id),
        )

    def _load_cluster_map(self, notebook_id: str) -> dict[str, str]:
        with self._connect() as database:
            return self.unified_kg.cluster_map_rows(database, notebook_id)

    def _candidate_cluster_map(
        self, notebook_id: str, object_ids: Iterable[str]
    ) -> dict[str, str]:
        """Load canonical mappings for this scored window, never the full KG."""
        folded: dict[str, str] = {}
        with self._connect() as database:
            for batch in self._in_batches(object_ids):
                for row in self.unified_kg.cluster_fold_rows(
                    database, notebook_id, batch
                ):
                    folded[row["member_object_id"]] = row["canonical_id"]
        return folded

    def _gather_kg_graph(self, notebook_id: str, source_ids=None, synonym_edges=None,
                         as_arrays: bool = False):
        return self.scale_runtime.builder.gather_graph(
            notebook_id, source_ids=source_ids, synonym_edges=synonym_edges,
            as_arrays=as_arrays,
        )

    def _knowledge_objects(self, database, notebook_id, object_type,
                           statuses=USABLE_STATUSES, id_filter=None):
        return self.knowledge.retrieval_objects(
            database, notebook_id, object_type, statuses, id_filter,
            batch_size=self._IN_CHUNK,
        )

    def _notebook_has_kg(self, notebook_id: str) -> bool:
        return self.knowledge.has_kg(notebook_id)

    def _unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        """Whether non-partitioned channels must be disabled before I/O."""
        from app.services.source_scope import (
            current_source_scope,
            source_scope_restricted,
            source_scope_visible_universe_matches,
        )

        scope = current_source_scope()
        if scope is None or scope.notebook_id != notebook_id:
            # Omitted scope is the historical whole-library path.  Do not add
            # repository probes to it; this also keeps bounded/legacy service
            # adapters that do not own a SourceStore fully compatible.
            return False
        if source_scope_restricted():
            return True
        visible_ids = getattr(self.sources, "all_visible_source_ids", None)
        if not callable(visible_ids):
            # Compatibility for bounded test/adapter doubles. Production
            # repositories always expose the universe probe.
            return False
        # Re-read the hidden half with the identity the freeze used, not with
        # whoever happens to be current here: this runs inside a detached
        # worker, and that half is owner-scoped. Comparing an owner-scoped
        # snapshot against an unfiltered (or differently-scoped) live read
        # would report drift forever in any shared notebook holding another
        # member's Memory, silently disabling these channels for good.
        hidden_ids = getattr(self.sources, "hidden_source_ids", None)
        return not source_scope_visible_universe_matches(
            notebook_id,
            visible_ids(notebook_id),
            (hidden_ids(notebook_id, scope.owner_id)
             if callable(hidden_ids) else None),
        )

    def _any_base_notebook_has_kg(self, notebook_id: str, database=None) -> bool:
        if database is not None:
            return self.knowledge.any_mounted_has_kg_on(database, notebook_id)
        return self.knowledge.any_mounted_has_kg(notebook_id)

    def _federated_rx_graph(self, *args, **kwargs):
        return self._peer._federated_rx_graph(*args, **kwargs)

    def _ppr_retrieve(self, *args, **kwargs):
        return self._peer._ppr_retrieve(*args, **kwargs)

    def _kg_source_chunks(self, *args, **kwargs):
        return self._peer._kg_source_chunks(*args, **kwargs)


class CandidateRetrievalService(_RetrievalState):
    _CJK_RE = re.compile(r"[一-鿿]")
    _LATIN_RE = re.compile(r"[A-Za-z]")
    _IN_CHUNK = 900
    _MIX_KG_KEY_BASE = 1000

    def __init__(
        self,
        *,
        notebooks,
        sources,
        chunks,
        embeddings,
        knowledge,
        governance,
        unified_kg,
        queries,
        snapshots,
        scale_runtime,
        model_clients,
        model_error_sink,
        settings,
        event_log,
        database,
        embedder,
        notebook_languages,
        memory_retriever=None,
    ) -> None:
        super().__init__(
            database=database,
            queries=queries,
            snapshots=snapshots,
            scale_runtime=scale_runtime,
            model_clients=model_clients,
            model_error_sink=model_error_sink,
            notebook_languages=notebook_languages,
            settings=settings,
            event_log=event_log,
            embedder=embedder,
        )
        self.notebooks = notebooks
        self.sources = sources
        self.chunks = chunks
        self.embeddings = embeddings
        self.knowledge = knowledge
        self.governance = governance
        self.unified_kg = unified_kg
        self.queries = queries
        self.snapshots = snapshots
        self.scale_runtime = scale_runtime
        self.model_clients = model_clients
        self.model_error_sink = model_error_sink
        self.database = database
        self.memory_retriever = memory_retriever

    def notebook_memory_hits(
        self, user_id: str, notebook_id: str, query: str, limit: int = 8
    ):
        if self.memory_retriever is None:
            return []
        return self.memory_retriever.notebook_memory_hits(
            user_id, notebook_id, query, limit
        )

    def agent_memory_hits(
        self, user_id: str, notebook_id: str, query: str,
        include_candidates: bool, limit: int = 8,
    ):
        if self.memory_retriever is None:
            return []
        return self.memory_retriever.agent_memory_hits(
            user_id, notebook_id, query, include_candidates, limit
        )

    def weak_support_relations(
        self, notebook_id: str, object_ids: Iterable[str]
    ) -> List[GapRelationRow]:
        """给定一批 KG 对象 id,取它们在 canonical 层上**支撑薄弱**的出边。

        设计文档 §3.3 的服务端三步,全部在这里完成(调用方零 SQL):

        1. 经既有 `cluster_fold_rows` 把本轮的 id 折叠成 canonical 集 —— 只折**本轮
           给定的 id**,绝不加载整个 notebook 的 cluster map(大库那张表可达数百万
           条,物化一次就是一次 OOM 级的内存尖峰)。
        2. 按 canonical 源端探测 `canonical_relations`(主键前缀 seek + **来源数**
           阈值 + LIMIT)。判据是 `source_count` 而不是 `support_count`,理由见
           存储层同名方法:后者是聚合掉的原始关系行数,单源边攒到 >2 行是常态。
        3. 一次有界批量解析两端显示名(簇 `canonical_name` 优先、对象 payload name
           回退),解析不到的行丢弃 —— 一条只有内部 id 的提示对模型毫无用处,而把
           `K-<seed>` 这种内部形状喂进 prompt,模型会把它当名字复述进答案。

        `canonical_relations` 是**可选**派生产物(从未 rebuild 过就是空表),那时
        整段静默缺席、返回空列表,不是错误。

        联邦刻意不做:`canonical_relations` 是 per-notebook 产物,挂载参考库的绑定
        贡献零候选(与 exact_lookup「联邦刻意未做」同先例,登记为残余)。

        返回按 (来源数升序, 源端 id, 目标端 id) 排序 —— 展示顺序必须稳定可测,
        否则「同一份库两次跑出两份提示」会让回归用例只能测长度。SQL 的 `ORDER BY`
        不能代替这一步:它的次键是 `canonical_tgt`(为的是让 LIMIT 截断本身确定),
        与这里的 `(src, tgt)` 展示序在真并列上给出不同结果。
        """
        seeds = [
            text for text in dict.fromkeys(str(oid) for oid in object_ids) if text
        ][:_KG_GAP_MAX_SEEDS]
        if not seeds:
            return []
        with self._connect() as database:
            folded: Dict[str, str] = {}
            cluster_names: Dict[str, str] = {}
            for row in self.unified_kg.cluster_fold_rows(
                database, notebook_id, seeds
            ):
                folded[row["member_object_id"]] = row["canonical_id"]
                name = (row["canonical_name"] or "").strip()
                if name:
                    cluster_names.setdefault(row["canonical_id"], name)
            # 没有簇行的对象:它自己就是 canonical 端点(rebuild_canonical_relations
            # 的 COALESCE 回退口径),原样入探测集合。
            canonical_ids = list(dict.fromkeys(
                folded.get(seed, seed) for seed in seeds
            ))
            edges = self.unified_kg.weak_support_relation_rows(
                database, notebook_id, canonical_ids,
                _KG_GAP_SOURCE_MAX, _KG_GAP_PROBE_LIMIT,
            )
            if not edges:
                return []
            sample_by_edge: Dict[tuple, str] = {}
            for edge in edges:
                sample = _first_relation_sample(edge["sample_relation_ids"])
                if sample:
                    sample_by_edge[(
                        edge["canonical_src"], edge["edge_type"],
                        edge["canonical_tgt"],
                    )] = sample
            name_rows = self.unified_kg.relation_endpoint_name_rows(
                database, notebook_id,
                list(dict.fromkeys(sample_by_edge.values())),
            )
        names_by_relation = {
            row["rid"]: ((row["src_name"] or "").strip(),
                         (row["tgt_name"] or "").strip())
            for row in name_rows
        }
        rows: List[GapRelationRow] = []
        for edge in edges:
            key = (edge["canonical_src"], edge["edge_type"], edge["canonical_tgt"])
            sampled_src, sampled_tgt = names_by_relation.get(
                sample_by_edge.get(key, ""), ("", "")
            )
            # 源端优先用第 1 步已经拿到的簇名(那是**本轮绑定的那个对象**自己的
            # 簇行,零额外查询);拿不到才回退样本关系解析出来的名字。两者可以不
            # 同名:样本关系的源端往往是同一簇里的**另一个**成员行,而模型手上拿
            # 到的是它绑定的那一个,提示行按它称呼才对得上。
            source_name = cluster_names.get(edge["canonical_src"], "") or sampled_src
            if not source_name or not sampled_tgt:
                continue
            rows.append(GapRelationRow(
                canonical_src=edge["canonical_src"],
                canonical_tgt=edge["canonical_tgt"],
                src_name=source_name,
                tgt_name=sampled_tgt,
                edge_type=edge["edge_type"],
                source_count=int(edge["source_count"]),
            ))
        rows.sort(key=lambda row: (
            row.source_count, row.canonical_src, row.canonical_tgt
        ))
        return rows

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
            rows = self.chunks.language_probe_rows(db, notebook_id)
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

    def _lexical_corpus_langs(
        self, notebook_id: str, *, source_scoped: bool = False
    ) -> Optional[List[str]]:
        """The corpus languages a lexical probe set may be filtered against.

        Always the languages of the notebook the ADAPTER is about to query —
        pass the same `notebook_id` that goes into `fts_search` /
        `chunk_fts_search`, never the active notebook, or a mounted reference
        library would be filtered against somebody else's corpus.

        `None` means "do not filter": the adapters treat an unprobed corpus as
        unknown, so the kill switch is expressed simply by not answering.
        `_notebook_langs` is cached per notebook and invalidated at chunk-write
        settle points, so this costs no query on the hot path.

        `source_scoped=True` always answers `None`, and that exemption is
        load-bearing rather than cautious. A run bounded to selected sources
        deliberately skips the notebook-wide ANN, so its lexical arm is the
        ONLY candidate generator — while the language hint is sampled from the
        notebook's chunk table as a whole. A Chinese source sitting in the
        unsampled middle of an otherwise English notebook would therefore have
        every CJK term dropped and return no evidence at all, instead of merely
        losing its lexical share of a wider candidate pool. The cost argument
        also inverts here: the source predicate is applied inside the LATERAL
        before LIMIT, so a scoped probe only ever scans the selected sources'
        rows — the pathological whole-notebook scan this gate exists to stop
        cannot happen on that path. Highest risk, lowest reward: do not gate.
        (codex #428 round-2 P1)

        Fail-open, and it has to be: callers run this BEFORE they open their
        connection, which puts it outside the `except` that keeps a failing
        lexical arm from taking retrieval down with it. A language hint that
        cannot be read is exactly the "unknown corpus" this returns `None` for
        — the probe stops filtering, retrieval carries on byte-identically to
        the pre-feature path, and the failure is still visible as an event.
        """
        try:
            if source_scoped or not self.settings.lexical_language_gate_enabled:
                return None
            return self._notebook_langs(notebook_id)
        except Exception as exc:  # noqa: BLE001 — a hint must never break retrieval
            self.event_log.emit({
                "kind": "lexical_language_probe_failed",
                "notebook_id": notebook_id,
                "error_type": type(exc).__name__,
            })
            return None

    def _embed_query(self, query: str) -> Optional[List[float]]:
        """查询 embedding(端点按原生维出向量)→ 运行时截断(EMBED_RUNTIME_DIM,
        与语料侧 build_matrix 共用 truncate_vec 同一口径)。查询/语料两侧维度
        不一致 = 静默零召回,是截断项目的头号风险 —— 勿在别处另行截断。
        P1-A:ask 作用域内(_ASK_EMBED_CACHE 非 None)按配置的 embedding 输入上限
        复用同文本
        的截断后向量,砍 federated 双 tier/seed/quota 对同一问题的重复 RTT;
        失败不缓存(保留每次重试语义)。default None(非 ask 路径)行为不变。"""
        if not getattr(self.embedder, "configured", False):
            return None
        key = query[: self.settings.embed_truncate_chars]

        def _compute() -> Optional[List[float]]:
            vec = self.embedder.embed_query(key)
            from app.services.vector_index import resolve_runtime_dim, truncate_vec
            rd = resolve_runtime_dim(self.settings)
            if rd and vec is not None and len(vec) > rd:
                import numpy as np
                vec = truncate_vec(np.asarray(vec, dtype=np.float32), rd).tolist()
            return vec

        try:
            # The retrieval-run state is thread-safe and single-flight.  Keep
            # the old Ask ContextVar as a compatibility fallback for direct
            # service/test callers that have not entered the common run yet.
            if current_retrieval_run() is not None:
                return memoized_query_embedding(key, _compute)
            cache = _ASK_EMBED_CACHE.get()
            if cache is not None:
                hit = cache.get(key)
                if hit is not None:
                    return hit
            vec = _compute()
        except Exception as exc:
            self._note_model_error(
                "embed",
                exc,
                workload_id="retrieval_query_embedding",
            )
            return None
        if cache is not None and vec is not None:
            cache[key] = vec
        return vec
    def _gather_elements(self, db: object, notebook_id: str,
                         with_vectors: bool = True,
                         allowed_source_ids=None) -> List[dict]:
        # 运行时截断旁路(计划 §1.2 旁路 1):element 兜底不走 build_matrix,逐向量
        # 进 retrieval.cosine 与 _embed_query(已截断,T2)比较 —— cosine 对 len 不等
        # 静默返 0.0,漏截 = 全部 element 静默零相似度。
        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        rd = resolve_runtime_dim(self.settings)
        if allowed_source_ids is None:
            rows = self.sources.retrieval_element_rows(db, notebook_id)
        else:
            rows = self.sources.retrieval_element_rows(
                db, notebook_id, allowed_source_ids
            )
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
    def _gather_chunks(
        self, db: object, notebook_id: str, allowed_source_ids=None
    ) -> List[dict]:
        if allowed_source_ids is None:
            rows = self.chunks.retrieval_rows(db, notebook_id)
        else:
            id_rows = self.chunks.ids_for_sources(
                db, notebook_id, allowed_source_ids
            )
            rows = self.chunks.hydrate_rows(
                db, [row["id"] for row in id_rows]
            )
        return [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
    def _vector_matrix_version(self, db: object, notebook_id: str, table: str):
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
        ver = self.embeddings.version_row(db, notebook_id, table)
        return (table, ver["c"], ver["ts"], resolve_runtime_dim(self.settings))
    def _vector_matrix(self, db: object, notebook_id: str,
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
            rows = self.embeddings.vector_rows(db, notebook_id, table, id_col)
            return build_matrix(((r["vid"], r["vector"]) for r in rows),
                                runtime_dim=runtime_dim)

        return self._vector_cache.get(f"{notebook_id}:matrix:{table}", version, _load)
    def _vector_matrix_warm(self, db: object, notebook_id: str, table: str) -> bool:
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

        ver = self.knowledge.object_version_row(db, notebook_id)
        version = ("kwtok", ver["c"], ver["ts"])
        return self._vector_cache.get(f"{notebook_id}:kwtok", version, lambda: _build(objects))
    def _relations_with_names(self, db: object, notebook_id: str,
                              relation_ids: Optional[List[str]] = None) -> List[dict]:
        """关系 + 两端实体名 + evidence,预构建 keyword/embed 文本。JOIN 丢弃悬空边
        (端点不在 knowledge_objects),与图节点过滤一致。

        relation_ids=None(默认): 全量(现状,仍是 _backfill_relation_embeddings 等
        维护路径的正确语义 — 全库补全向量必须看到每一条关系)。
        relation_ids=[...]: 只 JOIN 这些 id(P0-1/2 候选界定 — 热路径调用方先按
        向量 sim 定好候选集,这里只 hydrate 需要打分的那几行文本),chunk 在
        `_IN_CHUNK` 防止超 SQLite 变量数上限;空列表直接返回 []。"""
        from app.services.kg.edge_schema import is_queryable_edge_pair
        from app.services.retrieval import relation_embed_text, _payload_text
        if relation_ids is not None:
            if not relation_ids:
                return []
        rows = self.knowledge.relation_context_rows(
            db, notebook_id, relation_ids, batch_size=self._IN_CHUNK,
        )
        out = []
        for r in rows:
            if not is_queryable_edge_pair(r["et"], r["st"], r["tt"]):
                continue
            spans = [e.get("quoted_span", "") for e in json.loads(r["ev"] or "[]")
                     if isinstance(e, dict)]
            src_name = _payload_text(json.loads(r["sp"] or "{}"))[:80]
            tgt_name = _payload_text(json.loads(r["tpl"] or "{}"))[:80]
            out.append({
                "id": r["id"], "source_object_id": r["s"], "target_object_id": r["t"],
                "edge_type": r["et"],
                "review_status": r["review_status"],
                "text": relation_embed_text(src_name, r["et"], tgt_name, spans),
            })
        return out

    def _lexical_knn_allowed(
        self, notebook_id: str, allowed_source_ids=None
    ) -> bool:
        """KNN 路由判定——必须在调用方打开连接**之前**调用。

        `notebook_copy_stats` 的版本信号会自己开一条池化连接;在
        `with self._connect()` 里面算这个判定 = 嵌套获取,报告多节并发时会把
        连接池坐死(`_lexical_object_hits` docstring 里整段警告的正是这个形态,
        `_lexical_corpus_langs` 是同一约定的先例)。flag 关闭时第一行就短路,
        零查询;开启时每次判定付一次 version-signal SELECT(memo 只挡后面的
        facts 重算)——这是缓存版本检查的既有代价,不是新形态。

        三个条件缺一即 False(SQL 逐字回到 legacy):
        ① 开关开;② run 未按来源收窄——受限 run 的词法是唯一候选来源,且
        scoped 形状无 bench;③ 库的 nodes+chunks ≥ `postgres_lexical_knn_min_rows`
        ——GiST 索引没有 notebook 键,KNN 是**全库**距离序逐行过滤,只有主导
        份额的库才划算;小份额库要在别人的行里翻找自己的 67 条,反而丢掉复合
        GIN 快路径。判定链上的任何异常按 False 收(fail closed 到 legacy——
        旧路径永远是安全出口)。
        """
        if not self.settings.postgres_lexical_knn_enabled:
            return False
        # 能力声明先于规模判定:SQLite 适配器声明不具备(hint 在 FTS5 上无事可
        # 做),这里就零成本短路——发行默认后端不为 PG 专属特性付版本查询/冷缓存
        # 五条聚合(codex #464 R1 P2)。问的是「绑定的适配器能不能」而非后端是
        # 什么,service 不判 dialect 的红线不破;缺声明的桩按不具备收(fail
        # closed 到 legacy)。
        if not getattr(self.knowledge, "lexical_knn_capable", False):
            return False
        if allowed_source_ids is not None:
            return False
        # 登记接受的取舍:PostgreSQL 侧每次未收窄探针为 sizing 付一条已索引的
        # 单行版本校验查询(亚毫秒,与其后的词法 LATERAL 相比是噪声;master 的
        # 无 ANN 兜底分支本就无条件付它)。曾为消掉它做过零连接的探测缓存 hint,
        # 连续三轮评审各暴露一个真缺陷(未探测表被进程级 absence 否决/瞬态探测
        # 失败被固化/热路径双探测),按「简单正确 > 省一条小查询」撤回——见
        # search.py 探测缓存旁的登记,别在这里重新引入。
        try:
            size = self.notebook_copy_stats(notebook_id).get("size") or {}
            rows = int(size.get("nodes") or 0) + int(size.get("chunks") or 0)
        except Exception:  # noqa: BLE001 — 判定失败必须留在 legacy
            return False
        return rows >= int(self.settings.postgres_lexical_knn_min_rows)

    def _lexical_object_hits(
        self,
        db: object,
        notebook_id: str,
        query: str,
        recall: int,
        *,
        site: str,
        corpus_langs,
        allowed_source_ids=None,
        allow_knn: bool = False,
    ) -> list[dict]:
        """Fail-open bounded lexical object recall shared by KG and relations.

        `corpus_langs` is a REQUIRED parameter rather than something probed
        here, because this helper is always handed an already-open `db`.
        Probing inside it would acquire a second connection on a cold language
        cache, and concurrent report sections holding the pool with their outer
        connections would then block on that nested acquire — a pool timeout
        that the `except` below would swallow into an empty lexical arm. That is
        the exact failure this whole gate exists to remove, re-entered through
        the pool instead of the statement timeout. Every caller therefore probes
        BEFORE it opens its connection, which is also what the chunk-side call
        sites have always done.

        `allow_knn` 同理是**参数**而不是在这里计算:它的判定要读
        `notebook_copy_stats`,而那条链(version_signal)会自己开一条池化连接——
        在持连接时算它,就是上面整段警告的那个「嵌套获取坐死连接池」。调用方在
        打开连接之前用 `_lexical_knn_allowed` 算好传进来(与 `corpus_langs`
        同一形态、同一理由)。
        """
        try:
            if allowed_source_ids is None:
                return self.knowledge.fts_search(
                    db, notebook_id, query, k=recall, corpus_langs=corpus_langs,
                    allow_knn=allow_knn,
                )
            return self.knowledge.fts_search(
                db, notebook_id, query, k=recall,
                allowed_source_ids=allowed_source_ids,
                corpus_langs=corpus_langs,
            )
        except Exception as exc:  # noqa: BLE001 — lexical failure keeps ANN usable
            self.event_log.emit({
                "kind": "lexical_retrieval_failed",
                "notebook_id": notebook_id,
                "site": site,
                "error_type": type(exc).__name__,
            })
            return []

    def _relation_lexical_candidate_ids(
        self, db: object, notebook_id: str, query: str, recall: int,
        *, corpus_langs, allow_knn: bool = False,
    ) -> list[str]:
        """Map lexical KG endpoint hits to a bounded relation candidate set.

        Forwards `corpus_langs` and `allow_knn` for the same reason
        `_lexical_object_hits` requires them: this helper also runs on a
        caller-owned connection, so both verdicts are computed before it.
        """
        object_hits = self._lexical_object_hits(
            db, notebook_id, query, recall, site="relation_endpoint_fts",
            corpus_langs=corpus_langs, allow_knn=allow_knn,
        )
        if not object_hits:
            return []
        try:
            rows = self.knowledge.relation_id_rows_for_objects(
                db,
                notebook_id,
                [hit["object_id"] for hit in object_hits],
                recall,
                batch_size=self._IN_CHUNK,
            )
        except Exception as exc:  # noqa: BLE001 — relation ANN remains usable
            self.event_log.emit({
                "kind": "lexical_retrieval_failed",
                "notebook_id": notebook_id,
                "site": "relation_endpoint_probe",
                "error_type": type(exc).__name__,
            })
            return []
        return [row["id"] for row in rows]

    def _relation_ann_candidates(self, notebook_id, query_vector, idx, recall) -> dict:
        """ANN 核候选(idx.relation_ann_path=relation_embeddings)⊕ delta 关系暴力。
        镜像 _kg_object_candidates,relation 版。返回 {relation_id: sim∈[0,1]}。
        词法端点候选由调用方另行并入,避免给 lex-only id 伪造 0 语义分。
        fail-open 返回 {} 让上层退回词法/全量矩阵/guard 路径。"""
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
                    self._note_model_error(
                        "relation_ann_query",
                        "",
                        exc,
                        service="embedding",
                    )
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
                            drows.extend(self.embeddings.relation_delta_rows(
                                db, notebook_id, batch,
                            ))
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    for rid, s in (query_sims(query_vector, d_ids, d_mat) if d_ids else {}).items():
                        sims[rid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
                self._note_model_error(
                    "relation_ann_delta",
                    "",
                    exc,
                    service="embedding",
                )
        return sims
    def _retrieve_relations_scored(self, notebook_id: str, query: str) -> List["RetrievedRelation"]:
        """对 notebook 关系按 query 打分(关键词 + 关系索引语义)。镜像 _retrieve_scored;
        关系矩阵是独立索引(dual-index 分离)。

        relation-ann task:候选界定优先级从高到低——
          ① relations 表空 → 早退 [](原有语义不变)。
          ② 持久化 relation ANN(self scale index allow_stale=True 且
             has_relation_ann)存在 → (ANN 核 ⊕ delta 暴力)∪词法命中的 KG
             端点相邻关系,只 hydrate 最多 2K 个候选文本做关键词+语义融合打分。
             词法候选不伪造 0 语义分,仍按 keyword-only 归一化。knn_query 与
             词法窗口的 K 均沿用 relation_recall。这是大库常态路径——ANN
             侧路让下面的冷矩阵守卫从常态退位为「无 ANN 大库」的最后兜底。
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

        def live_relations(rows: List[dict]) -> List[dict]:
            return [row for row in rows if row["review_status"] != "rejected"]

        # 语言探针必须在拿外层连接**之前**跑:冷缓存时它自己要开一条连接,
        # 在这条 `with` 里面探测 = 嵌套获取,并发时会把连接池坐死(见
        # `_lexical_object_hits` 的说明)。与三个 chunk 调用点同一形态。
        # KNN 判定同一约定、同一理由(它要读 notebook_copy_stats 的版本信号)。
        corpus_langs = self._lexical_corpus_langs(notebook_id)
        allow_knn = self._lexical_knn_allowed(notebook_id)
        with self._connect() as db:
            if not self.knowledge.relation_exists(db, notebook_id):
                return []

            # ── 分支②: 持久化 relation ANN ⊕ delta(大库常态路径)─────────
            # 必须先于下面的冷矩阵守卫:ANN 候选界定本身 O(bounded)、不加载
            # 全量矩阵,已建索引的大库不该被守卫拦下。embed 只在 ANN 真实
            # 存在时才发生(守卫路径保持 master 原语义:守卫命中时零 embed)。
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None and getattr(idx, "relation_ann_labels", None):
                query_vector = self._embed_query(query)
                cand_sims: dict[str, float] = {}
                if query_vector is not None:
                    cand_sims = self._relation_ann_candidates(
                        notebook_id, query_vector, idx, self.settings.relation_recall)
                lexical_ids = self._relation_lexical_candidate_ids(
                    db, notebook_id, query, self.settings.relation_recall,
                    corpus_langs=corpus_langs, allow_knn=allow_knn,
                )
                candidate_ids = list(dict.fromkeys([*cand_sims, *lexical_ids]))
                if candidate_ids:
                    relations = live_relations(
                        self._relations_with_names(
                            db, notebook_id, relation_ids=candidate_ids
                        )
                    )
                    return score_relations(
                        query,
                        relations,
                        query_vector=query_vector,
                        relation_sims=cand_sims,
                        downweight_edges=self.settings.kg_about_downweight_enabled,
                    )
                # fail-open(embed/FTS 失败或均为空)→ 继续走守卫/全量路径。

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
                relations = live_relations(
                    self._relations_with_names(db, notebook_id)
                )
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
                relations = live_relations(
                    self._relations_with_names(
                        db, notebook_id, relation_ids=top_ids
                    )
                )
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
                    self._note_model_error(
                        "kg_obj_ann",
                        "",
                        exc,
                        service="embedding",
                    )
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
                            drows.extend(self.embeddings.knowledge_delta_rows(
                                db, notebook_id, batch,
                            ))
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    for oid, s in (query_sims(query_vector, d_ids, d_mat) if d_ids else {}).items():
                        sims[oid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
                self._note_model_error(
                    "kg_obj_delta",
                    "",
                    exc,
                    service="embedding",
                )
        return sims
    def _knowhow_object_types(self, notebook_id: str) -> tuple:
        """Distinct Knowhow cell-KO object types (each a table COLUMN NAME — a
        dynamic type projected by ``KnowhowProjector``) currently usable in the
        notebook. The store scopes these rows through
        ``knowhow_tables.hidden_source_id``, rather than treating every custom
        schema type as a Knowhow column. Empty tuple ⇒ the notebook has
        no Knowhow graph content ⇒ every caller no-ops. Only ever reached on the
        flag-on branch (see ``_scored_type_scope``).

        The result is version-cached because one reasoning request calls this
        path for several subqueries. Fixed names are intentionally retained:
        free-form table columns can legally be named ``claim``/``formula`` etc.,
        and those structural-only KOs still need the chunk-vector bridge.
        """
        with self._connect() as db:
            version = (
                "knowhow_types",
                *self.unified_kg.graph_seq_row(db, notebook_id),
            )

            def _load():
                rows = self.queries.knowhow_knowledge_type_rows(
                    db, notebook_id, USABLE_STATUSES
                )
                return tuple(row["object_type"] for row in rows)

            return self._vector_cache.get(
                f"{notebook_id}:knowhow_types", version, _load)

    def _scored_type_scope(self, notebook_id: str, types) -> tuple[List[str], tuple]:
        """Return ``(scored types, Knowhow-owned types)`` from one scoped read."""
        base = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        if not self.settings.knowhow_kg_node_retrieval_enabled:
            return base, ()
        knowhow_types = self._knowhow_object_types(notebook_id)
        if not knowhow_types:
            return base, ()
        allowed = set(_KG_TYPES) | set(knowhow_types)
        source = list(types) if types else [*_KG_TYPES, *(
            t for t in knowhow_types if t not in _KG_TYPES
        )]
        return [t for t in source if t in allowed], knowhow_types

    def _scored_types(self, notebook_id: str, types) -> List[str]:
        """The object_types ``_retrieve_scored`` will fetch+score. FLAG OFF (or a
        notebook with no knowhow types) ⇒ BYTE-IDENTICAL to the historical line:
        caller ``types`` (or all four ``_KG_TYPES`` by default) filtered to
        ``_KG_TYPES``. FLAG ON + knowhow present ⇒ the allowed set widens to
        ``_KG_TYPES ∪ knowhow-types`` and the default (no caller ``types``)
        unions the knowhow types in too; a caller-supplied list is still just
        filtered against the (now wider) allowed set. ``_knowhow_object_types``
        is only touched on the flag-on branch, so flag-off issues no new query."""
        return self._scored_type_scope(notebook_id, types)[0]

    def _knowhow_ko_candidates(self, db, notebook_id: str, query: str,
                               query_vector) -> dict:
        """Gate 0: ``{ko_id: sim}`` for knowhow cell KOs whose
        hidden-source chunk vectors match the query — the semantic signal a
        structural-only KO (no ``knowledge_embeddings`` row) otherwise lacks.

        The normalized chunk matrix and chunk→KO reverse map are cached by the
        notebook's monotonic KG sequence, scoped chunk-vector generation, and
        runtime embedding dimension. The vector generation matters because a
        vector-only repair intentionally does not mutate KG state. A
        reasoning request may issue several subqueries, but it reads/builds the
        scoped Knowhow corpus once. KG mutations also explicitly evict the key,
        covering same-version delete/reingest collisions. ``query`` is accepted
        for signature stability / future lexical use; gate 0 is semantic today.
        """
        from app.services.knowhow.kg_node_bridge import (
            build_knowhow_bridge_index,
            query_knowhow_bridge_index,
        )
        from app.services.vector_index import resolve_runtime_dim
        if query_vector is None:
            return {}
        runtime_dim = resolve_runtime_dim(self.settings)
        vector_version = self.chunks.knowhow_bridge_version_row(db, notebook_id)
        version = (
            "knowhow_bridge",
            *self.unified_kg.graph_seq_row(db, notebook_id),
            vector_version["c"],
            vector_version["ts"],
            runtime_dim,
        )

        def _load():
            chunk_rows = self.chunks.knowhow_chunk_rows(db, notebook_id)
            chunk_to_element = {}
            for row in chunk_rows:
                element_ids = json.loads(row["element_ids"] or "[]")
                if element_ids:
                    chunk_to_element[row["id"]] = element_ids[0]
            vector_rows: list = []
            for batch in self._in_batches(list(chunk_to_element)):
                vector_rows.extend(self.embeddings.vector_rows_for_ids(
                    db, notebook_id, "chunk_embeddings", "chunk_id", batch))
            elements = self.sources.evidence_elements(
                list(dict.fromkeys(chunk_to_element.values())))
            return build_knowhow_bridge_index(
                chunk_to_element,
                vector_rows,
                elements,
                runtime_dim=runtime_dim,
            )

        index = self._vector_cache.get(
            f"{notebook_id}:knowhow_bridge", version, _load)
        return query_knowhow_bridge_index(
            index, query_vector, recall=self.settings.chunk_recall)

    def _retrieve_scored(self, notebook_id: str, query: str,
                         types: Optional[Iterable[str]] = None,
                         w_keyword: float = W_KEYWORD,
                         w_semantic: float = W_SEMANTIC, *,
                         allowed_source_ids=None) -> List[RetrievedKnowledge]:
        """Score KG objects of `types` (default all 4 _KG_TYPES) for `query`,
        returning RetrievedKnowledge sorted by fused relevance desc. Shared by
        the reasoning retriever's tools; `w_keyword`/`w_semantic` carry the
        per-sub-query `prefer` bias."""
        # ask_stage 埋点(纯观测):阶段墙钟拆解,生产诊断 20-30s 级检索用。
        t0 = time.perf_counter()
        # gate i (type widening): flag-off / non-knowhow ⇒ historical filter,
        # byte-identical. flag-on + knowhow present ⇒ column-name types join
        # the fetch set (see _scored_types).
        type_list, knowhow_types = self._scored_type_scope(notebook_id, types)
        # gate 0/i are live only when the widening actually surfaced a knowhow
        # (column-name) type — exactly "flag on AND this notebook has knowhow
        # graph content" (or a caller explicitly asked for one). Flag off /
        # non-knowhow ⇒ empty ⇒ pure no-op, no bridge query, no injection.
        from app.services.source_scope import scoped_allowed_source_ids

        allowed_source_ids = scoped_allowed_source_ids(
            notebook_id, allowed_source_ids
        )
        source_filter = (
            tuple(dict.fromkeys(str(value) for value in allowed_source_ids if value))
            if allowed_source_ids is not None else None
        )
        if source_filter is not None and not source_filter:
            return []
        knowhow_on = bool(set(type_list) & set(knowhow_types)) and source_filter is None
        query_vector = self._embed_query(query)
        t_embed = time.perf_counter()
        # indexed 时用 (ANN 核 ⊕ delta)∪FTS 取有界候选。candidate_filter 与
        # cand_sims 分开:lex-only id 进入 hydration,但不伪造 0 语义分。
        cand_sims: Optional[dict[str, float]] = None
        candidate_filter: Optional[set[str]] = None
        lexical_candidate_count = 0
        if source_filter is not None:
            # Selected-source runs never enter a notebook-wide fallback.  The
            # indexed provenance table gates completeness, while lexical SQL
            # applies the source predicate before LIMIT.  This path deliberately
            # skips the notebook-wide ANN until artifacts carry an aligned
            # source partition, so out-of-scope labels cannot occupy its top-K.
            # 语言探针在外层连接之前(冷缓存时它自己要开连接;嵌套获取会在并发
            # 下坐死连接池)。这条是**选定来源**分支:词法是它唯一的候选来源,
            # 一律不过滤(见 `_lexical_corpus_langs` 的 source_scoped 说明)。
            corpus_langs = self._lexical_corpus_langs(
                notebook_id, source_scoped=True)
            with self._connect() as db:
                if not self.knowledge.source_index_backfilled(db, notebook_id):
                    return []
                lexical_hits = self._lexical_object_hits(
                    db, notebook_id, query, self.settings.chunk_recall,
                    site="kg_source_scoped_fts",
                    corpus_langs=corpus_langs,
                    allowed_source_ids=source_filter,
                )
            lexical_ids = [hit["object_id"] for hit in lexical_hits]
            lexical_candidate_count = len(lexical_ids)
            cand_sims = {}
            candidate_filter = set(lexical_ids)
            if not candidate_filter:
                return []
        elif query_vector is not None:
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is None:
                # 无 ANN 核 → 本次退回全量暴力(O(N))。O(1) once-set 兜底:大库应
                # 自动建索引,避免长期停留在暴力回退稳态。fail-open,不影响本次检索。
                try:
                    self.maybe_auto_index(notebook_id)
                except Exception:
                    pass
            elif getattr(idx, "ann_labels", None):
                ann_sims = self._kg_object_candidates(
                    notebook_id, query_vector, idx, self.settings.chunk_recall)
                # `source_filter is None` on this branch; spelled out rather
                # than assumed so restructuring cannot silently start gating a
                # source-scoped run.
                corpus_langs = self._lexical_corpus_langs(
                    notebook_id, source_scoped=source_filter is not None)
                # KNN 判定与语言探针同一约定:连接之前算(它读 copy-stats 的
                # 版本信号,自己要开连接)。source_filter 显式传入,与上面同理。
                allow_knn = self._lexical_knn_allowed(
                    notebook_id, allowed_source_ids=source_filter)
                with self._connect() as db:
                    lexical_hits = self._lexical_object_hits(
                        db,
                        notebook_id,
                        query,
                        self.settings.chunk_recall,
                        site="kg_ann_fts",
                        corpus_langs=corpus_langs,
                        allow_knn=allow_knn,
                    )
                lexical_ids = [hit["object_id"] for hit in lexical_hits]
                lexical_candidate_count = len(lexical_ids)
                if ann_sims or lexical_ids:
                    cand_sims = ann_sims
                    candidate_filter = set(ann_sims) | set(lexical_ids)
                # both fail/empty → candidate_filter=None → existing fail-open.
        if candidate_filter is None and not self.notebook_copy_stats(notebook_id)["copyable"]:
            # 大库拿不到 ANN 候选(未建索引/ANN 打不开/维度失配/embed 失败)——
            # 一个原则:绝不全量暴力(全表 json 解析 + 全量分词 + GB 级矩阵,
            # 49 万对象生产实测数十分钟)。FTS 词法有界兜底:kg_objects_fts 覆盖
            # 全部对象(含 delta),候选的语义分仍由下方按候选 evidence 元素向量
            # 有界补充。FTS 空 → [](与 relation 侧冷矩阵守卫同一 fail-open 出口)。
            corpus_langs = self._lexical_corpus_langs(
                notebook_id, source_scoped=source_filter is not None)
            allow_knn = self._lexical_knn_allowed(
                notebook_id, allowed_source_ids=source_filter)
            with self._connect() as db:
                lex = self._lexical_object_hits(
                    db,
                    notebook_id,
                    query,
                    self.settings.chunk_recall,
                    site="kg_large_fallback_fts",
                    corpus_langs=corpus_langs,
                    allow_knn=allow_knn,
                )
            self.event_log.emit({
                "kind": "kg_bruteforce_refused", "notebook_id": notebook_id,
                "site": "_retrieve_scored", "lexical_candidates": len(lex),
            })
            if not lex:
                return []
            cand_sims = {}
            candidate_filter = {h["object_id"] for h in lex}
            lexical_candidate_count = len(candidate_filter)
        t_ann = time.perf_counter()
        from app.services.vector_index import query_sims, build_matrix
        with self._connect() as db:
            # ── gate 0: knowhow cell-KO semantic sidecar ─────────────────────
            # Reverse-lookup the query against ONLY the hidden knowhow source's
            # chunk vectors → {ko_id: sim}. Computed once, folded into whichever
            # scoring path this retrieval is on. Empty (no cost) unless knowhow_on.
            knowhow_sims = (
                self._knowhow_ko_candidates(db, notebook_id, query, query_vector)
                if knowhow_on else {})
            if knowhow_sims and candidate_filter is not None:
                # Bounded path: union into the candidate set BEFORE id_filter so
                # the KOs are fetched, and (via knowledge_sims=cand_sims below)
                # pick up their semantic score.
                for ko_id, sim in knowhow_sims.items():
                    candidate_filter.add(ko_id)
                    cand_sims[ko_id] = max(cand_sims.get(ko_id, 0.0), sim)
            id_filter = candidate_filter
            kg_objs = {t: self._knowledge_objects(db, notebook_id, t, id_filter=id_filter)
                       for t in type_list}
            all_kg_objs = [o for objs in kg_objs.values() for o in objs]
            if source_filter is not None:
                allowed_sources = set(source_filter)
                for obj in all_kg_objs:
                    obj["evidence"] = [
                        ev for ev in obj.get("evidence", [])
                        if ev.source_id in allowed_sources
                    ]
                all_kg_objs = [obj for obj in all_kg_objs if obj["evidence"]]
                allowed_object_ids = {obj["id"] for obj in all_kg_objs}
                for object_type in tuple(kg_objs):
                    kg_objs[object_type] = [
                        obj for obj in kg_objs[object_type]
                        if obj["id"] in allowed_object_ids
                    ]
            # P0-A: candidate_filter is not None ⟺ bounded ANN∪FTS candidate path
            # (≤2*chunk_recall objects, plus bounded knowhow bridge) — skip the COUNT probe
            # and the process-wide cache (which the candidate-set-varies-per-query
            # path never hit anyway) and build the token sets directly for this batch.
            token_sets = self._keyword_token_sets(
                db, notebook_id, all_kg_objs, bounded=candidate_filter is not None)
            # candidate object ids for this retrieval call
            candidate_ids = {o["id"] for o in all_kg_objs}
            # 孤立节点降权: 有边节点集合。降权仅作用于 score(排序),不进 relevance([0,1]/tau 守恒)。
            if candidate_filter is not None:
                # 有界:每个候选只做带索引的 EXISTS 短路探测；绝不能拉取完整
                # 邻接边，否则一个高出度 hub 就能把几十个候选的 hydration
                # 放大成数百万行。candidate_ids 仍按参数上限分批。
                connected_ids: set[str] = set()
                for batch in self._in_batches(candidate_ids):
                    connected_ids.update(
                        row["object_id"]
                        for row in self.knowledge.relation_connected_object_ids(
                            db, notebook_id, batch,
                        )
                    )
                elem_id_set = {ev.element_id for o in all_kg_objs
                               for ev in o.get("evidence", []) if getattr(ev, "element_id", None)}
                # elem_id_set 同理分批;各批互不相交(_in_batches 去重保序切片),
                # extend 不会产生跨批重复行。
                erows = []
                for batch in self._in_batches(elem_id_set):
                    erows.extend(self.embeddings.vector_rows_for_ids(
                        db, notebook_id, "element_embeddings", "element_id", batch,
                    ))
            else:
                rel_rows = self.knowledge.relation_endpoint_rows(db, notebook_id)
                connected_ids = set()
                for row in rel_rows:
                    connected_ids.add(row["source_object_id"])
                    connected_ids.add(row["target_object_id"])
            isolated_ids: set = candidate_ids - connected_ids
            if candidate_filter is not None:
                knowledge_sims = cand_sims
                e_ids, e_mat = build_matrix((r["vid"], r["vector"]) for r in erows)
                element_sims = query_sims(query_vector, e_ids, e_mat) if e_ids else {}
            else:
                elem_ids, elem_mat = self._vector_matrix(db, notebook_id, "element_embeddings", "element_id")
                kn_ids, kn_mat = self._vector_matrix(db, notebook_id, "knowledge_embeddings", "object_id")
                element_sims = query_sims(query_vector, elem_ids, elem_mat) if query_vector else None
                knowledge_sims = query_sims(query_vector, kn_ids, kn_mat) if query_vector else None
            if knowhow_sims and candidate_filter is None and knowledge_sims is not None:
                # Full-scan path: knowhow KOs are absent from the
                # knowledge_embeddings matrix (structural-only projection), so
                # their semantic score comes solely from gate 0. Merge AFTER the
                # matrix build; do NOT flip cand_sims to non-None (that would
                # wrongly bound doc-KO retrieval). gate i already put their types
                # in type_list, so _knowledge_objects(id_filter=None) fetched them.
                for ko_id, sim in knowhow_sims.items():
                    knowledge_sims[ko_id] = max(knowledge_sims.get(ko_id, 0.0), sim)
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
            scored = fold_by_canonical(
                scored,
                self._candidate_cluster_map(
                    notebook_id, (hit.object_id for hit in scored)
                ),
            )
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
            "ann_gated": candidate_filter is not None,
            "lexical_candidates": lexical_candidate_count,
        })
        return scored
    def _federated_retrieve_impl(
        self,
        active_notebook_id: str,
        query: str,
        types: Optional[Iterable[str]] = None,
        w_keyword: float = W_KEYWORD,
        w_semantic: float = W_SEMANTIC,
        *,
        allowed_source_keys=None,
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
        with self._connect() as db:
            notebook_ids, tier_map = self.notebooks.participant_tiers(
                db, active_notebook_id,
            )

        all_hits: List[RetrievedKnowledge] = []
        sources_by_notebook = None
        if allowed_source_keys is not None:
            sources_by_notebook = {}
            for source_notebook_id, source_id in allowed_source_keys:
                if source_notebook_id and source_id:
                    sources_by_notebook.setdefault(source_notebook_id, []).append(source_id)
        from app.services.source_scope import (
            notebook_in_scope, scoped_allowed_source_ids,
        )

        for nid in notebook_ids:
            # Library dimension, stated in the loop that owns it. Belt and
            # braces: `scoped_allowed_source_ids` below already answers `()`
            # for an unchecked library and this loop already `continue`s on a
            # falsy allow-list, so no behavioural test can tell the two apart
            # -- deleting either one alone changes nothing observable (deleting
            # BOTH reopens the library). It stays because the library question
            # is the one this loop is being asked, and reading it off the
            # source-allow-list's emptiness is a `None`-vs-`()` subtlety one
            # refactor away from silently reopening an unchecked library.
            if not notebook_in_scope(nid):
                continue
            explicit_allowed = (
                sources_by_notebook.get(nid)
                if sources_by_notebook is not None else None
            )
            if sources_by_notebook is not None and not explicit_allowed:
                continue
            allowed = scoped_allowed_source_ids(nid, explicit_allowed)
            if allowed is not None:
                if not allowed:
                    continue
                hits = self._retrieve_scored(
                    nid, query, types=types, w_keyword=w_keyword,
                    w_semantic=w_semantic, allowed_source_ids=allowed,
                )
            else:
                hits = self._retrieve_scored(
                    nid, query, types=types, w_keyword=w_keyword,
                    w_semantic=w_semantic,
                )
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)

        # Pure-relevance ordering (tier-blind); base wins ONLY on an exact score
        # tie (True > False). Zero magnitude — no constant is added to any score.
        all_hits.sort(key=lambda it: (it.score, getattr(it, "tier", "") == "base"), reverse=True)
        return all_hits
    def _federated_retrieve_relations_impl(self, active_notebook_id: str,
                                     query: str) -> List["RetrievedRelation"]:
        """跨 {base notebook(s)} ∪ {active} 检索关系,逐本 .notebook_id/.tier 标注。
        每本走 _retrieve_relations_scored(同尺),合并按 score 降序。"""
        with self._connect() as db:
            notebook_ids, tier_map = self.notebooks.participant_tiers(
                db, active_notebook_id,
            )
        all_hits: List["RetrievedRelation"] = []
        from app.services.source_scope import notebook_in_scope

        for nid in notebook_ids:
            # Library dimension. The active-notebook check below is
            # `nid == active_notebook_id`-guarded, so it never even looks at a
            # participant library -- a run that unchecked one would otherwise
            # keep searching it and rely entirely on the result boundary.
            if not notebook_in_scope(nid):
                continue
            # Relation ANN/FTS artifacts are not source partitioned yet.  A
            # truly narrowed run skips this active-notebook channel before I/O;
            # an all-selected frozen snapshot keeps the normal graph path and
            # receives result-boundary evidence filtering.  Base participants
            # remain independent and searchable.
            if (
                nid == active_notebook_id
                and self._unsafe_source_scope_restricted(active_notebook_id)
            ):
                continue
            hits = self._retrieve_relations_scored(nid, query)
            tier = tier_map.get(nid, "personal")
            for h in hits:
                h.notebook_id = nid
                h.tier = tier
            all_hits.extend(hits)
        all_hits.sort(key=lambda it: it.score, reverse=True)
        return all_hits

    def _federated_retrieve_elements_impl(
        self, active_notebook_id: str, query: str, *,
        allowed_source_keys, limit: int = 8,
    ) -> List[RetrievedElement]:
        """Search raw elements only in explicitly authorized participants."""
        with self._connect() as db:
            notebook_ids, _tier_map = self.notebooks.participant_tiers(
                db, active_notebook_id
            )
        sources_by_notebook: dict[str, list[str]] = {}
        for notebook_id, source_id in allowed_source_keys:
            if notebook_id and source_id:
                sources_by_notebook.setdefault(notebook_id, []).append(source_id)
        from app.services.source_scope import notebook_in_scope

        hits: List[RetrievedElement] = []
        for notebook_id in notebook_ids:
            # Library dimension, and the last point at which an element's
            # origin library is still known: `RetrievedElement` has no
            # notebook_id field, so `filter_retrieval_items(..., "element",
            # ...)` can only ever judge one against the ACTIVE notebook.
            # Correctness downstream still holds without this line (the inner
            # `_retrieve_elements` intersects the same ceiling and comes back
            # empty), but only after paying for the search -- this is what
            # makes an unchecked library free rather than merely harmless.
            if not notebook_in_scope(notebook_id):
                continue
            source_ids = sources_by_notebook.get(notebook_id)
            if not source_ids:
                continue
            hits.extend(self._retrieve_elements(
                notebook_id, query, limit=limit,
                allowed_source_ids=source_ids,
            ))
        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]
    def _retrieve_neighbors(self, notebook_id: str, object_id: str,
                            edge_type: Optional[str] = None,
                            direction: str = "both") -> NeighborExpansion:
        """1-hop graph neighbours of `object_id` as RetrievedKnowledge with
        placeholder relevance=0 (final relevance unified by run() via the
        original question). Honours edge_type filter; direction out=object as
        source, in=as target, both=either.

        预算的单位是**唯一合格邻居**(每方向至多
        `REASONING_NEIGHBOR_EXPAND_LIMIT` 个),不是关系行:一个邻居常有多条重复/
        佐证关系,还有一部分行会被 `is_queryable_edge_pair` 判为不可查。按行截断
        会让这些行吃掉预算——展开数远少于配置值,而靠后的合法邻居被整个略过,且
        `visited` 禁止重复展开、这一轮丢掉就再也捡不回来。

        对象状态的合格性(`USABLE_STATUSES`)同样**下推到 SQL、落在 LIMIT 之前**:
        事后再丢弃 deprecated 之类的对象,等于让它们白占有界读取窗口,行序靠后的
        可用邻居被整个漏掉——相对旧的无界行为那是回归。该查询本就 JOIN 了 src/tgt
        两张 knowledge_objects,加谓词零额外成本。

        SQL 侧仍是有界读取,只是读取界放到 `(limit+1) × _NEIGHBOR_ROW_OVERSCAN`
        (仓库既有的「有界过扫描」先例,见集合枚举的 `max_rows × 4`)——过扫描仍要
        吸收重复关系与 `is_queryable_edge_pair` 这层 Python 侧过滤。hydrate 仍按
        ≤2×limit 有界:病态枢纽节点(百万边)此前会把自己的全部邻接边取回、再整批
        hydrate 对应 knowledge_objects 行。截断与否随结果返回
        (`NeighborExpansion.truncated`),由调用方披露,不静默。"""
        # Targeted index hits (idx_knowledge_relations_nb_source/_nb_target)
        # instead of loading every notebook edge: O(neighbours), not O(E).
        neighbour_ids: set = set()
        truncated = False
        limit = max(1, int(self.settings.reasoning_neighbor_expand_limit))
        read_cap = (limit + 1) * _NEIGHBOR_ROW_OVERSCAN
        from app.services.kg.edge_schema import is_queryable_edge_pair

        def _bounded_neighbours(rows, endpoint_key: str) -> List[str]:
            """按索引序过滤 + 去重,取前 `limit` 个唯一合格邻居。

            两种情况都判截断,且语义都并进同一条披露:
              * 出现第 `limit+1` 个唯一合格邻居 —— 确定还有;
              * 读满了过扫描窗口 —— 可能还有(窗口外没看过,不能假装看全了)。
            """
            nonlocal truncated
            rows = list(rows)
            if len(rows) >= read_cap:
                truncated = True
            picked: List[str] = []
            seen: set = set()
            for row in rows:
                if not is_queryable_edge_pair(
                    row["edge_type"], row["source_type"], row["target_type"]
                ):
                    continue
                neighbour = row[endpoint_key]
                if neighbour in seen:
                    continue
                if len(picked) >= limit:
                    truncated = True
                    break
                seen.add(neighbour)
                picked.append(neighbour)
            return picked

        with self._connect() as db:
            if direction in ("out", "both"):
                neighbour_ids.update(_bounded_neighbours(
                    self.knowledge.neighbor_ids(
                        db, notebook_id, object_id,
                        endpoint="source_object_id", edge_type=edge_type,
                        limit=read_cap, usable_statuses=USABLE_STATUSES,
                    ),
                    "target_object_id",
                ))
            if direction in ("in", "both"):
                neighbour_ids.update(_bounded_neighbours(
                    self.knowledge.neighbor_ids(
                        db, notebook_id, object_id,
                        endpoint="target_object_id", edge_type=edge_type,
                        limit=read_cap, usable_statuses=USABLE_STATUSES,
                    ),
                    "source_object_id",
                ))
            if not neighbour_ids:
                return NeighborExpansion([], truncated)
            # 保留(幂等的防御性过滤):status 已在 SQL 侧下推,这一层只在两处口径
            # 万一分叉时兜底,绝不是本轮筛选的主力。
            rows = self.knowledge.usable_object_rows_on(
                db, neighbour_ids, USABLE_STATUSES,
            )
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
        return NeighborExpansion(out, truncated)
    def _retrieve_elements(self, notebook_id: str, query: str,
                           limit: int = 8, *,
                           allowed_source_ids=None) -> List[RetrievedElement]:
        """Keyword+semantic search over raw source_elements (fallback layer 2)."""
        from app.services.source_scope import scoped_allowed_source_ids

        allowed_source_ids = scoped_allowed_source_ids(
            notebook_id, allowed_source_ids
        )
        if (allowed_source_ids is not None
                or not self.notebook_copy_stats(notebook_id)["copyable"]):
            # source_elements 没有独立 ANN；绝不恢复 17 万元素×4096 维的
            # 全表扫描。先走已有 chunk ANN/FTS 召回，再只按这些 chunk 的
            # element_ids 做有界 PK hydration，仍能返回精确 element_id。显式
            # 来源范围不论 notebook 是否 copyable 都走这条路：一个
            # 小文件仍可以产生大量 element/vector，所以 copyable 不是
            # 选定来源的候选行数上限。
            chunks, _ids, _matrix = self._retrieve_chunks(
                notebook_id, query, recall=max(limit * 4, limit),
                allowed_source_ids=allowed_source_ids,
            )
            elements = self._retrieve_elements_from_chunks(
                query, chunks, limit=limit
            )
            self.event_log.emit({
                "kind": "element_scoring_bounded", "notebook_id": notebook_id,
                "site": "_retrieve_elements", "reason": "large_notebook",
                "chunk_candidates": len(chunks),
                "element_hits": len(elements),
            })
            return elements
        query_vector = self._embed_query(query)
        with self._connect() as db:
            elements = (
                self._gather_elements(db, notebook_id, with_vectors=True)
                if allowed_source_ids is None
                else self._gather_elements(
                    db, notebook_id, with_vectors=True,
                    allowed_source_ids=allowed_source_ids,
                )
            )
        return score_elements(query, elements, query_vector, limit=limit)

    def _retrieve_elements_from_chunks(
        self, query: str, chunks, *, limit: int
    ) -> List[RetrievedElement]:
        """Hydrate and rank a bounded set of exact elements from chunk hits.

        Candidate ids are collected round-robin from relevance-ordered chunks so
        one large chunk cannot consume the whole budget.  At most ``limit * 8``
        (capped at 64) primary-key rows are read; there is no notebook-wide text
        or vector materialisation.
        """
        if limit <= 0 or not chunks:
            return []
        baseline, supplemental = partition_generated_question_chunks(chunks)
        cap = min(64, max(limit, limit * 8))

        def _candidate_ids(partition, budget, excluded=()):
            ordered = sorted(
                partition,
                key=lambda chunk: (
                    -float(getattr(chunk, "relevance", 0.0)
                           or getattr(chunk, "score", 0.0) or 0.0),
                    str(getattr(chunk, "chunk_id", "")),
                ),
            )
            selected: list[str] = []
            coarse_scores: dict[str, float] = {}
            unavailable = set(excluded)
            position = 0
            while len(selected) < budget:
                added = False
                for chunk in ordered:
                    element_ids = [
                        str(element_id or "")
                        for element_id in (
                            getattr(chunk, "element_ids", None) or []
                        )
                        if str(element_id or "") not in unavailable
                    ]
                    if position >= len(element_ids):
                        continue
                    element_id = element_ids[position]
                    if not element_id or element_id in coarse_scores:
                        continue
                    coarse_scores[element_id] = float(
                        getattr(chunk, "relevance", 0.0)
                        or getattr(chunk, "score", 0.0) or 0.0
                    )
                    selected.append(element_id)
                    added = True
                    if len(selected) >= budget:
                        break
                if not added:
                    break
                position += 1
            return selected, coarse_scores

        def _hydrate_and_rank(selected, coarse_scores, output_limit):
            if not selected or output_limit <= 0:
                return []
            hydrated = self.sources.evidence_elements(selected)
            source_ids = [
                str(row.get("source_id") or "") for row in hydrated.values()
                if row.get("source_id")
            ]
            metadata = self.sources.source_metadata(source_ids)
            candidates = []
            for element_id in selected:
                row = hydrated.get(element_id)
                if not row:
                    continue
                source_id = str(row.get("source_id") or "")
                candidates.append({
                    "element_id": element_id,
                    "source_id": source_id,
                    # Same naming rule as the citation cards these elements end
                    # up on (``source_display.source_display_title``).
                    "source_title": source_display_title(
                        metadata.get(source_id) or {}
                    ),
                    "location_label": str(row.get("location_label") or ""),
                    "element_type": str(row.get("element_type") or ""),
                    "text": str(row.get("text") or ""),
                })
            lexical = score_elements(
                query, candidates, limit=output_limit
            )
            seen = {item.element_id for item in lexical}
            by_id = {row["element_id"]: row for row in candidates}
            for element_id in selected:
                if len(lexical) >= output_limit:
                    break
                if element_id in seen or element_id not in by_id:
                    continue
                row = by_id[element_id]
                lexical.append(RetrievedElement(
                    element_id=element_id,
                    source_id=row["source_id"],
                    source_title=row["source_title"],
                    location_label=row["location_label"],
                    element_type=row["element_type"],
                    text=row["text"],
                    score=coarse_scores.get(element_id, 0.0),
                ))
            return lexical

        primary = baseline or supplemental
        selected_ids, coarse_scores = _candidate_ids(primary, cap)
        result = _hydrate_and_rank(selected_ids, coarse_scores, limit)
        if not baseline or not supplemental or len(result) >= limit:
            return result

        remaining_hydration = cap - len(selected_ids)
        if remaining_hydration <= 0:
            return result
        addition_ids, addition_scores = _candidate_ids(
            supplemental, remaining_hydration, selected_ids
        )
        additions = _hydrate_and_rank(
            addition_ids, addition_scores, limit - len(result)
        )
        return [*result, *additions]
    def _retrieve_chunks(
        self, notebook_id: str, query: str, recall: int = 0, *,
        allowed_source_ids=None,
    ):
        baseline = self._retrieve_chunks_baseline(
            notebook_id,
            query,
            recall,
            allowed_source_ids=allowed_source_ids,
        )
        return self._generated_question_supplement(
            notebook_id,
            query,
            baseline,
            allowed_source_ids=allowed_source_ids,
        )

    def _generated_question_supplement(
        self,
        notebook_id: str,
        query: str,
        baseline,
        *,
        allowed_source_ids=None,
    ):
        """Bounded low-recall supplement; shadow mode cannot alter results."""
        mode = self.settings.generated_question_index_mode
        scored, _ids, _matrix = baseline
        if mode == "off" or len(scored) >= self.settings.generated_question_trigger_hits:
            return baseline

        try:
            return self._generated_question_supplement_optional(
                notebook_id,
                query,
                baseline,
                mode=mode,
                allowed_source_ids=allowed_source_ids,
            )
        except Exception:  # noqa: BLE001 — optional recall must preserve baseline
            self._emit_generated_question_query_event({
                "kind": "chunk_question_index_query",
                "notebook_id": notebook_id,
                "mode": mode,
                "status": "failed_open",
                "baseline_hits": len(scored),
            })
            return baseline

    def _emit_generated_question_query_event(self, event: dict) -> None:
        """Question-index observability is itself fail-open and content-free."""
        try:
            self.event_log.emit({
                key: value for key, value in event.items()
                if key in _GENERATED_QUESTION_QUERY_EVENT_FIELDS
            })
        except Exception:  # noqa: BLE001 — telemetry never changes retrieval
            return

    def _generated_question_supplement_optional(
        self,
        notebook_id: str,
        query: str,
        baseline,
        *,
        mode: str,
        allowed_source_ids=None,
    ):
        """Evaluate the optional index behind one all-or-nothing boundary."""
        scored, _ids, _matrix = baseline

        from app.services.source_scope import scoped_allowed_source_ids
        from app.services.retrieval import (
            RetrievalSupport,
            add_chunk_supports,
            score_chunks,
        )
        from app.services.vector_index import build_matrix, top_k_sims

        allowed = scoped_allowed_source_ids(notebook_id, allowed_source_ids)
        scan_limit = self.settings.generated_question_max_scan_rows
        rows = self.chunks.question_index_rows(
            notebook_id,
            allowed_source_ids=allowed,
            limit=scan_limit + 1,
        )
        if len(rows) > scan_limit:
            self._emit_generated_question_query_event({
                "kind": "chunk_question_index_query",
                "notebook_id": notebook_id,
                "mode": mode,
                "status": "skipped_scan_limit",
                "baseline_hits": len(scored),
                "scan_limit": scan_limit,
            })
            return baseline
        if not rows:
            self._emit_generated_question_query_event({
                "kind": "chunk_question_index_query",
                "notebook_id": notebook_id,
                "mode": mode,
                "status": "evaluated",
                "baseline_hits": len(scored),
                "question_rows": 0,
                "matched_chunks": 0,
                "added_chunks": 0,
            })
            return baseline
        query_vector = self._embed_query(query)
        question_ids, question_matrix = build_matrix(
            (row["id"], row["vector"]) for row in rows
        )
        question_take = (
            self.settings.generated_question_recall
            * self.settings.generated_question_questions_per_chunk
        )
        nearest = top_k_sims(
            query_vector, question_ids, question_matrix, question_take
        )
        row_by_id = {str(row["id"]): row for row in rows}
        best_by_chunk: dict[str, float] = {}
        for question_id, similarity in nearest:
            row = row_by_id.get(question_id)
            if row is None:
                continue
            chunk_id = str(row["chunk_id"])
            best_by_chunk[chunk_id] = max(
                similarity, best_by_chunk.get(chunk_id, float("-inf"))
            )
        selected_chunk_ids = [
            chunk_id
            for chunk_id, _score in itertools.islice(
                sorted(best_by_chunk.items(), key=lambda item: item[1], reverse=True),
                self.settings.generated_question_recall,
            )
        ]
        if not selected_chunk_ids:
            self._emit_generated_question_query_event({
                "kind": "chunk_question_index_query",
                "notebook_id": notebook_id,
                "mode": mode,
                "status": "evaluated",
                "baseline_hits": len(scored),
                "question_rows": len(rows),
                "matched_chunks": 0,
                "added_chunks": 0,
            })
            return baseline
        chunks, _supp_ids, _supp_matrix = self._hydrate_chunk_candidates(
            selected_chunk_ids
        )
        supplemental = score_chunks(
            query,
            chunks,
            query_vector,
            best_by_chunk,
            limit=self.settings.generated_question_recall,
        )
        add_chunk_supports(supplemental, {
            chunk.chunk_id: [RetrievalSupport(
                "generated_question",
                "chunk",
                chunk.chunk_id,
                best_by_chunk.get(chunk.chunk_id),
            )]
            for chunk in supplemental
        })
        baseline_ids = {chunk.chunk_id for chunk in scored}
        added = sum(chunk.chunk_id not in baseline_ids for chunk in supplemental)
        self._emit_generated_question_query_event({
            "kind": "chunk_question_index_query",
            "notebook_id": notebook_id,
            "mode": mode,
            "status": "evaluated",
            "baseline_hits": len(scored),
            "question_rows": len(rows),
            "matched_chunks": len(supplemental),
            "added_chunks": added,
        })
        if mode == "shadow" or not supplemental:
            return baseline
        merged_chunk_ids = list(dict.fromkeys([
            *(chunk.chunk_id for chunk in scored),
            *(chunk.chunk_id for chunk in supplemental),
        ]))
        merged_ids, merged_matrix = [], None
        if merged_chunk_ids:
            _rows, merged_ids, merged_matrix = self._hydrate_chunk_candidates(
                merged_chunk_ids
            )
        # Support union mutates a baseline collision.  Keep it after every
        # fallible optional read/build step so the outer fail-open can return
        # the exact untouched baseline tuple on error.
        merged = self._union_chunk_candidates(scored, supplemental)
        return merged, merged_ids, merged_matrix

    def _retrieve_chunks_baseline(
        self, notebook_id: str, query: str, recall: int = 0, *,
        allowed_source_ids=None,
    ):
        """大召回 chunk 候选。返回 (scored, ids, matrix);后两者供 MMR 取两两余弦
        (matrix 行已 L2 归一化, 点积即余弦)。"""
        from app.services.source_scope import scoped_allowed_source_ids

        allowed_source_ids = scoped_allowed_source_ids(
            notebook_id, allowed_source_ids
        )
        from app.services.retrieval import (
            RetrievalSupport, add_chunk_supports, score_chunks,
        )
        from app.services.vector_index import query_sims
        recall = recall or self.settings.chunk_recall
        query_vector = self._embed_query(query)
        if (self.settings.chunk_ann_enabled
                and query_vector is not None):
            idx = self._scale_index(notebook_id, allow_stale=True)
            if idx is not None and getattr(idx, "chunk_ann_labels", None):
                if allowed_source_ids is None:
                    ann = self._retrieve_chunks_ann(
                        notebook_id, query, query_vector, idx, recall
                    )
                else:
                    ann = self._retrieve_chunks_ann(
                        notebook_id, query, query_vector, idx, recall,
                        allowed_source_ids=allowed_source_ids,
                    )
                if ann is not None:
                    return ann
        if allowed_source_ids is not None:
            # A selected scope is always a bounded candidate query.  Do not
            # materialize every chunk/vector merely because the enclosing
            # notebook is below the broad copy/share threshold.  The degraded
            # helper uses n_chunks only for diagnostics; avoid an otherwise
            # unnecessary whole-notebook COUNT on this source-bounded path.
            return self._retrieve_chunks_fts_degraded(
                notebook_id, query, query_vector, recall, -1,
                allowed_source_ids=allowed_source_ids,
            )
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
                n_chunks = self.chunks.count_row(db, notebook_id)["c"]
            if large or n_chunks > threshold:
                return self._retrieve_chunks_fts_degraded(
                    notebook_id, query, query_vector, recall, n_chunks
                )
        # ↓ 现有暴力路径保持不变
        with self._connect() as db:
            chunks = (
                self._gather_chunks(db, notebook_id)
                if allowed_source_ids is None
                else self._gather_chunks(
                    db, notebook_id, allowed_source_ids=allowed_source_ids
                )
            )
            if allowed_source_ids is None:
                ids, mat = self._vector_matrix(
                    db, notebook_id, "chunk_embeddings", "chunk_id"
                )
            else:
                from app.services.vector_index import build_matrix
                chunk_ids = [chunk["chunk_id"] for chunk in chunks]
                vector_rows = self.embeddings.vector_rows_for_ids(
                    db, notebook_id, "chunk_embeddings", "chunk_id", chunk_ids
                )
                ids, mat = build_matrix(
                    (row["vid"], row["vector"]) for row in vector_rows
                )
        chunk_sims = query_sims(query_vector, ids, mat) if query_vector else None
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        supports = {}
        for chunk in scored:
            if chunk_sims is not None and chunk.chunk_id in chunk_sims:
                supports[chunk.chunk_id] = [RetrievalSupport(
                    "semantic", "chunk", chunk.chunk_id, chunk_sims[chunk.chunk_id]
                )]
            else:
                supports[chunk.chunk_id] = [RetrievalSupport(
                    "lexical", "chunk", chunk.chunk_id, chunk.relevance
                )]
        add_chunk_supports(scored, supports)
        return scored, ids, mat
    def _retrieve_chunks_fts_degraded(self, notebook_id, query, query_vector,
                                      recall, n_chunks, *,
                                      allowed_source_ids=None):
        """大库且 chunk ANN 不可用时的有界降级:FTS5 词法候选(k=recall)→ 只对
        候选 hydrate 文本+向量,候选内做关键词+语义融合打分。绝不 _gather_chunks
        全表、不全量分词、不触发全量向量矩阵加载(与 PR#158「查询恒定成本」取向
        一致)。fail-open:FTS 异常(如旧库缺 chunks_fts)按零候选处理 →
        ([], [], None),事件携带 fts_error 供 diag_slow.py 定位。"""
        from app.services.retrieval import (
            RetrievalSupport, add_chunk_supports, score_chunks,
        )
        from app.services.vector_index import query_sims
        hits, fts_error = [], ""
        try:
            # 选定来源时，旧索引没有行→来源 sidecar 才会走这条
            # 降级路径；条件化 ANN 可用时会在 Top-K 前完成来源过滤。
            corpus_langs = self._lexical_corpus_langs(
                notebook_id, source_scoped=allowed_source_ids is not None)
            with self._connect() as db:
                if allowed_source_ids is None:
                    hits = self.knowledge.chunk_fts_search(
                        db, notebook_id, query, k=recall,
                        corpus_langs=corpus_langs,
                    )
                else:
                    hits = self.knowledge.chunk_fts_search(
                        db, notebook_id, query, k=recall,
                        allowed_source_ids=allowed_source_ids,
                        corpus_langs=corpus_langs,
                    )
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
        lexical_ids = {hit["chunk_id"] for hit in hits}
        add_chunk_supports(scored, {
            chunk.chunk_id: [
                *([RetrievalSupport(
                    "semantic", "chunk", chunk.chunk_id, chunk_sims[chunk.chunk_id]
                )] if chunk_sims is not None and chunk.chunk_id in chunk_sims else []),
                *([RetrievalSupport(
                    "lexical", "chunk", chunk.chunk_id, chunk.relevance
                )] if chunk.chunk_id in lexical_ids else []),
            ]
            for chunk in scored
        })
        return scored, ids, mat
    def _retrieve_chunks_ann(
        self, notebook_id, query, query_vector, idx, recall, *,
        allowed_source_ids=None,
    ):
        """ANN 候选版 chunk 检索:只对 top-recall 候选打分,避免全表 matmul+重分词。
        返回 (scored, ids, matrix) 同 _retrieve_chunks;失败返回 None(上层回退暴力)。"""
        import numpy as np
        from app.services.retrieval import (
            RetrievalSupport, add_chunk_supports, score_chunks,
        )
        from app.services.vector_index import build_matrix, query_sims
        labels = idx.chunk_ann_labels
        if not labels:
            return None
        allowed = (
            frozenset(str(value) for value in allowed_source_ids)
            if allowed_source_ids is not None else None
        )
        source_codes = getattr(idx, "chunk_ann_source_codes", None)
        source_names = getattr(idx, "chunk_ann_source_names", None)
        source_counts = getattr(idx, "chunk_ann_source_counts", None)
        if allowed is not None and (
            source_codes is None
            or source_names is None
            or source_counts is None
            or len(source_codes) != len(labels)
        ):
            # Older indexes have no row→source sidecar.  Do not run global ANN
            # and filter its Top-K afterward: out-of-scope rows could starve
            # valid evidence.  Returning None selects the bounded scoped-FTS
            # fallback until the index is rebuilt/folded.
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
            if allowed is None:
                k = min(recall, len(labels))
                labs, dists = ann.knn_query(qarr, k=k)
            else:
                code_by_source = {
                    str(source_id): code
                    for code, source_id in enumerate(source_names)
                }
                allowed_codes = frozenset(
                    code_by_source[source_id]
                    for source_id in allowed
                    if source_id in code_by_source
                )
                eligible = sum(
                    int(source_counts[code]) for code in allowed_codes
                )
                if eligible:
                    k = min(recall, eligible)
                    labs, dists = ann.knn_query(
                        qarr,
                        k=k,
                        filter=lambda label: int(source_codes[int(label)])
                        in allowed_codes,
                    )
                else:
                    labs = np.empty((1, 0), dtype=np.int64)
                    dists = np.empty((1, 0), dtype=np.float32)
        except Exception as exc:  # noqa: BLE001 — fail-open, 回退暴力
            self._note_model_error(
                "chunk_ann_query",
                "",
                exc,
                service="embedding",
            )
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
                delta_sources = list(delta["delta_sources"])
                if allowed is not None:
                    delta_sources = [
                        source_id for source_id in delta_sources
                        if source_id in allowed
                    ]
                if delta_sources:
                    drows = []
                    with self._connect() as db:
                        for batch in self._in_batches(delta_sources):
                            drows.extend(self.embeddings.chunk_delta_rows(
                                db, notebook_id, batch,
                            ))
                    d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
                    d_sims = query_sims(query_vector, d_ids, d_mat) if d_ids else {}
                    for cid, s in d_sims.items():
                        if cid not in chunk_sims:
                            cand_ids.append(cid)
                        chunk_sims[cid] = s
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮检索,退回仅核候选
                self._note_model_error(
                    "chunk_ann_delta",
                    "",
                    exc,
                    service="embedding",
                )

        semantic_ids = set(chunk_sims)
        lexical_ids = set()
        # ∪ 词法:FTS5 命中补召回(ANN 是语义候选,纯关键词命中可能漏)
        try:
            corpus_langs = self._lexical_corpus_langs(
                notebook_id, source_scoped=allowed is not None
            )
            with self._connect() as db:
                lex = self.knowledge.chunk_fts_search(
                    db, notebook_id, query, k=recall,
                    allowed_source_ids=(
                        tuple(sorted(allowed)) if allowed is not None else None
                    ),
                    corpus_langs=corpus_langs)
            for h in lex:
                cid = h["chunk_id"]
                lexical_ids.add(cid)
                if cid not in chunk_sims:
                    cand_ids.append(cid)
                    # Candidate identity and semantic evidence stay separate:
                    # absence from chunk_sims means keyword-only fusion.
        except Exception as exc:  # noqa: BLE001 — 词法失败不拖垮检索
            self._note_model_error("chunk_fts", "", exc)

        if not cand_ids:
            return [], [], None
        chunks, ids, mat = self._hydrate_chunk_candidates(cand_ids)
        if allowed is not None:
            chunks = [chunk for chunk in chunks if chunk["source_id"] in allowed]
            kept_ids = {chunk["chunk_id"] for chunk in chunks}
            chunk_sims = {
                chunk_id: score for chunk_id, score in chunk_sims.items()
                if chunk_id in kept_ids
            }
            lexical_ids.intersection_update(kept_ids)
            semantic_ids.intersection_update(kept_ids)
        scored = score_chunks(query, chunks, query_vector, chunk_sims, limit=recall)
        add_chunk_supports(scored, {
            chunk.chunk_id: [
                *([RetrievalSupport(
                    "semantic", "chunk", chunk.chunk_id, chunk_sims[chunk.chunk_id]
                )] if chunk.chunk_id in semantic_ids else []),
                *([RetrievalSupport(
                    "lexical", "chunk", chunk.chunk_id, chunk.relevance
                )] if chunk.chunk_id in lexical_ids else []),
            ]
            for chunk in scored
        })
        return scored, ids, mat
    def hydrate_chunk_candidates(self, cand_ids):
        """按候选 id 有界取数:chunk 文本行 + 归一化向量矩阵。候选界定之后的
        hydrate,ANN 路径与大库 FTS 降级路径共用,绝不全表。返回 (chunks, ids, mat)。
        cand_ids 随 delta⊕词法补召回可无界增长——按 _in_batches 分批,防超 SQLite
        变量上限(调用方按构造去重,_in_batches 亦去重保序,extend 不产生重复行)。"""
        from app.services.vector_index import build_matrix
        rows, vrows = [], []
        with self._connect() as db:
            for batch in self._in_batches(cand_ids):
                rows.extend(self.chunks.hydrate_rows(db, batch))
                vrows.extend(self.embeddings.rows_by_ids(
                    db, "chunk_embeddings", "chunk_id", batch,
                ))
        chunks = [{
            "chunk_id": r["id"], "source_id": r["source_id"], "text": r["text"],
            "section_path": r["section_path"], "source_title": r["source_title"],
            "element_ids": json.loads(r["element_ids"] or "[]"),
        } for r in rows]
        ids, mat = build_matrix((r["vid"], r["vector"]) for r in vrows)
        return chunks, ids, mat

    # Compatibility for internal subclasses and older tests.  Composition
    # boundaries use the public RetrievalPort method above.
    def _hydrate_chunk_candidates(self, cand_ids):
        return self.hydrate_chunk_candidates(cand_ids)
    def _retrieve_chunks_multi(self, notebook_id, sub_queries):
        """对每个子查询并发跑 _retrieve_chunks;返回 (collected{chunk_id:best}, per_query, ids, mat)。
        ids/mat 取首个非空子查询的矩阵(同 notebook 矩阵一致,用于后续 MMR 兜底)。"""
        from concurrent.futures import ThreadPoolExecutor
        from app.services.retrieval import (
            merge_retrieval_supports,
            partition_generated_question_chunks,
        )

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
        per_query, collected, supplemental_rows, ids, mat = [], {}, [], [], None
        for scored, qids, qmat in results:
            per_query.append({c.chunk_id: c for c in scored})
            baseline_rows, query_supplemental = partition_generated_question_chunks(
                scored
            )
            supplemental_rows.extend(query_supplemental)
            # Aggregate every historical query result before considering any
            # optional supplement.  Otherwise a high-scoring question-only row
            # from an early query can take the dict position/representative of a
            # semantic hit found by a later query and change feature-off order.
            for c in baseline_rows:
                cur = collected.get(c.chunk_id)
                if cur is None:
                    collected[c.chunk_id] = c
                else:
                    supports = merge_retrieval_supports(
                        cur.retrieval_supports, c.retrieval_supports
                    )
                    if c.relevance > cur.relevance:
                        c.retrieval_supports = supports
                        collected[c.chunk_id] = c
                    else:
                        cur.retrieval_supports = supports
            if mat is None and len(qids):
                ids, mat = qids, qmat
        for c in supplemental_rows:
            cur = collected.get(c.chunk_id)
            if cur is None:
                collected[c.chunk_id] = c
                continue
            # A historical representative always wins a collision.  The
            # generated-question support remains useful provenance but cannot
            # replace its relevance or insertion position.
            cur.retrieval_supports = merge_retrieval_supports(
                cur.retrieval_supports, c.retrieval_supports
            )
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
        from app.services.retrieval import (
            RetrievalSupport, add_chunk_supports, score_chunks,
        )
        needle = (keywords or "").strip()
        if not needle:
            return []
        recall = recall or self.settings.chunk_recall
        from app.services.source_scope import scoped_allowed_source_ids

        allowed_source_ids = scoped_allowed_source_ids(notebook_id)
        try:
            corpus_langs = self._lexical_corpus_langs(
                notebook_id, source_scoped=allowed_source_ids is not None)
            with self._connect() as db:
                if allowed_source_ids is None:
                    hits = self.knowledge.chunk_fts_search(
                        db, notebook_id, needle, k=recall,
                        corpus_langs=corpus_langs)
                else:
                    hits = self.knowledge.chunk_fts_search(
                        db, notebook_id, needle, k=recall,
                        allowed_source_ids=allowed_source_ids,
                        corpus_langs=corpus_langs,
                    )
            if not hits:
                return []
            chunks, _ids, _mat = self._hydrate_chunk_candidates([h["chunk_id"] for h in hits])
            # keyword-only score (no query_vector/chunk_sims) — mirrors the ANN∪FTS
            # union where lexical hits get keyword score and semantic 0.
            scored = score_chunks(needle, chunks, None, None, limit=recall)
            add_chunk_supports(scored, {
                chunk.chunk_id: [RetrievalSupport(
                    "lexical", "chunk", chunk.chunk_id, chunk.relevance
                )]
                for chunk in scored
            })
            return scored
        except Exception as exc:  # noqa: BLE001 — lexical补召回失败绝不拖垮检索
            self._note_model_error("chunk_keyword_union", "", exc)
            return []
    def _exact_lookup_chunks(self, notebook_id: str, query: str):
        """Exact-identifier fast path → whole-section RetrievedChunks.

        Zero model calls and zero embeddings; every query is bounded by the
        `exact_lookup_*` settings. `exact_lookup_chunks` itself returns `[]`
        without touching the database when the query holds no identifier, so
        an ordinary question pays nothing here — the settings gate below is the
        operator's kill switch, not the hot-path guard.

        Called ONCE per ask (not per sub-query), exactly like
        `_keyword_chunk_candidates`, and fail-open for the same reason: a
        supplementary recall channel must never take the whole retrieval down.
        """
        # The exact-section helper currently allocates slots before hydration
        # and has no source predicate.  Skip it only for a truly narrowed run;
        # an all-selected frozen snapshot keeps the historical channel and its
        # output is checked again at the retrieval boundary.
        if self._unsafe_source_scope_restricted(notebook_id):
            return []
        if not self.settings.exact_lookup_enabled:
            return []
        from app.services.exact_lookup import (
            ExactLookupDeps, ExactLookupLimits, exact_lookup_chunks,
        )
        try:
            return exact_lookup_chunks(
                ExactLookupDeps(
                    connect=self._connect,
                    exact_search=self.knowledge.chunk_exact_search,
                    section_rows=self.chunks.chunks_by_section,
                    # `hydrate_rows` already returns the exact row shape the
                    # section fetch does (id/source_id/text/section_path/
                    # element_ids/source_title) on BOTH adapters, so the
                    # by-id path needs no new store primitive — only this seam.
                    chunk_rows=self.chunks.hydrate_rows,
                ),
                notebook_id,
                query,
                ExactLookupLimits(
                    max_identifiers=self.settings.exact_lookup_max_identifiers,
                    fts_k=self.settings.exact_lookup_fts_k,
                    max_sections=self.settings.exact_lookup_max_sections,
                    max_chunks_per_section=(
                        self.settings.exact_lookup_max_chunks_per_section),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — 精确通道失败绝不拖垮检索
            self._note_model_error("chunk_exact_lookup", "", exc)
            return []
    @staticmethod
    def _union_chunk_candidates(base: list, extra: list) -> list:
        """Append `extra` RetrievedChunks to `base`, deduped by chunk_id (keep the
        existing entry on collision — base already scored it with semantic signal).
        Order-preserving. Used to fold the bilingual-keyword lexical hits into a
        list-shaped candidate set (mix / single-subquery branches)."""
        if not extra:
            return base
        from app.services.retrieval import merge_retrieval_supports

        by_id = {c.chunk_id: c for c in base}
        seen = set(by_id)
        out = list(base)
        for c in extra:
            if c.chunk_id in seen:
                existing = by_id[c.chunk_id]
                existing.retrieval_supports = merge_retrieval_supports(
                    existing.retrieval_supports, c.retrieval_supports
                )
            else:
                seen.add(c.chunk_id)
                out.append(c)
                by_id[c.chunk_id] = c
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
        # 汇集所有类型对象,构建 (id, text) 列表及 id->obj 映射。按 kg_objs 实际
        # 键(= _retrieve_scored 的 type_list)遍历,而非硬编码 _KG_TYPES —— flag-off
        # 时 kg_objs 只含 _KG_TYPES 子集,逐字节等价;flag-on(knowhow)时列名动态
        # 类型也一并进 RRF 融合(否则会被取来却在 RRF 分支静默丢弃)。
        docs: List[tuple] = []
        id_to_obj: Dict[str, dict] = {}
        id_to_type: Dict[str, str] = {}
        # Iterate the four fixed types FIRST (their historical, caller-order-
        # independent sequence — preserves byte-identical tie-break ordering when
        # the feature is off / for any _KG_TYPES-only call), then append any
        # dynamic knowhow column-name types the widening added. `kg_objs.get(t)`
        # tolerates a fixed type the widened type_list happened to drop.
        for t in [*_KG_TYPES, *[k for k in kg_objs if k not in _KG_TYPES]]:
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

        basis = keyword_basis(query)
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
            doc_text = text_by_id.get(oid, "")
            relevance = _fuse(basis.coverage(set(_tokens(doc_text)), doc_text),
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
        exp = expand_query(self.model_clients.chat("query_rewrite"), question,
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
    def _chunk_kg_overlay(self, notebook_id: str, query: str, hl: str, id_offset: int):
        """种子(节点∪关系端点)→1-hop 子图→渲染。

        返回 ``(block, id_map, kg_hits, supports_by_object)``；最后一项保留
        本轮真实 relation id/review snapshot，供 evidence→chunk 映射后继续传到
        最终 selector。纯节点种子不伪造 relation support。

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
        from app.services.source_scope import source_scope_restricted

        unsafe_scope_probe = getattr(
            self, "_unsafe_source_scope_restricted", None
        )
        restricted = (
            bool(unsafe_scope_probe(notebook_id))
            if callable(unsafe_scope_probe)
            else source_scope_restricted()
        )
        if not restricted and self._federated_graph_is_large(notebook_id):
            self.event_log.emit({
                "kind": "graph_walk_refused",
                "notebook_id": notebook_id,
                "reason": "large_notebook",
                "site": "chunk_kg_overlay",
            })
            return "", {}, [], {}
        from app.services.kg.graph_reason import multihop_subgraph, render_subgraph_context
        settings = getattr(self, "settings", None)
        node_seed_top_n = getattr(
            settings,
            "chunk_kg_node_seed_top_n",
            DEFAULT_CHUNK_KG_NODE_SEED_TOP_N,
        )
        relation_seed_top_n = getattr(
            settings,
            "chunk_kg_relation_seed_top_n",
            DEFAULT_CHUNK_KG_RELATION_SEED_TOP_N,
        )
        max_depth = getattr(
            settings,
            "chunk_kg_max_depth",
            DEFAULT_CHUNK_KG_MAX_DEPTH,
        )
        fan_out = getattr(
            settings,
            "chunk_kg_fan_out",
            DEFAULT_CHUNK_KG_FAN_OUT,
        )
        node_hits = self.federated_retrieve(notebook_id, query)[
            :node_seed_top_n
        ]
        if restricted:
            # The whole federated graph is not source partitioned, but direct
            # federated KG search is: the active participant receives the
            # checkbox allow-list before LIMIT and mounted bases stay intact.
            # Render those safe seeds as isolated nodes, then map only their
            # own evidence back to chunks.  This preserves base-backed chunk
            # answers even when every active source is unchecked.
            subgraph = [
                ({
                    "object_id": hit.object_id,
                    "object_type": hit.object_type,
                    "name": str((hit.payload or {}).get("name") or hit.object_id),
                    "tier": getattr(hit, "tier", "personal"),
                    "notebook_id": getattr(hit, "notebook_id", ""),
                }, None, None)
                for hit in node_hits
            ]
            block, id_map = render_subgraph_context(
                subgraph, id_offset=id_offset
            )
            return block, id_map, node_hits, {}
        rel_hits = self.federated_retrieve_relations(notebook_id, hl or query)[
            :relation_seed_top_n
        ]
        seeds = [h.object_id for h in node_hits]
        for r in rel_hits:
            seeds.extend((r.source_object_id, r.target_object_id))
        seeds = list(dict.fromkeys(s for s in seeds if s))
        if not seeds:
            return "", {}, [], {}
        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
        if G is None or G.num_nodes() == 0:
            return "", {}, [], {}
        subgraph = multihop_subgraph(G, oid_to_idx, idx_to_oid, seed_ids=seeds,
                                     edge_types=None, max_depth=max_depth,
                                     max_fan_out=fan_out)
        from app.services.source_scope import scoped_subgraph_nodes

        # The federated graph is process-cached under a scope-blind key, so the
        # library ceiling is applied to the walk's RESULT (see the helper).
        subgraph = scoped_subgraph_nodes(subgraph)
        if not subgraph:
            return "", {}, [], {}
        block, id_map = render_subgraph_context(subgraph, id_offset=id_offset)
        from app.services.retrieval import RetrievalSupport, merge_retrieval_supports

        support_by_object = {}
        for hit in rel_hits:
            support = RetrievalSupport(
                "relation", "relation", hit.relation_id, hit.score,
                hit.review_status,
            )
            for oid in (hit.source_object_id, hit.target_object_id):
                support_by_object[oid] = merge_retrieval_supports(
                    support_by_object.get(oid, ()), (support,)
                )
        for node, edge, src_oid in subgraph:
            if not edge or not edge.get("rel_id"):
                continue
            support = RetrievalSupport(
                "relation", "relation", str(edge["rel_id"]),
                float(edge.get("confidence", 0.0)),
                str(edge.get("review_status") or "pending"),
            )
            for oid in (src_oid, node.get("object_id")):
                if oid:
                    support_by_object[oid] = merge_retrieval_supports(
                        support_by_object.get(oid, ()), (support,)
                    )
        return block, id_map, node_hits, support_by_object
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
        from app.services.retrieval import partition_generated_question_chunks

        vector_chunks, question_supplements = partition_generated_question_chunks(
            vector_chunks
        )
        kg_block, kg_id_map, kg_hits, kg_chunks = "", {}, [], []
        overlay_on = self.settings.chunk_kg_overlay_enabled and (
            self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg(notebook_id))
        if overlay_on:
            kg_block, kg_id_map, kg_hits, support_by_object = self._chunk_kg_overlay(
                notebook_id, query, hl, id_offset=self._MIX_KG_KEY_BASE)
            kg_chunks = self._kg_source_chunks(
                notebook_id, [v["object_id"] for v in kg_id_map.values()],
                support_by_object=support_by_object,
            )
        # 概念漫游(PPR)第 3 路:gated GRAPH_PPR_ENABLED;无 KG/无 reset → []。
        ppr_chunks = (
            self._ppr_retrieve(notebook_id, query)
            if self.settings.graph_ppr_enabled
            and not self._unsafe_source_scope_restricted(notebook_id)
            else []
        )
        from app.services.retrieval import merge_retrieval_supports

        merged, seen, by_id = [], set(), {}
        for i in range(max(len(vector_chunks), len(kg_chunks), len(ppr_chunks))):
            for src in (vector_chunks, kg_chunks, ppr_chunks):
                if i >= len(src):
                    continue
                chunk = src[i]
                if chunk.chunk_id in seen:
                    existing = by_id[chunk.chunk_id]
                    existing.retrieval_supports = merge_retrieval_supports(
                        existing.retrieval_supports, chunk.retrieval_supports
                    )
                    continue
                seen.add(chunk.chunk_id)
                by_id[chunk.chunk_id] = chunk
                merged.append(chunk)
        # Complete the historical vector/KG/PPR round-robin before optional
        # question-only chunks enter the pool.  A collision only enriches the
        # historical candidate's provenance; it never changes its position.
        for chunk in question_supplements:
            if chunk.chunk_id in seen:
                existing = by_id[chunk.chunk_id]
                existing.retrieval_supports = merge_retrieval_supports(
                    existing.retrieval_supports, chunk.retrieval_supports
                )
                continue
            seen.add(chunk.chunk_id)
            by_id[chunk.chunk_id] = chunk
            merged.append(chunk)
        return merged, kg_block, kg_id_map, kg_hits, len(ppr_chunks)
    def _build_chunk_retrieval_plan(self, notebook_id: str, sub_queries: list) -> ChunkRetrievalPlan:
        """一次读齐 chunk 检索路径的 flag/knob，产出不可变快照（W2.2）。见 ChunkRetrievalPlan。
        strategy 复刻 ask_chunk 的 overlay_on 三元 AND 与 if/elif/else 顺序，逐真值等价；
        fuse_k 复刻 multi 分支 quota_fuse 复用 chunk_mmr_k 的隐式契约。"""
        s = self.settings
        overlay_on = (s.chunk_kg_overlay_enabled
                      and self.model_clients.rerank("retrieval_rerank").configured
                      and (self._notebook_has_kg(notebook_id)
                           or self._any_base_notebook_has_kg(notebook_id)))
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

    def retrieve_scored(self, *args, **kwargs):
        return self._retrieve_scored(*args, **kwargs)

    def federated_retrieve(self, *args, **kwargs):
        from app.services.source_scope import filter_retrieval_items

        notebook_id = str(args[0] if args else kwargs.get("active_notebook_id", ""))
        return filter_retrieval_items(
            notebook_id, "knowledge",
            self._federated_retrieve_impl(*args, **kwargs),
        )

    def federated_retrieve_relations(self, *args, **kwargs):
        from app.services.source_scope import filter_retrieval_items

        notebook_id = str(args[0] if args else kwargs.get("active_notebook_id", ""))
        return filter_retrieval_items(
            notebook_id, "relation",
            self._federated_retrieve_relations_impl(*args, **kwargs),
        )

    def federated_retrieve_elements(self, *args, **kwargs):
        return self._federated_retrieve_elements_impl(*args, **kwargs)

    def retrieve_neighbors(self, *args, **kwargs) -> NeighborExpansion:
        from app.services.source_scope import filter_retrieval_items

        notebook_id = str(args[0] if args else kwargs.get("notebook_id", ""))
        # 来源范围过滤只作用于命中行;截断标志是「读取侧还有多少没看」的事实,
        # 与过滤掉多少行无关,原样传下去。
        expansion = self._retrieve_neighbors(*args, **kwargs)
        return NeighborExpansion(
            filter_retrieval_items(notebook_id, "knowledge", expansion.hits),
            expansion.truncated,
        )

    def retrieve_elements(self, *args, **kwargs):
        from app.services.source_scope import filter_retrieval_items

        notebook_id = str(args[0] if args else kwargs.get("notebook_id", ""))
        return filter_retrieval_items(
            notebook_id, "element", self._retrieve_elements(*args, **kwargs)
        )
