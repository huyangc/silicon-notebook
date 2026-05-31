"""S3: SourceElement -> EvidenceAtom with exact spans + atom_type.

Spans are computed by locating each sentence verbatim inside its element's
text and offsetting by the element's char_start, so source[span]==raw_text by
construction. atom_type is assigned by deterministic, profile-specific cues.

Selectivity: gold keeps a curated high-value subset of sentences (definitions,
principles, results, formulas, tables, examples), not every sentence. For the
textbook profile (where a chapter's line range is huge and mostly narrative)
we drop sentences that carry no value cue; structural atoms (formula/table/
figure) are always kept. The article profile keeps all sentences (gold curates
those far less aggressively).
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.services.qiefen.models import EvidenceAtom, SourceElementQ, SourceSpan

# Sentence boundary: terminator + following space. Keep the terminator with the
# left sentence.
_SENT = re.compile(r"(?<=[.!?。！？])\s+")

_ARTICLE_CUES = [
    ("scaling_law_result_atom", re.compile(r"\bU-shaped|scaling law\b", re.I)),
    ("method_sentence", re.compile(
        r"\bwe (introduce|propose|instantiate|present|extract|compute|design|use|define)\b"
        r"|\bas (shown|detailed|illustrated) in\b|\bFormally\b|\bgiven an input\b", re.I)),
    ("mechanism_sentence", re.compile(
        r"\bMechanistic|relieves|frees up|delegating|because\b", re.I)),
    ("result_sentence", re.compile(
        r"\bwe observe|achieving|\+\d|improv|outperform|gain|boost\b", re.I)),
    ("risk_sentence", re.compile(r"\bcollision|polysemy|risk|degrad\b", re.I)),
    ("limitation_sentence", re.compile(r"\blimitation|however|cannot|fails to\b", re.I)),
    ("experiment_setup_atom", re.compile(
        r"\bwe (train|pretrain|evaluate)|baseline|iso-(parameter|FLOPs)|tokens\b", re.I)),
]
_TEXTBOOK_CUES = [
    ("design_principle_atom", re.compile(
        r"\b(must|should|shall|recommend|important|necessary|require|ensure|avoid|"
        r"never|always|desirable|preferred)\b", re.I)),
    ("definition_atom", re.compile(r"\bis defined as|defined to be|refers to|denotes\b", re.I)),
    ("example_problem_atom", re.compile(r"\bExample\s+\d", re.I)),
    # Numbered list item (a problem in the PROBLEMS set). NOT keyed on verbs like
    # "design"/"find" — those are ubiquitous in a design textbook and over-match.
    ("problem_statement_atom", re.compile(r"^\s*\d+[.)]\s", re.I)),
    ("process_step_atom", re.compile(
        r"\b(step|first|second|then|next|finally|process|fabricat|deposit|etch|grown|"
        r"implant|oxidat|diffus)\b", re.I)),
    ("given_atom", re.compile(r"\bgiven\b|\bassume\b", re.I)),
    ("result_atom", re.compile(r"\btherefore|thus|hence|results? in|we (get|obtain|have)\b", re.I)),
    ("formula_usage_atom", re.compile(r"\busing Eq|from Eq|substitut\b", re.I)),
    ("concept_definition_atom", re.compile(
        r"\b(is|are)\s+(a|an|the|defined|called|considered|known|given|equal)\b"
        r"|\bconsists of|\bis the\b", re.I)),
]

# Textbook keep predicate: a paragraph sentence survives only if it carries a
# value cue (matched by any cue pattern above) or a quantitative signal.
_TEXTBOOK_QUANT = re.compile(
    r"=|\b\d+(\.\d+)?\s*(V|mV|mA|uA|nA|nm|um|mm|Ohm|kOhm|F|pF|fF|Hz|kHz|MHz|GHz|dB|%|W|K)\b",
    re.I)


def _sentence_type(sentence: str, profile: str) -> str:
    cues = _ARTICLE_CUES if profile == "article_research" else _TEXTBOOK_CUES
    for atom_type, pat in cues:
        if pat.search(sentence):
            return atom_type
    return "claim_sentence" if profile == "article_research" else "concept_definition_atom"


def _keep_sentence(sentence: str, profile: str) -> bool:
    if profile == "article_research":
        return True  # gold curates article sentences lightly; keep all
    if len(sentence.strip()) < 25:
        return False
    for _atom_type, pat in _TEXTBOOK_CUES:
        if pat.search(sentence):
            return True
    return bool(_TEXTBOOK_QUANT.search(sentence))


def _normalize(text: str) -> str:
    out = (text.replace("→", "->").replace("≤", "<=")
           .replace("≥", ">=").replace("×", "x"))
    return re.sub(r"\$([^$]*)\$", r"\1", out)


_TR = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
_TABLE_CAPTION = re.compile(r"^\s*Table\s+[\w.\-]+", re.IGNORECASE)


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

    def add_sentences(el: SourceElementQ) -> None:
        pos = 0
        for piece in _SENT.split(el.text):
            if not piece:
                continue
            local = el.text.index(piece, pos)
            pos = local + len(piece)
            if _keep_sentence(piece, profile):
                add(el, local, piece, _sentence_type(piece, profile))

    for el in elements:
        if el.type == "formula":
            add(el, 0, el.text, "formula_atom")
        elif el.type == "figure_caption":
            # Skip bare image embeds (![](...)); only real "Figure N ..." caption
            # text is a gold-worthy atom.
            if not el.text.lstrip().startswith("!["):
                add(el, 0, el.text, "figure_caption_atom")
        elif el.type == "table":
            _add_table_atoms(el, add)
        elif el.type == "heading":
            continue
        elif el.type == "paragraph" and _TABLE_CAPTION.match(el.text):
            add(el, 0, el.text, "table_caption_atom")
        else:  # paragraph / list_item -> sentences
            add_sentences(el)
    return atoms


def _add_table_atoms(el: SourceElementQ, add) -> None:
    """A MinerU <table>...</table> sits on one line. Split each <tr>...</tr>
    into a verbatim atom: the first row is the header, the rest are rows."""
    rows = list(_TR.finditer(el.text))
    if not rows:
        # MinerU <details> collapsed table or non-row markup: no clean row spans
        # to bind, so emit nothing rather than a noisy whole-block caption atom.
        return
    for i, m in enumerate(rows):
        atom_type = "table_header_atom" if i == 0 else "table_row_atom"
        add(el, m.start(), m.group(0), atom_type)
