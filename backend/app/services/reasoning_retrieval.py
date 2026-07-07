"""推理模式 (mode=reasoning) 的 agentic KG 检索。

结构化骨架 Plan→Retrieve→Reflect→Answer + Reflect 阶段自由图遍历深挖。
手搓 JSON-action 循环(无原生 tool calling),复用 SQLiteRepository 的检索原语。
ReasoningRetriever 持 repo 引用,运行时注入,避免与 sqlite_repository 循环导入。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.models.schemas import TraceStep
from app.services.prompts import (
    PLAN_SCHEMA_HINT, REFLECT_SCHEMA_HINT, plan_prompt, reflect_prompt,
)
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.retrieval import (
    RetrievedChunk, RetrievedElement, RetrievedKnowledge, W_KEYWORD, W_SEMANTIC,
)

KG_TYPES = ("claim", "formula", "procedure", "concept")
PREFER_WEIGHTS = {
    "keyword": (0.7, 0.3),
    "semantic": (0.2, 0.8),
    "balanced": (W_KEYWORD, W_SEMANTIC),
}
_PER_QUERY_LIMIT = 8
# agent 主动 ppr_retrieve 的累计次数上限。写死常量(非 env 开关):reasoning_max_steps=50
# 且每次 ppr_retrieve 都拉到新 chunk=算"有进展"→ stale 熔断不跳,无此上限一次推理可触发
# 多达 50 次全图 PageRank。镜像 search_elements 的 reasoning_max_element_searches。
# 注:run() 初检索后的 seed pass 不计入此上限(它是保证基线、非 agent 动作)。
_MAX_PPR_RETRIEVES = 3

# Reflect 循环中,当上一步检索动作未带来任何新证据时,附加到候选摘要里的提示。
# 目的:让模型"知道"重复检索已无收益,从而自主决定直接作答(而非被强制收尾),
# 仍不替模型拍板 —— 是否 answer 由模型在 reflect 中自行决定。
NO_NEW_EVIDENCE_NOTE = (
    "（系统提示:上一步检索未带来新证据。若现有候选不足以支撑作答,"
    "且继续同类检索难有新增,请直接选择 next_action=answer,并在答案中"
    "如实说明依据不足、据现有信息推理;不要为凑证据而重复无效检索。)"
)


def _norm_query(q: str) -> str:
    """子查询防重的归一化键:压空白 + casefold。保守精确匹配、不做语义归一——
    宁可放过真改写的近似查询(由回喂账目提示模型约束),不误杀新角度。"""
    return " ".join(str(q).split()).casefold()


@dataclass
class _QueryAttempt:
    """单条子查询的执行账目:原文、带来的新增证据数、尝试次数(含被跳过的重复)。"""
    query: str
    new: int = 0
    tries: int = 0


@dataclass
class SubQuery:
    query: str
    types: List[str] = field(default_factory=list)   # 空 = 全部 4 类
    prefer: str = "balanced"
    reason: str = ""


@dataclass
class ReflectDecision:
    sufficient: bool = False
    # answer|expand_graph|add_subquery|search_elements|ppr_retrieve|expand_community
    next_action: str = "answer"
    expand_object_id: str = ""
    expand_edge_type: Optional[str] = None
    expand_direction: str = "both"
    new_sub_query: Optional[SubQuery] = None
    community_focal: str = ""
    elements_query: str = ""
    ppr_query: str = ""
    reason: str = ""


@dataclass
class ReasoningResult:
    top_hits: List[RetrievedKnowledge] = field(default_factory=list)
    elements: List[RetrievedElement] = field(default_factory=list)
    trace: List[TraceStep] = field(default_factory=list)
    chunks: List[RetrievedChunk] = field(default_factory=list)
    # 子查询执行账目({"query","new","tries"}),供报告管线做知识缺口分析。
    attempted: List[dict] = field(default_factory=list)


class ReasoningRetriever:
    def __init__(self, repo, settings, cancel_event: CancelEvent = None):
        self.repo = repo
        self.settings = settings
        self.cancel_event = cancel_event

    # --- KG 工具箱(薄封装 repo 原语) ---
    def search(self, notebook_id, query, types=None, prefer="balanced"):
        wk, ws = PREFER_WEIGHTS.get(prefer, PREFER_WEIGHTS["balanced"])
        return self.repo.retrieval.federated_retrieve(notebook_id, query, types=types,
                                                      w_keyword=wk, w_semantic=ws)

    def neighbors(self, notebook_id, object_id, edge_type=None, direction="both"):
        return self.repo.retrieval.retrieve_neighbors(notebook_id, object_id, edge_type, direction)

    def get(self, notebook_id, object_id):
        try:
            return self.repo.retrieval.node_context(notebook_id, object_id)
        except KeyError:
            return {}

    def search_elements(self, notebook_id, query):
        return self.repo.retrieval.retrieve_elements(notebook_id, query)

    def ppr_retrieve(self, notebook_id, query):
        return self.repo.retrieval.ppr_retrieve(notebook_id, query)

    # --- LLM 决策点 ---
    def plan(self, question, history=""):
        raise_if_cancelled(self.cancel_event)
        from app.services.query_rewrite import expand_query
        fallback = [SubQuery(query=question)]
        ex = expand_query(self.repo.reasoning_llm_client, question, history,
                          timeout=self.settings.reasoning_timeout_seconds,
                          max_retries=self.settings.reasoning_max_retries,
                          max_subqueries=self.settings.reasoning_max_subqueries,
                          want_types=True,
                          cancel_event=self.cancel_event)
        out = [SubQuery(query=s.query, types=s.types, prefer=s.prefer, reason=s.reason)
               for s in ex.sub_queries]
        return out or fallback

    def reflect(self, question, candidates_summary):
        raise_if_cancelled(self.cancel_event)
        answer_decision = ReflectDecision(sufficient=True, next_action="answer")
        if not getattr(self.repo.reasoning_llm_client, "configured", False):
            return answer_decision
        try:
            raw = self.repo.reasoning_llm_client.chat_json(
                [{"role": "user", "content": reflect_prompt(question, candidates_summary)}],
                REFLECT_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries,
                cancel_event=self.cancel_event)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return answer_decision
            action = str(data.get("next_action", "answer"))
            if action not in ("answer", "expand_graph", "add_subquery",
                               "search_elements", "ppr_retrieve", "expand_community"):
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
            d.community_focal = str(data.get("community_focal", "")).strip()
            d.elements_query = str(data.get("elements_query", "")).strip()
            d.ppr_query = str(data.get("ppr_query", "")).strip()
            return d
        except AskCancelled:
            raise
        except Exception:
            return answer_decision

    # --- 编排 ---
    def _quota_rerank(self, notebook_id, collected, used_queries, top_n):
        """复合问题: 按子查询配额 round-robin 选 top_n。
        步骤 1: 每个子查询全库重打分(容错: 抛错则该组空)。
        步骤 2-4: 分组+轮转委托给通用 quota_fuse。
        返回 (top_hits, counts): counts[i]=第 i 个子查询贡献数, counts[-1]=兜底组。"""
        from app.services.retrieval import quota_fuse
        per_q = []
        for q in used_queries:
            try:
                per_q.append({h.object_id: h for h in self.search(notebook_id, q)})
            except Exception:
                per_q.append({})
        return quota_fuse(collected, per_q, top_n)

    @staticmethod
    def _window(items, head, tail):
        """头+尾窗口:超窗时保留最早 head 条 + 最新 tail 条,返回 (头段, 尾段, 省略数)。
        collected/elements/chunks 都按插入序只增不删,纯前缀窗口会让"最近新增"
        落在窗口外:reflect 看到的 summary 不变,误判无进展、重复请求。"""
        if len(items) <= head + tail:
            return list(items), [], 0
        return list(items[:head]), list(items[-tail:]), len(items) - head - tail

    def _summarize(self, collected, elements, chunks):
        lines = []

        def _kg_line(rk):
            name = str(rk.payload.get("name", "")).strip() or rk.object_id
            return f"- [{rk.object_type}] {name} (id={rk.object_id})"

        def _el_line(el):
            return f"- [element] {el.source_title} · {el.location_label}: {el.text[:80]}"

        def _ch_line(c):
            return f"- [chunk] {c.source_title} · {c.section_path}: {c.text[:80]}"

        for items, render, head_n, tail_n, noun in (
                (list(collected.values()), _kg_line, 20, 10, "条较早候选"),
                (elements, _el_line, 6, 4, "段较早原文"),
                (chunks, _ch_line, 6, 4, "段较早原文")):
            head, tail, omitted = self._window(items, head_n, tail_n)
            lines.extend(render(x) for x in head)
            if omitted:
                lines.append(f"-（省略中间 {omitted} {noun},以下为最近加入）")
            lines.extend(render(x) for x in tail)
        return "\n".join(lines) if lines else "(no candidates yet)"

    def run(self, notebook_id, question, history="", on_step=None, top_n=None, max_steps=None):
        raise_if_cancelled(self.cancel_event)
        # top_n 覆盖 settings.retrieval_top_n(报告管线每节独立预算);None=沿用全局。
        top_n = top_n or self.settings.retrieval_top_n
        # max_steps 覆盖 settings.reasoning_max_steps(报告滑块封顶 reflect 轮数);None=沿用全局。
        max_steps = max_steps or self.settings.reasoning_max_steps
        trace: List[TraceStep] = []
        collected: Dict[str, RetrievedKnowledge] = {}
        elements: List[RetrievedElement] = []
        chunks: List[RetrievedChunk] = []
        seen_chunks: set = set()
        visited: set = set()

        # 每步耗时 = 相邻两次 record 的墙钟差(步在其工作完成后才 record,故
        # 差值即该步工作耗时);首步从 run 起点算(含 plan 的 LLM 时间)。
        last_ts = time.perf_counter()

        def record(step: TraceStep) -> None:
            nonlocal last_ts
            raise_if_cancelled(self.cancel_event)
            now = time.perf_counter()
            step.duration_ms = round((now - last_ts) * 1000)
            last_ts = now
            trace.append(step)
            if on_step:
                on_step(step)

        subqueries = self.plan(question, history)
        raise_if_cancelled(self.cancel_event)
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
            raise_if_cancelled(self.cancel_event)
            try:
                hits = self.search(notebook_id, sq.query, sq.types, sq.prefer)[:_PER_QUERY_LIMIT]
                raise_if_cancelled(self.cancel_event)
                return hits
            except AskCancelled:
                raise
            except Exception:
                return []

        # 子查询执行账目(初始 plan 与 add_subquery 后补都记):归一化键 → 账目。
        # 每轮回喂 reflect(模型能看到试过什么、哪条是干的),add_subquery 对
        # 重复键硬跳过 —— 治「反复补充同一条子查询」的两层根源。
        attempted: Dict[str, _QueryAttempt] = {}
        if subqueries:
            with ThreadPoolExecutor(max_workers=min(len(subqueries), 8)) as ex:
                # map 保序:第 i 个结果对应第 i 个子查询,与提交顺序一致。
                for sq, hits in zip(subqueries, ex.map(_run_search, subqueries)):
                    raise_if_cancelled(self.cancel_event)
                    rec = attempted.setdefault(_norm_query(sq.query),
                                               _QueryAttempt(query=sq.query))
                    rec.tries += 1
                    for h in hits:
                        if h.object_id not in collected:
                            collected[h.object_id] = h
                            rec.new += 1
        record(TraceStep(step_type="retrieve",
                         summary=f"初检索得到 {len(collected)} 个候选节点",
                         detail={"count": len(collected)}))

        # PPR seed pass(确定性兜底):flag 开时无条件先跑一次跨文档 PPR,保证对比/跨文档题
        # 至少有一组跨文档 chunk,不赌 agent 是否选 ppr_retrieve。纯图传播、无 LLM、图已缓存。
        if self.settings.graph_ppr_enabled:
            raise_if_cancelled(self.cancel_event)
            seeded = [c for c in self.ppr_retrieve(notebook_id, question)
                      if c.chunk_id not in seen_chunks]
            for c in seeded:
                seen_chunks.add(c.chunk_id)
            chunks.extend(seeded)
            record(TraceStep(step_type="ppr",
                             summary=f"概念漫游:跨文档检索,得到 {len(seeded)} 段原文",
                             detail={"found": len(seeded), "phase": "seed"}))

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
        ppr_searches = 0
        while steps < max_steps:
            raise_if_cancelled(self.cancel_event)
            steps += 1
            summary = self._summarize(collected, elements, chunks)
            if no_progress:
                summary = f"{summary}\n\n{NO_NEW_EVIDENCE_NOTE}"
            # 已展开过的节点回喂 reflect, 提示模型勿重复请求(治"反复 expand 同节点"根源)。
            if visited:
                vis = ", ".join(
                    f"{str(collected[o].payload.get('name', o)) if o in collected else o}"
                    for o in visited)
                summary = f"{summary}\n\n（已展开过的节点，勿重复 expand_graph 请求它们: {vis}）"
            # 已执行过的子查询账目回喂 reflect(镜像 visited 回喂,治"反复补充同
            # 一条子查询"):模型据此区分"没查过"与"查过但没捞到";账目含尝试次数,
            # 重复被跳过时 prompt 仍变化 → 不再是不动点,LLM 缓存不会逐字重放决策。
            if attempted:
                tried = "、".join(
                    f"「{a.query}」(新增{a.new}条"
                    + (f",已试{a.tries}次" if a.tries > 1 else "") + ")"
                    for a in attempted.values())
                summary = (f"{summary}\n\n（已执行过的子查询及各自新增证据数: {tried}。"
                           "勿重复提交相同子查询;新增为 0 的方向请换明显不同的问法,"
                           "或改用其他动作。）")
            decision = self.reflect(question, summary)
            raise_if_cancelled(self.cancel_event)
            record(TraceStep(step_type="reflect",
                             summary=decision.reason or decision.next_action,
                             detail={"next_action": decision.next_action,
                                     "sufficient": decision.sufficient,
                                     "no_progress": no_progress, "stale": stale}))
            if decision.next_action == "answer" or decision.sufficient:
                break
            before = len(collected) + len(elements) + len(chunks)
            if decision.next_action == "expand_graph":
                oid = decision.expand_object_id
                if not oid or oid in visited:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 expand_graph(空或已访问节点)",
                                     detail={"object_id": oid, "reason": "empty_or_visited"}))
                else:
                    visited.add(oid)
                    # NB: expand/neighbors use the ACTIVE notebook_id only. A base-tier hit's
                    # neighbors live in the base notebook, so deep cross-tier graph walks are
                    # graph mode's job (_federated_rx_graph), not reasoning mode (P4 spec §F).
                    neigh = self.neighbors(notebook_id, oid,
                                           decision.expand_edge_type, decision.expand_direction)
                    raise_if_cancelled(self.cancel_event)
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
                    key = _norm_query(sq.query)
                    if key in attempted:
                        # 重复子查询硬跳过(镜像 expand_graph 的 visited 守卫):
                        # 不重跑检索;tries 递增让回喂账目(与 prompt)随之变化。
                        attempted[key].tries += 1
                        record(TraceStep(step_type="skip",
                                         summary=f"跳过重复子查询: {sq.query}",
                                         detail={"query": sq.query,
                                                 "reason": "duplicate_subquery",
                                                 "tries": attempted[key].tries}))
                    else:
                        added = 0
                        for h in self.search(notebook_id, sq.query,
                                             sq.types, sq.prefer)[:_PER_QUERY_LIMIT]:
                            raise_if_cancelled(self.cancel_event)
                            if h.object_id not in collected:
                                collected[h.object_id] = h
                                added += 1
                        attempted[key] = _QueryAttempt(query=sq.query,
                                                       new=added, tries=1)
                        if sq.query not in used_queries:
                            used_queries.append(sq.query)
                        record(TraceStep(step_type="retrieve",
                                         summary=f"补充子查询: {sq.query}",
                                         detail={"query": sq.query, "new": added}))
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
                    raise_if_cancelled(self.cancel_event)
                    elements.extend(els)
                    record(TraceStep(step_type="fallback",
                                     summary=f"降级查原文: {eq},新增 {len(els)} 段",
                                     detail={"query": eq, "found": len(els)}))
            elif decision.next_action == "ppr_retrieve":
                if not self.settings.graph_ppr_enabled:
                    record(TraceStep(step_type="skip",
                                     summary="跳过概念漫游(未启用)",
                                     detail={"reason": "ppr_disabled"}))
                elif ppr_searches >= _MAX_PPR_RETRIEVES:
                    record(TraceStep(step_type="skip",
                                     summary=f"跳过概念漫游(已达次数上限 {_MAX_PPR_RETRIEVES})",
                                     detail={"reason": "ppr_retrieve_cap"}))
                else:
                    ppr_searches += 1
                    pq = decision.ppr_query or question
                    new = [c for c in self.ppr_retrieve(notebook_id, pq)
                           if c.chunk_id not in seen_chunks]
                    for c in new:
                        seen_chunks.add(c.chunk_id)
                    chunks.extend(new)
                    record(TraceStep(step_type="ppr",
                                     summary=f"概念漫游:{pq},新增 {len(new)} 段",
                                     detail={"query": pq, "found": len(new), "phase": "action"}))
            else:
                break
            # 本轮动作后是否有新增(候选节点或原文段)。无新增 → 下一轮提示模型 + 累加 stale。
            no_progress = (len(collected) + len(elements) + len(chunks)) == before
            stale = stale + 1 if no_progress else 0
            # 连续 stale_limit 轮无有效进展 → 硬熔断, 强制走到末尾 answer(不再交模型自觉)。
            if stale >= self.settings.reasoning_stale_limit:
                record(TraceStep(step_type="skip",
                                 summary=f"连续 {stale} 轮无新进展,熔断收尾(避免空转)",
                                 detail={"reason": "stale_circuit_breaker", "stale": stale}))
                break

        answer_detail = {"elements": len(elements)}
        raise_if_cancelled(self.cancel_event)
        if self.settings.reasoning_quota_enabled and len(used_queries) >= 2:
            # 复合问题: 按子查询配额 round-robin, 避免一方通吃。
            top_hits, counts = self._quota_rerank(
                notebook_id, collected, used_queries, top_n)
            # 只暴露各子查询贡献数(不含兜底组), 便于观测。
            answer_detail["quota"] = counts[:len(used_queries)]
        else:
            # 单查询/开关关: 原全局重排(用原问题统一打分), 行为不变。
            scored_map = {h.object_id: h for h in self.repo.retrieval.retrieve_scored(notebook_id, question)}
            top_hits = [scored_map.get(oid, rk) for oid, rk in collected.items()]
            top_hits.sort(key=lambda h: h.relevance, reverse=True)
            top_hits = top_hits[:top_n]
        raise_if_cancelled(self.cancel_event)
        answer_detail["kg"] = len(top_hits)
        record(TraceStep(step_type="answer",
                         summary=f"合成: 采用 {len(top_hits)} 个KG候选 + {len(elements)} 段原文",
                         detail=answer_detail))
        return ReasoningResult(
            top_hits=top_hits, elements=elements, trace=trace, chunks=chunks,
            attempted=[{"query": a.query, "new": a.new, "tries": a.tries}
                       for a in attempted.values()])
