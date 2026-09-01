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
  * 0 extra queries for the sources collection: that signal query PROJECTS the
    user-visible flag (each adapter evaluates its own ``list_sources``
    predicate), so the collection's count and its traversal plan are arithmetic
    over rows already in hand — no second read of ``sources``, and, because both
    come out of the same helper, no way for the map's ``sources: N`` to disagree
    with the list the executor walks;
  * 0 element queries when the notebook's signal fingerprint is unchanged;
  * otherwise one batched ``GROUP BY source_id, element_type`` per batch of
    sources, restricted to the whitelist;
  * 1 O(1) ``unified_kg_state`` seq read, plus — only when that seq moved —
    the per-type GROUP BY, one bounded Memory-source id query, and (only when
    that notebook actually has Memory sources) one bounded per-source GROUP BY
    to subtract them (see ``_scope_kg_counts`` / ``_notebook_kg_counts``: the
    port call is memoized by the store on SQLite but NOT on PostgreSQL, so the
    catalog carries its own seq-keyed memo and both backends get one cheap read
    on the warm path);
  * 1 ``knowhow_tables`` index count.

**Private Memory is never in scope.**  A confirmed Memory is owner-private and
every other channel treats it that way, while a typed-collection listing is
scoped to a notebook's participants and has no owner filter of its own.  So the
map counts — and the enumeration lists — exclude Memory synthetic sources and
the knowledge objects extracted from them, unconditionally: the same listing
means the same thing in a one-person notebook as in a shared one.  The element
side gets this for free (``source_change_signal_rows`` drops those rows, so
they are absent from every count AND from the traversal plan); the KG side
subtracts them here and filters them in the executor.

Four caches, all bounded, all under one lock, and all instance-scoped (NOT
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
  * L4 ``_plan_sources``   — per (notebook, kind) list of the sources that
    hold that kind, keyed like L2 and bounded by total entries.  It is what
    the enumeration executor traverses, and what keeps a resumed enumeration
    from recounting a library that does not fit in L1.
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
from app.services.source_scope import scoped_participants


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
# Total ``ScopeSource`` entries the plan memo may hold across all (notebook,
# kind) pairs.  Each entry is three short fields, so this is a few megabytes
# at worst — and it is a TOTAL, because the size of one plan is a property of
# the library, not a constant.
_MAX_CACHED_PLAN_SOURCES = 50_000


@dataclass(frozen=True)
class ElementKindCount:
    """One whitelist kind: how many elements, and across how many sources."""

    kind: str
    count: int
    sources: int


@dataclass(frozen=True)
class ScopeSource:
    """One physical source an enumeration must visit for a given kind."""

    notebook_id: str
    source_id: str
    count: int


@dataclass(frozen=True)
class ScopeSourcePlan:
    """Which sources the SOURCES collection lists, in traversal order.

    Same four fields as ``ScopeElementPlan`` and deliberately a separate type:
    there ``ScopeSource.count`` means "how many elements of the requested kind
    this source holds", here every entry counts as exactly one listed row, and
    one dataclass carrying both meanings is how a row budget starts being
    charged in element units.

    ``total`` is exactly ``CollectionMap.sources`` for the same scope — both come
    out of ``_visible_signal_rows``.  Order: participants as the caller resolved
    them, and inside each participant ``(created_at, id)`` — i.e. what
    ``list_sources`` returns and the source tab shows. Not the element plan's
    id order: that one exists to keep an ``(source_id, element_id)`` cursor
    aligned, while this roster is re-aligned by KEY on resume and is free to use
    the order a user can actually recognize. Both are stable, which is the
    property a cursor handed back across calls needs.
    """

    notebook_ids: Tuple[str, ...]
    sources: Tuple[ScopeSource, ...]
    total: int
    fingerprint: str


@dataclass(frozen=True)
class ScopeElementPlan:
    """Which sources hold a kind, in traversal order, plus the scope identity.

    This is the map layer's answer to "where would an enumeration have to
    look?", and it is deliberately the ONLY way the executor
    (``app.services.collection_enumeration``) picks sources: the executor's
    physical source set is then the map's source set by construction, not by
    two implementations agreeing.  Diverging sets would surface as the worst
    possible failure — "the map says 12, the list shows 8" — with nothing in
    the response able to explain the gap.

    ``sources`` holds only sources whose count for the kind is non-zero, so a
    50 000-source base costs zero queries for the 49 990 sources that hold no
    formula.  ``total`` is exactly the number ``CollectionMap.element_count``
    reports for the same kind and scope.
    """

    notebook_ids: Tuple[str, ...]
    sources: Tuple[ScopeSource, ...]
    total: int
    fingerprint: str


@dataclass(frozen=True)
class CollectionMap:
    """Counts for one scope.  Every whitelist kind / KG type is always present
    (zero-valued when absent) so the rendered line has a stable shape."""

    notebook_ids: Tuple[str, ...]
    elements: Tuple[ElementKindCount, ...]
    kg_objects: Tuple[Tuple[str, int], ...]
    knowhow_tables: int
    # How many documents the scope holds, in the USER-VISIBLE sense — the number
    # the source tab shows, not the number of physical ``sources`` rows (Memory
    # synthetic rows and Knowhow projection rows are neither listed nor counted).
    # No default: a silently-zero count would render "sources: 0" on a library
    # full of documents, which reads as a fact rather than as a missing field.
    sources: int

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
        f"knowhow tables: {collection_map.knowhow_tables} | "
        f"sources: {collection_map.sources}"
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

    ``collection_map`` then narrows that list by the run's reference-library
    checkboxes (``scoped_participants``) — the map is the number the model
    decides to enumerate from, and counting an unchecked library into it would
    invite it to list documents the enumeration will (correctly) refuse to
    return.  Every count below is a ``for notebook_id in notebook_ids`` loop,
    so the filter reaches the element totals, the per-type KG totals, the
    Knowhow table count and the ``sources`` count from ONE place: they cannot
    drift apart from the plans the executor walks, which are built from the
    same filtered list.  The methods that take ``notebook_ids`` as a PARAMETER
    (``scope_element_plan`` / ``scope_source_plan`` / ``scope_kg_type_counts``
    / ``scope_signal_fingerprint``) deliberately do not re-filter: their caller
    already resolved and narrowed the scope, and a second filter would be a
    second place for the definition to live.

    ``notebook_catalog``'s board counts are pointedly NOT affected — they
    answer "how much knowledge does this notebook hold", not "what may this run
    read".

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
        # notebook_id -> ((kg_reset_epoch, kg_mutation_seq), {object_type: count})
        # R1 (P2-2, post-review, batch-3-W1 PR-2): widened from a bare
        # kg_mutation_seq int -- a delete_notebook_kg + reingest can
        # legitimately re-climb kg_mutation_seq back to a value this memo
        # already cached counts under; epoch is what makes that not alias
        # (zero extra cost: graph_seq_row is already a single-row read here,
        # this just keeps one more int from the same row).
        self._kg_counts: "OrderedDict[str, Tuple[Tuple[int, int], Dict[str, int]]]" = OrderedDict()
        # L4 (notebook_id, kind) -> (signal fingerprint, non-zero source list).
        # L2's twin for the enumeration plan.  It exists for the same reason L2
        # does and then some: a library past ``_MAX_CACHED_SOURCES`` cannot hold
        # its working set in L1, so without this every enumeration — including
        # every RESUMED page of one — re-runs the whole notebook's batched
        # count.  L2 cannot serve it: L2 memoizes per-kind TOTALS, and a plan
        # needs which sources those totals came from.
        self._plan_sources: (
            "OrderedDict[Tuple[str, str], Tuple[str, Tuple[ScopeSource, ...]]]"
        ) = OrderedDict()
        self._plan_source_entries = 0
        # One lock for all four maps: every critical section is a handful of
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
            notebook_ids = scoped_participants(
                self._notebooks.participant_ids(db, active_notebook_id)
            )
            elements, sources = self._scope_signal_row_counts(db, notebook_ids)
            kg_objects = self._scope_kg_counts(db, notebook_ids)
            knowhow_tables = self._scope_knowhow_tables(db, notebook_ids)
        return CollectionMap(
            notebook_ids=notebook_ids,
            elements=elements,
            kg_objects=kg_objects,
            knowhow_tables=knowhow_tables,
            sources=sources,
        )

    def collection_map_text(self, active_notebook_id: str) -> str:
        return render_collection_map(self.collection_map(active_notebook_id))

    def scope_element_plan(
        self, db: object, notebook_ids: Sequence[str], kind: str
    ) -> ScopeElementPlan:
        """Traversal plan for one kind over one already-resolved scope.

        Runs on the CALLER's connection and through the same
        ``source_change_signal_rows`` + per-source count path the map uses, so
        it hits (and fills) L1 rather than opening a second counting road.

        Order is deterministic and backend-independent: participants in the
        order the caller resolved them (active first, then mounted bases in
        mount order), sources sorted by id inside each participant.  The
        signal query has no ORDER BY — neither engine promises a row order and
        SQLite will change it with the plan — so the sort happens here, on a
        list that is already materialized, and costs nothing.  A cursor
        handed back across calls is only meaningful because of this order.

        Memoized per (notebook, kind) on that notebook's own signal
        fingerprint, so a warm scope costs one signal query per participant
        and NO counting — which is what makes a resumed enumeration as cheap
        as its first page on a library too large for L1.
        """
        sources: List[ScopeSource] = []
        all_signals: List[Tuple[str, str]] = []
        total = 0
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            all_signals.extend(signals)
            notebook_sources = self._notebook_plan_sources(
                db, notebook_id, kind, signals
            )
            sources.extend(notebook_sources)
            total += sum(entry.count for entry in notebook_sources)
        return ScopeElementPlan(
            notebook_ids=tuple(notebook_ids),
            sources=tuple(sources),
            total=total,
            fingerprint=signal_fingerprint(all_signals),
        )

    def scope_source_plan(
        self, db: object, notebook_ids: Sequence[str]
    ) -> ScopeSourcePlan:
        """Traversal plan for the SOURCES collection over one resolved scope.

        The user-visible document list, in the same participant/id order the
        element plan uses, with the same signal fingerprint as its scope
        identity — so the sources cursor, the closing stability check and the
        completeness proof are literally the element side's machinery, not a
        second implementation of it.

        It costs NOTHING beyond the signal query the caller pays anyway — the
        visibility flag rides in that query's projection — and no count query:
        the number of visible sources is the length of this list, which is also
        what the map reports (``_visible_signal_rows`` is the one place that
        decides).  Deliberately not memoized: unlike the element plan there is
        no counting to skip and nothing left to read.
        """
        sources: List[ScopeSource] = []
        all_signals: List[Tuple[str, str]] = []
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            all_signals.extend(signals)
            sources.extend(
                self._notebook_visible_sources(notebook_id, signals)
            )
        return ScopeSourcePlan(
            notebook_ids=tuple(notebook_ids),
            sources=tuple(sources),
            total=len(sources),
            fingerprint=signal_fingerprint(all_signals),
        )

    @staticmethod
    def _visible_signal_rows(
        signals: Sequence[Tuple[str, str, str, bool]],
    ) -> List[Tuple[str, str, str, bool]]:
        """One notebook's user-visible source rows, from signals already read.

        THE definition of "which sources does the sources collection contain",
        used by the map's count and by the executor's plan alike — the same
        consistency-by-construction rule the element side follows, and the one
        that keeps "map says 7, list shows 8" impossible.

        Two exclusions, neither of them spelled here:

        * Memory synthetic rows are already absent from
          ``source_change_signal_rows`` (its contract, for the privacy reason
          documented there and in this module's header);
        * the Knowhow projection row is dropped by the row's own
          ``user_visible`` flag, which each adapter evaluates from its own
          user-visible-source predicate — the one ``list_sources`` /
          ``visible_document_count`` share.  So this list is the source tab's
          list, by derivation rather than by resemblance.

        **Zero queries, and that is the point.**  This used to subtract a
        ``hidden_source_ids`` read, one per participant per map build: nothing
        indexes ``source_type``, so that query scanned every source row of the
        notebook to find the one or two hidden ones — immediately after the
        signal query had walked the same rows.  Moving the predicate into the
        signal projection makes the whole thing arithmetic on rows in hand.
        There is deliberately no fallback for a short row: a backend that does
        not carry the flag is a contract violation and should fail loudly here
        rather than silently list a hidden projection source.

        Returns rows, NOT ordered ``ScopeSource``s: the map only needs how many
        there are, and sorting a 50 000-source notebook to produce a length
        would be work the count never uses.  ``_notebook_visible_sources``
        below adds the order, for the one caller that walks them.
        """
        return [row for row in signals if row[3]]

    def _notebook_visible_sources(
        self,
        notebook_id: str,
        signals: Sequence[Tuple[str, str, str, bool]],
    ) -> Tuple[ScopeSource, ...]:
        """The same set, in the order the SOURCE TAB shows it.

        ``(created_at, id)`` — ``list_sources``' own ``ORDER BY``, reproduced
        over the sort keys the signal rows carry.  Ordering by id alone (what
        this did first) produced a roster in an order no user has ever seen,
        which matters the moment anything truncates: "the first 5 documents" of
        an id-ordered roster is an arbitrary subset, while of a creation-ordered
        one it is the 5 the user added first — the only reading of "first" the
        interface supports.

        ``count=1``: for this collection one source IS one listed row, and the
        row budget must be charged in listed rows.
        """
        return tuple(
            ScopeSource(notebook_id=notebook_id, source_id=row[0], count=1)
            for row in sorted(
                self._visible_signal_rows(signals),
                key=lambda row: (row[2], row[0]),
            )
        )

    def _notebook_plan_sources(
        self,
        db: object,
        notebook_id: str,
        kind: str,
        signals: Sequence[Tuple[str, ...]],
    ) -> Tuple[ScopeSource, ...]:
        fingerprint = signal_fingerprint(signals)
        key = (notebook_id, kind)
        with self._lock:
            cached = self._plan_sources.get(key)
            if cached is not None and cached[0] == fingerprint:
                self._plan_sources.move_to_end(key)
                return cached[1]

        counts = self._per_source_counts(db, signals)
        result = tuple(
            ScopeSource(notebook_id=notebook_id, source_id=source_id, count=count)
            for source_id, count in (
                (source_id, int(counts.get(source_id, {}).get(kind, 0)))
                # 元素侧顺序刻意**仍按 source_id**:它的游标是
                # (source_id, element_id) 的 keyset,换成 created_at 序会让
                # 已经发出去的游标在下一次调用里对不上位置。来源清单那侧没有
                # 这个约束(它按 key 重对齐),所以只有它改成来源页签顺序。
                for source_id, _signal, *_ in sorted(signals)
            )
            if count > 0
        )
        # Bounded by TOTAL entries, not by number of plans: one plan is as
        # large as the notebook's non-zero source count, so a per-plan LRU
        # would bound the count of unbounded things.  An oversized plan is
        # simply not stored — recomputing it is cheaper than evicting every
        # other library to hold it.
        with self._lock:
            if len(result) <= _MAX_CACHED_PLAN_SOURCES:
                previous = self._plan_sources.get(key)
                if previous is not None:
                    self._plan_source_entries -= len(previous[1])
                self._plan_sources[key] = (fingerprint, result)
                self._plan_sources.move_to_end(key)
                self._plan_source_entries += len(result)
                while (
                    self._plan_source_entries > _MAX_CACHED_PLAN_SOURCES
                    and len(self._plan_sources) > 1
                ):
                    _evicted_key, evicted = self._plan_sources.popitem(last=False)
                    self._plan_source_entries -= len(evicted[1])
        return result

    def scope_signal_fingerprint(
        self, db: object, notebook_ids: Sequence[str]
    ) -> str:
        """Just the identity half of ``scope_element_plan``.

        Used for the closing check of an enumeration: one signal query per
        participant, no counting, no cache write.  Equality with the opening
        fingerprint is what turns "the cursor ran out" into "the collection is
        complete".
        """
        all_signals: List[Tuple[str, str]] = []
        for notebook_id in notebook_ids:
            all_signals.extend(self._sources.source_change_signal_rows(db, notebook_id))
        return signal_fingerprint(all_signals)

    def scope_kg_type_counts(
        self, db: object, notebook_ids: Sequence[str]
    ) -> Tuple[Tuple[str, int], ...]:
        """Per-type KG totals for a resolved scope — the enumeration's
        denominator, from the same seq-gated memo the map renders."""
        return self._scope_kg_counts(db, notebook_ids)

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
            self._plan_sources.clear()
            self._plan_source_entries = 0

    # ----------------------------------------------------------------- element
    def _scope_signal_row_counts(
        self, db: object, notebook_ids: Sequence[str]
    ) -> Tuple[Tuple[ElementKindCount, ...], int]:
        """Element counts AND the user-visible source count, in one pass.

        Both answers come out of the same ``source_change_signal_rows`` read, so
        they are computed together rather than by two loops: the signal query is
        this module's only unconditional query and it returns one row per source,
        so calling it twice per notebook would double the map's floor cost on a
        50 000-source base for nothing.  Peak memory is unchanged — one
        notebook's signal list at a time, exactly as before.
        """
        totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        source_totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        visible_sources = 0
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            for item in self._notebook_element_counts(db, notebook_id, signals):
                totals[item.kind] += item.count
                source_totals[item.kind] += item.sources
            # 计数只要个数,不要顺序:排序留给真的要遍历那份清单的调用方
            # (`scope_source_plan`),否则 5 万源的库会为了一个 len() 排一遍。
            visible_sources += len(
                self._visible_signal_rows(signals)
            )
        elements = tuple(
            ElementKindCount(
                kind=kind, count=totals[kind], sources=source_totals[kind]
            )
            for kind in ENUMERABLE_ELEMENT_KINDS
        )
        return elements, visible_sources

    def _notebook_element_counts(
        self, db: object, notebook_id: str, signals: Sequence[Tuple[str, ...]]
    ) -> Tuple[ElementKindCount, ...]:
        fingerprint = signal_fingerprint(signals)
        with self._lock:
            cached = self._notebook_counts.get(notebook_id)
            if cached is not None and cached[0] == fingerprint:
                self._notebook_counts.move_to_end(notebook_id)
                return cached[1]

        per_source = self._per_source_counts(db, signals)
        totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        source_totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        for counts in per_source.values():
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
        self, db: object, signals: Sequence[Tuple[str, ...]]
    ) -> Dict[str, Dict[str, int]]:
        """``{source_id: {kind: count}}`` for every source in the notebook,
        served from the LRU where the change signal still matches and queried
        in one batched round trip for the rest.

        Keyed by source id (not a bare list) because the enumeration plan has
        to ask "which sources hold this kind?", and answering that from a
        second query path is exactly how a map and its list start disagreeing.

        The query returns only (source, kind) pairs that actually exist, so a
        source with no whitelisted element caches an empty dict — that is a
        real answer (zero of everything) and must be cached, otherwise every
        prose-only source re-queries forever.
        """
        results: Dict[str, Dict[str, int]] = {}
        stale: List[Tuple[str, str]] = []
        with self._lock:
            for source_id, signal, *_ in signals:
                cached = self._source_counts.get(source_id)
                if cached is not None and cached[0] == signal:
                    self._source_counts.move_to_end(source_id)
                    results[source_id] = cached[1]
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
                results[source_id] = counts
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
        path.  Both backends now serve it from their own seq-gated store-level
        memo (SQLite: #245; PostgreSQL: the large-notebook-latency-analysis
        port, ``postgres/knowledge_counts_cache.type_status_counts``) instead
        of a live ``GROUP BY object_type`` per call.  This catalog-level memo
        is still worth keeping on top of that, though: what it caches is not
        the raw KG count but the Memory-deducted ASSEMBLED result for the
        scope, and recomputing that assembly (even against an already-warm
        store memo) still means one dict walk per notebook in scope on every
        build.  So the catalog carries its own memo keyed on the O(1)
        ``graph_seq_row`` read, and both backends end up paying one
        single-row seq read on the warm path.

        The key is ``kg_mutation_seq`` alone, NOT L2's ``sources`` fingerprint:
        a KG rebuild/merge/promotion moves no ``sources`` row, so an L2-keyed
        entry would keep serving pre-rebuild counts.  It is also not the whole
        ``graph_seq_row`` triple: a cluster rebuild deliberately leaves
        ``kg_mutation_seq`` stable precisely BECAUSE it changes no counts, and
        keying on the triple would throw the memo away on every rebuild.

        Restricted to ``USABLE_STATUSES`` for the same reason retrieval is: a
        deprecated object is not something the model can be told to enumerate.

        And restricted to non-Memory sources, for the reason the element side
        is: a confirmed Memory is owner-private, a typed-collection listing has
        no owner filter, and the map is the listing's denominator.  This is
        deliberately NOT the same number ``notebook_catalog`` shows on the
        board — that one answers "how much knowledge does this notebook hold",
        which legitimately includes the viewer-independent total.  The two
        counts differ on purpose; see ``_notebook_kg_counts``.
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
        """Per-type usable object counts MINUS the ones a private Memory owns.

        Two queries on a miss instead of one, and the second only when the
        notebook has Memory synthetic sources at all (a reference library has
        none, so it keeps paying exactly what it paid before).  Both are
        index-seeked and bounded by the Memory count, and the whole result is
        memoized on ``(kg_reset_epoch, kg_mutation_seq)`` (batch-3-W1 PR-2) —
        kg_mutation_seq is bumped by every write that can move either number,
        including ``ingest_memory_source``'s own post-extraction dirty mark;
        kg_reset_epoch by ``delete_notebook_kg`` alone, closing the aliasing
        window a delete + reingest re-climbing the same raw seq would
        otherwise open.

        The subtraction happens here rather than inside
        ``knowledge_type_count_rows`` because that port is also
        ``notebook_catalog``'s board count, where the Memory objects genuinely
        belong.  Enumeration and the board answer different questions; giving
        them one number would mean getting one of them wrong.
        """
        row = self._unified_kg.graph_seq_row(db, notebook_id)
        version = (int(row[3]), int(row[0]))
        with self._lock:
            cached = self._kg_counts.get(notebook_id)
            if cached is not None and cached[0] == version:
                self._kg_counts.move_to_end(notebook_id)
                return cached[1]

        # Query first, cache after — same rule as the element path.
        counts = {
            row["object_type"]: int(row["c"])
            for row in self._queries.knowledge_type_count_rows(
                db, notebook_id, USABLE_STATUSES
            )
        }
        memory_ids = list(self._sources.memory_source_ids(db, notebook_id))
        if memory_ids:
            for row in self._queries.knowledge_type_count_rows_for_sources(
                db, notebook_id, memory_ids, USABLE_STATUSES
            ):
                object_type = row["object_type"]
                if object_type in counts:
                    # max(0, …) is a floor, not a fix: the two counts come from
                    # one connection but not one snapshot, so a Memory
                    # extraction committing between them can make the
                    # subtrahend the larger number.  A negative count would
                    # then travel into the map line and the coverage
                    # denominator; the seq gate means the wrong entry is
                    # unreachable on the next build anyway.
                    counts[object_type] = max(0, counts[object_type] - int(row["c"]))
        with self._lock:
            self._kg_counts[notebook_id] = (version, counts)
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


def signal_fingerprint(signals: Sequence[Tuple[str, ...]]) -> str:
    """Order-independent digest of a (source, signal) set.

    Public because the enumeration executor needs the SAME digest to decide
    whether a scope stayed still from its first page to its last; a second
    implementation there would be a completeness claim resting on two
    definitions of "unchanged".

    Sorted before hashing so two reads of an unchanged notebook agree
    regardless of row order, and a source added / removed / re-parsed changes
    it.  blake2b at 16 bytes: this gates a cache, not a security boundary, but
    it still has to be collision-free in practice across a library's lifetime.

    Consumes exactly the first two fields and ignores any that follow, so the
    ``created_at`` sort key the rows now carry does NOT enter the digest.  That
    is deliberate and pinned by
    ``test_created_at_key_does_not_change_the_fingerprint``: creation time never
    changes for a live source, so hashing it could only widen the token, and a
    changed fingerprint means "re-count everything" — a cache key must not move
    for a reason that cannot affect what it caches.  Sorting still lands in the
    same order because source ids are unique, so the digest is byte-identical to
    the pre-``created_at`` one.
    """
    digest = hashlib.blake2b(digest_size=16)
    for row in sorted(signals):
        source_id, signal = row[0], row[1]
        digest.update(source_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(signal.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()
