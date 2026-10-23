"""Bounded graph expansion over an immutable selected-source snapshot.

``SelectedSourceGraphActivationService`` expands from its seeds with
``expand_graph``. It rechecks the snapshot's capability and ownership
boundary on every call, so the orchestration cannot accidentally turn a
source-local expansion into a whole-notebook fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from app.services.source_subgraph import (
    SourceSubgraphCapability,
    SourceSubgraphNode,
    SourceSubgraphRelation,
    SourceSubgraphSnapshot,
)


@dataclass(frozen=True)
class SourceGraphPrimitiveLimits:
    max_fan_out: int = 16
    max_expand_depth: int = 3
    max_expand_nodes: int = 128


@dataclass(frozen=True)
class SourceGraphNeighborResult:
    capability: SourceSubgraphCapability
    nodes: tuple[SourceSubgraphNode, ...] = ()
    relations: tuple[SourceSubgraphRelation, ...] = ()
    truncated: bool = False


def _disabled(reason: str) -> SourceSubgraphCapability:
    return SourceSubgraphCapability(False, reason)


class SourceGraphPrimitives:
    """Pure bounded consumers of ``SourceSubgraphSnapshot``."""

    def __init__(self) -> None:
        self._limits = SourceGraphPrimitiveLimits()

    @staticmethod
    def _owned_snapshot(
        snapshot: SourceSubgraphSnapshot,
    ) -> tuple[set[str], dict[str, SourceSubgraphNode]]:
        allowed = set(snapshot.allowed_source_ids)
        nodes = {
            node.object_id: node
            for node in snapshot.nodes
            if node.source_ids and set(node.source_ids) <= allowed
        }
        return allowed, nodes

    @staticmethod
    def _safe_relations(
        snapshot: SourceSubgraphSnapshot,
        nodes: Mapping[str, SourceSubgraphNode],
    ) -> tuple[SourceSubgraphRelation, ...]:
        allowed = set(snapshot.allowed_source_ids)
        return tuple(
            relation
            for relation in snapshot.relations
            if relation.source_id in allowed
            and relation.source_object_id in nodes
            and relation.target_object_id in nodes
            and relation.review_status in {"pending", "verified"}
            and relation.evidence
        )

    def expand_graph(
        self,
        snapshot: SourceSubgraphSnapshot,
        seed_object_ids: Sequence[str],
        *,
        max_depth: int = 2,
        max_fan_out: int = 8,
        max_nodes: int = 64,
    ) -> SourceGraphNeighborResult:
        capability = snapshot.capability("neighbors")
        if not capability.enabled:
            return SourceGraphNeighborResult(capability)
        _allowed, nodes = self._owned_snapshot(snapshot)
        requested_seeds = [
            node_id for node_id in dict.fromkeys(seed_object_ids) if node_id in nodes
        ]
        node_limit = max(1, min(int(max_nodes), self._limits.max_expand_nodes))
        seeds = requested_seeds[:node_limit]
        if not seeds:
            return SourceGraphNeighborResult(_disabled("seed_nodes_out_of_scope"))
        depth_limit = max(1, min(int(max_depth), self._limits.max_expand_depth))
        fan_out = max(1, min(int(max_fan_out), self._limits.max_fan_out))
        relations = self._safe_relations(snapshot, nodes)
        visited = set(seeds)
        frontier = list(seeds)
        selected_relations: dict[str, SourceSubgraphRelation] = {}
        truncated = len(requested_seeds) > node_limit
        for _depth in range(depth_limit):
            next_frontier = []
            for object_id in frontier:
                adjacent = [
                    relation
                    for relation in relations
                    if object_id
                    in {relation.source_object_id, relation.target_object_id}
                ]
                adjacent.sort(key=lambda row: (row.edge_type, row.relation_id))
                if len(adjacent) > fan_out:
                    truncated = True
                for relation in adjacent[:fan_out]:
                    other = (
                        relation.target_object_id
                        if relation.source_object_id == object_id
                        else relation.source_object_id
                    )
                    if other not in nodes:
                        continue
                    if other not in visited:
                        if len(visited) >= node_limit:
                            truncated = True
                            continue
                        visited.add(other)
                        next_frontier.append(other)
                    selected_relations[relation.relation_id] = relation
            frontier = next_frontier
            if not frontier:
                break
        ordered_nodes = tuple(nodes[node_id] for node_id in sorted(visited))
        ordered_relations = tuple(
            selected_relations[key] for key in sorted(selected_relations)
        )
        return SourceGraphNeighborResult(
            capability=capability,
            nodes=ordered_nodes,
            relations=ordered_relations,
            truncated=truncated,
        )
