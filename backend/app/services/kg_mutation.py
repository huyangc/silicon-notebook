"""KG mutation side-effect coordinator (Task 14).

Owns the post-commit side effects every KG write funnels through: unified-
cache invalidation and the kg_mutation_seq dirty bump, plus the in-transaction
cluster-sequence bump. The facade's `_invalidate_unified_cache` /
`_mark_unified_kg_dirty` / `_bump_cluster_mutation_seq` compatibility wrappers
delegate here — call sites keep calling the wrappers, so the frozen
per-operation phase matrix (tests/fixtures/repository_contract/
mutation_phases.json, replayed by test_kg_mutation_phase_matrix) is unchanged:

    store_kg               chunks; embeds; invalidate; dirty
    relink_notebook_kg     transaction; invalidate; dirty (no-op when 0 added)
    set_edge_review        transaction; dirty; invalidate
    write_clusters         replace + cluster-seq bump in ONE transaction; invalidate
    append_clusters        append + bump in one transaction; invalidate when added
    confirm/reject merge   transaction; invalidate; dirty
    review_pending_merges  transaction; dirty then invalidate when decisions exist
    approve_promotion      transaction; best-effort embed; invalidate; dirty
    update_knowledge       transaction; best-effort embed; invalidate; dirty
    merge_knowledge        transaction; dirty; invalidate
    conflict discard/modify  ... plus a second dirty bump
    confirm_conflict       mutation commits before the candidate-status transaction
    unified rebuild        cluster rewrite + cluster seq; NO kg_mutation_seq bump
    deep copy / migration / fixture writes   never call this coordinator

Red lines:
- ``mark_unified_kg_dirty`` stays the ONLY online entry that advances
  kg_mutation_seq (single choke point — see the update_knowledge/re-embed
  bypass lesson). Do not add a second dirty entry.
- The dirty bump's write transaction rides the FACADE's ``_write``
  compatibility seat (injected ``write`` callable, resolved per call), so the
  frozen begin/commit phase traces and failure injections keep observing it.
- All cache/state collaborators are the facade's EXISTING objects, held by
  identity — never replacement copies (Task 17 transfers ownership later).
"""
from __future__ import annotations

import sqlite3
from typing import Callable, ContextManager, List, MutableMapping, Set

from app.repositories.sqlite.unified_kg_store import UnifiedKgStore
from app.services.vector_cache import VectorCache


class KgMutationCoordinator:
    def __init__(
        self,
        unified_store: UnifiedKgStore,
        unified_cache: MutableMapping[tuple, object],
        vector_cache: VectorCache,
        auto_index_checked: Set[str],
        notebook_languages: MutableMapping[str, List[str]],
        *,
        write: Callable[[], ContextManager[sqlite3.Connection]],
        now: Callable[[], str],
    ) -> None:
        self.unified_store = unified_store
        self.unified_cache = unified_cache
        self.vector_cache = vector_cache
        self.auto_index_checked = auto_index_checked
        self.notebook_languages = notebook_languages
        self._write = write
        self._now = now

    def invalidate_unified_cache(self, notebook_id: str) -> None:
        for key in [k for k in self.unified_cache if k[0] == notebook_id]:
            self.unified_cache.pop(key, None)
        # Matrices are stored under "{nb}:matrix:{table}" (see _vector_matrix). The old
        # "{nb}:knowledge" key never matched (dead no-op). Invalidate BOTH embedding
        # tables so an in-place re-embed (same row count + same-second created_at, i.e.
        # an unchanged version tuple) cannot serve a stale vector.
        for table in ("knowledge_embeddings", "element_embeddings"):
            self.vector_cache.invalidate(f"{notebook_id}:matrix:{table}")
        self.vector_cache.invalidate(f"{notebook_id}:kwtok")
        # Federated graph caches are keyed "{active_id}:fed_rxgraph" — the ACTIVE
        # (personal) notebook's id, NOT this notebook's. A change in THIS notebook
        # (e.g. a base notebook) may affect any federated graph that includes it,
        # so evict every fed_rxgraph entry; tracking participants per key is
        # overkill for the POC. This explicit eviction also guards against
        # same-second in-place edits that leave the version tuple unchanged.
        for key in [k for k in self.vector_cache._store if k.endswith(":fed_rxgraph")]:
            self.vector_cache.invalidate(key)
        # PPR graph (concept_clusters + knowledge_objects + chunks → HippoRAG graph) —
        # evict so a same-second KG edit with an unchanged version tuple cannot serve stale.
        self.vector_cache.invalidate(f"{notebook_id}:ppr_graph")
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
        # size/copyable verdict to the ask-path guards / share paths.
        self.vector_cache.invalidate(f"{notebook_id}:copystats")

    def mark_unified_kg_dirty(self, notebook_id: str) -> None:
        # Bump the monotonic mutation counter on every KG write. This is the ONLY
        # place kg_mutation_seq advances, and every mutation funnels through here,
        # so _cluster_input_version sees a deterministic change on any edit —
        # including same-second in-place edits (rename/decision-flip/re-embed) that
        # a timestamp MAX at 1s resolution would miss. The upsert (store-owned
        # since Task 13) references the table's own current value (+1), NOT
        # excluded, so an existing row increments rather than resets to the
        # inserted literal (1). First mutation -> seq 1.
        now = self._now()
        with self._write() as db:
            self.unified_store.mark_dirty(db, notebook_id, now)
        # Re-arm maybe_auto_index's once-set: the index this nb was previously
        # judged against (fresh/absent) is now stale by construction (KG just
        # changed), so the next write-path or read-path fallback call should
        # re-evaluate rather than trust a stale "checked" verdict.
        self.auto_index_checked.discard(notebook_id)
        # Content just changed → the memoized corpus-language hint may be stale
        # (a new source could add a 2nd language). Drop it; next _notebook_langs
        # re-samples. This is the single mutation funnel, so it covers chunk adds,
        # re-chunk, re-embed and KG edits — the cheapest correct invalidation site.
        self.notebook_languages.pop(notebook_id, None)

    def bump_cluster_mutation_seq(
        self, connection: sqlite3.Connection, notebook_id: str
    ) -> None:
        """concept_clusters 写路径的单调计数器 bump。与 mark_unified_kg_dirty 不同,
        本原语在调用方已持有的写事务 connection 内执行(写簇+bump 同 commit,原子——
        不存在"簇写了、seq 没 bump"的窗口)。kg_mutation_seq 不在此处动:rebuild
        刻意保持它稳定(幂等,见 _cluster_input_version),clusters 的变化信号独立成列。"""
        self.unified_store.bump_cluster_seq(connection, notebook_id, self._now())
