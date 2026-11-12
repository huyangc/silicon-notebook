"""Shared ranking request identity and cursor encoding for all consumers."""
from __future__ import annotations

import base64
import hashlib
import json

from app.domain.concept_ranking_contracts import (
    CONCEPT_RANK_FINGERPRINT_CHARS,
    CONCEPT_RANK_PAGE_SIZE,
    CONCEPT_RANK_PRECOMPUTED_ROWS,
    RANK_STATUS_OK,
)


def rank_fingerprint(query: str, scope: str, participants) -> str:
    return hashlib.sha256(json.dumps(
        {"q": query.strip(), "s": scope, "p": tuple(participants)}, sort_keys=True,
    ).encode()).hexdigest()[:CONCEPT_RANK_FINGERPRINT_CHARS]


def _library_page_sizes(computation, page_size: int) -> tuple[int, ...]:
    eligible = tuple(library.coverage.status == RANK_STATUS_OK and not library.coverage.skipped
                     for library in computation.libraries)
    count = sum(eligible)
    if not count:
        return (0,) * len(eligible)
    quota = max(1, page_size // count)
    remainder = max(0, page_size - quota * count)
    sizes = []
    for included in eligible:
        sizes.append(quota + (remainder > 0) if included else 0)
        if included:
            remainder -= 1
    return tuple(sizes)


def rank_page_valid(computation, page_number: int,
                    page_size: int = CONCEPT_RANK_PAGE_SIZE) -> bool:
    """Bound a page by each library's retained rows, not a global row offset."""
    if (type(page_number) is not int or page_number < 0
            or type(page_size) is not int or not 1 <= page_size <= CONCEPT_RANK_PAGE_SIZE):
        return False
    quotas = tuple(quota for quota in _library_page_sizes(computation, page_size) if quota)
    if len(quotas) < len(computation.libraries):
        # A continuation can lose previously successful libraries. Its page
        # remains valid under their original nominal quota, while failed
        # libraries still receive no row seats on this call.
        quotas = (max(1, page_size // len(computation.libraries)),)
    return (any(page_number * quota < CONCEPT_RANK_PRECOMPUTED_ROWS for quota in quotas)
            if quotas else page_number == 0)


def rank_page_slices(computation, page_number: int,
                     page_size: int = CONCEPT_RANK_PAGE_SIZE) -> tuple[slice, ...]:
    """One stable quota per participating library, even after another runs out."""
    return tuple(slice(page_number * quota,
                       min((page_number + 1) * quota, CONCEPT_RANK_PRECOMPUTED_ROWS))
                 for quota in _library_page_sizes(computation, page_size))


def rank_page_rows(computation, page_number: int,
                   page_size: int = CONCEPT_RANK_PAGE_SIZE) -> tuple:
    return tuple(row for library, window in zip(
        computation.libraries, rank_page_slices(computation, page_number, page_size))
        for row in library.rows[window])


def rank_next_cursor(computation, page_number: int,
                     page_size: int = CONCEPT_RANK_PAGE_SIZE) -> str:
    following = page_number + 1
    return (encode_rank_cursor(computation, following, page_size)
            if rank_page_valid(computation, following, page_size)
            and rank_page_rows(computation, following, page_size) else "")


def encode_rank_cursor(computation, page_number: int,
                       page_size: int = CONCEPT_RANK_PAGE_SIZE) -> str:
    payload = dict(v=2, f=computation.fingerprint, p=page_number, l=page_size,
                   s=computation.seqs, d=computation.ceiling_digest)
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")
