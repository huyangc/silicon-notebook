"""论文元数据抽取:prompt/schema hint + 零 LLM 接地校验(anti-hallucination)。

设计不变量(specs/2026-07-15-paper-metadata-extraction-design.md §5.3):LLM 返回
的每个字段写库前必须「接地」——归一化后能在文档头部文本中找到——防模型对
「认识的」论文用参数记忆补全(张冠李戴作者/机构)。不在文本中的字段不落库,
丢弃明细进 raw_json 审计信封。纯函数,无 DB/网络依赖。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

PAPER_META_SCHEMA_HINT = (
    '{"is_paper":true,"title":"","authors":[{"name":"","affiliations":[""]}],'
    '"venue":"","year":2024,"doi":"","keywords":[""]}'
)

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def paper_meta_prompt(head_text: str) -> str:
    return (
        "Extract bibliographic metadata from the OPENING TEXT of a document.\n"
        "Rules:\n"
        "- Use ONLY the text below. Even if you recognize the paper, do NOT "
        "fill in anything from memory — omit whatever the text does not show.\n"
        "- If this is not an academic paper (web page, manual, slides, notes, "
        '...), return {"is_paper": false} and leave every other field empty.\n'
        "- authors: in byline order, names EXACTLY as written in the text "
        "(original language/spelling). affiliations: that author's "
        "institutions per the superscript/layout mapping; use [] when unsure "
        "— never guess.\n"
        "- venue: journal/conference name only if it appears in the text; "
        "year: publication year only if it appears in the text; doi: only if "
        "a DOI string appears; keywords: only from an explicit keyword list.\n"
        "- Return JSON only.\n\n"
        f"Opening text:\n{head_text}"
    )


def _norm(text: str) -> str:
    """接地匹配归一化:NFKD 去变音符、casefold、只留字母数字(空白/标点不敏感)。"""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


def grounded(value: str, head_norm: str) -> bool:
    needle = _norm(value)
    return bool(needle) and needle in head_norm


def author_grounded(name: str, head_norm: str) -> bool:
    """姓名接地:直接匹配,或 2+ token 时容忍「姓,名」/「名 姓」次序翻转。"""
    if grounded(name, head_norm):
        return True
    tokens = [t for t in re.split(r"[\s,]+", name or "") if t]
    if len(tokens) >= 2:
        return grounded("".join(reversed(tokens)), head_norm)
    return False


def verify_paper_meta(data: Dict[str, Any], head_text: str, model: str) -> Dict[str, Any]:
    """接地校验:返回可直接交给 SourceStore.upsert_paper_meta 的 meta dict。
    不在头部文本中的字段不落库;丢弃明细记入 raw_json 审计信封
    {"llm": 原始返回, "dropped": {...}} 并以 "dropped" 键回传给调用方记事件。"""
    head_text = head_text or ""
    head_norm = _norm(head_text)
    dropped: Dict[str, Any] = {}
    is_paper = bool(data.get("is_paper"))

    title = str(data.get("title") or "").strip() or None
    if title and not grounded(title, head_norm):
        dropped["title"] = title
        title = None

    venue = str(data.get("venue") or "").strip() or None
    if venue and not grounded(venue, head_norm):
        dropped["venue"] = venue
        venue = None

    year: Optional[int] = None
    raw_year = data.get("year")
    if raw_year is not None and str(raw_year).strip():
        try:
            candidate = int(str(raw_year).strip())
        except ValueError:
            candidate = 0
        if 1900 <= candidate <= 2100 and str(candidate) in head_text:
            year = candidate
        else:
            dropped["year"] = raw_year

    doi = str(data.get("doi") or "").strip() or None
    if doi and not (_DOI_RE.match(doi) and doi.lower() in head_text.lower()):
        dropped["doi"] = doi
        doi = None

    keywords: List[str] = []
    dropped_keywords: List[str] = []
    for raw_kw in data.get("keywords") or []:
        keyword = str(raw_kw).strip()
        if not keyword:
            continue
        (keywords if grounded(keyword, head_norm) else dropped_keywords).append(keyword)
    if dropped_keywords:
        dropped["keywords"] = dropped_keywords

    authors: List[Dict[str, Any]] = []
    dropped_authors: List[str] = []
    cleared_affiliations: List[str] = []
    position = 0
    for raw_author in data.get("authors") or []:
        name = str((raw_author or {}).get("name") or "").strip()
        if not name:
            continue
        if not author_grounded(name, head_norm):
            dropped_authors.append(name)
            continue
        affiliations = [
            str(a).strip()
            for a in (raw_author or {}).get("affiliations") or []
            if str(a).strip()
        ]
        kept = [a for a in affiliations if grounded(a, head_norm)]
        cleared_affiliations.extend(a for a in affiliations if a not in kept)
        authors.append(
            {"position": position, "name": name, "affiliation": "; ".join(kept)}
        )
        position += 1
    if dropped_authors:
        dropped["authors"] = dropped_authors
    if cleared_affiliations:
        dropped["affiliations"] = cleared_affiliations

    return {
        "is_paper": is_paper,
        "paper_title": title if is_paper else None,
        "venue": venue if is_paper else None,
        "pub_year": year if is_paper else None,
        "doi": doi if is_paper else None,
        "keywords": keywords if is_paper else [],
        "authors": authors if is_paper else [],
        "model": model,
        "raw_json": json.dumps({"llm": data, "dropped": dropped}, ensure_ascii=False),
        "dropped": dropped,
    }
