"""S4: atoms -> SemanticChunk. P0: one chunk per contiguous run of same-section
atoms, typed by a profile default keyed on the dominant atom_type."""
from __future__ import annotations

from typing import Dict, List

from app.services.qiefen.models import EvidenceAtom, SemanticChunk

_ARTICLE_CHUNK_BY_ATOM = {
    "scaling_law_result_atom": "scaling_law_block",
    "result_sentence": "experiment_result_block",
    "method_sentence": "architecture_component_block",
    "mechanism_sentence": "article_core_claim_block",
}
_TEXTBOOK_CHUNK_BY_ATOM = {
    "formula_atom": "formula_definition_block",
    "definition_atom": "concept_definition_block",
    "process_step_atom": "process_flow_block",
    "example_problem_atom": "example_solution_block",
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
        nonlocal n
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

    cur_section = None
    for a in atoms:
        if cur_section is not None and a.section_id != cur_section:
            flush()
            run = []
        cur_section = a.section_id
        run.append(a)
    flush()
    return chunks
