"""Runtime-owned retrieval snapshot caches (Task 17).

Owns the two per-process cache objects the facade constructor used to build
inline and hand around by identity:

- ``vector_cache`` — the version-keyed :class:`VectorCache` (single-flight +
  LRU) behind the ``{nb}:matrix:*`` embedding matrices, ``{nb}:kwtok``
  keyword tokens, ``{active}:fed_rxgraph`` federated graphs,
  ``{nb}:ppr_graph`` HippoRAG graph, ``{nb}:entchunk`` / ``{nb}:elemchunk``
  reverse maps, ``{nb}:knowhow_types`` source-scoped type tuple,
  ``{nb}:knowhow_bridge`` cell-vector sidecar,
  ``{nb}:edge_centrality`` betweenness map, ``{nb}:clustermap``
  membership map and ``{nb}:edge_support``
  annotation map. Version keys (table row counts / MAX created_at /
  kg_mutation_seq) stay computed at the call sites; LRU + single-flight stay
  VectorCache-owned.
- ``unified_cache`` — the ``(notebook_id, level)``-keyed unified-graph dict.

The facade's ``_vector_cache`` / ``_unified_cache`` handles are write-through
descriptors over THESE objects and the KgMutationCoordinator reads them
through this cache — one owner, no facade-only copies.

``invalidate_kg`` is the KG key-family eviction every online KG mutation
funnels through (``KgMutationCoordinator.invalidate_unified_cache`` delegates
here; the frozen per-operation phase matrix in mutation_phases.json is
untouched). It evicts exactly the frozen families — the embedding matrices
(all four embedding tables), kwtok, EVERY fed_rxgraph entry, ppr_graph, entchunk,
elemchunk, knowhow_types, knowhow_bridge, edge_centrality and clustermap —
plus this notebook's unified-cache entries, plus the copy-stats memo that now
lives in ``notebook_scale`` (R2-2 moved it out of this cache; the eviction is
the SAME frozen family, only its storage changed — see that module's
docstring).
``{nb}:edge_support`` deliberately stays out: it is versioned by
canonical_rel_seq and self-invalidates on table rewrites.
"""
from __future__ import annotations

from typing import Callable, Hashable, MutableMapping

from app.services.notebook_scale import CopyStatsMemo
from app.services.vector_cache import VectorCache


class RetrievalSnapshotCache:
    def __init__(
        self,
        vector_cache: VectorCache,
        unified_cache: MutableMapping[tuple, object],
        copy_stats_memo: CopyStatsMemo,
    ) -> None:
        self.vector_cache = vector_cache
        self.unified_cache = unified_cache
        # copy-stats memo:R2-2 把它从 vector_cache 的 ``{nb}:copystats`` 键族搬
        # 进自己的存储,codex PR#634 R2 P2-2 又把那份存储从模块级全局收成
        # runtime-owned 对象。所有者仍是这里 —— 这个类本来就是「冻结键族及其
        # 失效」的所有者,而 copy-stats 曾经就是它的一个键族。
        self.copy_stats_memo = copy_stats_memo

    def get(
        self,
        key: str,
        version: Hashable,
        loader: Callable[[], object],
    ) -> object:
        return self.vector_cache.get(key, version, loader)

    def peek(self, key: str, version: Hashable) -> bool:
        return self.vector_cache.peek(key, version)

    def invalidate(self, key: str) -> None:
        self.vector_cache.invalidate(key)

    def invalidate_unified(self, notebook_id: str) -> None:
        """Drop only this notebook's unified-graph dict entries (the
        ``(notebook_id, level)`` keys) — WITHOUT the vector-cache family sweep
        invalidate_kg does. Used by the scale-index build to release the whole
        full_viz_graph('object') dict (~12-20GB at 8M objects) once viz_arrays
        has extracted the compact arrays, so it never rides resident through
        persist."""
        for key in [k for k in self.unified_cache if k[0] == notebook_id]:
            self.unified_cache.pop(key, None)

    def invalidate_kg(self, notebook_id: str) -> None:
        for key in [k for k in self.unified_cache if k[0] == notebook_id]:
            self.unified_cache.pop(key, None)
        # Matrices are stored under "{nb}:matrix:{table}" (see _vector_matrix). The old
        # "{nb}:knowledge" key never matched (dead no-op). Invalidate all four
        # embedding tables so an in-place re-embed (same row count + same-second
        # created_at, i.e. an unchanged version tuple) cannot serve stale vectors.
        for table in (
            "knowledge_embeddings",
            "element_embeddings",
            "relation_embeddings",
            "chunk_embeddings",
        ):
            self.vector_cache.invalidate(f"{notebook_id}:matrix:{table}")
        self.vector_cache.invalidate(f"{notebook_id}:kwtok")
        self.vector_cache.invalidate(f"{notebook_id}:knowhow_types")
        # Knowhow hidden-chunk matrix + chunk→cell-KO reverse map. It is also
        # versioned by graph_seq_row, but explicit eviction covers a same-seq
        # delete/reingest collision and frees stale matrices promptly.
        self.vector_cache.invalidate(f"{notebook_id}:knowhow_bridge")
        # Federated graph caches are keyed "{active_id}:fed_rxgraph" — the ACTIVE
        # (personal) notebook's id, NOT this notebook's. A change in THIS notebook
        # (e.g. a base notebook) may affect any federated graph that includes it,
        # so evict every fed_rxgraph entry; tracking participants per key is
        # overkill for the POC. This explicit eviction also guards against
        # same-second in-place edits that leave the version tuple unchanged.
        for key in [k for k in self.vector_cache.keys() if k.endswith(":fed_rxgraph")]:
            self.vector_cache.invalidate(key)
        # PPR graph (concept_clusters + knowledge_objects + chunks → HippoRAG graph).
        # Like fed_rxgraph, a PPR graph is keyed on the ACTIVE nb but includes base
        # participant(s), so a change/delete in THIS (possibly base) nb must evict
        # every dependent :ppr_graph, not just this notebook's own. This also used
        # to be the belt-and-braces for the seq-reset case: the graph version key
        # is the (kg/cluster/mention, kg_reset_epoch) quadruple (batch-3-W1 PR-2
        # appended kg_reset_epoch — see graph_seq_row's docstring), which used to
        # RESET to (0,0,-1) with no fourth element when delete_notebook_kg dropped
        # the state row and re-climbed from 0 on re-ingest — a delete+reingest of a
        # base participant could collide on an identical triple with different
        # content. PR-2 closes that structurally (the epoch element never repeats),
        # so this loop's role for THAT specific hazard is now redundant; it still
        # evicts every :ppr_graph entry for the SAME-SECOND-in-place-edit reason
        # every other key family in this method exists for (an edit whose version
        # tuple happens not to change within one second's resolution) — kept
        # unconditional rather than narrowed to "only if key not found", because
        # that would be the one key family in this method special-cased away from
        # its own stated purpose.
        for key in [k for k in self.vector_cache.keys() if k.endswith(":ppr_graph")]:
            self.vector_cache.invalidate(key)
        # entity->chunk / element->chunk reverse maps (P0-5) — evict so a same-second
        # in-place evidence/element_ids edit with an unchanged version tuple cannot
        # serve a stale membership map to the PPR-fallback / chunk-overlay paths.
        self.vector_cache.invalidate(f"{notebook_id}:entchunk")
        self.vector_cache.invalidate(f"{notebook_id}:elemchunk")
        # review_queue's edge betweenness centrality map (P0-3) — evict so a
        # same-second in-place edit (e.g. review_status flip) with an unchanged
        # version tuple cannot serve a stale centrality map.
        self.vector_cache.invalidate(f"{notebook_id}:edge_centrality")
        # cluster_map (member_object_id -> canonical_id, P1-2) — evict so a
        # same-second concept_clusters rewrite (rename / rebuild's DELETE+INSERT,
        # which can land COUNT and MAX(created_at) on the same values as before)
        # with an unchanged version tuple cannot serve a stale membership map.
        self.vector_cache.invalidate(f"{notebook_id}:clustermap")
        # notebook_copy_stats memo (perf-audit A3) — evict so a same-second
        # in-place edit with an unchanged version tuple cannot serve a stale
        # size/copyable verdict to the ask-path guards / share paths. R2-2 moved
        # this memo out of the shared VectorCache (it was being evicted by
        # unrelated key families, forcing a five-aggregate cold reload on every
        # ask); the eviction contract here is unchanged — same family, same
        # trigger, one call away.
        self.copy_stats_memo.invalidate(notebook_id)
