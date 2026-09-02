"""KG mutation side-effect coordinator (Task 14).

Owns the post-commit side effects every KG write funnels through: unified-
cache invalidation and the kg_mutation_seq dirty bump, plus the in-transaction
cluster-sequence bump. The facade's `_invalidate_unified_cache` /
`_mark_unified_kg_dirty` / `_bump_cluster_mutation_seq` compatibility wrappers
delegate here — call sites keep calling the wrappers, so the frozen
per-operation phase matrix (tests/fixtures/repository_contract/
mutation_phases.json, replayed by test_kg_mutation_phase_matrix) is unchanged:

    store_kg               object/relation chunks + dirty bump in ONE
                           transaction (mark_unified_kg_dirty_in_tx, codex #638
                           R5 P1 — every chunk already shared one transaction,
                           so this is both the last graph write's transaction
                           and the only one); embeds and invalidate follow it,
                           post-commit. Previously the bump trailed the
                           embeddings, which left the rows committed and the
                           seq unmoved for the whole (minutes-long) embedding
                           pass, and skipped the bump outright when a
                           non-replacement embed raised. See the FULL CENSUS
                           below for why this was the third instance of one
                           bug, not a third unrelated bug.
    complete_relations_for_source
                           paged loop, ONE TRANSACTION PER PAGE: each page's
                           relation insert carries its OWN dirty bump (codex
                           #638 R5), keyed on "this page inserted rows" —
                           the run-level bump it replaces was stale for every
                           page but the last and lost entirely when a later
                           page raised. Same per-partition shape as
                           relink_notebook_kg. invalidate stays a run tail.
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
    begin_extraction_run   when preserve_existing=False, the source-graph clear
                           (clear_source_graph_state — knowledge_relations/
                           knowledge_objects for that source) + dirty bump ride
                           ONE transaction (mark_unified_kg_dirty_in_tx, codex
                           #638 R4 P2), opened by the FACADE (`_write`) rather
                           than the store's self-contained `begin_extraction` —
                           stores don't import this coordinator. A clear with
                           no matching bump left review_queue_memo's seq gate
                           unable to see the delete (the fallback-to-store
                           invalidate call is process-local and cannot reach a
                           sibling process's warm memo); see review_queue_memo's
                           module docstring, gap 3. No bump when
                           preserve_existing=True (nothing was cleared).
    delete_source          the source teardown transaction (graph rows via
                           clear_source_extraction_state → clear_source_graph_
                           state, then the source row) carries the dirty bump
                           (codex #638 R5). It used to sit past the commit
                           behind two filesystem teardowns that can raise.
                           UNCONDITIONAL inside the transaction, matching the
                           post-commit call one-for-one (the existence guard
                           never gated it). invalidate stays post-commit.
    knowhow projection     ``KnowhowProjector`` holds the IN-TX seat ONLY
                           (``mark_unified_dirty_in_tx``; there is deliberately
                           no non-tx seat on it). ``_project_table_locked``
                           bumps inside its delete-and-reinsert publication
                           transaction, ``delete_table_projection`` inside its
                           teardown transaction (codex #638 R5). Both counts
                           are unchanged — the publication branch is the only
                           one that used to reach the post-commit call.
    write_clusters         replace + cluster-seq bump in ONE transaction; invalidate
    append_clusters        append + bump in one transaction; invalidate when added
    confirm/reject merge   transaction; invalidate; dirty
    review_pending_merges  transaction; dirty then invalidate when decisions exist
    approve_promotion      base-object/provenance transaction + dirty bump in
                           ONE transaction (codex #638 R5, placed past both
                           idempotent early returns so an already-approved
                           candidate still bumps nothing); best-effort embed
                           and invalidate follow. The embed here is NOT
                           wrapped in try/except, so the ex-post bump used to
                           be skipped outright on an embedder outage.
    update_knowledge       object transaction + dirty bump in ONE transaction
                           (codex #638 R5); best-effort embed — when the
                           payload was edited, this REPLACES the object's
                           existing knowledge_embeddings row, so it carries
                           its OWN second dirty bump inside the vector
                           write's own transaction (codex #638 R6 P1 — see
                           the VECTOR-REPLACE CENSUS below); invalidate
    merge_knowledge        transaction + dirty bump in ONE transaction
                           (codex #638 R5); invalidate
    conflict discard/modify  ... plus a second dirty bump (post-tx,
                           belt-and-suspenders: the graph write and its own
                           atomic bump already happened inside the nested
                           set_edge_review / update_knowledge call)
    confirm_conflict       mutation commits before the candidate-status transaction
    unified rebuild        cluster rewrite + cluster seq; NO kg_mutation_seq bump
    deep copy / migration / fixture writes   never call this coordinator

FULL CENSUS — "every bump is atomic with the data it announces"
---------------------------------------------------------------
codex #638 R2 (set_edge_review), R4 (begin_extraction_run) and R5 (store_kg)
were three sightings of ONE defect: a post-commit bump leaves a window where
graph rows are durable and ``kg_mutation_seq`` still describes the world
before them, and any exception in that window drops the bump for good. R5
therefore stopped patching sightings and swept the matrix. Rule established
(re-adjudicated by batch-3-W1 PR-2, design doc Sec 3.3 option C — see the
``delete_notebook_kg`` census entry below for why "seq bump" alone stopped
being the whole story):

    every transaction that commits ``knowledge_objects`` /
    ``knowledge_relations`` / ``concept_clusters`` rows commits its seq bump
    with them.

PR-2's amendment: a transaction that commits those rows must advance its
VERSION IDENTITY atomically with them — ``kg_mutation_seq`` for every writer
that keeps the graph around, or ``kg_reset_epoch`` for the one writer that
empties it. Before PR-2 there was only one version-identity primitive
(``kg_mutation_seq``), so "commits its seq bump" and "advances its version
identity" were the same sentence; ``delete_notebook_kg`` is what forced them
apart — see its entry below for why bumping ``kg_mutation_seq`` itself
(rather than resetting it) was never the available move for that writer.

Graph-row writers, all now in-transaction (✓ = already was):

    store_kg                              R5      knowledge_lifecycle
    complete_relations_for_source         R5      knowledge_lifecycle (per page)
    _relink_one_source                    ✓ R1    knowledge_lifecycle
    _begin_extraction_run (clear)         ✓ R4    repository_facade
    set_edge_review                       ✓ R2    knowledge_governance
    update_knowledge                      R5      knowledge_governance
    merge_knowledge                       R5      knowledge_governance
    approve_promotion                     R5      knowledge_governance
    delete_source (teardown)              R5      source_ingestion
    knowhow _project_table_locked         R5      knowhow/projection
    knowhow delete_table_projection       R5      knowhow/projection
    write_clusters / append_clusters /
      _sweep_orphan_clusters_page_loop /
      _write_cluster_map_streamed         ✓       cluster-seq bump in-tx;
                                                  kg_mutation_seq deliberately
                                                  NOT bumped (rebuild
                                                  idempotency — see
                                                  bump_cluster_mutation_seq)
    publish_indexing_pipeline_success     ✓       store-owned: the graph rows
                                                  AND the unified_kg_state
                                                  upsert are one transaction
                                                  in kg_build_job_store. The
                                                  one place outside this
                                                  coordinator that advances
                                                  kg_mutation_seq; it is
                                                  grandfathered because it is
                                                  already atomic, and it is
                                                  not a licence for a second
                                                  online entry (see red lines).
    delete_notebook_graph_rows            PR-2    knowledge_store: deletes the
      (delete_notebook_kg's store call)            user-document knowledge_
                                                  objects/knowledge_relations
                                                  rows and blanket-clears
                                                  concept_clusters, in the SAME
                                                  transaction as the
                                                  unified_kg_state UPDATE that
                                                  resets kg_mutation_seq to 0
                                                  AND advances kg_reset_epoch
                                                  by 1. THE ONLY WRITER OF
                                                  kg_reset_epoch in the whole
                                                  codebase — no second online
                                                  entry, same discipline as
                                                  mark_unified_kg_dirty_in_tx's
                                                  single-choke-point red line
                                                  below.
    drain_notebook_graph_rows_page       T-5a     knowledge_store: ONE bounded
      (delete_notebook_kg's pre-reset              pre-reset page of the same
      drain, _drain_graph_rows_before_             tables/predicates the final
      reset)                                       pass clears. Its caller
                                                  bumps kg_mutation_seq
                                                  through mark_unified_kg_
                                                  dirty_in_tx — the ONE
                                                  online choke point below,
                                                  NOT a second dirty entry —
                                                  in the SAME write() as each
                                                  page, so two mid-drain
                                                  reads can never cache
                                                  different partial graphs
                                                  under one (epoch, seq) key,
                                                  and the choke point's memo
                                                  work (auto_index_checked
                                                  re-arm, corpus-language
                                                  drop) runs per batch. It
                                                  never touches
                                                  kg_reset_epoch; the final
                                                  pass's reset (seq→0,
                                                  epoch+1) still supersedes
                                                  every drain-era key.

VECTOR-REPLACE CENSUS — codex #638 R6 P1
-----------------------------------------
R5's rule above covers rows in ``knowledge_objects`` / ``knowledge_relations``
/ ``concept_clusters``. It says nothing about ``knowledge_embeddings`` (the
per-object payload vector table), because R5's own bump already accounted for
that table's usual case: ``_cluster_input_version``'s ``emb_c`` term is
``COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?``, and every call
site below except one only ever INSERTs a vector for a BRAND-NEW object_id —
the row count changes, emb_c already reflects it, no second bump is needed.

R6 found the one exception: ``update_knowledge``'s best-effort re-embed
REPLACES the vector of an object that already had one (same object_id,
INSERT OR REPLACE / ON CONFLICT DO UPDATE) — row count unchanged, emb_c
blind to it. The object's OWN row bump (R5, above) already advanced
kg_mutation_seq once for the payload edit, so a reader racing the gap
between that commit and the embed's own (a real embedder HTTP call sits in
between) sees the NEW seq paired with the OLD vector — and because the count
never moves again, nothing ever re-triggers a rebuild to fix it. The fix:
the replace's own transaction (``replace_knowledge_vectors``, both backends)
takes an optional ``mark_dirty_in_tx`` invoked on ITS OWN connection right
after the row commits — a SECOND call to the same single dirty entry
(``mark_unified_kg_dirty_in_tx``), not a new one, same shape as
``apply_conflict_resolution``'s belt-and-suspenders second bump below. Every
other call site passes nothing (``mark_dirty_in_tx=None``, the parameter's
default), so this is additive: none of them change behavior.

    store_kg (embed_objects_batch)        INSERT — objects carry freshly
                                           allocated ``ko-`` ids (store_kg
                                           always mints them, replace_source
                                           or not); emb_c moves with the row
                                           count. No bump added.
    complete_relations_for_source
      (embed_relations_batch)             INSERT — relations carry freshly
                                           allocated ``rel-`` ids per page;
                                           same shape as store_kg. No bump.
    update_knowledge (_embed_knowledge)   REPLACE of an existing object_id's
                                           vector on a payload edit. THE FIX:
                                           second bump inside the vector
                                           write's own transaction.
    merge_knowledge                       No embed call at all — deprecates
                                           the loser object in place and
                                           never touches its vector. N/A.
    approve_promotion (_embed_knowledge,
      both call sites)                    INSERT only: the memory-promotion
                                           branch embeds
                                           ``created_object_ids`` (freshly
                                           created, never the pre-existing
                                           ``merged_object_ids``); the direct
                                           branch embeds only when
                                           ``approval.created_new_object`` is
                                           True. A promotion that merges into
                                           an existing base object never
                                           re-embeds it. No bump added.
    delete_source (teardown)              Deletes ``knowledge_embeddings``
                                           rows (``clear_embeddings=True``)
                                           inside the SAME transaction as the
                                           graph-row delete + its R5 bump —
                                           never a separate replace step, so
                                           already atomic without help.
    knowhow _project_table_locked /
      delete_table_projection             Write ``chunk_embeddings``, not
                                           ``knowledge_embeddings`` — a
                                           different table emb_c never reads.
                                           Out of scope for this gate.
    backfill_knowledge_embeddings          Explicitly filters to object ids
      (ask()-triggered)                   MISSING a vector
                                           (``embedded_object_ids``) before
                                           embedding; rides the same
                                           INSERT-only ``embed_objects_batch``
                                           as store_kg. No bump added.

Deliberately NOT moved into a transaction, with the reason for each:

    delete_notebook_kg          batch-3-W1 PR-2 MOVED THIS ENTRY UP into the
                                in-transaction census table above (see
                                ``delete_notebook_graph_rows``). The old
                                reasoning here — "cannot bump: it DELETES the
                                unified_kg_state row, so the seq restarts from
                                0 and aliases" — is exactly the problem PR-2
                                closes: it no longer bumps kg_mutation_seq at
                                all (that column is RESET to 0, in-transaction,
                                same as a fresh notebook's birth row), and the
                                aliasing that used to force this into "keep
                                the explicit memo/count invalidation instead"
                                is now closed structurally by kg_reset_epoch —
                                a persistent counter, bumped in the SAME
                                transaction, that only increases and so never
                                repeats. kg_analysis's ``kg_mutation_seq == 0
                                means absent`` contract (review_queue_memo,
                                former gap 2) is UNCHANGED: the reset row is
                                byte-identical to a birth row on every column
                                that contract reads, kg_reset_epoch is not one
                                of them. See knowledge_lifecycle.delete_
                                notebook_kg's own docstring for the full
                                writeup of what replaced the three explicit
                                invalidation calls this entry used to license.
    RepositoryFacade.add_relations   Fixture/test-only bare insert; never
                                bumps at all, invalidates explicitly
                                (review_queue_memo, gap 1).
    apply_conflict_resolution's second bump   Writes no rows of its own: the
                                graph write and its atomic bump already
                                happened inside the nested set_edge_review /
                                update_knowledge call. Pure belt-and-braces
                                on a monotonic counter.
    _run_success_side_effects   Run-level tail of a KG build; the rows were
                                written (and bumped) by the store_kg calls
                                inside the build. No transaction of its own.
    source_ingestion's post-extraction bumps (process_source,
      _reextract_retyped, memory-source ingest)   Same shape: run-level
                                redundancy after run_extraction → store_kg,
                                which now bumps atomically. Deliberately
                                log-only so a failed bump can never flip an
                                actually-extracted source to 'failed'.
    confirm_merge / reject_merge / review_pending_merges   Write
                                ``concept_merge_candidates``, not graph rows.
                                The bump is a deliberate OVER-invalidation
                                (a confirmed decision changes future
                                clustering), so it announces no committed
                                graph row that a reader could see early.
    build_chunks (source_chunking)   Writes ``chunks``; not a graph row and
                                not a review-queue input.
    backfill_node_embeddings / reembed_kg   Write embedding tables; offline
                                CLI/batch. Not graph rows.

Red lines:
- ``mark_unified_kg_dirty`` / ``mark_unified_kg_dirty_in_tx`` stay the ONLY
  online entry that advances kg_mutation_seq (single choke point — see the
  update_knowledge/re-embed bypass lesson). Do not add a second dirty ENTRY.
  Calling the SAME entry a second time from a second commit (update_
  knowledge's R6 P1 vector-replace bump, apply_conflict_resolution's belt-
  and-suspenders bump) is not a second entry — it is fine, and the ONLY
  sanctioned way to advance the seq a second time within one logical
  operation.
- The dirty bump's write transaction rides the FACADE's ``_write``
  compatibility seat (injected ``write`` callable, resolved per call), so the
  frozen begin/commit phase traces and failure injections keep observing it
  — including when the bump is invoked from inside ``replace_knowledge_
  vectors``'s own transaction (embedding_store's ``write`` is bound to the
  SAME facade seat via ``RepositoryRuntime.wire_persistence``, not a private
  connection of its own).
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

    def mark_unified_kg_dirty_in_tx(
        self, connection: Any, notebook_id: str
    ) -> "tuple[int, int]":
        """Same effects as ``mark_unified_kg_dirty``, but riding a write
        transaction the CALLER already holds open, rather than opening its
        own.  Since codex #638 R5 this is the DEFAULT for anything that
        commits graph rows — see the module docstring's FULL CENSUS for the
        complete list and for the operations deliberately left outside it.
        The two reasons it was introduced remain the sharpest statements of
        why:

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

        Returns ``(kg_mutation_seq, kg_reset_epoch)``, both read back on the
        SAME connection (so they observe this call's own uncommitted write —
        ordinary read-your-writes within one transaction, true for both
        backends) rather than a new post-commit connection that could race
        another writer's own bump. This call never itself advances
        kg_reset_epoch (only delete_notebook_graph_rows does — see the FULL
        CENSUS), so the epoch half is a plain read of whatever the row
        currently holds; it is returned alongside the seq (batch-3-W1 PR-2)
        so ``set_edge_review`` can pair its ReviewQueueMemo carry's expected/
        new version tags with the SAME epoch this transaction observed,
        without a second read. Every OTHER existing caller only cared about
        the seq side effect (``relink_notebook_kg`` and the rest of the
        call sites the FULL CENSUS lists) and discards the return value
        entirely — this is a backward-compatible signature widening
        (int -> tuple[int, int]), not a behavior change for them, following
        the same precedent this docstring already established once before."""
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
        row = self.unified_store.graph_seq_row(connection, notebook_id)
        return int(row[0]), int(row[3])

    def bump_cluster_mutation_seq(
        self, connection: object, notebook_id: str
    ) -> None:
        """concept_clusters 写路径的单调计数器 bump。与 mark_unified_kg_dirty 不同,
        本原语在调用方已持有的写事务 connection 内执行(写簇+bump 同 commit,原子——
        不存在"簇写了、seq 没 bump"的窗口)。kg_mutation_seq 不在此处动:rebuild
        刻意保持它稳定(幂等,见 _cluster_input_version),clusters 的变化信号独立成列。"""
        self.unified_store.bump_cluster_seq(connection, notebook_id, self._now())
