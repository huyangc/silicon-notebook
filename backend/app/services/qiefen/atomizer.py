"""S3: SourceElement -> EvidenceAtom with exact spans + atom_type.

Spans are computed by locating each sentence verbatim inside its element's
text and offsetting by the element's char_start, so source[span]==raw_text by
construction. atom_type is assigned by deterministic, profile-specific cues.
"""
from __future__ import annotations

import re
from typing import List

from app.services.qiefen.models import EvidenceAtom, SourceElementQ, SourceSpan

# Sentence boundary: terminator + following space. Keep the terminator with the
# left sentence. (Sub-sentence splitting for "while ... we observe ..." is a
# later refinement driven by the harness atom report.)
_SENT = re.compile(r"(?<=[.!?。！？])\s+")

_ARTICLE_CUES = [
    ("scaling_law_result_atom", re.compile(r"\bU-shaped|scaling law\b", re.I)),
    ("method_sentence", re.compile(r"\bwe (introduce|propose|instantiate|present)\b", re.I)),
    ("mechanism_sentence", re.compile(r"\bMechanistic|relieves|frees up|delegating\b", re.I)),
    ("result_sentence", re.compile(r"\bwe observe|achieving|\+\d|improv|outperform\b", re.I)),
    ("risk_sentence", re.compile(r"\bcollision|polysemy|risk|degrad\b", re.I)),
]
_TEXTBOOK_CUES = [
    ("definition_atom", re.compile(r"\bis defined as|refers to|means\b", re.I)),
    ("process_step_atom", re.compile(r"\b(step|then|next|first|finally)\b", re.I)),
    ("example_problem_atom", re.compile(r"\bExample\s+\d", re.I)),
    ("given_atom", re.compile(r"\bgiven\b", re.I)),
    ("formula_usage_atom", re.compile(r"\b(find|calculate|using Eq)\b", re.I)),
]


def _sentence_type(sentence: str, profile: str) -> str:
    cues = _ARTICLE_CUES if profile == "article_research" else _TEXTBOOK_CUES
    for atom_type, pat in cues:
        if pat.search(sentence):
            return atom_type
    return "claim_sentence" if profile == "article_research" else "concept_definition_atom"


def _normalize(text: str) -> str:
    out = (text.replace("→", "->").replace("≤", "<=")
           .replace("≥", ">=").replace("×", "x"))
    return re.sub(r"\$([^$]*)\$", r"\1", out)


def atomize(source_text: str, elements: List[SourceElementQ], section_id: str,
            profile: str) -> List[EvidenceAtom]:
    atoms: List[EvidenceAtom] = []
    n = 0

    def add(el: SourceElementQ, local_start: int, raw: str, atom_type: str) -> None:
        nonlocal n
        if not raw.strip():
            return
        cstart = el.char_start + local_start
        cend = cstart + len(raw)
        assert source_text[cstart:cend] == raw, "span/raw mismatch"
        n += 1
        atoms.append(EvidenceAtom(
            id=f"A-{section_id}-{n}", section_id=section_id, atom_type=atom_type,
            source_element_id=el.id,
            source_span=SourceSpan(file=el.file, line_start=el.line_start,
                                   line_end=el.line_end, char_start=cstart,
                                   char_end=cend),
            raw_text=raw, normalized_text=_normalize(raw),
        ))

    for el in elements:
        if el.type == "formula":
            add(el, 0, el.text, "formula_atom")
        elif el.type == "figure_caption":
            add(el, 0, el.text, "figure_caption_atom")
        elif el.type == "table":
            add(el, 0, el.text, "table_caption_atom")
        elif el.type == "heading":
            continue
        else:  # paragraph / list_item -> sentences
            pos = 0
            for piece in _SENT.split(el.text):
                if not piece:
                    continue
                local = el.text.index(piece, pos)
                pos = local + len(piece)
                add(el, local, piece, _sentence_type(piece, profile))
    return atoms
