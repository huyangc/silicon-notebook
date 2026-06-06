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
