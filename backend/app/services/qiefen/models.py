"""Pydantic types mirroring the gold.yaml schema (v0.3.3). to_pred_dict()
emits keys in the gold top-level order so emit.py is a trivial dump."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceSpan(BaseModel):
    file: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int


class SourceElementQ(BaseModel):
    id: str
    type: str  # heading | paragraph | formula | table | figure_caption | list_item
    file: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    text: str  # verbatim slice of source_file[char_start:char_end]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SectionNode(BaseModel):
    id: str
    path: str
    title: str
    parent: Optional[str] = None
    kind: Optional[str] = None


class EvidenceAtom(BaseModel):
    id: str
    section_id: str
    atom_type: str
    source_element_id: str
    source_span: SourceSpan
    raw_text: str
    normalized_text: str
    evidence_strength: str = "direct"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SemanticChunk(BaseModel):
    id: str
    profile: str
    chunk_type: str
    section_path: str
    atom_ids: List[str] = Field(default_factory=list)
    central_atom_ids: List[str] = Field(default_factory=list)
    boundary_reason: str = ""
    extraction_targets: List[str] = Field(default_factory=list)
    gold_must_cover_atoms: List[str] = Field(default_factory=list)


class ContextPackage(BaseModel):
    id: str
    profile: str
    chunk_id: str
    section_path: str
    document_title: str
    atoms: List[Dict[str, str]] = Field(default_factory=list)  # {atom_id, atom_type}
    linked_context: Dict[str, Any] = Field(default_factory=dict)
    extraction_targets: List[str] = Field(default_factory=list)
    expected_objects: List[str] = Field(default_factory=list)


class Mention(BaseModel):
    id: str
    text: str
    type: str
    atom_id: str
    canonical_key: str = ""


class KnowledgeObjectQ(BaseModel):
    id: str
    type: str
    section_path: str = ""
    home_package: str = ""
    payload: Dict[str, Any] = Field(default_factory=dict)
    local_evidence_atom_ids: List[str] = Field(default_factory=list)
    supporting_context_atom_ids: List[str] = Field(default_factory=list)


class RelationQ(BaseModel):
    id: str
    relation_type: str
    source_object_id: str
    target_object_id: str
    evidence_atom_ids: List[str] = Field(default_factory=list)


class SourceMeta(BaseModel):
    source_id: str
    profile: str
    title: str
    source_file: str
    source_line_range: List[int] = Field(default_factory=list)
    scope: str = ""
    extraction_targets: List[str] = Field(default_factory=list)


# Gold top-level key order (README/_AGENT_SPEC.md).
_ORDER = (
    "schema_version", "source_meta", "section_tree", "evidence_atoms",
    "semantic_chunks", "context_packages", "mentions", "canonicalization",
    "objects", "relations", "do_not_extract",
)


class QiefenDocument(BaseModel):
    schema_version: str = "0.3.3"
    source_meta: SourceMeta
    section_tree: List[SectionNode] = Field(default_factory=list)
    evidence_atoms: List[EvidenceAtom] = Field(default_factory=list)
    semantic_chunks: List[SemanticChunk] = Field(default_factory=list)
    context_packages: List[ContextPackage] = Field(default_factory=list)
    mentions: List[Mention] = Field(default_factory=list)
    canonicalization: List[Dict[str, Any]] = Field(default_factory=list)
    objects: List[KnowledgeObjectQ] = Field(default_factory=list)
    relations: List[RelationQ] = Field(default_factory=list)
    do_not_extract: List[Dict[str, Any]] = Field(default_factory=list)

    def to_pred_dict(self) -> Dict[str, Any]:
        dumped = self.model_dump(mode="python", exclude_none=True)
        return {k: dumped[k] for k in _ORDER if k in dumped}
