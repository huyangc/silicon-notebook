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
        parent_version: Any = None,
        max_results: int = 20,
        unsafe_scope_drift: bool = False,
    ) -> ActivatedSourceGraphResult:
        baseline = tuple(baseline_chunks)
        scope = current_source_scope()
        if scope is not None and not scope.restricted and unsafe_scope_drift:
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
        decision = self._decision(notebook_id)
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

        titles = dict(source_titles(list(source_ids))) if source_titles else {}
        candidates: dict[str, RetrievedChunk] = {}
        ppr_started = time.perf_counter()
        try:
            ppr = self._online_ppr.retrieve(
                snapshot,
                object_seeds=object_seeds,
                chunk_seeds=chunk_seeds,
                max_results=max_results,
            )
        except Exception:
            ppr = type("PprFailure", (), {
                "hits": (), "cache_hit": False,
                "capability": type("Capability", (), {
                    "enabled": False, "reason": "ppr_run_failed"
                })(),
            })()
        for hit in ppr.hits:
            candidates[hit.chunk.chunk_id] = self._chunk_from_snapshot(
                hit.chunk, titles.get(hit.chunk.source_id, ""), hit.score, hit.support
            )

        # Neighbor expansion is useful even when PPR is disabled or has no
        # reset hits.  Memberships are already part of the frozen snapshot.
        neighbor_ids: set[str] = set()
        if object_seeds:
            try:
                expanded = self._primitives.expand_graph(
                    snapshot,
                    list(object_seeds),
                    max_depth=2,
                    max_fan_out=8,
                    max_nodes=80,
                )
            except Exception:
                expanded = type("GraphFailure", (), {
                    "capability": type("Capability", (), {
                        "enabled": False, "reason": "graph_expand_failed"
                    })(),
                    "nodes": (),
                })()
            if expanded.capability.enabled:
                neighbor_ids = {node.object_id for node in expanded.nodes}
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
        degraded = list(snapshot.degraded_reasons)
        if not getattr(ppr.capability, "enabled", False):
            degraded.append(str(getattr(ppr.capability, "reason", "ppr_unavailable")))
        if object_seeds and not getattr(expanded.capability, "enabled", False):
            degraded.append(str(expanded.capability.reason or "graph_expand_unavailable"))
        if not candidates and hydrate_chunk_ids is not None and parent_version is not None:
            try:
                partitioned = self._partitioned_ppr.retrieve(
                    notebook_id,
                    source_ids,
                    parent_version=parent_version,
                    object_seeds=object_seeds,
                    chunk_seeds=chunk_seeds,
                    max_results=max_results,
                )
            except Exception:
                partitioned = type("PartitionFailure", (), {
                    "hits": (), "cache_hit": False,
                    "capability": type("Capability", (), {
                        "enabled": False,
                        "reason": "source_partition_artifact_load_failed",
                    })(),
                })()
            cache_hit = bool(getattr(partitioned, "cache_hit", False))
            if partitioned.capability.enabled and partitioned.hits:
                hydrated = {
                    chunk.chunk_id: chunk
                    for chunk in hydrate_chunk_ids(
                        [hit.chunk_id for hit in partitioned.hits]
                    )
                }
                for hit in partitioned.hits:
                    chunk = hydrated.get(hit.chunk_id)
                    if chunk is None or chunk.source_id != hit.source_id:
                        continue
                    chunk.score = hit.score
                    chunk.relevance = hit.score
                    chunk.retrieval_supports = (hit.support,)
                    candidates[chunk.chunk_id] = chunk
            elif not partitioned.capability.enabled:
                degraded.append(partitioned.capability.reason)
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
        protected = self._enrichment.run(
            baseline,
            lambda: safe_candidates,
            max_enrichment_tokens=int(
                self._settings.selected_source_graph_enrichment_tokens
            ),
            shadow_enabled=True,
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
