"""KG mutation side-effect coordinator (Task 14).

Owns the post-commit side effects every KG write funnels through: unified-
cache invalidation and the kg_mutation_seq dirty bump, plus the in-transaction
cluster-sequence bump. The facade's `_invalidate_unified_cache` /
`_mark_unified_kg_dirty` / `_bump_cluster_mutation_seq` compatibility wrappers
delegate here — call sites keep calling the wrappers, so the frozen
per-operation phase matrix (tests/fixtures/repository_contract/
mutation_phases.json, replayed by test_kg_mutation_phase_matrix) is unchanged:

    store_kg               chunks; embeds; invalidate; dirty
    relink_notebook_kg     the dirty bump rides EACH SOURCE's own write
                           transaction (mark_unified_kg_dirty_in_tx, committed
                           atomically with that source's edge insert) — a
                           `finally`-keyed bump cannot survive a kill -9 landing
                           between a source's commit and the run's finally, and
                           those already-committed edges must not escape
                           kg_mutation_seq (the `_cluster_input_version`
                           fallback COUNT does not count relations, so a missed
                           bump makes 「刷新图谱」 short-circuit for that
                           notebook forever). invalidate stays a single
                           run-level `finally` call, keyed on "any edge has
                           been committed so far" (cheap in-process cache
                           eviction — safe to defer/dedupe across sources).
                           No-op anywhere when zero edges were written.
    set_edge_review        relation UPDATE + dirty bump + same-tx seq readback
                           in ONE transaction (mark_unified_kg_dirty_in_tx,
                           R2 P2 fix, codex #638 R2 — see review_queue_memo's
                           module docstring for the two races this closes);
                           invalidate stays a separate post-commit call
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
- The unified/vector caches are the runtime-owned RetrievalSnapshotCache's
  objects (Task 17), read through by identity — never replacement copies: the
  ``unified_cache``/``vector_cache`` properties alias the SAME objects the
  facade's write-through descriptors expose, so a facade-level cache swap is
  seen here immediately. The remaining state collaborators (auto-index
  once-set, corpus-language memo) stay the facade's EXISTING objects, held by
  identity.
"""
from __future__ import annotations

from typing import Any, Callable, ContextManager, List, MutableMapping, Set

from app.repositories.ports import UnifiedKgStorePort
from app.services.retrieval_snapshot_cache import RetrievalSnapshotCache
from app.services.vector_cache import VectorCache


class KgMutationCoordinator:
    def __init__(
        self,
        unified_store: UnifiedKgStorePort,
        snapshots: RetrievalSnapshotCache,
        auto_index_checked: Set[str],
        notebook_languages: MutableMapping[str, List[str]],
        *,
        write: Callable[[], ContextManager[Any]],
        now: Callable[[], str],
    ) -> None:
        self.unified_store = unified_store
        self.snapshots = snapshots
        self.auto_index_checked = auto_index_checked
        self.notebook_languages = notebook_languages
        self._write = write
        self._now = now

    @property
    def unified_cache(self) -> MutableMapping[tuple, object]:
        return self.snapshots.unified_cache

    @property
    def vector_cache(self) -> VectorCache:
        return self.snapshots.vector_cache

    def invalidate_unified_cache(self, notebook_id: str) -> None:
        # Task 17: the key-family eviction body lives on the runtime-owned
        # RetrievalSnapshotCache (one owner). This hook stays the mutation
        # funnel the frozen phase matrix observes — same effects, same timing.
        self.snapshots.invalidate_kg(notebook_id)

    def mark_unified_kg_dirty(self, notebook_id: str) -> None:
        # Bump the monotonic mutation counter on every KG write. This is the ONLY
        # place kg_mutation_seq advances, and every mutation funnels through here,
        # so _cluster_input_version sees a deterministic change on any edit —
        # including same-second in-place edits (rename/decision-flip/re-embed) that
        # a timestamp MAX at 1s resolution would miss. The upsert (store-owned
        # since Task 13) references the table's own current value (+1), NOT
        # excluded, so an existing row increments rather than resets to the
        # inserted literal (1). First mutation -> seq 1.
        with self._write() as db:
            self.mark_unified_kg_dirty_in_tx(db, notebook_id)

    def mark_unified_kg_dirty_in_tx(self, connection: Any, notebook_id: str) -> int:
        """Same effects as ``mark_unified_kg_dirty``, but riding a write
        transaction the CALLER already holds open, rather than opening its
        own.  Exists for two reasons now:

        1. ``relink_notebook_kg`` commits per source, and a `finally`-keyed
           dirty bump cannot survive a kill -9 landing between a source's
           commit and the run's finally — the seq advance has to be atomic
           with the edge insert it accompanies, or a killed process can leave
           real edges in the database that never escaped kg_mutation_seq (see
           the coordinator's module docstring for why that permanently
           short-circuits 「刷新图谱」 for that notebook).
        2. ``set_edge_review`` (R2 P2 fix, codex #638 R2) needs the bump AND
           the freshly-bumped seq value ATOMIC with its own review_status
           UPDATE — see ``review_queue_memo``'s module docstring for the two
           races a separate post-commit bump+read used to open.

        Returns the freshly-bumped ``kg_mutation_seq``, read back on the SAME
        connection (so it observes this call's own uncommitted write —
        ordinary read-your-writes within one transaction, true for both
        backends) rather than a new post-commit connection that could race
        another writer's own bump. Existing callers that only cared about the
        side effect (``relink_notebook_kg``) simply ignore the return value —
        this is a backward-compatible signature widening, not a behavior
        change for them."""
        now = self._now()
        self.unified_store.mark_dirty(connection, notebook_id, now)
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
        return int(self.unified_store.graph_seq_row(connection, notebook_id)[0])

    def bump_cluster_mutation_seq(
        self, connection: object, notebook_id: str
    ) -> None:
        """concept_clusters 写路径的单调计数器 bump。与 mark_unified_kg_dirty 不同,
        本原语在调用方已持有的写事务 connection 内执行(写簇+bump 同 commit,原子——
        不存在"簇写了、seq 没 bump"的窗口)。kg_mutation_seq 不在此处动:rebuild
        刻意保持它稳定(幂等,见 _cluster_input_version),clusters 的变化信号独立成列。"""
        self.unified_store.bump_cluster_seq(connection, notebook_id, self._now())
