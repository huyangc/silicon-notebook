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

Five contracts this module owns:

1. **Coverage is a fact.**  ``complete=True`` requires that the traversal
   walked off the end of the plan, that the scope identity taken before the
   first page equals the one taken after the last, AND — across a resumed
   chain — that everything returned adds up to the known total.  That identity
   includes the PARTICIPANT SET, re-resolved at the close (see
   ``_closing_participants``): a reference library mounted or unmounted
   mid-walk changes what "the whole scope" means, and a fingerprint recomputed
   over the opening id list cannot see it.  Anything else
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
   the same per-source counts the map sums — including the Knowhow-projection
   synthetic source.  The sources collection does the same through
   ``scope_source_plan``, whose set is the USER-VISIBLE source list (the
   source tab's own predicate, so a hidden Knowhow projection row is in
   neither its count nor its list).  Were a plan and its count to drift, the UI
   would show "map: 12 / list: 8" with nothing able to explain the gap.  The
   one exception is an explicitly named ``source_id``, which is queried
   directly: absence from the plan means "the map counted zero", and a source
   the user named by hand is worth one index-seeked query rather than an answer
   derived from a cached zero.
4. **The KG listable predicate is the counting predicate.**  Both sides
   evaluate the very same ``USABLE_STATUSES`` object (defined once in
   ``app.services.knowledge_contracts``) and subtract/skip the very same
   ``memory_source_ids``, so an object can never be counted-but-not-listed.
   Both are evaluated HERE rather than in SQL because the keyset index carries
   neither ``status`` nor a source type (see ``_usable_kg_page``); the page
   query stays O(limit) and this module over-scans within an explicit ceiling
   instead.
5. **A listing never contains private Memory.**  A confirmed Memory belongs to
   one user; a typed-collection listing is scoped to a notebook's participants
   and has no owner filter of its own.  So the Memory synthetic source's
   elements and the knowledge objects extracted from it are outside every
   collection this module can enumerate, in a shared notebook and in a
   one-person notebook alike — one listing, one meaning.  The element side
   inherits this from the map (those sources are absent from
   ``source_change_signal_rows``, hence from every count AND every plan); the
   KG side filters rows in ``_usable_kg_page`` against the same id list the
   catalog subtracts from the denominator; the sources side never sees those
   rows at all, because the user-visible source predicate it inherits drops
   them before anything is listed or counted.

Cost shape per action, all index-assisted and bounded by the budget:

  * elements — 1 signal query per participant (plus the map's batched
    per-source count only when the plan memo misses), ONE label query per
    window of up to ``max_rows`` sources, then one page query per visited
    source per page.  Sources with zero items of the kind are never visited;
  * KG objects — 1 O(1) ``kg_mutation_seq`` read plus 1 bounded Memory-source
    id read per participant, plus the memoized per-type counts, then one page
    query per page, plus top-up queries when deprecated or Memory-derived
    objects are interleaved — bounded, per action, by
    ``max_rows × _KG_RAW_SCAN_FACTOR`` raw rows;
  * sources — 1 signal query plus 1 bounded hidden-id read per participant
    (that IS the plan; no query walks the collection), then one batched
    primary-key hydration per window of up to ``page_size`` documents;
  * closing check — 1 participant resolution, then 1 signal query per
    participant (elements, sources) or 1 seq read per participant (KG).

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

import hashlib
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
from app.services.extraction_profiles import PROFILES
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.source_display import source_display_title
from app.services.source_scope import scoped_participants


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

# Upper bound on the sources one title→id resolution may examine.  The plan it
# walks holds only sources that carry the requested kind, so this is already a
# small fraction of a big library; the cap exists so that a mounted base with
# tens of thousands of formula-bearing sources cannot turn one reflection into
# a full label sweep.  A plan LONGER than this is declined outright rather than
# resolved from its prefix: uniqueness is a property of the whole scope, and a
# prefix cannot witness it (see ``resolve_source_title``).  The caller skips the
# action and says so — never a silent whole-scope enumeration of the wrong
# thing, and never a confident pick between two same-titled documents.
_MAX_TITLE_RESOLVE_SOURCES = 4 * _MAX_TITLE_WINDOW

# How many RAW knowledge-object rows one KG action may read while filtering out
# unusable (deprecated / retired) objects, expressed as a multiple of that
# action's row budget.
#
# Why a multiple and not a constant: the ceiling has to scale with what the
# action was asked to produce, or the deepest effort level would truncate on
# the same absolute scan the shallowest one survives.
#
# Why 4: the page query is keyset-ordered on
# ``(notebook_id, object_type, created_at, id)`` and carries no status
# predicate, so a notebook where three of every four objects of a type have
# been deprecated still lists its full row budget without a single top-up
# beyond this ceiling.  Past that ratio the cost of finding the next usable row
# stops being proportional to the answer, and an honest ``budget`` partial is
# the right answer — the alternative (a status-aware index) would freeze the
# status vocabulary into the schema and cost a third migration bump in this
# change.  Deliberately NOT charged against ``max_pages``: that allowance
# bounds the round trips a caller pools across actions, and a top-up read is an
# artifact of this notebook's history, not of the caller's request.
_KG_RAW_SCAN_FACTOR = 4


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

    ``scope_seqs`` is the opening ``(notebook_id, kg_reset_epoch,
    kg_mutation_seq)`` vector (P2-2, post-review, batch-3-W1 PR-2 — widened
    from a bare ``kg_mutation_seq`` int), which pins the participant list and
    each participant's graph generation at once — the notebooks already
    walked past are exactly the ones a position-only cursor could never
    re-check.
    """

    object_type: str
    notebook_id: str
    created_at: Any
    object_id: str
    scope_seqs: Tuple[Tuple[str, int, int], ...]
    returned_before: int


@dataclass(frozen=True)
class SourceCursor:
    """Resume handle for source enumeration (see ``ElementCursor``).

    Position is ``(notebook_id, source_id)`` of the first source NOT yet listed
    — inclusive, unlike the element cursor's "last consumed" spelling.  One
    source is one row here, so "the next row" and "the next source" are the same
    thing, and pointing AT the unlisted source is what lets a payload ceiling
    that fires before the first item still hand back a usable cursor.

    Identity is the opening participant list plus the scope's signal
    fingerprint, exactly as on the element side: between two calls sits an LLM
    round trip, and a document added, deleted or unmounted in that window must
    turn the chain into ``concurrent_change`` rather than into a confident,
    wrong "complete".  ``returned_before`` carries the chain's running total so
    the denominator check survives resumption.

    ``emitted_meta`` is the chain's running ``(source_id, metadata digest)``
    ledger for every document already handed out — see
    ``_source_meta_digest`` for what it covers and why the scope fingerprint
    cannot.  It lives on the cursor because the executor is stateless between
    calls and the closing check has to verify the WHOLE chain, not just the last
    call's slice.  In-memory only (the run holds it in ``_EnumChain.cursor``; it
    is never serialized, persisted, or shown to the model) and bounded by the
    row pool, so a 600-document chain carries 600 short pairs.
    """

    notebook_id: str
    source_id: str
    scope_notebook_ids: Tuple[str, ...]
    scope_fingerprint: str
    returned_before: int
    emitted_meta: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EnumerationCoverage:
    """What the action did and did not cover.  Structured, so the badge in the
    UI and the coverage header in the prompt both read the same fact.

    ``total`` is the map's number for this collection and scope, or ``None``
    when it could not be established — ``None`` means "unknown denominator",
    NOT zero, and a renderer must say so rather than print "N/0".
    ``returned`` and ``scanned`` are per CALL; ``returned_total`` adds
    everything the cursor chain returned before it, and is the number to show
    against ``total``.  ``scanned`` counts the rows the traversal actually
    read, so it exceeds ``returned`` when the payload ceiling stopped emission
    mid-page and — on the KG side — by every unusable row the status filter
    dropped (that gap is the whole reason the over-scan ceiling exists).
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
class SourceItem:
    """One enumerated document of the scope's user-visible source list.

    The projection is deliberately the source CARD's, not the source's: display
    title (the frozen ``source_display.source_display_title`` rule — a grounded
    paper title beats an upload name, and citations name the same source the same
    way),
    the document type as the label the upload picker shows, and the summary the
    library already stored, excerpt-truncated.  Nothing here is derived from a
    model call, and no element text or file path travels: this is the answer to
    "which documents are in this library", and a per-document deep dive is a
    separate retrieval the model can ask for by title.
    """

    source_id: str
    source_title: str
    doc_type_label: str
    summary: str
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
    # Serialized characters this call's items actually cost, measured exactly
    # the way ``max_payload_chars`` bounds them.  Same role as ``extra_pages``
    # and same reasoning for keeping it out of ``coverage``: a run that pools
    # one documented payload allowance across several actions can only pass the
    # REMAINDER to the next action if each action reports what it spent.
    # Without it every action would receive a fresh full allowance and a deep
    # run would return several times the documented ceiling.
    payload_chars: int


@dataclass(frozen=True)
class KgObjectEnumeration:
    object_type: str
    items: Tuple[KgObjectItem, ...]
    coverage: EnumerationCoverage
    cursor: Optional[KgObjectCursor]
    extra_pages: int        # see ``ElementEnumeration.extra_pages``
    payload_chars: int      # see ``ElementEnumeration.payload_chars``


@dataclass(frozen=True)
class SourceEnumeration:
    """No ``kind``/``object_type``: the sources collection has no sub-type — the
    library's document list is one collection, whole."""

    items: Tuple[SourceItem, ...]
    coverage: EnumerationCoverage
    cursor: Optional[SourceCursor]
    extra_pages: int        # see ``ElementEnumeration.extra_pages``
    payload_chars: int      # see ``ElementEnumeration.payload_chars``


def _payload_chars(item: object) -> int:
    """Serialized size of one item, measured the way it will travel.

    Same technique as ``structured_retrieval``: a compact JSON dump of the
    actual item, so the character rail applies to the real payload rather than
    to the text field alone.
    """
    return len(json.dumps(
        asdict(item), ensure_ascii=False, separators=(",", ":"), default=str
    ))


def _source_meta_digest(display_title: str, doc_type_label: str) -> str:
    """Short digest of the two listed fields the CHANGE SIGNAL cannot see.

    The signal token is ``updated_at | parse_status | chunked_at`` on
    ``sources``, so it moves for element swaps, status transitions and reparses.
    Two of a listed document's three fields sit outside it:

    * the display title can come from ``source_paper_meta.paper_title``, and
      ``upsert_paper_meta`` writes that table (plus ``source_authors``) WITHOUT
      touching ``sources.updated_at`` — verified, not assumed.  So a paper-metadata
      backfill running while a roster is being paged changes what page 1 should
      have said, invisibly to the signal;
    * ``doc_type`` is a plain column a maintenance update can set on its own.

    ``summary`` is deliberately NOT in here, and that is not an oversight: its
    only writer is ``set_status``, which sets ``updated_at`` in the same
    statement, so a summary edit already moves the fingerprint and is already
    reported.  Hashing it too would only add false ``concurrent_change`` reports
    on routine re-summarization.

    A digest rather than the strings: the ledger rides on the cursor for the
    whole chain, and 600 titles is prose held in memory to answer a yes/no
    question.  8 bytes because it gates a freshness check inside one request,
    not a security boundary, and the values it separates are two short fields of
    the same row.
    """
    digest = hashlib.blake2b(digest_size=8)
    digest.update(str(display_title).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(doc_type_label).encode("utf-8"))
    return digest.hexdigest()


def _doc_type_label(value: Any) -> str:
    """The document type as the interface names it, or "" when it has none.

    ``PROFILES`` is the same registry the upload picker's ``GET /doc-types``
    renders, so a listed document says "学术论文" in the answer exactly as it does
    on its source card — one label table, no second translation on the frontend
    to drift out of sync.

    An unknown or empty ``doc_type`` yields "" rather than the raw column value:
    ``academic_paper`` is an internal id, and interface copy does not show
    internal ids.  Omitting the line is the honest fallback — a legacy row whose
    type was never detected genuinely has nothing to say here.
    """
    profile = PROFILES.get(str(value or ""))
    return str(profile.label) if profile is not None else ""
def _normalized_title(value: Any) -> str:
    """The comparison form for title→id resolution.

    Trim plus case fold, nothing else: the point is to survive a model copying
    a title with stray whitespace or different capitalisation, NOT to match
    approximately.  Normalising punctuation or dropping an extension would let
    two genuinely different documents collide, and the caller would enumerate
    one of them and call it complete.
    """
    return str(value or "").strip().casefold()


def _row_get(row: Any, key: str) -> Any:
    """Read one column from a backend row (sqlite3.Row / psycopg dict row)."""
    return row[key]


class EnumerationInvariantError(RuntimeError):
    """One action issued more page queries than its budget can account for.

    Not a user-facing condition and not a budget ceiling: the ceilings already
    stop a walk honestly (``_Stop`` → an ``explicit_partial`` result).  This is
    the executor telling on itself — the round-trip count is supposed to be
    bounded by the budget *by construction* (see ``_Walk.bound_queries``), so
    exceeding it means a traversal, a plan or a ceiling stopped behaving the
    way the cost argument assumes.  Raising is the point: a silent overrun is a
    library-sized query storm behind a hundred-row request, and it would look
    exactly like a slow day.

    The caller (``reasoning_retrieval``) treats it like any other executor
    failure — one skipped action, fail-open — so a breach costs a list, never
    a request.
    """


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
        # RAW rows read by a status-filtered traversal (KG only).  Tracked
        # apart from ``scanned`` because ``scanned`` is a reported fact and
        # this is a ceiling: see ``_KG_RAW_SCAN_FACTOR``.
        self.raw_scanned = 0
        # Page queries issued by this action, and the ceiling the traversal
        # proved before it started.  See ``bound_queries``.
        self.queries = 0
        self.query_limit = 0
        self.reason = ""

    @property
    def returned_total(self) -> int:
        return self.returned_before + self.returned

    @property
    def raw_scan_limit(self) -> int:
        return max(1, int(self.budget.max_rows)) * _KG_RAW_SCAN_FACTOR

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
        self.charge_page(first_page=first_page)
        usable = list(rows[:allowance])
        self.scanned += len(usable)
        return usable, len(rows) > allowance

    def charge_page(self, *, first_page: bool) -> None:
        """Charge one logical page against ``max_pages``.

        Split out of ``take_page`` because the KG traversal assembles one
        logical page out of several raw reads (``_usable_kg_page``) and must
        charge the page exactly once, not once per read.
        """
        if not first_page:
            self.pages += 1

    def bound_queries(self, limit: int) -> None:
        """Arm this action's page-query ceiling, once, before the traversal.

        The number of round trips one action can spend IS bounded by the
        budget, but only through an argument about the traversal rather than
        through any single counter — and an unstated bound is one refactor
        away from not holding.  Each traversal states its own bound here (the
        derivations live at the two call sites) and every page query is charged
        against it, so the property is checked instead of believed.

        Deliberately NOT the same thing as ``max_pages``.  That allowance
        bounds the EXTRA round trips a caller pools across actions and
        deliberately leaves each partition's first page free — charging first
        pages would make a wide-and-thin corpus (one formula each across a
        hundred sources, the ordinary shape) unenumerable at every effort
        level, which is a completeness regression this feature already had to
        fix once.  So the first pages stay free and are bounded HERE instead,
        by the row budget: a partition is only visited because the map counted
        rows in it, and a visited partition yields rows.  Two rails, two
        questions, both answered.
        """
        self.query_limit = max(1, int(limit))

    def charge_query(self) -> None:
        """Charge one page query BEFORE it is issued.

        Before, not after: the ceiling exists to stop the round trip, not to
        notice it afterwards.
        """
        self.queries += 1
        if self.query_limit and self.queries > self.query_limit:
            raise EnumerationInvariantError(
                "enumeration issued more page queries than its budget allows "
                f"({self.queries} > {self.query_limit})"
            )

    def scan_raw(self, count: int) -> bool:
        """Charge raw rows read by a status-filtered traversal.

        Returns whether the over-scan ceiling still has room.  It is a return
        value rather than a raise because the caller has to keep the rows it
        already gathered AND advance the keyset past the unusable ones before
        it stops — a raise here would either discard usable rows or leave a
        resumed chain re-reading the same deprecated prefix forever.
        """
        self.scanned += count
        self.raw_scanned += count
        return self.raw_scanned < self.raw_scan_limit

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

    This check promotes a cache into a completeness assertion, so the cache has
    to be exact rather than merely eventually right.  It is: ``replace_elements``
    advances the source's ``updated_at`` in the same write transaction as the
    new elements, so the change signal flips the instant they commit and a
    count taken against the previous generation can never be served again.
    (Earlier this held only for a REPARSE — a first parse landed elements
    without moving any component of the signal, and a map built before the
    following status write counted the source as zero.  That window is gone,
    not merely disclosed.)  What the denominator check catches is a plan that
    promised rows the walk could not find, and that is reported as
    ``concurrent_change`` rather than swallowed, because answering "complete"
    on a denominator that disagrees with the list is exactly the false "all"
    this module exists to prevent.
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
                self.titles[candidate_id] = source_display_title(row)
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

    That list then passes through ``scoped_participants`` — the run's
    reference-library checkboxes — at ``_participants`` and
    ``_closing_participants``, the only two places this service resolves it.
    Narrowing there rather than at each consumer is what keeps the plan, the
    walk, the denominator and the closing fingerprint derived from ONE list:
    they are not three filters that have to agree, they are one filtered list
    read four times.  ``resolve_participants`` itself is untouched — it is also
    the authorization predicate, and a retrieval checkbox must not narrow that.
    ``enumeration_active()`` is likewise untouched: the tool stays available,
    only its scope shrinks.

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
        none": one explicitly named source is worth an index-seeked query
        rather than an answer inferred from a cached zero.
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
            notebook_ids, tiers = self._participants(db, active_notebook_id)
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
                    key: source_display_title(row) for key, row in preloaded.items()
                },
                # The explicit-source path already paid for its label with the
                # scope check; do not query it a second time.
                loaded_until=len(sources) if preloaded else 0,
            )
            # Round-trip bound, proved from the two rails:
            #   * every page query beyond a source's first is charged to
            #     ``max_pages``  ⇒  at most ``max_pages`` of those;
            #   * a source is in the plan only because the map counted rows in
            #     it, and the walk visits sources in plan order  ⇒  each first
            #     page belongs to a source that yields a row, and rows are
            #     capped at ``max_rows``.
            # Hence ``max_rows + max_pages`` page queries, whatever the corpus
            # shape.  A stale plan entry (counted rows, holds none by the time
            # it is read) is the one way a first page can come back empty, and
            # the same concurrent write that caused it makes the closing scope
            # check report ``concurrent_change`` — the result is already known
            # bad, and a breach here degrades to one skipped action.
            walk.bound_queries(budget.max_rows + budget.max_pages)
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
                        walk.charge_query()
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

            closing_ids = self._closing_participants(db, active_notebook_id)
            scope_stable = (
                closing_ids == tuple(notebook_ids)
                and self._catalog.scope_signal_fingerprint(db, closing_ids)
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
            payload_chars=walk.payload,
        )

    # -------------------------------------------------- name → id resolution
    def resolve_source_title(
        self,
        active_notebook_id: str,
        kind: str,
        title: str,
        *,
        cancel_event: CancelEvent = None,
    ) -> Tuple[str, int, bool]:
        """Resolve a source TITLE to the id of the one source that bears it.

        The model never sees internal source ids — candidate summaries and
        citations carry titles — so "list every formula in <title>" can only be
        expressed by name.  Resolution is deterministic and server-side, and it
        is deliberately not a search: the comparison is exact after trimming
        and case folding, over the sources the map already plans to visit for
        this kind.  A fuzzy match would silently enumerate the wrong document
        and report it as complete.

        Returns ``(source_id, matches, truncated)``.  ``matches`` is 0 when
        nothing bears that title, 1 with the resolved id, and 2 when the title
        is ambiguous (the scan stops at the second hit — the caller only needs
        to know that it is not unique).  The caller decides what to do with
        anything other than exactly one; this method never guesses.

        Bounded by construction: it reuses ``scope_element_plan`` (which
        already holds only the sources that carry this kind) and reads their
        labels through the same batched ``source_display_rows`` window the walk
        uses.  When the plan is LONGER than ``_MAX_TITLE_RESOLVE_SOURCES`` the
        method refuses to answer at all — ``truncated=True``, no scan, no id.
        Scanning a prefix and reporting "exactly one match" would be a claim
        about the whole scope drawn from a part of it: a second source with the
        same title could sit anywhere past the cap, and the caller would then
        enumerate one of two same-titled documents and report it complete.
        Declining is cheap and honest; the alternative to both is a per-request
        label sweep of every source in a mounted base.
        """
        if kind not in ENUMERABLE_ELEMENT_KINDS:
            raise ValueError(f"unknown enumerable element kind: {kind!r}")
        wanted = _normalized_title(title)
        if not wanted:
            return "", 0, False
        raise_if_cancelled(cancel_event)
        found = ""
        matches = 0
        with self._database.connect() as db:
            notebook_ids, _tiers = self._participants(db, active_notebook_id)
            sources = self._catalog.scope_element_plan(
                db, notebook_ids, kind
            ).sources
            if len(sources) > _MAX_TITLE_RESOLVE_SOURCES:
                return "", 0, True
            for start in range(0, len(sources), _MAX_TITLE_WINDOW):
                raise_if_cancelled(cancel_event)
                window = sources[start:start + _MAX_TITLE_WINDOW]
                rows = self._source_display(
                    db, [entry.source_id for entry in window]
                )
                for entry in window:
                    row = rows.get(entry.source_id)
                    if row is None:
                        continue
                    if _normalized_title(source_display_title(row)) != wanted:
                        continue
                    matches += 1
                    if matches > 1:
                        return "", 2, False
                    found = entry.source_id
        return found, matches, False

    # --------------------------------------------------------------- sources
    def enumerate_sources(
        self,
        active_notebook_id: str,
        *,
        budget: EnumerationBudget,
        cursor: Optional[SourceCursor] = None,
        cancel_event: CancelEvent = None,
    ) -> SourceEnumeration:
        """List the scope's USER-VISIBLE documents, in the source tab's order.

        This is the collection that makes "分析当前 notebook 的文章" answerable as
        a per-document pass instead of as a relevance sample: the model gets the
        library's table of contents (title, type, stored summary) and can then
        ask for whatever document is worth a deeper look, by name.

        Three properties it inherits rather than re-implements:

        * **the set** is ``CollectionCatalogService.scope_source_plan``, i.e. the
          same list the map counts and the same predicate the source tab shows
          (Memory synthetic rows and Knowhow projection rows are in neither);
        * **the identity** is the scope's signal fingerprint plus the
          re-resolved participant set, so completeness is proved exactly the way
          the element side proves it;
        * **the ceilings** are the shared ``_Walk``: one source is one row, and
          the whole collection is ONE partition — its first hydration window is
          free (the "free first page per partition" rule this feature is built
          on) and every later window is charged to ``max_pages``.

        No page query walks the collection: the plan is already in memory (it is
        arithmetic over the change-signal rows the map read), so a window costs
        exactly one batched primary-key hydration.  That is also why no lookahead
        row is needed here — "is there more?" is ``position < len(plan)``, an
        exact answer rather than an inferred one.
        """
        raise_if_cancelled(cancel_event)
        walk = _Walk(budget, cursor.returned_before if cursor else 0)
        items: List[SourceItem] = []
        resume: Optional[SourceCursor] = None
        exhausted = True
        # 整条链已发出文档的 (id, 元数据摘要) 账目:续跑时从游标接回来,收尾复读时
        # 逐条比对。链级而非本次调用级——混代页正是「早先那一页」被改掉。
        emitted: List[Tuple[str, str]] = list(cursor.emitted_meta) if cursor else []

        with self._database.connect() as db:
            notebook_ids, tiers = self._participants(db, active_notebook_id)
            plan = self._catalog.scope_source_plan(db, notebook_ids)
            sources: Optional[Tuple[ScopeSource, ...]] = plan.sources
            total: Optional[int] = plan.total
            scope_id = (tuple(notebook_ids), plan.fingerprint)
            if cursor is not None:
                if (cursor.scope_notebook_ids, cursor.scope_fingerprint) != scope_id:
                    return self._source_scope_moved(walk, total)
                sources = _resume_source_plan(plan.sources, cursor)
                if sources is None:
                    return self._source_scope_moved(walk, total)
            # Round-trip bound, proved from the traversal: one hydration query
            # per window, the first window free and every later one charged to
            # ``max_pages`` — hence ``1 + max_pages`` queries, whatever the
            # library's size.
            walk.bound_queries(1 + budget.max_pages)
            try:
                position = 0
                first_window = True
                while position < len(sources):
                    raise_if_cancelled(cancel_event)
                    # Set BEFORE the ceiling check and BEFORE the fetch: every
                    # stop from here on must hand back a cursor pointing at the
                    # first source not yet listed, including a payload ceiling
                    # that fires on the very first item of the window.
                    resume = _source_cursor(
                        sources[position], scope_id, walk, emitted)
                    allowance = walk.emit_allowance(first_page=first_window)
                    window = sources[position:position + allowance]
                    walk.charge_query()
                    rows = self._source_listing(
                        db, [entry.source_id for entry in window]
                    )
                    walk.charge_page(first_page=first_window)
                    first_window = False
                    for entry in window:
                        resume = _source_cursor(entry, scope_id, walk, emitted)
                        # Counted as read whether or not it yields an item: a
                        # source whose row vanished between the plan and this
                        # hydration was still visited, and hiding that would
                        # make ``scanned == returned`` claim a walk that did not
                        # happen.  It also makes the missing row visible where it
                        # belongs — as the denominator mismatch that reports
                        # ``concurrent_change`` below.
                        walk.scanned += 1
                        row = rows.get(entry.source_id)
                        if row is not None:
                            item = SourceItem(
                                source_id=entry.source_id,
                                source_title=source_display_title(row),
                                doc_type_label=_doc_type_label(
                                    _row_get(row, "doc_type")
                                ),
                                summary=str(_row_get(row, "summary") or "")[
                                    : max(0, int(budget.excerpt_chars))
                                ],
                                notebook_id=entry.notebook_id,
                                tier=str(tiers.get(entry.notebook_id, "")),
                            )
                            walk.admit(item)
                            items.append(item)
                            emitted.append((
                                entry.source_id,
                                _source_meta_digest(
                                    item.source_title, item.doc_type_label),
                            ))
                        position += 1
            except _Stop as stop:
                walk.reason = stop.reason
                exhausted = False

            closing_ids = self._closing_participants(db, active_notebook_id)
            scope_stable = (
                closing_ids == tuple(notebook_ids)
                and self._catalog.scope_signal_fingerprint(db, closing_ids)
                == plan.fingerprint
            )
            # 元数据换代复检。作用域指纹证明的是「源集合与元素代次没变」,证明不了
            # 「这些文档的显示名/类型还是发出去时那个」——见 ``_source_meta_digest``:
            # 论文元数据回填不碰 ``sources.updated_at``。不复检的话,走页期间的一次
            # 回填会产出一份混代目录、却仍然报 complete。
            # 成本 = 每条链收尾一次有界 IN 点查(≤行池上限,≤1 条 SQL);刻意不计入
            # ``charge_query``,那个预算约束的是**翻页**往返,而这是与收尾参与者/
            # 指纹读取同级的固定尾巴。
            if emitted and not self._source_meta_unchanged(db, emitted):
                scope_stable = False

        coverage = _coverage(
            walk, total=total, exhausted=exhausted, scope_stable=scope_stable
        )
        return SourceEnumeration(
            items=tuple(items),
            coverage=coverage,
            cursor=_resumable(coverage, resume),
            extra_pages=walk.pages,
            payload_chars=walk.payload,
        )

    def _source_scope_moved(
        self, walk: _Walk, total: Optional[int]
    ) -> SourceEnumeration:
        """The scope this cursor was cut from is gone: stop, do not restart."""
        return SourceEnumeration(
            items=(),
            coverage=_coverage(
                walk, total=total, exhausted=False, scope_stable=False
            ),
            cursor=None,
            extra_pages=walk.pages,
            payload_chars=walk.payload,
        )

    def _source_listing(
        self, db: object, source_ids: Sequence[str]
    ) -> Dict[str, Any]:
        return {
            str(row["id"]): row
            for row in self._sources.source_listing_rows(db, list(source_ids))
        }

    def _source_meta_unchanged(
        self, db: object, emitted: Sequence[Tuple[str, str]]
    ) -> bool:
        """Re-read the chain's emitted documents and confirm their listed
        metadata still digests to what was handed out.

        ``False`` on ANY discrepancy, including a row that has since disappeared:
        the caller turns that into ``concurrent_change``, which is the honest
        answer for "a roster whose earlier pages may describe a different
        generation than its later ones".  It never repairs or re-emits — an
        enumeration hands out one generation or says it could not.

        One batched primary-key read (``source_listing_rows`` batches by the
        adapter's ``IN`` width, and the ledger is capped by the run's row pool),
        so the whole check is one query per chain close.
        """
        rows = self._source_listing(db, [source_id for source_id, _ in emitted])
        for source_id, digest in emitted:
            row = rows.get(source_id)
            if row is None:
                return False
            current = _source_meta_digest(
                source_display_title(row),
                _doc_type_label(_row_get(row, "doc_type")),
            )
            if current != digest:
                return False
        return True

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

        The status predicate is ``USABLE_STATUSES`` — the same object the
        catalog's per-type counting uses.  This is the whole reason the map and
        the list can be shown side by side: "concept 89" and a list of 89 come
        from one definition of usable, not two.  It is applied to rows this
        module reads rather than inside the page query, because the keyset
        index carries no ``status`` column; see ``_usable_kg_page`` for the
        ceiling that keeps that filtering bounded.
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
            notebook_ids, tiers = self._participants(db, active_notebook_id)
            opening_seqs = self._kg_seqs(db, notebook_ids)
            total = self._kg_total(db, notebook_ids, object_type)
            walk_ids: Sequence[str] = notebook_ids
            # Private-Memory exclusion, resolved per participant.  One bounded
            # id query per notebook actually walked (the set is one row per
            # confirmed Memory), deliberately NOT a SQL predicate on the page
            # query — see ``knowledge_object_page_rows``.  It is charged to
            # neither ``max_pages`` nor the round-trip bound below: both count
            # PAGE queries, and this is a once-per-participant scope read, the
            # same shape as ``_kg_seqs``.
            memory_ids: Dict[str, frozenset] = {}
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
                        payload_chars=walk.payload,
                    )
                walk_ids = notebook_ids[notebook_ids.index(cursor.notebook_id):]
                after = (
                    (cursor.created_at, cursor.object_id)
                    if cursor.created_at is not None else None
                )

            # Round-trip bound, same shape as the element side but with the
            # status filter's top-up reads in it (see ``_usable_kg_page``):
            #   * logical pages ≤ one free first page per participant, plus
            #     ``max_pages`` charged ones;
            #   * inside one logical page, every query that comes back with
            #     rows charges them to ``raw_scan_limit``, and the first query
            #     that comes back short ends that page — so the queries that
            #     are NOT bounded by the raw ceiling are at most one per page.
            # Participants replace ``max_rows`` here because a participant with
            # no objects of the type still costs its one query: unlike the
            # element plan, the KG side has no per-partition count to skip on.
            walk.bound_queries(
                len(walk_ids) + budget.max_pages + walk.raw_scan_limit
            )
            try:
                for notebook_id in walk_ids:
                    raise_if_cancelled(cancel_event)
                    tier = str(tiers.get(notebook_id, ""))
                    memory_ids[notebook_id] = frozenset(
                        self._sources.memory_source_ids(db, notebook_id)
                    )
                    resume = _kg_cursor(
                        object_type, notebook_id, after, opening_seqs, walk
                    )
                    first_page = True
                    while True:
                        raise_if_cancelled(cancel_event)
                        allowance = walk.emit_allowance(first_page=first_page)
                        usable, scan_after, capped = self._usable_kg_page(
                            db, notebook_id, object_type, after,
                            allowance + 1, walk, cancel_event,
                            memory_ids[notebook_id],
                        )
                        walk.charge_page(first_page=first_page)
                        first_page = False
                        lookahead = len(usable) > allowance
                        page = usable[:allowance]
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
                        if capped:
                            # Everything scanned past the last emitted row was
                            # unusable (the over-scan only stops while it is
                            # still short of what it was asked for), so the
                            # cursor may skip that stretch for good.  Without
                            # this the next call would re-read the very
                            # deprecated prefix that exhausted this one and the
                            # chain would never advance.
                            after = scan_after
                            resume = _kg_cursor(
                                object_type, notebook_id, after, opening_seqs, walk
                            )
                            raise _Stop(TRUNCATED_BUDGET)
                        if not lookahead:
                            # No lookahead and no ceiling = this participant's
                            # keyset ran out.
                            break
                    after = None        # each notebook restarts its own keyset
            except _Stop as stop:
                walk.reason = stop.reason
                exhausted = False

            closing_ids = self._closing_participants(db, active_notebook_id)
            scope_stable = (
                closing_ids == tuple(notebook_ids)
                and self._kg_seqs(db, closing_ids) == opening_seqs
            )

        coverage = _coverage(
            walk, total=total, exhausted=exhausted, scope_stable=scope_stable
        )
        return KgObjectEnumeration(
            object_type=object_type,
            items=tuple(items),
            coverage=coverage,
            cursor=_resumable(coverage, resume),
            extra_pages=walk.pages,
            payload_chars=walk.payload,
        )

    def _usable_kg_page(
        self,
        db: object,
        notebook_id: str,
        object_type: str,
        after: Optional[Tuple[Any, str]],
        want: int,
        walk: _Walk,
        cancel_event: CancelEvent,
        memory_source_ids: frozenset,
    ) -> Tuple[List[Any], Optional[Tuple[Any, str]], bool]:
        """Assemble ONE logical page of LISTABLE rows out of raw keyset rows.

        The page query carries neither a status nor a source-type predicate
        (see the port docstring), so this is where "listable" is decided, out
        of two parts:

        * ``USABLE_STATUSES`` — the counting path's own object, so the map and
          the list can never disagree about what a deprecated object is;
        * not owned by a private Memory synthetic source — the ids come from
          ``SourceStore.memory_source_ids``, the same list the catalog
          subtracts from this type's count, so again map and list share one
          definition.  A confirmed Memory is owner-private and this traversal
          has no owner filter; without this, any member of a shared notebook
          would read another member's Memory-derived objects.

        Filtering here costs top-up reads whenever unlistable objects are
        interleaved, and that cost is what ``_KG_RAW_SCAN_FACTOR`` bounds: each
        fetch is clamped to the remaining raw allowance, so the loop can never
        read past ``walk.raw_scan_limit`` raw rows for this action, whatever it
        has managed to collect.  Stopping short is reported
        honestly (``truncated_reason="budget"``) rather than silently — a list
        that quietly ends where the scan gave up is exactly the false "all"
        this module exists to prevent.

        Returns ``(listable rows, position of the last row READ, ceiling hit)``.
        The second value lets the caller move its cursor past a stretch of
        skipped rows: neither a deprecated object nor a Memory-derived one is
        part of this collection, so skipping them permanently is correct, and
        it is what guarantees a resumed chain makes progress instead of
        re-reading the same prefix.  The ceiling flag can only be set while the
        page is still SHORT of ``want``, so a caller that skips ahead on it can
        never skip a listable row it has not emitted.
        """
        collected: List[Any] = []
        scan_after = after
        while len(collected) < want:
            raise_if_cancelled(cancel_event)
            # The ceiling is enforced BEFORE the query, not observed after it
            # (codex #395 round-5): the fetch is clamped to the remaining raw
            # allowance, so the documented ``raw_scan_limit`` can never be
            # exceeded — not even by the final batch — and an exhausted
            # allowance stops with an honest ``budget`` truncation instead of
            # letting an over-budget read fill the page and report success.
            remaining_raw = walk.raw_scan_limit - walk.raw_scanned
            if remaining_raw <= 0:
                return collected, scan_after, True
            # The first read of an all-usable notebook still asks for exactly
            # ``want`` rows — precisely the query it issued before the status
            # filter moved out of SQL.
            fetch = min(max(1, want - len(collected)), remaining_raw)
            walk.charge_query()
            rows = self._knowledge.knowledge_object_page_rows(
                db, notebook_id, object_type, scan_after, fetch
            )
            within_ceiling = walk.scan_raw(len(rows))
            for row in rows:
                usable = (
                    str(_row_get(row, "status") or "") in USABLE_STATUSES
                    and str(_row_get(row, "source_id") or "")
                    not in memory_source_ids
                )
                scan_after = (
                    _row_get(row, "created_at"), str(_row_get(row, "id"))
                )
                if usable:
                    collected.append(row)
                    if len(collected) >= want:
                        break
            if len(collected) >= want:
                break
            if len(rows) < fetch:
                break                   # this participant's keyset ran out
            if not within_ceiling:
                return collected, scan_after, True
        return collected, scan_after, False

    # ---------------------------------------------------------------- reads
    def _explicit_source_plan(
        self,
        db: object,
        notebook_ids: Sequence[str],
        plan: ScopeElementPlan,
        source_id: str,
    ) -> Tuple[Tuple[ScopeSource, ...], Optional[int], Dict[str, Any]]:
        """Scope-check and plan a single explicitly requested source.

        One primary-key read proves three things at once: that the source
        exists, that it belongs to a participant notebook, and that it is not a
        private Memory synthetic row.  An out-of-scope id raises rather than
        returning another notebook's rows, and rather than being answered with
        a silent empty list; a Memory row raises for the same reason, in the
        same shape.

        The Memory check is defence in depth rather than a reachable path
        today: the only producer of a ``source_id`` here is
        ``resolve_source_title``, which walks the map's plan, and the plan no
        longer contains Memory sources at all.  But this branch exists
        precisely to walk a source the plan does NOT list ("absence from the
        plan is a statement about a cache"), so the one place that argument
        must not be extended is the one place the plan's absence is a privacy
        boundary rather than a cache miss.  It costs ONE extra bounded query,
        only on this explicitly-named path, and it asks the same
        ``memory_source_ids`` the rest of the feature asks rather than
        re-spelling the predicate.

        The source is then walked whatever the map says about it.  Its
        ``total`` comes from the plan when the map has a count for it, and is
        ``None`` (unknown denominator) otherwise: "absent from the plan" is a
        statement about a cache, and reporting an unknown denominator costs
        nothing next to answering a hand-named source with a confident zero.
        """
        rows = self._source_display(db, [source_id])
        row = rows.get(source_id)
        if row is None or str(row["notebook_id"]) not in set(notebook_ids):
            raise ValueError(f"source is not in scope: {source_id!r}")
        owner_notebook_id = str(row["notebook_id"])
        if source_id in set(
            self._sources.memory_source_ids(db, owner_notebook_id)
        ):
            raise ValueError(f"source is not enumerable: {source_id!r}")
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
            payload_chars=walk.payload,
        )

    def _participants(
        self, db: object, active_notebook_id: str
    ) -> Tuple[Tuple[str, ...], Dict[str, str]]:
        """The scope's participants, narrowed by the run's library checkboxes.

        The ONE place this service turns "which libraries could participate"
        into "which libraries this run reads".  Tiers are returned unfiltered
        on purpose: they are a display lookup keyed by notebook id, so entries
        for skipped libraries are simply never asked for.
        """
        notebook_ids, tiers = self._notebooks.participant_tiers(
            db, active_notebook_id
        )
        return scoped_participants(notebook_ids), tiers

    def _closing_participants(
        self, db: object, active_notebook_id: str
    ) -> Tuple[str, ...]:
        """Re-resolve the scope's participants for the closing stability check.

        The opening participant list is NOT reusable here, and that is the whole
        point.  Between the first page and the last sits at least one LLM round
        trip, during which a reference library can be mounted, unmounted,
        downgraded or change owner.  Recomputing a fingerprint over the OLD id
        list asks "did the libraries I already knew about change?" — a question
        whose answer is yes even when a whole new library appeared, and no when
        one silently dropped out.  Either way the walk never visited it, and
        reporting ``complete=true`` would be a false "all".

        Resolution goes through ``participant_ids``, i.e. the same
        ``resolve_participants`` and the same ``mount_sql.py`` validity
        predicate the opening ``participant_tiers`` call used — one definition
        of "in scope", checked twice.  Tiers are deliberately not re-read: they
        are display metadata already carried by the emitted items, not part of
        the scope's identity.

        The run's library checkboxes are re-applied here too, so the comparison
        is EFFECTIVE scope against effective scope.  A library the run never
        reads is not part of this walk's identity: mounting or unmounting an
        unchecked one mid-chain changes nothing about what was enumerated, and
        reporting ``concurrent_change`` for it would refuse a complete answer
        over an event with no bearing on it.  A newly mounted CHECKED library
        still lands in this list and still breaks the comparison — which is the
        case that matters, because the walk never visited it.
        """
        return scoped_participants(
            self._notebooks.participant_ids(db, active_notebook_id)
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
    ) -> Tuple[Tuple[str, int, int], ...]:
        """The scope's ``(kg_reset_epoch, kg_mutation_seq)`` vector — one O(1)
        row per participant, and the KG side's answer to "did the scope
        move?".

        A vector rather than a sum: two participants whose seqs move in
        opposite directions must not cancel out, and the participant list
        itself has to be part of the identity.

        R1 (P2-2, post-review, batch-3-W1 PR-2): widened each element from a
        bare ``kg_mutation_seq`` int to ``(kg_reset_epoch, kg_mutation_seq)``.
        This vector is compared opening-vs-closing across a paginated
        enumeration walk (``concurrent_change`` detection); a delete_
        notebook_kg mid-walk that happens to re-climb kg_mutation_seq back to
        the opening value before the closing read would otherwise report
        "scope unchanged" for a participant whose graph really was reset.
        """
        result = []
        for notebook_id in notebook_ids:
            row = self._unified_kg.graph_seq_row(db, notebook_id)
            result.append((notebook_id, int(row[3]), int(row[0])))
        return tuple(result)

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
    scope_seqs: Tuple[Tuple[str, int, int], ...],
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


def _source_cursor(
    entry: ScopeSource,
    scope_id: Tuple[Tuple[str, ...], str],
    walk: _Walk,
    emitted: Sequence[Tuple[str, str]],
) -> SourceCursor:
    """A cursor pointing AT ``entry`` — the first source not yet listed.

    ``emitted`` is snapshotted (``tuple(...)``) rather than referenced: the
    caller keeps appending to that list, and a cursor is supposed to describe the
    moment it was cut.  Aliasing it would let a later item retroactively appear
    in an earlier cursor's ledger.
    """
    return SourceCursor(
        notebook_id=entry.notebook_id,
        source_id=entry.source_id,
        scope_notebook_ids=scope_id[0],
        scope_fingerprint=scope_id[1],
        returned_before=walk.returned_total,
        emitted_meta=tuple(emitted),
    )


def _resume_source_plan(
    sources: Sequence[ScopeSource], cursor: SourceCursor
) -> Optional[Tuple[ScopeSource, ...]]:
    """Re-align a source plan against a cursor from an earlier call.

    By KEY, not by position, for the same reason ``_resume_sources`` is: the plan
    is rebuilt on every call, and an index-based resume would skip or repeat a
    document whenever one was added or removed in between.  The slice STARTS at
    the cursor's source (inclusive — it is the first one not yet listed).
    ``None`` means that document is no longer in the plan, which cannot be
    honestly continued: the caller reports ``concurrent_change`` rather than
    silently restarting the list.
    """
    for index, entry in enumerate(sources):
        if (
            entry.notebook_id == cursor.notebook_id
            and entry.source_id == cursor.source_id
        ):
            return tuple(sources[index:])
    return None


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
