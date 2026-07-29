"""Exact-identifier fast path: locate a section precisely, then fetch it whole.

Motivation. A reference manual's `set_db` section is split by the 600-char
chunker into main description / Arguments / Examples. Ordinary retrieval scores
those three independently and keeps whichever one wins, so "set_db 命令是怎样的"
gets the prose and loses the parameter table. The fix is not better ranking —
it is a different question: *which section is this*, answered by exact
substring match, followed by *give me all of it*.

Cost contract. The channel itself makes zero model calls and zero embeddings;
downstream, the mix path does feed its chunks into the one existing rerank
call (at most max_sections x max_chunks_per_section extra documents there).
Every query is bounded (`fts_k`, `max_sections`, `max_chunks_per_section`),
and `exact_probe_terms` is the master switch: a query with no probe-worthy
name does no I/O at all, so the overwhelming majority of asks pay literally
nothing for this module. That gate is deliberately narrower than the lexical
layer's recall-side `identifier_terms` — see its docstring. Scoped to the active
notebook only — mounted base libraries are deliberately out of scope (manuals
live in the user's own library).

Layering mirrors `chunking.py` / `mmr.py`: the logic here is pure, and every
database touch arrives as an injected callable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Sequence

from app.repositories.lexical_query import exact_probe_terms
from app.services.retrieval import (
    RetrievalSupport,
    RetrievedChunk,
    keyword_score,
)


@dataclass(frozen=True)
class ExactLookupLimits:
    """Every bound this path obeys, in one place (all from Settings)."""

    max_identifiers: int = 3
    fts_k: int = 50
    max_sections: int = 3
    max_chunks_per_section: int = 12


@dataclass(frozen=True)
class ExactLookupDeps:
    """The four store seams, injected — this module owns no SQL and no facade.

    ``exact_search(db, notebook_id, needle, k)`` →
        ``[{chunk_id, source_id, section_path, score, ...}]``
    ``section_rows(db, notebook_id, source_id, section_path, limit)`` →
        chunk rows (``id, source_id, text, section_path, element_ids,
        source_title``) in document order.
    ``chunk_rows(db, chunk_ids)`` →
        the SAME row shape, addressed by primary key. Needed because "take the
        whole section" is only meaningful when the library HAS sections; see
        `exact_lookup_chunks` for the MinerU case where it does not.
    """

    connect: Callable[[], ContextManager[Any]]
    exact_search: Callable[[Any, str, str, int], Sequence[Any]]
    section_rows: Callable[[Any, str, str, str, int], Sequence[Any]]
    chunk_rows: Callable[[Any, Sequence[str]], Sequence[Any]]


@dataclass(frozen=True)
class _SectionKey:
    source_id: str
    section_path: str


def _element_ids(raw: Any) -> list[str]:
    """Both adapters hand back the JSON string form (PostgreSQL normalises its
    jsonb through `_compat_element_ids`); tolerate the decoded list anyway."""
    if isinstance(raw, list):
        return [str(value) for value in raw]
    try:
        parsed = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(value) for value in parsed] if isinstance(parsed, list) else []


def _identifier_named(section_path: str, terms: Sequence[str]) -> bool:
    """True when an identifier appears anywhere in the breadcrumb.

    This is the cheap "is this node inside the identifier's scope" test that
    gates subtree expansion: `Manual > Commands > set_db` and its sub-section
    `... > set_db > Notes` both qualify (an identifier in any segment means
    the subtree lives under an identifier-titled heading, so expansion stays
    command-scoped), while a document root like `Manual` — hit only because
    its intro/TOC prose lists command names — does not, so its subtree (the
    entire document) is never pulled in wholesale.
    """
    haystack = section_path.lower()
    return any(term.lower() in haystack for term in terms)


def _group_path(section_path: str, terms: Sequence[str]) -> str:
    """The shortest identifier-named prefix of `section_path`, else the path.

    This is the unit a slot is spent on. `Commands > set_db`,
    `... > set_db > Arguments` and `... > set_db > Examples` are three
    breadcrumbs but ONE command, and the subtree fetch below returns all of
    them from the ancestor alone — so they must compete for one slot, not
    three.

    Prefixes are accumulated segment by segment and the first
    identifier-named one wins, which deliberately picks the shallowest node
    still inside the identifier's scope: taking `Commands > set_db` rather
    than its Arguments leaf is what "fetch the whole command" means. When no
    prefix is identifier-named the path is its own group — a document root
    whose intro/TOC prose merely lists command names never becomes anyone's
    subtree root.
    """
    segments = section_path.split(" > ")
    for end in range(1, len(segments) + 1):
        prefix = " > ".join(segments[:end])
        if _identifier_named(prefix, terms):
            return prefix
    return section_path


def _rank_groups(
    hits_by_group: dict[_SectionKey, dict[str, None]],
    max_sections: int,
) -> list[_SectionKey]:
    """Most-hit groups first; ties broken by the SHORTEST breadcrumb.

    Hit counts are compared per GROUP, which is the whole point: the previous
    per-breadcrumb ranking let one command's sub-sections take every slot.
    Measured shape — `set_db > Arguments` 4 hits, `set_db > Examples` 3,
    `set_db` itself 2, `report_timing` 2, three slots: the first three keys
    were all the same command and `report_timing` was dropped entirely, even
    though a subtree fetch of `set_db` alone would have returned all of them.
    Folded, that question spends one slot per command and both survive. "A
    command's sub-sections out-number its main section" is the normal shape of
    a reference manual, not an edge case.

    Character length is the tie-break between unrelated groups (merely
    deterministic, and mildly prefers the shallower/more general node); the
    remaining keys keep the order stable across backends.
    """
    ordered = sorted(
        hits_by_group,
        key=lambda key: (
            -len(hits_by_group[key]),
            len(key.section_path),
            key.section_path,
            key.source_id,
        ),
    )
    return ordered[: max(0, max_sections)]


def exact_lookup_chunks(
    deps: ExactLookupDeps,
    notebook_id: str,
    query: str,
    limits: ExactLookupLimits,
) -> list[RetrievedChunk]:
    """Return the chunks of every section an identifier in `query` names.

    Empty list — and, crucially, **zero store calls** — when the query holds no
    probe-worthy identifier. That is the whole neutrality guarantee for the
    other 95% of questions, so it is asserted directly by a stub-based test
    rather than inferred.

    Hits are folded onto their command (`_group_path`) before slots are handed
    out, then each group is fetched one of two ways:

    * **Identifier-named group** — subtree fetch from the group root. This is
      the "give me all of it" the whole module exists for, and it needs the
      library to actually carry breadcrumbs.
    * **Anything else** — hydrate the matched chunk ids directly. A section
      *path* is not a usable address in every library: MinerU-parsed PDFs
      produce no breadcrumbs at all, so a manual's several dozen bare
      `Arguments` headings all share one section key, and re-querying by
      (source, path) under a LIMIT returned the first 12 of them — the probe
      had found the parameter table and then the channel itself threw it away.
      Addressing the hits by primary key cannot lose them.

    Known anchoring limit (codex #399 round 2, deferred): the probe searches
    chunk TEXT only, and the scored text carries just the tail-two breadcrumb
    segments. A command's own chunks (`[Commands > set_db]`) and its child
    chunks (`[set_db > Arguments]`) both carry the name, so the section
    anchors in every ordinary shape; the miss needs a command whose section
    AND child sections hold zero prose while only grandchild chunks
    (`[Arguments > Required]`) exist and the body never names the command.
    Fixing that requires section_path to join the FTS/trigram index (schema
    migration + backfill) — an unindexed LIKE over `chunks.section_path`
    would be an unbounded per-notebook scan, which this module must not do.
    Pinned by test_probe_anchors_via_text_only_so_prose_free_grandchild_
    sections_are_not_found.
    """
    terms = exact_probe_terms(query)[: max(0, limits.max_identifiers)]
    if not terms or limits.max_sections <= 0 or limits.max_chunks_per_section <= 0:
        # Zero-IO gates: no identifier, or the section budget is configured
        # away — either way no probe is worth issuing.
        return []

    # dict-as-ordered-set, never a plain set: the non-named branch truncates
    # this collection to `max_chunks_per_section`, and slicing a set would make
    # *which* hits survive depend on string hash randomisation.
    hits_by_group: dict[_SectionKey, dict[str, None]] = {}
    rows: list[Any] = []
    with deps.connect() as db:
        for term in terms:
            for hit in deps.exact_search(db, notebook_id, term, limits.fts_k):
                source_id = str(hit["source_id"] or "")
                section_path = str(hit["section_path"] or "")
                if not source_id or not section_path:
                    # A chunk outside any heading has no section to complete;
                    # ordinary retrieval already covers it.
                    continue
                # Group by the term THAT PRODUCED this hit, not the full term
                # list: with both `set_db` and `config.yaml` in the question, a
                # hit under `set_db > config.yaml` must keep its own slot — the
                # full-list fold would collapse it onto `set_db`, whose bounded
                # subtree fetch may exhaust before ever reaching it.
                group = _SectionKey(
                    source_id=source_id,
                    section_path=_group_path(section_path, [term]),
                )
                hits_by_group.setdefault(group, {})[str(hit["chunk_id"])] = None
        if not hits_by_group:
            return []
        for group in _rank_groups(hits_by_group, limits.max_sections):
            if _identifier_named(group.section_path, terms):
                rows.extend(deps.section_rows(
                    db,
                    notebook_id,
                    group.source_id,
                    group.section_path,
                    limits.max_chunks_per_section,
                ))
            else:
                hit_ids = list(hits_by_group[group])[
                    : limits.max_chunks_per_section]
                rows.extend(_ordered_by_id(
                    deps.chunk_rows(db, hit_ids), hit_ids))

    return _build_chunks(rows, terms)


def _ordered_by_id(rows: Sequence[Any], chunk_ids: Sequence[str]) -> list[Any]:
    """Put hydrated rows back into the requested order.

    A primary-key `IN` query returns rows in whatever order the backend's plan
    produces — SQLite by id (random 128-bit surrogates), PostgreSQL by
    `ordinal`. Neither is a contract, and a channel whose output feeds a
    character budget must not shuffle between backends. The requested order is
    probe order, i.e. each backend's own exact-search ranking, so the strongest
    hits also sit ahead of the budget cut.
    """
    by_id = {str(row["id"]): row for row in rows}
    return [by_id[cid] for cid in chunk_ids if cid in by_id]


def _build_chunks(
    rows: Sequence[Any], terms: Sequence[str]
) -> list[RetrievedChunk]:
    """Score keyword-only and deduplicate, preserving section/document order.

    Three deliberate choices:

    * **Scored against the probed NAMES, not the caller's query.** This is a
      producer-native score — the same philosophy as PPR, whose normalised
      score is not "relevance to the raw question" either — and it is what the
      channel's admission criterion actually measures: coverage of the name
      that addressed this section. Scoring against the raw question instead
      made the two callers disagree about the same evidence: the mix path
      passed the whole sentence and got 0.2857 (under `tau_high=0.35`, so a
      correctly grounded answer whose only anchor was this chunk was reported
      as "overview"), while the reasoning path already passed the joined names
      and got 1.0. One channel, one meaning.
    * **Keyword-only, no fabricated semantic component.** These chunks were
      never embedded against the query, so `relevance` is honest keyword
      coverage in [0,1]. The haystack is `section_path + text`: the breadcrumb
      IS exactly-matched addressable content (an `Arguments` sibling in a
      breadcrumb library carries the command name only in its path), so
      counting it keeps a genuinely-matched sibling off 0.0 without polluting
      any general scoring path — this scoring is local to the exact channel.
      A 0-relevance citation would otherwise drag `anchored_rel` (a max, so it
      can only be the answer's SOLE anchor) below tau and flip a perfectly
      grounded answer to "overview".
    * **No RELEVANCE_FLOOR filter**, unlike `score_chunks`. The floor is a
      fuzzy-recall filter and this channel's admission criterion is not fuzzy:
      the chunk belongs to a section an identifier matched exactly. Coverage
      fraction can still dip below the floor despite a genuine match — a long
      Chinese question against a terse parameter table leaves most query
      tokens unmatched — and dropping exactly those siblings is what this
      feature exists to prevent. The bound that keeps this safe is structural
      (max_sections x max_chunks_per_section), not score-based.

    The support is recorded as ordinary `lexical` — which it truthfully is, a
    substring match. It deliberately does not mint a new `RetrievalSupport`
    origin: `is_graph_only_chunk` and the reserve logic read that vocabulary,
    and "this came from the exact channel" is carried to the selector as an
    explicit chunk-id set instead, where it cannot perturb existing consumers.

    Downstream consumers are unaffected by the scale of this score in the ways
    that would matter: mix's rerank decides the final order and the reserve
    admits by chunk-id set, while `quota_fuse` sorts only *within* a producer's
    own group — where a flat 1.0 degenerates to insertion order, i.e. document
    order, which is the order this channel wants anyway.
    """
    probe = " ".join(terms)
    chunks: list[RetrievedChunk] = []
    seen: set[str] = set()
    for row in rows:
        chunk_id = str(row["id"])
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        text = row["text"] or ""
        section_path = row["section_path"] or ""
        haystack = f"{section_path} {text}" if section_path else text
        relevance = keyword_score(probe, haystack)
        chunks.append(
            RetrievedChunk(
                chunk_id=chunk_id,
                source_id=str(row["source_id"]),
                source_title=row["source_title"] or "",
                section_path=row["section_path"] or "",
                text=text,
                element_ids=_element_ids(row["element_ids"]),
                score=relevance,
                relevance=relevance,
                retrieval_supports=(
                    RetrievalSupport("lexical", "chunk", chunk_id, relevance),
                ),
            )
        )
    return chunks
