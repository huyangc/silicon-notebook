"""Answer-side glue for typed-collection enumeration (PR-2 T5, design doc §2.5).

``app.services.collection_enumeration`` is the zero-LLM executor; it never
knows an ``AskResponse`` exists.  ``app.services.reasoning_retrieval`` drives
the reflect loop and collects one ``CollectionEnumerationOutcome`` per
enumerated collection; it never renders prompt text or a wire model either.
This module is the seam between those two closed-out layers (T3/T4) and the
response contract/synthesis prompt (T5): it turns a run's outcomes into

* ``typed_collection_results`` — the ``TypedCollectionResult`` rows that join
  ``AskResponse.result_sets`` alongside Knowhow's ``StructuredKnowhowResult``,
  and the place the documented structured-payload ceiling is enforced against
  the SERIALIZED shape (the executor's own rail weighs a narrower dataclass);
* ``delivered_outcomes`` — outcome views carrying exactly what those rows
  carry, so the synthesis preview below cannot describe a longer list than the
  result card holds;
* ``enumeration_prompt_block`` — the bounded, English, model-facing preview
  spliced into the answer-synthesis evidence block (mirrors
  ``app.services.structured_retrieval.structured_prompt_block``), with its row
  allowance SPLIT across the run's collections rather than handed out
  first-come-first-served;
* ``collection_map_block`` — the run's collection MAP (counts, no rows) wrapped
  for that same evidence block, so the count the reflect prompt tells the model
  to answer with actually reaches the model that writes the answer.

All four are pure functions over what the run already produced — no I/O, no
model calls, no mutation of the outcomes they read.  (``typed_collection_results``
mutates the *rows it just built*, via ``apply_synthesis_preview_counts``, but
never the outcomes.)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, List, Mapping, Sequence

from app.core.ask_retrieval_policy import EXPLICIT_PARTIAL_OVERFLOW
from app.models.ask import (
    TypedCollectionCoverage,
    TypedCollectionItem,
    TypedCollectionResult,
)
from app.services.collection_catalog import COLLECTION_MAP_MAX_CHARS
from app.services.collection_enumeration import (
    MAX_EVIDENCE_REFS,
    TRUNCATED_BUDGET,
    TRUNCATED_CONCURRENT_CHANGE,
    TRUNCATED_PAYLOAD,
    ElementItem,
    EnumerationCoverage,
    KgObjectItem,
    SourceItem,
)

if TYPE_CHECKING:
    from app.services.reasoning_retrieval import CollectionEnumerationOutcome


# ---------------------------------------------------------------------------
# AskResponse.result_sets rows
# ---------------------------------------------------------------------------


def _typed_item(
    raw: object,
    citations_by_item_id: Mapping[str, object] | None = None,
) -> TypedCollectionItem:
    """Project one ``ElementItem``/``KgObjectItem``/``SourceItem`` onto the
    shared wire item.

    ``isinstance`` rather than a ``collection`` string switch: the outcome's
    own ``items`` list is already homogeneous (the executor never mixes the
    two dataclasses in one outcome), and dispatching on the concrete type
    keeps this function correct even if a future outcome's ``collection``
    label and its items' real type ever disagreed — it would raise here
    instead of silently emitting an all-empty row.

    ``evidence_element_ids`` is truncated to ``MAX_EVIDENCE_REFS`` as a second
    line of defense: the executor already bounds it to that width, but the
    wire model's own ``max_length=3`` (kept in literal sync — see
    ``test_max_evidence_refs_parity_between_executor_and_wire_model``) would
    otherwise turn a widened executor value into a 500 instead of a clamp.
    """
    citations = citations_by_item_id or {}
    if isinstance(raw, ElementItem):
        return TypedCollectionItem(
            item_id=raw.element_id,
            source_id=raw.source_id,
            source_title=raw.source_title,
            element_type=raw.element_type,
            location_label=raw.location_label,
            text=raw.text,
            asset_id=raw.asset_id,
            notebook_id=raw.notebook_id,
            tier=raw.tier,
            citation=citations.get(raw.element_id),
        )
    if isinstance(raw, KgObjectItem):
        return TypedCollectionItem(
            item_id=raw.object_id,
            name=raw.name,
            section_path=raw.section_path,
            notebook_id=raw.notebook_id,
            tier=raw.tier,
            evidence_element_ids=list(raw.evidence_element_ids)[:MAX_EVIDENCE_REFS],
            citation=citations.get(raw.object_id),
        )
    if isinstance(raw, SourceItem):
        # A listed document reuses the element arm's fields rather than adding a
        # third set of near-synonyms to the wire union: ``source_title`` is the
        # display title either way, ``text`` is the excerpt either way, and
        # ``location_label`` is the one short "where/what is this" label the card
        # prints under the title — here the document type, already in interface
        # words (see ``_doc_type_label``).  ``element_type`` stays empty: a
        # document is not an element, and the frontend routes on ``collection``.
        return TypedCollectionItem(
            item_id=raw.source_id,
            source_id=raw.source_id,
            source_title=raw.source_title,
            location_label=raw.doc_type_label,
            text=raw.summary,
            notebook_id=raw.notebook_id,
            tier=raw.tier,
            # Keyed by ``source_id`` because that IS this row's identity (the
            # sibling arms key by element/object id).  The citation points at the
            # document itself with an empty ``element_id`` — a document has no
            # sub-location, so it can never be an EXACT original-text locator,
            # which is exactly how the grounding classifier should read it.
            citation=citations.get(raw.source_id),
        )
    raise TypeError(f"unknown collection-enumeration item type: {type(raw)!r}")


def _typed_coverage(raw: EnumerationCoverage) -> TypedCollectionCoverage:
    return TypedCollectionCoverage(
        returned_total=raw.returned_total,
        total=raw.total,
        complete=raw.complete,
        truncated_reason=raw.truncated_reason,
        overflow_semantics=raw.overflow_semantics,
    )


def _wire_chars(model: object) -> int:
    """One model's exact serialized width, in the shape it will travel.

    ``model_dump_json()`` is the same compact, non-escaping serializer the
    response and the persisted answer go through (verified character-for-
    character against ``json.dumps(..., ensure_ascii=False,
    separators=(",", ":"))`` by
    ``test_wire_measure_matches_the_transport_serializer``), so this measures
    the real payload rather than a proxy for it.
    """
    return len(model.model_dump_json())


def typed_collection_results(
    outcomes: Sequence["CollectionEnumerationOutcome"],
    *,
    payload_chars: int,
    citations_by_item_id: Mapping[str, object] | None = None,
) -> List[TypedCollectionResult]:
    """Map one run's enumeration outcomes onto ``AskResponse.result_sets`` rows.

    Order is preserved (action order == outcome order, per
    ``ReasoningResult.enumerations``); the caller appends these after any
    Knowhow ``StructuredKnowhowResult`` rows.

    Every row's ``synthesis_rows``/``synthesis_complete`` stay at their
    defaults (``0``/``None``, meaning "no synthesis preview attempted") —
    this function runs even when the answer model is unconfigured or the
    prompt block is never built.  ``apply_synthesis_preview_counts`` backfills
    them once ``enumeration_prompt_block`` has actually rendered a preview.

    **The documented payload ceiling is enforced HERE, on the wire shape.**
    Two rails, two jobs, and they measure different things on purpose:

    * the executor's own ``EnumerationBudget.max_payload_chars`` (pooled per
      run from ``AskRetrievalLimits.structured_payload_chars``) charges the
      compact executor dataclass.  Its job is to stop the *walk* — to keep a
      traversal from reading and holding more than the request is allowed to
      produce — and it has to be charged before an item is ever mapped;
    * this rail charges ``TypedCollectionItem``, which is what is actually
      serialized, streamed and persisted.  That model is a two-armed union:
      an element row still carries ``name``/``section_path``/
      ``evidence_element_ids`` and a knowledge-object row still carries
      ``source_title``/``location_label``/``text``/``asset_id``, all at their
      defaults, plus each result's own metadata and coverage.  So the wire
      form runs materially wider than the dataclass the executor weighed, and
      an exhaustive run sitting just under the executor's ceiling could ship a
      response well over it.  (Knowhow solves the same problem with a final
      exact serialization-and-trim pass; this is the forward equivalent —
      one serialization per item instead of one per whole-list re-check.)

    Each row's envelope (its metadata + coverage, with an empty ``items``
    list) is reserved BEFORE any item is admitted.  A row's envelope is what
    *discloses* that its list was cut; dropping it to save a few hundred
    characters would remove the disclosure and leave a silently missing card.
    The accounting is a strict upper bound on the final serialized array (it
    charges one separator per row and per item where the real encoding writes
    ``n-1``), so the emitted payload is never larger than it claims.

    When the ceiling stops a row short, THAT ROW's coverage degrades
    honestly: ``complete=False``, ``truncated_reason="payload"``,
    ``overflow_semantics=explicit_partial``, and ``returned_total`` becomes
    the number actually delivered.  Coverage describes the list the user
    holds, not the one the walk produced — the executor's own (larger) figure
    is still in the reasoning trace, where it belongs as cost accounting.
    """
    budget = max(0, int(payload_chars))
    item_citations = citations_by_item_id or {}
    results: List[TypedCollectionResult] = []
    for outcome in outcomes:
        # Explicit per-collection mapping rather than "elements or else": the
        # sources collection has NO sub-type, and an "everything that is not
        # elements is an object type" shortcut would quietly stamp its empty
        # kind into ``object_type`` — a field the frontend reads to pick a label.
        results.append(TypedCollectionResult(
            collection=outcome.collection,
            element_kind=outcome.kind if outcome.collection == "elements" else "",
            object_type=outcome.kind if outcome.collection == "kg_objects" else "",
            source_id=outcome.source_id,
            items=[],
            coverage=_typed_coverage(outcome.coverage),
        ))
    # ``2`` = the enclosing ``[]`` of the serialized array; ``+ 1`` per row and
    # per item = its separator, one more than the encoding actually writes.
    remaining = budget - 2 - sum(_wire_chars(row) + 1 for row in results)
    for result, outcome in zip(results, outcomes):
        trimmed = False
        for raw in outcome.items:
            item = _typed_item(raw, item_citations)
            cost = _wire_chars(item) + 1
            if cost > remaining:
                trimmed = True
                break
            remaining -= cost
            result.items.append(item)
        if trimmed:
            result.coverage.returned_total = len(result.items)
            result.coverage.complete = False
            result.coverage.truncated_reason = TRUNCATED_PAYLOAD
            result.coverage.overflow_semantics = EXPLICIT_PARTIAL_OVERFLOW
    return results


def delivered_outcomes(
    outcomes: Sequence["CollectionEnumerationOutcome"],
    results: Sequence[TypedCollectionResult],
) -> List["CollectionEnumerationOutcome"]:
    """Outcome views that carry exactly what the wire rows carry.

    ``enumeration_prompt_block`` renders both the preview lines and the
    coverage header from an outcome, so feeding it the raw outcomes after
    ``typed_collection_results`` trimmed a row would print rows the result
    card does not hold, under a header that still claims the list is complete
    — the prompt and the card disagreeing about the same list is the exact
    failure this feature's coverage contract exists to prevent.

    Derived, never recomputed: the delivered items are the prefix the mapping
    already admitted (``len(result.items)``) and the four coverage fields the
    wire rail can change are copied straight off the row it produced.  There
    is therefore one definition of the trim, and this is a view of it.  The
    inputs are not mutated — each view is a fresh ``replace``.
    """
    views: List["CollectionEnumerationOutcome"] = []
    for outcome, result in zip(outcomes, results):
        delivered = len(result.items)
        if delivered == len(outcome.items):
            views.append(outcome)
            continue
        views.append(replace(
            outcome,
            items=list(outcome.items[:delivered]),
            coverage=replace(
                outcome.coverage,
                returned_total=result.coverage.returned_total,
                complete=result.coverage.complete,
                truncated_reason=result.coverage.truncated_reason,
                overflow_semantics=result.coverage.overflow_semantics,
            ),
        ))
    return views


def apply_synthesis_preview_counts(
    results: Sequence[TypedCollectionResult],
    shown_rows: Sequence[int],
) -> None:
    """Backfill ``synthesis_rows``/``synthesis_complete`` after the prompt
    block has actually been rendered.

    ``results`` and ``shown_rows`` must be the same length, built from the
    same outcomes list in the same order — the caller (``ask_service``)
    guarantees this by constructing both from one ``enumerations`` list in
    the same try block (never partially, per the "both null on failure"
    contract). Mutates ``results`` in place: these rows were just built by
    ``typed_collection_results`` and have not been handed to a caller yet.
    """
    for result, shown in zip(results, shown_rows):
        result.synthesis_rows = int(shown)
        result.synthesis_complete = (int(shown) == result.coverage.returned_total)


def enumeration_sub_budget(
    *, chunk_context_chars: int, structured_block_len: int
) -> int:
    """The character budget available to ``enumeration_prompt_block`` once a
    Knowhow ``structured_block`` (if any) has already claimed its share of
    ``chunk_context_chars``. Three layers:

    1. Subtract the Knowhow block's own length.
    2. Subtract the 2-char ``"\\n\\n"`` joiner the caller inserts between the
       Knowhow block and the enumeration block — but ONLY when the Knowhow
       block is non-empty, since that joiner is never written when there is
       nothing to join. Omitting this term is exactly how two blocks each
       sized right up to their own ceiling overflow the combined
       ``chunk_context_chars`` by 2 characters once concatenated.
    3. Cap the result to half of ``chunk_context_chars`` regardless of how
       much headroom step 1-2 leaves, so an enumeration action can never
       crowd out the OTHER half of the evidence budget (chunks/elements)
       reserved for whatever else the question needs.
    """
    reserved = int(structured_block_len) + (2 if structured_block_len else 0)
    remaining = max(0, int(chunk_context_chars) - reserved)
    return min(remaining, int(chunk_context_chars) // 2)


# ---------------------------------------------------------------------------
# Synthesis prompt block
# ---------------------------------------------------------------------------

# English labels for the reasons a partial list stopped, mirroring
# ``structured_prompt_block``'s coverage header vocabulary. Concurrent-change
# and "denominator unknown" are handled as their own branches below (not a
# generic reason label) because both need bespoke wording, not just a noun.
_REASON_LABELS = {
    TRUNCATED_BUDGET: "run budget",
    TRUNCATED_PAYLOAD: "payload limit",
}

# Prompt-line clamp, distinct from the executor's own transport excerpt
# (``collection_enumeration.DEFAULT_EXCERPT_CHARS`` = 1,000, which still
# governs ``TypedCollectionItem.text`` for the result card). This constant
# only shortens what actually enters one preview LINE of the LLM prompt —
# a much tighter budget than the card copy, because the prompt already pays
# for every row again via ``inline_rows``/``budget_chars``.
_PROMPT_LINE_EXCERPT_CHARS = 200

# Dedicated namespace for deterministic collection rows.  Existing answer
# evidence uses k1+, k1001+, k2001+, k3001+ and k4001+; collection previews
# start at k5001 so their reverse bindings cannot collide with any ranked
# retrieval producer.
COLLECTION_KEY_BASE = 5000

# What a document with no display name is called, in the prompt preview and on
# the result card alike (``answer-panel.tsx`` renders the same words).  Kept as
# one constant on this side so the two never drift into "未命名来源" on screen and
# a raw source id in the prompt — the model quotes what it is given.
UNNAMED_SOURCE_LABEL = "未命名来源"

# A single, one-time reminder that the preview is a SUBSET of what was
# listed. Mirrors ``structured_prompt_block``'s coverage-header instruction
# sentence, but is not repeated per outcome (one enumeration_prompt_block
# call can carry several outcome headers; the disclosure rule is the same
# for all of them, so it is stated once up front instead of N times).
_INSTRUCTION_LINE = (
    "[Enumeration preview note: base analysis only on each block's "
    "\"previewed\" count, never its \"listed\" count. When previewed is "
    "less than listed, explicitly disclose that the analysis covers only "
    "that subset. Every preview row has a kN id: cite a row with its own [kN] "
    "marker whenever the answer uses it — the full list is authoritative in "
    "the result card, not this preview.]"
)

# Adaptive granularity for a DOCUMENT roster, emitted only when this run
# actually carries one (design doc §6.2).  It sits here rather than in
# ``answer_prompt``'s enumeration rules for two reasons: the roster is the only
# listing whose right answer shape changes with its size, and a rule that lives
# in ``answer_prompt`` would be paid by every synthesis in the product, most of
# which never see a document list.  Emitted next to the roster, it costs
# nothing when there is no roster and it reads as guidance about the block
# immediately below it.
#
# NO numeric threshold, on purpose: the model already has the exact count (from
# this block's header and from the collection map) and it is the only party that
# knows how much the question wants per document.  A hard-coded "more than N ⇒
# summarize" would be lexical routing under another name, and it would be wrong
# in both directions — five dense papers can want a thematic answer while
# thirty one-page notes can want a line each.
_SOURCE_GRANULARITY_LINE = (
    "[Document roster guidance: choose the granularity from how many documents "
    "are listed against how much the question wants about each. A roster small "
    "enough to treat individually should be answered document by document, each "
    "with its own titled passage. A roster too large for that should be "
    "organized by THEME instead — group the documents into a few dimensions the "
    "titles and summaries actually share, name the documents belonging to each, "
    "and say plainly that the answer is organized thematically rather than one "
    "document at a time. Either way, name every document you draw on and never "
    "silently drop part of the roster.]"
)


# Header for the deterministic count line (the collection MAP, not a list).
# Two things it must say and nothing more: the numbers are server-computed and
# exact, and they are quotable without a [k] marker.  The second half matters
# because Rule 11 otherwise forbids an uncited claim — the same markerless
# endorsement the enumeration preview's coverage header carries, for the same
# reason: no per-row citation key exists to attach.
_COLLECTION_MAP_HEADER = (
    "[Collection counts — computed by the server, exact. Quote them WITHOUT a "
    "[k] marker. They say how many items EXIST in scope, not how many were "
    "retrieved; private Memory is never counted.]"
)

# Hard ceiling on the whole block, DERIVED rather than picked: a fixed header
# plus a map line the catalog already caps at ``COLLECTION_MAP_MAX_CHARS``.
# Pinned by ``test_collection_map_block_stays_bounded`` so a longer header or a
# raised map cap has to be a deliberate change, not a silent one.
COLLECTION_MAP_BLOCK_MAX_CHARS = (
    len(_COLLECTION_MAP_HEADER) + 1 + COLLECTION_MAP_MAX_CHARS
)


def collection_map_block(collection_map_text: str) -> str:
    """The map, wrapped for the answer-synthesis evidence block.

    The reflect prompt tells the model that a collection far larger than the
    run's listing allowance should NOT be paged through — it should be answered
    with the map's count instead.  The answer-synthesis model is a different
    call with a different context: it sees retrieved evidence and enumeration
    previews, and never saw the map.  Without this block that instruction asks
    for a number nobody supplied, and in the extreme case (a big collection, no
    other evidence) synthesis would not even run (codex round-4 P2).

    Deterministic server output, so it carries no ``[k]`` id and none can be
    invented for it; hard-bounded at ``COLLECTION_MAP_BLOCK_MAX_CHARS`` (the
    map line is clamped here rather than merely assumed to be short — the
    catalog's renderer already caps it, but this block rides in EVERY answer
    prompt and a ceiling that depends on another module keeping its promise is
    not a ceiling); empty in, empty out — a run without the enumeration tools,
    or whose map failed to build, injects nothing and behaves exactly as it did
    before.
    """
    text = str(collection_map_text or "").strip()[:COLLECTION_MAP_MAX_CHARS]
    if not text:
        return ""
    return f"{_COLLECTION_MAP_HEADER}\n{text}"


def _collection_noun(collection: str) -> str:
    if collection == "elements":
        return "elements"
    if collection == "sources":
        return "documents"
    return "objects"


def _coverage_phrase(outcome: "CollectionEnumerationOutcome", *, previewed: int) -> str:
    """The bracketed coverage header line for one outcome.

    Branch order matters and is the T3/T4 handoff contract restated as text:

    1. ``concurrent_change`` is a terminal, sui-generis state (§2.3/§2.4):
       the collection can neither continue nor be re-run, so it gets its own
       wording rather than falling into "partial" — collapsing it there is
       exactly the regression the mutation suite pins down.
    2. ``total is None`` ("denominator unknown") is checked before
       ``complete`` because a cursor can exhaust with a genuinely unknown
       denominator (``test_kg_total_is_omitted_when_the_map_cannot_answer``)
       — printing "N/None" or silently treating that as "N/0" would both be
       worse than naming the gap. ``complete`` is still stated explicitly
       even when the denominator is unknown: a card whose badge says
       "listed all" must not sit next to prompt text that only says
       "denominator unknown" and drops the word "complete" — that reads as
       self-contradictory (card says done, prompt hedges). When it is not
       complete, the truncation reason is kept alongside "denominator
       unknown": an unknown denominator must never swallow the fact that a
       budget ceiling, not the collection's end, stopped the walk.
    3. Otherwise a plain complete/partial-with-reason line, denominator
       included.

    Every branch appends ``, previewed {previewed}`` — the number of rows
    that actually made it into THIS prompt (bounded separately from
    ``listed``/``total`` by ``inline_rows``/``budget_chars``), because
    Rule 11 requires the model to base analysis on what it actually saw,
    not on the coverage claim alone.
    """
    coverage = outcome.coverage
    # The sources collection has no sub-type, so its label is the noun alone —
    # ``f"{kind} {noun}"`` with an empty kind would emit a leading space and
    # read as a truncated field rather than as "the documents".
    noun = _collection_noun(outcome.collection)
    label = f"{outcome.kind} {noun}" if outcome.kind else noun
    listed = coverage.returned_total
    suffix = f", previewed {previewed}"
    if coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE:
        return (
            f"[Enumeration: {label}, listed {listed}, "
            f"INTERRUPTED by concurrent change — completeness unknown{suffix}]"
        )
    if coverage.total is None:
        if coverage.complete:
            return (
                f"[Enumeration: {label}, listed {listed}, complete, "
                f"denominator unknown{suffix}]"
            )
        reason_label = _REASON_LABELS.get(
            coverage.truncated_reason, coverage.truncated_reason or "unknown"
        )
        return (
            f"[Enumeration: {label}, listed {listed}, "
            f"partial: {reason_label}, denominator unknown{suffix}]"
        )
    if coverage.complete:
        return (
            f"[Enumeration: {label}, listed {listed}/{coverage.total}, "
            f"complete{suffix}]"
        )
    reason_label = _REASON_LABELS.get(
        coverage.truncated_reason, coverage.truncated_reason or "unknown"
    )
    return (
        f"[Enumeration: {label}, listed {listed}/{coverage.total}, "
        f"partial: {reason_label}{suffix}]"
    )


def _clean(text: object) -> str:
    """Collapse a possibly multi-line value onto one line.

    ``code_block``/``table`` element text routinely contains newlines; left
    alone, one item would spill across several preview lines and break both
    the visual row structure and the "(+N more rows)" accounting (which
    counts preview LINES, not items).
    """
    return " ".join(str(text).split())


def _item_line(collection: str, item: object, key: str) -> str:
    """One preview row.

    Deliberately avoids ANY ``[...]`` wrapper around title/location/section:
    a bracketed bare number — an ordinary ``section_path`` like ``"1"`` or
    ``"2.3"`` is the common case for a KG object — reads as a ``[k]``/``[3]``
    citation marker to the frontend's citation regex, turning a plain list
    row into a clickable-but-bogus reference. ``·``/``:`` take over the
    separator role instead. (The coverage header above still uses
    ``[...]``; that is safe because its bracket body is always multi-word
    prose, never a bare number.)
    """
    if collection == "elements":
        text = _clean(item.text)[:_PROMPT_LINE_EXCERPT_CHARS]
        return f"{key}: [enumerated-source-element] {item.source_title} · {item.location_label}: {text}"
    if collection == "sources":
        # Tag follows the sibling arms' naming: an element row is an element OF a
        # source, a KG row is an object of its type, and this row IS a source.
        # Title first — it is the handle the model uses to ask for a deeper pass
        # on one document (an ordinary add_subquery on that title) — then the
        # document type, then whatever summary the library stored.  An untyped or
        # unsummarized document still gets a row: "this document exists" is the
        # fact the listing is for.
        # Field names are ``SourceItem``'s, not ``TypedCollectionItem``'s: this
        # function renders the EXECUTOR's dataclasses (the wire projection
        # happens elsewhere, from the same items).
        summary = _clean(item.summary)[:_PROMPT_LINE_EXCERPT_CHARS]
        # A nameless document falls back to the SAME neutral placeholder the
        # result card shows, not to its internal id.  Two reasons the id is
        # wrong here: the model would quote it back as a title (the roster's
        # whole purpose is to give it titles to deepen by name, and an id is a
        # name that matches nothing), and internal ids are not interface copy —
        # they leak into an answer the moment the model echoes the line.
        parts = [str(item.source_title or UNNAMED_SOURCE_LABEL)]
        if item.doc_type_label:
            parts.append(str(item.doc_type_label))
        line = " · ".join(parts)
        head = f"{key}: [enumerated-source] {line}"
        return f"{head}: {summary}" if summary else head
    location = item.section_path or "—"
    name = _clean(item.name)[:_PROMPT_LINE_EXCERPT_CHARS]
    return f"{key}: [enumerated-{item.object_type}] {name} · {location}"


def _outcome_block(
    outcome: "CollectionEnumerationOutcome",
    *,
    shown: int,
    key_start: int = COLLECTION_KEY_BASE + 1,
) -> str:
    """Render one outcome's header + up to ``shown`` item lines.

    A pure sizing/rendering helper: ``enumeration_prompt_block`` calls this
    with successively smaller ``shown`` values (never mutating the outcome)
    until the result fits its remaining budget, so the header's own
    ``previewed`` number is always the TRUE final count — never patched
    after the fact.
    """
    lines = [_coverage_phrase(outcome, previewed=shown)]
    if outcome.source_id:
        title = (
            outcome.items[0].source_title
            if outcome.items and outcome.collection == "elements"
            else outcome.source_id
        )
        lines.append(f"[scope: single source {title}]")
    for offset, item in enumerate(outcome.items[:shown]):
        lines.append(_item_line(outcome.collection, item, f"k{key_start + offset}"))
    omitted = len(outcome.items) - shown
    if omitted > 0:
        lines.append(f"(+{omitted} more rows in the result card)")
    return "\n".join(lines)


@dataclass
class EnumerationPreview:
    """Result of ``enumeration_prompt_block``: the rendered text plus how
    many rows of EACH outcome actually made it in.

    ``shown_rows`` is parallel to the ``outcomes`` sequence passed in (same
    order, same length) — the caller zips it with
    ``typed_collection_results(outcomes)`` via ``apply_synthesis_preview_counts``
    to backfill each result row's ``synthesis_rows``/``synthesis_complete``.
    """

    text: str = ""
    shown_rows: List[int] = field(default_factory=list)
    evidence_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)


def _preview_evidence(
    collection: str,
    item: object,
    citation: object | None,
) -> dict[str, Any]:
    """Build the reverse binding for one row that actually entered synthesis."""
    if collection == "elements":
        object_id = str(item.element_id)
        object_type = "element"
        name = str(item.location_label or item.source_title)
        definition = str(item.text)
    elif collection == "sources":
        # A cited document row binds to the DOCUMENT, and ``object_id`` stays
        # empty on purpose: that field is the knowledge-graph handle, and a
        # source is not a graph object.  Leaving it empty is what makes the
        # reference detail's graph button correctly report "this citation is not
        # bound to a knowledge object" instead of offering a lookup that cannot
        # resolve.  The locator the row DOES have (its ``source_id``) arrives
        # below from the citation, which is what "查看原文" needs.
        object_id = ""
        object_type = "source"
        name = str(item.source_title or item.source_id)
        definition = str(item.doc_type_label or "")
    else:
        object_id = str(item.object_id)
        object_type = str(item.object_type)
        name = str(item.name)
        definition = str(getattr(citation, "quoted_span", "") or "")
    source_id = str(getattr(citation, "source_id", "") or "")
    element_id = str(getattr(citation, "element_id", "") or "")
    source_title = str(
        getattr(citation, "label", "").rsplit(" · ", 1)[0]
        if citation else getattr(item, "source_title", "") or ""
    )
    location = str(
        getattr(citation, "location_label", "")
        if citation else getattr(item, "location_label", "") or ""
    )
    snippet = str(
        getattr(citation, "quoted_span", "")
        if citation else getattr(item, "text", "") or ""
    )
    return {
        "object_id": object_id,
        "object_type": object_type,
        "name": name,
        "definition": definition,
        "snippet": snippet[:300],
        "source_id": source_id,
        "element_id": element_id,
        "source_title": source_title,
        "location_label": location,
        "tier": str(getattr(item, "tier", "personal") or "personal"),
        "notebook_id": str(getattr(citation, "notebook_id", "") or ""),
        "relevance": 0.0,
        "provenance": {
            "producer": "collection_enumeration",
            "authority": "deterministic",
        },
        "knowhow": getattr(citation, "knowhow", None) if citation else None,
    }


def _row_quota(
    outcomes: Sequence["CollectionEnumerationOutcome"], inline_rows: int
) -> List[int]:
    """Split the shared ``inline_rows`` allowance across the run's outcomes.

    Two passes, and the first one is the whole point.  Handing the allowance
    out first-come-first-served lets outcome #1 take all of it whenever it
    holds at least ``inline_rows`` items — which a real enumeration usually
    does — leaving every later collection with ``previewed 0``.  A
    multi-collection or hybrid question then gets an answer synthesized from
    the first list alone, while the other cards sit in the response unread:
    the precise starvation the shared (rather than per-outcome) allowance was
    introduced to avoid.

    Pass 1 reserves ``max(1, inline_rows // n)`` for each outcome, clamped by
    what it actually holds and by what is left (so more outcomes than rows
    still degrades gracefully instead of over-committing).  Pass 2 hands the
    remainder out greedily in outcome order, so a small collection never
    wastes the quota it cannot fill and the leftovers still go to the biggest
    list first.

    A single outcome is unchanged by construction: ``floor`` becomes the whole
    allowance, pass 1 gives it everything it can hold, and pass 2 has nothing
    left to move.
    """
    count = len(outcomes)
    left = max(0, int(inline_rows))
    quota = [0] * count
    if not count or left <= 0:
        return quota
    floor = max(1, left // count)
    for index, outcome in enumerate(outcomes):
        take = min(len(outcome.items), floor, left)
        quota[index] = take
        left -= take
    for index, outcome in enumerate(outcomes):
        if left <= 0:
            break
        take = min(len(outcome.items) - quota[index], left)
        quota[index] += take
        left -= take
    return quota


def enumeration_prompt_block(
    outcomes: Sequence["CollectionEnumerationOutcome"],
    *,
    inline_rows: int,
    budget_chars: int,
    citations_by_item_id: Mapping[str, object] | None = None,
) -> EnumerationPreview:
    """Bound the enumeration preview injected into answer synthesis.

    Mirrors ``structured_prompt_block``: a coverage header the model can
    quote, followed by item lines, both bounded — ``inline_rows`` shared
    across every outcome in this run and split by ``_row_quota`` so one giant
    list cannot starve every other collection's preview, ``budget_chars`` a
    hard character ceiling the returned text never exceeds (every join
    separator is charged against it too, not just the lines themselves).

    Header-over-items priority: for each outcome this tries the largest
    ``shown`` (bounded by the remaining row quota and outcome size) that
    still fits the remaining budget, counting DOWN to zero rather than
    truncating an already-committed line — so the header (with an accurate
    ``previewed`` count) always wins over any item line, and an outcome that
    cannot even fit its bare header is skipped entirely rather than emitting
    a half-written fragment. The leading instruction sentence is tried first
    but is not load-bearing: a budget too small even for that one sentence
    still lets outcome headers try.

    Every admitted item line receives a key in the isolated k5001+ namespace
    and a matching reverse-map entry.  Coverage headers remain deterministic
    server metadata and therefore do not pretend to be source citations.
    """
    if not outcomes:
        return EnumerationPreview(text="", shown_rows=[])
    budget = max(0, int(budget_chars))
    quota = _row_quota(outcomes, inline_rows)
    blocks: List[str] = []
    used = 0
    shown_rows: List[int] = [0] * len(outcomes)
    evidence_by_id: dict[str, dict[str, Any]] = {}
    item_citations = citations_by_item_id or {}
    next_key = COLLECTION_KEY_BASE + 1

    def room() -> int:
        return budget - used

    def try_commit(text: str) -> bool:
        nonlocal used
        joiner_cost = 2 if blocks else 0
        if len(text) + joiner_cost > room():
            return False
        blocks.append(text)
        used += len(text) + joiner_cost
        return True

    try_commit(_INSTRUCTION_LINE)
    # Only when a document roster is actually in this run — see the constant.
    # Tried after the disclosure note and, like it, not load-bearing: a budget
    # too small for it still lets the outcome blocks themselves through, because
    # a list without its granularity hint is far better than a hint without its
    # list.
    if any(outcome.collection == "sources" for outcome in outcomes):
        try_commit(_SOURCE_GRANULARITY_LINE)

    for index, outcome in enumerate(outcomes):
        for shown in range(quota[index], -1, -1):
            candidate = _outcome_block(
                outcome, shown=shown, key_start=next_key
            )
            if try_commit(candidate):
                shown_rows[index] = shown
                for offset, item in enumerate(outcome.items[:shown]):
                    key = f"k{next_key + offset}"
                    item_id = str(
                        getattr(item, "element_id", "")
                        or getattr(item, "object_id", "")
                        or ""
                    )
                    evidence_by_id[key] = _preview_evidence(
                        outcome.collection, item, item_citations.get(item_id)
                    )
                next_key += shown
                break
        # Falling through the loop without committing (even at shown=0) means
        # this outcome's bare header does not fit in what remains; it is
        # skipped entirely rather than truncated mid-line, and the next
        # outcome — which may be smaller — still gets its own attempt.
        # Rows an outcome could not fit into the CHARACTER budget are not
        # re-donated: at that point the block is character-bound, not
        # row-bound, so the next outcome could not spend them either.

    return EnumerationPreview(
        text="\n\n".join(blocks),
        shown_rows=shown_rows,
        evidence_by_id=evidence_by_id,
    )
