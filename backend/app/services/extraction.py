"""Candidate knowledge extraction from parsed source elements.

Produces rule / method / risk / case / checklist / glossary candidates, each
bound to element-level evidence. Uses the configured OpenAI-compatible LLM when
available, otherwise falls back to deterministic heuristics so the local beta
can still run the extraction -> review -> retrieval loop offline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.core.llm import OpenAICompatibleClient
from app.models.schemas import Evidence, SourceElement
from app.services.prompts import EXTRACTION_SCHEMA_HINT, extraction_prompt
from app.services.retrieval import token_overlap

# Windowed extraction so large documents are covered in full, not just the
# first ~9000 chars. Each window is sent to the LLM; candidates are merged and
# de-duplicated afterwards. Set MAX_WINDOWS > 0 only when a deployment needs an
# explicit cost cap; the local beta defaults to full coverage.
WINDOW_MAX_CHARS = 9000
WINDOW_OVERLAP_CHARS = 500
MAX_WINDOWS = 0
# Minimum CJK-aware token overlap for the fuzzy evidence-binding fallback.
BIND_OVERLAP_THRESHOLD = 0.6

_RULE_PATTERNS = re.compile(
    r"\b(must|shall|should|require[sd]?|do not|don'?t|never|always)\b|必须|应当|应该|不应|不得|禁止|需要",
    re.IGNORECASE,
)
_RISK_PATTERNS = re.compile(
    r"\b(risk|fail(s|ure)?|degrade[sd]?|damage|stress|hazard)\b|风险|失效|故障|损坏|退化|隐患",
    re.IGNORECASE,
)
_CASE_PATTERNS = re.compile(
    r"\b(measured|observed|issue|problem|root cause|symptom|debug)\b|测得|观察到|问题|故障|根因|复现|调试",
    re.IGNORECASE,
)
_METHOD_PATTERNS = re.compile(
    r"\b(method|approach|technique|best practice|procedure)\b|方法|流程|步骤|最佳实践|做法",
    re.IGNORECASE,
)
_CHECK_PATTERNS = re.compile(
    r"\b(check|verify|confirm|ensure|review)\b|检查|确认|核对|验证|审查",
    re.IGNORECASE,
)

# Splits a directive sentence into recommendation vs. risk-if-ignored.
_RISK_CLAUSE = re.compile(r"(否则|以免|以防|or risk|to avoid|otherwise)", re.IGNORECASE)

# Definition phrasing -> glossary term/definition (conservative; avoids the old
# "every heading becomes an empty-definition glossary" noise).
_DEF_PATTERNS = [
    re.compile(r"^(?P<term>.{2,40}?)\s*[:：]\s*(?P<def>.{8,})$"),
    re.compile(
        r"^(?P<term>.{2,40}?)\s*(?:is defined as|refers to|means|是指|指的是|定义为)\s*(?P<def>.{4,})$",
        re.IGNORECASE,
    ),
]

# Light bilingual scope vocabulary for rule applies_to detection.
_SCOPE_TERMS = [
    "analog", "afe", "wirebond", "bondwire", "package", "esd", "pll", "adc",
    "dac", "ldo", "bandgap", "layout", "die", "wafer", "cmos", "bcd", "rf",
    "封装", "模拟", "低噪声", "版图", "前端", "电源", "时钟", "射频",
]

# Element types that are poor sources for directive/case heuristics (avoid noise).
# Headings are titles/labels, not statements, so they only feed glossary (via a
# definition phrase), never rule/case/checklist heuristics.
_NOISE_TYPES = {"heading", "table", "formula", "image_caption", "speaker_notes"}


@dataclass
class CandidateRecord:
    candidate_type: str
    payload: Dict[str, object]
    evidence: List[Evidence] = field(default_factory=list)
    status: str = "candidate"
    extraction_mode: str = "heuristic"
    confidence: float = 0.5


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _make_evidence(
    element: SourceElement,
    source_title: str,
    quoted_span: str,
    confidence: float,
) -> Evidence:
    return Evidence(
        source_id=element.source_id,
        source_title=source_title,
        element_id=element.id,
        element_type=element.element_type,
        location_label=element.location_label,
        quoted_span=quoted_span.strip()[:400],
        confidence=confidence,
    )


def bind_evidence(
    quoted_span: str,
    elements: List[SourceElement],
    source_title: str,
    confidence: float,
) -> Optional[Evidence]:
    needle = _normalize(quoted_span)
    if len(needle) < 6:
        return None
    # 1) Exact / containment substring on normalized text.
    for element in elements:
        haystack = _normalize(element.text)
        if needle in haystack or haystack in needle:
            return _make_evidence(element, source_title, quoted_span, confidence)
    # 2) CJK-aware token-overlap fallback: pick the best-overlapping element.
    #    Handles LLM spans that paraphrase or differ in punctuation/whitespace,
    #    which would otherwise fail to bind and lose semantic recall.
    best_element: Optional[SourceElement] = None
    best_ratio = 0.0
    for element in elements:
        ratio = token_overlap(quoted_span, element.text)
        if ratio > best_ratio:
            best_ratio = ratio
            best_element = element
    if best_element is not None and best_ratio >= BIND_OVERLAP_THRESHOLD:
        return _make_evidence(
            best_element, source_title, quoted_span, max(0.0, confidence - 0.1)
        )
    return None


def _records_from_llm_section(
    candidate_type: str,
    items: List[dict],
    payload_keys: List[str],
    elements: List[SourceElement],
    source_title: str,
) -> List[CandidateRecord]:
    records: List[CandidateRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        payload = {key: item.get(key, "") for key in payload_keys}
        if not any(str(value).strip() for value in payload.values()):
            continue
        quoted_span = str(item.get("quoted_span", "")).strip()
        evidence = bind_evidence(quoted_span, elements, source_title, 0.7)
        status = "candidate" if evidence else "needs_review"
        records.append(
            CandidateRecord(
                candidate_type=candidate_type,
                payload=payload,
                evidence=[evidence] if evidence else [],
                status=status,
                extraction_mode="llm",
            )
        )
    return records


_LLM_SECTIONS = [
    ("rule", "rules", ["title", "statement", "applies_to", "recommendation", "risk_if_ignored", "severity"]),
    ("method", "methods", ["name", "use_when", "benefit", "limitation"]),
    ("risk", "risks", ["title", "description", "severity"]),
    ("case", "cases", ["symptom", "context", "root_cause", "resolution", "lesson_learned"]),
    ("checklist", "checklist", ["question", "severity", "required_evidence"]),
    ("glossary", "glossary", ["term", "definition"]),
]


def _element_windows(elements: List[SourceElement]) -> List[List[SourceElement]]:
    """Split elements into overlapping ~WINDOW_MAX_CHARS windows so the whole
    document is covered (capped at MAX_WINDOWS to bound cost). Always makes
    forward progress."""
    windows: List[List[SourceElement]] = []
    i = 0
    n = len(elements)
    while i < n and (MAX_WINDOWS <= 0 or len(windows) < MAX_WINDOWS):
        chunk: List[SourceElement] = []
        size = 0
        j = i
        while j < n:
            piece = f"[{elements[j].location_label}] {elements[j].text}"
            if chunk and size + len(piece) > WINDOW_MAX_CHARS:
                break
            chunk.append(elements[j])
            size += len(piece) + 1
            j += 1
        windows.append(chunk)
        if j >= n:
            break
        # Step back to overlap with the next window, but never stall.
        back = 0
        k = j
        while k > i + 1 and back < WINDOW_OVERLAP_CHARS:
            back += len(elements[k - 1].text)
            k -= 1
        i = k if k > i else i + 1
    return windows


def _parse_llm_records(
    data: object,
    elements: List[SourceElement],
    source_title: str,
) -> List[CandidateRecord]:
    if not isinstance(data, dict):
        raise ValueError("extraction did not return a JSON object")
    records: List[CandidateRecord] = []
    for candidate_type, key, payload_keys in _LLM_SECTIONS:
        items = data.get(key) or []
        if isinstance(items, list):
            records.extend(
                _records_from_llm_section(
                    candidate_type, items, payload_keys, elements, source_title
                )
            )
    return records


def _dedupe_records(records: List[CandidateRecord]) -> List[CandidateRecord]:
    """Drop duplicate candidates that recur across overlapping windows, keyed by
    (type, normalized primary text)."""
    seen = set()
    deduped: List[CandidateRecord] = []
    for record in records:
        payload = record.payload
        primary = (
            payload.get("title")
            or payload.get("name")
            or payload.get("term")
            or payload.get("question")
            or payload.get("symptom")
            or payload.get("statement")
            or ""
        )
        key = (record.candidate_type, _normalize(str(primary)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _extract_with_llm(
    client: OpenAICompatibleClient,
    elements: List[SourceElement],
    source_title: str,
) -> List[CandidateRecord]:
    records: List[CandidateRecord] = []
    for window in _element_windows(elements):
        elements_block = "\n".join(
            f"[{element.location_label}] {element.text}" for element in window
        )[:WINDOW_MAX_CHARS]
        raw = client.chat_json(
            [{"role": "user", "content": extraction_prompt(source_title, elements_block)}],
            EXTRACTION_SCHEMA_HINT,
        )
        # Bind evidence against the full element list (not just the window) for
        # the best chance of a successful binding.
        records.extend(_parse_llm_records(json.loads(raw), elements, source_title))
    return _dedupe_records(records)


def _first_sentence(text: str, limit: int = 200) -> str:
    sentences = _sentences(text)
    sentence = sentences[0] if sentences else text.strip()
    return sentence[:limit]


def _sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[.!?。！？；;])\s+|\n+", text.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 12]


def _detect_scope(text: str) -> List[str]:
    low = text.lower()
    found: List[str] = []
    for term in _SCOPE_TERMS:
        if term.lower() in low and term not in found:
            found.append(term)
    return found[:5]


def _split_recommendation_risk(sentence: str) -> tuple[str, str]:
    match = _RISK_CLAUSE.search(sentence)
    if not match:
        return sentence.strip(), ""
    left = sentence[: match.start()].strip(" ,，;；")
    right = sentence[match.end():].strip(" ,，;；")
    return left or sentence.strip(), right


def _glossary_from_text(text: str) -> Optional[tuple[str, str]]:
    candidate = text.strip()
    if len(candidate) > 200:
        return None
    for pattern in _DEF_PATTERNS:
        match = pattern.match(candidate)
        if match:
            term = match.group("term").strip()
            definition = match.group("def").strip()
            if term and definition and "|" not in term and not term.isdigit():
                return term[:120], definition[:400]
    return None


def _classify_sentence(sentence: str, element_type: str) -> Optional[str]:
    """Best single candidate type for a sentence (priority order)."""
    if _RULE_PATTERNS.search(sentence):
        return "rule"
    if _CASE_PATTERNS.search(sentence):
        return "case"
    if element_type == "list_item" or _CHECK_PATTERNS.search(sentence):
        return "checklist"
    if _RISK_PATTERNS.search(sentence):
        return "risk"
    if _METHOD_PATTERNS.search(sentence):
        return "method"
    return None


def _heuristic_payload(candidate_type: str, sentence: str) -> tuple[Dict[str, object], float]:
    if candidate_type == "rule":
        recommendation, risk = _split_recommendation_risk(sentence)
        return (
            {
                "title": sentence[:120],
                "statement": sentence[:500],
                "applies_to": _detect_scope(sentence),
                "recommendation": recommendation[:300] if risk else "",
                "risk_if_ignored": risk[:300],
                "severity": "medium",
            },
            0.7 if risk else 0.55,
        )
    if candidate_type == "case":
        return (
            {
                "symptom": sentence[:200],
                "context": "",
                "root_cause": "",
                "resolution": "",
                "lesson_learned": sentence[:300],
            },
            0.5,
        )
    if candidate_type == "checklist":
        return ({"question": sentence[:200], "severity": "medium", "required_evidence": ""}, 0.45)
    if candidate_type == "risk":
        return ({"title": sentence[:120], "description": sentence[:400], "severity": "medium"}, 0.5)
    if candidate_type == "method":
        return ({"name": sentence[:120], "use_when": "", "benefit": sentence[:300], "limitation": ""}, 0.5)
    return ({}, 0.4)


def _extract_with_heuristics(
    elements: List[SourceElement],
    source_title: str,
) -> List[CandidateRecord]:
    """Sentence-level offline extraction.

    One element can yield several candidates (one per qualifying sentence).
    Glossary terms come only from real definition phrasing, and noise-prone
    element types (tables/formulas/captions/notes) are skipped for directive and
    case heuristics.
    """
    records: List[CandidateRecord] = []
    for element in elements:
        text = element.text.strip()
        if len(text) < 12:
            continue

        glossary = _glossary_from_text(text)
        if glossary is not None:
            records.append(
                CandidateRecord(
                    "glossary",
                    {"term": glossary[0], "definition": glossary[1]},
                    [_make_evidence(element, source_title, text[:300], 0.6)],
                    status="candidate",
                    extraction_mode="heuristic",
                    confidence=0.6,
                )
            )

        if element.element_type in _NOISE_TYPES:
            continue

        for sentence in _sentences(text):
            candidate_type = _classify_sentence(sentence, element.element_type)
            if candidate_type is None:
                continue
            payload, confidence = _heuristic_payload(candidate_type, sentence)
            records.append(
                CandidateRecord(
                    candidate_type,
                    payload,
                    [_make_evidence(element, source_title, sentence[:300], 0.4)],
                    status="candidate" if confidence >= 0.6 else "needs_review",
                    extraction_mode="heuristic",
                    confidence=confidence,
                )
            )
    return records


def run_extraction(
    client: OpenAICompatibleClient,
    elements: List[SourceElement],
    source_title: str,
) -> List[CandidateRecord]:
    elements = [element for element in elements if element.text.strip()]
    if not elements:
        return []
    records: List[CandidateRecord] = []
    if client.configured:
        try:
            records = _extract_with_llm(client, elements, source_title)
        except Exception:
            records = []
    if not records:
        records = _extract_with_heuristics(elements, source_title)
    records = _dedupe_records(records)
    records.sort(key=lambda record: record.confidence, reverse=True)
    return records
