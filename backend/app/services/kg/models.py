"""KG data model. 4 node types, typed edges, verbatim evidence spans."""
from __future__ import annotations
from typing import Any, Dict, List
from pydantic import BaseModel, Field

NodeType = str

class Evidence(BaseModel):
    file: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    quote: str

class Step(BaseModel):
    name: str = ""
    evidence: List[Evidence] = Field(default_factory=list)

class Node(BaseModel):
    id: str
    type: NodeType
    name: str = ""              # node text: Concept/Procedure name, Claim statement, Formula expression
    section_path: str = ""
    evidence: List[Evidence] = Field(default_factory=list)
    mentions: List[Evidence] = Field(default_factory=list)
    steps: List[Step] = Field(default_factory=list)   # ordered steps for a flow Procedure
    validity_scope: Dict[str, Any] = Field(default_factory=dict)  # claim/formula only: {region[],assumptions[],approximation,range}

class Edge(BaseModel):
    id: str
    type: str
    source_id: str
    target_id: str
    evidence: List[Evidence] = Field(default_factory=list)

class KnowledgeGraph(BaseModel):
    doc_id: str
    doc_type: str
    nodes: List[Node] = Field(default_factory=list)
    edges: List[Edge] = Field(default_factory=list)
    total_windows: int = 0
    failed_windows: int = 0
    windows_skipped: int = 0
    concepts_dropped: int = 0
    claims_dropped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = self.model_dump(mode="python", exclude_none=True)
        return {k: d[k] for k in ("doc_id", "doc_type", "nodes", "edges")}
