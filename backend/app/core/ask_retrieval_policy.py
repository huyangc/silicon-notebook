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
RESULT_SCOPES: tuple[ResultScope, ...] = (
    "ranked",
    "complete",
    "aggregate",
    "hybrid",
)
DEFAULT_RETRIEVAL_EFFORT: RetrievalEffort = "standard"

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
    budget. On any hard ceiling the executor must return explicit coverage
    (scanned/known total) with ``complete=false``; it must never label the
    partial result "all".
    """

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
    overflow_semantics: Literal["explicit_partial"] = EXPLICIT_PARTIAL_OVERFLOW

# Threshold table (counts are intentionally literal and reviewable here):
#
# effort       take floor/aspect/cap steps/subq  KG ctx  chunk ctx  elem items
# overview       4     8/2/12          4/2       4k       12k          4
# standard       8    20/3/36          8/5       6k       30k          6
# deep           8    24/4/48         16/6       8k       50k          8
# thorough      12    32/5/64         32/8      12k       80k         12
# exhaustive    16    40/6/96        50/10      16k      120k         16
#
# Every profile also uses page=25, max_pages=50, max_rows=1,250, max_tables=8,
# max_columns=8, cell_excerpt=1,000 chars, structured_payload=256,000 chars,
# and inline_answer_rows=100.  These are shared completeness/transport safety
# rails, not effort-dependent relevance limits.
# ``answer_element_items`` is the cap on direct source elements (formulas,
# tables, images, ...) admitted into the final synthesis prompt, chosen by
# relevance descending; it does not change ``chunk_context_chars``.
# Enumeration limits do not shrink in overview mode: "all" still means all up
# to the shared safety cap.  The exhaustive profile spends more reasoning effort
# on that bounded set; it does not convert enumeration into a larger top-N.
_ASK_RETRIEVAL_LIMITS: dict[RetrievalEffort, AskRetrievalLimits] = {
    "overview": AskRetrievalLimits(
        4, 8, 2, 12, 4, 2,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 4_000, 12_000, 4
    ),
    "standard": AskRetrievalLimits(
        8, 20, 3, 36, 8, 5,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 6_000, 30_000, 6
    ),
    "deep": AskRetrievalLimits(
        8, 24, 4, 48, 16, 6,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 8_000, 50_000, 8
    ),
    "thorough": AskRetrievalLimits(
        12, 32, 5, 64, 32, 8,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 12_000, 80_000, 12
    ),
    "exhaustive": AskRetrievalLimits(
        16, 40, 6, 96, 50, 10,
        25, 50, 1_250, 8, 8, 1_000, 256_000, 100, 16_000, 120_000, 16
    ),
}
ASK_RETRIEVAL_LIMITS: Mapping[RetrievalEffort, AskRetrievalLimits] = (
    MappingProxyType(_ASK_RETRIEVAL_LIMITS)
)


def ask_retrieval_limits(effort: RetrievalEffort) -> AskRetrievalLimits:
    """Return the immutable limits for a validated protocol effort id."""
    return ASK_RETRIEVAL_LIMITS[effort]
