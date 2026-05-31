from app.models.schemas import SourceElement
from app.services.qiefen.models import (
    EvidenceAtom, KnowledgeObjectQ, QiefenDocument, SourceMeta, SourceSpan,
)
from app.services.qiefen_ingest import reconstruct_markdown, qiefen_doc_to_candidates


def _el(etype, text, meta=None):
    return SourceElement(id="", source_id="s", element_type=etype,
                         location_label="", text=text, metadata=meta or {})


def test_reconstruct_markdown_marks_structures():
    md = reconstruct_markdown([
        _el("heading", "2. Architecture"),
        _el("paragraph", "Engram is a module."),
        _el("formula", "C_j = C_j0"),
        _el("table", "x", {"table_html": "<table><tr><td>a</td></tr></table>"}),
    ])
    assert "# 2. Architecture" in md
    assert "$$ C_j = C_j0 $$" in md
    assert "<table><tr><td>a</td></tr></table>" in md


def _atom(aid, raw):
    return EvidenceAtom(id=aid, section_id="SEC", atom_type="claim_sentence",
                        source_element_id="SE",
                        source_span=SourceSpan(file="x.md", line_start=11, line_end=11,
                                               char_start=0, char_end=len(raw)),
                        raw_text=raw, normalized_text=raw)


def test_objects_to_candidates_with_atom_evidence():
    doc = QiefenDocument(
        source_meta=SourceMeta(source_id="engram", profile="article_research",
                               title="t", source_file="x.md"),
        evidence_atoms=[_atom("A1", "Conditional memory complements MoE.")],
        objects=[KnowledgeObjectQ(
            id="OBJ-1", type="ArticleClaim", section_path="Abstract",
            payload={"statement": "conditional memory complements MoE"},
            local_evidence_atom_ids=["A1", "A_MISSING"])],
    )
    cands = qiefen_doc_to_candidates(doc, "src-1", "Engram Paper")
    assert len(cands) == 1
    c = cands[0]
    assert c.candidate_type == "ArticleClaim"
    assert c.payload["statement"] == "conditional memory complements MoE"
    assert c.payload["section_path"] == "Abstract"
    assert c.extraction_mode == "qiefen"
    assert len(c.evidence) == 1  # A_MISSING dropped
    ev = c.evidence[0]
    assert ev.element_id == "A1"
    assert ev.quoted_span == "Conditional memory complements MoE."
    assert ev.source_id == "src-1"
