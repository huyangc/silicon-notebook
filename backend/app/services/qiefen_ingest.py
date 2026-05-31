"""Bridge the qiefen pipeline into the live ingestion path.

`reconstruct_markdown` turns stored SourceElements back into a markdown string
the qiefen S1 parser can atomize (PDFs are parsed to elements upstream; qiefen
needs text + char offsets, which it computes against this reconstruction).
`qiefen_doc_to_candidates` maps qiefen objects into the existing CandidateRecord
shape (payload + atom-grounded Evidence) so the candidate -> approve ->
knowledge_objects -> browse/Q&A flow works unchanged.
"""
from __future__ import annotations

from typing import List

from app.models.schemas import Evidence, SourceElement
from app.services.extraction import CandidateRecord
from app.services.qiefen.models import QiefenDocument

# Map the product's detected doc-type/profile id to a qiefen profile.
QIEFEN_PROFILE_BY_DOCTYPE = {
    "academic_paper": "article_research",
    "article": "article_research",
    "textbook": "textbook",
}


def reconstruct_markdown(elements: List[SourceElement]) -> str:
    """Rebuild a markdown string from parsed elements for qiefen S1."""
    parts: List[str] = []
    for el in elements:
        text = (el.text or "").strip()
        if not text:
            continue
        etype = el.element_type
        if etype == "heading":
            parts.append(f"# {text}")
        elif etype == "formula":
            parts.append(text if text.startswith("$$") else f"$$ {text} $$")
        elif etype == "table":
            parts.append(el.metadata.get("table_html") or text)
        elif etype in ("image_caption", "figure_caption"):
            parts.append(text)
        elif etype == "list_item":
            parts.append(f"- {text}")
        else:
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def qiefen_doc_to_candidates(doc: QiefenDocument, source_id: str,
                             source_title: str) -> List[CandidateRecord]:
    atoms = {a.id: a for a in doc.evidence_atoms}
    records: List[CandidateRecord] = []
    for obj in doc.objects:
        evidence: List[Evidence] = []
        for aid in obj.local_evidence_atom_ids:
            atom = atoms.get(aid)
            if atom is None:
                continue
            loc = obj.section_path or f"line {atom.source_span.line_start}"
            evidence.append(Evidence(
                source_id=source_id, source_title=source_title,
                element_id=atom.id, element_type=atom.atom_type,
                location_label=loc, quoted_span=(atom.raw_text or "")[:400],
                confidence=1.0,
            ))
        payload = dict(obj.payload)
        if obj.section_path:
            payload.setdefault("section_path", obj.section_path)
        records.append(CandidateRecord(
            candidate_type=obj.type, payload=payload, evidence=evidence,
            status="candidate" if evidence else "needs_review",
            extraction_mode="qiefen", confidence=0.8,
        ))
    return records
