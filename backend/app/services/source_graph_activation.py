"""Quality-gated selected-source graph enrichment shared by Ask and reports.

The historical retrieval lane (B) always finishes first.  This orchestrator
may build a source-induced snapshot and propose graph chunks (G), but G owns a
separate token budget and can only be appended after B.  Any uncertainty,
scope drift, quality-gate failure, or baseline mutation fails closed to B.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from app.domain.extensions import (
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
    lane_is_dormant,
)
from app.services.cancellation import AskCancelled
from app.services.retrieval import RetrievedChunk, RetrievalSupport, est_tokens
from app.services.source_graph_rollout import (
    decide_source_graph_rollout,
    load_quality_attestation,
)
from app.services.source_scope import current_source_scope


_MIN_CONTRIBUTION_EXECUTION_BUDGET = 1
_HOST_ACCOUNTED_TOKEN_COST = 0
# The graph capability decision registered by the extension composition root is
# exactly ``selected_source_graph_access is not None``, which is exactly "the
# deployment configured the service".  An unconfigured feature is therefore a
# call-site fact rather than a live decision, so the workflow may declare it up
# front the way the sibling generated-question lane already does.  (The decision
# itself is not named here: workflows must not reference the composition root,
# and that boundary is enforced by scripts/check_architecture_boundaries.py's
# AST import-graph check, which rejects any ``app.services.*`` module that
# imports the extension composition package (``backend/app/extensions/``)
# outside an approved root -- reinforced, for this package specifically, by a
# companion literal-text scan in
# backend/tests/test_phase0_architecture_guard.py.)
UNCONFIGURED_SELECTED_SOURCE_GRAPH_CAPABILITIES = frozenset({
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY
})
_SOURCE_GRAPH_STATES = frozenset({
    "active", "shadow", "off", "historical", "degraded",
})


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
        leaf_io: Callable[[], Any] | None = None,
    ) -> ActivatedSourceGraphResult:
        baseline = tuple(baseline_chunks)
        leaf_slot = leaf_io or nullcontext
        scope = current_source_scope()
        try:
            if scope is not None and not scope.restricted:
                if callable(unsafe_scope_drift):
                    with leaf_slot():
                        scope_drifted = bool(unsafe_scope_drift())
                else:
                    scope_drifted = bool(unsafe_scope_drift)
            else:
                scope_drifted = False
        except AskCancelled:
            raise
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
            with leaf_slot():
                snapshot = self._snapshots.snapshot(notebook_id, source_ids)
        except AskCancelled:
            raise
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
            if source_titles:
                with leaf_slot():
                    titles = dict(source_titles(list(source_ids)))
            else:
                titles = {}
        except AskCancelled:
            raise
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
            with leaf_slot():
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
        except AskCancelled:
            raise
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
                with leaf_slot():
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
            except AskCancelled:
                raise
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
                if callable(parent_version):
                    with leaf_slot():
                        resolved_parent_version = parent_version()
                else:
                    resolved_parent_version = parent_version
                with leaf_slot():
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
                    with leaf_slot():
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
            except AskCancelled:
                raise
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


class _AskCancellationToken:
    """Adapt the production Event to the host's native-cancellation contract."""

    def __init__(self, event: Any) -> None:
        self._event = event

    def is_set(self) -> bool:
        if self._event is None:
            return False
        cancelled = self._event.is_set()
        if type(cancelled) is not bool:
            raise TypeError("malformed cancellation state")
        return cancelled

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise AskCancelled()


class _SelectedEvidenceAdmissionSource:
    """Graph in-memory authority plus one scope-bound core fallback batch."""

    def __init__(
        self,
        graph: "SelectedSourceGraphContributionCall | None",
        notebook_id: str,
        actor_id: str,
        fallback: Callable[
            [str, str, Sequence[str]], Sequence[RetrievedChunk]
        ],
        leaf_io: Callable[[], Any] | None,
    ) -> None:
        self._graph = graph
        self._notebook_id = notebook_id
        self._actor_id = actor_id
        self._fallback = fallback
        self._leaf_io = leaf_io or nullcontext

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]:
        primary = self._graph.read(identities) if self._graph is not None else ()
        by_id = {proposal.identity: proposal for proposal in primary}
        missing = tuple(identity for identity in identities if identity not in by_id)
        if missing:
            try:
                with self._leaf_io():
                    hydrated = self._fallback(
                        self._notebook_id, self._actor_id, missing
                    )
            except Exception:
                return ()
            if type(hydrated) not in (list, tuple):
                return ()
            missing_set = set(missing)
            for chunk in hydrated:
                if (
                    type(chunk) is not RetrievedChunk
                    or type(chunk.chunk_id) is not str
                    or chunk.chunk_id not in missing_set
                    or chunk.chunk_id in by_id
                    or type(chunk.source_id) is not str
                    or not chunk.source_id
                    or type(chunk.notebook_id) is not str
                    or chunk.notebook_id != self._notebook_id
                    or type(chunk.text) is not str
                ):
                    return ()
                by_id[chunk.chunk_id] = RetrievalEvidenceProposal(
                    identity=chunk.chunk_id,
                    notebook_id=self._notebook_id,
                    source_id=chunk.source_id,
                    provenance_kind="chunk",
                    provenance_reference=chunk.chunk_id,
                    value=chunk,
                    token_cost=est_tokens(chunk.text),
                )
        return tuple(by_id[identity] for identity in identities if identity in by_id)


class SelectedSourceGraphContributionCall:
    """Request-local bridge retaining the legacy activation's stronger result.

    The built-in plugin receives only the proposal/read methods through the
    host.  Baseline chunks, graph services, status, and rollout details remain
    core-private here.
    """

    def __init__(
        self,
        service: SelectedSourceGraphActivationService | None,
        notebook_id: str,
        baseline_chunks: Sequence[RetrievedChunk],
        *,
        object_seeds: Mapping[str, float] | None = None,
        chunk_seeds: Mapping[str, float] | None = None,
        source_titles: Callable[[list[str]], Mapping[str, str]] | None = None,
        hydrate_chunk_ids: Callable[
            [Sequence[str]], Sequence[RetrievedChunk]
        ] | None = None,
        parent_version: Any | Callable[[], Any] = None,
        max_results: int,
        unsafe_scope_drift: bool | Callable[[], bool] = False,
        leaf_io: Callable[[], Any] | None = None,
    ) -> None:
        self._service = service
        self._notebook_id = notebook_id
        self._baseline = tuple(baseline_chunks)
        self._kwargs = {
            "object_seeds": object_seeds,
            "chunk_seeds": chunk_seeds,
            "source_titles": source_titles,
            "hydrate_chunk_ids": hydrate_chunk_ids,
            "parent_version": parent_version,
            "max_results": max_results,
            "unsafe_scope_drift": unsafe_scope_drift,
            "leaf_io": leaf_io,
        }
        self._activated: ActivatedSourceGraphResult | None = None
        self._attempted = False
        self._proposals: tuple[RetrievalEvidenceProposal, ...] = ()
        self._by_id: dict[str, RetrievalEvidenceProposal] = {}

    def propose(self) -> tuple[RetrievalEvidenceProposal, ...]:
        if self._attempted:
            return self._proposals
        self._attempted = True
        if self._service is None:
            return ()
        try:
            activated = self._service.run(
                self._notebook_id,
                self._baseline,
                **self._kwargs,
            )
            if (
                not self._valid_activation_result(activated)
                or not self._activation_preserves_frozen_baseline(activated)
            ):
                raise TypeError("invalid selected-source graph result")
        except AskCancelled:
            raise
        except Exception:
            activated = self._fail_closed_activation("activation_seam_failed")
            if activated is None:
                return ()
        self._activated = activated
        if activated.status.state != "active":
            return ()
        try:
            self._proposals = tuple(
                RetrievalEvidenceProposal(
                    identity=chunk.chunk_id,
                    notebook_id=self._notebook_id,
                    source_id=chunk.source_id,
                    provenance_kind="ppr",
                    provenance_reference=chunk.chunk_id,
                    value=chunk,
                    # The legacy activation already enforces its independent
                    # token budget.  The host must not spend or truncate it a
                    # second time.
                    token_cost=_HOST_ACCOUNTED_TOKEN_COST,
                )
                for chunk in activated.enrichment_chunks
            )
        except Exception:
            self._activated = self._fail_closed_activation(
                "activation_seam_failed"
            )
            return ()
        self._by_id = {proposal.identity: proposal for proposal in self._proposals}
        return self._proposals

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]:
        return tuple(
            proposal
            for identity in identities
            if (proposal := self._by_id.get(identity)) is not None
        )

    def visible_result(self, host_chunks: Sequence[RetrievedChunk]):
        """Apply the legacy output only after the generic atomic host accepts G."""
        activated = self._activated
        if activated is None:
            return list(host_chunks), None
        status = activated.status
        host_chunks = tuple(host_chunks)
        host_tail = host_chunks[len(self._baseline):]
        if status.state == "historical":
            return list(host_chunks), None
        expected_ids = tuple(proposal.identity for proposal in self._proposals)
        accepted_ids = tuple(
            chunk.chunk_id for chunk in host_tail
        )
        expected_set = set(expected_ids)
        accepted_graph_ids = tuple(
            identity for identity in accepted_ids if identity in expected_set
        )
        if status.state == "active" and accepted_graph_ids != expected_ids:
            non_graph_tail = tuple(
                chunk for chunk in host_tail if chunk.chunk_id not in expected_set
            )
            failed = self._fail_closed_activation("extension_admission_failed")
            if failed is None:
                return [*self._baseline, *non_graph_tail], None
            return [*self._baseline, *non_graph_tail], failed.status
        # Use the legacy result rather than rebuilding it: duplicate-support
        # overlays and its frozen baseline copies are part of the stronger
        # selected-source graph contract.
        if status.state == "active":
            return [*activated.chunks[:len(self._baseline)], *host_tail], status
        return [*self._baseline, *host_tail], status

    def fail_closed_result(self, reason: str):
        """Keep workflow callers behind the bridge on seam-level failure."""
        failed = self._fail_closed_activation(reason)
        if failed is None:
            return list(self._baseline), None
        return list(self._baseline), failed.status

    def _fail_closed_activation(
        self, reason: str
    ) -> ActivatedSourceGraphResult | None:
        if self._service is None:
            return None
        try:
            failed = self._service.fail_closed(
                self._notebook_id, self._baseline, reason
            )
            if (
                not self._valid_activation_result(failed)
                or not self._activation_preserves_frozen_baseline(failed)
            ):
                return None
            return failed
        except Exception:
            return None

    @staticmethod
    def _valid_activation_result(result: object) -> bool:
        try:
            return (
                type(result) is ActivatedSourceGraphResult
                and type(result.status) is SourceGraphStatus
                and type(result.status.state) is str
                and result.status.state in _SOURCE_GRAPH_STATES
                and type(result.status.reason) is str
                and type(result.chunks) is tuple
                and type(result.baseline_chunks) is tuple
                and type(result.enrichment_chunks) is tuple
            )
        except Exception:
            return False

    def _activation_preserves_frozen_baseline(
        self, result: ActivatedSourceGraphResult
    ) -> bool:
        baseline = self._baseline
        if (
            len(result.baseline_chunks) != len(baseline)
            or any(
                candidate is not original
                for candidate, original in zip(result.baseline_chunks, baseline)
            )
        ):
            return False
        if result.status.state != "active":
            return (
                len(result.chunks) == len(baseline)
                and all(
                    self._baseline_chunk_is_exact(candidate, original)
                    for candidate, original in zip(result.chunks, baseline)
                )
                and (
                    result.status.state == "shadow"
                    or not result.enrichment_chunks
                )
            )
        if (
            result.status.baseline_preserved is not True
            or type(result.status.baseline_evicted_count) is not int
            or result.status.baseline_evicted_count != 0
            or len(result.chunks)
            != len(baseline) + len(result.enrichment_chunks)
        ):
            return False
        prefix = result.chunks[:len(baseline)]
        tail = result.chunks[len(baseline):]
        return (
            all(
                self._baseline_chunk_is_monotonic(candidate, original)
                for candidate, original in zip(prefix, baseline)
            )
            and all(
                candidate is enrichment
                for candidate, enrichment in zip(tail, result.enrichment_chunks)
            )
            and not (
                {chunk.chunk_id for chunk in baseline}
                & {chunk.chunk_id for chunk in result.enrichment_chunks}
            )
        )

    @classmethod
    def _baseline_chunk_is_monotonic(
        cls, candidate: RetrievedChunk, original: RetrievedChunk
    ) -> bool:
        return (
            cls._chunk_baseline_signature(candidate)
            == cls._chunk_baseline_signature(original)
            and all(
                support in candidate.retrieval_supports
                for support in original.retrieval_supports
            )
        )

    @classmethod
    def _baseline_chunk_is_exact(
        cls, candidate: RetrievedChunk, original: RetrievedChunk
    ) -> bool:
        return (
            cls._chunk_baseline_signature(candidate)
            == cls._chunk_baseline_signature(original)
            and candidate.retrieval_supports == original.retrieval_supports
        )

    @staticmethod
    def _chunk_baseline_signature(chunk: RetrievedChunk) -> tuple[object, ...]:
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


def selected_evidence_lane_is_dormant(host: Any) -> bool:
    """True when an unconfigured graph leaves ``selected_evidence`` with nothing.

    Delegates to ``app.domain.extensions.lane_is_dormant`` for the generic
    probe-safety argument (defensive read, fail into the host) shared with
    ``generated_question_contribution.generated_question_lane_is_dormant``.
    What is lane-specific here: the unit that keeps or drops together is the
    *registration* under a plugin manifest's ``requires``, not the individual
    contribution -- a manifest that registers more than one
    ``selected_evidence`` contribution shares one ``requires`` set, so
    disabling the graph capability filters every contribution that manifest
    registers, not just the one that logically needs it.
    """
    return lane_is_dormant(
        host,
        "selected_evidence",
        UNCONFIGURED_SELECTED_SOURCE_GRAPH_CAPABILITIES,
    )


def selected_source_graph_call_context(
    call: SelectedSourceGraphContributionCall,
    *,
    actor_id: str,
    cancel_event: Any,
    connection_probe: Any,
    admission_hydrate: Callable[
        [str, str, Sequence[str]], Sequence[RetrievedChunk]
    ],
    admission_leaf_io: Callable[[], Any] | None = None,
    max_results: int,
    max_tokens: int,
) -> RetrievalContributionCallContext:
    """Build a content-free typed invocation envelope without any I/O."""
    from app.services.retrieval_run import current_retrieval_run

    scope = current_source_scope()
    run = current_retrieval_run()
    run_id = str(getattr(run, "run_id", "") or "selected-evidence")
    # The ref only correlates one already-frozen call; hashing every source id
    # here would add O(scope size) work to the all-selected hot path.
    scope_id = hashlib.sha256(
        f"{run_id}:{id(call)}".encode("ascii")
    ).hexdigest()
    execution_limit = max(_MIN_CONTRIBUTION_EXECUTION_BUDGET, int(max_results))
    return RetrievalContributionCallContext(
        actor_id=actor_id,
        notebook_id=call._notebook_id,
        scope_id=scope_id,
        scope_narrowed=bool(getattr(scope, "restricted", False)),
        run_id=run_id,
        run_kind=str(getattr(run, "run_kind", "") or "ask"),
        cancellation=_AskCancellationToken(cancel_event),
        max_items=execution_limit,
        max_tokens=max(_MIN_CONTRIBUTION_EXECUTION_BUDGET, int(max_tokens)),
        max_proposals=execution_limit,
        admission_source=_SelectedEvidenceAdmissionSource(
            call if call._service is not None else None,
            call._notebook_id,
            actor_id,
            admission_hydrate,
            admission_leaf_io,
        ),
        selected_source_graph_source=(
            call if call._service is not None else None
        ),
        connection_probe=connection_probe,
    )
