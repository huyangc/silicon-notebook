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
    query: str
    sub_queries: List[SubQuerySpec]
    high_level_keywords: List[str] = field(default_factory=list)
    low_level_keywords: List[str] = field(default_factory=list)
    comparison: Optional[dict] = None   # {"focal": 实体名} 若 LLM 判定为对比题,否则 None


def expand_query(client, question: str, history: str = "", *,
                 timeout: Optional[float] = None, max_retries: Optional[int] = None,
                 max_subqueries: int = 4, want_types: bool = False,
                 corpus_langs: Optional[List[str]] = None,
                 cancel_event: CancelEvent = None,
                 fail_closed: bool = False,
                 system_instruction: str = "") -> ExpandedQuery:
    """一次 LLM 调用:问题(任意语言)→ 同语言规范改写 + 1..max_subqueries 个具体子查询
    (保持问题本身的语言)。keywords 按 corpus_langs 双语化(供纯词法 FTS/KG 名匹配),
    sub_queries 单语言(多语向量 embedder 一次即跨语,无需二次嵌入)。
    want_types=True 时每个子查询附 KG types/prefer(供 reasoning)。
    任何失败/未配置/空 → 回退 [normalize_terms(question)] 单子查询。始终 >=1。"""
    from app.services.prompts import expand_query_prompt, EXPAND_SCHEMA_HINT
    fallback = ExpandedQuery(query=question,
                             sub_queries=[SubQuerySpec(query=normalize_terms(question))])
    if not getattr(client, "configured", False):
        if fail_closed:
            raise RuntimeError("query rewrite model is not configured")
        return fallback
    kw = {}
    if timeout is not None: kw["timeout"] = timeout
    if max_retries is not None: kw["max_retries"] = max_retries
    try:
        messages = [{
            "role": "user",
            "content": expand_query_prompt(
                question,
                history,
                want_types,
                max_subqueries=max_subqueries,
                corpus_langs=corpus_langs,
            ),
        }]
        if system_instruction:
            messages.insert(0, {"role": "system", "content": system_instruction})
        raw = client.chat_json(
            messages,
            EXPAND_SCHEMA_HINT, cancel_event=cancel_event, **kw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            if fail_closed:
                raise ValueError("query rewrite model returned a non-object")
            return fallback
        subs_raw = data.get("sub_queries")
        if not isinstance(subs_raw, list) or not subs_raw:
            if fail_closed:
                raise ValueError("query rewrite model returned no sub-queries")
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
            if fail_closed:
                raise ValueError("query rewrite model returned no valid sub-queries")
            return fallback
        def _kw_list(v):
            if isinstance(v, str):
                return [x.strip() for x in re.split(r"[,;\n]", v) if x.strip()]
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []
        hl = _kw_list(data.get("high_level_keywords"))
        ll = _kw_list(data.get("low_level_keywords"))
        query = str(data.get("query", "")).strip() or question
        comp = data.get("comparison")
        comparison = None
        if isinstance(comp, dict) and str(comp.get("focal", "")).strip():
            comparison = {"focal": str(comp["focal"]).strip()}
        return ExpandedQuery(query=query, sub_queries=out,
                             high_level_keywords=hl, low_level_keywords=ll,
                             comparison=comparison)
    except AskCancelled:
        raise
    except Exception:
        if fail_closed:
            raise
        return fallback
