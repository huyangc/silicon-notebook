"""推理模式 (mode=reasoning) 的 agentic KG 检索。

结构化骨架 Plan→Retrieve→Reflect→Answer + Reflect 阶段自由图遍历深挖。
手搓 JSON-action 循环(无原生 tool calling),复用 SQLiteRepository 的检索原语。
ReasoningRetriever 持 repo 引用,运行时注入,避免与 sqlite_repository 循环导入。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
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

# Reflect 循环中,当上一步检索动作未带来任何新证据时,附加到候选摘要里的提示。
# 目的:让模型"知道"重复检索已无收益,从而自主决定直接作答(而非被强制收尾),
# 仍不替模型拍板 —— 是否 answer 由模型在 reflect 中自行决定。
NO_NEW_EVIDENCE_NOTE = (
    "（系统提示:上一步检索未带来新证据。若现有候选不足以支撑作答,"
    "且继续同类检索难有新增,请直接选择 next_action=answer,并在答案中"
    "如实说明依据不足、据现有信息推理;不要为凑证据而重复无效检索。)"
)


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
        if not getattr(self.repo.reasoning_llm_client, "configured", False):
            return fallback
        try:
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": plan_prompt(question, history)}],
                PLAN_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries)
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
        if not getattr(self.repo.reasoning_llm_client, "configured", False):
            return answer_decision
        try:
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": reflect_prompt(question, candidates_summary)}],
                REFLECT_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries)
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

    # --- 编排 ---
    def _quota_rerank(self, notebook_id, collected, used_queries, top_n):
        """复合问题: 按子查询配额 round-robin 选 top_n, 避免整串全局排序让信息量大的
        一方通吃。每个候选归到它 relevance 最高的子查询组, 各组内降序后跨组轮流取队首;
        所有子查询都查不到的候选(relevance 全 0)归兜底组, 最后轮转。
        返回 (top_hits, counts): counts[i]=第 i 个子查询贡献数, counts[-1]=兜底组。"""
        # 1. 每个子查询全库重打分(容错: 抛错则该组空)。
        per_q = []
        for q in used_queries:
            try:
                per_q.append({h.object_id: h for h in self.search(notebook_id, q)})
            except Exception:
                per_q.append({})
        # 2. 每个候选归到 relevance 最高的子查询组; 都查不到 → 兜底组。
        groups = [[] for _ in used_queries]
        fallback = []
        for oid, rk in collected.items():
            best_i, best_h = -1, None
            for i, scored in enumerate(per_q):
                h = scored.get(oid)
                if h is not None and (best_h is None or h.relevance > best_h.relevance):
                    best_i, best_h = i, h
            if best_i >= 0:
                groups[best_i].append(best_h)
            else:
                fallback.append(rk)
        # 3. 组内按 relevance 降序。
        for g in groups:
            g.sort(key=lambda h: h.relevance, reverse=True)
        # 4. round-robin 跨组轮流取队首未选过的; 兜底组放最后。
        queues = groups + [fallback]
        idx = [0] * len(queues)
        result, seen, sources = [], set(), []
        while len(result) < top_n:
            progressed = False
            for qi in range(len(queues)):
                if len(result) >= top_n:
                    break
                while idx[qi] < len(queues[qi]):
                    h = queues[qi][idx[qi]]
                    idx[qi] += 1
                    if h.object_id not in seen:
                        seen.add(h.object_id)
                        result.append(h)
                        sources.append(qi)
                        progressed = True
                        break
            if not progressed:
                break
        counts = [sources.count(i) for i in range(len(queues))]
        return result, counts

    def _summarize(self, collected, elements):
        lines = []
        for rk in list(collected.values())[:30]:
            name = str(rk.payload.get("name", "")).strip() or rk.object_id
            lines.append(f"- [{rk.object_type}] {name} (id={rk.object_id})")
        for el in elements[:10]:
            lines.append(f"- [element] {el.source_title} · {el.location_label}: {el.text[:80]}")
        return "\n".join(lines) if lines else "(no candidates yet)"

    def run(self, notebook_id, question, history="", on_step=None):
        trace: List[TraceStep] = []
        collected: Dict[str, RetrievedKnowledge] = {}
        elements: List[RetrievedElement] = []
        visited: set = set()

        def record(step: TraceStep) -> None:
            trace.append(step)
            if on_step:
                on_step(step)

        subqueries = self.plan(question, history)
        record(TraceStep(
            step_type="plan", summary=f"规划了 {len(subqueries)} 个子查询",
            detail={"sub_queries": [{"query": s.query, "types": s.types,
                                     "prefer": s.prefer, "reason": s.reason}
                                    for s in subqueries]}))

        # 初检索:N 个子查询并发执行 search(只读检索,线程安全),按 subqueries
        # 原顺序收集结果再依次 setdefault —— 故去重/确定性与串行版完全等价
        # (每个 object_id 保留按"子查询顺序 + 查询内顺序"的第一个版本)。
        # 单个子查询失败被吞掉(记空结果),不拖垮整个 run。
        def _run_search(sq: SubQuery) -> List[RetrievedKnowledge]:
            try:
                return self.search(notebook_id, sq.query, sq.types, sq.prefer)[:_PER_QUERY_LIMIT]
            except Exception:
                return []

        if subqueries:
            with ThreadPoolExecutor(max_workers=min(len(subqueries), 8)) as ex:
                # map 保序:第 i 个结果对应第 i 个子查询,与提交顺序一致。
                for hits in ex.map(_run_search, subqueries):
                    for h in hits:
                        collected.setdefault(h.object_id, h)
        record(TraceStep(step_type="retrieve",
                         summary=f"初检索得到 {len(collected)} 个候选节点",
                         detail={"count": len(collected)}))

        # 复合问题最终配额排序用: 记录所有用过的子查询(保序去重)。
        used_queries = list(dict.fromkeys(s.query for s in subqueries))

        steps = 0
        # 是否"上一步检索未带来新证据":喂回 reflect,让模型自主判断要不要直接作答。
        # 初检索 0 命中也视为无进展(提前提示模型 KG 可能为空)。
        no_progress = len(collected) == 0
        # 确定性熔断: 连续无有效进展轮数; search_elements 累计执行次数。
        # 软提示(NO_NEW_EVIDENCE_NOTE)交模型自觉, stale 是硬熔断——模型若无视软提示
        # 反复请求同一已访问节点 / 反复 search_elements, 这里强制收尾, 不空转到上限。
        stale = 1 if no_progress else 0
        elements_searches = 0
        while steps < self.settings.reasoning_max_steps:
            steps += 1
            summary = self._summarize(collected, elements)
            if no_progress:
                summary = f"{summary}\n\n{NO_NEW_EVIDENCE_NOTE}"
            # 已展开过的节点回喂 reflect, 提示模型勿重复请求(治"反复 expand 同节点"根源)。
            if visited:
                vis = ", ".join(
                    f"{str(collected[o].payload.get('name', o)) if o in collected else o}"
                    for o in visited)
                summary = f"{summary}\n\n（已展开过的节点，勿重复 expand_graph 请求它们: {vis}）"
            decision = self.reflect(question, summary)
            record(TraceStep(step_type="reflect",
                             summary=decision.reason or decision.next_action,
                             detail={"next_action": decision.next_action,
                                     "sufficient": decision.sufficient,
                                     "no_progress": no_progress, "stale": stale}))
            if decision.next_action == "answer" or decision.sufficient:
                break
            before = len(collected) + len(elements)
            if decision.next_action == "expand_graph":
                oid = decision.expand_object_id
                if not oid or oid in visited:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 expand_graph(空或已访问节点)",
                                     detail={"object_id": oid, "reason": "empty_or_visited"}))
                else:
                    visited.add(oid)
                    neigh = self.neighbors(notebook_id, oid,
                                           decision.expand_edge_type, decision.expand_direction)
                    for h in neigh:
                        collected.setdefault(h.object_id, h)
                    # 展示用人读节点名(优先 collected 命中, 再查 node_context, 兜底裸 id),
                    # 避免 trace 里出现 "顺关系深挖 ko-8375b40126" 这种用户看不懂的内部 id。
                    node_name = ""
                    if oid in collected:
                        node_name = str(collected[oid].payload.get("name", "")).strip()
                    if not node_name:
                        ctx = self.get(notebook_id, oid)
                        node_name = str(ctx.get("name", "")).strip() if ctx else ""
                    node_name = node_name or oid
                    record(TraceStep(step_type="expand",
                                     summary=f"顺关系深挖「{node_name}」,得到 {len(neigh)} 个邻居",
                                     detail={"object_id": oid, "name": node_name,
                                             "edge_type": decision.expand_edge_type,
                                             "found": len(neigh)}))
            elif decision.next_action == "add_subquery":
                if not decision.new_sub_query:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 add_subquery(缺少 new_sub_query)",
                                     detail={"reason": "missing_new_sub_query"}))
                else:
                    sq = decision.new_sub_query
                    for h in self.search(notebook_id, sq.query, sq.types, sq.prefer)[:_PER_QUERY_LIMIT]:
                        collected.setdefault(h.object_id, h)
                    if sq.query not in used_queries:
                        used_queries.append(sq.query)
                    record(TraceStep(step_type="retrieve",
                                     summary=f"补充子查询: {sq.query}",
                                     detail={"query": sq.query}))
            elif decision.next_action == "search_elements":
                if elements_searches >= self.settings.reasoning_max_element_searches:
                    record(TraceStep(step_type="skip",
                                     summary=f"跳过 search_elements(已达次数上限 "
                                             f"{self.settings.reasoning_max_element_searches})",
                                     detail={"reason": "element_search_cap"}))
                else:
                    elements_searches += 1
                    eq = decision.elements_query or question
                    seen_el = {e.element_id for e in elements}
                    els = [e for e in self.search_elements(notebook_id, eq)
                           if e.element_id not in seen_el]
                    elements.extend(els)
                    record(TraceStep(step_type="fallback",
                                     summary=f"降级查原文: {eq},新增 {len(els)} 段",
                                     detail={"query": eq, "found": len(els)}))
            else:
                break
            # 本轮动作后是否有新增(候选节点或原文段)。无新增 → 下一轮提示模型 + 累加 stale。
            no_progress = (len(collected) + len(elements)) == before
            stale = stale + 1 if no_progress else 0
            # 连续 stale_limit 轮无有效进展 → 硬熔断, 强制走到末尾 answer(不再交模型自觉)。
            if stale >= self.settings.reasoning_stale_limit:
                record(TraceStep(step_type="skip",
                                 summary=f"连续 {stale} 轮无新进展,熔断收尾(避免空转)",
                                 detail={"reason": "stale_circuit_breaker", "stale": stale}))
                break

        answer_detail = {"elements": len(elements)}
        if self.settings.reasoning_quota_enabled and len(used_queries) >= 2:
            # 复合问题: 按子查询配额 round-robin, 避免一方通吃。
            top_hits, counts = self._quota_rerank(
                notebook_id, collected, used_queries, self.settings.retrieval_top_n)
            # 只暴露各子查询贡献数(不含兜底组), 便于观测。
            answer_detail["quota"] = counts[:len(used_queries)]
        else:
            # 单查询/开关关: 原全局重排(用原问题统一打分), 行为不变。
            scored_map = {h.object_id: h for h in self.repo._retrieve_scored(notebook_id, question)}
            top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
            top_hits.sort(key=lambda h: h.relevance, reverse=True)
            top_hits = top_hits[: self.settings.retrieval_top_n]
        answer_detail["kg"] = len(top_hits)
        record(TraceStep(step_type="answer",
                         summary=f"合成: 采用 {len(top_hits)} 个KG候选 + {len(elements)} 段原文",
                         detail=answer_detail))
        return ReasoningResult(top_hits=top_hits, elements=elements, trace=trace)
