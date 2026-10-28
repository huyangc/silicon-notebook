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
the lanes with a semantic signal only; on the FTS-degraded lane a passage-only
source needs two hits to be ``related``.  The order is total and deterministic,
so a page is recomputed on every call and the cursor is just an offset plus a
fingerprint of the parameters (any change invalidates it).
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.domain.retrieval import RetrievedChunk
from app.domain.source_find_terms import (
    STRONG_FIELDS,
    fold_term,
    ordered_fields,
)
from app.models.sources import paper_meta_status
from app.services.cancellation import CancelEvent, raise_if_cancelled
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
#: Passages a passage-only source needs on the lexical-only lane.
LEXICAL_LANE_MIN_HITS = 2

_CURSOR_PREFIX = "fs1."


class FindSourcesError(ValueError):
    """The request is malformed (the caller maps it to a refusal)."""


class FindSourcesCursorError(FindSourcesError):
    """The cursor does not belong to these parameters (or is not a cursor)."""


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
    #: ``{notebook_id: lane}`` of the libraries the semantic leg read.
    lanes: Mapping[str, str]


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
    lane: str = ""
    best: Optional[float] = None
    hits: int = 0
    passage: Optional[RetrievedChunk] = None
    tier: str = ""


def paper_card_status(
    meta_row: Mapping[str, Any],
    card: Optional[Mapping[str, Any]],
    basis: str,
    *,
    card_version: int,
) -> str:
    """``card_status`` of one found source (design §5.4 + ``meta_pending``).

    No paper-meta row: ``meta_pending`` for a source eligible for one (the
    metadata is extracted lazily), else ``not_paper``.  A meta row saying "not
    a paper": ``not_paper``.  A paper: ``ready`` / ``failed`` when its card row
    is for the current element ``basis`` and ``card_version``, else
    ``missing`` (absent, stale, or of an older version -- regenerated lazily).
    ``card_version`` is passed in by the caller (the paper card module's
    ``PAPER_CARD_VERSION``) so this judgement has one definition."""
    status = paper_meta_status(
        meta_row.get("is_paper"), meta_row.get("source_type", ""),
        meta_row.get("doc_type", ""), meta_row.get("parse_status", ""),
    )
    if status == "missing":
        return "meta_pending"
    if status != "has_meta":
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
    best: Optional[float],
    hits: int,
    lane: str,
    *,
    strong_line: float,
    related_line: float,
) -> str:
    semantic = lane != LEXICAL_ONLY_LANE and best is not None
    if any(matches.get(field) is True for field in STRONG_FIELDS):
        return "strong"
    if semantic and best >= strong_line:
        return "strong"
    if matches:
        return "related"
    if semantic and best >= related_line:
        return "related"
    if lane == LEXICAL_ONLY_LANE and hits >= LEXICAL_LANE_MIN_HITS:
        return "related"
    return ""


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
        _display_title(candidate.row),
        candidate.row["source_id"],
    )


def _display_title(row: Mapping[str, Any]) -> str:
    """The paper title when metadata has one, else the (presented) title."""
    return str(row["paper_title"] or row["title"])


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
    limit = int(request.limit or FIND_SOURCES_PAGE_SIZE)
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


def encode_cursor(offset: int, fingerprint: str) -> str:
    raw = json.dumps({"o": int(offset), "f": fingerprint}, separators=(",", ":"))
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> int:
    """The offset ``cursor`` carries; ``FindSourcesCursorError`` when it is not
    a cursor of these parameters over this source scope."""
    if not cursor:
        return 0
    if not cursor.startswith(_CURSOR_PREFIX):
        raise FindSourcesCursorError("not a find_sources cursor")
    body = cursor[len(_CURSOR_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        offset = int(data["o"])
        cut_for = str(data["f"])
    except (ValueError, TypeError, KeyError) as exc:
        raise FindSourcesCursorError("not a find_sources cursor") from exc
    if cut_for != fingerprint or offset < 0:
        raise FindSourcesCursorError(
            "the cursor belongs to other parameters; start a new search"
        )
    return offset


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
    ) -> None:
        self._database = database
        self._sources = sources
        self._retrieval = retrieval
        self._enumeration = enumeration
        self._settings = settings
        self._card_version = int(paper_card_version)

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
            offset = decode_cursor(request.cursor, fingerprint)
            scope_rows: Dict[str, List[Dict[str, Any]]] = {}
            for library in libraries:
                raise_if_cancelled(cancel_event)
                scope_rows[library.notebook_id] = self._metadata_rows(
                    db, library, request,
                )
        candidates = {
            row["source_id"]: _Candidate(row=row)
            for rows in scope_rows.values() for row in rows
        }
        lanes: Dict[str, str] = {}
        capped = False
        if request.query:
            for notebook_id, rows in scope_rows.items():
                raise_if_cancelled(cancel_event)
                if rows:
                    lane, library_capped = self._semantic_leg(
                        notebook_id, request.query,
                        [row["source_id"] for row in rows], candidates,
                    )
                    lanes[notebook_id] = lane
                    capped = capped or library_capped
        ranked = self._ranked(candidates, request)
        page = ranked[offset:offset + request.limit]
        items = self._hydrate(page, offset)
        more = offset + request.limit < len(ranked)
        return FindSourcesResult(
            items=items,
            strong=sum(1 for c in ranked if c.tier == "strong"),
            related=sum(1 for c in ranked if c.tier == "related"),
            filtered_scope_total=len(candidates),
            lexical_complete=bool(request.terms) or not request.query,
            semantic_recall_capped=capped,
            next_cursor=encode_cursor(offset + request.limit, fingerprint) if more else "",
            lanes=lanes,
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
        fingerprint."""
        if allowed_source_ids is not None:
            explicit = tuple(dict.fromkeys(
                str(value) for value in allowed_source_ids if str(value or "")
            ))
            return (
                [_Library(active_notebook_id, explicit, None, None)],
                ("explicit", active_notebook_id),
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

    def _metadata_rows(
        self, db: Any, library: _Library, request: FindSourcesRequest,
    ) -> List[Dict[str, Any]]:
        rows = self._sources.find_sources_metadata_rows(
            db, library.notebook_id,
            terms=list(request.terms),
            year_from=request.year_from, year_to=request.year_to,
            venue=request.venue, author=request.author, kind=request.kind,
            allowed_source_ids=(
                None if library.allowed is None else list(library.allowed)
            ),
        )
        # A read without the run's list is verified against the ceiling taken
        # before it (a source outside flips the verdict for later reads); every
        # read is then cut to the plan ``enumerate_sources`` lists, so the two
        # collections are one set by construction.
        verify_unbound_read(
            library.notebook_id, library.unbound,
            (row["source_id"] for row in rows),
        )
        if library.plan_ids is None:
            return rows
        return [row for row in rows if row["source_id"] in library.plan_ids]

    # --------------------------------------------------------- semantic leg
    def _semantic_leg(
        self,
        notebook_id: str,
        query: str,
        source_ids: Sequence[str],
        candidates: Dict[str, _Candidate],
    ) -> Tuple[str, bool]:
        recall = int(self._settings.find_sources_chunk_recall)
        chunks, lane = self._retrieval.retrieve_chunks_in_sources(
            notebook_id, query, recall, list(source_ids),
        )
        for chunk in chunks:
            candidate = candidates.get(str(chunk.source_id))
            if candidate is None or candidate.row["notebook_id"] != notebook_id:
                continue
            candidate.lane = lane
            candidate.hits += 1
            relevance = float(chunk.relevance)
            if (
                candidate.best is None or relevance > candidate.best
                or (relevance == candidate.best
                    and chunk.chunk_id < candidate.passage.chunk_id)
            ):
                candidate.best = relevance
                candidate.passage = chunk
        capped = len(chunks) >= recall and (
            lane == LEXICAL_ONLY_LANE
            or min(float(chunk.relevance) for chunk in chunks)
            >= float(self._settings.find_sources_related_relevance)
        )
        return lane, capped

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
                candidate.row["matches"], candidate.best, candidate.hits,
                candidate.lane, strong_line=strong_line, related_line=related_line,
            )
            if candidate.tier:
                kept.append(candidate)
        return sorted(kept, key=_relevance_order)

    # -------------------------------------------------------------- hydrate
    def _hydrate(
        self, page: Sequence[_Candidate], offset: int,
    ) -> Tuple[FoundSource, ...]:
        """Authors and card status for one page, batched per owning library
        (four bounded reads per library on one connection)."""
        by_notebook: Dict[str, List[str]] = {}
        for candidate in page:
            by_notebook.setdefault(candidate.row["notebook_id"], []).append(
                candidate.row["source_id"]
            )
        metas: Dict[str, dict] = {}
        cards: Dict[str, dict] = {}
        bases: Dict[str, str] = {}
        with self._database.connect() as db:
            for notebook_id, ids in by_notebook.items():
                metas.update(self._sources.paper_meta_for_sources(
                    db, ids, notebook_id=notebook_id))
                papers = [
                    source_id for source_id in ids
                    if metas.get(source_id, {}).get("is_paper")
                ]
                if papers:
                    cards.update(self._sources.paper_cards_for_sources(
                        db, papers, notebook_id=notebook_id))
                    bases.update(self._sources.source_card_bases(db, papers))
        items = []
        for position, candidate in enumerate(page):
            row = candidate.row
            source_id = row["source_id"]
            passage = candidate.passage
            reasons = ordered_fields(row["matches"]) + (
                (f"passages:{candidate.hits}",) if candidate.hits else ()
            )
            items.append(FoundSource(
                rank=offset + position + 1,
                source_id=source_id,
                notebook_id=row["notebook_id"],
                title=_display_title(row),
                pub_year=row["pub_year"],
                venue=row["venue"],
                authors=tuple(
                    str(author["name"])
                    for author in metas.get(source_id, {}).get("authors", [])
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
                    row, cards.get(source_id), bases.get(source_id, ""),
                    card_version=self._card_version,
                ),
            ))
        return tuple(items)
