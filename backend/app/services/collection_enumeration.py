"""Typed-collection enumeration: the *清单层* under the collection map.

Design doc ``docs/reasoning-enumeration-tools-design.md`` §2.3.  The map
(``app.services.collection_catalog``) answers "how many formulas are in
scope?"; this module answers "which ones", as an ordered, bounded, cursor-paged
list with an explicit coverage proof.  Both layers are zero-LLM: the model
decides *whether* to enumerate, the executor decides nothing at all.

Why a separate executor rather than a wider relevance top-N: a ranked search
can return the best formulas but can never prove it returned every formula.
Completeness here is a property of a cursor running out over a scope that did
not move, and it is computed — never asserted by a model, never inferred from
"the page came back short".

Four contracts this module owns:

1. **Coverage is a fact.**  ``complete=True`` requires that the traversal
   walked off the end of the plan, that the scope identity taken before the
   first page equals the one taken after the last, AND — across a resumed
   chain — that everything returned adds up to the known total.  Anything else
   is ``complete=False`` with a ``truncated_reason`` and the shared
   ``EXPLICIT_PARTIAL_OVERFLOW`` semantics; the caller must never render a
   truncated list as "all".
2. **A cursor carries the scope it was cut from.**  Between two calls of one
   run there is an LLM reflection — seconds during which sources can appear,
   be reparsed or be unmounted.  A cursor therefore carries the opening scope
   identity and the running returned count, and a resumed call that opens on a
   different scope stops immediately with ``concurrent_change``.  It never
   silently restarts, and it never lets a chain that skipped a newly inserted
   source call itself complete.
3. **The source set is the map's source set.**  Element traversal picks its
   sources exclusively from ``CollectionCatalogService.scope_element_plan``,
   the same per-source counts the map sums — including Memory-derived and
   Knowhow-projection synthetic sources.  Were the two to drift, the UI would
   show "map: 12 / list: 8" with nothing able to explain the gap.  The one
   exception is an explicitly named ``source_id``, which is queried directly:
   absence from the plan means "the map counted zero", and inferring an empty
   answer from a cached zero would hide the rows of a source parsed seconds
   ago (see the under-count window registered on ``_coverage``).
4. **The KG status predicate is the counting predicate.**  Both sides pass the
   very same ``USABLE_STATUSES`` object (defined once in
   ``app.services.knowledge_contracts``) into SQL, so a deprecated object can
   never be counted-but-not-listed.

Cost shape per action, all index-assisted and bounded by the budget:

  * elements — 1 signal query per participant (plus the map's batched
    per-source count only when the plan memo misses), ONE label query per
    window of up to ``max_rows`` sources, then one page query per visited
    source per page.  Sources with zero items of the kind are never visited;
  * KG objects — 1 O(1) ``kg_mutation_seq`` read per participant plus the
    memoized per-type counts, then one page query per page;
  * closing check — 1 signal query per participant (elements) or 1 seq read
    per participant (KG).

Concurrency shape, for callers that share a connection pool: one action holds
ONE connection for its whole walk.  On SQLite that is the thread's reused
connection in autocommit, so no read transaction is parked in front of the
write lock; on PostgreSQL ``connect()`` checks out one pooled connection and
the walk runs inside its transaction until the closing check, so a long
enumeration occupies a pool slot for its duration.  That is why the budget
ceilings are hard and why nothing here waits on a model.

Deliberately NOT here: relevance scoring, deduplication ("how many *kinds* of
X" stays on the documented fallback contract), and any hydration of evidence
text or unbounded element markup.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.ask_retrieval_policy import EXPLICIT_PARTIAL_OVERFLOW
from app.repositories.ports import (
    KnowledgeStorePort,
    NotebookStorePort,
    RepositoryDatabasePort,
    SourceStorePort,
    UnifiedKgStorePort,
)
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
    CollectionCatalogService,
    ScopeElementPlan,
    ScopeSource,
)
from app.services.knowledge_contracts import USABLE_STATUSES


# ``truncated_reason`` vocabulary — exactly the three the design doc fixes.
# "budget" covers both row and page ceilings on purpose: they are the same
# statement to a reader ("the action ran out of its allowance"), and splitting
# them would put a knob name into a protocol field.
TRUNCATED_BUDGET = "budget"
TRUNCATED_PAYLOAD = "payload"
TRUNCATED_CONCURRENT_CHANGE = "concurrent_change"

# Per-item excerpt default, mirroring ``AskRetrievalLimits.cell_excerpt_chars``
# (1 000 across all five efforts today).  It is a *budget field* rather than an
# import because it is a per-effort field, not a module constant: reading the
# effort table here would couple the executor to the very knob table the
# caller is supposed to own.  ``test_collection_enumeration`` pins this default
# against all five profiles, so a future divergence fails loudly instead of
# silently truncating at the wrong width.
DEFAULT_EXCERPT_CHARS = 1_000

# How many evidence element ids one enumerated KG object may carry.  Evidence
# is a reference for the UI to resolve on demand, not payload: hydrating the
# quoted spans of every listed object would multiply the response by the size
# of the corpus it points at.
# Mirrored (as a literal, not an import — app.models must not import
# app.services) by app.models.ask.TypedCollectionItem.evidence_element_ids's
# max_length=3; kept in sync by
# test_typed_collection_result_sets.py::test_max_evidence_refs_parity_between_executor_and_wire_model.
MAX_EVIDENCE_REFS = 3

# Upper bound on one batched label lookup.  A walk can visit at most
# ``max_rows`` sources (every visited source yields at least one row, except
# the rare stale-count source that yields none), so the window follows
# ``max_rows`` and this constant only stops a very large row budget from
# turning the label query into a thousand-id IN list.
_MAX_TITLE_WINDOW = 256


@dataclass(frozen=True)
class EnumerationBudget:
    """One action's hard ceilings.  Supplied by the caller (T4 maps the
    effort profile onto it); this module never reads the effort table.

    ``page_size`` is a transport batch, not an answer top-N — the same
    distinction ``structured_page_size`` draws for Knowhow.  ``max_rows`` is
    the primary ceiling.  ``max_pages`` bounds only the EXTRA round trips: the
    first page of each source (or, for KG objects, of each participant) is
    free, because charging it would make completeness unreachable for the
    ordinary shape of a real corpus — one formula each across a hundred
    sources would exhaust a 4-page allowance after 4% of a 100-row budget.
    ``max_payload_chars`` bounds the serialized result the way
    ``structured_payload_chars`` bounds a Knowhow batch.

    All four are validated at construction: a non-positive ceiling is a caller
    bug, and answering it with an empty "partial" result would be
    indistinguishable from a real truncation.  A caller whose run budget is
    exhausted must skip the action, not request zero rows.
    """

    page_size: int
    max_rows: int
    max_pages: int
    max_payload_chars: int
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS

    def __post_init__(self) -> None:
        for name in ("page_size", "max_rows", "max_pages", "max_payload_chars"):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"enumeration budget {name} must be positive, "
                    f"got {getattr(self, name)!r}"
                )
        if int(self.excerpt_chars) < 0:
            raise ValueError("enumeration budget excerpt_chars must not be negative")


@dataclass(frozen=True)
class ElementCursor:
    """Resume handle for element enumeration.

    Position: ``(source_id, created_at, element_id)`` is the last item
    CONSUMED, and ``created_at is None`` means "nothing consumed in this source
    yet" — the spelling that lets a payload ceiling hit on a source's very
    first row still hand back a usable cursor.

    ``created_at`` holds the value the STORE returned (SQLite text /
    PostgreSQL ``datetime``) and is passed back unparsed, so a page boundary
    can never skip or repeat a row through a timestamp round trip.  That makes
    the cursor process-local by contract: it is a run-scoped resume handle, it
    is never persisted, never serialized into a response, and never shown to
    the model.

    Identity: ``kind`` plus the opening ``scope_notebook_ids`` /
    ``scope_fingerprint`` make the cursor answerable about the world it was cut
    from.  Between two calls sits an LLM round trip; without carrying the
    identity, a source inserted in that window would simply not exist for the
    rest of the chain and the final call would report a confident, wrong
    "complete".  ``returned_before`` carries the chain's running total so the
    denominator check survives resumption.
    """

    kind: str
    notebook_id: str
    source_id: str
    created_at: Any
    element_id: str
    scope_notebook_ids: Tuple[str, ...]
    scope_fingerprint: str
    returned_before: int


@dataclass(frozen=True)
class KgObjectCursor:
    """Resume handle for KG object enumeration (see ``ElementCursor``).

    ``scope_seqs`` is the opening ``(notebook_id, kg_mutation_seq)`` vector,
    which pins the participant list and each participant's graph generation at
    once — the notebooks already walked past are exactly the ones a
    position-only cursor could never re-check.
    """

    object_type: str
    notebook_id: str
    created_at: Any
    object_id: str
    scope_seqs: Tuple[Tuple[str, int], ...]
    returned_before: int


@dataclass(frozen=True)
class EnumerationCoverage:
    """What the action did and did not cover.  Structured, so the badge in the
    UI and the coverage header in the prompt both read the same fact.

    ``total`` is the map's number for this collection and scope, or ``None``
    when it could not be established — ``None`` means "unknown denominator",
    NOT zero, and a renderer must say so rather than print "N/0".
    ``returned`` and ``scanned`` are per CALL; ``returned_total`` adds
    everything the cursor chain returned before it, and is the number to show
    against ``total``.  ``scanned`` exceeds ``returned`` only when the payload
    ceiling stopped emission mid-page.
    """

    returned: int
    returned_total: int
    scanned: int
    total: Optional[int]
    has_more: bool
    complete: bool
    truncated_reason: str
    overflow_semantics: str


@dataclass(frozen=True)
class ElementItem:
    """One enumerated source element.

    ``text`` is excerpt-truncated and ``asset_id`` is a short reference for an
    image; the authoritative element stays addressable by ``element_id``.

    Decision (spec §2.6): the item carries a BOUNDED projection of the
    element's metadata and nothing else.  An image's ``asset_id`` is a short
    id, so it travels.  A table's rendered HTML does not: it is unbounded, the
    payload rail cannot police it, and truncating markup produces broken
    fragments rather than a smaller table.  The result card renders a table
    from the text excerpt and links out to the source for the full one.
    """

    element_id: str
    source_id: str
    source_title: str
    element_type: str
    location_label: str
    text: str
    asset_id: str
    notebook_id: str
    tier: str


@dataclass(frozen=True)
class KgObjectItem:
    """One enumerated knowledge object with bounded evidence references."""

    object_id: str
    object_type: str
    name: str
    section_path: str
    notebook_id: str
    tier: str
    evidence_element_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ElementEnumeration:
    kind: str
    items: Tuple[ElementItem, ...]
    coverage: EnumerationCoverage
    cursor: Optional[ElementCursor]
    # Round trips this call spent BEYOND the first page of each visited source
    # — exactly what ``EnumerationBudget.max_pages`` bounds (see ``_Walk.pages``).
    # It is cost accounting for a caller that pools a page allowance across
    # several actions, and it is deliberately NOT part of ``coverage``: coverage
    # is the user-facing proof of what was and was not listed, and a knob's
    # consumption is not evidence about the collection.  No default on purpose:
    # a silently-zero page charge is an under-charge, which is the precise
    # failure this field exists to prevent.
    extra_pages: int


@dataclass(frozen=True)
class KgObjectEnumeration:
    object_type: str
    items: Tuple[KgObjectItem, ...]
    coverage: EnumerationCoverage
    cursor: Optional[KgObjectCursor]
    extra_pages: int        # see ``ElementEnumeration.extra_pages``


def _payload_chars(item: object) -> int:
    """Serialized size of one item, measured the way it will travel.

    Same technique as ``structured_retrieval``: a compact JSON dump of the
    actual item, so the character rail applies to the real payload rather than
    to the text field alone.
    """
    return len(json.dumps(
        asdict(item), ensure_ascii=False, separators=(",", ":"), default=str
    ))


def _display_title(row: Mapping[str, Any]) -> str:
    """A grounded paper title beats an upload name; everything else keeps its
    ordinary source title (then file name).

    The citation-side twin of this rule is
    ``EvidenceContextService.citation_titles``; both exist because the
    enumeration list and the answer's ``[k]`` citations must name the same
    source the same way.  Deliberately not refactored into a shared helper in
    this task: that would drag the evidence/citation hydration path into a
    change that has no other reason to touch it.
    """
    paper_title = str(row["paper_title"] or "").strip()
    ordinary = str(row["title"] or row["file_name"] or "").strip()
    return paper_title if row["is_paper"] and paper_title else ordinary


def _row_get(row: Any, key: str) -> Any:
    """Read one column from a backend row (sqlite3.Row / psycopg dict row)."""
    return row[key]


class _Stop(Exception):
    """Internal: a budget ceiling fired mid-traversal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Walk:
    """Shared budget accounting for one enumeration action.

    Kept as one object because every ceiling has to be checked in the same
    order on both traversals; two hand-rolled copies of "have we run out yet?"
    is how one collection ends up reporting complete where the other reports
    partial for the same shape of truncation.
    """

    def __init__(self, budget: EnumerationBudget, returned_before: int = 0) -> None:
        self.budget = budget
        self.returned = 0
        self.returned_before = max(0, int(returned_before))
        self.scanned = 0
        self.pages = 0          # EXTRA round trips only; see emit_allowance
        self.payload = 0
        self.reason = ""

    @property
    def returned_total(self) -> int:
        return self.returned_before + self.returned

    # ------------------------------------------------------------- ceilings
    def emit_allowance(self, *, first_page: bool) -> int:
        """How many rows the next page may still emit.

        Raises when a ceiling has already fired.  The page query then asks for
        one row MORE than this: that lookahead row is never emitted and never
        counted, it only distinguishes "the collection ended here" from "the
        budget ended here".  Without it, a collection whose size is exactly
        ``max_rows`` (or an exact multiple of ``page_size``) would report a
        false partial — the single most damaging error this module can make,
        because it turns a complete answer into a hedged one.  It also removes
        the alternative fix, which is to keep paging until a short page comes
        back: that spends one extra query per exactly-full source, on every
        source, forever.

        ``first_page`` pages are not charged against ``max_pages``.  A page
        allowance is there to bound extra round trips, and the first read of a
        source is not extra — it is the only way to see the source at all.
        Charging it would make "one formula each in a hundred sources" — the
        ordinary shape of a real corpus — unenumerable at every effort level,
        while the row budget already bounds how many sources can be visited.
        """
        if not first_page and self.pages >= self.budget.max_pages:
            raise _Stop(TRUNCATED_BUDGET)
        remaining = self.budget.max_rows - self.returned
        if remaining <= 0:
            raise _Stop(TRUNCATED_BUDGET)
        return max(1, min(int(self.budget.page_size), remaining))

    def take_page(
        self, rows: Sequence[Any], allowance: int, *, first_page: bool
    ) -> Tuple[List[Any], bool]:
        """Split a fetched page into emittable rows and the lookahead answer."""
        if not first_page:
            self.pages += 1
        usable = list(rows[:allowance])
        self.scanned += len(usable)
        return usable, len(rows) > allowance

    def admit(self, item: object) -> None:
        """Charge one item against the row and payload ceilings."""
        chars = _payload_chars(item)
        if self.payload + chars > self.budget.max_payload_chars:
            raise _Stop(TRUNCATED_PAYLOAD)
        self.payload += chars
        self.returned += 1


def _coverage(
    walk: _Walk, *, total: Optional[int], exhausted: bool, scope_stable: bool
) -> EnumerationCoverage:
    """Turn the walk's account into the coverage proof.

    ``complete`` is a conjunction on purpose.  Cursor exhaustion alone proves
    only that the traversal ran out of rows *it could see*; a source added or
    reparsed underneath it would be silently missing.  A stable scope identity
    alone proves only that nothing moved, not that everything was read.

    The denominator check (exhausted, stable, yet ``returned_total != total``)
    is the third leg, and it counts the WHOLE cursor chain — a resumed call
    returns only its own tail, so comparing one call's ``returned`` against the
    collection's size would condemn every continuation.

    Registered asymmetry, because this check quietly promotes a cache into a
    completeness assertion: the map's per-source counts can under-report during
    the first-parse window (elements land while ``chunked_at`` is still NULL
    and ``updated_at`` has not moved yet — see
    ``SourceStore.source_change_signal_rows``).  In that window the source is
    counted as zero, is therefore not in the plan, is not walked, and both
    numbers agree at zero: the list stays consistent with the map, and the
    error is an under-count that heals at the next status write.  What the
    check does catch is the reverse — a plan that promised rows the walk could
    not find — and that is reported as ``concurrent_change`` rather than
    swallowed, because answering "complete" on a denominator that disagrees
    with the list is exactly the false "all" this module exists to prevent.
    """
    reason = walk.reason
    if (
        exhausted
        and scope_stable
        and total is not None
        and walk.returned_total != total
    ):
        reason = reason or TRUNCATED_CONCURRENT_CHANGE
    if not scope_stable:
        reason = reason or TRUNCATED_CONCURRENT_CHANGE
    complete = exhausted and scope_stable and not reason
    return EnumerationCoverage(
        returned=walk.returned,
        returned_total=walk.returned_total,
        scanned=walk.scanned,
        total=total,
        has_more=not exhausted,
        complete=complete,
        truncated_reason=reason,
        overflow_semantics="" if complete else EXPLICIT_PARTIAL_OVERFLOW,
    )


@dataclass
class _TitleWindow:
    """Batched source labels for the sources a walk may actually visit.

    Loads a window at a time instead of the whole plan: a base can hold tens of
    thousands of sources with formulas while the budget lets the walk touch
    ten, and it loads a window at all instead of one label per source because
    the ordinary walk then costs exactly one label query.
    """

    sources: Sequence[ScopeSource]
    load: Callable[[Sequence[str]], Dict[str, Any]]
    window: int
    titles: Dict[str, str] = field(default_factory=dict)
    loaded_until: int = 0

    def title_for(self, index: int) -> str:
        if index >= self.loaded_until:
            end = min(len(self.sources), index + max(1, self.window))
            rows = self.load(
                [entry.source_id for entry in self.sources[index:end]]
            )
            for candidate_id, row in rows.items():
                self.titles[candidate_id] = _display_title(row)
            self.loaded_until = end
        # A source whose row vanished between the plan and this lookup has no
        # label and is NOT re-queried: the window advanced past it, and an
        # empty label is a display detail, not a reason to spend a query per
        # missing source.
        return self.titles.get(self.sources[index].source_id, "")


class CollectionEnumerationService:
    """Enumerates one typed collection over one active notebook's scope.

    Scope resolution is ``NotebookStore.participant_tiers`` — the participant
    ids are the same list ``participant_ids`` returns (both go through
    ``resolve_participants``, whose validity predicate lives once in
    ``mount_sql.py``), with the tiers the items carry for free.  A base that
    was mounted but has since been downgraded drops out of retrieval and must
    drop out of enumeration with it.

    Every read of one call runs on ONE connection, and one connection is not
    one snapshot.  That is deliberate: holding a read transaction across an
    action that can legitimately last many pages would park a SQLite reader in
    front of the write lock for the whole time.  The identity checks are what
    make it safe, by refusing to *claim* stability rather than by enforcing it.
    """

    def __init__(
        self,
        *,
        database: RepositoryDatabasePort,
        catalog: CollectionCatalogService,
        sources: SourceStorePort,
        notebooks: NotebookStorePort,
        knowledge: KnowledgeStorePort,
        unified_kg: UnifiedKgStorePort,
    ) -> None:
        self._database = database
        self._catalog = catalog
        self._sources = sources
        self._notebooks = notebooks
        self._knowledge = knowledge
        self._unified_kg = unified_kg

    # ------------------------------------------------------------- elements
    def enumerate_elements(
        self,
        active_notebook_id: str,
        kind: str,
        *,
        source_id: str = "",
        budget: EnumerationBudget,
        cursor: Optional[ElementCursor] = None,
        cancel_event: CancelEvent = None,
    ) -> ElementEnumeration:
        """List source elements of one whitelisted kind across the scope.

        ``kind`` must be in ``ENUMERABLE_ELEMENT_KINDS`` — imported from the
        catalog, never re-spelled here — and an unknown kind raises
        ``ValueError`` rather than returning an empty list: "no formulas
        exist" and "you asked for a collection that is not enumerable" are
        different answers, and only the caller knows whether the run should
        fail open on the second.  A cursor cut for a different kind raises for
        the same reason.

        ``source_id`` narrows the traversal to a single source, which must
        belong to a participant notebook (one primary-key check) and is then
        paged DIRECTLY.  Its absence from the map's plan is not read as "it has
        none": the plan's counts are a cache, and a source parsed moments ago
        can hold rows the cache still reports as zero.
        """
        if kind not in ENUMERABLE_ELEMENT_KINDS:
            raise ValueError(f"unknown enumerable element kind: {kind!r}")
        if cursor is not None and cursor.kind != kind:
            raise ValueError(
                f"cursor belongs to kind {cursor.kind!r}, not {kind!r}"
            )
        raise_if_cancelled(cancel_event)
        walk = _Walk(budget, cursor.returned_before if cursor else 0)
        items: List[ElementItem] = []
        resume: Optional[ElementCursor] = None
        exhausted = True

        with self._database.connect() as db:
            notebook_ids, tiers = self._notebooks.participant_tiers(
                db, active_notebook_id
            )
            plan = self._catalog.scope_element_plan(db, notebook_ids, kind)
            sources = plan.sources
            total: Optional[int] = plan.total
            preloaded: Dict[str, Any] = {}
            if source_id:
                sources, total, preloaded = self._explicit_source_plan(
                    db, notebook_ids, plan, source_id
                )

            scope_id = (tuple(notebook_ids), plan.fingerprint)
            if cursor is not None:
                if (cursor.scope_notebook_ids, cursor.scope_fingerprint) != scope_id:
                    return self._element_scope_moved(kind, walk, total)
                sources, start_after = _resume_sources(sources, cursor)
                if sources is None:
                    return self._element_scope_moved(kind, walk, total)
            else:
                start_after = None

            titles = _TitleWindow(
                sources=sources,
                load=lambda ids: self._source_display(db, ids),
                window=min(_MAX_TITLE_WINDOW, budget.max_rows),
                titles={
                    key: _display_title(row) for key, row in preloaded.items()
                },
                # The explicit-source path already paid for its label with the
                # scope check; do not query it a second time.
                loaded_until=len(sources) if preloaded else 0,
            )
            try:
                for index, entry in enumerate(sources):
                    raise_if_cancelled(cancel_event)
                    # Only the first source of a resumed call starts mid-way.
                    after = start_after
                    start_after = None
                    title = titles.title_for(index)
                    tier = str(tiers.get(entry.notebook_id, ""))
                    # Set BEFORE the first fetch: any ceiling that fires from
                    # here on must still hand back a resumable cursor, and the
                    # very first row of a source can be the one that overflows
                    # the payload rail.
                    resume = _element_cursor(kind, entry, after, scope_id, walk)
                    first_page = True
                    while True:
                        raise_if_cancelled(cancel_event)
                        allowance = walk.emit_allowance(first_page=first_page)
                        rows = self._sources.element_page_rows(
                            db, entry.source_id, kind, after, allowance + 1
                        )
                        page, lookahead = walk.take_page(
                            rows, allowance, first_page=first_page
                        )
                        first_page = False
                        for row in page:
                            item = ElementItem(
                                element_id=str(_row_get(row, "id")),
                                source_id=entry.source_id,
                                source_title=title,
                                element_type=str(_row_get(row, "element_type")),
                                location_label=str(_row_get(row, "location_label")),
                                text=str(_row_get(row, "text") or "")[
                                    : max(0, int(budget.excerpt_chars))
                                ],
                                asset_id=str(_row_get(row, "asset_id") or ""),
                                notebook_id=entry.notebook_id,
                                tier=tier,
                            )
                            walk.admit(item)
                            items.append(item)
                            after = (
                                _row_get(row, "created_at"), str(_row_get(row, "id"))
                            )
                            resume = _element_cursor(
                                kind, entry, after, scope_id, walk
                            )
                        # The lookahead row is the whole proof: we asked for
                        # allowance+1 and did not get it, so this source holds
                        # nothing past what was just emitted.
                        if not lookahead:
                            break
            except _Stop as stop:
                walk.reason = stop.reason
                exhausted = False

            scope_stable = (
                self._catalog.scope_signal_fingerprint(db, notebook_ids)
                == plan.fingerprint
            )

        coverage = _coverage(
            walk, total=total, exhausted=exhausted, scope_stable=scope_stable
        )
        return ElementEnumeration(
            kind=kind,
            items=tuple(items),
            coverage=coverage,
            cursor=_resumable(coverage, resume),
            extra_pages=walk.pages,
        )

    # ----------------------------------------------------------- KG objects
    def enumerate_kg_objects(
        self,
        active_notebook_id: str,
        object_type: str,
        *,
        budget: EnumerationBudget,
        cursor: Optional[KgObjectCursor] = None,
        cancel_event: CancelEvent = None,
    ) -> KgObjectEnumeration:
        """List usable knowledge objects of one type across the scope.

        The status predicate is ``USABLE_STATUSES`` — the same tuple the
        catalog's per-type counting passes into ``knowledge_type_count_rows``
        — and it is applied in SQL.  This is the whole reason the map and the
        list can be shown side by side: "concept 89" and a list of 89 come
        from one definition of usable, not two.
        """
        if object_type not in ENUMERABLE_KG_OBJECT_TYPES:
            raise ValueError(f"unknown enumerable object type: {object_type!r}")
        if cursor is not None and cursor.object_type != object_type:
            raise ValueError(
                f"cursor belongs to type {cursor.object_type!r}, not {object_type!r}"
            )
        raise_if_cancelled(cancel_event)
        walk = _Walk(budget, cursor.returned_before if cursor else 0)
        items: List[KgObjectItem] = []
        resume: Optional[KgObjectCursor] = None
        exhausted = True

        with self._database.connect() as db:
            notebook_ids, tiers = self._notebooks.participant_tiers(
                db, active_notebook_id
            )
            opening_seqs = self._kg_seqs(db, notebook_ids)
            total = self._kg_total(db, notebook_ids, object_type)
            walk_ids: Sequence[str] = notebook_ids
            after: Optional[Tuple[Any, str]] = None
            if cursor is not None:
                if (
                    cursor.scope_seqs != opening_seqs
                    or cursor.notebook_id not in notebook_ids
                ):
                    return KgObjectEnumeration(
                        object_type=object_type,
                        items=(),
                        coverage=_coverage(
                            walk, total=total, exhausted=False, scope_stable=False
                        ),
                        cursor=None,
                        extra_pages=walk.pages,
                    )
                walk_ids = notebook_ids[notebook_ids.index(cursor.notebook_id):]
                after = (
                    (cursor.created_at, cursor.object_id)
                    if cursor.created_at is not None else None
                )

            try:
                for notebook_id in walk_ids:
                    raise_if_cancelled(cancel_event)
                    tier = str(tiers.get(notebook_id, ""))
                    resume = _kg_cursor(
                        object_type, notebook_id, after, opening_seqs, walk
                    )
                    first_page = True
                    while True:
                        raise_if_cancelled(cancel_event)
                        allowance = walk.emit_allowance(first_page=first_page)
                        rows = self._knowledge.knowledge_object_page_rows(
                            db,
                            notebook_id,
                            object_type,
                            USABLE_STATUSES,
                            after,
                            allowance + 1,
                        )
                        page, lookahead = walk.take_page(
                            rows, allowance, first_page=first_page
                        )
                        first_page = False
                        for row in page:
                            payload = _json_object(_row_get(row, "payload"))
                            item = KgObjectItem(
                                object_id=str(_row_get(row, "id")),
                                object_type=str(_row_get(row, "object_type")),
                                name=str(payload.get("name") or "")[
                                    : max(0, int(budget.excerpt_chars))
                                ],
                                section_path=str(payload.get("section_path") or ""),
                                notebook_id=notebook_id,
                                tier=tier,
                                evidence_element_ids=_evidence_refs(
                                    _row_get(row, "evidence")
                                ),
                            )
                            walk.admit(item)
                            items.append(item)
                            after = (
                                _row_get(row, "created_at"), str(_row_get(row, "id"))
                            )
                            resume = _kg_cursor(
                                object_type, notebook_id, after, opening_seqs, walk
                            )
                        if not lookahead:
                            break
                    after = None        # each notebook restarts its own keyset
            except _Stop as stop:
                walk.reason = stop.reason
                exhausted = False

            scope_stable = self._kg_seqs(db, notebook_ids) == opening_seqs

        coverage = _coverage(
            walk, total=total, exhausted=exhausted, scope_stable=scope_stable
        )
        return KgObjectEnumeration(
            object_type=object_type,
            items=tuple(items),
            coverage=coverage,
            cursor=_resumable(coverage, resume),
            extra_pages=walk.pages,
        )

    # ---------------------------------------------------------------- reads
    def _explicit_source_plan(
        self,
        db: object,
        notebook_ids: Sequence[str],
        plan: ScopeElementPlan,
        source_id: str,
    ) -> Tuple[Tuple[ScopeSource, ...], Optional[int], Dict[str, Any]]:
        """Scope-check and plan a single explicitly requested source.

        One primary-key read proves both that the source exists and that it
        belongs to a participant notebook — an out-of-scope id raises rather
        than returning another notebook's rows, and rather than being answered
        with a silent empty list.

        The source is then walked whatever the map says about it.  Its
        ``total`` comes from the plan when the map has a count for it, and is
        ``None`` (unknown denominator) otherwise: inferring zero from "absent
        from the plan" would turn a just-parsed source into a confident empty
        answer.
        """
        rows = self._source_display(db, [source_id])
        row = rows.get(source_id)
        if row is None or str(row["notebook_id"]) not in set(notebook_ids):
            raise ValueError(f"source is not in scope: {source_id!r}")
        planned = next(
            (entry for entry in plan.sources if entry.source_id == source_id), None
        )
        entry = planned or ScopeSource(
            notebook_id=str(row["notebook_id"]), source_id=source_id, count=0
        )
        return (entry,), (planned.count if planned else None), rows

    def _element_scope_moved(
        self, kind: str, walk: _Walk, total: Optional[int]
    ) -> ElementEnumeration:
        """The scope this cursor was cut from is gone: stop, do not restart."""
        return ElementEnumeration(
            kind=kind,
            items=(),
            coverage=_coverage(
                walk, total=total, exhausted=False, scope_stable=False
            ),
            cursor=None,
            extra_pages=walk.pages,
        )

    def _source_display(
        self, db: object, source_ids: Sequence[str]
    ) -> Dict[str, Any]:
        return {
            str(row["id"]): row
            for row in self._sources.source_display_rows(db, list(source_ids))
        }

    def _kg_seqs(
        self, db: object, notebook_ids: Sequence[str]
    ) -> Tuple[Tuple[str, int], ...]:
        """The scope's ``kg_mutation_seq`` vector — one O(1) row per
        participant, and the KG side's answer to "did the scope move?".

        A vector rather than a sum: two participants whose seqs move in
        opposite directions must not cancel out, and the participant list
        itself has to be part of the identity.
        """
        return tuple(
            (notebook_id, int(self._unified_kg.graph_seq_row(db, notebook_id)[0]))
            for notebook_id in notebook_ids
        )

    def _kg_total(
        self, db: object, notebook_ids: Sequence[str], object_type: str
    ) -> Optional[int]:
        """The map's count for this type, or ``None`` when it is unavailable.

        Unlike the element side — where the per-source counts ARE the
        traversal plan, so a failure there is a failure of the enumeration —
        the KG total is only the denominator: the pages come straight from the
        keyset.  So a counting failure degrades this one field to "unknown"
        (the design doc's "取不到则省略") instead of failing an action that can
        still return a correct, honestly-partial list.  It is not silent:
        ``total=None`` is visible in the coverage the caller renders.
        """
        try:
            counts = dict(self._catalog.scope_kg_type_counts(db, notebook_ids))
        except AskCancelled:
            raise           # a cancelled run is not a missing denominator
        except Exception:
            return None
        if object_type not in counts:
            return None
        return int(counts[object_type])


def _element_cursor(
    kind: str,
    entry: ScopeSource,
    after: Optional[Tuple[Any, str]],
    scope_id: Tuple[Tuple[str, ...], str],
    walk: _Walk,
) -> ElementCursor:
    return ElementCursor(
        kind=kind,
        notebook_id=entry.notebook_id,
        source_id=entry.source_id,
        created_at=after[0] if after is not None else None,
        element_id=after[1] if after is not None else "",
        scope_notebook_ids=scope_id[0],
        scope_fingerprint=scope_id[1],
        returned_before=walk.returned_total,
    )


def _kg_cursor(
    object_type: str,
    notebook_id: str,
    after: Optional[Tuple[Any, str]],
    scope_seqs: Tuple[Tuple[str, int], ...],
    walk: _Walk,
) -> KgObjectCursor:
    return KgObjectCursor(
        object_type=object_type,
        notebook_id=notebook_id,
        created_at=after[0] if after is not None else None,
        object_id=after[1] if after is not None else "",
        scope_seqs=scope_seqs,
        returned_before=walk.returned_total,
    )


def _resumable(coverage: EnumerationCoverage, resume: Optional[Any]) -> Optional[Any]:
    """The cursor contract, in one place.

    ``complete=False`` implies a usable cursor — the caller must be able to
    tell "ran out of budget, ask again" from "cannot be continued" — and the
    sole exception is ``concurrent_change``, where continuing would mean
    resuming into a world that no longer matches the one already reported.
    """
    if coverage.complete:
        return None
    if coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE:
        return None
    return resume


def _resume_sources(
    sources: Sequence[ScopeSource], cursor: ElementCursor
) -> Tuple[Optional[Tuple[ScopeSource, ...]], Optional[Tuple[Any, str]]]:
    """Re-align a plan against a cursor from an earlier call.

    Resumption is by KEY, not by position: the plan is rebuilt on every call
    and a source added or removed in between would make an index-based resume
    skip or repeat a whole source.  Returns ``(None, None)`` when the cursor's
    source is no longer in the plan — that enumeration cannot be honestly
    continued, and the caller gets an explicit ``concurrent_change`` rather
    than a silently re-started walk.
    """
    for index, entry in enumerate(sources):
        if (
            entry.notebook_id == cursor.notebook_id
            and entry.source_id == cursor.source_id
        ):
            start = (
                (cursor.created_at, cursor.element_id)
                if cursor.created_at is not None else None
            )
            return tuple(sources[index:]), start
    return None, None


def _json_object(value: Any) -> Dict[str, Any]:
    """Decode a payload column that arrives as text (SQLite) or already
    decoded (PostgreSQL ``jsonb``)."""
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _evidence_refs(value: Any) -> Tuple[str, ...]:
    """Bounded, deduplicated element ids from an object's evidence column."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value or "[]")
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, Sequence):
        return ()
    out: List[str] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        element_id = str(entry.get("element_id") or "")
        if element_id and element_id not in out:
            out.append(element_id)
        if len(out) >= MAX_EVIDENCE_REFS:
            break
    return tuple(out)
