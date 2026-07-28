"""Typed-collection map: how many of each enumerable collection are in scope.

The map is the *地图层* of the reasoning enumeration tools (design doc
``docs/reasoning-enumeration-tools-design.md`` §2.2).  A step-by-step reasoning
run injects one short line of counts into its plan/reflect context so the model
can decide whether enumerating a collection is worth an action at all — the
counts are the cheap thing, the enumeration is the expensive thing.

Three hard properties, in priority order:

1. **Zero model calls.**  Nothing here touches an LLM or an embedder.
2. **Bounded, index-assisted reads.**  Element counts restrict on
   ``element_type`` inside the query (``idx_source_elements_source_type``), so
   a source's prose is never read to count its formulas; KG object counts go
   through the existing ``knowledge_type_count_rows`` port rather than a second
   query path of our own.
3. **No user content.**  The rendered map carries collection kinds and numbers
   only: no titles, no file names, no text.  It is prompt input, and prompt
   input that quotes the corpus is how a "map" silently becomes evidence.

Cost shape (per build, per notebook in scope):

  * 1 ``sources`` query for the change signals — the ONLY unconditional query;
  * 0 element queries when the notebook's signal fingerprint is unchanged;
  * otherwise one batched ``GROUP BY source_id, element_type`` per batch of
    sources, restricted to the whitelist;
  * 1 O(1) ``unified_kg_state`` seq read, plus the per-type GROUP BY only when
    that seq moved (see ``_scope_kg_counts``: the port call is memoized by the
    store on SQLite but NOT on PostgreSQL, so the catalog carries its own
    seq-keyed memo and both backends get one cheap read on the warm path);
  * 1 ``knowhow_tables`` index count.

Three caches, all bounded, all under one lock, and all instance-scoped (NOT
module-globals like ``knowledge_counts_cache``): the per-source key is a plain
``source_id``, and test suites happily reuse literal ids such as ``"sA"``
across throwaway databases, so a process-global map keyed on it could serve one
database's count to another.  One catalog instance per repository runtime keeps
that impossible.

  * L1 ``_source_counts``  — per source, keyed on the source's change signal.
  * L2 ``_notebook_counts``— per notebook element totals, keyed on a
    fingerprint of the whole (source, signal) list.
  * L3 ``_kg_counts``      — per notebook KG per-type totals, keyed on
    ``kg_mutation_seq``.  It must NOT share L2's key: a KG rebuild moves no
    ``sources`` row, so an L2 fingerprint would happily serve stale KG counts.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from app.repositories.ports import (
    NotebookStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
    SourceStorePort,
    UnifiedKgStorePort,
)
from app.services.knowledge_contracts import USABLE_STATUSES


# Enumerable element kinds.  paragraph / heading / page_text / knowhow_cell and
# friends are deliberately absent: listing them is semantically meaningless
# ("here are the notebook's 400 000 paragraphs") and they dominate the table by
# volume.  T3's executor and T4's reflect action share THIS tuple — one
# whitelist, not three copies.
ENUMERABLE_ELEMENT_KINDS: Tuple[str, ...] = ("formula", "table", "image", "code_block")

# KG object types the map reports, in render order.  Mirrors the retrieval-side
# ``_KG_TYPES`` core set; admin-defined dynamic types are intentionally NOT
# surfaced here (the map must stay a fixed-shape, bounded line).
ENUMERABLE_KG_OBJECT_TYPES: Tuple[str, ...] = (
    "concept",
    "claim",
    "formula",
    "procedure",
)

# Hard cap on the rendered map.  It rides in every plan/reflect prompt of a run,
# so its worst case has to be a constant, not a function of library size.
COLLECTION_MAP_MAX_CHARS = 600

# Bounded LRUs.  4096 source-level entries (each a tiny {kind: int} dict) covers
# an ordinary working set; the notebook-level fingerprint memo is what keeps a
# 50k-source mounted base from re-querying after the LRU has rolled over.
_MAX_CACHED_SOURCES = 4096
_MAX_CACHED_NOTEBOOKS = 512


@dataclass(frozen=True)
class ElementKindCount:
    """One whitelist kind: how many elements, and across how many sources."""

    kind: str
    count: int
    sources: int


@dataclass(frozen=True)
class CollectionMap:
    """Counts for one scope.  Every whitelist kind / KG type is always present
    (zero-valued when absent) so the rendered line has a stable shape."""

    notebook_ids: Tuple[str, ...]
    elements: Tuple[ElementKindCount, ...]
    kg_objects: Tuple[Tuple[str, int], ...]
    knowhow_tables: int

    def element_count(self, kind: str) -> int:
        for item in self.elements:
            if item.kind == kind:
                return item.count
        return 0


def render_collection_map(collection_map: CollectionMap) -> str:
    """Render the map as the single prompt line, hard-capped at
    ``COLLECTION_MAP_MAX_CHARS``.

    English keys on purpose: this string goes into the model prompt next to the
    other English scaffolding (it is not user-facing UI copy, so the interface
    vocabulary guard does not apply).  An empty library renders the same shape
    with zeros rather than an empty or absent line — the model must be able to
    tell "nothing there" apart from "no map available".

    The ``(N sources)`` spread is shown only when a kind spans MORE than one
    source: "spread over 1 source" is exactly what a bare non-zero count
    already means, and every character here is prompt budget spent on every
    round of the run.
    """
    elements = ", ".join(
        f"{item.kind} {item.count}"
        + (f" ({item.sources} sources)" if item.sources > 1 else "")
        for item in collection_map.elements
    )
    kg_objects = ", ".join(
        f"{object_type} {count}" for object_type, count in collection_map.kg_objects
    )
    text = (
        "[Collections in scope] "
        f"elements: {elements} | "
        f"KG objects: {kg_objects} | "
        f"knowhow tables: {collection_map.knowhow_tables}"
    )
    if len(text) > COLLECTION_MAP_MAX_CHARS:
        return text[: COLLECTION_MAP_MAX_CHARS - 1] + "…"
    return text


class CollectionCatalogService:
    """Counts the enumerable collections reachable from one active notebook.

    Scope = active notebook + its currently VALID mounted bases, resolved by
    ``NotebookStore.participant_ids`` — the same participant set Ask's federated
    retrieval uses, whose validity predicate lives once in ``mount_sql.py``.  A
    base that was mounted but has since been downgraded or changed owner drops
    out of retrieval and must drop out of the map with it; anything else would
    promise the model collections it cannot reach.

    Failures propagate.  A single source's query blowing up must NOT leave a
    wrong number cached, so nothing is written to any cache until its batch has
    come back whole; the fail-open decision (answer without a map) belongs to
    the caller (T4), which is the only layer that knows whether a run can
    continue.
    """

    def __init__(
        self,
        *,
        database: RepositoryDatabasePort,
        sources: SourceStorePort,
        notebooks: NotebookStorePort,
        queries: QueryStorePort,
        unified_kg: UnifiedKgStorePort,
    ) -> None:
        self._database = database
        self._sources = sources
        self._notebooks = notebooks
        self._queries = queries
        self._unified_kg = unified_kg
        # source_id -> (change signal, {kind: count}).  OrderedDict as LRU.
        self._source_counts: "OrderedDict[str, Tuple[str, Dict[str, int]]]" = (
            OrderedDict()
        )
        # notebook_id -> (fingerprint of the whole signal list, per-kind totals)
        self._notebook_counts: "OrderedDict[str, Tuple[str, Tuple[ElementKindCount, ...]]]" = (
            OrderedDict()
        )
        # notebook_id -> (kg_mutation_seq, {object_type: count})
        self._kg_counts: "OrderedDict[str, Tuple[int, Dict[str, int]]]" = OrderedDict()
        # One lock for all three maps: every critical section is a handful of
        # dict operations, and the service is called from request threads.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def collection_map(self, active_notebook_id: str) -> CollectionMap:
        """Build the scope's counts over one connection.

        One connection, NOT one snapshot: SQLite hands back the thread's reused
        autocommit connection, so each statement reads its own implicit
        transaction and a write landing mid-build can be half-visible across
        the participant / signal / count reads.  That is deliberate and safe
        here because nothing is keyed on cross-table agreement: a count read
        against a newer element generation than its signal simply gets stored
        under the OLD signal and can never be served again (the microsecond
        ``updated_at`` and the monotonic seq never come back), so the next
        build recomputes it.  Wrong entries are unreachable, not sticky.
        """
        with self._database.connect() as db:
            notebook_ids = tuple(
                self._notebooks.participant_ids(db, active_notebook_id)
            )
            elements = self._scope_element_counts(db, notebook_ids)
            kg_objects = self._scope_kg_counts(db, notebook_ids)
            knowhow_tables = self._scope_knowhow_tables(db, notebook_ids)
        return CollectionMap(
            notebook_ids=notebook_ids,
            elements=elements,
            kg_objects=kg_objects,
            knowhow_tables=knowhow_tables,
        )

    def collection_map_text(self, active_notebook_id: str) -> str:
        return render_collection_map(self.collection_map(active_notebook_id))

    def invalidate(self) -> None:
        """Drop every cached count.

        Not needed for correctness — all three keys are change-gated — but a
        cheap safety valve for tests and for anything that wrote outside the
        ordinary pipeline (a raw INSERT bumps no signal and no seq).
        Deliberately clear-ALL with no per-notebook variant: a per-notebook
        drop could only evict L2/L3, while L1 would still answer from the
        unchanged per-source signals, so it would not do what its name
        promises.
        """
        with self._lock:
            self._source_counts.clear()
            self._notebook_counts.clear()
            self._kg_counts.clear()

    # ----------------------------------------------------------------- element
    def _scope_element_counts(
        self, db: object, notebook_ids: Sequence[str]
    ) -> Tuple[ElementKindCount, ...]:
        totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        source_totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        for notebook_id in notebook_ids:
            for item in self._notebook_element_counts(db, notebook_id):
                totals[item.kind] += item.count
                source_totals[item.kind] += item.sources
        return tuple(
            ElementKindCount(
                kind=kind, count=totals[kind], sources=source_totals[kind]
            )
            for kind in ENUMERABLE_ELEMENT_KINDS
        )

    def _notebook_element_counts(
        self, db: object, notebook_id: str
    ) -> Tuple[ElementKindCount, ...]:
        signals = self._sources.source_change_signal_rows(db, notebook_id)
        fingerprint = _fingerprint(signals)
        with self._lock:
            cached = self._notebook_counts.get(notebook_id)
            if cached is not None and cached[0] == fingerprint:
                self._notebook_counts.move_to_end(notebook_id)
                return cached[1]

        per_source = self._per_source_counts(db, signals)
        totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        source_totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        for counts in per_source:
            for kind, count in counts.items():
                if count <= 0:
                    continue
                totals[kind] += count
                source_totals[kind] += 1
        result = tuple(
            ElementKindCount(
                kind=kind, count=totals[kind], sources=source_totals[kind]
            )
            for kind in ENUMERABLE_ELEMENT_KINDS
        )
        with self._lock:
            self._notebook_counts[notebook_id] = (fingerprint, result)
            self._notebook_counts.move_to_end(notebook_id)
            while len(self._notebook_counts) > _MAX_CACHED_NOTEBOOKS:
                self._notebook_counts.popitem(last=False)
        return result

    def _per_source_counts(
        self, db: object, signals: Sequence[Tuple[str, str]]
    ) -> List[Dict[str, int]]:
        """Per-source ``{kind: count}`` for every source in the notebook, served
        from the LRU where the change signal still matches and queried in one
        batched round trip for the rest.

        The query returns only (source, kind) pairs that actually exist, so a
        source with no whitelisted element caches an empty dict — that is a
        real answer (zero of everything) and must be cached, otherwise every
        prose-only source re-queries forever.
        """
        results: List[Dict[str, int]] = []
        stale: List[Tuple[str, str]] = []
        with self._lock:
            for source_id, signal in signals:
                cached = self._source_counts.get(source_id)
                if cached is not None and cached[0] == signal:
                    self._source_counts.move_to_end(source_id)
                    results.append(cached[1])
                else:
                    stale.append((source_id, signal))
        if not stale:
            return results

        # Query first, cache after: a failure here raises out of the whole
        # build with nothing half-written.
        fresh: Dict[str, Dict[str, int]] = {source_id: {} for source_id, _ in stale}
        for source_id, element_type, count in self._sources.element_type_count_rows(
            db, [source_id for source_id, _ in stale], ENUMERABLE_ELEMENT_KINDS
        ):
            if source_id in fresh:
                fresh[source_id][element_type] = count
        with self._lock:
            for source_id, signal in stale:
                counts = fresh[source_id]
                self._source_counts[source_id] = (signal, counts)
                self._source_counts.move_to_end(source_id)
                results.append(counts)
            while len(self._source_counts) > _MAX_CACHED_SOURCES:
                self._source_counts.popitem(last=False)
        return results

    # ---------------------------------------------------------------------- KG
    def _scope_kg_counts(
        self, db: object, notebook_ids: Sequence[str]
    ) -> Tuple[Tuple[str, int], ...]:
        """Sum the per-type counts over the scope, memoized per notebook on
        ``kg_mutation_seq``.

        ``knowledge_type_count_rows`` is the SAME port call
        ``notebook_catalog`` makes for the notebook summary — no second query
        path.  But its cost is backend-dependent: on SQLite the store serves it
        from the seq-gated memo (#245), while on PostgreSQL it is a live
        ``GROUP BY object_type`` every time, which on a multi-million-object
        library is a per-call scan we would otherwise pay on every build of
        every notebook in scope.  So the catalog carries its own memo keyed on
        the O(1) ``graph_seq_row`` read, and both backends end up paying one
        single-row seq read on the warm path.

        The key is ``kg_mutation_seq`` alone, NOT L2's ``sources`` fingerprint:
        a KG rebuild/merge/promotion moves no ``sources`` row, so an L2-keyed
        entry would keep serving pre-rebuild counts.  It is also not the whole
        ``graph_seq_row`` triple: a cluster rebuild deliberately leaves
        ``kg_mutation_seq`` stable precisely BECAUSE it changes no counts, and
        keying on the triple would throw the memo away on every rebuild.

        Restricted to ``USABLE_STATUSES`` for the same reason retrieval is: a
        deprecated object is not something the model can be told to enumerate.
        """
        totals: Dict[str, int] = {
            object_type: 0 for object_type in ENUMERABLE_KG_OBJECT_TYPES
        }
        for notebook_id in notebook_ids:
            for object_type, count in self._notebook_kg_counts(db, notebook_id).items():
                if object_type in totals:
                    totals[object_type] += count
        return tuple(
            (object_type, totals[object_type])
            for object_type in ENUMERABLE_KG_OBJECT_TYPES
        )

    def _notebook_kg_counts(self, db: object, notebook_id: str) -> Dict[str, int]:
        seq = int(self._unified_kg.graph_seq_row(db, notebook_id)[0])
        with self._lock:
            cached = self._kg_counts.get(notebook_id)
            if cached is not None and cached[0] == seq:
                self._kg_counts.move_to_end(notebook_id)
                return cached[1]

        # Query first, cache after — same rule as the element path.
        counts = {
            row["object_type"]: int(row["c"])
            for row in self._queries.knowledge_type_count_rows(
                db, notebook_id, USABLE_STATUSES
            )
        }
        with self._lock:
            self._kg_counts[notebook_id] = (seq, counts)
            self._kg_counts.move_to_end(notebook_id)
            while len(self._kg_counts) > _MAX_CACHED_NOTEBOOKS:
                self._kg_counts.popitem(last=False)
        return counts

    # ----------------------------------------------------------------- knowhow
    def _scope_knowhow_tables(self, db: object, notebook_ids: Sequence[str]) -> int:
        """How many Knowhow tables the scope holds — one index count per
        notebook over ``idx_knowhow_tables_nb``, on the caller's connection.

        Deliberately NOT ``knowhow_enumeration_catalog``: that one also runs an
        aggregate CTE with a per-table row COUNT and change-log MAX and is not
        memoized anywhere, which is a lot of work for a number we do not use.
        ``list_knowhow_tables`` is worse still (it hydrates projection health).
        Reusing the generic ``count_rows`` primitive keeps this at one bounded
        index count, cheap enough that it needs no cache of its own.
        """
        return sum(
            self._queries.count_rows(db, "knowhow_tables", "notebook_id", notebook_id)
            for notebook_id in notebook_ids
        )


def _fingerprint(signals: Sequence[Tuple[str, str]]) -> str:
    """Order-independent digest of the notebook's whole (source, signal) set.

    Sorted before hashing so two reads of an unchanged notebook agree
    regardless of row order, and a source added / removed / re-parsed changes
    it.  blake2b at 16 bytes: this gates a cache, not a security boundary, but
    it still has to be collision-free in practice across a library's lifetime.
    """
    digest = hashlib.blake2b(digest_size=16)
    for source_id, signal in sorted(signals):
        digest.update(source_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(signal.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
