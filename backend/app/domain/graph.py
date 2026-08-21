"""Stable graph reasoning result values."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


def evidence_quote(evidence: Mapping[str, object]) -> str:
    return str(evidence.get("quote") or evidence.get("quoted_span") or "").strip()


@dataclass
class ChainHop:
    """One directly stored, evidence-bearing relation in a composed path."""

    relation_id: str
    notebook_id: str
    tier: str
    source_object_id: str
    target_object_id: str
    edge_type: str
    source_name: str
    target_name: str
    evidence: list[dict] = field(default_factory=list)
    review_status: str = "pending"
    source_title: str = ""
    trust: float = 0.0

    @property
    def primary_evidence(self) -> dict:
        """First non-empty quoted evidence entry, or an empty mapping."""
        for item in self.evidence:
            if isinstance(item, dict) and evidence_quote(item):
                return item
        return {}

    @property
    def quote(self) -> str:
        return evidence_quote(self.primary_evidence)

    @property
    def location_label(self) -> str:
        ev = self.primary_evidence
        return str(ev.get("location_label") or ev.get("section_path") or "").strip()

    @property
    def object_id(self) -> str:
        """Evidence-classifier identity matching relation AnswerAnchor ids."""
        return self.relation_id


@dataclass
class InferredChain:
    """A transient ``A -> C`` inference backed by two direct relation hops."""

    source_object_id: str
    via_object_id: str
    target_object_id: str
    source_name: str
    via_name: str
    target_name: str
    inferred_edge_type: str
    hops: tuple[ChainHop, ChainHop]
    validity_scope: dict = field(default_factory=dict)
    chain_trust: float = 0.0
    notebook_id: str = ""
    tier: str = "personal"
    # Relevance of the candidate that authorized this action. The repository
    # cannot know query relevance, so orchestration sets it only after verifying
    # that the start object is in the current candidate pool. It is deliberately
    # separate from chain_trust (evidence/review/tier quality).
    query_relevance: float = 0.0
    # How the caller reached the path. The path and both hops remain normalized
    # to stored source -> target order.
    search_direction: str = "out"

    @property
    def edge_type(self) -> str:
        """Compatibility alias for consumers naming the inferred type edge_type."""
        return self.inferred_edge_type

    @property
    def intermediate_object_id(self) -> str:
        return self.via_object_id

    # Short aliases keep call sites readable while the explicit fields document
    # that all three values are object ids.
    @property
    def source_id(self) -> str:
        return self.source_object_id

    @property
    def via_id(self) -> str:
        return self.via_object_id

    @property
    def target_id(self) -> str:
        return self.target_object_id


@dataclass
class FollowChainResult:
    """Repository/service result shape used by the reasoning orchestrator."""

    inferences: list[InferredChain] = field(default_factory=list)
    nodes: list[dict] = field(default_factory=list)
