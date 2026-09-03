"""Central, protocol-stable budgets for Ask retrieval effort.

These profiles are intentionally separate from the relevance ``top_n`` knobs
in :mod:`app.core.config`.  A relevance top-N answers "which evidence is most
useful?"; it cannot prove that a finite collection was enumerated completely.
Future executors must therefore use the ranked fields only for ranked retrieval
and the structured fields for ``complete``/``aggregate``/``hybrid`` contracts.

Every count below is an inclusive hard ceiling.  Models may stop *before* a
ceiling after deciding the evidence is sufficient, but may never raise one.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


RetrievalEffort = Literal[
    "overview", "standard", "deep", "thorough", "exhaustive"
]
ResultScope = Literal["ranked", "complete", "aggregate", "hybrid"]

RETRIEVAL_EFFORTS: tuple[RetrievalEffort, ...] = (
    "overview",
    "standard",
    "deep",
    "thorough",
    "exhaustive",
)
# Ambiguity rows on a query-intent contract: the pydantic ceilings on
# ``QueryIntentContract.ambiguities`` / ``QueryIntentAmbiguity.question``, the
# trimming in ``plan_query_intent`` and the rendering in
# ``clarification_gate_message`` all read these two names, so the model, the
# planner and the error copy cannot disagree about how many rows or how long
# a question can be.
AMBIGUITY_ROWS_MAX = 8
AMBIGUITY_QUESTION_MAX_CHARS = 500

RESULT_SCOPES: tuple[ResultScope, ...] = (
    "ranked",
    "complete",
    "aggregate",
    "hybrid",
)
DEFAULT_RETRIEVAL_EFFORT: RetrievalEffort = "standard"
# The single spelling of "the user asked for the most expensive profile".
# Features whose cost the user has to opt into explicitly (the reasoning
# outline scratchpad) gate on this rather than on a second knob, so the effort
# picker stays the one place that decision is made.
EXHAUSTIVE_RETRIEVAL_EFFORT: RetrievalEffort = "exhaustive"

# This value is deliberately the same for every effort level: effort changes
# how much work may be attempted, never whether an overflow may be hidden.
EXPLICIT_PARTIAL_OVERFLOW = "explicit_partial"


@dataclass(frozen=True)
class AskRetrievalLimits:
    """All row/count/context ceilings for one user-selected Ask effort.

    Ranked evidence is retrieved at ``ranked_per_query_take`` per query.  Final
    selection starts at ``ranked_final_floor``, grows by
    ``ranked_per_aspect`` for each confirmed aspect, and is clamped to
    ``ranked_final_cap``.  The model may stop early; it cannot raise these
    values, ``max_reasoning_steps``, or ``max_initial_subqueries``.

    Structured enumeration uses stable cursor pages.  ``structured_page_size``
    is a transport/hydration batch size, *not* a final answer top-N.  A complete,
    aggregate, or hybrid request that the physical-row executor can prove
    (direct whole-table list/count) continues until cursor exhaustion or until
    the exact ``structured_max_pages``/``structured_max_rows`` safety ceiling. A
    request may touch at most ``structured_max_tables`` tables and that many
    columns per table; these are request-wide safety limits, not model choices.

    ``kg_context_chars`` limits KG objects/relations, confirmed Memory, and
    query-time chains. ``chunk_context_chars`` limits structured preview, chunks,
    and direct source elements; their combined prompt evidence cannot exceed the
    sum. Structured rows stay pageable outside
    that prompt; ``structured_payload_chars`` bounds their transport payload and
    ``inline_answer_rows`` bounds only the inline prose/list preview.  Neither
    limit may silently reduce collection completeness.  A single structured cell
    contributes at most ``cell_excerpt_chars`` to model context; the authoritative
    cell remains addressable by id.  ``answer_element_items`` bounds how many
    direct source elements (formulas/tables/images/etc.) the final synthesis
    prompt may include; they are chosen by retrieval relevance descending, not
    insertion order, and still consume the shared ``chunk_context_chars``
    budget.

    Typed-collection enumeration (the reasoning ``enumerate_*`` tools) is
    budgeted per RUN, not per action: ``enum_rows_per_run`` is the total number
    of listed items one reasoning run may retrieve across every collection it
    touches, and ``enum_pages_per_run`` the total number of EXTRA page round
    trips on top of the first page of each visited source.  ``enum_page_size``
    is a transport batch like ``structured_page_size`` — identical at every
    effort, because a page size is a round-trip shape, not an answer width.
    The three are consistent by construction:
    ``enum_rows_per_run == enum_page_size * enum_pages_per_run``, which is what
    lets a caller charge extra pages conservatively from rows scanned.

    On any hard ceiling the executor must return explicit coverage
    (scanned/known total) with ``complete=false``; it must never label the
    partial result "all".

    ``effort`` is the row's own identity — the same id that selected it.  It is
    carried IN the row because the row is what travels: ``ReasoningRetriever.run``
    receives only these limits, and a capability that is only offered at one
    effort level (the outline scratchpad, gated on ``exhaustive``) has to be able
    to ask which level this is.  Recovering it by comparing budget values would
    make any ``dataclasses.replace`` of a single budget silently change the
    answer.
    """

    effort: RetrievalEffort
    ranked_per_query_take: int
    ranked_final_floor: int
    ranked_per_aspect: int
    ranked_final_cap: int
    max_reasoning_steps: int
    max_initial_subqueries: int
    structured_page_size: int
    structured_max_pages: int
    structured_max_rows: int
    structured_max_tables: int
    structured_max_columns: int
    cell_excerpt_chars: int
    structured_payload_chars: int
    inline_answer_rows: int
    kg_context_chars: int
    chunk_context_chars: int
    answer_element_items: int
    enum_page_size: int
    enum_pages_per_run: int
    enum_rows_per_run: int
    overflow_semantics: Literal["explicit_partial"] = EXPLICIT_PARTIAL_OVERFLOW

# Threshold table (counts are intentionally literal and reviewable here):
#
# effort       take floor/aspect/cap steps/subq  KG ctx  chunk ctx  elem items  enum pages/rows
# overview       4     8/2/12          4/2       4k       12k          4           2/100
# standard       8    20/3/36          8/5       6k       30k          6           4/200
# deep           8    24/4/48         16/6       8k       50k          8           6/300
# thorough      12    32/5/64         32/8      12k       80k         12           8/400
# exhaustive    16    40/6/96        50/10      16k      120k         16          12/600
#
# Every profile also uses page=25, max_pages=50, max_rows=1,250, max_tables=8,
# max_columns=8, cell_excerpt=1,000 chars, structured_payload=256,000 chars,
# inline_answer_rows=100, and enum_page_size=50.  These are shared
# completeness/transport safety rails, not effort-dependent relevance limits.
# ``answer_element_items`` is the cap on direct source elements (formulas,
# tables, images, ...) admitted into the final synthesis prompt, chosen by
# relevance descending; it does not change ``chunk_context_chars``.
# ``enum_pages_per_run`` / ``enum_rows_per_run`` are per-RUN pools shared by
# every ``enumerate_*`` action of one reasoning run: effort buys a longer list,
# never a wider page, and the pool is what makes the cost of "list them all"
# a property of the selected effort rather than of the corpus.
# Enumeration limits do not shrink in overview mode: "all" still means all up
# to the shared safety cap.  The exhaustive profile spends more reasoning effort
# on that bounded set; it does not convert enumeration into a larger top-N.
_ASK_RETRIEVAL_LIMITS: dict[RetrievalEffort, AskRetrievalLimits] = {
    "overview": AskRetrievalLimits(
        "overview",
        4, 8, 2, 12, 4, 2,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 4_000, 12_000, 4,
        50, 2, 100
    ),
    "standard": AskRetrievalLimits(
        "standard",
        8, 20, 3, 36, 8, 5,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 6_000, 30_000, 6,
        50, 4, 200
    ),
    "deep": AskRetrievalLimits(
        "deep",
        8, 24, 4, 48, 16, 6,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 8_000, 50_000, 8,
        50, 6, 300
    ),
    "thorough": AskRetrievalLimits(
        "thorough",
        12, 32, 5, 64, 32, 8,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 12_000, 80_000, 12,
        50, 8, 400
    ),
    "exhaustive": AskRetrievalLimits(
        "exhaustive",
        16, 40, 6, 96, 50, 10,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 16_000, 120_000, 16,
        50, 12, 600
    ),
}
ASK_RETRIEVAL_LIMITS: Mapping[RetrievalEffort, AskRetrievalLimits] = (
    MappingProxyType(_ASK_RETRIEVAL_LIMITS)
)


def ask_retrieval_limits(effort: RetrievalEffort) -> AskRetrievalLimits:
    """Return the immutable limits for a validated protocol effort id."""
    return ASK_RETRIEVAL_LIMITS[effort]
