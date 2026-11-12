"""Shared concept-ranking contracts, independent of services and storage."""
import heapq
from dataclasses import dataclass
from typing import Iterable, List, Tuple

CONCEPT_RANK_PRECOMPUTED_ROWS = 80
CONCEPT_RANK_HEADER_RANK = 0
CONCEPT_RANK_PAGE_SIZE = 20
CONCEPT_RANK_MAX_PAGES = 4
CONCEPT_RANK_CITATIONS_PER_ROW = 3
CONCEPT_RANK_RECOUNT_BATCH_ROWS = 10_000
CONCEPT_RANK_QUERY_MAX_CHARS = 200
CONCEPT_RANK_INLINE_ROWS_PER_RUN = 40
CONCEPT_RANK_REPRESENTATIVE_BATCH = 80

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
RANK_REASON_LIBRARY_SKIPPED = "library_skipped"
RANK_FAILURE_REASONS = (
    RANK_REASON_SCOPED_LARGE_NOTEBOOK,
    RANK_REASON_NOT_COMPUTED,
    RANK_REASON_ANALYSIS_UNAVAILABLE,
    RANK_REASON_REFRESH_PRODUCED_NOTHING,
    RANK_REASON_RECOUNT_BUDGET_EXHAUSTED,
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


def rank_sort_key(canonical_id: str, source_count: int, object_count: int) -> tuple:
    return (-int(source_count), -int(object_count), str(canonical_id))


def top_concept_ranks(
    entries: Iterable[Tuple[str, int, int]],
    limit: int = CONCEPT_RANK_PRECOMPUTED_ROWS,
) -> List[Tuple[str, int, int]]:
    return heapq.nsmallest(limit, entries, key=lambda entry: rank_sort_key(*entry))


assert CONCEPT_RANK_MAX_PAGES * CONCEPT_RANK_PAGE_SIZE == CONCEPT_RANK_PRECOMPUTED_ROWS
