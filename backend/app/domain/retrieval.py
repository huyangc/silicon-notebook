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
    # Fused relevance before type weight / scenario boost (0..1).
    relevance: float = 0.0
    # Type authority stays separate from relevance so it cannot pollute
    # within-type ranking; consumers may use it for grouping or tie-breaking.
    weight: float = 0.0
    status: str = "approved"
    owner: str = ""
    last_reviewed: str = ""
    # Federation tags default to the active personal notebook. Federated paths
    # fill these when a hit originates in another participating notebook.
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
    """One bounded one-hop expansion plus its truncation disclosure.

    The truncation bit travels with the hits because reasoning must distinguish
    "more neighbors existed but the budget stopped" from "these were all the
    neighbors".  Keeping it as out-of-band state would invite silent loss at
    exactly the user/model disclosure boundary.
    """

    hits: List[RetrievedKnowledge]
    truncated: bool = False


@dataclass(frozen=True)
class GapRelationRow:
    """A weakly supported canonical relation used only as a reasoning hint.

    This is not a retrieval result, never enters the evidence pool, and cannot
    be cited as ``[k]``.  It deliberately has no score, relevance, or evidence
    field so callers do not accidentally mix it into candidate ranking.

    Canonical ids support run-level deduplication while names are the only
    model-facing values. ``source_count`` means distinct supporting documents,
    not the number of raw relation rows represented by
    ``canonical_relations.support_count``; those counts can differ materially
    after alias normalization or claim clustering.
    """

    canonical_src: str
    canonical_tgt: str
    src_name: str
    tgt_name: str
    edge_type: str
    source_count: int


@dataclass(frozen=True)
class RetrievalSupport:
    """One producer's immutable support for a retrieved chunk.

    ``score`` belongs to that producer and never replaces the chunk's fused
    relevance. ``support_id`` is a real object/relation/chunk id when known;
    PPR intentionally leaves it empty instead of inventing a relation.
    """

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
    # Origin notebook, set only by cross-tier paths. Empty means the notebook
    # of the current retrieval run; ordinary single-notebook paths leave it
    # unset and callers fall back to that run's notebook id.
    notebook_id: str = ""
    retrieval_supports: tuple[RetrievalSupport, ...] = field(
        default=(), compare=False
    )

    @property
    def object_id(self) -> str:
        # Evidence classification and AnswerAnchor both use the chunk id as the
        # object identity; keeping them aligned is what makes anchored relevance
        # measurable.
        return self.chunk_id


@dataclass(frozen=True)
class ChunkRetrievalPlan:
    """Immutable orchestration decision snapshot for one chunk retrieval.

    ``_build_chunk_retrieval_plan`` reads settings, KG presence, and reranker
    configuration once so ``ask_chunk`` consumes one decision snapshot without
    changing its control-flow shape.

    This type intentionally contains orchestration decisions only: strategy,
    overlay, and MMR/fusion knobs. It deliberately excludes candidate-layer
    global flags such as ``chunk_ann_enabled``,
    ``chunk_bruteforce_max_chunks``, ``scale_search_include_delta``, and
    ``graph_ppr_enabled``. Those values are not per-query decisions and the
    candidate function has non-Ask callers; threading them through this plan
    would create two long-lived read paths in a cascade with a history of
    production stalls. Shared RRF/canonical-fold/relation/top-N flags also stay
    out because they govern reasoning and graph paths beyond ``ask_chunk``.
    """

    strategy: str  # "mix" | "multi" | "single"
    overlay_on: bool  # overlay flag + reranker configured + local/base KG exists
    mmr_k: int  # single-branch MMR size
    mmr_lambda: float  # single-branch MMR diversity knob
    fuse_k: int  # multi-branch fusion size; intentionally shares the MMR knob
