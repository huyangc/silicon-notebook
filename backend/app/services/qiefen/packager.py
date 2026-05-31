"""S5: one ContextPackage per chunk. expected_objects is left empty in P0
(filled by the LLM object stage in P1)."""
from __future__ import annotations

from typing import Dict, List

from app.services.qiefen.models import (
    ContextPackage, EvidenceAtom, SemanticChunk,
)


def build_packages(chunks: List[SemanticChunk], atoms_by_id: Dict[str, EvidenceAtom],
                   document_title: str, profile: str) -> List[ContextPackage]:
    pkgs: List[ContextPackage] = []
    for i, ch in enumerate(chunks, start=1):
        pairs = []
        for aid in ch.atom_ids:
            a = atoms_by_id.get(aid)
            if a is not None:
                pairs.append({"atom_id": aid, "atom_type": a.atom_type})
        pkgs.append(ContextPackage(
            id=f"PKG-{i}", profile=profile, chunk_id=ch.id,
            section_path=ch.section_path, document_title=document_title,
            atoms=pairs, extraction_targets=ch.extraction_targets,
        ))
    return pkgs
