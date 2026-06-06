"""推理模式 (mode=reasoning) 的 agentic KG 检索。

结构化骨架 Plan→Retrieve→Reflect→Answer + Reflect 阶段自由图遍历深挖。
手搓 JSON-action 循环(无原生 tool calling),复用 SQLiteRepository 的检索原语。
ReasoningRetriever 持 repo 引用,运行时注入,避免与 sqlite_repository 循环导入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models.schemas import TraceStep
from app.services.prompts import (
    PLAN_SCHEMA_HINT, REFLECT_SCHEMA_HINT, plan_prompt, reflect_prompt,
)
from app.services.retrieval import (
    RetrievedElement, RetrievedKnowledge, W_KEYWORD, W_SEMANTIC,
)

KG_TYPES = ("claim", "formula", "procedure", "concept")
PREFER_WEIGHTS = {
    "keyword": (0.7, 0.3),
    "semantic": (0.2, 0.8),
    "balanced": (W_KEYWORD, W_SEMANTIC),
}
_PER_QUERY_LIMIT = 8


@dataclass
class SubQuery:
    query: str
    types: List[str] = field(default_factory=list)   # 空 = 全部 4 类
    prefer: str = "balanced"
    reason: str = ""


@dataclass
class ReflectDecision:
    sufficient: bool = False
    next_action: str = "answer"   # answer|expand_graph|add_subquery|search_elements
    expand_object_id: str = ""
    expand_edge_type: Optional[str] = None
    expand_direction: str = "both"
    new_sub_query: Optional[SubQuery] = None
    elements_query: str = ""
    reason: str = ""


@dataclass
class ReasoningResult:
    top_hits: List[RetrievedKnowledge] = field(default_factory=list)
    elements: List[RetrievedElement] = field(default_factory=list)
    trace: List[TraceStep] = field(default_factory=list)


class ReasoningRetriever:
    def __init__(self, repo, settings):
        self.repo = repo
        self.settings = settings

    # --- KG 工具箱(薄封装 repo 原语) ---
    def search(self, notebook_id, query, types=None, prefer="balanced"):
        wk, ws = PREFER_WEIGHTS.get(prefer, PREFER_WEIGHTS["balanced"])
        return self.repo._retrieve_scored(notebook_id, query, types=types,
                                          w_keyword=wk, w_semantic=ws)

    def neighbors(self, notebook_id, object_id, edge_type=None, direction="both"):
        return self.repo._retrieve_neighbors(notebook_id, object_id, edge_type, direction)

    def get(self, notebook_id, object_id):
        try:
            return self.repo.node_context(notebook_id, object_id)
        except KeyError:
            return {}

    def search_elements(self, notebook_id, query):
        return self.repo._retrieve_elements(notebook_id, query)

    # --- LLM 决策点 ---
    def plan(self, question, history=""):
        fallback = [SubQuery(query=question)]
        if not getattr(self.repo.llm_client, "configured", False):
            return fallback
        try:
            raw = self.repo.llm_client.chat_json(
                [{"role": "user", "content": plan_prompt(question, history)}],
                PLAN_SCHEMA_HINT)
            data = json.loads(raw)
            subs = data.get("sub_queries") if isinstance(data, dict) else None
            if not isinstance(subs, list) or not subs:
                return fallback
            out: List[SubQuery] = []
            for s in subs[: self.settings.reasoning_max_subqueries]:
                if not isinstance(s, dict):
                    continue
                q = str(s.get("query", "")).strip()
                if not q:
                    continue
                _types_raw = s.get("types")
                types = [t for t in (_types_raw if isinstance(_types_raw, list) else []) if t in KG_TYPES]
                prefer = s.get("prefer") if s.get("prefer") in PREFER_WEIGHTS else "balanced"
                out.append(SubQuery(query=q, types=types, prefer=prefer,
                                    reason=str(s.get("reason", ""))))
            return out or fallback
        except Exception:
            return fallback

    def reflect(self, question, candidates_summary):
        answer_decision = ReflectDecision(sufficient=True, next_action="answer")
        if not getattr(self.repo.llm_client, "configured", False):
            return answer_decision
        try:
            raw = self.repo.llm_client.chat_json(
                [{"role": "user", "content": reflect_prompt(question, candidates_summary)}],
                REFLECT_SCHEMA_HINT)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return answer_decision
            action = str(data.get("next_action", "answer"))
            if action not in ("answer", "expand_graph", "add_subquery", "search_elements"):
                action = "answer"
            d = ReflectDecision(
                sufficient=bool(data.get("sufficient", False)),
                next_action=action, reason=str(data.get("reason", "")))
            exp = data.get("expand")
            if isinstance(exp, dict):
                d.expand_object_id = str(exp.get("object_id", ""))
                et = exp.get("edge_type")
                d.expand_edge_type = str(et) if et else None
                dr = exp.get("direction")
                d.expand_direction = dr if dr in ("out", "in", "both") else "both"
            nsq = data.get("new_sub_query")
            if isinstance(nsq, dict) and str(nsq.get("query", "")).strip():
                _nsq_types = nsq.get("types")
                types = [t for t in (_nsq_types if isinstance(_nsq_types, list) else []) if t in KG_TYPES]
                prefer = nsq.get("prefer") if nsq.get("prefer") in PREFER_WEIGHTS else "balanced"
                d.new_sub_query = SubQuery(query=str(nsq["query"]).strip(),
                                           types=types, prefer=prefer,
                                           reason=str(nsq.get("reason", "")))
            d.elements_query = str(data.get("elements_query", "")).strip()
            return d
        except Exception:
            return answer_decision
