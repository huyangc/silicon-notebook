"""查询理解层(chunk 与 reasoning 共用):规整 + LLM 改写/分解。"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.services.cancellation import AskCancelled, CancelEvent

# 在字母↔数字边界插空格,让 "gpt4" 这类连写匹配上语料 "GPT-4"→tokens "gpt","4"。
# 注意:无法把 "deepseekv2" 拆成 "deepseek v2"(中间无边界)——那类靠 expand_query 的
# LLM 改写写出规范名(DeepSeek-V2)。此处只做边界明确的廉价补充(也惠及无 LLM 回退)。
_LD = re.compile(r"(?<=[A-Za-z]{2})(?=\d)|(?<=[A-Za-z])(?=\d{2,})")


def normalize_terms(q: str) -> str:
    return _LD.sub(" ", q or "")


_KG_TYPES = ("concept", "claim", "formula", "procedure")
_PREFER = ("keyword", "semantic", "balanced")


@dataclass
class SubQuerySpec:
    query: str
    types: List[str] = field(default_factory=list)
    prefer: str = "balanced"
    reason: str = ""


@dataclass
class ExpandedQuery:
    query_en: str
    sub_queries: List[SubQuerySpec]
    high_level_keywords: List[str] = field(default_factory=list)
    low_level_keywords: List[str] = field(default_factory=list)


def expand_query(client, question: str, history: str = "", *,
                 timeout: Optional[float] = None, max_retries: Optional[int] = None,
                 max_subqueries: int = 4, want_types: bool = False,
                 cancel_event: CancelEvent = None) -> ExpandedQuery:
    """一次 LLM 调用:问题(任意语言)→ 英文改写 + 1..max_subqueries 个具体英文子查询。
    want_types=True 时每个子查询附 KG types/prefer(供 reasoning)。
    任何失败/未配置/空 → 回退 [normalize_terms(question)] 单子查询。始终 >=1。"""
    from app.services.prompts import expand_query_prompt, EXPAND_SCHEMA_HINT
    fallback = ExpandedQuery(query_en=question,
                             sub_queries=[SubQuerySpec(query=normalize_terms(question))])
    if not getattr(client, "configured", False):
        return fallback
    kw = {}
    if timeout is not None: kw["timeout"] = timeout
    if max_retries is not None: kw["max_retries"] = max_retries
    try:
        raw = client.chat_json(
            [{"role": "user", "content": expand_query_prompt(question, history, want_types)}],
            EXPAND_SCHEMA_HINT, cancel_event=cancel_event, **kw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return fallback
        subs_raw = data.get("sub_queries")
        if not isinstance(subs_raw, list) or not subs_raw:
            return fallback
        out: List[SubQuerySpec] = []
        seen = set()
        for s in subs_raw:
            if not isinstance(s, dict):
                continue
            q = normalize_terms(str(s.get("query", "")).strip())
            if not q or q in seen:
                continue
            seen.add(q)
            types, prefer = [], "balanced"
            if want_types:
                tr = s.get("types")
                types = [t for t in (tr if isinstance(tr, list) else []) if t in _KG_TYPES]
                prefer = s.get("prefer") if s.get("prefer") in _PREFER else "balanced"
            out.append(SubQuerySpec(query=q, types=types, prefer=prefer,
                                    reason=str(s.get("reason", ""))))
            if len(out) >= max_subqueries:
                break
        if not out:
            return fallback
        def _kw_list(v):
            if isinstance(v, str):
                return [x.strip() for x in re.split(r"[,;\n]", v) if x.strip()]
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []
        hl = _kw_list(data.get("high_level_keywords"))
        ll = _kw_list(data.get("low_level_keywords"))
        query_en = str(data.get("query_en", "")).strip() or question
        return ExpandedQuery(query_en=query_en, sub_queries=out,
                             high_level_keywords=hl, low_level_keywords=ll)
    except AskCancelled:
        raise
    except Exception:
        return fallback
