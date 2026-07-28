"""Answer-side glue for typed-collection enumeration (PR-2 T5, design doc §2.5).

``app.services.collection_enumeration`` is the zero-LLM executor; it never
knows an ``AskResponse`` exists.  ``app.services.reasoning_retrieval`` drives
the reflect loop and collects one ``CollectionEnumerationOutcome`` per
enumerated collection; it never renders prompt text or a wire model either.
This module is the seam between those two closed-out layers (T3/T4) and the
response contract/synthesis prompt (T5): it turns a run's outcomes into

* ``typed_collection_results`` — the ``TypedCollectionResult`` rows that join
  ``AskResponse.result_sets`` alongside Knowhow's ``StructuredKnowhowResult``;
* ``enumeration_prompt_block`` — the bounded, English, model-facing preview
  spliced into the answer-synthesis evidence block (mirrors
  ``app.services.structured_retrieval.structured_prompt_block``).

Both are pure functions over ``CollectionEnumerationOutcome`` — no I/O, no
model calls, no mutation of the outcomes they read.  (``typed_collection_results``
mutates the *rows it just built*, via ``apply_synthesis_preview_counts``, but
never the outcomes.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Sequence

from app.models.ask import (
    TypedCollectionCoverage,
    TypedCollectionItem,
    TypedCollectionResult,
)
from app.services.collection_enumeration import (
    MAX_EVIDENCE_REFS,
    TRUNCATED_BUDGET,
    TRUNCATED_CONCURRENT_CHANGE,
    TRUNCATED_PAYLOAD,
    ElementItem,
    EnumerationCoverage,
    KgObjectItem,
)

if TYPE_CHECKING:
    from app.services.reasoning_retrieval import CollectionEnumerationOutcome


# ---------------------------------------------------------------------------
# AskResponse.result_sets rows
# ---------------------------------------------------------------------------


def _typed_item(raw: object) -> TypedCollectionItem:
    """Project one ``ElementItem``/``KgObjectItem`` onto the shared wire item.

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
        )
    if isinstance(raw, KgObjectItem):
        return TypedCollectionItem(
            item_id=raw.object_id,
            name=raw.name,
            section_path=raw.section_path,
            notebook_id=raw.notebook_id,
            tier=raw.tier,
            evidence_element_ids=list(raw.evidence_element_ids)[:MAX_EVIDENCE_REFS],
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


def typed_collection_results(
    outcomes: Sequence["CollectionEnumerationOutcome"],
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

    Payload size: each item's text/name already passed through the
    executor's own run-level budget (``EnumerationBudget.max_payload_chars``,
    wired from ``AskRetrievalLimits.structured_payload_chars`` = 256,000
    characters at every effort profile) before it ever reached this mapping,
    so one turn's collection rows cannot exceed that same ~256k-character
    ceiling. This function adds no further truncation of its own — the
    prompt-preview clamp below (``_PROMPT_LINE_EXCERPT_CHARS``) is a
    separate, much smaller cut that only shortens what enters the LLM
    prompt, not the transport/card copy carried here.
    """
    results: List[TypedCollectionResult] = []
    for outcome in outcomes:
        is_elements = outcome.collection == "elements"
        results.append(TypedCollectionResult(
            collection=outcome.collection,
            element_kind=outcome.kind if is_elements else "",
            object_type=outcome.kind if not is_elements else "",
            source_id=outcome.source_id,
            items=[_typed_item(item) for item in outcome.items],
            coverage=_typed_coverage(outcome.coverage),
        ))
    return results


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

# A single, one-time reminder that the preview is a SUBSET of what was
# listed. Mirrors ``structured_prompt_block``'s coverage-header instruction
# sentence, but is not repeated per outcome (one enumeration_prompt_block
# call can carry several outcome headers; the disclosure rule is the same
# for all of them, so it is stated once up front instead of N times).
_INSTRUCTION_LINE = (
    "[Enumeration preview note: base analysis only on each block's "
    "\"previewed\" count, never its \"listed\" count. When previewed is "
    "less than listed, explicitly disclose that the analysis covers only "
    "that subset — the full list is authoritative in the result card, not "
    "this preview.]"
)


def _collection_noun(collection: str) -> str:
    return "elements" if collection == "elements" else "objects"


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
    label = f"{outcome.kind} {_collection_noun(outcome.collection)}"
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


def _item_line(collection: str, item: object) -> str:
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
        return f"- {item.source_title} · {item.location_label}: {text}"
    location = item.section_path or "—"
    name = _clean(item.name)[:_PROMPT_LINE_EXCERPT_CHARS]
    return f"- {name} · {location}"


def _outcome_block(outcome: "CollectionEnumerationOutcome", *, shown: int) -> str:
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
    for item in outcome.items[:shown]:
        lines.append(_item_line(outcome.collection, item))
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


def enumeration_prompt_block(
    outcomes: Sequence["CollectionEnumerationOutcome"],
    *,
    inline_rows: int,
    budget_chars: int,
) -> EnumerationPreview:
    """Bound the enumeration preview injected into answer synthesis.

    Mirrors ``structured_prompt_block``: a coverage header the model can
    quote, followed by item lines, both bounded — ``inline_rows`` shared
    across every outcome in this run (not per-outcome, so one giant list
    cannot starve every other collection's preview), ``budget_chars`` a hard
    character ceiling the returned text never exceeds (every join separator
    is charged against it too, not just the lines themselves).

    Header-over-items priority: for each outcome this tries the largest
    ``shown`` (bounded by the remaining row quota and outcome size) that
    still fits the remaining budget, counting DOWN to zero rather than
    truncating an already-committed line — so the header (with an accurate
    ``previewed`` count) always wins over any item line, and an outcome that
    cannot even fit its bare header is skipped entirely rather than emitting
    a half-written fragment. The leading instruction sentence is tried first
    but is not load-bearing: a budget too small even for that one sentence
    still lets outcome headers try.

    Item lines deliberately carry no ``[k]`` id: Rule 11 exempts structured
    rows with no per-row citation key, because the coverage header line
    already backs the whole block.
    """
    if not outcomes:
        return EnumerationPreview(text="", shown_rows=[])
    budget = max(0, int(budget_chars))
    remaining_rows = max(0, int(inline_rows))
    blocks: List[str] = []
    used = 0
    shown_rows: List[int] = [0] * len(outcomes)

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

    for index, outcome in enumerate(outcomes):
        row_cap = max(0, min(remaining_rows, len(outcome.items)))
        for shown in range(row_cap, -1, -1):
            candidate = _outcome_block(outcome, shown=shown)
            if try_commit(candidate):
                shown_rows[index] = shown
                remaining_rows -= shown
                break
        # Falling through the loop without committing (even at shown=0) means
        # this outcome's bare header does not fit in what remains; it is
        # skipped entirely rather than truncated mid-line, and the next
        # outcome — which may be smaller — still gets its own attempt.

    return EnumerationPreview(text="\n\n".join(blocks), shown_rows=shown_rows)
