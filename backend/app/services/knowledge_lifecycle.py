"""Knowledge lifecycle and unified-KG orchestration (Task 15).

Owns the KG lifecycle surface the facade previously carried inline:
deletion / full-notebook build / rebuild (with the ``kg_building`` in-flight
flag), store/relink of extracted graphs, cluster writes, incremental source
fusion, the unified-KG reads (status / graph / neighbors) and the streamed
unified rebuild plus its derived layers (canonical relations, mention
bridge, communities and community reports).

Composition rules (Gate 5): no facade import. Persistence goes through the
injected Task-13 stores; transactions ride the injected ``write``/``connect``
seats (the facade's ``_write``/``_connect`` compatibility seams, resolved at
call time so transaction-counting / failure-injection monkeypatches keep
observing every commit boundary). Post-commit KG side effects keep funnelling
through the facade's ``_invalidate_unified_cache`` / ``_mark_unified_kg_dirty``
/ ``_bump_cluster_mutation_seq`` wrappers (injected late) — the Task-14
coordinator stays the single dirty entry and the frozen mutation phase matrix
is unchanged. Until Gate 6 the scale/viz domain is reached through callable
adapters (scale-index load / ANN open / viz index+probe / build-viz /
auto-index / copy-stats), all late-bound to the facade so frozen repo-level
patch seats keep working.

Red lines preserved verbatim from the facade:
- ``rebuild_unified_kg``'s resume/cache semantics: the ``cluster_input_version``
  (v2) O(1) skip gate, ``force = force or fresh`` boundary self-defense,
  checkpoint GC AFTER the skip gate, the ``exclude_emb_count`` checkpoint
  version namespace and the merge-review / concept-desc checkpoint resume.
- ``_stream_seed_reps`` / ``_write_cluster_map_streamed`` ORDER BY rowid
  anchoring (PR#136: a covering index must never perturb canonical order).
- Concept-description concurrency: the kg LLM client is resolved ONCE in the
  main thread (worker threads don't inherit the request ContextVar).
- Merge-review batching + fail-open (the try wraps the whole adjudication
  block, never just json.loads).
- The ``kg_building`` single-flight guard covers the delete phase of a full
  rebuild.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.repositories.sqlite.governance_store import GovernanceStore
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.knowledge_governance import KnowledgeGovernanceService


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


class KnowledgeLifecycleService:
    def __init__(
        self,
        *,
        settings: Settings,
        event_log: EventLogger,
        knowledge: KnowledgeStore,
        governance_store: GovernanceStore,
        unified_kg: UnifiedKgStore,
        governance: KnowledgeGovernanceService,
        kg_building: set,
        unified_cache: Dict[Any, Any],
        scale_artifacts: Any,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        connect: Callable[[], sqlite3.Connection],
        write: Callable[[], Any],
        get_notebook: Callable[[str], Any],
        invalidate_unified_cache: Callable[[str], None],
        mark_unified_kg_dirty: Callable[[str], None],
        bump_cluster_mutation_seq: Callable[[sqlite3.Connection, str], None],
        embed_objects_batch: Callable[..., None],
        embed_relations_batch: Callable[[str, List[dict]], None],
        source_ids_from_evidence: Callable[[Optional[str]], set],
        set_source_status: Callable[..., None],
        run_extraction: Callable[[str], None],
        llm: Callable[[], Any],
        kg_llm: Callable[[], Any],
        cluster_map: Callable[[str], Dict[str, str]],
        annotate_edge_support: Callable[[str, List[dict]], List[dict]],
        decided_seed_pairs: Callable[[str], Dict[frozenset, str]],
        relations_for_notebook: Callable[[str], List[dict]],
        notebook_copy_stats: Callable[[str], dict],
        note_model_error: Callable[..., None],
    ) -> None:
        self.settings = settings
        self.event_log = event_log
        self.knowledge = knowledge
        self.governance_store = governance_store
        self.unified_kg = unified_kg
        self.governance = governance
        # KG build/rebuild 的进行中标志(进程内;重启后天然为空=未构建,无需 reconcile)。
        # 集合本体归 NotebookCatalogService 所有(get_notebook 在那读成员资格);
        # 本服务持同一个 set 对象,build/rebuild 路径照旧 add/discard;facade 的
        # `_kg_building`/`_kg_building_lock` 属性别名这里的同一对象。
        self.kg_building = kg_building
        self.kg_building_lock = threading.Lock()
        # The facade's EXISTING cache objects, held BY IDENTITY (never
        # replacement copies) — the Task-14 coordinator evicts the same dict.
        self.unified_cache = unified_cache
        self.scale_artifacts = scale_artifacts
        self._new_id = new_id
        self._now = now
        self._connect = connect
        self._write = write
        self.get_notebook = get_notebook
        self._invalidate_unified_cache = invalidate_unified_cache
        self._mark_unified_kg_dirty = mark_unified_kg_dirty
        self._bump_cluster_mutation_seq = bump_cluster_mutation_seq
        self._embed_objects_batch = embed_objects_batch
        self._embed_relations_batch = embed_relations_batch
        self._source_ids_from_evidence = source_ids_from_evidence
        self._set_source_status = set_source_status
        self._run_extraction = run_extraction
        self._llm = llm
        self._kg_llm = kg_llm
        self.cluster_map = cluster_map
        self._annotate_edge_support = annotate_edge_support
        self.decided_seed_pairs = decided_seed_pairs
        self.relations_for_notebook = relations_for_notebook
        self.notebook_copy_stats = notebook_copy_stats
        self._note_model_error = note_model_error

    # Late-bound model clients: resolved per call through the facade's frozen
    # properties, so class-property monkeypatches (type(repo).kg_llm_client)
    # and the mutable llm_client setter keep being observed — and the per-user
    # ContextVar resolution stays on the calling thread.
    @property
    def llm_client(self):
        return self._llm()

    @property
    def kg_llm_client(self):
        return self._kg_llm()

    # ------------------------------------------------------------------
    # KG deletion / store / relink / cluster writes / incremental fusion
    # ------------------------------------------------------------------

    def delete_notebook_kg(self, notebook_id: str) -> dict:
        """Delete all KG artifacts for a notebook (objects, relations, clusters,
        merge candidates, embeddings, extraction runs, unified state) while KEEPING
        sources and source_elements so it can be re-extracted from already-parsed
        elements. Returns {table: rows_deleted}."""
        self.get_notebook(notebook_id)
        with self._write() as db:
            counts = self.knowledge.delete_notebook_graph_rows(db, notebook_id)
        self._invalidate_unified_cache(notebook_id)
        return counts

    def store_kg(self, notebook_id: str, source_id: Optional[str],
                 objects: List[dict], relations: List[dict]) -> Tuple[int, int]:
        """Insert KG nodes/edges (remapping local ids to DB ids), embeds payload.

        分块写入(每块 CHUNK 行, 各自一个 _write() 事务), 避免单源 2.6万行塞一个
        事务长时间持锁。本地 id->DB id 在分块前一次性预分配, 跨块关系仍能正确
        remap。代价: 失整源原子性(崩溃可能留半本); _run_extraction 逐源自清 +
        可重跑兜底。Relations 引用不到的 local id 静默跳过。"""
        CHUNK = 1000
        now = self._now()
        local_to_id: Dict[str, str] = {}
        for obj in objects:
            local_to_id[obj["local_id"]] = self._new_id("ko")
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
                "_rid": self._new_id("rel"),
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
            nb_row = self.knowledge.notebook_tier_row(db, notebook_id)
        auto_status = 'reviewed' if (nb_row and nb_row["tier"] == 'base') else 'approved'

        for i in range(0, len(objects), CHUNK):
            chunk = objects[i:i + CHUNK]
            with self._write() as db:
                self.knowledge.insert_object_chunk(
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
                    self.knowledge.insert_kg_fts_rows(db, fts_rows)
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
                    self.knowledge.insert_object_source_rows(db, kos_rows)
        for i in range(0, len(db_relations), CHUNK):
            chunk = db_relations[i:i + CHUNK]
            with self._write() as db:
                self.knowledge.insert_relation_chunk(
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
            obj_rows, rel_rows, valid_src = self.knowledge.relink_rows(db, notebook_id)

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
        now = self._now()
        new_rows = []
        for e in proposed:
            triple = (e["source_object_id"], e["target_object_id"], e["edge_type"])
            if triple in existing_triples:
                continue
            existing_triples.add(triple)
            src = src_by_id.get(e["source_object_id"], "")
            new_rows.append((
                self._new_id("rel"), notebook_id,
                src if src in valid_src else None,   # NULL if source gone (FK-safe)
                e["source_object_id"], e["target_object_id"], e["edge_type"],
                json.dumps([{"basis": e["basis"], "quote": ""}], ensure_ascii=False),
                now,
            ))

        if new_rows:
            with self._write() as db:
                self.knowledge.insert_relation_chunk(db, new_rows)
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

    # --- Concept-cluster writes ---------------------------------------------

    def write_clusters(self, notebook_id: str, rows: List[dict],
                       object_type: str = "concept") -> None:
        now = self._now()
        with self._write() as db:
            self.governance_store.delete_clusters(db, notebook_id, object_type)
            self.governance_store.insert_clusters(
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
        now = self._now()
        with self._write() as db:
            added = self.governance_store.insert_clusters(
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
            swept = self.governance_store.sweep_orphan_clusters(db, notebook_id)
            # P0-A: only bump if this orphan-sweep actually deleted rows — the
            # later append_clusters calls in this method self-bump on their own
            # additions, so this guards just the DELETE branch (no double-count,
            # no fake signal on a no-op sweep).
            if swept > 0:
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
            new = self.knowledge.incremental_object_rows(
                db, notebook_id, source_id, "concept"
            )
            cn = self.governance_store.incremental_cluster_rows(
                db, notebook_id, "concept"
            )
        new_objs = [{"object_id": r["id"],
                     "name": json.loads(r["payload"] or "{}").get("name", "")} for r in new]
        if new_objs:
            canon_names = {r["canonical_id"]: r["canonical_name"] for r in cn}
            rows = place_new_concepts(new_objs, cmap, canon_names,
                                      seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
            self.append_clusters(notebook_id, rows, object_type="concept")
            with self._connect() as db:
                ex = self.knowledge.incremental_object_rows(
                    db, notebook_id, source_id, "concept", exclude_source=True
                )
            # Tier2 桥接候选来源三分支(P1-3,perf audit):
            #   1) 有可用 kg ANN(即使版本漂移/stale,advisory 桥接可接受)→ ANN 近邻查询,
            #      任意规模可用,恢复大库(> max_entities)上一直被静默跳过的跨文档桥接。
            #      stale 索引只缺"新↔新"对象自身(下轮重建后补),"新↔存量"这一主场景
            #      不受影响(见 _tier2_bridge_candidates_ann 文档)。
            #   2) 无索引且已有 concept 数 ≤ max_entities → 原暴力 O(new×existing) 余弦(不动)。
            #   3) 无索引且已有 concept 数 > max_entities → 跳过,但显式发 tier2_skipped 事件
            #      (P1-3 修复点:旧代码这里静默跳过,大库上 Tier2 从未真正跑过)。
            idx = self.scale_artifacts.load(notebook_id, allow_stale=True)
            ann = self.scale_artifacts.open_ann(idx, "kg") if (idx is not None and idx.ann_labels) else None
            cands: list = []
            if ann is not None:
                cands = self._tier2_bridge_candidates_ann(
                    notebook_id, idx, ann, new_objs, cmap)
            elif len(ex) <= self.settings.kg_incremental_tier2_max_entities:
                with self._connect() as db:
                    vrows = self.knowledge.embedding_rows(db, notebook_id)
                    pend = self.governance_store.merge_candidate_pairs(
                        db, notebook_id, ("pending",)
                    )
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
                    _decided = self.governance_store.merge_candidate_pairs(
                        _db, notebook_id, ("confirmed", "rejected", "deferred")
                    )
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
                now = self._now()
                with self._write() as db:
                    for c in cands:
                        self.governance_store.insert_merge_candidate(
                            db, notebook_id, c["canonical_a"], c["canonical_b"],
                            c["score"], now, id_prefix="cm")
        from app.services.kg_merge import seed_claim, seed_formula, seed_procedure
        _TYPES = {"claim": (seed_claim, "KL-"), "formula": (seed_formula, "KF-"),
                  "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPES.items():
            with self._connect() as db:
                trows = self.knowledge.incremental_object_rows(
                    db, notebook_id, source_id, t
                )
                tcn = self.governance_store.incremental_cluster_rows(
                    db, notebook_id, t
                )
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
            new_rows = self.knowledge.embedding_rows_for_objects(
                db, notebook_id, [o["object_id"] for o in new_objs]
            )
            _decided = self.governance_store.merge_candidate_pairs(
                db, notebook_id, ("confirmed", "rejected", "deferred", "pending")
            )
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
                alive = self.governance_store.valid_object_ids(db, unknown)
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

    # ------------------------------------------------------------------
    # Full-notebook build / rebuild (kg_building single-flight guard)
    # ------------------------------------------------------------------

    def build_notebook_kg(self, notebook_id: str, *, progress=None) -> dict:
        """按需对该 notebook 下"尚无 KG"的 source 抽取(复用 _run_extraction)。
        幂等:已有 knowledge_objects 的 source 跳过。无 LLM → RuntimeError。
        跨源**并发**(提交到全局 KG job 池;窗口仍由全局 window 池封顶,两池分离防死锁);
        单 source 失败隔离,不连累其余、不回退其终态,错误入 event log。
        progress(i, n, source_id, ok):可选回调,每抽完一源调一次(批量 CLI 显示进度用)。"""
        self.get_notebook(notebook_id)  # KeyError if missing
        with self.kg_building_lock:
            self.kg_building.add(notebook_id)
        try:
            if not getattr(self.llm_client, "configured", False):
                raise RuntimeError("LLM not configured; cannot build KG")
            with self._connect() as db:
                src_ids, kgful = self.knowledge.source_build_rows(db, notebook_id)
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
                    self.governance.resolve_notebook_conflicts(notebook_id)
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
            self.scale_artifacts.maybe_enqueue_fold(notebook_id)
            return result
        finally:
            with self.kg_building_lock:
                self.kg_building.discard(notebook_id)

    def rebuild_notebook_kg(self, notebook_id: str) -> dict:
        """Full re-extract: wipe all KG artefacts, then build from scratch.

        Equivalent to delete_notebook_kg + build_notebook_kg but in a single
        call so background threads don't need to chain the two methods.
        After delete every source has no KG, so build re-extracts all of them;
        build's tail relink pass runs automatically.
        Missing-notebook KeyError is raised by delete_notebook_kg's own
        get_notebook guard (unchanged) — no separate pre-check here, so this
        wrapper adds zero new behavior on the invalid-id path."""
        # kg_building must cover the delete phase too — large notebooks
        # (590k+ objects) can spend >6s in delete_notebook_kg alone, and the
        # frontend polls every 6s. Without this wrapper, a poll landing during
        # delete reads kg_building=False (build_notebook_kg hasn't set it yet)
        # and the UI reports "build complete" while the job is still running.
        # Nesting is safe: set.add is idempotent, so build_notebook_kg's own
        # add is a no-op; its finally-discard fires when build ends (the last
        # step of rebuild), and this outer finally-discard is then a no-op too.
        with self.kg_building_lock:
            self.kg_building.add(notebook_id)
        try:
            self.delete_notebook_kg(notebook_id)
            return self.build_notebook_kg(notebook_id)
        finally:
            with self.kg_building_lock:
                self.kg_building.discard(notebook_id)

    # ------------------------------------------------------------------
    # Unified-KG reads (status / graph / neighbors)
    # ------------------------------------------------------------------

    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = self.unified_kg.state_row(db, notebook_id)
            clusters = self.unified_kg.distinct_cluster_count(db, notebook_id)
        viz = self.scale_artifacts.viz_probe(notebook_id)
        viz_building = notebook_id in self.scale_artifacts.viz_building
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
        Python dicts AND caches the multi-GB result in self.unified_cache,
        which is how a 490k-object production library fills 64GB RAM. This
        applies regardless of `limit`/`level`: a missing `limit` (old frontend,
        bare API calls, curl/tests) gets a server-side default cap
        (settings.viz_default_limit), and level='concept' is treated like
        'object' (the persisted folded viz graph is object-level only — the
        frontend always sends level=object, but we still defend the API for
        level=concept / no-level callers that would otherwise slip through)."""
        with self._connect() as db:
            nb_count = self.knowledge.active_object_count(db, notebook_id)
        if int(nb_count) > self.settings.viz_sync_build_max_objects:
            effective_limit = limit if limit is not None else self.settings.viz_default_limit
            idx = self.scale_artifacts.viz_index(notebook_id)
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
            idx = self.scale_artifacts.viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(notebook_id, idx, limit)
            if idx is None and notebook_id in self.scale_artifacts.viz_building:
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
        cached = self.unified_cache.get((notebook_id, level))
        if cached is not None:
            return cached
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = self.knowledge.unified_graph_rows(db, notebook_id)
        nodes = [{"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}")} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"], "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        g = derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))
        if level == "concept":
            cids = {n["id"] for n in g["nodes"] if n["object_type"] == "concept"}
            g = {"nodes": [n for n in g["nodes"] if n["object_type"] == "concept"],
                 "edges": [e for e in g["edges"] if e["source_object_id"] in cids and e["target_object_id"] in cids]}
        self.unified_cache[(notebook_id, level)] = g
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
        idx = self.scale_artifacts.viz_index(notebook_id)
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
        with self._connect() as db:
            rows = self.knowledge.neighbor_relation_rows(db, notebook_id, raw_ids)
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
        with self._connect() as db:
            rows = self.knowledge.object_meta_rows_for_notebook(
                db, notebook_id, rep_ids
            )
        by_raw = {r["id"]: (r["object_type"], json.loads(r["payload"] or "{}").get("name", ""))
                  for r in rows}
        return {fid: by_raw.get(rep[fid], ("", "")) for fid in folded_ids}

    # ------------------------------------------------------------------
    # Streamed unified rebuild
    # ------------------------------------------------------------------

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
            seq, obj_c, dec_c, emb_c = self.unified_kg.cluster_input_facts(
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
            self.unified_kg.clear_scratch_run(db, notebook_id, run_id)

        # Pass A1: stream names once to build the acronym alias map.
        def _name_gen():
            with self._connect() as db:
                cur = self.unified_kg.seed_payload_rows(db, notebook_id, object_type)
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
            cur = self.unified_kg.stream_seed_rows(rdb, notebook_id, object_type)
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
                        self.unified_kg.insert_scratch_rows(wdb, buf)
                        buf.clear()
                if buf:
                    self.unified_kg.insert_scratch_rows(wdb, buf)
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
            cur = self.unified_kg.scratch_vector_rows(db, notebook_id, run_id)
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
        now = self._now()
        desc_by_cid = desc_by_cid or {}
        desc_sig_by_cid = desc_sig_by_cid or {}
        with self._connect() as rdb:
            cur = self.unified_kg.stream_scratch_rows(rdb, notebook_id, run_id)
            with self._write() as wdb:
                def _rows():
                    for r in cur:
                        cid = seed_to_canonical.get(r["seed"])
                        if cid is None:
                            continue
                        yield (
                            self._new_id("cc"), notebook_id, cid, r["object_id"],
                            canonical_names.get(cid, ""), object_type,
                            desc_by_cid.get(cid, ""), desc_sig_by_cid.get(cid, ""), now,
                        )

                self.unified_kg.replace_cluster_rows_streamed(
                    wdb, notebook_id, object_type, _rows()
                )
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
                row = self.unified_kg.state_row(db, notebook_id)
                cc = self.unified_kg.concept_clusters_count(db, notebook_id)
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
                self.unified_kg.checkpoint_clear(notebook_id)
            else:
                self.unified_kg.checkpoint_gc(notebook_id, _ck_ver)
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
                cached = self.unified_kg.checkpoint_load(
                    notebook_id, _ck_ver, "merge_review")
                todo = [c for c in cand_dicts if id_to_key[c["id"]] not in cached]

                def _persist(chunk_decisions):
                    rows = [(id_to_key[d["candidate_id"]],
                             {"decision": d["decision"], "confidence": d["confidence"],
                              "canonical_name": d.get("canonical_name", "")})
                            for d in chunk_decisions if d.get("candidate_id") in id_to_key]
                    if rows:
                        self.unified_kg.checkpoint_put(
                            notebook_id, _ck_ver, "merge_review", rows, self._now())

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
                for r in self.unified_kg.cluster_description_rows(db, notebook_id):
                    old_desc[r["canonical_id"]] = (r["canonical_description"] or "", r["canonical_desc_sig"] or "")
            # 同 input_version 的 checkpoint(写簇前被杀留下的已完成描述)作第一优先复用源。
            try:
                desc_ckpt = self.unified_kg.checkpoint_load(
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
                with self._connect() as db:
                    erows = self.unified_kg.cluster_evidence_rows(
                        db, notebook_id, run_id, cseeds
                    )
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
                                    self.unified_kg.checkpoint_put(
                                        notebook_id, _ck_ver, "concept_desc", _ck_buf, self._now())
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
                            self.unified_kg.checkpoint_put(
                                notebook_id, _ck_ver, "concept_desc", _ck_buf, self._now())
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
        now = self._now()
        _t = _time.perf_counter()
        with self._write() as db:
            self.governance_store.delete_pending_merges(db, notebook_id)
            self.governance_store.insert_pending_merge_rows(
                db,
                [(self._new_id("mc"), notebook_id, ca, cb, sa, sb, score, now, now)
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
            self.unified_kg.finish_rebuild_state(
                db, notebook_id, _ver, cluster_count, now
            )
        # Final cleanup: drop only THIS run's scratch rows (run_id-scoped so a
        # concurrent rebuild with a different run_id is unaffected).
        with self._write() as db:
            self.unified_kg.clear_scratch_run(db, notebook_id, run_id)
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
            self.scale_artifacts.build_viz(notebook_id)
        except Exception:
            self.event_log.logger.warning(
                "build_viz_index failed after rebuild for %s", notebook_id, exc_info=True)
        # rebuild 后检索索引必然 stale(clusters/objects 已变)—— 大库自动重建/入队。
        # maybe_auto_index 自身 fail-open,这里再包一层只是双保险。
        try:
            self.scale_artifacts.maybe_auto_index(notebook_id)
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

    # ------------------------------------------------------------------
    # Derived layers: canonical relations / mention bridge / communities
    # ------------------------------------------------------------------

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
            st = self.unified_kg.state_row(db, notebook_id)
            cnt = self.unified_kg.canonical_relations_count(db, notebook_id)
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["canonical_rel_seq"] == seq and cnt > 0):
            return int(cnt)
        agg: Dict[tuple, dict] = {}
        with self._connect() as db:
            cur = self.unified_kg.canonical_relation_seed_rows(db, notebook_id)
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
        now = self._now()
        rows = [(notebook_id, s, et, t, ent["n"], max(1, len(ent["docs"])),
                 json.dumps(ent["samples"]), now)
                for (s, et, t), ent in agg.items()]
        with self._write() as db:
            self.unified_kg.replace_canonical_relations(
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
                self.unified_kg.clear_mention_bridge(db, notebook_id)
            return 0
        # seq 闸(照抄 rebuild_canonical_relations,列名换 mention_seq)。
        with self._connect() as db:
            st = self.unified_kg.state_row(db, notebook_id)
            cnt = self.unified_kg.mention_edges_count(db, notebook_id)
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
            cur, claim_rows = self.unified_kg.mention_seed_rows(db, notebook_id)
            for r in cur:
                ent = clusters.setdefault(r["cid"], {"name": r["cname"], "srcs": set()})
                if r["src"]:
                    ent["srcs"].add(r["src"])
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
                self.unified_kg.claim_name_rows(
                    scan_db,
                    [(i, folded) for i, (_cid, folded) in enumerate(claims, 1)],
                )
                # 4) 每别名 phrase MATCH 召回 → boundary_hit 校验 → DF 双门。
                for alias in sorted(alias_to_canons):
                    if len(alias) < 3:     # trigram 最短查询=3;别名门已保证,双保险
                        continue
                    canons = alias_to_canons[alias]
                    match_expr = '"' + alias.replace('"', '""') + '"'
                    hits = []
                    for row in self.unified_kg.mention_scan_matches(scan_db, match_expr):
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
            self.unified_kg.replace_mention_bridge(
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
            _st = self.unified_kg.state_row(_db, notebook_id)
            _cnt = self.unified_kg.communities_count(_db, notebook_id, level)
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
                and self.scale_artifacts.load(notebook_id, allow_stale=True) is None):
            self.event_log.emit({"kind": "community_build_refused",
                                 "notebook_id": notebook_id, "reason": "no_scale_index"})
            return 0
        # canonical 整数边图:SQL-join 把关系两端映射到 canonical(未聚类→自身 object_id),
        # 整数索引累加边权。避开 networkx dict-of-dicts 与全量 cluster_map dict → 10^7 边
        # 内存有界(concept_clusters.member_object_id 有索引,join 走索引)。
        can2idx: "Dict[str, int]" = {}
        ew: "Dict[tuple, int]" = {}
        with self._connect() as db:
            names, graph_rows = self.unified_kg.community_graph_rows(db, notebook_id)
            for r in graph_rows:
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
        now = self._now()
        min_size = self.settings.community_min_size
        # Policy (min-size filter + id minting + member ordering) stays here;
        # the store owns the two-table full rewrite.
        kept_rows = [(self._new_id("cm"), sorted(comm))
                     for comm in comms if len(comm) >= min_size]
        kept = len(kept_rows)
        with self._write() as db:
            self.unified_kg.replace_communities(
                db, notebook_id, level, kept_rows, names, deg, now
            )
        # 记版本:社区已按 _seq 建好(无 unified_kg_state 行则 UPDATE no-op,下次仍重建)。
        with self._write() as db:
            self.unified_kg.set_community_seq(db, notebook_id, _seq)
        self.event_log.emit({"kind": "communities_rebuilt", "notebook_id": notebook_id,
                             "level": level, "communities": kept, "nodes": n_nodes})
        return kept

    def list_communities(self, notebook_id: str, level: int = 0) -> List[List[str]]:
        """Member-id lists of each detected community (for summaries / global search)."""
        with self._connect() as db:
            return self.unified_kg.community_member_ids(db, notebook_id, level)

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
            crows = self.unified_kg.community_rows_for_summary(
                db, notebook_id, level)
        done = 0
        for cr in crows:
            members = json.loads(cr["member_ids"] or "[]")
            if not members:
                continue
            with self._connect() as db:
                orows, rrows = self.knowledge.community_context_rows(
                    db, notebook_id, members
                )
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
                self.unified_kg.set_community_summary(
                    db, cr["id"], title, summary, json.dumps(findings))
            done += 1
        return done

    def get_community_reports(self, notebook_id: str, level: int = 0) -> List[dict]:
        """Persisted community reports (only those summarized). For global search."""
        with self._connect() as db:
            return self.unified_kg.community_reports(db, notebook_id, level)
