"""Quality-gated selected-source graph enrichment shared by Ask and reports.

The historical retrieval lane (B) always finishes first.  This orchestrator
may build a source-induced snapshot and propose graph chunks (G), but G owns a
separate token budget and can only be appended after B.  Any uncertainty,
scope drift, quality-gate failure, or baseline mutation fails closed to B.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.services.retrieval import RetrievedChunk, RetrievalSupport
from app.services.source_graph_rollout import (
    decide_source_graph_rollout,
    load_quality_attestation,
)
from app.services.source_scope import current_source_scope


@dataclass(frozen=True)
class SourceGraphStatus:
    state: str
    reason: str
    selected_source_count: int = 0
    scope_hash: str = ""
    node_count: int = 0
    relation_count: int = 0
    chunk_count: int = 0
    enrichment_count: int = 0
    cache_hit: bool = False
    build_ms: int = 0
    ppr_ms: int = 0
    baseline_preserved: bool = True
    baseline_evicted_count: int = 0
    post_scope_drop_count: int = 0
    degraded_reasons: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "selected_source_count": self.selected_source_count,
            "node_count": self.node_count,
            "relation_count": self.relation_count,
            "chunk_count": self.chunk_count,
            "enrichment_count": self.enrichment_count,
            "cache_hit": self.cache_hit,
            "baseline_preserved": self.baseline_preserved,
            "degraded_reasons": list(self.degraded_reasons),
        }


@dataclass(frozen=True)
class ActivatedSourceGraphResult:
    chunks: tuple[RetrievedChunk, ...]
    baseline_chunks: tuple[RetrievedChunk, ...]
    enrichment_chunks: tuple[RetrievedChunk, ...]
    status: SourceGraphStatus


def hydrate_selected_graph_chunk_rows(rows: Sequence[Mapping[str, Any]]):
    """Convert the bounded retrieval projection at the composition root."""
    return [RetrievedChunk(
        chunk_id=str(row.get("chunk_id") or ""),
        source_id=str(row.get("source_id") or ""),
        source_title=str(row.get("source_title") or ""),
        section_path=str(row.get("section_path") or ""),
        text=str(row.get("text") or ""),
        element_ids=list(row.get("element_ids") or ()),
    ) for row in rows]


class SelectedSourceGraphActivationService:
    """One bounded activation seam for chunk/reasoning/graph/report consumers."""

    def __init__(
        self,
        *,
        settings: Any,
        snapshots: Any,
        primitives: Any,
        online_ppr: Any,
        partitioned_ppr: Any,
        enrichment: Any,
        event_log: Any,
    ) -> None:
        self._settings = settings
        self._snapshots = snapshots
        self._primitives = primitives
        self._online_ppr = online_ppr
        self._partitioned_ppr = partitioned_ppr
        self._enrichment = enrichment
        self._event_log = event_log

    def _emit(self, notebook_id: str, status: SourceGraphStatus) -> None:
        # Content-free by construction: no query, source ids, evidence, names,
        # text, or citations leave this service.
        try:
            self._event_log.emit({
                "kind": "selected_source_graph",
                "notebook_id": notebook_id,
                "state": status.state,
                "reason": status.reason,
                "selected_source_count": status.selected_source_count,
                "scope_hash": status.scope_hash,
                "node_count": status.node_count,
                "relation_count": status.relation_count,
                "chunk_count": status.chunk_count,
                "enrichment_count": status.enrichment_count,
                "cache_hit": status.cache_hit,
                "build_ms": status.build_ms,
                "ppr_ms": status.ppr_ms,
                "baseline_preserved": status.baseline_preserved,
                "baseline_evicted_count": status.baseline_evicted_count,
                "post_scope_drop_count": status.post_scope_drop_count,
                "degraded_reasons": list(status.degraded_reasons),
            })
        except Exception:
            # Observability is subordinate to returning the frozen baseline.
            pass

    def fail_closed(
        self,
        notebook_id: str,
        baseline_chunks: Sequence[RetrievedChunk],
        reason: str,
        *,
        source_ids: Sequence[str] = (),
        snapshot: Any = None,
        build_ms: int = 0,
        ppr_ms: int = 0,
        degraded_reasons: Sequence[str] = (),
    ) -> ActivatedSourceGraphResult:
        """Return the immutable historical lane for any graph-lane failure."""
        baseline = tuple(baseline_chunks)
        status = SourceGraphStatus(
            state="degraded",
            reason=reason,
            selected_source_count=len(source_ids),
            scope_hash=str(getattr(snapshot, "scope_hash", "") or ""),
            node_count=len(getattr(snapshot, "nodes", ()) or ()),
            relation_count=len(getattr(snapshot, "relations", ()) or ()),
            chunk_count=len(getattr(snapshot, "chunks", ()) or ()),
            build_ms=build_ms,
            ppr_ms=ppr_ms,
            degraded_reasons=tuple(dict.fromkeys(
                value for value in (*degraded_reasons, reason) if value
            )),
        )
        self._emit(notebook_id, status)
        return ActivatedSourceGraphResult(baseline, baseline, (), status)

    def _decision(self, notebook_id: str):
        attestation = None
        path = str(self._settings.selected_source_graph_attestation_path or "").strip()
        if path:
            try:
                attestation = load_quality_attestation(Path(path))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                attestation = None
        expected_model = None
        raw_model = str(
            self._settings.selected_source_graph_expected_model_json or ""
        ).strip()
        if raw_model:
            try:
                parsed = json.loads(raw_model)
                if isinstance(parsed, dict):
                    expected_model = parsed
            except json.JSONDecodeError:
                expected_model = None
        allowlist = frozenset(
            value.strip()
            for value in str(
                self._settings.selected_source_graph_notebook_allowlist or ""
            ).split(",")
            if value.strip()
        )
        return decide_source_graph_rollout(
            notebook_id=notebook_id,
            mode=self._settings.selected_source_graph_rollout_mode,
            attestation=attestation,
            notebook_allowlist=allowlist,
            rollout_percent=float(
                self._settings.selected_source_graph_rollout_percent
            ),
            expected_corpus_signature=(
                str(
                    self._settings.selected_source_graph_expected_corpus_signature
                    or ""
                ).strip()
                or None
            ),
            expected_model=expected_model,
        )

    @staticmethod
    def _chunk_from_snapshot(chunk: Any, title: str, score: float, support: Any):
        return RetrievedChunk(
            chunk_id=chunk.chunk_id,
            source_id=chunk.source_id,
            source_title=title,
            section_path=chunk.section_path,
            text=chunk.text,
            element_ids=list(chunk.element_ids),
            score=score,
            relevance=score,
            retrieval_supports=(support,),
        )

    def run(
        self,
        notebook_id: str,
        baseline_chunks: Sequence[RetrievedChunk],
        *,
        object_seeds: Mapping[str, float] | None = None,
        chunk_seeds: Mapping[str, float] | None = None,
        source_titles: Callable[[list[str]], Mapping[str, str]] | None = None,
        hydrate_chunk_ids: Callable[[Sequence[str]], Sequence[RetrievedChunk]] | None = None,
        parent_version: Any | Callable[[], Any] = None,
        max_results: int = 20,
        unsafe_scope_drift: bool | Callable[[], bool] = False,
    ) -> ActivatedSourceGraphResult:
        baseline = tuple(baseline_chunks)
        scope = current_source_scope()
        try:
            scope_drifted = bool(
                unsafe_scope_drift()
                if callable(unsafe_scope_drift)
                else unsafe_scope_drift
            ) if scope is not None and not scope.restricted else False
        except Exception:
            return self.fail_closed(
                notebook_id, baseline, "scope_drift_probe_failed",
                source_ids=(scope.source_ids if scope is not None else ()),
            )
        if scope is not None and not scope.restricted and scope_drifted:
            status = SourceGraphStatus(
                "degraded",
                "scope_drift",
                selected_source_count=len(scope.source_ids),
            )
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)
        # Whole-scope and all-selected requests must remain byte-identical to
        # the historical path, including a one-source notebook whose sole row
        # is selected.  This lane exists only for genuine narrowing.
        if scope is None or not scope.restricted:
            status = SourceGraphStatus("historical", "whole_scope")
            return ActivatedSourceGraphResult(baseline, baseline, (), status)
        if scope.notebook_id != notebook_id or scope.mode != "include":
            status = SourceGraphStatus("degraded", "scope_not_frozen_include")
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)
        source_ids = tuple(sorted(scope.source_ids))
        try:
            decision = self._decision(notebook_id)
        except Exception:
            return self.fail_closed(
                notebook_id, baseline, "rollout_decision_failed",
                source_ids=source_ids,
            )
        if not decision.enabled:
            status = SourceGraphStatus(
                "off", decision.reason, selected_source_count=len(source_ids)
            )
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)

        started = time.perf_counter()
        try:
            snapshot = self._snapshots.snapshot(notebook_id, source_ids)
        except Exception:
            status = SourceGraphStatus(
                "degraded", "snapshot_failed", selected_source_count=len(source_ids)
            )
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)
        build_ms = round((time.perf_counter() - started) * 1000)
        if tuple(snapshot.allowed_source_ids) != source_ids:
            status = SourceGraphStatus(
                "degraded", "scope_drift", len(source_ids), snapshot.scope_hash,
                build_ms=build_ms,
            )
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)

        try:
            titles = dict(source_titles(list(source_ids))) if source_titles else {}
        except Exception:
            return self.fail_closed(
                notebook_id, baseline, "source_titles_failed",
                source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
            )
        candidates: dict[str, RetrievedChunk] = {}
        degraded = list(snapshot.degraded_reasons)
        online_ppr_available = False
        ppr_unavailable_reason = "ppr_unavailable"
        ppr_started = time.perf_counter()
        try:
            ppr = self._online_ppr.retrieve(
                snapshot,
                object_seeds=object_seeds,
                chunk_seeds=chunk_seeds,
                max_results=max_results,
            )
            online_ppr_available = bool(ppr.capability.enabled)
            if online_ppr_available:
                for hit in ppr.hits:
                    candidates[hit.chunk.chunk_id] = self._chunk_from_snapshot(
                        hit.chunk,
                        titles.get(hit.chunk.source_id, ""),
                        hit.score,
                        hit.support,
                    )
            else:
                ppr_unavailable_reason = str(
                    ppr.capability.reason or "ppr_unavailable"
                )
                degraded.append(ppr_unavailable_reason)
        except Exception:
            return self.fail_closed(
                notebook_id, baseline, "ppr_run_failed",
                source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
                ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
            )

        # Neighbor expansion complements a successful PPR producer.  The G
        # lane is atomic: one requested producer failing discards all of G.
        neighbor_ids: set[str] = set()
        if object_seeds and online_ppr_available:
            try:
                expanded = self._primitives.expand_graph(
                    snapshot,
                    list(object_seeds),
                    max_depth=2,
                    max_fan_out=8,
                    max_nodes=80,
                )
                if not expanded.capability.enabled:
                    return self.fail_closed(
                        notebook_id,
                        baseline,
                        str(expanded.capability.reason or "graph_expand_unavailable"),
                        source_ids=source_ids,
                        snapshot=snapshot,
                        build_ms=build_ms,
                        ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
                    )
                neighbor_ids = {node.object_id for node in expanded.nodes}
            except Exception:
                return self.fail_closed(
                    notebook_id, baseline, "graph_expand_failed",
                    source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
                    ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
                )
        member_chunk_ids = {
            chunk_id
            for object_id, chunk_id in snapshot.memberships
            if object_id in neighbor_ids
        }
        graph_support = RetrievalSupport("kg_source", "object", "", 1.0)
        for chunk in snapshot.chunks:
            if chunk.chunk_id in member_chunk_ids and chunk.chunk_id not in candidates:
                candidates[chunk.chunk_id] = self._chunk_from_snapshot(
                    chunk, titles.get(chunk.source_id, ""), 1.0, graph_support
                )

        cache_hit = bool(getattr(ppr, "cache_hit", False))
        if not online_ppr_available:
            if hydrate_chunk_ids is None or parent_version is None:
                return self.fail_closed(
                    notebook_id, baseline, ppr_unavailable_reason,
                    source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
                    ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
                    degraded_reasons=degraded,
                )
            try:
                resolved_parent_version = (
                    parent_version() if callable(parent_version) else parent_version
                )
                partitioned = self._partitioned_ppr.retrieve(
                    notebook_id,
                    source_ids,
                    parent_version=resolved_parent_version,
                    object_seeds=object_seeds,
                    chunk_seeds=chunk_seeds,
                    max_results=max_results,
                )
                if not partitioned.capability.enabled:
                    return self.fail_closed(
                        notebook_id,
                        baseline,
                        str(
                            partitioned.capability.reason
                            or "source_partition_artifact_unavailable"
                        ),
                        source_ids=source_ids,
                        snapshot=snapshot,
                        build_ms=build_ms,
                        ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
                    )
                cache_hit = bool(getattr(partitioned, "cache_hit", False))
                if partitioned.hits:
                    partition_hits = tuple(partitioned.hits)
                    requested_chunk_ids = {
                        str(hit.chunk_id) for hit in partition_hits
                    }
                    hydrated_rows = tuple(hydrate_chunk_ids(
                        [hit.chunk_id for hit in partition_hits]
                    ))
                    hydrated = {chunk.chunk_id: chunk for chunk in hydrated_rows}
                    allowed_sources = set(source_ids)
                    hydration_mismatch = (
                        len(hydrated) != len(hydrated_rows)
                        or any(
                            chunk.chunk_id not in requested_chunk_ids
                            or chunk.source_id not in allowed_sources
                            for chunk in hydrated_rows
                        )
                        or any(
                            (chunk := hydrated.get(hit.chunk_id)) is None
                            or hit.source_id not in allowed_sources
                            or chunk.source_id != hit.source_id
                            for hit in partition_hits
                        )
                    )
                    if hydration_mismatch:
                        return self.fail_closed(
                            notebook_id,
                            baseline,
                            "source_partition_hydration_mismatch",
                            source_ids=source_ids,
                            snapshot=snapshot,
                            build_ms=build_ms,
                            ppr_ms=round(
                                (time.perf_counter() - ppr_started) * 1000
                            ),
                            degraded_reasons=degraded,
                        )
                    for hit in partition_hits:
                        chunk = hydrated[hit.chunk_id]
                        chunk.score = hit.score
                        chunk.relevance = hit.score
                        chunk.retrieval_supports = (hit.support,)
                        candidates[chunk.chunk_id] = chunk
            except Exception:
                return self.fail_closed(
                    notebook_id, baseline, "source_partition_artifact_load_failed",
                    source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
                    ppr_ms=round((time.perf_counter() - ppr_started) * 1000),
                )
        ppr_ms = round((time.perf_counter() - ppr_started) * 1000)

        allowed = set(source_ids)
        post_scope_drop = sum(
            1 for chunk in candidates.values() if chunk.source_id not in allowed
        )
        if post_scope_drop:
            status = SourceGraphStatus(
                state="degraded",
                reason="post_scope_drop",
                selected_source_count=len(source_ids),
                scope_hash=snapshot.scope_hash,
                node_count=len(snapshot.nodes),
                relation_count=len(snapshot.relations),
                chunk_count=len(snapshot.chunks),
                cache_hit=cache_hit,
                build_ms=build_ms,
                ppr_ms=ppr_ms,
                post_scope_drop_count=post_scope_drop,
                degraded_reasons=tuple(dict.fromkeys(degraded)),
            )
            self._emit(notebook_id, status)
            return ActivatedSourceGraphResult(baseline, baseline, (), status)
        safe_candidates = tuple(
            chunk for chunk in candidates.values() if chunk.source_id in allowed
        )
        try:
            protected = self._enrichment.run(
                baseline,
                lambda: safe_candidates,
                max_enrichment_tokens=int(
                    self._settings.selected_source_graph_enrichment_tokens
                ),
                shadow_enabled=True,
            )
        except Exception:
            return self.fail_closed(
                notebook_id, baseline, "baseline_enrichment_failed",
                source_ids=source_ids, snapshot=snapshot, build_ms=build_ms,
                ppr_ms=ppr_ms, degraded_reasons=degraded,
            )
        active = not decision.shadow_only and protected.baseline_evicted_count == 0
        visible = protected.shadow_chunks if active else protected.baseline_chunks
        state = "active" if active else "shadow"
        reason = decision.reason
        if protected.baseline_evicted_count:
            state, reason = "degraded", "baseline_guard"
        status = SourceGraphStatus(
            state=state,
            reason=reason,
            selected_source_count=len(source_ids),
            scope_hash=snapshot.scope_hash,
            node_count=len(snapshot.nodes),
            relation_count=len(snapshot.relations),
            chunk_count=len(snapshot.chunks),
            enrichment_count=len(protected.enrichment_chunks),
            cache_hit=cache_hit,
            build_ms=build_ms,
            ppr_ms=ppr_ms,
            baseline_preserved=protected.baseline_evicted_count == 0,
            baseline_evicted_count=protected.baseline_evicted_count,
            post_scope_drop_count=post_scope_drop,
            degraded_reasons=tuple(dict.fromkeys(value for value in degraded if value)),
        )
        self._emit(notebook_id, status)
        return ActivatedSourceGraphResult(
            tuple(visible), baseline, tuple(protected.enrichment_chunks), status
        )
