"""Deterministic KG denoising filters (pure, no IO).

- should_extract_window: skip低价值抽取窗口（习题/索引/参考文献/索引式/前言/目录页）。
- is_noise_concept: 判定正文噪声概念（符号/实例号/图号/章节标题/过短），
  白名单命中优先保护。
规则最终取值以现有概念上的离线验证（scripts/validate_concept_filter.py）为准。
"""
from __future__ import annotations

import re
from typing import Sequence, Tuple

from app.services.kg.parsing import SourceElementQ

# --- normalization (must match concept_whitelist 存储/查找口径) ---
_WS_RE = re.compile(r"[\s\-_]+")


def _norm(name: str) -> str:
    return _WS_RE.sub(" ", (name or "").strip().lower())


# --- window filter ---
_PROBLEM_RE = re.compile(r"(^|[>\s])problems?$|(^|[>\s])exercises?$|习题|练习", re.IGNORECASE)
_BACKMATTER_SEGMENTS = {"index", "glossary", "references", "bibliography"}
_BACKMATTER_CJK = ("索引", "参考文献", "术语表")
# 精确段名匹配(不做前缀/子串), 防 'Preface to the Noise Model' 这类正文标题误伤
_FRONTMATTER_SEGMENTS = {
    "preface", "foreword", "acknowledgments", "acknowledgements",
    "contents", "table of contents", "brief contents",
    "about the author", "about the authors",
    "to the instructor", "to the student", "suggestions for instructors",
}
_FRONTMATTER_CJK = ("前言", "目录", "致谢", "序言")
_INDEX_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /\-(),]+,\s*\d+([,–\-\s\d]+)?$")
_TOC_LINE_RE = re.compile(r"^\d+(\.\d+)*\s+\S.{0,90}?\s+\d{1,4}$")


def _index_like_ratio(elements: Sequence[SourceElementQ]) -> float:
    texts = [(e.text or "").strip() for e in elements if (e.text or "").strip()]
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if _INDEX_LINE_RE.match(t))
    return hits / len(texts)


def _toc_like_ratio(elements: Sequence[SourceElementQ]) -> float:
    texts = [(e.text or "").strip() for e in elements if (e.text or "").strip()]
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if _TOC_LINE_RE.match(t))
    return hits / len(texts)


def _is_backmatter(section_path: str) -> bool:
    """True if a breadcrumb SEGMENT is exactly a backmatter section name
    (index/glossary/references/bibliography) — avoids substring false positives
    like 'Indexed Addressing' / 'Index Register Theory'. CJK terms match as substring."""
    for seg in (section_path or "").split(">"):
        if seg.strip().lower() in _BACKMATTER_SEGMENTS:
            return True
    return any(t in (section_path or "") for t in _BACKMATTER_CJK)


def _is_frontmatter(section_path: str) -> bool:
    """Exact-segment match like _is_backmatter (avoids 'Preface to the Noise
    Model' false positives). CJK terms match as substring."""
    for seg in (section_path or "").split(">"):
        if seg.strip().lower() in _FRONTMATTER_SEGMENTS:
            return True
    return any(t in (section_path or "") for t in _FRONTMATTER_CJK)


def should_extract_window(section_path: str, elements: Sequence[SourceElementQ],
                          doc_type: str) -> Tuple[bool, str]:
    path = section_path or ""
    if (doc_type or "").lower() == "textbook" and _PROBLEM_RE.search(path):
        return False, "textbook_problem_section"
    if _is_backmatter(path):
        return False, "backmatter_section"
    if _is_frontmatter(path):
        return False, "frontmatter_section"
    if _index_like_ratio(elements) >= 0.6:
        return False, "index_like_window"
    if _toc_like_ratio(elements) >= 0.6:
        return False, "toc_like_window"
    return True, ""


# --- concept noise filter ---
_REF_RE = re.compile(r"^(fig|figure|table|eq|equation|sec|section|§)\b", re.IGNORECASE)
_SECTION_RE = re.compile(r"^\d+(\.\d+)+\s+[A-Z]")  # dotted-number + space + Title-Case heading; keeps '0.8 μm ...', '802.11g'
_INSTANCE_RE = re.compile(r"^[A-Za-z]\d+$")


def is_noise_concept(name: str, whitelist) -> Tuple[bool, str]:
    raw = (name or "").strip()
    if _norm(raw) in whitelist:          # 白名单保护优先
        return False, ""
    if len(raw) <= 2:
        return True, "too_short"
    if raw.isdigit():
        return True, "pure_number"
    if _REF_RE.match(raw):
        return True, "reference"
    if _SECTION_RE.match(raw):
        return True, "section_heading"
    if (" " not in raw) and ("_" in raw or "^" in raw):
        return True, "symbol"  # bare single-token symbol; multi-word names with a symbol are kept
    if _INSTANCE_RE.match(raw):
        return True, "instance_label"
    return False, ""
