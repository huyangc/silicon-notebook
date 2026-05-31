"""S4: atoms -> SemanticChunk. One chunk per contiguous run of same-section
atoms, typed by a profile default keyed on the dominant atom_type.

Chunks are kept at section granularity (not finer): fewer, section-level
packages mean far fewer LLM calls downstream, and the chunk-set vs gold aligns
better at this granularity. The LLM object stage bounds its INPUT by capping
high-value atoms per prompt (see objects/packaging), not by splitting chunks.
"""
from __future__ import annotations

from typing import Dict, List

from app.services.qiefen.models import EvidenceAtom, SemanticChunk

_ARTICLE_CHUNK_BY_ATOM = {
    "scaling_law_result_atom": "scaling_law_block",
    "result_sentence": "experiment_result_block",
    "method_sentence": "architecture_component_block",
    "mechanism_sentence": "article_core_claim_block",
    "formula_atom": "formula_definition_block",
    "table_header_atom": "experiment_result_block",
}
_TEXTBOOK_CHUNK_BY_ATOM = {
    "formula_atom": "formula_definition_block",
    "table_header_atom": "hierarchy_table_block",
    "table_caption_atom": "hierarchy_table_block",
    "definition_atom": "concept_definition_block",
    "concept_definition_atom": "concept_definition_block",
    "process_step_atom": "process_flow_block",
    "example_problem_atom": "example_solution_block",
    "problem_statement_atom": "problem_set_block",
    "design_principle_atom": "design_principle_block",
}


def _chunk_type(profile: str, atom_types: List[str]) -> str:
    table = _ARTICLE_CHUNK_BY_ATOM if profile == "article_research" else _TEXTBOOK_CHUNK_BY_ATOM
    for at in atom_types:
        if at in table:
            return table[at]
    return "article_core_claim_block" if profile == "article_research" else "chapter_overview_block"


def build_chunks(atoms: List[EvidenceAtom], profile: str,
                 section_paths: Dict[str, str]) -> List[SemanticChunk]:
    chunks: List[SemanticChunk] = []
    run: List[EvidenceAtom] = []
    n = 0

    def flush() -> None:
        nonlocal n, run
        if not run:
            return
        n += 1
        sid = run[0].section_id
        chunks.append(SemanticChunk(
            id=f"C-{sid}-{n}", profile=profile,
            chunk_type=_chunk_type(profile, [a.atom_type for a in run]),
            section_path=section_paths.get(sid, ""),
            atom_ids=[a.id for a in run],
            central_atom_ids=[run[0].id],
        ))
        run = []

    for a in atoms:
        if run and a.section_id != run[0].section_id:
            flush()
        run.append(a)
    flush()
    return chunks
