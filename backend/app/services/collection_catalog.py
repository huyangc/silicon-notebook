"""Typed-collection map: how many of each enumerable collection are in scope.

The map is the *地图层* of the reasoning enumeration tools (design doc
``docs/superpowers/specs/2026-07-28-reasoning-enumeration-tools-design.md`` §2.2).  A step-by-step reasoning
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
    sources for the non-``table`` whitelist kinds (covering-index scan), PLUS
    one more batched ``GROUP BY source_id`` scoped to ``table`` alone (index
    seek + a ``location_label`` heap fetch, to exclude an overlong table's
    later split segments from the count — ``element_type_count_rows`` on
    each store adapter has the "why two statements, why ``location_label``
    and not ``metadata``" detail);
  * 1 O(1) ``unified_kg_state`` seq read, plus — only when that seq moved —
    the per-type GROUP BY, one bounded Memory-source id query, and (only when
    that notebook actually has Memory sources) one bounded per-source GROUP BY
    to subtract them (see ``_scope_kg_counts`` / ``_notebook_kg_counts``: the
    port call is memoized by the store on SQLite but NOT on PostgreSQL, so the
    catalog carries its own seq-keyed memo and both backends get one cheap read
    on the warm path);
  * 1 ``knowhow_tables`` index count — for the active notebook only, and only
    when the Knowhow enumeration executor can reach it at all (see
    ``knowhow_enumeration_reachable``).

**The run's source ceiling is honoured by every number and plan — when it
binds.**  A ceiling binds in a subjectless (global) run (each library's
frozen visible-source list, always) and, in a single-notebook run, only when
the source scope is really narrowed or has drifted since it was frozen — the
caller's once-per-run verdict, passed in as ``ceiling_binds`` (see
``source_ceiling``).  The browser freezes an include list even when every
source is ticked; such an un-narrowed, un-drifted run behaves exactly as it
did before the ceiling existed.  While a ceiling binds, every count, every
plan and every scope fingerprint is taken over the IN-CEILING sources of each
participant only, via ONE predicate (``source_ceiling``).  The memos stay
scope-independent: L2/L4 are still keyed on the notebook's whole signal list,
and the ceiling is applied AFTER them, so a run with different ticks never
poisons another run's cache.  Two consequences, for narrowed, drifted and
global runs only (the sentences for ``docs/product-and-api.md`` are with the
PR-B docs task):

  * a KG object is in the collection only when at least one of its evidence
    items comes from an in-ceiling source, so an object with no evidence at
    all is not listed and not counted;
  * a source uploaded after the freeze is outside the ceiling, so it is neither
    counted nor listed, and it does not move the scope fingerprint (an
    out-of-ceiling upload is not a ``concurrent_change``; an in-ceiling reparse
    still is).

Without a binding ceiling every number, plan, fingerprint and cursor is
byte-identical to what this module produced before the ceiling existed.

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
  * L3 ``_kg_counts``      — per (notebook, source-ceiling digest) KG
    per-type totals, keyed on ``kg_mutation_seq``.  It must NOT share L2's
    key: a KG rebuild moves no ``sources`` row, so an L2 fingerprint would
    happily serve stale KG counts.  The ceiling digest is part of the KEY
    (``""`` for "no ceiling", so an unscoped build hits exactly the entry it
    always did) because a ceiling count is a different number, not a
    different version of the same one; the LRU is bounded at
    ``_MAX_CACHED_NOTEBOOKS`` entries TOTAL across all (notebook, ceiling)
    pairs, so a user clicking through many tick combinations evicts old
    combinations rather than growing the map.
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
from functools import cached_property
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.repositories.ports import (
    KnowledgeStorePort,
    NotebookStorePort,
    QueryStorePort,
    RepositoryDatabasePort,
    SourceStorePort,
    UnifiedKgStorePort,
)
from app.services.knowledge_contracts import USABLE_STATUSES
# Reader #3 on ``retrieval_participants``' frozen whitelist: the collection map
# reports what THIS RUN may enumerate, so a participant override has to reach
# it or the map would advertise libraries retrieval will not read.
from app.services.retrieval_participants import (
    resolve_retrieval_participant_ids,
)
from app.services.retrieval_run import current_viewer_id, memoized_retrieval_value
from app.services.source_scope import (
    CeilingSet,
    collection_ceiling_drifted,
    current_source_scope,
    record_collection_ceiling_drift,
    scoped_participants,
    source_scope_restricted,
    subjectless_run_active,
)


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


@dataclass(frozen=True, eq=False)
class SourceCeiling:
    """One participant's frozen source ceiling.

    Built ONLY by ``CollectionCatalogService.source_ceiling``; every reader —
    the element counts and plans, the source roster, the three scope
    fingerprints, the KG count, the KG page pushdown, the executor's evidence
    references — asks this one object, so "in the ceiling" has one meaning for
    the number and for the list.  An empty ``members`` is an explicit deny,
    not "no ceiling"; "no ceiling" is ``source_ceiling`` returning ``None``.

    ``members`` is the scope's OWN frozenset whenever the scope holds one (a
    per-library freeze, or ticks with no hidden half) — accepted as-is, never
    copied.  The two derived shapes are lazy and computed at most once per
    object: ``source_ids`` (sorted) and ``digest`` (for the L3 memo key and
    cursor identity).  The store is handed ``members`` itself, never
    ``source_ids``: its bound form (``repositories/*/source_ceiling
    .ceiling_param``) is cached by that frozenset's identity, so one binding
    serves every page and count of the run.  Membership
    alone never sorts anything.  ``eq=False``: identity, not a comparison of
    two 49k-element sets.
    """

    members: frozenset

    def allows(self, source_id: str) -> bool:
        return bool(source_id) and source_id in self.members

    @cached_property
    def source_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(value for value in self.members if value))

    @cached_property
    def digest(self) -> str:
        """Order-independent by construction: a hash of the SORTED ids, taken
        once per ceiling object.  ``c:`` prefix because a deny-all ceiling
        digests the empty input and must not collide with the ``""`` key an
        unscoped build uses."""
        digest = hashlib.blake2b(digest_size=16)
        for source_id in self.source_ids:
            digest.update(source_id.encode("utf-8"))
            digest.update(b"\x00")
        return "c:" + digest.hexdigest()


def _make_ceiling(source_ids) -> SourceCeiling:
    return SourceCeiling(members=CeilingSet(str(value) for value in source_ids if value))


def _scope_ceiling(scope, notebook_id: str) -> SourceCeiling:
    """The ceiling a scope holds AS A SET for ``notebook_id``, without copying
    it when it can be avoided: the per-library freeze is the scope's own
    frozenset; local ticks with no hidden half are ``source_ids`` itself; only
    ticks plus a hidden half need one union.  Callers have already checked that
    one of those two branches applies (``source_ceiling``)."""
    ceiling = scope.source_ceiling_for(notebook_id)
    if ceiling is not None:
        return SourceCeiling(members=ceiling)
    if not scope.hidden_source_ids:
        return SourceCeiling(members=scope.source_ids)
    return SourceCeiling(
        members=CeilingSet(scope.source_ids | scope.hidden_source_ids))


def _local_freeze(scope: Any, notebook_id: str) -> bool:
    """The active library carries the browser's frozen include list."""
    return bool(
        scope.ceiling_active
        and notebook_id == scope.notebook_id
        and scope.mode == "include"
    )


def _frozen_ceiling(scope: Any, notebook_id: str) -> SourceCeiling:
    """The frozen ceiling of ``notebook_id``, built once per run."""
    return memoized_retrieval_value(
        ("collection_source_ceiling", scope, notebook_id),
        lambda: _scope_ceiling(scope, notebook_id),
    )


def knowhow_enumeration_reachable(scope_unsafe: Optional[bool] = None) -> bool:
    """Can this run's Knowhow full enumeration read any table at all?

    The Knowhow executor (``AskService``'s completeness lane over
    ``knowhow_enumeration_catalog``) reads the ACTIVE notebook's tables only,
    and only when the run is not subjectless (a global run has no current
    library whose tables it would be reading) and its source scope is SAFE:
    neither narrowed (a table is not a ticked source, so the lane is switched
    off rather than half-filtered) nor drifted (a table whose hidden
    projection source appeared after the freeze is outside the ceiling).
    The collection map's ``knowhow tables`` count mirrors exactly that, so the
    map never advertises tables no executor can list — mounted libraries'
    tables in particular are not counted, because nothing enumerates them.

    ``scope_unsafe`` is the caller's verdict from
    ``reasoning_retrieval.unsafe_scope_restricted`` (narrowed OR drifted).
    This module has no retrieval port and cannot run the drift probe itself,
    so the verdict is PASSED IN: ``reasoning_retrieval
    .knowhow_completeness_reachable`` evaluates it once per retrieval run
    (memoized) and hands the same answer to the executor's gate
    (``AskService._knowhow_completeness_in_scope``) and to the map
    (``collection_map(knowhow_reachable=...)``) — one predicate, one
    evaluation.  ``None`` (a caller with no retriever) falls back to the
    narrowing half alone, which is all that can be known without the probe.
    """
    if scope_unsafe is None:
        scope_unsafe = source_scope_restricted()
    return not subjectless_run_active() and not scope_unsafe


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
    # The same user-visible count restricted to the ACTIVE notebook — the
    # ``sources`` total minus every mounted reference library's share.  Two
    # consumers now.  It is rendered beside the federated total because the
    # source roster is enumerable at EITHER scope (``enumerate.scope``), so the
    # model choosing between them needs both numbers, not one.  And it exists
    # in the first place because ``AskService._no_kg_scope_admits_run`` had to
    # judge a channel whose reach was narrower than the map's: source-passage
    # retrieval (``search_chunks`` / the first-round passage seed, which since
    # 2026-09-29 runs whether or not the scope has a graph) rides chunk
    # mode's own primitives, and those used to be active-notebook-local.  They
    # are federated now (``chunk_federation``), so that gate reads ``sources``
    # whenever ``CHUNK_FEDERATION_ENABLED`` is on and falls back to this number
    # only on the rollback switch -- the second consumer did not disappear, its
    # reach became conditional.  Deriving it here rather than counting again at
    # the call site costs zero extra queries (the per-notebook loop already
    # reads each participant's signal rows) and keeps ONE definition of
    # "user-visible source" for both numbers.
    # No default, for ``sources``' reason and then some: a silent zero here
    # turns into a refusal to answer.
    active_sources: int

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

    ``sources`` is the one count rendered TWICE — the federated total and, in
    parentheses, the active notebook's share.  It is the only collection whose
    enumeration takes a scope parameter (``enumerate.scope``), so the model has
    to pick between two numbers; showing only the federated one leaves it
    guessing what ``current_notebook`` would return, and a wrong guess is not
    free (a listing spends the run's shared row budget).  Unconditional, unlike
    the element spread above: "the two numbers happen to be equal" and "this
    build does not report the second number" must not look the same, and the
    equal case is exactly the one where picking either scope is safe.

    In peer (subjectless) runs the parenthesised share is omitted, because
    the ``current_notebook`` enumeration scope is not offered there (and
    ``enumerate_sources`` ignores ``local_only``); the single predicate is
    ``not subjectless_run_active()``, shared with the prompts and
    ``reasoning_retrieval.local_only_scope_offered()``.
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
        + ("" if subjectless_run_active() else
           f" (current notebook: {collection_map.active_sources})")
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
    promise the model collections it cannot reach.  When a run installs a
    participant override that set is REPLACED by the override's libraries, for
    the same reason: the map must describe the libraries this run reads, and a
    federated run reads libraries that are not mounted into its nominal active
    notebook at all.

    ``collection_map`` then narrows that list by the run's reference-library
    checkboxes (``scoped_participants``) — the map is the number the model
    decides to enumerate from, and counting an unchecked library into it would
    invite it to list documents the enumeration will (correctly) refuse to
    return.  Every count below is a ``for notebook_id in notebook_ids`` loop,
    so the filter reaches the element totals, the per-type KG totals and the
    ``sources`` count from ONE place: they cannot drift apart from the plans
    the executor walks, which are built from the same filtered list.  (The
    Knowhow table count reads the same list but counts only what the Knowhow
    executor reaches — see ``_reachable_knowhow_tables``.)  The methods that
    take ``notebook_ids`` as a PARAMETER (``scope_element_plan`` /
    ``scope_source_plan`` / ``scope_kg_type_counts`` /
    ``scope_signal_fingerprint``) deliberately do not re-filter the LIBRARY
    list: their caller already resolved and narrowed it, and a second filter
    would be a second place for the definition to live.  The SOURCE ceiling
    inside each library is a different dimension and is applied inside every
    one of them, through ``source_ceiling``.

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
        knowledge: KnowledgeStorePort,
    ) -> None:
        self._database = database
        self._sources = sources
        self._notebooks = notebooks
        self._queries = queries
        self._unified_kg = unified_kg
        # Read ONLY under a source ceiling (``_ceiling_kg_counts``): the
        # per-type count of objects SUPPORTED by in-ceiling sources is
        # ``count_knowledge(supported_by_source_ids=...)``, the exact
        # denominator of ``knowledge_object_page_rows(allowed_source_ids=...)``
        # — the store the enumeration executor pages from.
        self._knowledge = knowledge
        # source_id -> (change signal, {kind: count}).  OrderedDict as LRU.
        self._source_counts: "OrderedDict[str, Tuple[str, Dict[str, int]]]" = (
            OrderedDict()
        )
        # notebook_id -> (fingerprint of the whole signal list, per-kind totals)
        self._notebook_counts: "OrderedDict[str, Tuple[str, Tuple[ElementKindCount, ...]]]" = (
            OrderedDict()
        )
        # (notebook_id, ceiling digest) -> ((kg_reset_epoch, kg_mutation_seq),
        # {object_type: count}).  The digest is "" without a ceiling.
        # R1 (P2-2, post-review, batch-3-W1 PR-2): widened from a bare
        # kg_mutation_seq int -- a delete_notebook_kg + reingest can
        # legitimately re-climb kg_mutation_seq back to a value this memo
        # already cached counts under; epoch is what makes that not alias
        # (zero extra cost: graph_seq_row is already a single-row read here,
        # this just keeps one more int from the same row).
        self._kg_counts: (
            "OrderedDict[Tuple[str, str], Tuple[Tuple[int, int], Dict[str, int]]]"
        ) = OrderedDict()
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
        # One lock for all the maps: every critical section is a handful of
        # dict operations, and the service is called from request threads.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def collection_map(
        self,
        active_notebook_id: str,
        *,
        knowhow_reachable: Optional[bool] = None,
        ceiling_binds: bool = True,
    ) -> CollectionMap:
        """Build the scope's counts over one connection.

        ``ceiling_binds`` is the caller's once-per-run verdict "this run's
        source scope is narrowed or has drifted" (see ``source_ceiling``); the
        default is the SAFE one — apply the ceiling — so a caller that does not
        pass it over-filters rather than leaks.

        ``knowhow_reachable`` is the run's verdict on whether the Knowhow
        complete enumeration may run (``reasoning_retrieval
        .knowhow_completeness_reachable`` — the same memoized value the
        executor's gate reads, drift included).  ``None`` falls back to
        ``knowhow_enumeration_reachable()`` without a drift verdict.

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
            # Order is load-bearing: the override REPLACES the participant set,
            # ``scoped_participants`` then NARROWS whatever came out by the
            # run's library checkboxes. Reversing them would narrow the mount
            # table and then throw the result away.
            notebook_ids = scoped_participants(
                resolve_retrieval_participant_ids(
                    active_notebook_id,
                    lambda: self._notebooks.participant_ids(
                        db, active_notebook_id, viewer_id=current_viewer_id(),
                    ),
                )
            )
            elements, sources, active_sources = self._scope_signal_row_counts(
                db, notebook_ids, active_notebook_id, ceiling_binds=ceiling_binds,
            )
            kg_objects = self._scope_kg_counts(
                db, notebook_ids, ceiling_binds=ceiling_binds,
            )
            scope = current_source_scope()
            knowhow_tables = self._reachable_knowhow_tables(
                db, notebook_ids, active_notebook_id,
                (knowhow_enumeration_reachable() if knowhow_reachable is None
                 else knowhow_reachable)
                # Drift found by THIS build's signal read (see
                # ``_fast_path_holds``): the sources changed after the freeze.
                and not (scope is not None and collection_ceiling_drifted(
                    scope, active_notebook_id)),
            )
        return CollectionMap(
            notebook_ids=notebook_ids,
            elements=elements,
            kg_objects=kg_objects,
            knowhow_tables=knowhow_tables,
            sources=sources,
            active_sources=active_sources,
        )

    def collection_map_text(
        self,
        active_notebook_id: str,
        *,
        knowhow_reachable: Optional[bool] = None,
        ceiling_binds: bool = True,
    ) -> str:
        return render_collection_map(self.collection_map(
            active_notebook_id, knowhow_reachable=knowhow_reachable,
            ceiling_binds=ceiling_binds,
        ))

    def scope_element_plan(
        self,
        db: object,
        notebook_ids: Sequence[str],
        kind: str,
        *,
        ceiling_binds: bool = True,
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

        Under a source ceiling the memoized plan is filtered AFTER the memo, by
        ``source_ceiling`` — the memo itself stays scope-independent, so two
        runs with different ticks share it — and the fingerprint hashes the
        in-ceiling signals only (see ``_ceiling_split``).
        """
        sources: List[ScopeSource] = []
        all_signals: List[Tuple[str, str]] = []
        total = 0
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            kept, excluded = self._ceiling_split(
                db, notebook_id, signals, ceiling_binds=ceiling_binds,
            )
            all_signals.extend(kept)
            notebook_sources = self._notebook_plan_sources(
                db, notebook_id, kind, signals
            )
            if excluded:
                dropped = {row[0] for row in excluded}
                notebook_sources = tuple(
                    entry for entry in notebook_sources
                    if entry.source_id not in dropped
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
        self,
        db: object,
        notebook_ids: Sequence[str],
        *,
        ceiling_binds: bool = True,
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
            kept, _excluded = self._ceiling_split(
                db, notebook_id, signals, ceiling_binds=ceiling_binds,
            )
            all_signals.extend(kept)
            sources.extend(
                self._notebook_visible_sources(notebook_id, kept)
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
        """One notebook's user-visible source rows, from IN-CEILING signals
        already read (callers pass ``_ceiling_split``'s ``kept`` half — the
        source ceiling is applied there, once, for the roster, the counts and
        the fingerprints alike, rather than a second time here).

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
        self,
        db: object,
        notebook_ids: Sequence[str],
        *,
        ceiling_binds: bool = True,
    ) -> str:
        """Just the identity half of ``scope_element_plan``.

        Used for the closing check of an enumeration: one signal query per
        participant, no counting, no cache write.  Equality with the opening
        fingerprint is what turns "the cursor ran out" into "the collection is
        complete".  In-ceiling signals only, exactly like the opening
        fingerprints, or every closing check under a ceiling would disagree
        with its own opening.
        """
        all_signals: List[Tuple[str, str]] = []
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            all_signals.extend(self._ceiling_split(
                db, notebook_id, signals, ceiling_binds=ceiling_binds,
            )[0])
        return signal_fingerprint(all_signals)

    def source_ceiling(
        self,
        db: object,
        notebook_id: str,
        signals: Optional[Sequence[Tuple[str, ...]]] = None,
        *,
        ceiling_binds: bool = True,
    ) -> Optional[SourceCeiling]:
        """``notebook_id``'s frozen source ceiling for THIS run, or ``None``
        when no ceiling binds that library.

        WHEN a ceiling binds (product decision, 2026-09-29): in a subjectless
        (global) run the per-library ceilings always bind; in a single-notebook
        run only when ``ceiling_binds`` is true — the caller's once-per-run
        verdict "this run's source scope is narrowed or has drifted", the same
        one the ``source_scoped`` disclosure reads.  The browser freezes an
        include list even when every source is ticked (``narrowed=False``);
        such an un-narrowed, un-drifted run must enumerate and count exactly as
        it did before the ceiling existed — no pushdown into the KG statements,
        objects without evidence listed and counted — and its frozen list
        admits every live source anyway, so skipping it loses nothing.  The
        default is the SAFE value (bind), so a caller that does not pass the
        verdict over-filters rather than leaks.  The private-Memory exclusion
        does not depend on any of this; it is unconditional.  Two ceilings bind
        whatever the verdict says, the same two arms as the knowledge re-read
        verdict (``source_scope.ceiling_binds``): a library the scope excludes
        (deny all), and a library that carries its own per-notebook freeze —
        the verdict speaks only for the active notebook's ticks and drift, and
        nothing proves such a freeze still equals that library's sources.
        Neither occurs on a browser run's all-ticked path (unticked libraries
        are not participants; per-notebook freezes come with global runs), so
        its bytes are unchanged.

        THE one definition every collection reader shares (``SourceCeiling``'s
        docstring lists them).  Same branch order as
        ``scoped_allowed_source_ids`` and ``ActiveSourceScope.allows``:
        library exclusion (explicit deny), then the per-notebook freeze (a
        federated run's visible sources), then the active notebook's local
        include list (ticks ∪ the requester's own hidden sources) — so a row
        this ceiling admits is a row ``source_allowed`` admits, and vice versa
        (``test_source_ceiling_members_equal_scoped_allowed_source_ids`` pins
        the equivalence for every shape).  It does not CALL
        ``scoped_allowed_source_ids`` because that sorts the whole list on
        every call; this takes the scope's own frozenset as-is and sorts only
        when a store parameter or a digest is actually needed.

        Memoized on the RETRIEVAL RUN (``memoized_retrieval_value``, keyed on
        the scope object and the notebook), not in a process-level cache: the
        repeats that matter (plan, closing fingerprint, KG count, KG walk,
        cursor digest) all happen inside one run, and a process cache keyed by
        scope objects would pin megabyte-sized sets of runs long gone.
        Outside a run it is simply built — cheap, since nothing is copied.

        The one shape the scope cannot hand over as a set is the legacy
        EXCLUDE-mode local ceiling (a direct service caller, or a report scope
        persisted before the include freeze).  It is materialized here from
        the notebook's live signal rows filtered by ``allows`` — ``signals``
        when the caller already holds them, otherwise one signal read — and
        not memoized, because it depends on live rows: an exclusion list
        cannot be pushed into a KG page query or hashed into a memo key, and
        treating it as "no ceiling" would list exactly the excluded sources.
        """
        scope = current_source_scope()
        if scope is None:
            return None
        if not scope.covers_notebook(notebook_id):
            return SourceCeiling(members=frozenset())
        if (
            not ceiling_binds
            and not scope.subjectless
            and scope.source_ceiling_for(notebook_id) is None
            and self._fast_path_holds(scope, notebook_id, signals)
        ):
            return None
        if (
            scope.source_ceiling_for(notebook_id) is not None
            or _local_freeze(scope, notebook_id)
        ):
            return _frozen_ceiling(scope, notebook_id)
        if not (scope.ceiling_active and notebook_id == scope.notebook_id):
            return None
        if signals is None:
            signals = self._sources.source_change_signal_rows(db, notebook_id)
        return _make_ceiling(
            row[0] for row in signals if scope.allows(notebook_id, row[0])
        )

    def _fast_path_holds(
        self,
        scope: Any,
        notebook_id: str,
        signals: Optional[Sequence[Tuple[str, ...]]],
    ) -> bool:
        """Verify-on-read for the un-bound fast path (codex #817 r1, the rule
        #806 adopted for ``node_context``): the run's verdict "not narrowed, not
        drifted" is memoised, so a source uploaded after it was taken would
        otherwise be counted, listed and cited by every later collection read.

        A read that has the library's signal rows in hand checks them against
        the frozen ceiling (frozenset membership, nothing normalised).  One row
        outside it means the source set drifted after the verdict: the drift is
        recorded on the run (``record_collection_ceiling_drift``) and THIS read
        and every later one of the library binds the ceiling.  A read without
        rows in hand (a KG count, a cursor digest) trusts the fast path only
        until some read has recorded drift; the executor's KG entry confirms it
        first (``confirm_fast_path``) and re-checks every row it returns
        (``fast_path_members``).  Libraries without a local include freeze have
        nothing to verify against and keep the historical behaviour.
        """
        if collection_ceiling_drifted(scope, notebook_id):
            return False
        if signals is None or not _local_freeze(scope, notebook_id):
            return True
        members = _frozen_ceiling(scope, notebook_id).members
        if all(row[0] in members for row in signals):
            return True
        record_collection_ceiling_drift(scope, notebook_id)
        return False

    def confirm_fast_path(
        self, db: object, notebook_ids: Sequence[str], *, ceiling_binds: bool,
    ) -> None:
        """Before a read that holds no signal rows (the KG walk and its count),
        verify the un-bound fast path once: one signal read of the library
        carrying the local freeze, judged by ``_fast_path_holds``.  A no-op
        when the ceiling binds anyway, when drift is already recorded, and when
        no participant carries a local include freeze."""
        scope = current_source_scope()
        if ceiling_binds or scope is None or scope.subjectless:
            return
        for notebook_id in notebook_ids:
            if (
                _local_freeze(scope, notebook_id)
                and not collection_ceiling_drifted(scope, notebook_id)
            ):
                self._fast_path_holds(
                    scope, notebook_id,
                    list(self._sources.source_change_signal_rows(db, notebook_id)),
                )

    def fast_path_members(
        self, notebook_id: str, *, ceiling_binds: bool,
    ) -> Optional[frozenset]:
        """The frozen ceiling a fast-path read must still honour row by row:
        the library's local include freeze when its reads are NOT binding it
        (``source_ceiling`` returned ``None`` for it), else ``None`` — a bound
        read is filtered in SQL, and a library without a freeze has nothing to
        verify against."""
        scope = current_source_scope()
        if (
            ceiling_binds or scope is None or scope.subjectless
            or not _local_freeze(scope, notebook_id)
            or collection_ceiling_drifted(scope, notebook_id)
        ):
            return None
        return _frozen_ceiling(scope, notebook_id).members

    def scope_ceiling_digest(
        self,
        db: object,
        notebook_ids: Sequence[str],
        *,
        ceiling_binds: bool = True,
    ) -> str:
        """One digest of every participant's source ceiling, for cursor identity.

        ``""`` when no participant carries a ceiling — so a cursor cut without
        one is exactly the cursor cut before this existed.  Otherwise a digest
        over ``(notebook, ceiling digest)`` pairs in participant order.

        Why a cursor needs it although the scope fingerprints already hash only
        in-ceiling signals: a ceiling can change without changing which LIVE
        sources are in it (the requester's own hidden Memory ids ride in the
        include ceiling and have no signal row), and the KG side's identity is
        the graph seq vector, which a ceiling change does not move at all.  A
        continuation cut under one ceiling and resumed under another would
        then silently skip the rows the new ceiling admits before the cursor
        position.  Each ceiling's own digest is computed once per ceiling
        object (``SourceCeiling.digest`` is cached, and built ceilings are
        memoized per scope), not per page.
        """
        pairs = []
        for notebook_id in notebook_ids:
            ceiling = self.source_ceiling(
                db, notebook_id, ceiling_binds=ceiling_binds,
            )
            if ceiling is not None:
                pairs.append((notebook_id, ceiling.digest))
        if not pairs:
            return ""
        digest = hashlib.blake2b(digest_size=16)
        for notebook_id, ceiling_digest in pairs:
            digest.update(notebook_id.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(ceiling_digest.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def _ceiling_split(
        self,
        db: object,
        notebook_id: str,
        signals: Sequence[Tuple[str, ...]],
        *,
        ceiling_binds: bool = True,
    ) -> Tuple[List[Tuple[str, ...]], List[Tuple[str, ...]]]:
        """``(in-ceiling signal rows, out-of-ceiling signal rows)``.

        Everything scope-shaped in this module is derived from the first half:
        counts, plans, the source roster and the three scope fingerprints.  No
        ceiling (or one that admits every row) returns ``(signals, [])``, which
        is what keeps an unscoped build byte-identical — the fingerprint of the
        whole list, the notebook-level L2 figure, the unfiltered L4 plan.
        """
        ceiling = self.source_ceiling(
            db, notebook_id, signals, ceiling_binds=ceiling_binds,
        )
        if ceiling is None:
            return list(signals), []
        kept: List[Tuple[str, ...]] = []
        excluded: List[Tuple[str, ...]] = []
        for row in signals:
            (kept if ceiling.allows(row[0]) else excluded).append(row)
        return kept, excluded

    def scope_kg_type_counts(
        self,
        db: object,
        notebook_ids: Sequence[str],
        *,
        ceiling_binds: bool = True,
    ) -> Tuple[Tuple[str, int], ...]:
        """Per-type KG totals for a resolved scope — the enumeration's
        denominator, from the same seq-gated memo the map renders."""
        return self._scope_kg_counts(
            db, notebook_ids, ceiling_binds=ceiling_binds,
        )

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
        self,
        db: object,
        notebook_ids: Sequence[str],
        active_notebook_id: str,
        *,
        ceiling_binds: bool = True,
    ) -> Tuple[Tuple[ElementKindCount, ...], int, int]:
        """Element counts AND the user-visible source count, in one pass.

        Both answers come out of the same ``source_change_signal_rows`` read, so
        they are computed together rather than by two loops: the signal query is
        this module's only unconditional query and it returns one row per source,
        so calling it twice per notebook would double the map's floor cost on a
        50 000-source base for nothing.  Peak memory is unchanged — one
        notebook's signal list at a time, exactly as before.

        The active notebook's own share of that source count comes out of the
        SAME loop iteration (third return value) rather than from a second
        counting road: it is the one number an active-notebook-local channel may
        judge itself by (see ``CollectionMap.active_sources``), and computing it
        anywhere else would be a second definition of "user-visible source".
        """
        totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        source_totals: Dict[str, int] = {kind: 0 for kind in ENUMERABLE_ELEMENT_KINDS}
        visible_sources = 0
        active_visible_sources = 0
        for notebook_id in notebook_ids:
            signals = list(self._sources.source_change_signal_rows(db, notebook_id))
            kept, excluded = self._ceiling_split(
                db, notebook_id, signals, ceiling_binds=ceiling_binds,
            )
            counts = (
                self._ceiling_element_counts(db, notebook_id, signals, kept, excluded)
                if excluded
                else self._notebook_element_counts(db, notebook_id, signals)
            )
            for item in counts:
                totals[item.kind] += item.count
                source_totals[item.kind] += item.sources
            # 计数只要个数,不要顺序:排序留给真的要遍历那份清单的调用方
            # (`scope_source_plan`),否则 5 万源的库会为了一个 len() 排一遍。
            notebook_visible = len(self._visible_signal_rows(kept))
            visible_sources += notebook_visible
            if notebook_id == active_notebook_id:
                active_visible_sources += notebook_visible
        elements = tuple(
            ElementKindCount(
                kind=kind, count=totals[kind], sources=source_totals[kind]
            )
            for kind in ENUMERABLE_ELEMENT_KINDS
        )
        return elements, visible_sources, active_visible_sources

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

    def _ceiling_element_counts(
        self,
        db: object,
        notebook_id: str,
        signals: Sequence[Tuple[str, ...]],
        kept: Sequence[Tuple[str, ...]],
        excluded: Sequence[Tuple[str, ...]],
    ) -> Tuple[ElementKindCount, ...]:
        """Per-kind totals over the IN-CEILING sources only.

        The notebook-level L2 figure counts every source, so it cannot be
        served as-is once the ceiling leaves one out.  The answer is summed
        from the per-kind L4 plans instead (``_notebook_plan_sources``): they
        are scope-independent, memoized on the notebook's whole signal list,
        and already carry each source's count — so a warm build issues NO
        per-source statement whatever the ceiling's size, and it cannot sweep
        the L1 LRU (reading per-source counts for a 24,500-source ceiling on
        every build would evict every other library's entries).  It is also
        literally the plan the executor walks, filtered the same way
        (``scope_element_plan``), so the map's figure and the plan's ``total``
        stay one definition.  L2 is neither read for a ceiling nor written: it
        stays the scope-independent whole-notebook memo.  ``kept`` is unused
        on purpose: the excluded half is the one to test against, exactly as
        ``scope_element_plan`` does.
        """
        dropped = {row[0] for row in excluded}
        result = []
        for kind in ENUMERABLE_ELEMENT_KINDS:
            count = sources = 0
            for entry in self._notebook_plan_sources(
                db, notebook_id, kind, signals
            ):
                if entry.source_id in dropped:
                    continue
                count += entry.count
                sources += 1
            result.append(ElementKindCount(kind=kind, count=count, sources=sources))
        return tuple(result)

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
        self,
        db: object,
        notebook_ids: Sequence[str],
        *,
        ceiling_binds: bool = True,
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
            for object_type, count in self._notebook_kg_counts(
                db, notebook_id, ceiling_binds=ceiling_binds,
            ).items():
                if object_type in totals:
                    totals[object_type] += count
        return tuple(
            (object_type, totals[object_type])
            for object_type in ENUMERABLE_KG_OBJECT_TYPES
        )

    def _notebook_kg_counts(
        self, db: object, notebook_id: str, *, ceiling_binds: bool = True,
    ) -> Dict[str, int]:
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

        Under a source ceiling the number is a different one — objects
        SUPPORTED by an in-ceiling source (``_ceiling_kg_counts``) — memoized
        under its own key (the ceiling's digest), never under the unscoped
        entry.
        """
        ceiling = self.source_ceiling(
            db, notebook_id, ceiling_binds=ceiling_binds,
        )
        row = self._unified_kg.graph_seq_row(db, notebook_id)
        version = (int(row[3]), int(row[0]))
        key = (notebook_id, "" if ceiling is None else ceiling.digest)
        with self._lock:
            cached = self._kg_counts.get(key)
            if cached is not None and cached[0] == version:
                self._kg_counts.move_to_end(key)
                return cached[1]

        # Query first, cache after — same rule as the element path.
        counts = (
            self._unscoped_kg_counts(db, notebook_id) if ceiling is None
            else self._ceiling_kg_counts(db, notebook_id, ceiling)
        )
        with self._lock:
            self._kg_counts[key] = (version, counts)
            self._kg_counts.move_to_end(key)
            while len(self._kg_counts) > _MAX_CACHED_NOTEBOOKS:
                self._kg_counts.popitem(last=False)
        return counts

    def _ceiling_kg_counts(
        self, db: object, notebook_id: str, ceiling: SourceCeiling
    ) -> Dict[str, int]:
        """Per-type usable object counts under a source ceiling.

        The listable predicate the executor pages with, in SQL: usable status,
        at least one evidence item from an in-ceiling source
        (``supported_by_source_ids`` — the exact predicate of
        ``knowledge_object_page_rows(allowed_source_ids=...)``), and not owned
        by a private Memory source (``excluding_owner_source_ids`` — the
        executor's own row drop).  One ``count_knowledge`` per enumerable type
        (four index-assisted counts on a miss, memoized on the seq and the
        ceiling digest).  A deny-all ceiling costs no query at all.
        """
        if not ceiling.members:
            return {object_type: 0 for object_type in ENUMERABLE_KG_OBJECT_TYPES}
        memory_ids = tuple(self._sources.memory_source_ids(db, notebook_id))
        # ``members`` — the run's ONE frozenset — not the sorted tuple: the
        # store's bound form is cached by that object's identity, so every
        # count and page of the run shares one binding (codex #817 r1).
        return {
            object_type: int(self._knowledge.count_knowledge(
                db, notebook_id, object_type, USABLE_STATUSES,
                supported_by_source_ids=ceiling.members,
                excluding_owner_source_ids=memory_ids,
            ))
            for object_type in ENUMERABLE_KG_OBJECT_TYPES
        }

    def _unscoped_kg_counts(self, db: object, notebook_id: str) -> Dict[str, int]:
        """The historical (no-ceiling) count: every usable object of the
        notebook minus the ones a private Memory owns."""
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
        return counts

    # ----------------------------------------------------------------- knowhow
    def _reachable_knowhow_tables(
        self,
        db: object,
        notebook_ids: Sequence[str],
        active_notebook_id: str,
        reachable: bool,
    ) -> int:
        """How many Knowhow tables THIS RUN's Knowhow enumeration can reach —
        one index count over ``idx_knowhow_tables_nb``, on the caller's
        connection, or zero queries when it can reach none.

        Reach, not presence.  The Knowhow executor lists the ACTIVE notebook's
        tables only, and not at all on a subjectless run or one whose source
        scope is narrowed or drifted (``reachable`` — the run's verdict, see
        ``knowhow_enumeration_reachable``).  This used to sum every
        participant's tables, so a mounted library's tables — and, in a global
        run, every selected library's — were advertised on the map although
        nothing could list them; the model then spent an action discovering
        that.  The active notebook must also still be in the checked scope
        (``notebook_ids``), which it always is today; the guard keeps the count
        honest if that ever stops being true.

        Deliberately NOT ``knowhow_enumeration_catalog``: that one also runs an
        aggregate CTE with a per-table row COUNT and change-log MAX and is not
        memoized anywhere, which is a lot of work for a number we do not use.
        ``list_knowhow_tables`` is worse still (it hydrates projection health).
        Reusing the generic ``count_rows`` primitive keeps this at one bounded
        index count, cheap enough that it needs no cache of its own.
        """
        if not reachable:
            return 0
        if active_notebook_id not in notebook_ids:
            return 0
        return self._queries.count_rows(
            db, "knowhow_tables", "notebook_id", active_notebook_id
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
