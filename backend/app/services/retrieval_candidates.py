"""Candidate retrieval owner.

The service is composed from narrow stores/runtime collaborators.  It retains
no repository facade; compatibility operation functions are unbound and run
against this facade-independent state object. All query families are hosted
by their owning stores.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from app.core.ask_context import _ASK_EMBED_CACHE
from app.models.common import Evidence
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.model_config import model_client_fingerprint
from app.services.retrieval import (
    RELEVANCE_FLOOR,
    W_KEYWORD,
    W_SEMANTIC,
    RetrievedElement,
    RetrievedKnowledge,
    RetrievedRelation,
    _TYPE_WEIGHT,
    _fuse,
    _payload_text,
    bm25_scores,
    ensure_procedure_quota,
    is_process_query,
    keyword_score,
    rrf_fuse,
    score_elements,
    score_knowledge,
    type_weight,
)


_KG_TYPES = ("claim", "formula", "procedure", "concept")


@dataclass(frozen=True)
class ChunkRetrievalPlan:
    strategy: str
    overlay_on: bool
    mmr_k: int
    mmr_lambda: float
    fuse_k: int


class _RetrievalState:
    def __init__(
        self,
        *,
        database,
        snapshots,
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
    def llm_client(self):
        return self.model_clients.llm_client

    @property
    def reasoning_llm_client(self):
        return self.model_clients.reasoning_llm_client

    @property
    def rewrite_llm_client(self):
        return self.model_clients.rewrite_llm_client

    @property
    def rerank_client(self):
        return self.model_clients.rerank_client

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
        model: str,
        exc: Exception,
        service: str = "",
        *,
        provider_failure: bool = False,
        failed_fingerprint: str = "",
    ) -> None:
        self.model_error_sink.note_model_error(
            stage,
            model,
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
    _MIX_NODE_SEEDS = 20
    _MIX_REL_SEEDS = 10
    _MIX_FANOUT = 8
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
            self._note_model_error(
                "embed",
                self.settings.embed_model,
                exc,
                service="embedding",
                provider_failure=True,
                failed_fingerprint=model_client_fingerprint(self.embedder),
            )
            return None
        from app.services.vector_index import resolve_runtime_dim, truncate_vec
        rd = resolve_runtime_dim(self.settings)
        if rd and vec is not None and len(vec) > rd:
            import numpy as np
            vec = truncate_vec(np.asarray(vec, dtype=np.float32), rd).tolist()
        if cache is not None and vec is not None:
            cache[key] = vec
        return vec
    def _gather_elements(self, db: sqlite3.Connection, notebook_id: str,
                         with_vectors: bool = True) -> List[dict]:
        # 运行时截断旁路(计划 §1.2 旁路 1):element 兜底不走 build_matrix,逐向量
        # 进 retrieval.cosine 与 _embed_query(已截断,T2)比较 —— cosine 对 len 不等
        # 静默返 0.0,漏截 = 全部 element 静默零相似度。
        from app.services.vector_index import decode_vector, resolve_runtime_dim, truncate_vec

        rd = resolve_runtime_dim(self.settings)
        rows = self.sources.retrieval_element_rows(db, notebook_id)
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
        rows = self.chunks.retrieval_rows(db, notebook_id)
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
        ver = self.embeddings.version_row(db, notebook_id, table)
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
            rows = self.embeddings.vector_rows(db, notebook_id, table, id_col)
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

        ver = self.knowledge.object_version_row(db, notebook_id)
        version = ("kwtok", ver["c"], ver["ts"])
        return self._vector_cache.get(f"{notebook_id}:kwtok", version, lambda: _build(objects))
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
        if relation_ids is not None:
            if not relation_ids:
                return []
        rows = self.knowledge.relation_context_rows(
            db, notebook_id, relation_ids, batch_size=self._IN_CHUNK,
        )
        out = []
        for r in rows:
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
                    self._note_model_error(
                        "relation_ann_query",
                        self.settings.embed_model,
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
                    self.settings.embed_model,
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

        def live_relations(rows: List[dict]) -> List[dict]:
            return [row for row in rows if row["review_status"] != "rejected"]

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
                if query_vector is not None:
                    cand_sims = self._relation_ann_candidates(
                        notebook_id, query_vector, idx, self.settings.relation_recall)
                    if cand_sims:
                        top_ids = list(cand_sims.keys())
                        relations = live_relations(
                            self._relations_with_names(
                                db, notebook_id, relation_ids=top_ids
                            )
                        )
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
                        self.settings.embed_model,
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
                    self.settings.embed_model,
                    exc,
                    service="embedding",
                )
        return sims
    def _knowhow_object_types(self, notebook_id: str) -> tuple:
        """Distinct knowhow cell-KO object_types (each a table COLUMN NAME — a
        dynamic type projected by ``KnowhowProjector``) currently usable in the
        notebook: ``type_counts`` (seq-memoized on ``kg_mutation_seq``, which the
        knowhow projection bumps via ``mark_unified_dirty``, so this self-
        invalidates) minus the four fixed ``_KG_TYPES``. Empty tuple ⇒ the
        notebook has no knowhow graph content ⇒ every caller no-ops. Only ever
        reached on the flag-on branch (see ``_scored_types``)."""
        from app.repositories.sqlite import knowledge_counts_cache
        with self._connect() as db:
            counts = knowledge_counts_cache.type_counts(
                db, notebook_id, statuses=USABLE_STATUSES)
        return tuple(t for t in counts if t not in _KG_TYPES)

    def _scored_types(self, notebook_id: str, types) -> List[str]:
        """The object_types ``_retrieve_scored`` will fetch+score. FLAG OFF (or a
        notebook with no knowhow types) ⇒ BYTE-IDENTICAL to the historical line:
        caller ``types`` (or all four ``_KG_TYPES`` by default) filtered to
        ``_KG_TYPES``. FLAG ON + knowhow present ⇒ the allowed set widens to
        ``_KG_TYPES ∪ knowhow-types`` and the default (no caller ``types``)
        unions the knowhow types in too; a caller-supplied list is still just
        filtered against the (now wider) allowed set. ``_knowhow_object_types``
        is only touched on the flag-on branch, so flag-off issues no new query."""
        base = [t for t in (list(types) if types else list(_KG_TYPES)) if t in _KG_TYPES]
        if not self.settings.knowhow_kg_node_retrieval_enabled:
            return base
        knowhow_types = self._knowhow_object_types(notebook_id)
        if not knowhow_types:
            return base
        allowed = set(_KG_TYPES) | set(knowhow_types)
        source = list(types) if types else [*_KG_TYPES, *knowhow_types]
        return [t for t in source if t in allowed]

    def _knowhow_ko_candidates(self, db, notebook_id: str, query: str,
                               query_vector) -> dict:
        """Gate 0 (default-off): ``{ko_id: sim}`` for knowhow cell KOs whose
        hidden-source chunk vectors match the query — the semantic signal a
        structural-only KO (no ``knowledge_embeddings`` row) otherwise lacks.

        Fetches the scoped inputs through this service's stores (the hidden
        knowhow source's chunk rows → their vectors → the cell elements — a
        bounded brute-force over the tiny knowhow chunk set, NOT a notebook-wide
        retrieval) and hands them to the pure ``kg_node_bridge`` reduction.
        ``query`` is accepted for signature stability / future lexical use;
        gate 0 is purely semantic today."""
        from app.services.knowhow.kg_node_bridge import knowhow_ko_candidates
        if query_vector is None:
            return {}
        chunk_rows = self.chunks.knowhow_chunk_rows(db, notebook_id)
        if not chunk_rows:
            return {}
        chunk_to_element = {}
        for row in chunk_rows:
            element_ids = json.loads(row["element_ids"] or "[]")
            if element_ids:
                chunk_to_element[row["id"]] = element_ids[0]
        if not chunk_to_element:
            return {}
        # Batch the IN-clause like every other candidate fetch here: knowhow is
        # <100 rows/table by design, but a notebook accumulating many tables can
        # still push the chunk-id list past SQLite's variable limit.
        vector_rows: list = []
        for batch in self._in_batches(list(chunk_to_element)):
            vector_rows.extend(self.embeddings.vector_rows_for_ids(
                db, notebook_id, "chunk_embeddings", "chunk_id", batch))
        elements = self.sources.evidence_elements(
            list(dict.fromkeys(chunk_to_element.values())))
        return knowhow_ko_candidates(
            chunk_to_element, vector_rows, elements, query_vector,
            recall=self.settings.chunk_recall)

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
        # gate i (type widening): flag-off / non-knowhow ⇒ historical filter,
        # byte-identical. flag-on + knowhow present ⇒ column-name types join
        # the fetch set (see _scored_types).
        type_list = self._scored_types(notebook_id, types)
        # gate 0/i are live only when the widening actually surfaced a knowhow
        # (column-name) type — exactly "flag on AND this notebook has knowhow
        # graph content" (or a caller explicitly asked for one). Flag off /
        # non-knowhow ⇒ empty ⇒ pure no-op, no bridge query, no injection.
        knowhow_on = bool(set(type_list) - set(_KG_TYPES))
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
                lex = self.knowledge.fts_search(
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
            # ── gate 0 (default-off): knowhow cell-KO semantic sidecar ───────
            # Reverse-lookup the query against ONLY the hidden knowhow source's
            # chunk vectors → {ko_id: sim}. Computed once, folded into whichever
            # scoring path this retrieval is on. Empty (no cost) unless knowhow_on.
            knowhow_sims = (
                self._knowhow_ko_candidates(db, notebook_id, query, query_vector)
                if knowhow_on else {})
            if knowhow_sims and cand_sims is not None:
                # Bounded path: union into the candidate set BEFORE id_filter so
                # the KOs are fetched, and (via knowledge_sims=cand_sims below)
                # pick up their semantic score.
                for ko_id, sim in knowhow_sims.items():
                    cand_sims[ko_id] = max(cand_sims.get(ko_id, 0.0), sim)
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
                    rel_rows.extend(self.knowledge.relation_connected_rows(
                        db, notebook_id, batch,
                    ))
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
            if knowhow_sims and cand_sims is None and knowledge_sims is not None:
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
    def _federated_retrieve_impl(
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
        with self._connect() as db:
            notebook_ids, tier_map = self.notebooks.participant_tiers(
                db, active_notebook_id,
            )

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
    def _federated_retrieve_relations_impl(self, active_notebook_id: str,
                                     query: str) -> List["RetrievedRelation"]:
        """跨 {base notebook(s)} ∪ {active} 检索关系,逐本 .notebook_id/.tier 标注。
        每本走 _retrieve_relations_scored(同尺),合并按 score 降序。"""
        with self._connect() as db:
            notebook_ids, tier_map = self.notebooks.participant_tiers(
                db, active_notebook_id,
            )
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
        neighbour_ids: set = set()
        with self._connect() as db:
            if direction in ("out", "both"):
                neighbour_ids.update(
                    r["target_object_id"] for r in self.knowledge.neighbor_ids(
                        db, notebook_id, object_id,
                        endpoint="source_object_id", edge_type=edge_type,
                    )
                )
            if direction in ("in", "both"):
                neighbour_ids.update(
                    r["source_object_id"] for r in self.knowledge.neighbor_ids(
                        db, notebook_id, object_id,
                        endpoint="target_object_id", edge_type=edge_type,
                    )
                )
            if not neighbour_ids:
                return []
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
        return out
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
                n_chunks = self.chunks.count_row(db, notebook_id)["c"]
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
                hits = self.knowledge.chunk_fts_search(
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
            self._note_model_error(
                "chunk_ann_query",
                self.settings.embed_model,
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
                if delta["delta_sources"]:
                    drows = []
                    with self._connect() as db:
                        for batch in self._in_batches(delta["delta_sources"]):
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
                    self.settings.embed_model,
                    exc,
                    service="embedding",
                )

        # ∪ 词法:FTS5 命中补召回(ANN 是语义候选,纯关键词命中可能漏)
        try:
            with self._connect() as db:
                lex = self.knowledge.chunk_fts_search(
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
                hits = self.knowledge.chunk_fts_search(
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
            self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg(notebook_id))
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
    def _build_chunk_retrieval_plan(self, notebook_id: str, sub_queries: list) -> ChunkRetrievalPlan:
        """一次读齐 chunk 检索路径的 flag/knob，产出不可变快照（W2.2）。见 ChunkRetrievalPlan。
        strategy 复刻 ask_chunk 的 overlay_on 三元 AND 与 if/elif/else 顺序，逐真值等价；
        fuse_k 复刻 multi 分支 quota_fuse 复用 chunk_mmr_k 的隐式契约。"""
        s = self.settings
        overlay_on = (s.chunk_kg_overlay_enabled
                      and self.rerank_client.configured
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
        return self._federated_retrieve_impl(*args, **kwargs)

    def federated_retrieve_relations(self, *args, **kwargs):
        return self._federated_retrieve_relations_impl(*args, **kwargs)

    def retrieve_neighbors(self, *args, **kwargs):
        return self._retrieve_neighbors(*args, **kwargs)

    def retrieve_elements(self, *args, **kwargs):
        return self._retrieve_elements(*args, **kwargs)
