"""Stable retrieval values shared by ports and application services."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, NamedTuple, Optional

from app.models.common import Evidence


W_KEYWORD = 0.4
W_SEMANTIC = 0.6


@dataclass
class RetrievedKnowledge:
    object_id: str
    object_type: str
    payload: Dict[str, object]
    evidence: List[Evidence] = field(default_factory=list)
    score: float = 0.0
    relevance: float = 0.0
    weight: float = 0.0
    status: str = "approved"
    owner: str = ""
    last_reviewed: str = ""
    notebook_id: str = ""
    tier: str = "personal"


@dataclass
class RetrievedElement:
    element_id: str
    source_id: str
    source_title: str
    location_label: str
    element_type: str
    text: str
    score: float = 0.0


@dataclass
class RetrievedRelation:
    relation_id: str
    source_object_id: str
    target_object_id: str
    edge_type: str
    text: str = ""
    evidence: List[Evidence] = field(default_factory=list)
    score: float = 0.0
    relevance: float = 0.0
    notebook_id: str = ""
    tier: str = "personal"
    review_status: str = "pending"


class NeighborExpansion(NamedTuple):
    """One bounded one-hop expansion plus its truncation disclosure."""

    hits: List[RetrievedKnowledge]
    truncated: bool = False


@dataclass(frozen=True)
class GapRelationRow:
    """A weakly supported canonical relation used only as a reasoning hint."""

    canonical_src: str
    canonical_tgt: str
    src_name: str
    tgt_name: str
    edge_type: str
    source_count: int


@dataclass(frozen=True)
class RetrievalSupport:
    """One producer's immutable support for a retrieved chunk."""

    origin: Literal[
        "semantic", "lexical", "generated_question", "kg_source", "ppr", "relation"
    ]
    support_kind: Literal["chunk", "object", "relation", "ppr"]
    support_id: str = ""
    score: Optional[float] = None
    review_status_snapshot: str = ""


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_id: str
    source_title: str
    section_path: str
    text: str
    element_ids: List[str] = field(default_factory=list)
    score: float = 0.0
    relevance: float = 0.0
    notebook_id: str = ""
    retrieval_supports: tuple[RetrievalSupport, ...] = field(
        default=(), compare=False
    )

    @property
    def object_id(self) -> str:
        return self.chunk_id


@dataclass(frozen=True)
class ChunkRetrievalPlan:
    """Immutable orchestration decision snapshot for one chunk retrieval."""

    strategy: str
    overlay_on: bool
    mmr_k: int
    mmr_lambda: float
    fuse_k: int

