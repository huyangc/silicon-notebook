"""Document-level search ``find_sources`` (design §5): which papers/documents of
the run's source set are about X, ranked, tiered and paged.

Two legs over ONE source set (§5.2):

* **the source set** -- in a question run, exactly what ``enumerate_sources``
  lists (``CollectionEnumerationService.source_scope``: the checkbox-narrowed
  participants, the user-visible in-ceiling plan, the ceiling digest); each
  library's metadata read takes its source list from
  ``scoped_allowed_source_ids`` or, when the run's ceiling was pushed down,
  reads without one and verifies what it read (``verify_unbound_read``).
  A caller WITHOUT a run scope (MCP) passes ``allowed_source_ids`` explicitly --
  ``all_visible_source_ids(nb) ∪ hidden_source_ids(nb, owner)`` -- because
  nothing else narrows a chunk read there: without it another member's Memory
  projection could surface as a best passage.
* **the metadata leg** -- ``SourceStorePort.find_sources_metadata_rows``, one
  library at a time: the filtered scope (``kind`` / year / venue / author) and
  which fields the literal ``terms`` matched, with the word-boundary flag.
* **the semantic leg** -- ``RetrievalPort.retrieve_chunks_in_sources`` over the
  filtered scope of each library (the filters decide the allow list first),
  ``FIND_SOURCES_CHUNK_RECALL`` passages, grouped by source: the best fused
  ``relevance``, the passage count and the lane that ran.

Tiering (§5.3) exposes no float: ``strong`` / ``related`` from the matched
fields, the word-boundary rule and two configured relevance lines that apply on
passages whose relevance carries a semantic signal only; without one (the
FTS-degraded lane, or a semantic lane whose passages carried no vector
similarity -- no query embedding, unembedded passages) a passage-only source
needs two hits to be ``related``.  The order is total and deterministic,
so a page is recomputed on every call and the cursor is just an offset plus a
fingerprint of the parameters (any change invalidates it).  Within one
retrieval run the ranked list is memoized by that fingerprint
(``memoized_retrieval_value``), so turning a page only slices it; without a run
(MCP) every call recomputes.  Only the page is hydrated (titles, authors, card
status); the ranking reads ids, years, the pending flag and the matched fields.

Participant libraries other than the run's subject are read like a federated
PEER (``chunk_federation.is_peer_library``): their semantic leg runs under the
federation's peek-only lane (``peer_library_lane``: warm ANN or bounded FTS,
never a cold scale-index load), and each such library's reads run under its own
read budget (the federation's per-library timeout, capped by the phase when a
run plan is installed) with failure isolation: a library that fails or times
out is skipped, reported in ``skipped_libraries`` and a
``find_sources_library_skipped`` event, and makes ``lexical_complete`` false.
The page's display hydration runs under the same isolation and budget: a peer
whose display read fails or times out drops only its own items from the page
(``hydrate:<reason>``).  Hydration is outside the run memo, so such a skip is
never kept for a later page or call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.domain.promotion_source import presented_source_title
from app.domain.retrieval import RetrievedChunk
from app.domain.retrieval_control import RetrievalControlError
from app.domain.source_find_terms import (
    STRONG_FIELDS,
    fold_term,
    ordered_fields,
    really_narrowed,
)
from app.repositories.read_budget import classify_read_failure, read_budget
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.chunk_federation import is_peer_library
from app.services.federated_run import current_federated_run_plan
from app.services.retrieval_participants import federated_ask_active
from app.services.retrieval_run import (
    memoized_retrieval_value,
    paper_meta_write_generation,
)
from app.services.source_scope import (
    scoped_allowed_source_ids,
    subjectless_run_active,
    unbound_ceiling,
    verify_unbound_read,
)

#: Items per page (the Agent and MCP alike, design §5.4).
FIND_SOURCES_PAGE_SIZE = 20
#: ``terms``: at most this many, each at most this long (design §5.1).
FIND_SOURCES_MAX_TERMS = 6
FIND_SOURCES_TERM_MAX_CHARS = 64
#: ``venue`` / ``author`` substrings and the free-text ``query``.
FIND_SOURCES_FILTER_MAX_CHARS = 64
FIND_SOURCES_QUERY_MAX_CHARS = 1000
#: Display bounds of one item.
BEST_PASSAGE_CHARS = 240
AUTHORS_SHOWN = 3

FIND_SOURCES_SCOPES = ("all", "current_notebook")
FIND_SOURCES_TIERS = ("strong", "related")
#: The lane on which a fused relevance carries no semantic signal.
LEXICAL_ONLY_LANE = "fts_degraded"
#: Appended to a semantic lane (``baseline`` / ``ann``) whose returned passages
#: carried no vector similarity at all (the query embedding or the passages'
#: vectors were unavailable): what ran, and that its evidence was lexical only.
NO_VECTOR_LANE_SUFFIX = ":lexical_only"
#: ``RetrievalSupport.origin`` values whose score is a vector similarity that
#: ``score_chunks`` folded into the passage's fused relevance.
VECTOR_SUPPORT_ORIGINS = frozenset({"semantic", "generated_question"})
#: Passages a passage-only source needs on the lexical-only lane.
LEXICAL_LANE_MIN_HITS = 2

_CURSOR_PREFIX = "fs1."
#: Without a run plan, all peer libraries of one call share one budget of
#: this many per-library timeouts.
PEER_BUDGET_LIBRARIES = 2
#: ``skipped_libraries`` reasons with this prefix lost only the semantic leg.
SEMANTIC_SKIP_PREFIX = "semantic:"
#: ``skipped_libraries`` reasons with this prefix were ranked but their page
#: items could not be displayed (the display read failed or timed out).
HYDRATE_SKIP_PREFIX = "hydrate:"


#: The title of a source that has neither a paper title, a stored title nor a
#: file name: a raw source id is never shown as a title.
UNTITLED_SOURCE = "（未命名文档）"


def source_display_title(
    meta: Optional[Mapping[str, Any]], listing: Mapping[str, Any],
) -> str:
    """The one display-title rule of a found / requested source: the extracted
    paper title, else the stored title (``presented_source_title``), else the
    file name, else ``UNTITLED_SOURCE``."""
    return (
        str((meta or {}).get("paper_title") or "").strip()
        or presented_source_title(
            listing.get("title"), source_type=listing.get("source_type"),
            source_id=listing.get("id"),
        ).strip()
        or str(listing.get("file_name") or "").strip()
        or UNTITLED_SOURCE
    )


@dataclass(frozen=True)
class SourceDisplay:
    """What a source is shown as: title, publication year (``None``: unknown)
    and venue."""

    title: str
    pub_year: Optional[int]
    venue: str


class FindSourcesError(ValueError):
    """The request is malformed (the caller maps it to a refusal)."""


class FindSourcesCursorError(FindSourcesError):
    """The cursor does not belong to these parameters (or is not a cursor)."""


class FindSourcesCursorStaleError(FindSourcesCursorError):
    """The cursor's parameters match, but the paper metadata (or the set of
    libraries read) changed since its page: the ranking it was cut from is
    gone, so its offset would skip or repeat sources.  Restart from page 1."""


@dataclass(frozen=True)
class FindSourcesRequest:
    query: str = ""
    terms: Tuple[str, ...] = ()
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    venue: str = ""
    author: str = ""
    kind: str = "papers"
    scope: str = "all"
    cursor: str = ""
    limit: int = FIND_SOURCES_PAGE_SIZE


@dataclass(frozen=True)
class BestPassage:
    chunk_id: str
    text: str
    element_ids: Tuple[str, ...]


@dataclass(frozen=True)
class FoundSource:
    rank: int
    source_id: str
    notebook_id: str
    title: str
    pub_year: Optional[int]
    venue: str
    authors: Tuple[str, ...]
    tier: str
    reasons: Tuple[str, ...]
    best_passage: Optional[BestPassage]
    card_status: str


@dataclass(frozen=True)
class FindSourcesResult:
    items: Tuple[FoundSource, ...]
    strong: int
    related: int
    filtered_scope_total: int
    lexical_complete: bool
    semantic_recall_capped: bool
    next_cursor: str
    #: ``{notebook_id: lane}`` of the libraries the semantic leg read; a
    #: semantic lane whose passages carried no vector similarity is reported
    #: with ``NO_VECTOR_LANE_SUFFIX`` (e.g. ``baseline:lexical_only``).
    lanes: Mapping[str, str]
    #: Eligible papers without extracted metadata that a year / venue / author
    #: filter excluded (they cannot be judged), design §5.4.
    meta_pending_excluded: int = 0
    #: Eligible papers without extracted metadata in the filtered scope when
    #: ``terms`` were given (only title / file name / summary could be checked).
    meta_pending_unchecked: int = 0
    #: ``{notebook_id: reason}`` of the participant libraries skipped in this
    #: call (``timeout`` / ``saturated`` / ``queue_deadline`` / ``unavailable``;
    #: prefixed ``semantic:`` when only the semantic leg failed and the
    #: library's metadata hits are still in the result; ``hydrate:`` when the
    #: library ranked but its items on this page could not be displayed).
    skipped_libraries: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Library:
    """One library's read plan: the store allow list (``None``: read without
    one), the frozen ceiling an unlisted read is verified against, and the
    plan ids the result is cut to (``None`` for an explicit source set)."""

    notebook_id: str
    allowed: Optional[Tuple[str, ...]]
    unbound: Optional[frozenset]
    plan_ids: Optional[frozenset]


@dataclass
class _Candidate:
    row: Dict[str, Any]
    #: The owning library's lane (as reported in ``lanes``).
    lane: str = ""
    best: Optional[float] = None
    #: The best relevance among passages that carried a vector similarity
    #: (``None``: no semantic evidence -- the lexical-only rule applies).
    semantic_best: Optional[float] = None
    hits: int = 0
    passage: Optional[RetrievedChunk] = None
    tier: str = ""


@dataclass(frozen=True)
class _MetadataRead:
    rows: Tuple[Dict[str, Any], ...]
    excluded: int
    narrowed: bool
    meta_state: str = ""


@dataclass(frozen=True)
class _LibraryRead:
    rows: Tuple[Dict[str, Any], ...]
    meta_pending_excluded: int
    lane: str = ""
    capped: bool = False
    chunks: Tuple[RetrievedChunk, ...] = ()
    meta_state: str = ""


@dataclass(frozen=True)
class _PeerBudget:
    """How peer libraries are bounded in one phase: each read gets
    ``min(now + timeout, phase_deadline)`` under ``cancel``."""

    timeout: float
    phase_deadline: float
    cancel: Any


@dataclass(frozen=True)
class _Display:
    """One library's display rows for a page."""

    listings: Dict[str, Mapping[str, Any]]
    metas: Dict[str, dict]
    cards: Dict[str, dict]
    bases: Dict[str, str]


@dataclass(frozen=True)
class _Ranking:
    """Everything one parameter fingerprint ranks to (memoized per run)."""

    ranked: Tuple[_Candidate, ...]
    filtered_scope_total: int
    capped: bool
    lanes: Mapping[str, str]
    skipped: Mapping[str, str]
    meta_pending_excluded: int
    meta_pending_unchecked: int
    #: Digest of what the ranking was cut from -- each library's paper-meta
    #: state, the libraries skipped and the run's write generation -- carried
    #: by its cursors (``FindSourcesCursorStaleError`` when it moved).
    meta_digest: str = ""


def paper_card_status(
    is_paper: Optional[bool],
    pending: bool,
    card: Optional[Mapping[str, Any]],
    basis: str,
    *,
    card_version: int,
) -> str:
    """``card_status`` of one found source (design §5.4 + ``meta_pending``).

    No paper-meta row (``is_paper`` is ``None``): ``meta_pending`` for a
    source eligible for one (``pending``: the store's
    ``PAPER_META_ELIGIBLE_SQL``, metadata is extracted lazily), else
    ``not_paper``.  A meta row saying "not a paper": ``not_paper``.  A paper:
    ``ready`` / ``failed`` when its card row is for the current element
    ``basis`` and ``card_version``, else ``missing`` (absent, stale, or of an
    older version -- regenerated lazily).  ``card_version`` is passed in by the
    caller (the paper card module's ``PAPER_CARD_VERSION``) so this judgement
    has one definition."""
    if is_paper is None:
        return "meta_pending" if pending else "not_paper"
    if not is_paper:
        return "not_paper"
    if (
        card is None
        or int(card.get("card_version", -1)) != int(card_version)
        or str(card.get("basis") or "") != str(basis or "")
    ):
        return "missing"
    return "ready" if card.get("status") == "ready" else "failed"


def _tier(
    matches: Mapping[str, bool],
    semantic_best: Optional[float],
    hits: int,
    *,
    strong_line: float,
    related_line: float,
) -> str:
    """The relevance lines read ``semantic_best`` only: a keyword-only fused
    relevance (``score_chunks`` renormalizes it to 0..1, so one mention scores
    1.0) is no semantic support.  A source without semantic evidence takes the
    lexical-only rule whatever lane ran."""
    if any(matches.get(field) is True for field in STRONG_FIELDS):
        return "strong"
    if semantic_best is not None and semantic_best >= strong_line:
        return "strong"
    if matches:
        return "related"
    if semantic_best is not None:
        return "related" if semantic_best >= related_line else ""
    if hits >= LEXICAL_LANE_MIN_HITS:
        return "related"
    return ""


def _vector_supported(chunk: RetrievedChunk) -> bool:
    """Whether a vector similarity contributed to this passage's relevance --
    read from the producer's own evidence, not from the lane's name."""
    return any(
        support.origin in VECTOR_SUPPORT_ORIGINS
        for support in chunk.retrieval_supports
    )


def _lexical_only(lane: str) -> bool:
    return lane == LEXICAL_ONLY_LANE or lane.endswith(NO_VECTOR_LANE_SUFFIX)


def _year_key(value: Optional[int]) -> Tuple[bool, int]:
    return (value is None, -(value or 0))


def _relevance_order(candidate: _Candidate) -> tuple:
    return (
        FIND_SOURCES_TIERS.index(candidate.tier),
        -len(candidate.row["matches"]),
        -(candidate.best or 0.0),
        -candidate.hits,
        *_year_key(candidate.row["pub_year"]),
        candidate.row["source_id"],
    )


def _filter_order(candidate: _Candidate) -> tuple:
    return (
        *_year_key(candidate.row["pub_year"]),
        candidate.row["sort_title"],
        candidate.row["source_id"],
    )


def _clean_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise FindSourcesError(f"{field} is longer than {limit} characters")
    return text


def _normalized(request: FindSourcesRequest) -> FindSourcesRequest:
    """Validate and normalize; over-long input is refused, never sliced."""
    terms = tuple(dict.fromkeys(
        term for term in (
            _clean_text(value, "term", FIND_SOURCES_TERM_MAX_CHARS)
            for value in request.terms
        ) if term
    ))
    if len(terms) > FIND_SOURCES_MAX_TERMS:
        raise FindSourcesError(f"at most {FIND_SOURCES_MAX_TERMS} terms")
    if request.kind not in ("papers", "all"):
        raise FindSourcesError(f"unknown kind: {request.kind!r}")
    if request.scope not in FIND_SOURCES_SCOPES:
        raise FindSourcesError(f"unknown scope: {request.scope!r}")
    years = []
    for value in (request.year_from, request.year_to):
        if value is None or value == "":
            years.append(None)
            continue
        try:
            years.append(int(value))
        except (TypeError, ValueError) as exc:
            raise FindSourcesError("year must be an integer") from exc
    if None not in years and years[0] > years[1]:
        raise FindSourcesError("year_from is after year_to")
    try:
        limit = int(request.limit or FIND_SOURCES_PAGE_SIZE)
    except (TypeError, ValueError) as exc:
        raise FindSourcesError("limit must be an integer") from exc
    if not 1 <= limit <= FIND_SOURCES_PAGE_SIZE:
        raise FindSourcesError(f"limit must be 1..{FIND_SOURCES_PAGE_SIZE}")
    normalized = FindSourcesRequest(
        query=_clean_text(request.query, "query", FIND_SOURCES_QUERY_MAX_CHARS),
        terms=terms,
        year_from=years[0],
        year_to=years[1],
        venue=_clean_text(request.venue, "venue", FIND_SOURCES_FILTER_MAX_CHARS),
        author=_clean_text(request.author, "author", FIND_SOURCES_FILTER_MAX_CHARS),
        kind=request.kind,
        scope=request.scope,
        cursor=str(request.cursor or ""),
        limit=limit,
    )
    if not (
        normalized.query or normalized.terms or normalized.venue
        or normalized.author or normalized.year_from is not None
        or normalized.year_to is not None
    ):
        raise FindSourcesError(
            "find_sources needs a query, terms or at least one filter"
        )
    return normalized


def _fingerprint(request: FindSourcesRequest, scope_identity: Sequence[Any]) -> str:
    payload = json.dumps(
        [
            request.query, [fold_term(term) for term in request.terms],
            request.year_from, request.year_to, fold_term(request.venue),
            fold_term(request.author), request.kind, request.scope,
            list(scope_identity),
        ],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


def encode_cursor(offset: int, fingerprint: str, meta_digest: str) -> str:
    raw = json.dumps(
        {"o": int(offset), "f": fingerprint, "m": meta_digest}, separators=(",", ":"))
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> Tuple[int, str]:
    """The offset and the metadata digest ``cursor`` carries (``(0, "")``
    without one); ``FindSourcesCursorError`` when it is not a cursor of these
    parameters over this source scope.  The digest is checked against the
    current ranking's (``check_cursor_digest``) once that ranking exists."""
    if not cursor:
        return 0, ""
    if not cursor.startswith(_CURSOR_PREFIX):
        raise FindSourcesCursorError("not a find_sources cursor")
    body = cursor[len(_CURSOR_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        offset = int(data["o"])
        cut_for = str(data["f"])
        digest = str(data.get("m") or "")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise FindSourcesCursorError("not a find_sources cursor") from exc
    if cut_for != fingerprint or offset < 0:
        raise FindSourcesCursorError(
            "the cursor belongs to other parameters; start a new search"
        )
    return offset, digest


#: ``FindSourcesCursorStaleError``'s message.
STALE_CURSOR_MESSAGE = (
    "the paper metadata changed since this page was listed (a paper's metadata "
    "was just extracted), so the ranking changed; restart from the first page "
    "without a cursor"
)


def check_cursor_digest(cursor: str, cursor_digest: str, meta_digest: str) -> None:
    """``FindSourcesCursorStaleError`` when ``cursor`` was cut from a ranking
    with another metadata digest (a cursor without one is from before the
    digest existed: stale too)."""
    if cursor and cursor_digest != meta_digest:
        raise FindSourcesCursorStaleError(STALE_CURSOR_MESSAGE)


def _meta_digest(
    states: Mapping[str, str], skipped: Mapping[str, str], generation: int,
) -> str:
    payload = json.dumps(
        [sorted(states.items()), sorted(skipped.items()), int(generation)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


def _explicit_identity(source_ids: Sequence[str]) -> str:
    """A digest of an explicit source set (order-free), for the cursor."""
    payload = "\x1e".join(sorted(source_ids))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


class SourceFinder:
    """``find_sources`` over the run's source set (or an explicit one)."""

    def __init__(
        self,
        *,
        database: Any,
        sources: Any,
        retrieval: Any,
        enumeration: Any,
        settings: Any,
        paper_card_version: int,
        event_log: Any = None,
    ) -> None:
        self._database = database
        self._sources = sources
        self._retrieval = retrieval
        self._enumeration = enumeration
        self._settings = settings
        self._card_version = int(paper_card_version)
        self._event_log = event_log

    # ----------------------------------------------------------------- entry
    def find(
        self,
        active_notebook_id: str,
        request: FindSourcesRequest,
        *,
        ceiling_binds: bool = True,
        allowed_source_ids: Optional[Sequence[str]] = None,
        cancel_event: CancelEvent = None,
    ) -> FindSourcesResult:
        """One page of ``find_sources``.

        ``allowed_source_ids`` given: the source set is that list over
        ``active_notebook_id`` alone (a caller without a run scope -- MCP --
        passes ``all_visible ∪ hidden(owner)``).  Omitted: the run's set,
        resolved as ``enumerate_sources`` resolves it, under the run's
        ``ceiling_binds`` verdict."""
        request = _normalized(request)
        raise_if_cancelled(cancel_event)
        with self._database.connect() as db:
            libraries, scope_identity = self._source_set(
                db, active_notebook_id, request,
                ceiling_binds=ceiling_binds, allowed_source_ids=allowed_source_ids,
            )
        fingerprint = _fingerprint(request, scope_identity)
        offset, cursor_digest = decode_cursor(request.cursor, fingerprint)
        generation = paper_meta_write_generation()
        # One ranking per fingerprint per run: a page turn only slices it.
        # Without a run (MCP) the memo is a pass-through.
        ranking = memoized_retrieval_value(
            (
                "find_sources_ranking", fingerprint,
                # Paper metadata written earlier in this run (a card's lazy
                # extraction) changes years / venues / pending: re-rank.
                generation,
                int(self._settings.find_sources_chunk_recall),
                float(self._settings.find_sources_strong_relevance),
                float(self._settings.find_sources_related_relevance),
            ),
            lambda: self._rank(
                active_notebook_id, libraries, request, cancel_event, generation),
        )
        # A page of an older ranking (metadata written since, in this run or
        # between MCP calls) would skip or repeat sources: refuse it.
        check_cursor_digest(request.cursor, cursor_digest, ranking.meta_digest)
        ranked = ranking.ranked
        page = ranked[offset:offset + request.limit]
        # Hydration is outside the memo: a peer skipped here is re-read on the
        # next page or call, never remembered as a skip.
        items, hydrate_skipped = self._hydrate(
            active_notebook_id, page, offset, cancel_event)
        skipped = {**ranking.skipped, **hydrate_skipped}
        more = offset + request.limit < len(ranked)
        return FindSourcesResult(
            items=items,
            strong=sum(1 for c in ranked if c.tier == "strong"),
            related=sum(1 for c in ranked if c.tier == "related"),
            filtered_scope_total=ranking.filtered_scope_total,
            lexical_complete=(bool(request.terms) or not request.query)
            and not ranking.meta_pending_excluded
            and not ranking.meta_pending_unchecked
            and not any(
                not reason.startswith(SEMANTIC_SKIP_PREFIX)
                for reason in skipped.values()
            ),
            semantic_recall_capped=ranking.capped,
            next_cursor=(
                encode_cursor(offset + request.limit, fingerprint, ranking.meta_digest)
                if more else ""
            ),
            lanes=dict(ranking.lanes),
            meta_pending_excluded=ranking.meta_pending_excluded,
            meta_pending_unchecked=ranking.meta_pending_unchecked,
            skipped_libraries=skipped,
        )

    # ------------------------------------------------------------ source set
    def _source_set(
        self,
        db: Any,
        active_notebook_id: str,
        request: FindSourcesRequest,
        *,
        ceiling_binds: bool,
        allowed_source_ids: Optional[Sequence[str]],
    ) -> Tuple[List[_Library], Tuple[Any, ...]]:
        """The libraries to read, plus the scope's identity for the cursor
        fingerprint (an explicit set is identified by a digest of its ids, so
        a changed list invalidates the cursor)."""
        if allowed_source_ids is not None:
            explicit = tuple(dict.fromkeys(
                str(value) for value in allowed_source_ids if str(value or "")
            ))
            return (
                [_Library(active_notebook_id, explicit, None, None)],
                ("explicit", active_notebook_id, _explicit_identity(explicit)),
            )
        resolved = self._enumeration.source_scope(
            db, active_notebook_id, ceiling_binds=ceiling_binds,
            local_only=request.scope == "current_notebook"
            and not subjectless_run_active(),
        )
        plan_ids: Dict[str, set] = {nb: set() for nb in resolved.notebook_ids}
        for entry in resolved.plan.sources:
            plan_ids.setdefault(entry.notebook_id, set()).add(entry.source_id)
        libraries = []
        for notebook_id in resolved.notebook_ids:
            # One verdict read per library, BEFORE the read: either the frozen
            # ceiling to verify an unlisted read against, or the list itself.
            unbound = unbound_ceiling(notebook_id)
            allowed = (
                None if unbound is not None
                else scoped_allowed_source_ids(notebook_id)
            )
            libraries.append(_Library(
                notebook_id,
                None if allowed is None else tuple(allowed),
                unbound,
                frozenset(plan_ids.get(notebook_id, ())),
            ))
        return libraries, (
            "run", list(resolved.notebook_ids), resolved.ceiling_digest,
        )

    # --------------------------------------------------------------- ranking
    def _rank(
        self,
        active_notebook_id: str,
        libraries: Sequence[_Library],
        request: FindSourcesRequest,
        cancel_event: CancelEvent,
        generation: int = 0,
    ) -> _Ranking:
        """Both legs over every library, tiered and totally ordered.

        A PEER library (``is_peer_library``) is read under its own budget and
        in isolation: its failure skips it alone.  With a run plan the budget
        is the federation's ``_budgeted_retrieve_for`` bound, ``min(phase
        deadline, now + per-library timeout)``.  Without one it is ``now +
        GLOBAL_ASK_NOTEBOOK_TIMEOUT_SECONDS`` capped by ONE budget for all the
        peers together (that timeout × ``min(peers, 2)``), so a call over
        eight slow peers cannot take eight timeouts.  A peer whose metadata
        leg fails is skipped (``reason``); one whose SEMANTIC leg fails keeps
        its metadata rows and is reported as ``semantic:<reason>`` -- the
        lexical coverage is intact, the semantic recall is unknown (reported
        capped).  The subject library outside peer mode fails the call, as
        the single-library chunk path does.  Cancellation is never a skip: it
        is re-checked after every failure and before the ranking is returned
        (and so before the run memo keeps it)."""
        budget = self._peer_budget(
            active_notebook_id, [library.notebook_id for library in libraries],
            cancel_event,
        )
        timeout, phase_deadline, cancel = (
            budget.timeout, budget.phase_deadline, budget.cancel)
        reads: Dict[str, _LibraryRead] = {}
        skipped: Dict[str, str] = {}
        semantic_unknown = False
        for library in libraries:
            raise_if_cancelled(cancel_event)
            notebook_id = library.notebook_id
            if not is_peer_library(active_notebook_id, notebook_id):
                read = self._metadata_read(library, request)
                reads[notebook_id] = self._semantic_read(library, request, read, peer=False)
                continue
            if phase_deadline - time.monotonic() <= 0:
                skipped[notebook_id] = "queue_deadline"
                self._emit_skipped(notebook_id, "queue_deadline", "", 0)
                continue
            deadline = min(time.monotonic() + timeout, phase_deadline)
            started = time.perf_counter()
            # A run already cancelled must not open a connection, and the
            # answer is ``AskCancelled`` -- entering a budget whose token is
            # set would raise ``ReadBudgetExceeded`` and read as a timeout.
            raise_if_cancelled(cancel)
            try:
                with read_budget(deadline, cancel):
                    read = self._metadata_read(library, request)
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:  # noqa: BLE001 - one library must not fail the call
                self._skip(notebook_id, exc, deadline, started, skipped, "",
                           cancel, cancel_event)
                continue
            reads[notebook_id] = _LibraryRead(
                read.rows, read.excluded, meta_state=read.meta_state)
            try:
                with read_budget(deadline, cancel):
                    reads[notebook_id] = self._semantic_read(
                        library, request, read, peer=True)
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:  # noqa: BLE001 - keep the lexical rows
                self._skip(notebook_id, exc, deadline, started, skipped,
                           SEMANTIC_SKIP_PREFIX, cancel, cancel_event)
                semantic_unknown = True
        # A stop that landed during the last read must not be published -- or
        # memoized for the rest of the run -- as a ranking with skips in it.
        raise_if_cancelled(cancel)
        raise_if_cancelled(cancel_event)
        candidates: Dict[str, _Candidate] = {}
        lanes: Dict[str, str] = {}
        capped = semantic_unknown
        for notebook_id, read in reads.items():
            for row in read.rows:
                candidates[row["source_id"]] = _Candidate(row=row)
            if read.lane:
                lanes[notebook_id] = read.lane
                capped = capped or read.capped
            for chunk in read.chunks:
                candidate = candidates.get(str(chunk.source_id))
                if candidate is None or candidate.row["notebook_id"] != notebook_id:
                    continue
                candidate.lane = read.lane
                candidate.hits += 1
                relevance = float(chunk.relevance)
                if not _lexical_only(read.lane) and _vector_supported(chunk) and (
                    candidate.semantic_best is None
                    or relevance > candidate.semantic_best
                ):
                    candidate.semantic_best = relevance
                if (
                    candidate.best is None or relevance > candidate.best
                    or (relevance == candidate.best
                        and chunk.chunk_id < candidate.passage.chunk_id)
                ):
                    candidate.best = relevance
                    candidate.passage = chunk
        return _Ranking(
            ranked=tuple(self._ranked(candidates, request)),
            filtered_scope_total=len(candidates),
            capped=capped,
            lanes=lanes,
            skipped=skipped,
            meta_pending_excluded=sum(r.meta_pending_excluded for r in reads.values()),
            meta_pending_unchecked=sum(
                1 for c in candidates.values() if c.row["pending"]
            ) if request.terms else 0,
            meta_digest=_meta_digest(
                {notebook_id: read.meta_state for notebook_id, read in reads.items()},
                skipped, generation,
            ),
        )

    def _peer_budget(
        self, active_notebook_id: str, notebook_ids: Sequence[str],
        cancel_event: CancelEvent,
    ) -> _PeerBudget:
        """The phase budget for the peers among ``notebook_ids`` (see
        ``_rank``): the run plan's per-library and phase timeouts, or without
        one the configured per-library timeout and ONE shared phase of that
        timeout × ``min(peers, PEER_BUDGET_LIBRARIES)``."""
        plan = current_federated_run_plan() if federated_ask_active() else None
        timeout = float(
            plan.notebook_timeout_seconds if plan is not None
            else self._settings.global_ask_notebook_timeout_seconds
        )
        peers = sum(
            1 for notebook_id in notebook_ids
            if is_peer_library(active_notebook_id, notebook_id)
        )
        return _PeerBudget(
            timeout,
            time.monotonic() + (
                float(plan.phase_timeout_seconds) if plan is not None
                else timeout * min(peers, PEER_BUDGET_LIBRARIES)
            ),
            plan.cancel if plan is not None else cancel_event,
        )

    def _skip(
        self, notebook_id: str, exc: BaseException, deadline: float, started: float,
        skipped: Dict[str, str], prefix: str, cancel: Any, cancel_event: CancelEvent,
    ) -> None:
        """Record one peer's failed read -- unless the run was stopped: a set
        cancel token makes the read budget raise ``ReadBudgetExceeded``, which
        would otherwise classify as ``timeout``."""
        raise_if_cancelled(cancel)
        raise_if_cancelled(cancel_event)
        reason = prefix + (classify_read_failure(exc) or (
            "timeout" if time.monotonic() >= deadline else "unavailable"
        ))
        skipped[notebook_id] = reason
        self._emit_skipped(
            notebook_id, reason, type(exc).__name__,
            round((time.perf_counter() - started) * 1000),
        )

    def _metadata_read(
        self, library: _Library, request: FindSourcesRequest,
    ) -> "_MetadataRead":
        with self._database.connect() as db:
            return self._metadata_rows(db, library, request)

    def _semantic_read(
        self, library: _Library, request: FindSourcesRequest,
        read: "_MetadataRead", *, peer: bool,
    ) -> _LibraryRead:
        """With a ``query``, the semantic leg over the filtered scope (the
        filters decide the allow list first)."""
        rows = tuple(read.rows)
        if not request.query or not rows:
            return _LibraryRead(rows, read.excluded, meta_state=read.meta_state)
        recall = int(self._settings.find_sources_chunk_recall)
        chunks, lane = self._retrieval.retrieve_chunks_in_sources(
            library.notebook_id, request.query, recall,
            [row["source_id"] for row in rows],
            peer=peer, narrowed=read.narrowed,
        )
        if (
            chunks and lane != LEXICAL_ONLY_LANE
            and not any(_vector_supported(chunk) for chunk in chunks)
        ):
            lane = f"{lane}{NO_VECTOR_LANE_SUFFIX}"
        capped = len(chunks) >= recall and (
            _lexical_only(lane)
            or min(float(chunk.relevance) for chunk in chunks)
            >= float(self._settings.find_sources_related_relevance)
        )
        return _LibraryRead(
            rows, read.excluded, lane, capped, tuple(chunks), read.meta_state)

    def _metadata_rows(
        self, db: Any, library: _Library, request: FindSourcesRequest,
    ) -> "_MetadataRead":
        """The metadata leg of one library, cut to the run plan.

        ``narrowed`` (the ``producer_explicit`` attestation for the semantic
        leg) only when the narrowing came from a year / venue / author filter
        or an explicit (MCP) list -- never from ``kind`` alone or the run's
        contextual ceiling -- AND the scope is really narrow
        (``source_find_terms.really_narrowed``): the attestation switches the
        lexical corpus-language gate off, which on a near-whole-library scan
        is the ASK-1 pathology.

        ``excluded`` is the store's SQL count over the library's (visible ∧
        allowed) sources; a read without a list is not cut to the plan here
        (a count cannot be), so it may include a source added after the run
        froze its ceiling -- a coverage hint, never an id."""
        meta_filtered = (
            request.year_from is not None or request.year_to is not None
            or bool(request.venue) or bool(request.author)
        )
        scope = self._sources.find_sources_metadata_rows(
            db, library.notebook_id,
            terms=list(request.terms),
            year_from=request.year_from, year_to=request.year_to,
            venue=request.venue, author=request.author, kind=request.kind,
            allowed_source_ids=(
                None if library.allowed is None else list(library.allowed)
            ),
            with_titles=not request.query and not request.terms,
        )
        rows = scope["rows"]
        narrowed = (meta_filtered or library.plan_ids is None) and really_narrowed(
            len(rows), int(scope["visible_total"]),
        )
        # A read without the run's list is verified against the ceiling taken
        # before it (a source outside flips the verdict for later reads); every
        # read is then cut to the plan ``enumerate_sources`` lists, so the two
        # collections are one set by construction.
        verify_unbound_read(
            library.notebook_id, library.unbound,
            (row["source_id"] for row in rows),
        )
        excluded = max(0, int(scope["meta_pending_excluded"]))
        if library.plan_ids is not None:
            rows = [row for row in rows if row["source_id"] in library.plan_ids]
        return _MetadataRead(
            tuple(rows), excluded, narrowed, str(scope.get("meta_state") or ""))

    def _emit_skipped(
        self, notebook_id: str, reason: str, error_type: str, latency_ms: int,
    ) -> None:
        """Content-free skip receipt (``chunk_federation_skipped``'s fields),
        fail-open: never break the search to report."""
        emit = getattr(self._event_log, "emit", None)
        if not callable(emit):
            return
        try:
            emit({
                "kind": "find_sources_library_skipped",
                "notebook_id": notebook_id,
                "error_type": error_type,
                "latency_ms": latency_ms,
                "reason": reason,
            })
        except Exception:  # noqa: BLE001 - observability is fail-open
            pass

    # ---------------------------------------------------------------- order
    def _ranked(
        self, candidates: Mapping[str, _Candidate], request: FindSourcesRequest,
    ) -> List[_Candidate]:
        if not request.query and not request.terms:
            for candidate in candidates.values():
                candidate.tier = "strong"
            return sorted(candidates.values(), key=_filter_order)
        strong_line = float(self._settings.find_sources_strong_relevance)
        related_line = float(self._settings.find_sources_related_relevance)
        kept = []
        for candidate in candidates.values():
            candidate.tier = _tier(
                candidate.row["matches"], candidate.semantic_best, candidate.hits,
                strong_line=strong_line, related_line=related_line,
            )
            if candidate.tier:
                kept.append(candidate)
        return sorted(kept, key=_relevance_order)

    # -------------------------------------------------------------- hydrate
    def display_metadata(
        self, keys: Sequence[Tuple[str, str]],
    ) -> Dict[Tuple[str, str], SourceDisplay]:
        """``{(notebook_id, source_id): SourceDisplay}`` for sources named
        without a ``find_sources`` sighting (a raw id handed straight to the
        card / extraction tools): the same title rule as a found source and
        the paper metadata of the source's OWN library, batched per owning
        library on one connection.  The caller passes only pairs it already
        resolved inside the run's source ceiling; a pair whose source is not
        stored in that library is left out."""
        by_notebook: Dict[str, List[str]] = {}
        for notebook_id, source_id in dict.fromkeys(keys):
            by_notebook.setdefault(str(notebook_id), []).append(str(source_id))
        out: Dict[Tuple[str, str], SourceDisplay] = {}
        if not by_notebook:
            return out
        with self._database.connect() as db:
            for notebook_id, ids in by_notebook.items():
                listings = {
                    str(row["id"]): dict(row)
                    for row in self._sources.source_listing_rows(db, ids)
                    if str(row["notebook_id"]) == notebook_id
                }
                metas = self._sources.paper_meta_for_sources(
                    db, list(listings), notebook_id=notebook_id) if listings else {}
                for source_id, listing in listings.items():
                    meta = metas.get(source_id)
                    year = (meta or {}).get("pub_year")
                    out[(notebook_id, source_id)] = SourceDisplay(
                        title=source_display_title(meta, listing),
                        pub_year=int(year) if isinstance(year, int) else None,
                        venue=str((meta or {}).get("venue") or "").strip(),
                    )
        return out

    def _display_read(self, notebook_id: str, ids: Sequence[str]) -> _Display:
        """Titles, paper metadata, card rows and card bases of one library's
        page items: bounded primary-key reads on one connection."""
        ids = list(ids)
        with self._database.connect() as db:
            listings = {
                str(row["id"]): dict(row)
                for row in self._sources.source_listing_rows(db, ids)
            }
            metas = self._sources.paper_meta_for_sources(
                db, ids, notebook_id=notebook_id)
            papers = [
                source_id for source_id in ids
                if metas.get(source_id, {}).get("is_paper")
            ]
            cards: Dict[str, dict] = {}
            bases: Dict[str, str] = {}
            if papers:
                cards = self._sources.paper_cards_for_sources(
                    db, papers, notebook_id=notebook_id)
                bases = self._sources.source_card_bases(db, papers)
        return _Display(listings, metas, cards, bases)

    def _hydrate(
        self, active_notebook_id: str, page: Sequence[_Candidate], offset: int,
        cancel_event: CancelEvent,
    ) -> Tuple[Tuple[FoundSource, ...], Dict[str, str]]:
        """Titles, paper metadata, authors and card status for ONE page,
        batched per owning library -- the ranking never reads a display row.

        Isolated like ``_rank``: a PEER library's display read runs on its own
        connection under its own budget, and its failure drops only its items
        from the page and returns it as ``hydrate:<reason>``; the subject
        library's failure fails the call.  Cancellation is never a skip."""
        by_notebook: Dict[str, List[str]] = {}
        for candidate in page:
            by_notebook.setdefault(candidate.row["notebook_id"], []).append(
                candidate.row["source_id"]
            )
        budget = self._peer_budget(active_notebook_id, list(by_notebook), cancel_event)
        displays: Dict[str, _Display] = {}
        skipped: Dict[str, str] = {}
        for notebook_id, ids in by_notebook.items():
            raise_if_cancelled(cancel_event)
            if not is_peer_library(active_notebook_id, notebook_id):
                displays[notebook_id] = self._display_read(notebook_id, ids)
                continue
            if budget.phase_deadline - time.monotonic() <= 0:
                reason = HYDRATE_SKIP_PREFIX + "queue_deadline"
                skipped[notebook_id] = reason
                self._emit_skipped(notebook_id, reason, "", 0)
                continue
            deadline = min(time.monotonic() + budget.timeout, budget.phase_deadline)
            started = time.perf_counter()
            raise_if_cancelled(budget.cancel)
            try:
                with read_budget(deadline, budget.cancel):
                    displays[notebook_id] = self._display_read(notebook_id, ids)
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception as exc:  # noqa: BLE001 - one library must not fail the page
                self._skip(notebook_id, exc, deadline, started, skipped,
                           HYDRATE_SKIP_PREFIX, budget.cancel, cancel_event)
        # A stop during the last read is a cancellation, not a page with skips.
        raise_if_cancelled(budget.cancel)
        raise_if_cancelled(cancel_event)
        listings: Dict[str, Mapping[str, Any]] = {}
        metas: Dict[str, dict] = {}
        cards: Dict[str, dict] = {}
        bases: Dict[str, str] = {}
        for display in displays.values():
            listings.update(display.listings)
            metas.update(display.metas)
            cards.update(display.cards)
            bases.update(display.bases)
        items = []
        for position, candidate in enumerate(page):
            row = candidate.row
            if row["notebook_id"] in skipped:
                continue
            source_id = row["source_id"]
            listing = listings.get(source_id) or {}
            meta = metas.get(source_id)
            passage = candidate.passage
            reasons = ordered_fields(row["matches"]) + (
                (f"passages:{candidate.hits}",) if candidate.hits else ()
            )
            title = source_display_title(meta, {**listing, "id": source_id})
            items.append(FoundSource(
                rank=offset + position + 1,
                source_id=source_id,
                notebook_id=row["notebook_id"],
                title=title,
                pub_year=row["pub_year"],
                venue=str((meta or {}).get("venue") or ""),
                authors=tuple(
                    str(author["name"])
                    for author in (meta or {}).get("authors", [])
                    if str(author.get("name") or "")
                )[:AUTHORS_SHOWN],
                tier=candidate.tier,
                reasons=reasons,
                best_passage=None if passage is None else BestPassage(
                    chunk_id=passage.chunk_id,
                    text=str(passage.text or "")[:BEST_PASSAGE_CHARS],
                    element_ids=tuple(str(e) for e in passage.element_ids or ()),
                ),
                card_status=paper_card_status(
                    None if meta is None else bool(meta.get("is_paper")),
                    row["pending"], cards.get(source_id), bases.get(source_id, ""),
                    card_version=self._card_version,
                ),
            ))
        return tuple(items), skipped
