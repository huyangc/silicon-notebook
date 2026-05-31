import yaml
from app.services.qiefen.pipeline import run
from app.services.qiefen.emit import to_yaml


def test_pipeline_abstract_atoms_satisfy_span_invariant(source_text):
    src = source_text("engram_paper_mineru.md")
    doc = run(src, source_file="engram_paper_mineru.md",
              profile="article_research", line_range=[9, 11],
              source_id="engram", title="Engram", scope="Abstract only")
    assert doc.evidence_atoms, "expected atoms from the abstract"
    for a in doc.evidence_atoms:
        s = a.source_span
        assert src[s.char_start:s.char_end] == a.raw_text
    # every atom is in exactly one chunk
    in_chunks = [aid for c in doc.semantic_chunks for aid in c.atom_ids]
    assert sorted(in_chunks) == sorted(a.id for a in doc.evidence_atoms)
    # emit is valid YAML with gold key order
    parsed = yaml.safe_load(to_yaml(doc))
    assert list(parsed.keys())[1] == "source_meta"
    assert parsed["evidence_atoms"][0]["raw_text"]
