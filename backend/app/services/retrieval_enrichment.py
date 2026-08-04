"""Baseline-preserving shadow enrichment for selected-source retrieval.

The historical retrieval lane finishes first and owns the user-visible result.
Graph evidence is selected under a separate token budget and assembled only in
the shadow lane.  This module deliberately does not call graph primitives or
Ask/Report yet; later consumers supply a bounded provider after their baseline
is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Literal, Sequence

from app.services.retrieval import (
    RetrievedChunk,
    RetrievalSupport,
    est_tokens,
    merge_retrieval_supports,
)
from app.services.retrieval_baseline import RetrievalBaselineManifest


EnrichmentStatus = Literal[
    "off", "completed", "failed", "timeout", "baseline_guard"
]


@dataclass
class ReasoningLaneBudget:
    """Two counters whose capacities cannot be borrowed across lanes."""

    baseline_limit: int
    enrichment_limit: int
    baseline_used: int = 0
    enrichment_used: int = 0

    @staticmethod
    def _consume(used: int, limit: int, amount: int) -> tuple[bool, int]:
        amount = max(0, int(amount))
        limit = max(0, int(limit))
        if used + amount > limit:
            return False, used
        return True, used + amount

    def consume_baseline(self, amount: int = 1) -> bool:
        accepted, used = self._consume(
            self.baseline_used, self.baseline_limit, amount
        )
        if accepted:
            self.baseline_used = used
        return accepted

    def consume_enrichment(self, amount: int = 1) -> bool:
        accepted, used = self._consume(
            self.enrichment_used, self.enrichment_limit, amount
        )
        if accepted:
            self.enrichment_used = used
        return accepted

    @property
    def baseline_remaining(self) -> int:
        return max(0, int(self.baseline_limit) - self.baseline_used)

    @property
    def enrichment_remaining(self) -> int:
        return max(0, int(self.enrichment_limit) - self.enrichment_used)


@dataclass(frozen=True)
class ProtectedEnrichmentResult:
    """A shadow proposal plus the untouched baseline visible to the caller."""

    baseline_chunks: tuple[RetrievedChunk, ...]
    shadow_chunks: tuple[RetrievedChunk, ...]
    enrichment_chunks: tuple[RetrievedChunk, ...]
    duplicate_chunk_ids: tuple[str, ...]
    baseline_manifest: RetrievalBaselineManifest | None
    status: EnrichmentStatus
    baseline_evicted_count: int = 0
    enrichment_tokens: int = 0
    dropped_enrichment_count: int = 0

    @property
    def visible_chunks(self) -> tuple[RetrievedChunk, ...]:
        # PR 7 is shadow-only.  A caller cannot accidentally expose G merely by
        # switching the provider on.
        return self.baseline_chunks


def _baseline_signature(chunk: RetrievedChunk) -> tuple[object, ...]:
    """Fields whose mutation would change evidence, ordering, or citation."""
    return (
        chunk.chunk_id,
        chunk.source_id,
        chunk.source_title,
        chunk.section_path,
        chunk.text,
        tuple(chunk.element_ids),
        chunk.score,
        chunk.relevance,
        chunk.notebook_id,
    )


def baseline_evicted_count(
    baseline: Sequence[RetrievedChunk], proposed: Sequence[RetrievedChunk]
) -> int:
    """Count baseline positions missing or changed in a proposed shadow lane."""
    proposed = tuple(proposed)
    return sum(
        1
        for index, chunk in enumerate(tuple(baseline))
        if index >= len(proposed)
        or _baseline_signature(proposed[index]) != _baseline_signature(chunk)
    )


def _guard_shadow(
    baseline: tuple[RetrievedChunk, ...], proposed: tuple[RetrievedChunk, ...]
) -> tuple[tuple[RetrievedChunk, ...], int]:
    evicted = baseline_evicted_count(baseline, proposed)
    if evicted:
        return baseline, evicted
    return proposed, 0


def _off_result(
    baseline: tuple[RetrievedChunk, ...],
    manifest: RetrievalBaselineManifest | None,
    status: EnrichmentStatus,
) -> ProtectedEnrichmentResult:
    return ProtectedEnrichmentResult(
        baseline_chunks=baseline,
        shadow_chunks=baseline,
        enrichment_chunks=(),
        duplicate_chunk_ids=(),
        baseline_manifest=manifest,
        status=status,
    )


class BaselineProtectedEnrichmentService:
    """Select graph chunks without sharing the baseline's item/token budget."""

    @staticmethod
    def _complete(
        baseline: tuple[RetrievedChunk, ...],
        candidates: tuple[RetrievedChunk, ...],
        token_limit: int,
        baseline_manifest: RetrievalBaselineManifest | None,
    ) -> ProtectedEnrichmentResult:
        baseline_by_id = {chunk.chunk_id: chunk for chunk in baseline}
        duplicate_supports: dict[str, list[RetrievalSupport]] = {}
        selected: list[RetrievedChunk] = []
        selected_ids: set[str] = set()
        dropped = 0
        used_tokens = 0
        for candidate in candidates:
            chunk_id = str(candidate.chunk_id or "")
            if not chunk_id:
                dropped += 1
                continue
            if chunk_id in baseline_by_id:
                duplicate_supports.setdefault(chunk_id, []).extend(
                    candidate.retrieval_supports
                )
                continue
            if chunk_id in selected_ids:
                continue
            tokens = est_tokens(candidate.text)
            if used_tokens + tokens > token_limit:
                dropped += 1
                continue
            selected.append(candidate)
            selected_ids.add(chunk_id)
            used_tokens += tokens

        shadow_baseline = []
        duplicate_ids = []
        for chunk in baseline:
            supports = tuple(duplicate_supports.get(chunk.chunk_id, ()))
            if not supports:
                shadow_baseline.append(chunk)
                continue
            duplicate_ids.append(chunk.chunk_id)
            shadow_baseline.append(replace(
                chunk,
                retrieval_supports=merge_retrieval_supports(
                    chunk.retrieval_supports, supports
                ),
            ))

        proposed = tuple((*shadow_baseline, *selected))
        guarded, evicted = _guard_shadow(baseline, proposed)
        if evicted:
            return ProtectedEnrichmentResult(
                baseline_chunks=baseline,
                shadow_chunks=guarded,
                enrichment_chunks=(),
                duplicate_chunk_ids=(),
                baseline_manifest=baseline_manifest,
                status="baseline_guard",
                baseline_evicted_count=evicted,
                dropped_enrichment_count=len(candidates),
            )
        return ProtectedEnrichmentResult(
            baseline_chunks=baseline,
            shadow_chunks=guarded,
            enrichment_chunks=tuple(selected),
            duplicate_chunk_ids=tuple(duplicate_ids),
            baseline_manifest=baseline_manifest,
            status="completed",
            enrichment_tokens=used_tokens,
            dropped_enrichment_count=dropped,
        )

    def run(
        self,
        baseline_chunks: Sequence[RetrievedChunk],
        provider: Callable[[], Sequence[RetrievedChunk]],
        *,
        max_enrichment_tokens: int,
        baseline_manifest: RetrievalBaselineManifest | None = None,
        shadow_enabled: bool = False,
    ) -> ProtectedEnrichmentResult:
        baseline_input = tuple(baseline_chunks)
        token_limit = max(0, int(max_enrichment_tokens))
        if not shadow_enabled or token_limit == 0:
            return _off_result(baseline_input, baseline_manifest, "off")

        # RetrievedChunk is mutable (notably ``element_ids`` and producer
        # supports). Freeze values before calling an enrichment provider so a
        # collaborator holding an alias cannot rewrite B under the guard.
        baseline = tuple(
            replace(
                chunk,
                element_ids=list(chunk.element_ids),
                retrieval_supports=tuple(chunk.retrieval_supports),
            )
            for chunk in baseline_input
        )

        try:
            candidates = tuple(provider() or ())
        except TimeoutError:
            return _off_result(baseline, baseline_manifest, "timeout")
        except Exception:
            return _off_result(baseline, baseline_manifest, "failed")
        try:
            return self._complete(
                baseline, candidates, token_limit, baseline_manifest
            )
        except Exception:
            # Malformed or stale graph candidates are graph-lane failures. They
            # must never turn a valid historical answer into a request error.
            return _off_result(baseline, baseline_manifest, "failed")
