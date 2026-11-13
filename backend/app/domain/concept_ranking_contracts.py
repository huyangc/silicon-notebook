"""Shared concept-ranking contracts, independent of services and storage."""
import heapq
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

CONCEPT_RANK_PRECOMPUTED_ROWS = 80
CONCEPT_RANK_HEADER_RANK = 0
CONCEPT_RANK_PAGE_SIZE = 20
CONCEPT_RANK_MAX_PAGES = 4  # 单库默认20行时的页数，不限制多库/小页的页号。
CONCEPT_RANK_MAX_CHAINS = 4
CONCEPT_RANK_CITATIONS_PER_ROW = 3
CONCEPT_RANK_RECOUNT_BATCH_ROWS = 10_000
CONCEPT_RANK_QUERY_MAX_CHARS = 200
CONCEPT_RANK_INLINE_ROWS_PER_RUN = 40
CONCEPT_RANK_REPRESENTATIVE_BATCH = 80
CONCEPT_RANK_FINGERPRINT_CHARS = 32

RANK_PATH_QUERY = "query"
RANK_PATH_SCOPE_FULL = "scope_full"
RANK_PATH_PRECOMPUTED = "precomputed"
RANK_PATHS = (RANK_PATH_QUERY, RANK_PATH_SCOPE_FULL, RANK_PATH_PRECOMPUTED)
RANK_STATUS_OK = "ok"
RANK_STATUS_UNSUPPORTED = "unsupported"
RANK_STATUS_UNAVAILABLE = "unavailable"
RANK_REASON_SCOPED_LARGE_NOTEBOOK = "scoped_large_notebook"
RANK_REASON_NOT_COMPUTED = "not_computed"
RANK_REASON_ANALYSIS_UNAVAILABLE = "analysis_unavailable"
RANK_REASON_REFRESH_PRODUCED_NOTHING = "refresh_produced_nothing"
RANK_REASON_RECOUNT_BUDGET_EXHAUSTED = "recount_budget_exhausted"
RANK_REASON_RANK_FAILED = "rank_failed"
RANK_REASON_LIBRARY_SKIPPED = "library_skipped"
RANK_FAILURE_REASONS = (
    RANK_REASON_SCOPED_LARGE_NOTEBOOK,
    RANK_REASON_NOT_COMPUTED,
    RANK_REASON_ANALYSIS_UNAVAILABLE,
    RANK_REASON_REFRESH_PRODUCED_NOTHING,
    RANK_REASON_RECOUNT_BUDGET_EXHAUSTED,
    RANK_REASON_RANK_FAILED,
    RANK_REASON_LIBRARY_SKIPPED,
)
RANK_SKIP_READ_FAILED = "read_failed"
RANK_SKIP_TIMEOUT = "timeout"
RANK_SCOPE_ALL = "all"
RANK_SCOPE_CURRENT_NOTEBOOK = "current_notebook"


@dataclass(frozen=True)
class PrecomputedRankRow:
    rank: int
    canonical_id: str
    representative_object_id: str
    source_count: int
    object_count: int


@dataclass(frozen=True)
class ConceptRankWrite:
    built_at_kg_seq: int
    built_at_cluster_seq: int
    rows: Tuple[PrecomputedRankRow, ...]


def concept_ranks_are_current(
    header: Optional[Tuple[int, int]], kg_seq: int, cluster_seq: int,
) -> bool:
    """The ranking header must exist and match both published graph generations."""
    return header == (kg_seq, cluster_seq)


# Model-facing instruction for a ranking call that did not complete. It lives in
# this leaf module because both the reflect ledger and the synthesis evidence
# block render it; importing it from either service would close an import cycle
# through the reflect loop.
RANK_FAILED_INSTRUCTION = (
    "Counting did not complete. Do not infer which items are mentioned most from retrieved samples. "
    "Tell the user plainly that it could not be counted and why, in everyday words. "
    "Never mention internal codes, field names or flags (such as reason codes or exact/coverage flags). "
    "The system already shows the user how to fix it above the answer; do not propose other remedies, "
    "and never suggest narrowing the selected sources."
)
RANK_FAILURE_CAUSES = {
    "scoped_large_notebook": "this notebook is large and only part of its sources are selected.",
    "not_computed": "ranking data for this notebook has not been generated yet.",
    "analysis_unavailable": "this notebook does not support this kind of count.",
    "recount_budget_exhausted": "counting timed out.",
    "rank_failed": "counting hit an error.",
}


def ranking_failure_instruction(reason: str, count: int = 1) -> str:
    cause = (f"{count} notebook(s) could not be read." if reason == "library_skipped"
             else RANK_FAILURE_CAUSES.get(reason, ""))
    return RANK_FAILED_INSTRUCTION + (f"\nCause: {cause}" if cause else "")


def rank_sort_key(canonical_id: str, source_count: int, object_count: int) -> tuple:
    return (-int(source_count), -int(object_count), str(canonical_id))


def top_concept_ranks(
    entries: Iterable[Tuple[str, int, int]],
    limit: int = CONCEPT_RANK_PRECOMPUTED_ROWS,
) -> List[Tuple[str, int, int]]:
    return heapq.nsmallest(limit, entries, key=lambda entry: rank_sort_key(*entry))


assert CONCEPT_RANK_MAX_PAGES * CONCEPT_RANK_PAGE_SIZE == CONCEPT_RANK_PRECOMPUTED_ROWS
