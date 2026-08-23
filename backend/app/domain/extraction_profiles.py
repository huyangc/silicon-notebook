"""Document-type extraction profiles for the KG pipeline.

Only two document types are supported: ``academic_paper`` and ``textbook``.
Both use the same four KG object types: concept, claim, formula, procedure.

Sunk to app.domain in B3 (zero app.services/app.repositories dependency, only
app.models.sources). ``app.services.extraction_profiles`` re-exports every
name here unchanged for existing importers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.models.sources import SourceElement

# Payload fields that carry a list value (the LLM should return an array, and
# the prompt schema hint renders them as ``[""]``). Everything else is a string.
LIST_FIELDS = {
    "applies_to",
    "related_rules",
    "related_cases",
    "related_methods",
    "related_concepts",
    "aliases",
}

# Fields constrained to a small vocabulary, rendered as a hint in the schema.
ENUM_FIELDS = {
    "severity": "high|medium|low",
    "rule_type": "mandatory|recommended|advisory|project_specific",
    "claim_type": "mechanism|result|recommendation|warning|comparison",
}


@dataclass(frozen=True)
class ObjectSchema:
    """A single typed knowledge object the extractor can emit."""

    type: str  # object_type stored in knowledge_objects
    plural: str  # JSON key the LLM returns (e.g. "concepts")
    fields: List[str]  # payload keys, in display order
    primary: str  # main text field, used for headline / dedupe
    description: str  # one-line guidance injected into the prompt
    # Extra list-valued fields beyond the global LIST_FIELDS (for custom/induced
    # types whose array fields are not in the global set).
    list_fields: List[str] = field(default_factory=list)


# --- KG object type registry -----------------------------------------------
OBJECT_SCHEMAS: Dict[str, ObjectSchema] = {
    "concept": ObjectSchema(
        type="concept",
        plural="concepts",
        fields=["name", "section_path"],
        primary="name",
        description="a named entity (term/method/component/device/material)",
        list_fields=[],
    ),
    "claim": ObjectSchema(
        type="claim",
        plural="claims",
        fields=["name", "section_path"],
        primary="name",
        description="a truth-evaluable assertion about concepts",
        list_fields=[],
    ),
    "formula": ObjectSchema(
        type="formula",
        plural="formulas",
        fields=["name", "section_path"],
        primary="name",
        description="an equation / expression",
        list_fields=[],
    ),
    "procedure": ObjectSchema(
        type="procedure",
        plural="procedures",
        fields=["name", "section_path"],
        primary="name",
        description="an ordered process / derivation / worked example",
        list_fields=[],
    ),
}


# Display labels (zh) for each object type.
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "concept": "概念 Concept",
    "claim": "论断 Claim",
    "formula": "公式 Formula",
    "procedure": "过程 Procedure",
}


@dataclass(frozen=True)
class ExtractionProfile:
    """A document-type lens: which object types to extract + framing."""

    id: str
    label: str
    object_type_keys: List[str]  # keys into OBJECT_SCHEMAS, priority order
    focus: str  # short doc-type framing injected into the prompt

    @property
    def object_types(self) -> List[str]:
        return list(self.object_type_keys)

    def schemas(
        self, registry: Optional[Dict[str, "ObjectSchema"]] = None
    ) -> List["ObjectSchema"]:
        reg = registry if registry is not None else OBJECT_SCHEMAS
        return [reg[key] for key in self.object_type_keys if key in reg]


PROFILES: Dict[str, ExtractionProfile] = {
    "academic_paper": ExtractionProfile(
        "academic_paper",
        "学术论文",
        ["concept", "claim", "formula", "procedure"],
        "an academic paper",
    ),
    "textbook": ExtractionProfile(
        "textbook",
        "教材 / 课本",
        ["concept", "claim", "formula", "procedure"],
        "a textbook / course material",
    ),
}

DEFAULT_PROFILE_ID = "academic_paper"

# Notebook template (§6.2) -> default extraction profile.
TEMPLATE_PROFILE: Dict[str, str] = {
    "article": "academic_paper",
    "textbook": "textbook",
}


# --- Heuristic per-source document-type detection -------------------------
_DETECTORS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "academic_paper",
        re.compile(
            r"\b(abstract|references|et al\.?|doi|arxiv|we propose|"
            r"experimental results|related work)\b|参考文献|摘要|实验结果|本文提出|相关工作",
            re.IGNORECASE,
        ),
    ),
    (
        "textbook",
        re.compile(
            r"\b(chapter|theorem|lemma|exercise|proof|definition)\b|"
            r"第[一二三四五六七八九十\d]+章|定理|引理|习题|本节|本章",
            re.IGNORECASE,
        ),
    ),
)

# Need at least this many cue hits, and a clear lead over the runner-up, before
# detection overrides the notebook default.
_DETECT_MIN_HITS = 2
_DETECT_MIN_LEAD = 2


def _document_sample(
    title: str, elements: Sequence[SourceElement], max_chars: int = 6000
) -> str:
    parts: List[str] = [title or ""]
    size = len(parts[0])
    for element in elements:
        text = (element.text or "").strip()
        if not text:
            continue
        parts.append(text)
        size += len(text)
        if size >= max_chars:
            break
    return "\n".join(parts)


def detect_doc_type_from_sample(sample: str) -> Optional[str]:
    """Classify a raw text sample (title + leading content already joined).

    Returns a profile id only when one type clearly dominates (>= _DETECT_MIN_HITS
    cue hits AND a >= _DETECT_MIN_LEAD lead over the runner-up); else None so the
    caller can fall back to a default / 'auto'. Shared by content-based detection
    (detect_doc_type) and the upload-time POST /detect-doc-types endpoint."""
    if not sample.strip():
        return None
    scores: List[tuple[str, int]] = []
    for profile_id, pattern in _DETECTORS:
        hits = len(pattern.findall(sample))
        if hits:
            scores.append((profile_id, hits))
    if not scores:
        return None
    scores.sort(key=lambda item: item[1], reverse=True)
    best_id, best_hits = scores[0]
    runner_up = scores[1][1] if len(scores) > 1 else 0
    if best_hits >= _DETECT_MIN_HITS and best_hits - runner_up >= _DETECT_MIN_LEAD:
        return best_id
    return None


def detect_doc_type(
    title: str, elements: Sequence[SourceElement]
) -> Optional[str]:
    """Return a profile id if the document clearly looks like one type, else None."""
    return detect_doc_type_from_sample(_document_sample(title, elements))


def get_profile(profile_id: Optional[str]) -> ExtractionProfile:
    return PROFILES.get(profile_id or "", PROFILES[DEFAULT_PROFILE_ID])


def profile_for_template(template_id: Optional[str]) -> ExtractionProfile:
    return get_profile(TEMPLATE_PROFILE.get(template_id or ""))


def resolve_profile(
    template_id: Optional[str],
    title: str,
    elements: Sequence[SourceElement],
) -> ExtractionProfile:
    """Pick the extraction profile for one source.

    Detection (per-source content) wins when confident; otherwise the notebook
    template's default applies; otherwise the default profile (academic_paper).
    """
    detected = detect_doc_type(title, elements)
    if detected:
        return PROFILES[detected]
    if template_id and template_id in TEMPLATE_PROFILE:
        return PROFILES[TEMPLATE_PROFILE[template_id]]
    return PROFILES[DEFAULT_PROFILE_ID]
