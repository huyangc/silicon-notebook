"""Bounded graph tools over an immutable selected-source snapshot.

The service is deliberately shadow-only in this PR: Ask and Deep Report do not
call it yet.  Every method rechecks the snapshot's capability and ownership
boundary, so later orchestration cannot accidentally turn a source-local tool
into a whole-notebook fallback.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.services.exact_lookup import (
    ExactLookupDeps,
    ExactLookupLimits,
    exact_lookup_chunks,
)
from app.services.kg.follow_chain import (
    TRANSITIVE_EDGE_TYPES,
    InferredChain,
    compose_two_hop_paths,
)
from app.services.retrieval import RetrievedChunk, _tokens, keyword_basis
from app.services.source_subgraph import (
    SourceSubgraphCapability,
    SourceSubgraphChunk,
    SourceSubgraphFact,
    SourceSubgraphNode,
    SourceSubgraphRelation,
    SourceSubgraphSnapshot,
)


@dataclass(frozen=True)
class SourceGraphPrimitiveLimits:
    max_fan_out: int = 16
    max_expand_depth: int = 3
    max_expand_nodes: int = 128
    max_chain_results: int = 16
    max_relation_results: int = 32
    max_enumeration_page: int = 100


@dataclass(frozen=True)
class SourceGraphNeighborResult:
    capability: SourceSubgraphCapability
    nodes: tuple[SourceSubgraphNode, ...] = ()
    relations: tuple[SourceSubgraphRelation, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class SourceGraphChainResult:
    capability: SourceSubgraphCapability
    inferences: tuple[InferredChain, ...] = ()
    nodes: tuple[SourceSubgraphNode, ...] = ()


@dataclass(frozen=True)
class SourceGraphRelationHit:
    relation: SourceSubgraphRelation
    source_name: str
    target_name: str
    score: float


@dataclass(frozen=True)
class SourceGraphRelationResult:
    capability: SourceSubgraphCapability
    hits: tuple[SourceGraphRelationHit, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class SourceGraphExactResult:
    capability: SourceSubgraphCapability
    chunks: tuple[RetrievedChunk, ...] = ()


@dataclass(frozen=True)
class SourceGraphEnumerationResult:
    capability: SourceSubgraphCapability
    kind: str
    items: tuple[SourceSubgraphNode | SourceSubgraphFact | SourceSubgraphChunk, ...]
    total: int
    next_cursor: str
    complete: bool


def _disabled(reason: str) -> SourceSubgraphCapability:
    return SourceSubgraphCapability(False, reason)


def _row_id(
    value: SourceSubgraphNode | SourceSubgraphFact | SourceSubgraphChunk,
) -> str:
    if isinstance(value, SourceSubgraphNode):
        return value.object_id
    if isinstance(value, SourceSubgraphFact):
        return value.fact_id
    return value.chunk_id


class SourceGraphPrimitives:
    """Pure bounded consumers of ``SourceSubgraphSnapshot``."""

    def __init__(self, *, snapshots: Any, settings: Any) -> None:
        self._snapshots = snapshots
        self._settings = settings
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

    def snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> SourceSubgraphSnapshot:
        return self._snapshots.snapshot(notebook_id, source_ids)

    def neighbors(
        self,
        snapshot: SourceSubgraphSnapshot,
        object_id: str,
        *,
        direction: str = "both",
        max_results: int = 8,
    ) -> SourceGraphNeighborResult:
        capability = snapshot.capability("neighbors")
        if not capability.enabled:
            return SourceGraphNeighborResult(capability)
        if direction not in {"out", "in", "both"}:
            return SourceGraphNeighborResult(_disabled("invalid_direction"))
        _allowed, nodes = self._owned_snapshot(snapshot)
        if object_id not in nodes:
            return SourceGraphNeighborResult(_disabled("start_node_out_of_scope"))
        limit = max(1, min(int(max_results), self._limits.max_fan_out))
        matches = []
        for relation in self._safe_relations(snapshot, nodes):
            is_out = relation.source_object_id == object_id
            is_in = relation.target_object_id == object_id
            if (
                (direction == "out" and is_out)
                or (direction == "in" and is_in)
                or (direction == "both" and (is_out or is_in))
            ):
                matches.append(relation)
        matches.sort(key=lambda row: (row.edge_type, row.relation_id))
        selected = matches[:limit]
        neighbor_ids = []
        for relation in selected:
            neighbor_id = (
                relation.target_object_id
                if relation.source_object_id == object_id
                else relation.source_object_id
            )
            if neighbor_id not in neighbor_ids:
                neighbor_ids.append(neighbor_id)
        return SourceGraphNeighborResult(
            capability=capability,
            nodes=tuple(nodes[node_id] for node_id in neighbor_ids),
            relations=tuple(selected),
            truncated=len(matches) > limit,
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

    def follow_chain(
        self,
        snapshot: SourceSubgraphSnapshot,
        start_object_id: str,
        *,
        direction: str = "out",
        edge_type: str | None = None,
        target_object_id: str = "",
        max_fan_out: int = 8,
        max_results: int = 4,
    ) -> SourceGraphChainResult:
        capability = snapshot.capability("follow_chain")
        if not capability.enabled:
            return SourceGraphChainResult(capability)
        _allowed, nodes = self._owned_snapshot(snapshot)
        if start_object_id not in nodes:
            return SourceGraphChainResult(_disabled("start_node_out_of_scope"))
        if edge_type and edge_type not in TRANSITIVE_EDGE_TYPES:
            return SourceGraphChainResult(_disabled("unsupported_chain_edge_type"))
        requested_types = (
            (edge_type,) if edge_type else tuple(sorted(TRANSITIVE_EDGE_TYPES))
        )
        safe_relations = self._safe_relations(snapshot, nodes)
        fan_out = max(1, min(int(max_fan_out), self._limits.max_fan_out))

        by_source: dict[str, list[SourceSubgraphRelation]] = {}
        by_target: dict[str, list[SourceSubgraphRelation]] = {}
        for relation in safe_relations:
            if relation.edge_type not in requested_types:
                continue
            by_source.setdefault(relation.source_object_id, []).append(relation)
            by_target.setdefault(relation.target_object_id, []).append(relation)

        def bounded_edges(
            index: Mapping[str, list[SourceSubgraphRelation]],
            object_id: str,
            requested_type: str,
        ) -> list[SourceSubgraphRelation]:
            rows = [
                row
                for row in index.get(object_id, ())
                if row.edge_type == requested_type
            ]
            rows.sort(
                key=lambda row: (
                    0 if row.review_status == "verified" else 1,
                    row.edge_type,
                    row.relation_id,
                )
            )
            return rows[:fan_out]

        relation_frontier: dict[str, SourceSubgraphRelation] = {}
        directions = ("out", "in") if direction == "both" else (direction,)
        if any(value not in {"out", "in"} for value in directions):
            return SourceGraphChainResult(_disabled("invalid_direction"))
        for walk_direction in directions:
            index = by_source if walk_direction == "out" else by_target
            for requested_type in requested_types:
                first_hops = bounded_edges(index, start_object_id, requested_type)
                for relation in first_hops:
                    relation_frontier[relation.relation_id] = relation
                    middle_id = (
                        relation.target_object_id
                        if walk_direction == "out"
                        else relation.source_object_id
                    )
                    for second in bounded_edges(index, middle_id, requested_type):
                        relation_frontier[second.relation_id] = second

        relation_rows = [
            {
                "id": relation.relation_id,
                "notebook_id": snapshot.active_notebook_id,
                "tier": "personal",
                "source_id": relation.source_id,
                "source_object_id": relation.source_object_id,
                "target_object_id": relation.target_object_id,
                "edge_type": relation.edge_type,
                "review_status": relation.review_status,
                "evidence": [dict(item) for item in relation.evidence],
            }
            for relation in relation_frontier.values()
        ]
        frontier_node_ids = {
            start_object_id,
            target_object_id,
            *(
                object_id
                for relation in relation_frontier.values()
                for object_id in (
                    relation.source_object_id,
                    relation.target_object_id,
                )
            ),
        }
        fact_payloads = {fact.object_id: fact.payload for fact in snapshot.facts}
        node_rows = {
            object_id: {
                "id": object_id,
                "object_type": node.object_type,
                "status": "approved",
                "name": node.name,
                "payload": dict(fact_payloads.get(object_id, {})),
            }
            for object_id, node in nodes.items()
            if object_id in frontier_node_ids
        }
        result_limit = max(1, min(int(max_results), self._limits.max_chain_results))
        inferences = compose_two_hop_paths(
            node_rows,
            relation_rows,
            start_object_id,
            direction=direction,
            edge_type=edge_type,
            target_object_id=target_object_id,
            direct_triples=frozenset(
                (
                    relation.source_object_id,
                    relation.target_object_id,
                    relation.edge_type,
                )
                for relation in safe_relations
            ),
            max_results=result_limit,
        )
        used_ids = {
            object_id
            for chain in inferences
            for object_id in (chain.source_id, chain.via_id, chain.target_id)
        }
        return SourceGraphChainResult(
            capability=capability,
            inferences=tuple(inferences),
            nodes=tuple(nodes[object_id] for object_id in sorted(used_ids)),
        )

    def retrieve_relations(
        self,
        snapshot: SourceSubgraphSnapshot,
        query: str,
        *,
        endpoint_object_ids: Sequence[str] = (),
        max_results: int = 12,
    ) -> SourceGraphRelationResult:
        capability = snapshot.capability("neighbors")
        if not capability.enabled:
            return SourceGraphRelationResult(capability)
        _allowed, nodes = self._owned_snapshot(snapshot)
        endpoints = {
            object_id for object_id in endpoint_object_ids if object_id in nodes
        }
        basis = keyword_basis(query)
        hits = []
        for relation in self._safe_relations(snapshot, nodes):
            source_name = nodes[relation.source_object_id].name
            target_name = nodes[relation.target_object_id].name
            evidence_text = " ".join(
                str(item.get("quote") or item.get("quoted_span") or "")
                for item in relation.evidence
            )
            text = " ".join(
                (source_name, relation.edge_type, target_name, evidence_text)
            )
            coverage = basis.coverage(set(_tokens(text)), text)
            adjacent = bool(
                endpoints & {relation.source_object_id, relation.target_object_id}
            )
            if coverage <= 0 and not adjacent:
                continue
            score = max(coverage, 0.01 if adjacent else 0.0)
            hits.append(
                SourceGraphRelationHit(
                    relation=relation,
                    source_name=source_name,
                    target_name=target_name,
                    score=score,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.relation.relation_id))
        limit = max(1, min(int(max_results), self._limits.max_relation_results))
        return SourceGraphRelationResult(
            capability=capability,
            hits=tuple(hits[:limit]),
            truncated=len(hits) > limit,
        )

    def exact_lookup(
        self, snapshot: SourceSubgraphSnapshot, query: str
    ) -> SourceGraphExactResult:
        capability = snapshot.capability("exact_lookup")
        if not capability.enabled:
            return SourceGraphExactResult(capability)
        allowed = set(snapshot.allowed_source_ids)
        chunks = tuple(chunk for chunk in snapshot.chunks if chunk.source_id in allowed)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}

        def exact_search(_db, _notebook_id, needle, limit):
            folded = needle.casefold()
            hits = [chunk for chunk in chunks if folded in chunk.text.casefold()]
            hits.sort(
                key=lambda chunk: (
                    -chunk.text.casefold().count(folded),
                    chunk.source_id,
                    chunk.chunk_id,
                )
            )
            return [
                {
                    "chunk_id": chunk.chunk_id,
                    "source_id": chunk.source_id,
                    "section_path": chunk.section_path,
                }
                for chunk in hits[: max(0, int(limit))]
            ]

        def section_rows(_db, _notebook_id, source_id, section_path, limit):
            prefix = section_path + " > "
            rows = [
                chunk
                for chunk in chunks
                if chunk.source_id == source_id
                and (
                    chunk.section_path == section_path
                    or chunk.section_path.startswith(prefix)
                )
            ]
            rows.sort(key=lambda chunk: (chunk.created_at, chunk.chunk_id))
            return [self._chunk_row(chunk) for chunk in rows[: max(0, int(limit))]]

        def chunk_rows(_db, chunk_ids):
            return [
                self._chunk_row(by_id[value]) for value in chunk_ids if value in by_id
            ]

        rows = exact_lookup_chunks(
            ExactLookupDeps(
                connect=lambda: nullcontext(None),
                exact_search=exact_search,
                section_rows=section_rows,
                chunk_rows=chunk_rows,
            ),
            snapshot.active_notebook_id,
            query,
            ExactLookupLimits(
                max_identifiers=self._settings.exact_lookup_max_identifiers,
                fts_k=self._settings.exact_lookup_fts_k,
                max_sections=self._settings.exact_lookup_max_sections,
                max_chunks_per_section=self._settings.exact_lookup_max_chunks_per_section,
            ),
        )
        safe = tuple(chunk for chunk in rows if chunk.source_id in allowed)
        return SourceGraphExactResult(capability=capability, chunks=safe)

    @staticmethod
    def _chunk_row(chunk: SourceSubgraphChunk) -> dict[str, Any]:
        return {
            "id": chunk.chunk_id,
            "source_id": chunk.source_id,
            "source_title": "",
            "section_path": chunk.section_path,
            "text": chunk.text,
            "element_ids": list(chunk.element_ids),
        }

    def enumerate(
        self,
        snapshot: SourceSubgraphSnapshot,
        kind: str,
        *,
        after: str = "",
        limit: int = 40,
    ) -> SourceGraphEnumerationResult:
        capability_name = {
            "nodes": "neighbors",
            "facts": "source_facts",
            "chunks": "enumeration",
        }.get(kind, "")
        capability = (
            snapshot.capability(capability_name)
            if capability_name
            else _disabled("unsupported_enumeration_kind")
        )
        if not capability.enabled:
            return SourceGraphEnumerationResult(capability, kind, (), 0, "", False)
        collections = {
            "nodes": tuple(snapshot.nodes),
            "facts": tuple(snapshot.facts),
            "chunks": tuple(snapshot.chunks),
        }
        values = collections.get(kind)
        assert values is not None
        allowed = set(snapshot.allowed_source_ids)
        scoped = []
        for value in values:
            if isinstance(value, SourceSubgraphNode):
                owned = bool(value.source_ids) and set(value.source_ids) <= allowed
            else:
                owned = value.source_id in allowed
            if owned:
                scoped.append(value)
        scoped.sort(key=_row_id)
        page_limit = max(1, min(int(limit), self._limits.max_enumeration_page))
        remaining = [value for value in scoped if _row_id(value) > after]
        page = tuple(remaining[:page_limit])
        has_more = len(remaining) > page_limit
        return SourceGraphEnumerationResult(
            capability=capability,
            kind=kind,
            items=page,
            total=len(scoped),
            next_cursor=_row_id(page[-1]) if has_more and page else "",
            complete=not has_more,
        )
