"""Sparse Personalized PageRank inside one selected-source snapshot.

This remains an internal/shadow producer. It never opens a whole-notebook
artifact and never filters a library-wide PPR result after traversal.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from app.services.kg.scale_index import build_transition, personalized_ppr
from app.services.retrieval import RetrievalSupport
from app.services.source_subgraph import (
    SourceSubgraphCapability,
    SourceSubgraphChunk,
    SourceSubgraphNode,
    SourceSubgraphSnapshot,
)


@dataclass(frozen=True)
class SourceSubgraphPprLimits:
    max_nodes: int = 40_000
    max_logical_edges: int = 100_000
    max_results: int = 100
    cache_entries: int = 8

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.max_nodes,
                self.max_logical_edges,
                self.max_results,
                self.cache_entries,
            )
        ):
            raise ValueError("source-subgraph PPR limits must be positive")


@dataclass(frozen=True)
class SourceSubgraphPprHit:
    chunk: SourceSubgraphChunk
    score: float
    support: RetrievalSupport


@dataclass(frozen=True)
class SourceSubgraphPprResult:
    capability: SourceSubgraphCapability
    hits: tuple[SourceSubgraphPprHit, ...] = ()
    cache_hit: bool = False
    node_count: int = 0
    logical_edge_count: int = 0
    iterations: int = 0


@dataclass(frozen=True)
class _SparseGraph:
    transition: Any
    node_index: Mapping[str, int]
    chunks_by_index: Mapping[int, SourceSubgraphChunk]
    node_count: int
    logical_edge_count: int


def _disabled(reason: str) -> SourceSubgraphCapability:
    return SourceSubgraphCapability(False, reason)


def _snapshot_key(snapshot: SourceSubgraphSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.active_notebook_id,
        snapshot.scope_hash,
        snapshot.kg_generation,
        snapshot.cluster_generation,
        tuple(
            (
                state.source_id,
                state.generation,
                state.projection_version,
                state.projection_status,
            )
            for state in snapshot.source_generations
        ),
    )


class SourceSubgraphPprService:
    """Build and cache one bounded CSR transition per frozen source scope."""

    def __init__(
        self,
        *,
        settings: Any,
        limits: SourceSubgraphPprLimits | None = None,
    ) -> None:
        self._settings = settings
        self._limits = limits or SourceSubgraphPprLimits()
        self._cache: OrderedDict[tuple[Any, ...], _SparseGraph] = OrderedDict()
        self._lock = threading.RLock()

    @property
    def cache_size(self) -> int:
        with self._lock:
            return len(self._cache)

    @staticmethod
    def _owned_nodes(
        snapshot: SourceSubgraphSnapshot,
    ) -> dict[str, SourceSubgraphNode]:
        allowed = set(snapshot.allowed_source_ids)
        return {
            node.object_id: node
            for node in snapshot.nodes
            if node.source_ids and set(node.source_ids) <= allowed
        }

    def _build(self, snapshot: SourceSubgraphSnapshot) -> _SparseGraph:
        allowed = set(snapshot.allowed_source_ids)
        nodes = self._owned_nodes(snapshot)
        chunks = {
            chunk.chunk_id: chunk
            for chunk in snapshot.chunks
            if chunk.source_id in allowed
        }

        cluster_groups: dict[str, list[str]] = defaultdict(list)
        for canonical_id, object_id in snapshot.cluster_memberships:
            if object_id in nodes:
                cluster_groups[canonical_id].append(object_id)
        cluster_groups = {
            canonical_id: list(dict.fromkeys(members))
            for canonical_id, members in cluster_groups.items()
            if members
        }

        node_ids = [*nodes]
        node_ids.extend(f"chunk:{chunk_id}" for chunk_id in chunks)
        node_ids.extend(f"cluster:{canonical_id}" for canonical_id in cluster_groups)
        if len(node_ids) > self._limits.max_nodes:
            raise OverflowError("ppr_node_limit_exceeded")

        pairs: set[tuple[str, str]] = set()

        def add_pair(left: str, right: str) -> None:
            if not left or not right or left == right:
                return
            pair = (left, right) if left < right else (right, left)
            pairs.add(pair)
            if len(pairs) > self._limits.max_logical_edges:
                raise OverflowError("ppr_edge_limit_exceeded")

        for relation in snapshot.relations:
            evidence_is_owned = bool(relation.evidence) and all(
                str(item.get("source_id") or "") == relation.source_id
                and relation.source_id in allowed
                for item in relation.evidence
            )
            if (
                relation.source_id in allowed
                and relation.source_object_id in nodes
                and relation.target_object_id in nodes
                and relation.review_status in {"pending", "verified"}
                and evidence_is_owned
            ):
                add_pair(relation.source_object_id, relation.target_object_id)
        for object_id, chunk_id in snapshot.memberships:
            if object_id in nodes and chunk_id in chunks:
                add_pair(object_id, f"chunk:{chunk_id}")
        for canonical_id, members in cluster_groups.items():
            router = f"cluster:{canonical_id}"
            for object_id in members:
                add_pair(router, object_id)

        reciprocal_edges = []
        for left, right in pairs:
            reciprocal_edges.append((left, right, 1.0))
            reciprocal_edges.append((right, left, 1.0))
        transition, node_index = build_transition(node_ids, reciprocal_edges)
        chunks_by_index = {
            node_index[f"chunk:{chunk_id}"]: chunk for chunk_id, chunk in chunks.items()
        }
        return _SparseGraph(
            transition=transition,
            node_index=node_index,
            chunks_by_index=chunks_by_index,
            node_count=len(node_ids),
            logical_edge_count=len(pairs),
        )

    def _graph(self, snapshot: SourceSubgraphSnapshot) -> tuple[_SparseGraph, bool]:
        key = _snapshot_key(snapshot)
        # Hold the bounded service lock through build: two simultaneous asks
        # for the same cold scope must not allocate two CSR transitions and
        # temporarily defeat the memory rail. Different cold scopes serialize
        # as well; this lane is shadow-only and each build has fixed O(N+E)
        # bounds, so predictable peak memory wins over cold-build throughput.
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached, True
            # Reserve the incoming entry before allocating its CSR and build
            # temporaries. Peak retained graphs are therefore at most the
            # configured capacity, not capacity + one cold in-flight graph.
            if len(self._cache) >= self._limits.cache_entries:
                self._cache.popitem(last=False)
            graph = self._build(snapshot)
            self._cache[key] = graph
            self._cache.move_to_end(key)
            return graph, False

    def retrieve(
        self,
        snapshot: SourceSubgraphSnapshot,
        *,
        object_seeds: Mapping[str, float] | None = None,
        chunk_seeds: Mapping[str, float] | None = None,
        max_results: int = 20,
    ) -> SourceSubgraphPprResult:
        if not bool(getattr(self._settings, "source_subgraph_ppr_enabled", False)):
            return SourceSubgraphPprResult(_disabled("ppr_disabled"))
        capability = snapshot.capability("ppr")
        if not capability.enabled:
            return SourceSubgraphPprResult(capability)
        try:
            requested_results = int(max_results)
        except (TypeError, ValueError):
            requested_results = 0
        if requested_results <= 0:
            return SourceSubgraphPprResult(capability)
        try:
            graph, cache_hit = self._graph(snapshot)
        except OverflowError as exc:
            return SourceSubgraphPprResult(_disabled(str(exc)))
        except Exception:
            return SourceSubgraphPprResult(_disabled("ppr_build_failed"))

        reset = np.zeros(graph.node_count, dtype=np.float64)

        def positive_weight(value: Any) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return 0.0
            return parsed if np.isfinite(parsed) and parsed > 0 else 0.0

        for object_id, weight in (object_seeds or {}).items():
            index = graph.node_index.get(str(object_id))
            parsed_weight = positive_weight(weight)
            if index is not None and parsed_weight:
                reset[index] += parsed_weight
        passage_weight = positive_weight(
            getattr(self._settings, "ppr_passage_node_weight", 0.05)
        )
        for chunk_id, weight in (chunk_seeds or {}).items():
            index = graph.node_index.get(f"chunk:{chunk_id}")
            parsed_weight = positive_weight(weight)
            if index is not None and parsed_weight:
                reset[index] += parsed_weight * passage_weight
        if not np.any(reset > 0):
            return SourceSubgraphPprResult(
                capability=capability,
                cache_hit=cache_hit,
                node_count=graph.node_count,
                logical_edge_count=graph.logical_edge_count,
            )

        stats: dict[str, int] = {}
        try:
            scores = personalized_ppr(
                graph.transition,
                reset,
                damping=float(getattr(self._settings, "ppr_damping", 0.5)),
                tol=float(getattr(self._settings, "ppr_tol", 1e-6)),
                stats=stats,
            )
        except Exception:
            return SourceSubgraphPprResult(
                capability=_disabled("ppr_run_failed"),
                cache_hit=cache_hit,
                node_count=graph.node_count,
                logical_edge_count=graph.logical_edge_count,
            )

        raw = [
            (chunk, float(scores[index]))
            for index, chunk in graph.chunks_by_index.items()
        ]
        if not raw:
            return SourceSubgraphPprResult(
                capability=capability,
                cache_hit=cache_hit,
                node_count=graph.node_count,
                logical_edge_count=graph.logical_edge_count,
                iterations=int(stats.get("iters", 0)),
            )
        values = [score for _chunk, score in raw]
        low, high = min(values), max(values)
        span = high - low
        ranked = [
            (chunk, (score - low) / span if span > 0 else 0.0) for chunk, score in raw
        ]
        ranked.sort(key=lambda row: (-row[1], row[0].chunk_id))
        limit = min(requested_results, self._limits.max_results)
        hits = tuple(
            SourceSubgraphPprHit(
                chunk=chunk,
                score=score,
                support=RetrievalSupport(origin="ppr", support_kind="ppr", score=score),
            )
            for chunk, score in ranked[:limit]
        )
        return SourceSubgraphPprResult(
            capability=capability,
            hits=hits,
            cache_hit=cache_hit,
            node_count=graph.node_count,
            logical_edge_count=graph.logical_edge_count,
            iterations=int(stats.get("iters", 0)),
        )
