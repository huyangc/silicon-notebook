from app.services.qiefen.models import EvidenceAtom, SemanticChunk, SourceSpan
from app.services.qiefen.packager import build_packages


def _atom(aid, atom_type="claim_sentence"):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type=atom_type,
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1, line_end=1,
                                               char_start=0, char_end=1),
                        raw_text="t", normalized_text="t")


def test_one_package_per_chunk_with_atom_type_pairs():
    atoms = [_atom("A1"), _atom("A2", "method_sentence")]
    chunk = SemanticChunk(id="C1", profile="article_research",
                          chunk_type="article_core_claim_block",
                          section_path="Abstract", atom_ids=["A1", "A2"])
    pkgs = build_packages([chunk], {a.id: a for a in atoms},
                          document_title="Engram", profile="article_research")
    assert len(pkgs) == 1
    assert pkgs[0].chunk_id == "C1"
    assert pkgs[0].atoms == [{"atom_id": "A1", "atom_type": "claim_sentence"},
                             {"atom_id": "A2", "atom_type": "method_sentence"}]
