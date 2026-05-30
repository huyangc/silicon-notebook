"""Document-type extraction profiles (产品方案 §5 对象模型 + §6.2 模板).

Why this exists
---------------
The original extractor hard-coded one fixed set of six object types
(rule/method/risk/case/checklist/glossary) for *every* document. That only
fits design specs and summaries; forcing it onto academic papers or textbooks
produces noise (a paper has *claims/findings*, not "must/should design rules";
a textbook has *concepts/principles*, not debug cases).

A **profile** answers, for a given kind of document: *which* typed knowledge
objects to extract and *which* fields each object carries. Everything still
lands in the one shared, typed ``knowledge_objects`` store, so cross-type
retrieval, governance and the relation graph keep working — only the extraction
*lens* changes per document.

Two axes pick the profile (see ``resolve_profile``):
  1. the notebook template (§6.2) supplies a sensible default, and
  2. a per-source heuristic doc-type detector can override it when a document
     clearly looks like something else (a paper dropped into a rule notebook).

Schema *induction* (proposing brand-new object types/fields from the corpus)
is intentionally NOT here — that is a later, curator-gated step. This registry
is the human-defined, closed schema backbone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.models.schemas import SourceElement

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
    plural: str  # JSON key the LLM returns (e.g. "rules")
    fields: List[str]  # payload keys, in display order (relations included)
    primary: str  # main text field, used for headline / dedupe
    description: str  # one-line guidance injected into the prompt
    # Extra list-valued fields beyond the global LIST_FIELDS (for custom/induced
    # types whose array fields are not in the global set).
    list_fields: List[str] = field(default_factory=list)


# --- Shared typed knowledge model (产品方案 §5) ----------------------------
# Existing six core types, now with the §5 fields that were missing — notably
# the *relation* fields (related_*) plus rule.condition / exception / rule_type.
OBJECT_SCHEMAS: Dict[str, ObjectSchema] = {
    "rule": ObjectSchema(
        "rule",
        "rules",
        [
            "title", "statement", "applies_to", "condition", "recommendation",
            "risk_if_ignored", "exception", "severity", "rule_type",
            "related_cases", "related_methods",
        ],
        primary="title",
        description="a design rule / constraint (must/should/不得/应当)",
    ),
    "method": ObjectSchema(
        "method",
        "methods",
        [
            "name", "use_when", "benefit", "limitation", "tradeoff",
            "required_condition", "related_rules", "related_cases",
        ],
        primary="name",
        description="an analysis/verification method or best practice",
    ),
    "risk": ObjectSchema(
        "risk",
        "risks",
        ["title", "description", "severity", "mitigation", "related_rules"],
        primary="title",
        description="a failure mode / risk and its impact",
    ),
    "case": ObjectSchema(
        "case",
        "cases",
        [
            "symptom", "context", "root_cause", "resolution", "lesson_learned",
            "related_rules", "related_methods",
        ],
        primary="symptom",
        description="a historical debug / failure case",
    ),
    "checklist": ObjectSchema(
        "checklist",
        "checklist",
        [
            "question", "applies_to", "required_evidence", "severity",
            "related_rules", "related_cases",
        ],
        primary="question",
        description="an actionable design-review checklist item",
    ),
    "glossary": ObjectSchema(
        "glossary",
        "glossary",
        ["term", "definition", "aliases"],
        primary="term",
        description="a domain term and its definition",
    ),
    # --- academic / textbook extensions ---------------------------------
    # These map document-native objects that do NOT fit the design-doc model.
    "claim": ObjectSchema(
        "claim",
        "claims",
        [
            "statement", "claim_type", "measurement_condition", "limitation",
            "related_rules",
        ],
        primary="statement",
        description="a technical claim asserted by a paper",
    ),
    "finding": ObjectSchema(
        "finding",
        "findings",
        ["statement", "metric", "condition", "dataset"],
        primary="statement",
        description="a quantitative experimental result / finding",
    ),
    "concept": ObjectSchema(
        "concept",
        "concepts",
        ["term", "definition", "why_it_matters", "related_concepts"],
        primary="term",
        description="a core concept defined in a textbook",
    ),
    "principle": ObjectSchema(
        "principle",
        "principles",
        ["statement", "rationale", "applies_to"],
        primary="statement",
        description="a governing principle / law / theorem",
    ),
    "example": ObjectSchema(
        "example",
        "examples",
        ["title", "problem", "approach", "result"],
        primary="title",
        description="a worked example / illustration",
    ),
}


# Display labels (zh) for each object type — used by the generic knowledge
# browser tabs and type counts.
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "rule": "规则 Rule",
    "method": "方法 Method",
    "risk": "风险 Risk",
    "case": "案例 Case",
    "checklist": "检查项 Checklist",
    "glossary": "术语 Glossary",
    "claim": "论文主张 Claim",
    "finding": "实验结论 Finding",
    "concept": "概念 Concept",
    "principle": "原理 Principle",
    "example": "例题 Example",
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
    "design_spec": ExtractionProfile(
        "design_spec",
        "设计规范 / 方案文档",
        ["rule", "checklist", "risk", "method"],
        "a design specification / guideline — focus on rules and constraints, "
        "review checks, and the risks of ignoring them",
    ),
    "method": ExtractionProfile(
        "method",
        "方法 / SOP 文档",
        ["method", "rule", "checklist"],
        "a methods / best-practice document — focus on when to use each method, "
        "its benefit, tradeoff, and limitations",
    ),
    "postmortem": ExtractionProfile(
        "postmortem",
        "复盘 / 经验总结",
        ["case", "rule", "method", "risk"],
        "a debug postmortem / lessons-learned summary — focus on symptom, root "
        "cause, resolution, and the lesson learned",
    ),
    "review": ExtractionProfile(
        "review",
        "评审 checklist",
        ["checklist", "rule", "risk"],
        "a design-review checklist — focus on actionable check items, the "
        "evidence each requires, and the scope it applies to",
    ),
    "academic_paper": ExtractionProfile(
        "academic_paper",
        "学术论文",
        ["claim", "finding", "method", "glossary"],
        "an academic paper — focus on its technical claims, quantitative "
        "findings, and methods; do NOT fabricate design rules the paper does "
        "not actually state",
    ),
    "textbook": ExtractionProfile(
        "textbook",
        "教材 / 课本",
        ["concept", "principle", "method", "example", "glossary"],
        "a textbook / course material — focus on core concepts, governing "
        "principles, methods, and worked examples",
    ),
    "general": ExtractionProfile(
        "general",
        "通用混合文档",
        ["rule", "method", "case", "checklist", "risk", "glossary"],
        "a mixed general document — extract whatever typed knowledge is clearly "
        "supported by the text",
    ),
}

DEFAULT_PROFILE_ID = "general"

# Notebook template (§6.2) -> default extraction profile.
TEMPLATE_PROFILE: Dict[str, str] = {
    "rule": "design_spec",
    "method": "method",
    "case": "postmortem",
    "review": "review",
    "article": "academic_paper",
    "general": "general",
}


# --- Heuristic per-source document-type detection -------------------------
# Offline-safe: scores a document sample against bilingual cue patterns and
# returns a profile id only when one type clearly wins, so it overrides the
# notebook default only when confident.
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
    (
        "postmortem",
        re.compile(
            r"\b(root cause|post-?mortem|symptom|lessons? learned|debug)\b|"
            r"根因|复盘|踩坑|经验教训|故障分析|问题回顾",
            re.IGNORECASE,
        ),
    ),
    (
        "review",
        re.compile(
            r"\b(checklist|sign-?off|review item|审查清单)\b|检查项|评审清单|签核|审查项",
            re.IGNORECASE,
        ),
    ),
    (
        "design_spec",
        re.compile(
            r"\b(shall|must not|design rule|specification|guideline|design guide)\b|"
            r"设计规范|设计指南|不得|必须遵守",
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


def detect_doc_type(
    title: str, elements: Sequence[SourceElement]
) -> Optional[str]:
    """Return a profile id if the document clearly looks like one type, else None."""
    sample = _document_sample(title, elements)
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
    template's default applies; otherwise the general profile.
    """
    detected = detect_doc_type(title, elements)
    if detected:
        return PROFILES[detected]
    if template_id and template_id in TEMPLATE_PROFILE:
        return PROFILES[TEMPLATE_PROFILE[template_id]]
    return PROFILES[DEFAULT_PROFILE_ID]
