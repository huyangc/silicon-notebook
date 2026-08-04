"""Bounded, source-induced graph snapshots for selected-source retrieval.

This module is deliberately not wired into Ask or Deep Report yet.  It owns an
immutable, generation-keyed cache over backend-neutral projection rows.  The
database adapters perform authorization predicates and endpoint checks before
every LIMIT; this service only normalizes the already-bounded result and
derives per-channel capabilities.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence


GRAPH_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class SourceSubgraphLimits:
    sources: int
    objects: int
    relations: int
    chunks: int
    facts: int
    fact_elements: int
    memberships: int
    cluster_memberships: int

    @classmethod
    def from_settings(cls, settings: Any) -> "SourceSubgraphLimits":
        return cls(
            sources=int(settings.source_subgraph_max_sources),
            objects=int(settings.source_subgraph_max_objects),
            relations=int(settings.source_subgraph_max_relations),
            chunks=int(settings.source_subgraph_max_chunks),
            facts=int(settings.source_subgraph_max_facts),
            fact_elements=int(settings.source_subgraph_max_fact_elements),
            memberships=int(settings.source_subgraph_max_memberships),
            cluster_memberships=int(settings.source_subgraph_max_cluster_memberships),
        )

    def as_mapping(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                "objects": self.objects,
                "relations": self.relations,
                "chunks": self.chunks,
                "facts": self.facts,
                "fact_elements": self.fact_elements,
                "memberships": self.memberships,
                "cluster_memberships": self.cluster_memberships,
            }
        )


@dataclass(frozen=True)
class SourceSubgraphCapability:
    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class SourceSubgraphSource:
    source_id: str
    generation: str
    expected_objects: int
    projection_version: int
    projection_status: str


@dataclass(frozen=True)
class SourceSubgraphNode:
    object_id: str
    object_type: str
    name: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceSubgraphRelation:
    relation_id: str
    source_id: str
    source_object_id: str
    target_object_id: str
    edge_type: str
    evidence: tuple[Mapping[str, Any], ...]
    review_status: str = "pending"


@dataclass(frozen=True)
class SourceSubgraphChunk:
    chunk_id: str
    source_id: str
    text: str
    section_path: str
    created_at: str
    element_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceSubgraphFact:
    fact_id: str
    source_id: str
    source_generation: str
    object_id: str
    object_type: str
    payload: Mapping[str, Any]
    evidence: tuple[Mapping[str, Any], ...]
    element_ids: tuple[str, ...]


@dataclass(frozen=True)
class SourceSubgraphSnapshot:
    active_notebook_id: str
    allowed_source_ids: tuple[str, ...]
    scope_hash: str
    kg_generation: int
    cluster_generation: int
    source_generations: tuple[SourceSubgraphSource, ...]
    nodes: tuple[SourceSubgraphNode, ...]
    relations: tuple[SourceSubgraphRelation, ...]
    chunks: tuple[SourceSubgraphChunk, ...]
    memberships: tuple[tuple[str, str], ...]
    cluster_memberships: tuple[tuple[str, str], ...]
    facts: tuple[SourceSubgraphFact, ...]
    capabilities: Mapping[str, SourceSubgraphCapability]
    complete: bool
    degraded_reasons: tuple[str, ...]

    def capability(self, name: str) -> SourceSubgraphCapability:
        return self.capabilities.get(
            name, SourceSubgraphCapability(False, "unknown_channel")
        )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _evidence(value: Any, source_id: str) -> tuple[Mapping[str, Any], ...]:
    out = []
    for item in _json_list(value):
        if not isinstance(item, dict):
            continue
        if str(item.get("source_id") or "") != source_id:
            continue
        out.append(_freeze_json(item))
    return tuple(out)


def _scope_hash(notebook_id: str, source_ids: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(notebook_id.encode("utf-8"))
    for source_id in source_ids:
        digest.update(b"\0")
        digest.update(source_id.encode("utf-8"))
    return digest.hexdigest()[:24]


class SourceSubgraphService:
    """Build and cache immutable source-induced projections.

    ``source_ids`` must be the server-resolved include list.  Empty always means
    empty; this service never interprets it as whole-notebook access.
    """

    def __init__(self, *, projections: Any, settings: Any) -> None:
        self._projections = projections
        self._limits = SourceSubgraphLimits.from_settings(settings)
        self._cache_max = int(settings.source_subgraph_cache_max_entries)
        self._cache: "OrderedDict[tuple[Any, ...], SourceSubgraphSnapshot]" = (
            OrderedDict()
        )
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_sources(source_ids: Sequence[str]) -> tuple[str, ...]:
        return tuple(sorted({str(value) for value in source_ids if str(value)}))

    def invalidate_notebook(self, notebook_id: str) -> None:
        with self._lock:
            stale = [key for key in self._cache if key[0] == notebook_id]
            for key in stale:
                self._cache.pop(key, None)

    def snapshot(
        self, notebook_id: str, source_ids: Sequence[str]
    ) -> SourceSubgraphSnapshot:
        allowed = self._normalize_sources(source_ids)
        scope_hash = _scope_hash(notebook_id, allowed)
        if not allowed:
            return self._empty(notebook_id, allowed, scope_hash, "empty_source_scope")
        if len(allowed) > self._limits.sources:
            return self._empty(
                notebook_id, allowed, scope_hash, "source_limit_exceeded"
            )

        signature = self._projections.source_subgraph_signature(notebook_id, allowed)
        key = (
            notebook_id,
            scope_hash,
            GRAPH_CONTRACT_VERSION,
            signature,
            self._limits,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        raw = self._projections.source_subgraph_rows(
            notebook_id, allowed, self._limits.as_mapping()
        )
        snapshot = self._normalize(notebook_id, allowed, scope_hash, raw)
        actual_key = (
            notebook_id,
            scope_hash,
            GRAPH_CONTRACT_VERSION,
            tuple(raw.get("signature") or ()),
            self._limits,
        )
        with self._lock:
            existing = self._cache.get(actual_key)
            if existing is not None:
                self._cache.move_to_end(actual_key)
                return existing
            self._cache[actual_key] = snapshot
            self._cache.move_to_end(actual_key)
            while len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
        return snapshot

    def _empty(
        self,
        notebook_id: str,
        source_ids: tuple[str, ...],
        scope_hash: str,
        reason: str,
    ) -> SourceSubgraphSnapshot:
        capability = MappingProxyType(
            {
                name: SourceSubgraphCapability(False, reason)
                for name in (
                    "neighbors",
                    "follow_chain",
                    "exact_lookup",
                    "enumeration",
                    "ppr",
                    "source_facts",
                    "community",
                    "variant_edges",
                    "synonym_edges",
                    "mention_bridge",
                )
            }
        )
        return SourceSubgraphSnapshot(
            active_notebook_id=notebook_id,
            allowed_source_ids=source_ids,
            scope_hash=scope_hash,
            kg_generation=0,
            cluster_generation=0,
            source_generations=(),
            nodes=(),
            relations=(),
            chunks=(),
            memberships=(),
            cluster_memberships=(),
            facts=(),
            capabilities=capability,
            complete=False,
            degraded_reasons=(reason,),
        )

    def _normalize(
        self,
        notebook_id: str,
        allowed: tuple[str, ...],
        scope_hash: str,
        raw: Mapping[str, Any],
    ) -> SourceSubgraphSnapshot:
        reasons = list(dict.fromkeys(str(v) for v in raw.get("reasons") or ()))
        states = tuple(
            SourceSubgraphSource(
                source_id=str(row["source_id"]),
                generation=str(row.get("generation") or ""),
                expected_objects=int(row.get("expected_objects") or 0),
                projection_version=int(row.get("projection_version") or 0),
                projection_status=str(row.get("projection_status") or "missing"),
            )
            for row in raw.get("sources") or ()
        )
        if tuple(row.source_id for row in states) != allowed:
            return self._empty(notebook_id, allowed, scope_hash, "source_scope_drift")
        object_sources: dict[str, set[str]] = defaultdict(set)
        safe_names: dict[str, str] = {}
        for row in raw.get("objects") or ():
            object_id = str(row["object_id"])
            source_id = str(row["source_id"])
            if source_id not in allowed:
                reasons.append("object_scope_violation")
                continue
            object_sources[object_id].add(source_id)
        chunks = tuple(
            SourceSubgraphChunk(
                chunk_id=str(row["chunk_id"]),
                source_id=str(row["source_id"]),
                text=str(row.get("text") or ""),
                section_path=str(row.get("section_path") or ""),
                created_at=str(row.get("created_at") or ""),
                element_ids=tuple(
                    dict.fromkeys(
                        str(v) for v in _json_list(row.get("element_ids")) if str(v)
                    )
                ),
            )
            for row in raw.get("chunks") or ()
            if str(row["source_id"]) in allowed
        )
        if len(chunks) != len(raw.get("chunks") or ()):
            reasons.append("chunk_scope_violation")
        chunk_elements: dict[str, set[str]] = {
            row.chunk_id: set(row.element_ids) for row in chunks
        }

        fact_elements: dict[str, list[str]] = defaultdict(list)
        for row in raw.get("fact_elements") or ():
            fact_elements[str(row["fact_id"])].append(str(row["element_id"]))
        facts = []
        fact_counts: dict[tuple[str, str], int] = defaultdict(int)
        fact_types: dict[str, set[str]] = defaultdict(set)
        element_objects: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in raw.get("facts") or ():
            source_id = str(row["source_id"])
            if source_id not in allowed:
                reasons.append("fact_scope_violation")
                continue
            if int(row.get("projection_version") or 0) != GRAPH_CONTRACT_VERSION:
                reasons.append("fact_projection_version_unsupported")
                continue
            object_id = str(row["object_id"])
            element_ids = tuple(
                dict.fromkeys(fact_elements.get(str(row["fact_id"]), ()))
            )
            payload = _freeze_json(_json_object(row.get("payload")))
            fact = SourceSubgraphFact(
                fact_id=str(row["fact_id"]),
                source_id=source_id,
                source_generation=str(row.get("source_generation") or ""),
                object_id=object_id,
                object_type=str(row.get("object_type") or ""),
                payload=payload,
                evidence=_evidence(row.get("evidence"), source_id),
                element_ids=element_ids,
            )
            facts.append(fact)
            fact_counts[(source_id, fact.source_generation)] += 1
            object_sources[object_id].add(source_id)
            if fact.object_type:
                fact_types[object_id].add(fact.object_type)
            name = str(payload.get("name") or "").strip()
            if name and object_id not in safe_names:
                safe_names[object_id] = name
            for element_id in element_ids:
                element_objects[(source_id, element_id)].add(object_id)

        resolved_states = []
        for state in states:
            status = state.projection_status
            if status == "live_pending_validation":
                actual = fact_counts.get((state.source_id, state.generation), 0)
                if "fact_limit_exceeded" in reasons or actual < state.expected_objects:
                    status = "incomplete"
                elif actual > state.expected_objects:
                    status = "fact_count_mismatch"
                else:
                    status = "live"
            resolved_states.append(
                SourceSubgraphSource(
                    source_id=state.source_id,
                    generation=state.generation,
                    expected_objects=state.expected_objects,
                    projection_version=state.projection_version,
                    projection_status=status,
                )
            )
        states = tuple(resolved_states)
        fully_projected = bool(states) and all(
            state.projection_status in {"live", "complete"} for state in states
        )
        if states and not fully_projected:
            reasons.append("source_projection_incomplete")

        node_types: dict[str, str] = {}
        for object_id in object_sources:
            local_types = fact_types.get(object_id, set())
            if len(local_types) == 1:
                node_types[object_id] = next(iter(local_types))
            elif len(local_types) > 1:
                reasons.append("fact_type_conflict")
                node_types[object_id] = ""
            else:
                if fully_projected:
                    reasons.append("fact_object_missing")
                node_types[object_id] = ""

        nodes = tuple(
            SourceSubgraphNode(
                object_id=object_id,
                object_type=node_types.get(object_id, ""),
                name=safe_names.get(object_id, ""),
                source_ids=tuple(sorted(source_ids)),
            )
            for object_id, source_ids in sorted(object_sources.items())
        )
        node_ids = {node.object_id for node in nodes}

        from app.services.kg.edge_schema import is_queryable_edge_pair

        relation_rows = []
        for row in raw.get("relations") or ():
            source_id = str(row["source_id"])
            source_object_id = str(row["source_object_id"])
            target_object_id = str(row["target_object_id"])
            edge_type = str(row.get("edge_type") or "")
            if source_id not in allowed:
                reasons.append("relation_scope_violation")
                continue
            if source_object_id not in node_ids or target_object_id not in node_ids:
                reasons.append("relation_endpoint_violation")
                continue
            if not node_types.get(source_object_id) or not node_types.get(
                target_object_id
            ):
                reasons.append("relation_endpoint_type_unavailable")
                continue
            if not is_queryable_edge_pair(
                edge_type,
                node_types.get(source_object_id, ""),
                node_types.get(target_object_id, ""),
            ):
                continue
            relation_rows.append(
                SourceSubgraphRelation(
                    relation_id=str(row["relation_id"]),
                    source_id=source_id,
                    source_object_id=source_object_id,
                    target_object_id=target_object_id,
                    edge_type=edge_type,
                    evidence=_evidence(row.get("evidence"), source_id),
                    review_status=str(row.get("review_status") or "pending"),
                )
            )
        relations = tuple(relation_rows)

        memberships = set()
        for chunk in chunks:
            for element_id in chunk_elements[chunk.chunk_id]:
                for object_id in element_objects.get((chunk.source_id, element_id), ()):
                    memberships.add((object_id, chunk.chunk_id))
        if len(memberships) > self._limits.memberships:
            memberships = set()
            reasons.append("membership_limit_exceeded")

        clusters = tuple(
            (str(row["canonical_id"]), str(row["member_object_id"]))
            for row in raw.get("clusters") or ()
            if str(row["member_object_id"]) in node_ids
        )
        reason_set = set(reasons)
        source_states_safe = bool(states) and all(
            state.projection_status in {"live", "complete", "incomplete"}
            and state.generation
            for state in states
        )
        base_projection_safe = source_states_safe and not (
            reason_set
            & {
                "source_scope_drift",
                "source_generation_unavailable",
                "source_projection_unavailable",
                "source_index_unavailable",
            }
        )
        topology_safe = base_projection_safe and not (
            reason_set
            & {
                "object_limit_exceeded",
                "object_scope_violation",
                "fact_object_missing",
                "fact_type_conflict",
                "fact_projection_version_unsupported",
            }
        )
        relations_safe = topology_safe and not (
            reason_set
            & {
                "relation_limit_exceeded",
                "relation_scope_violation",
                "relation_endpoint_violation",
                "relation_endpoint_type_unavailable",
            }
        )
        chunks_safe = base_projection_safe and not (
            reason_set
            & {"chunk_limit_exceeded", "chunk_scope_violation", "source_scope_drift"}
        )
        # An incomplete historical projection may still carry trustworthy
        # source-local names/evidence for the rows it did publish, but it is
        # not a complete fact or object↔chunk membership universe. Keep those
        # rows available for safe node labelling while disabling consumers
        # that would mistake the partial set for exhaustive provenance.
        facts_safe = (
            base_projection_safe
            and fully_projected
            and not (
                reason_set
                & {
                    "fact_limit_exceeded",
                    "fact_element_limit_exceeded",
                    "fact_scope_violation",
                    "fact_object_missing",
                    "fact_type_conflict",
                    "fact_projection_version_unsupported",
                }
            )
        )
        clusters_safe = topology_safe and "cluster_limit_exceeded" not in reason_set
        membership_safe = (
            facts_safe and chunks_safe and "membership_limit_exceeded" not in reason_set
        )
        capability_map = {
            "neighbors": SourceSubgraphCapability(
                relations_safe,
                ""
                if relations_safe
                else (reasons[0] if reasons else "topology_incomplete"),
            ),
            "follow_chain": SourceSubgraphCapability(
                relations_safe,
                ""
                if relations_safe
                else (reasons[0] if reasons else "topology_incomplete"),
            ),
            "exact_lookup": SourceSubgraphCapability(
                chunks_safe,
                "" if chunks_safe else (reasons[0] if reasons else "chunks_incomplete"),
            ),
            "enumeration": SourceSubgraphCapability(
                chunks_safe,
                "" if chunks_safe else (reasons[0] if reasons else "chunks_incomplete"),
            ),
            "source_facts": SourceSubgraphCapability(
                facts_safe, "" if facts_safe else "payload_provenance_incomplete"
            ),
            "ppr": SourceSubgraphCapability(
                relations_safe and clusters_safe and membership_safe,
                ""
                if relations_safe and clusters_safe and membership_safe
                else "ppr_snapshot_incomplete",
            ),
            "community": SourceSubgraphCapability(False, "whole_notebook_only"),
            "variant_edges": SourceSubgraphCapability(False, "provenance_unavailable"),
            "synonym_edges": SourceSubgraphCapability(False, "provenance_unavailable"),
            "mention_bridge": SourceSubgraphCapability(False, "provenance_unavailable"),
        }
        complete = bool(
            topology_safe
            and relations_safe
            and chunks_safe
            and facts_safe
            and clusters_safe
            and membership_safe
            and fully_projected
        )
        if not complete and not reasons:
            reasons.append("payload_provenance_incomplete")
        return SourceSubgraphSnapshot(
            active_notebook_id=notebook_id,
            allowed_source_ids=allowed,
            scope_hash=scope_hash,
            kg_generation=int(raw.get("kg_generation") or 0),
            cluster_generation=int(raw.get("cluster_generation") or 0),
            source_generations=states,
            nodes=nodes,
            relations=relations,
            chunks=chunks,
            memberships=tuple(sorted(memberships)),
            cluster_memberships=clusters,
            facts=tuple(facts),
            capabilities=MappingProxyType(capability_map),
            complete=complete,
            degraded_reasons=tuple(dict.fromkeys(reasons)),
        )
