from app.services.qiefen.models import EvidenceAtom, SourceSpan
from app.services.qiefen.do_not_extract import detect_negatives


def _atom(aid, raw):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type="claim_sentence",
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=1, line_end=1,
                                               char_start=0, char_end=len(raw)),
                        raw_text=raw, normalized_text=raw)


def test_detects_url_and_citation():
    atoms = [
        _atom("A1", "Code available at: https://github.com/deepseek-ai/Engram"),
        _atom("A2", "Transformers (Vaswani et al., 2017) lack lookup."),
        _atom("A3", "A plain sentence with no negatives."),
    ]
    dne = detect_negatives(atoms)
    kinds = {e["kind"] for e in dne}
    assert "out_of_slice_reference" in kinds  # url
    assert "citation_policy" in kinds         # author-year
    texts = " ".join(str(e.get("text", "")) + str(e.get("examples", "")) for e in dne)
    assert "github.com" in texts
