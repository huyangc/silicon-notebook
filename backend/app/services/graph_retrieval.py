"""Graph/PPR/follow-chain retrieval owner, composed without a facade."""
from __future__ import annotations

import heapq
import json
import math
import time
from typing import Dict, Iterable, List, Optional, Tuple

from app.models.common import Evidence
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.retrieval import RetrievedKnowledge
from app.services.retrieval_candidates import _RetrievalState


#: Rows per batched hnswlib ``knn_query`` in the cross-layer synonym bridge.
#: An EXECUTION batch size, not a result cap — every active vector is still
#: queried and every surviving neighbour still becomes an edge; only the number
#: of C-extension round trips changes (one per block instead of one per vector),
#: which is 3-4 orders of magnitude fewer at scale.
#: The block size bounds only the per-call query SLICE and the returned
#: label/distance arrays; the full active matrix is still materialized up front
#: by ``_vector_matrix`` (pre-existing behavior, unchanged here).
#: Registered trade-off: batched queries run on hnswlib's DEFAULT thread pool.
#: We deliberately do NOT ``set_num_threads(1)`` — the index handle is shared
#: (memoized on the ScaleIndex) and that setting would spill onto every other
#: consumer of the same handle. This path runs once per cache-version miss
#: (cold combined-graph load), not per query, so the pool is acceptable here.
_XBRIDGE_QUERY_BLOCK = 4096


def _xbridge_similarities(dists) -> "np.ndarray":
    """一块 knn_query 距离矩阵 -> 相似度矩阵，逐位复刻历史逐行版的
    ``max(0.0, 1.0 - float(dist))``。

    两处细节都是等价性要求，不是风格：

    1. **先转 float64 再做减法**。逐行版的 ``float(_dist)`` 把 float32 距离精确
       提升到双精度，随后 ``1.0 - d`` 在双精度里算。若直接让 numpy 从 float32
       数组减 python 标量 1.0，NEP50 下结果仍是 float32——单精度舍入可能翻转
       边界上的 ``>= threshold`` 判定（实测 d=1e-7 时双精度得 0.9999999，单精度
       得 0.99999988；d=0.17 时 0.83 vs 0.82999998）。
    2. **NaN 归零**。python 的 ``max(0.0, nan)`` 返回 0.0（``nan > 0.0`` 为假，
       取首参），而 ``np.maximum(nan, 0.0)`` 返回 nan。阈值 <= 0 的部署下这个
       差异会让 NaN 距离凭空产出一条 NaN 权重的桥边，故显式补回旧语义。
    """
    import numpy as np

    sims = 1.0 - np.asarray(dists, dtype=np.float64)
    sims = np.where(np.isnan(sims), 0.0, sims)
    np.maximum(sims, 0.0, out=sims)
    return sims


def _binary_text_key(value: str) -> bytes:
    """Locale-free identifier order shared with persisted graph artifacts."""
    return value.encode("utf-8", "surrogatepass")


def _normalized_score_ranking(
    scores: Iterable[Tuple[str, float]], *, limit: int | None = None
) -> Tuple[List[Tuple[str, float]], int]:
    """Return the legacy stable descending min-max ranking with bounded memory.

    ``limit=None`` deliberately preserves the public ``scale_ppr`` diagnostic
    surface.  Production hydration only needs ``ppr_top_chunks``; for that path a
    min-heap retains the exact same first K rows, including input-order tie breaks,
    while still visiting every score to preserve the legacy global min/max.
    """
    if limit is not None and limit <= 0:
        return [], 0
    raw: List[Tuple[str, float]] = []
    heap: list[Tuple[float, int, str, int]] = []
    lo = float("inf")
    hi = float("-inf")
    count = 0
    for position, (chunk_id, value) in enumerate(scores):
        score = float(value)
        if not math.isfinite(score):
            raise ValueError("non_finite_ppr_score")
        count += 1
        lo = min(lo, score)
        hi = max(hi, score)
        if limit is None:
            raw.append((chunk_id, score))
            continue
        # Larger score wins; at an exact tie the earlier input position wins,
        # matching Python's stable ``sort(..., reverse=True)`` used previously.
        entry = (score, -position, chunk_id, position)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)
    if count == 0:
        return [], 0
    if limit is None:
        raw.sort(key=lambda item: item[1], reverse=True)
        selected = raw
    else:
        selected = [
            (chunk_id, score)
            for score, _neg_position, chunk_id, position in sorted(
                heap, key=lambda item: (-item[0], item[3])
            )
        ]
    span = hi - lo
    return [
        (chunk_id, (score - lo) / span if span > 0 else 0.0)
        for chunk_id, score in selected
    ], count


class GraphRetrievalService(_RetrievalState):
    _IN_CHUNK = 900
    # 300 triples * 3 params/triple = 900 —— 与上面的单值 `_IN_CHUNK=900` 同一
    # 数量级惯例。批量点查两个后端各有自己的悬崖:SQLite 的行值 IN 表达式树在
    # 大约 N≈1000-10000(依编译选项而定)会撞 SQLITE_LIMIT_EXPR_DEPTH;
    # PostgreSQL 的行值 IN 规划耗时对 N 近似二次增长,且在 N≈8000 附近会撞
    # planner 的递归栈深度上限。300 远在两条悬崖之前,且仍比逐条查询(旧实现)
    # 快至少两个数量级。
    _RELATION_SUPPORT_IN_CHUNK = 300
    _PPR_RERANK_SCHEMA = '{"relevant_ids": ["..."]}'

    def federated_retrieve(self, *args, **kwargs):
        return self._peer.federated_retrieve(*args, **kwargs)

    def _retrieve_chunks(self, *args, **kwargs):
        return self._peer._retrieve_chunks(*args, **kwargs)

    def _embed_query(self, *args, **kwargs):
        return self._peer._embed_query(*args, **kwargs)

    def _vector_matrix(self, *args, **kwargs):
        return self._peer._vector_matrix(*args, **kwargs)

    def _federated_graph_is_large(self, *args, **kwargs):
        return self._peer._federated_graph_is_large(*args, **kwargs)

    def _gather_kg_graph(self, *args, **kwargs):
        return self._peer._gather_kg_graph(*args, **kwargs)

    def cluster_map(self, notebook_id: str):
        return self._peer.cluster_map(notebook_id)

    def cluster_fold(self, notebook_id: str, object_ids):
        """Bounded canonical fold for exactly ``object_ids`` — never the full
        per-notebook cluster map (B2 hot-path audit, 批 1). Delegates to the
        same primitive candidate-hydration already uses
        (``_RetrievalState._candidate_cluster_map`` → ``cluster_fold_rows``,
        see its docstring): a member id missing from the result means "not a
        clustered member", and callers fall back to the id itself — the same
        ``dict.get(id, id)`` semantics ``cluster_map(notebook_id)`` gives over
        the full table, just scoped to the ids the caller actually needs."""
        return self._candidate_cluster_map(notebook_id, object_ids)

    def _edge_support_map(self, notebook_id: str) -> dict:
        with self._connect() as database:
            state = self.unified_kg.state_row(database, notebook_id)
        sequence = int(state["canonical_rel_seq"]) if state else -1
        def load():
            with self._connect() as database:
                return {
                    (row["canonical_src"], row["edge_type"], row["canonical_tgt"]):
                    (int(row["support_count"]), int(row["source_count"]))
                    for row in self.unified_kg.edge_support_rows(database, notebook_id)
                }
        return self._vector_cache.get(
            f"{notebook_id}:edge_support", ("edge_support", sequence), load
        )
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
        self.snapshots = snapshots
        self.scale_runtime = scale_runtime
        self.model_clients = model_clients
        self.model_error_sink = model_error_sink
        self.database = database

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
            active_row, base_rows = self.notebooks.participant_rows(
                db, active_notebook_id,
            )
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
            # O(1) monotonic seq triple per participant instead of per-nb
            # COUNT/MAX scans over relations+objects+clusters, recomputed on
            # every call (even cache HIT). kg_mutation_seq covers relation +
            # object writes AND edge-review flips (set_edge_review bumps it —
            # replaces the old per-status n_rej/n_ver columns);
            # cluster_mutation_seq covers rebuild_unified_kg. invalidate_kg
            # evicts every :fed_rxgraph entry (belt-and-braces incl. the
            # delete+reingest seq-reset case).
            version_parts = []
            for nb_id, _ in participants:
                version_parts.append((nb_id,) + self.unified_kg.graph_seq_row(db, nb_id))
            version = ("fed_rxgraph", tuple(version_parts))

            tier_map = {nb_id: nb_tier for nb_id, nb_tier in participants}

            def _load():
                nodes: dict = {}
                all_relations: list = []
                cluster_groups: dict = {}
                ph = ",".join("?" for _ in USABLE_STATUSES)
                # gate ii (T3, knowhow KG-node retrieval): the ONE switch for the
                # whole feature. Flag OFF ⇒ node meta carries no table_id/rows ⇒
                # byte-identical federated graph (and render → knowhow None). The
                # flag is process-constant (pydantic-settings loaded at startup),
                # so the version-cached graph never goes stale against it; a
                # restart that flips it clears the in-memory _vector_cache.
                # getattr fallback matches the Settings default so lightweight
                # settings doubles that omit the field retain production behavior.
                knowhow_on = getattr(
                    self.settings, "knowhow_kg_node_retrieval_enabled", True)
                for nb_id, _ in participants:
                    obj_rows = self.knowledge.graph_object_rows(
                        db, nb_id, USABLE_STATUSES,
                    )
                    for r in obj_rows:
                        p = json.loads(r["payload"] or "{}")
                        meta = {
                            "type": r["object_type"],
                            "name": p.get("name", ""),
                            "tier": tier_map.get(nb_id, "personal"),
                            "notebook_id": nb_id,
                        }
                        # A single knowhow cell KO carries table_id+rows in its
                        # payload (KnowhowProjector._ko_object_row §④). Thread the
                        # raw fields so build_rx_graph → render can compute the
                        # row-drawer ref via the shared, len(rows)==1-guarded
                        # anchor helper. Merged multi-row KOs still carry the raw
                        # rows; the render-side helper collapses them to None.
                        if (knowhow_on and p.get("table_id")
                                and isinstance(p.get("rows"), list)):
                            meta["table_id"] = p["table_id"]
                            meta["rows"] = p["rows"]
                        nodes[r["id"]] = meta
                    # Track E: rejected edges are excluded from the federated
                    # reasoning graph too (curation feedback loop) — bare
                    # filter; the column is NOT NULL DEFAULT 'pending'
                    # (migration runs in __init__), so NULL is impossible.
                    rel_rows = self.knowledge.graph_relation_rows(db, nb_id)
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
                    for r in self.unified_kg.cluster_member_rows(db, nb_id):
                        cluster_groups.setdefault(r["canonical_id"], []).append(
                            r["member_object_id"])
                # `or None`: no clusters → None-path (no kind tag, no hubs),
                # byte-identical to the pre-hub federated graph shape.
                return build_rx_graph(
                    nodes, all_relations, tier="personal", tier_map=tier_map,
                    cluster_groups=cluster_groups or None)

            return self._vector_cache.get(
                f"{active_notebook_id}:fed_rxgraph", version, _load)
    def _ppr_graph(self, notebook_id: str):
        """Build (and version-cache) the graph-mode PPR graph: KG nodes + chunk
        nodes + relation/membership/synonym(+variant) edges. Always spans active
        + base-tier notebooks (object_ids / chunk_ids are globally unique;
        concept_clusters share name-derived canonical_ids so a concept in active
        and base bridges naturally).
        返回 (G, key_to_idx, chunk_idx_to_id)。"""
        from app.services.kg.edge_schema import EDGE_SCHEMA_VERSION
        from app.services.kg.ppr import build_ppr_graph
        with self._connect() as db:
            participants = self.notebooks.participant_ids(db, notebook_id)
            # O(1) monotonic seq triple (kg_mutation_seq, cluster_mutation_seq,
            # mention_seq) per participant instead of 5 COUNT/MAX scans over
            # relations/objects/chunks/clusters + the mention read. kg_mutation_seq
            # covers object/relation/chunk writes; cluster_mutation_seq the
            # rebuild; mention_seq the co-mention bridge. invalidate_kg evicts
            # every :ppr_graph entry (belt-and-braces for the delete+reingest
            # seq-reset case where a base participant could collide on the triple).
            version_parts = []
            for nb in participants:
                version_parts.append((nb,) + self.unified_kg.graph_seq_row(db, nb))
        # runtime_dim 入键(T3/R4):emb_synonym 边由向量矩阵派生,切
        # EMBED_RUNTIME_DIM 后旧空间算出的图不能再服役。
        from app.services.vector_index import resolve_runtime_dim
        version = ("ppr_graph", EDGE_SCHEMA_VERSION, tuple(version_parts),
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
                    for r in self.knowledge.graph_object_rows(db, nb, USABLE_STATUSES):
                        kg_nodes[r["id"]] = {"type": r["object_type"],
                                             "name": json.loads(r["payload"] or "{}").get("name", "")}
                    for r in self.knowledge.graph_relation_rows(
                            db, nb, include_id_evidence=False):
                        relations.append(dict(r))
                    for r in self.chunks.id_rows(db, nb):
                        chunk_ids.append(r["id"])
                    for r in self.unified_kg.cluster_member_rows(db, nb):
                        cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])
            memberships = sorted(
                (
                    (oid, cid)
                    for nb in participants
                    for oid, cids in self._ent_chunk_map(nb).items()
                    for cid in cids
                ),
                key=lambda pair: (
                    _binary_text_key(pair[0]),
                    _binary_text_key(pair[1]),
                ),
            )
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
                rows = self.unified_kg.mention_rows(db, notebook_id)
            return [(r["claim_object_id"], f"cluster:{r['concept_canonical_id']}", w)
                    for r in rows]
        except Exception:
            return []
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
        scale_manifest = (self.scale_runtime.artifacts.scale_dir(notebook_id)
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
    def _delta_vector_matrix(self, db: object, notebook_id: str,
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
                rows = self.embeddings.vector_rows_for_ids(
                    db, notebook_id, table, id_col, batch,
                )
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
            _k = min(_syn_k, len(_bidx.ann_labels))
            for _start in range(0, len(_a_ids), _XBRIDGE_QUERY_BLOCK):
                _end = min(_start + _XBRIDGE_QUERY_BLOCK, len(_a_ids))
                try:
                    _labs, _dists = _ann.knn_query(
                        _a_mat_arr[_start:_end], k=_k)
                except Exception as _exc:  # noqa: BLE001 — fail-open
                    # Observability difference vs the historical per-row loop
                    # (registered, deliberate): one bad query now drops the
                    # bridges of a whole block (<= _XBRIDGE_QUERY_BLOCK rows)
                    # instead of a single row, and reports one model error
                    # instead of one per row.  Under THIS repository's usage of
                    # hnswlib — no mark_deleted anywhere, and k clamped by
                    # len(ann_labels) above — a knn_query error can only hold
                    # for the whole index (dim mismatch / corrupt handle), never
                    # for one vector, so a failing block would have failed
                    # row-by-row too.
                    self._note_model_error(
                        "scale_ppr_xbridge_query",
                        "",
                        _exc,
                        service="embedding",
                    )
                    continue
                # Similarity arithmetic lives in _xbridge_similarities so the
                # float64-before-subtraction and NaN-to-zero rules that make it
                # bit-for-bit equal to the old `max(0.0, 1.0 - float(dist))`
                # are testable without a real ANN index (see its docstring).
                _labs = np.asarray(_labs)
                _sims = _xbridge_similarities(_dists)
                # np.nonzero yields row-major order, so walking the survivors
                # reproduces the per-row loop's emission order exactly
                # (row order × neighbour order).  Filtering on the threshold
                # before the exact-id check is order-preserving: both are pure
                # `continue` filters in the old loop.
                _rows, _cols = np.nonzero(_sims >= _syn_thr)
                for _r, _c in zip(_rows.tolist(), _cols.tolist()):
                    _a_id = _a_ids[_start + _r]
                    _base_nid = _bidx.ann_labels[int(_labs[_r, _c])]
                    if _base_nid == _a_id:
                        continue  # exact-id: already unified by splice_active
                    _sim = float(_sims[_r, _c])
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
        active_indexed = (self.scale_runtime.artifacts.scale_dir(notebook_id)
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
            # Direct query of one indexed notebook with delta search disabled is
            # the dominant large-library shape.  The combined graph is exactly
            # that ScaleIndex: reuse its multi-million-entry id list/map/idf by
            # identity instead of duplicating them during startup/first query.
            # Only the PPR float32 transition needs a conversion copy.
            if (
                len(base_indexes) == 1
                and first_id == notebook_id
                and active_indexed
                and not self.settings.scale_search_include_delta
            ):
                combined_A = getattr(first, "_ppr_transition", first.transition)
                if self.settings.ppr_float32 and not hasattr(
                    first, "_ppr_transition"
                ):
                    combined_A = combined_A.astype(np.float32, copy=False)
                combined_chunk_ids = getattr(first, "_ppr_chunk_ids", None)
                if combined_chunk_ids is None:
                    combined_chunk_ids = {
                        first.node_ids[int(i)]
                        for i in first.chunk_index
                        if 0 <= int(i) < len(first.node_ids)
                    }
                return {
                    "combined_ids": first.node_ids,
                    "combined_A": combined_A,
                    "combined_index": first.node_index,
                    # Keep the historical set iteration/tie behavior exactly;
                    # the memory win comes from reusing the much larger node
                    # list/map/idf, not from changing ranking order here.
                    "combined_chunk_ids": combined_chunk_ids,
                    "combined_idf": first.idf,
                }
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
                for i, nid in enumerate(idx.node_ids):
                    combined_idf_map.setdefault(nid, float(idx.idf[i]))
                for i in idx.chunk_index:
                    if 0 <= int(i) < len(idx.node_ids):
                        combined_chunk_ids.add(idx.node_ids[int(i)])

            if len(base_indexes) > 1:
                # One construction, exactly matching the old ordered left fold.
                # In particular, duplicates among earlier libraries collapse to
                # structure before the last library is added; duplicates in the
                # last library retain the historical 2x pre-normalisation weight.
                combined_ids, combined_A = si.splice_csr_many(
                    list(first.node_ids),
                    first.transition,
                    [
                        (list(idx.node_ids), idx.transition)
                        for _bid, idx in base_indexes[1:]
                    ],
                )

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
                combined_A = combined_A.astype(np.float32, copy=False)

            return {
                "combined_ids": combined_ids,
                "combined_A": combined_A,
                "combined_index": combined_index,
                "combined_chunk_ids": combined_chunk_ids,
                "combined_idf": combined_idf,
            }

        return self._vector_cache.get(
            f"{notebook_id}:scale_combined", version, _load)

    def _scale_ppr_impl(
        self, notebook_id: str, question: str, *, max_results: int | None = None
    ) -> List[Tuple[str, float]]:
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
            base_ids = self.notebooks.participant_ids(db, notebook_id)[1:]
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
                    self._note_model_error(
                        "scale_ppr_ann",
                        "",
                        exc,
                        service="embedding",
                    )
                    continue
                for lab, dist in zip(labels[0], distances[0]):
                    node_id = idx.ann_labels[int(lab)]
                    ci = combined_index.get(node_id)
                    if ci is None:
                        continue
                    sim = max(0.0, 1.0 - float(dist))  # cosine distance → similarity
                    if sim > 0:
                        # Keep the historical generic-composition arithmetic:
                        # it materialized IDF through ``float(...)`` before the
                        # float64 reset accumulation.  The self-only fast path
                        # reuses the persisted float32 IDF array by identity, so
                        # an explicit cast prevents a 1-ULP seed/ranking drift.
                        reset[ci] += sim * float(combined_idf[ci])
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
                        reset[ci] += float(sim) * float(combined_idf[ci])
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
        try:
            norm, chunks_considered = _normalized_score_ranking(
                (
                    (cid, float(x[ci]))
                    for cid in combined_chunk_ids
                    if (ci := combined_index.get(cid)) is not None
                ),
                limit=max_results,
            )
        except ValueError as exc:
            if str(exc) != "non_finite_ppr_score":
                raise
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "non_finite_chunk_score",
            })
            return []
        if not norm:
            self.event_log.emit({
                "kind": "scale_ppr_bailout",
                "notebook_id": notebook_id,
                "reason": "no_chunk_nodes",
            })
            return []
        self.event_log.emit({
            "kind": "scale_ppr_done", "notebook_id": notebook_id,
            "iters": _ppr_stats.get("iters", -1),
            "ppr_ms": round((time.perf_counter() - t_ppr0) * 1000),
            "nodes": len(combined_ids), "seeds": ann_seeds + active_seeds + chunk_seeds,
            "chunks_considered": chunks_considered,
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
        chunk 模式三路 mix;曾经的 graph 引擎 PPR 分支同样如此,该 ask 模式已
        退役)均已对 [] 容错降级。小库保留旧回退路径,字节不变。"""
        from app.services.kg.ppr import run_ppr
        top_chunks = self.settings.ppr_top_chunks
        ranked = self.scale_ppr(
            notebook_id,
            question,
            # Preserve the historical zero/negative configuration behavior:
            # scale PPR still succeeds and the legacy slice below decides the
            # empty/negative-prefix result, rather than treating the setting as
            # a scale failure and constructing the fallback graph.
            max_results=top_chunks if top_chunks > 0 else None,
        )
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
        ranked = ranked[:top_chunks]
        if not ranked:
            return []

        score_map = dict(ranked)
        with self._connect() as db:
            rows = self.chunks.graph_hydrate_rows(db, score_map)
        from app.services.retrieval import RetrievedChunk, RetrievalSupport
        # combined_chunk_ids (scale_ppr) spans base ⊕ active — a chunk here can
        # belong to a base notebook even though this call is scoped to
        # `notebook_id`. Tag each with its real origin so citation-building can
        # resolve tier correctly (see Citation.tier).
        out = [RetrievedChunk(
            chunk_id=r["id"], source_id=r["source_id"], source_title=r["source_title"],
            section_path=r["section_path"], text=r["text"],
            element_ids=json.loads(r["element_ids"] or "[]"),
            relevance=score_map[r["id"]],
            notebook_id=r["chunk_notebook_id"],
            retrieval_supports=(RetrievalSupport(
                "ppr", "ppr", "", score_map[r["id"]]
            ),)) for r in rows]
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
    def _ppr_fact_rerank(self, question: str, kg_hits: list) -> list:
        """Recognition memory:LLM 过滤候选 KG 种子,只留与 question 相关的。
        fail-open:LLM 未配/报错/非法返回/过滤后为空 → 原样返回 kg_hits(绝不因
        rerank 失败而清空种子)。"""
        client = self.model_clients.chat("evidence_refine")
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
                "ppr_fact_rerank", exc, workload_id="evidence_refine"
            )
            return kg_hits
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
            start = self.knowledge.follow_start_row(
                db, start_object_id, active_notebook_id, USABLE_STATUSES,
            )
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
                rows = self.knowledge.follow_endpoint_rows(
                    db, owner_notebook_id, oid, endpoint, raw_scan_limit + 1,
                )
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
                    for row in self.knowledge.follow_relation_evidence_rows(db, batch):
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
                for row in self.knowledge.follow_object_rows(
                        db, owner_notebook_id, batch, USABLE_STATUSES):
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
    def _elem_chunk_map(self, notebook_id: str) -> Dict[str, list]:
        """Cached {element_id: [chunk_id, ...]} — one chunks scan per version,
        reused by both _kg_source_chunks (per-query, few object_ids) and
        _ent_chunk_map (whole-notebook membership for PPR). P0-5: this used to
        be re-scanned (all chunks, per-row json.loads) on every call of either
        consumer; now it's version-cached like _vector_matrix/_keyword_token_sets."""
        version = tuple(self._scale_index_version(notebook_id))

        def _load():
            with self._connect() as db:
                chunk_rows = self.chunks.id_element_rows(db, notebook_id)
            out: Dict[str, list] = {}
            for cr in chunk_rows:
                for el in json.loads(cr["element_ids"] or "[]"):
                    out.setdefault(el, []).append(cr["id"])
            return out

        return self._vector_cache.get(f"{notebook_id}:elemchunk", version, _load)

    def _elem_chunks_scoped(
        self, db, notebook_id: str, elem_ids: list
    ) -> Dict[str, list]:
        """{element_id: [chunk_id, ...]} for THESE elements only.

        Two paths, and the fork is the persisted ``chunk_elements_indexed``
        marker (one indexed single-row read):

        - backfilled notebook: an indexed point lookup on the ``chunk_elements``
          reverse index, bounded by the handful of evidence elements this query
          actually hit;
        - not yet backfilled: the legacy version-cached whole-notebook
          ``_elem_chunk_map``, byte-for-byte unchanged.

        Both return per-element chunk lists in chunk insertion order, so the
        caller's first-seen ordering is unaffected by which path ran.
        """
        if not self.knowledge.chunk_elements_indexed(db, notebook_id):
            return self._elem_chunk_map(notebook_id)
        scoped: Dict[str, list] = {}
        for row in self.chunks.chunks_for_element_ids(db, notebook_id, elem_ids):
            scoped.setdefault(row["element_id"], []).append(row["chunk_id"])
        return scoped

    def _kg_source_chunks(
        self, notebook_id: str, object_ids: list, *, support_by_object=None
    ) -> list:
        """KG 对象 evidence 的 element_id → 含该 element 的 chunk(LightRAG 源 chunk)。
        返回 List[RetrievedChunk](relevance 占位 0.3,后续 rerank 重排)。

        P0-5: object_ids 是本次查询命中的一小撮 KG 对象(不是全库),所以 evidence
        只按 IN(...) 取这几行;element_id → chunk 的反查改走缓存的 _elem_chunk_map,
        不再对 chunks 表做全量扫描 + 逐行 json.loads + 集合交。

        输出序 = 确定性 first-seen 序:按 object_ids 顺序 → 各对象 evidence 内
        element 顺序 → 该 element 的 chunk 列表序。消费方 `_mix_retrieve`(chunk
        模式的 KG-overlay 分支)把这个序喂给 retrieval_rerank 作为输入序;
        rerank 未配置或调用失败时 RerankClient.rerank 原样退回这个序(identity
        fallback)——两种情形下这个序都直接决定 truncate_by_tokens 的截断存活集
        和引用编号,所以序必须确定(旧实现的「chunks 全表扫描序」依赖表物理序,
        本就不是契约)。

        批 5:element→chunk 反查改由 ``_elem_chunks_scoped`` 按标记分叉——已回填
        的 notebook 走 ``chunk_elements`` 有界点查(SQLite ``ORDER BY rowid`` /
        PostgreSQL ``ORDER BY ordinal``,即插入序),未回填的仍走原全量缓存,
        逐字不变。"""
        from app.services.retrieval import (
            RetrievedChunk, RetrievalSupport, merge_retrieval_supports,
        )
        if not object_ids:
            return []
        with self._connect() as db:
            erows = self.knowledge.object_evidence_rows(db, object_ids)
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
            elem_map = self._elem_chunks_scoped(db, notebook_id, elem_ids)
            chunk_ids: list = []
            seen_cid = set()
            chunk_support_objects: Dict[str, list[str]] = {}
            object_elements: Dict[str, list[str]] = {}
            for oid in object_ids:
                object_elements[oid] = [
                    e.get("element_id")
                    for e in json.loads(ev_by_id.get(oid) or "[]")
                    if isinstance(e, dict) and e.get("element_id")
                ]
            for el in elem_ids:
                for cid in elem_map.get(el, ()):
                    if cid not in seen_cid:
                        seen_cid.add(cid)
                        chunk_ids.append(cid)
                    for oid in object_ids:
                        if el in object_elements.get(oid, ()):
                            chunk_support_objects.setdefault(cid, []).append(oid)
            if not chunk_ids:
                return []
            crows = self.chunks.rows_by_ids(db, chunk_ids)
        by_id = {cr["id"]: cr for cr in crows}
        out = []
        for cid in chunk_ids:
            cr = by_id.get(cid)
            if cr is None:
                continue
            supports = tuple(
                RetrievalSupport("kg_source", "object", oid, 0.3)
                for oid in dict.fromkeys(chunk_support_objects.get(cid, ()))
            )
            if support_by_object:
                for oid in dict.fromkeys(chunk_support_objects.get(cid, ())):
                    supports = merge_retrieval_supports(
                        supports, support_by_object.get(oid, ())
                    )
            out.append(RetrievedChunk(
                chunk_id=cr["id"], source_id=cr["source_id"], source_title="",
                section_path=cr["section_path"], text=cr["text"],
                element_ids=json.loads(cr["element_ids"] or "[]"), relevance=0.3,
                retrieval_supports=supports))
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
                obj_rows = self.knowledge.notebook_object_evidence_rows(db, notebook_id)
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

    def ppr_retrieve(self, *args, **kwargs):
        from app.services.source_scope import filter_retrieval_items

        notebook_id = str(args[0] if args else kwargs.get("notebook_id", ""))
        return filter_retrieval_items(
            notebook_id, "chunk", self._ppr_retrieve(*args, **kwargs)
        )

    def follow_chain(self, *args, **kwargs):
        return self._follow_chain(*args, **kwargs)

    def node_context(self, *args, **kwargs):
        # Deliberately ungated: this method's two consumers each gate at their
        # own boundary, and neither is served by a gate here.
        # ``RetrievalService.node_context`` filters the row it gets back
        # (reasoning's chain hydration).  ``EvidenceContextService
        # .knowledge_context`` skips an out-of-scope hit BEFORE calling this at
        # all — it has to, because emptying the row would still leave the
        # object's NAME rendering into the prompt behind a live ``k{n}`` anchor,
        # the row being only the definition/snippet half of what it prints.
        return self.knowledge.node_context(*args, **kwargs)

    def scale_ppr(self, *args, **kwargs):
        return self._scale_ppr_impl(*args, **kwargs)

    def in_network_relations(self, participant_ids, object_ids):
        from app.services.kg.edge_schema import is_queryable_edge_pair

        ids = list(object_ids)
        if len(ids) < 2:
            return []
        out = []
        with self._connect() as database:
            for notebook_id in participant_ids:
                rows = self.knowledge.in_network_relation_rows(
                    database, notebook_id, ids,
                )
                out.extend(
                    {**dict(row), "notebook_id": notebook_id}
                    for row in rows
                    if is_queryable_edge_pair(
                        row["edge_type"], row["source_type"], row["target_type"]
                    )
                )
        return out

    def relation_support_count(self, notebook_id, source_id, edge_type, target_id):
        # 保留:唯一生产调用方(evidence_context.knowledge_context)已改用下面
        # 的批量 relation_support_counts(B1 热点整改批 1)——逐条调用每次都要
        # 整表冷缓存 _edge_support_map(生产 8.35M 行 canonical_relations,
        # ~3.6GB dict)+ cluster_map(同样整表)。不删是因为它仍是这条语义的
        # 唯一真源(批量实现按它的行为差分钉住,见 relation_support_counts
        # docstring),且仍有测试双态实现着它。
        support = self._edge_support_map(notebook_id)
        clusters = self.cluster_map(notebook_id)
        hit = support.get((clusters.get(source_id, source_id), edge_type,
                           clusters.get(target_id, target_id)))
        return hit[1] if hit else 1

    def relation_support_counts(self, notebook_id, raw_triples):
        """Batched replacement for calling ``relation_support_count`` once
        per triple. Semantic mapping against the single-triple
        implementation above (must match it exactly on every input):

            单条实现(每次调用都各自整表扫描)          批量实现的对应步骤
            ------------------------------------      --------------------------------
            self._edge_support_map(notebook_id)        一次 relation_support_rows() 定点
              (整表冷缓存, 生产 8.35M 行~3.6GB)          查询(PK 精确匹配, 至多几十行)
            self.cluster_map(notebook_id)               self._candidate_cluster_map(
              .get(id, id)  (整表冷缓存)                   notebook_id, ids)  (有界 fold,
                                                           同一张表、同一 .get(id, id) 回退)
            hit[1] if hit else 1                        同一规则:命中返回 source_count
                                                           (canonical_relations 的
                                                           support/source 元组第二项),
                                                           未命中返回 1

        ``raw_triples``: iterable of ``(raw_src, edge_type, raw_tgt)`` — the
        object ids exactly as they appear in the caller's relation rows,
        BEFORE canonical folding (folding happens inside this method, scoped
        to ``notebook_id`` — the single notebook the relation rows belong to,
        never the full participant list; this mirrors the single-triple
        implementation's ``self.cluster_map(notebook_id)``, not the reversed
        multi-participant fold ``EvidenceContextService._canonical`` uses for
        admitting hits). Returns a dict keyed by those exact raw triples —
        every input key is present in the output (duplicates collapse).

        The fold two lines below is deliberately its OWN bounded
        ``_candidate_cluster_map`` call, not a reuse of
        ``EvidenceContextService.knowledge_context``'s ``participant_folds``
        (computed earlier, once per participant, over the hit/priority id
        set). Two independent reasons, both load-bearing — don't "optimize"
        this into sharing that fold: (1) this method takes RAW triples and
        must match the single-triple ``relation_support_count`` above
        triple-for-triple in the differential tests
        (``test_relation_support_counts_matches_single_triple_implementation``)
        — accepting a pre-folded/shared structure would break that pinning;
        (2) ``participant_folds`` is indexed by participant position
        (reverse-scanned, "later participant wins"), a semantic specific to
        admitting KNOWLEDGE HITS across a federation. Folding relation
        endpoints is a single-notebook operation (the relation row's own
        ``notebook_id``, never the full participant list — see above) and
        leaking the participant-indexed shape into this method's signature
        would couple two independently-varying concerns.
        """
        raw_triples = list(raw_triples)
        if not raw_triples:
            return {}
        ids: list = []
        seen_ids: set = set()
        for raw_src, _edge_type, raw_tgt in raw_triples:
            for object_id in (raw_src, raw_tgt):
                if object_id not in seen_ids:
                    seen_ids.add(object_id)
                    ids.append(object_id)
        folded = self._candidate_cluster_map(notebook_id, ids)
        canonical_by_raw: dict = {}
        lookup_triples: set = set()
        for raw_triple in raw_triples:
            raw_src, edge_type, raw_tgt = raw_triple
            canonical_triple = (
                folded.get(raw_src, raw_src), edge_type, folded.get(raw_tgt, raw_tgt),
            )
            canonical_by_raw[raw_triple] = canonical_triple
            lookup_triples.add(canonical_triple)
        # sorted(), not list(): a bare ``set`` iterates in hash order, which is
        # randomized per-process (``PYTHONHASHSEED``) for str keys. That would
        # make the batch boundaries below — and therefore the exact SQL text
        # issued to either backend — non-deterministic across runs for the
        # SAME logical input, undermining the determinism T2
        # (``in_network_relation_rows``'s ``DISTINCT`` dedup) already
        # established one layer up. Sorting costs nothing observable (this
        # list is at most a few hundred triples) and makes query batching
        # reproducible.
        ordered_lookup_triples = sorted(lookup_triples)
        rows: list = []
        with self._connect() as database:
            for offset in range(0, len(ordered_lookup_triples), self._RELATION_SUPPORT_IN_CHUNK):
                batch = ordered_lookup_triples[offset:offset + self._RELATION_SUPPORT_IN_CHUNK]
                rows.extend(self.unified_kg.relation_support_rows(
                    database, notebook_id, batch,
                ))
        support_by_canonical = {
            (row["canonical_src"], row["edge_type"], row["canonical_tgt"]):
            int(row["source_count"])
            for row in rows
        }
        return {
            raw_triple: support_by_canonical.get(canonical_triple, 1)
            for raw_triple, canonical_triple in canonical_by_raw.items()
        }
