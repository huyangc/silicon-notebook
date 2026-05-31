from app.services.qiefen.models import EvidenceAtom, SourceSpan
from app.services.qiefen.chunker import build_chunks


def _atom(aid, section_id, atom_type="claim_sentence"):
    return EvidenceAtom(id=aid, section_id=section_id, atom_type=atom_type,
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1,
                                               line_end=1, char_start=0, char_end=1),
                        raw_text="t", normalized_text="t")


def test_atoms_grouped_by_section_each_in_one_chunk():
    atoms = [_atom("A1", "SEC1"), _atom("A2", "SEC1"), _atom("A3", "SEC2")]
    chunks = build_chunks(atoms, profile="article_research",
                          section_paths={"SEC1": "Abstract", "SEC2": "1. Intro"})
    assert len(chunks) == 2
    assert set(chunks[0].atom_ids) == {"A1", "A2"}
    assert all(c.chunk_type for c in chunks)  # every chunk is typed
    # every atom appears in exactly one chunk
    seen = [a for c in chunks for a in c.atom_ids]
    assert sorted(seen) == ["A1", "A2", "A3"]


def test_chunk_typed_from_dominant_atom():
    atoms = [_atom("A1", "SEC1", "concept_definition_atom"),
             _atom("A2", "SEC1", "formula_atom")]
    chunks = build_chunks(atoms, "textbook", {"SEC1": "2 > 2.2"})
    assert len(chunks) == 1
    # formula present -> formula_definition_block (mapped before the prose default)
    assert chunks[0].chunk_type == "concept_definition_block"
