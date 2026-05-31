from app.services.qiefen.models import (
    SourceSpan, EvidenceAtom, SemanticChunk, ContextPackage,
    SectionNode, QiefenDocument, SourceMeta,
)


def test_atom_roundtrips_and_to_pred_dict_key_order():
    atom = EvidenceAtom(
        id="A1", section_id="SEC", atom_type="claim_sentence",
        source_element_id="SE1",
        source_span=SourceSpan(file="x.md", line_start=11, line_end=11,
                               char_start=526, char_end=730),
        raw_text="While ...", normalized_text="While ...",
    )
    doc = QiefenDocument(
        source_meta=SourceMeta(source_id="engram", profile="article_research",
                               title="t", source_file="x.md",
                               source_line_range=[9, 11]),
        section_tree=[SectionNode(id="SEC", path="Abstract", title="Abstract")],
        evidence_atoms=[atom],
        semantic_chunks=[SemanticChunk(id="C1", profile="article_research",
                                       chunk_type="article_core_claim_block",
                                       section_path="Abstract", atom_ids=["A1"])],
        context_packages=[ContextPackage(id="P1", profile="article_research",
                                         chunk_id="C1", section_path="Abstract",
                                         document_title="t",
                                         atoms=[{"atom_id": "A1",
                                                 "atom_type": "claim_sentence"}])],
    )
    d = doc.to_pred_dict()
    assert list(d.keys())[:5] == [
        "schema_version", "source_meta", "section_tree",
        "evidence_atoms", "semantic_chunks",
    ]
    assert d["evidence_atoms"][0]["source_span"]["char_start"] == 526
    assert d["evidence_atoms"][0]["raw_text"] == "While ..."
