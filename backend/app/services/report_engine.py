"""深度报告引擎:大纲规划 → 每节完整 reasoning 深挖(节间并行) → 逐节撰写
(证据三层 [k]/（推断）/【通识】) → 汇总(执行摘要/参考文献/结尾局限)。

设计对齐 docs/superpowers/specs/2026-07-03-deep-report-mode-design.md。
形态镜像 ReasoningRetriever(Task 25 端口化):引擎只持 ReportEngineDependencies
里的窄端口(reports/retrieval/evidence_context/model_clients/model_errors/
source_query/communities),写库经 reports 端口,不再持 repository facade;
旧 repository 调用点由 from_repository 一次性工厂适配。
线程要点:节间 ThreadPoolExecutor 并行,worker 不继承 ContextVar——每个 submit
用 contextvars.copy_context().run 包裹,保住 per-user 模型解析。
取消注册表:进程全局所有权在 report_execution.REPORT_CANCELLATIONS,本模块的
register_cancel/cancel_report/unregister_cancel 是它的显式委托(冻结调用点)。
"""
from __future__ import annotations
import contextvars
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from app.core.ask_retrieval_policy import (
    AskRetrievalLimits, RetrievalEffort, ask_retrieval_limits,
)
from app.core.llm import cap_kwargs
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.report_execution import REPORT_CANCELLATIONS

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.repositories.ports import (
        CommunityQueryPort, EvidenceContextPort, ModelClientProvider,
        ModelErrorSink, ReportRepository, ReportSourceQueryPort, RetrievalPort,
    )

_MARKER = re.compile(r"\[(k\d+(?:\s*,\s*k\d+)*)\]")   # 节内 [k_i] 或 [k_i, k_j] 引用标记(全局重编号用)
_UNRESOLVED_REFERENCE = re.compile(
    r"(?:这个|那个|这些|那些|上述|前述|刚才|之前提到|该问题|该方案|它们?)"
    r"|\b(?:this|that|these|those|it|they|above|previous|former|latter)\b",
    re.IGNORECASE,
)
_GENERIC_REQUEST = re.compile(
    r"^(?:(?:帮我)?(?:分析|研究|介绍|讲讲|说说|看看|总结|比较|对比|优化)(?:一下|下)?"
    r"(?:这个|那个|它|问题|方案|内容|东西)?|"
    r"(?:please\s+)?(?:analy[sz]e|review|compare|explain|optimi[sz]e)\s*"
    r"(?:this|that|it|them)?)\s*[。.!！?？]*$",
    re.IGNORECASE,
)
_INTENT_TYPES = {"explain", "compare", "diagnose", "design", "review", "other"}


# --- 研究深度 → 检索档位(报告 PR-5,设计文档 §3.4)------------------------
# 报告的「研究深度」与 Ask 的「检索档位」共用 EffortPicker 和同一批档名,但报告
# 此前调 ReasoningRetriever.run 时 limits=None —— 五档共享 standard 预算,滑块只
# 改每节反思轮数。这张表把同名档位的**语义**对齐:同一个档名在两处买到同一份
# 相关性/上下文预算。行为变化是显式的(低档报告的检索预算随之变小、高档变大),
# 它是对齐修复,不是回归。
#
# 每节 `max_steps` 仍用报告自己的 depth 值(1/2/4/8/16),**不**采用档位表里的
# 4/8/16/32/50:报告的成本按节数放大,一节 50 轮乘以 6 节不是用户在滑块上同意的
# 那个量级。run() 里 `min(max_steps, limits.max_reasoning_steps)` 保证 depth 永远
# 是更紧的那个。
#
# 阈值表(depth 下界 → 档位),顺序必须与 frontend/app/report-view.tsx 的
# `DEPTHS = [1, 2, 4, 8, 16]` 逐档对应。写成阈值而不是精确字典:API 只把 depth
# clamp 到 [1,16],3/5/7 这类值必须有确定答案,而不是抛 KeyError。
REPORT_DEPTH_EFFORTS: Tuple[Tuple[int, RetrievalEffort], ...] = (
    (1, "overview"),
    (2, "standard"),
    (4, "deep"),
    (8, "thorough"),
    (16, "exhaustive"),
)


def report_retrieval_effort(depth: int) -> RetrievalEffort:
    """报告深度值 → 检索档位 id(总函数,表外深度取不超过它的最高档)。"""
    effort: RetrievalEffort = REPORT_DEPTH_EFFORTS[0][1]
    for threshold, candidate in REPORT_DEPTH_EFFORTS:
        if depth >= threshold:
            effort = candidate
    return effort


def report_retrieval_limits(depth: Optional[int]) -> Optional[AskRetrievalLimits]:
    """本次深挖的档位预算。``depth`` 为 None(未指定深度的调用方)时保持 None,
    即接入前的「无 limits」行为。"""
    if depth is None:
        return None
    return ask_retrieval_limits(report_retrieval_effort(int(depth)))


# --- 方向合并后的最终选集上限(报告 PR-5,codex PR#418 R1 P2-1)------------

def clamp_merged_evidence(result: Any, limits: Optional[AskRetrievalLimits]) -> None:
    """把「按已确认检索方向补检索」合并进来的证据压回所选档位的最终选集上限。

    档位贯通此前只到 `ReasoningRetriever.run` 为止:run 内部按 `ranked_final_*`
    夹好选集,`_deep_dive` 随后**逐方向**再追加(每方向一批 KG 命中 + 8 个元素),
    合并结果直接绕过了这个上限 —— 概览档买到的是 12 条 KG 的预算,4 个方向却能
    让它拿到 4 倍。方向本身照常执行(「已确认方向必须真执行」是合同,这里不动),
    只在合并**之后**重新截断。

    截断口径与 Ask 侧最终选集一致:KG 按相关度降序、同分保持原顺序;元素按
    `(-score, element_id)`(逐字照抄 `_answer_reasoning` 的键,两处对同一批元素
    必须给出同一个前 N,否则 `_draft_section` 的第二道同名闸会挑出另一批)。
    **没超上限就一个字节都不动**:未超限时重排没有任何取舍可做,却会改变上下文
    渲染顺序,那是一处与本修复无关、五档全会碰上的行为变化。

    **大纲绑定的对象豁免**(`outline_evidence` 那一批):它们的相关度在 retriever
    侧被刻意夹到选集最低分以下(见 `outline_truncated_kg_evidence`),按相关度排序
    必然排在最末 —— 一起参与截断等于把它们再删一次,而它们恰恰是「被选集挤出去、
    但模型指名要」的那一批。Ask 侧同样把它们放在 `top_hits` 选集之外
    (`outline_pool_extra` 直接进分类池),所以这里的豁免是同一条规则,不是报告
    的例外。它们进 prompt 仍由 `knowledge_context_with_outline` 的子预算负责。
    """
    if limits is None:
        return
    bound = {
        str(getattr(hit, "object_id", "") or "")
        for hit in (getattr(result, "outline_evidence", None) or [])
    }
    ranked = [
        hit for hit in result.top_hits
        if str(getattr(hit, "object_id", "") or "") not in bound
    ]
    if len(ranked) > limits.ranked_final_cap:
        exempt = [
            hit for hit in result.top_hits
            if str(getattr(hit, "object_id", "") or "") in bound
        ]
        result.top_hits = sorted(
            ranked, key=lambda hit: -float(getattr(hit, "relevance", 0.0) or 0.0)
        )[: limits.ranked_final_cap] + exempt
    if len(result.elements) > limits.answer_element_items:
        result.elements = rank_elements(
            result.elements, limits.answer_element_items)


def rank_elements(elements: Sequence[Any], keep: int) -> List[Any]:
    """按检索相关度降序取前 ``keep`` 个直接原文段(tie-break `element_id`)。

    键与 `ask_service._answer_reasoning` 逐字相同:按插入序切片会在检索到的元素
    多于上限时静默丢掉最相关的那几个。
    """
    return sorted(
        elements,
        key=lambda element: (-float(getattr(element, "score", 0.0) or 0.0),
                             str(getattr(element, "element_id", ""))),
    )[: max(0, int(keep))]


# --- 大纲绑定证据的 KG 上下文子预算(报告 PR-5)---------------------------
# 绑定子集分到的那一份 = 总预算的 1/2(照 ask_service 枚举块 `chunk_context_chars
# // 2` 的先例)。留一半而不是全给:绑定证据是结构支撑,不是本节证据的全部,
# ranked 命中被它整个饿死同样是一种坏法。
_OUTLINE_KG_BUDGET_DIVISOR = 2


def outline_bound_evidence_keys(sections: Sequence[Any]) -> set:
    """子大纲各节绑定的候选键(检索期 id;知识对象/元素/原文段三个 id 空间混在
    一起,消费方按自己那一路匹配即可 —— 三者互不相交)。"""
    return {
        str(key)
        for section in (sections or ())
        for key in (getattr(section, "evidence_keys", None) or ())
        if key
    }


def knowledge_context_with_outline(
    evidence_context: Any,
    notebook_id: str,
    hits: Sequence[Any],
    sections: Sequence[Any],
    *,
    id_offset: int,
    budget_chars: int,
) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """KG 上下文:本节子大纲绑定的命中先拿一份子预算,其余 ranked 命中吃剩下的。

    **为什么需要子预算**:`knowledge_context` 按输入序渲染、预算用尽即停,而
    `_deep_dive` 把大纲绑定的截断对象**追加在 `top_hits` 尾部**(它们的相关度本来
    就低于选集,只能排在后面)。KG 预算大约只装得下前几十条的一部分,而穷尽档的
    最终相关性 floor 是 40 —— 于是在候选池够大的场景里,尾部那批「模型自己指名要
    的结构支撑」**恒**被挤出上下文;`outline_structure_block` 反查不到就如实丢弃
    绑定,子话题退成裸标题,prompt 又教「缺证据的子话题略过」,最后它从成稿里
    整条消失。尾插本身不是承诺,子预算才是。

    **优先级交给端口在一次调用里处理,绝不由这里拆成两次**(codex PR#418 R2 P2):
    `knowledge_context` 末尾的 `relations:` 行是对本次证据集**内部**的边求的,拆两
    次会丢掉所有跨两半的关系(一个绑定对象与一个 ranked 对象同时在 prompt 里、它们
    之间那条存下来的边却没了),还会渲染出两行 `relations:` 各记一次预算,cluster
    去重也会各算各的。

    绑定子集为空(非穷尽档、或本节没整理出大纲、或绑定的全是元素/原文段)时不传
    优先级参数,输出与接入前逐字相同 —— 那是这条路径唯一可接受的关闭态。
    """
    bound_keys = outline_bound_evidence_keys(sections)
    bound_ids = [
        str(getattr(hit, "object_id", "") or "") for hit in hits
        if str(getattr(hit, "object_id", "") or "") in bound_keys
    ] if bound_keys else []
    if not bound_ids:
        return evidence_context.knowledge_context(
            notebook_id, hits, id_offset=id_offset, budget_chars=budget_chars)
    return evidence_context.knowledge_context(
        notebook_id, hits, id_offset=id_offset, budget_chars=budget_chars,
        priority_object_ids=bound_ids,
        priority_budget_chars=max(0, int(budget_chars)) // _OUTLINE_KG_BUDGET_DIVISOR)


# --- 「发现的结构」块(报告 PR-5)-----------------------------------------
# 深挖时整理出的子大纲进节撰写 prompt 的有界形态。硬界三条:行数、单行字符、
# 整块字符。纯拼装,零模型调用、零新查询 —— 大纲与 id_map 都是手上现成的。
_STRUCTURE_MAX_LINES = 12
_STRUCTURE_MAX_LINE_CHARS = 80
_STRUCTURE_MAX_CHARS = 1200


def outline_structure_block(
    sections: Sequence[Any], id_map: Dict[str, Dict[str, Any]]
) -> str:
    """终态子大纲 + 本节证据 id_map → 「发现的结构」块(每子节一行)。

    ``[k]`` 必须与撰写模型手上的**同一套** id 空间一致:绑定键存的是检索期的
    object/element/chunk id,而 `[k]` 是 `_draft_section` 装配上下文时分配的,所以
    这里按 id_map 的 `object_id` 反查。**映射不到的绑定如实丢弃**(它被上下文预算
    截断了,写进去就是让模型引用一个它看不见的 id),标题仍保留 —— 那一条子话题
    prompt 会教它「缺证据就略过」。

    顶层节渲染成 `###`(报告的节本身已经是 `##`),有父节的渲染成 `####`,层级
    信息不丢。超界按顺序截断并显式记账 `(+N 子节略)`:静默丢行会让模型以为它
    看到的就是全部结构。
    """
    if not sections:
        return ""
    by_object: Dict[str, str] = {}
    for key, value in (id_map or {}).items():
        object_id = str((value or {}).get("object_id") or "")
        if object_id:
            by_object.setdefault(object_id, key)
    candidates: List[str] = []
    for section in sections:
        title = str(getattr(section, "title", "") or "").strip()
        if not title:
            continue
        marks = list(dict.fromkeys(
            by_object[key]
            for key in (getattr(section, "evidence_keys", None) or [])
            if key in by_object
        ))
        heading = "####" if str(getattr(section, "parent", "") or "") else "###"
        # 行内先裁标题、再逐个装 [k],装不下的整条丢弃 —— 直接裁整行会在长标题
        # (parse 允许 60 字符)加多个绑定时把标记切成半截 `[k1, k`,那是一个撰写
        # 模型解释不了、却看着像引用的东西。
        line = f"{heading} {title}"[:_STRUCTURE_MAX_LINE_CHARS]
        rendered: List[str] = []
        for mark in marks:
            candidate = f"{line} — 证据: [" + ", ".join(rendered + [mark]) + "]"
            if len(candidate) > _STRUCTURE_MAX_LINE_CHARS:
                break
            rendered.append(mark)
        if rendered:
            line = f"{line} — 证据: [" + ", ".join(rendered) + "]"
        candidates.append(line)
    kept: List[str] = []
    used = 0
    for line in candidates:
        separator = 1 if kept else 0
        if (len(kept) >= _STRUCTURE_MAX_LINES
                or used + separator + len(line) > _STRUCTURE_MAX_CHARS):
            break
        kept.append(line)
        used += separator + len(line)
    if not kept:
        return ""
    omitted = len(candidates) - len(kept)
    while omitted:
        suffix = f"(+{omitted} 子节略)"
        if used + 1 + len(suffix) <= _STRUCTURE_MAX_CHARS:
            kept.append(suffix)
            break
        # 账目行本身也要在预算内:挤不下就再退一行(退掉的那行计进 N)。
        used -= len(kept[-1]) + (1 if len(kept) > 1 else 0)
        kept.pop()
        omitted += 1
        if not kept:
            return ""
    return "\n".join(kept)


# --- 取消注册表委托:report_id → threading.Event(活动后台 job 才在册) ---
# 所有权在 report_execution.REPORT_CANCELLATIONS(进程全局唯一实例);这三个
# 函数保持冻结调用点(routes cancel 端点/测试)可用。

def register_cancel(report_id: str) -> threading.Event:
    ev = threading.Event()
    if not REPORT_CANCELLATIONS.register(report_id, ev):
        raise RuntimeError(f"report already has an active job: {report_id}")
    return ev


def cancel_report(report_id: str) -> bool:
    return REPORT_CANCELLATIONS.cancel(report_id)


def unregister_cancel(report_id: str, event: threading.Event) -> None:
    REPORT_CANCELLATIONS.unregister(report_id, event)


@dataclass(frozen=True)
class ReportEngineDependencies:
    """引擎的全部协作面(窄端口,消费者所有的契约见 app.repositories.ports)。"""
    reports: "ReportRepository"
    retrieval: "RetrievalPort"
    evidence_context: "EvidenceContextPort"
    model_clients: "ModelClientProvider"
    model_errors: "ModelErrorSink"
    source_query: "ReportSourceQueryPort"
    communities: "CommunityQueryPort"
    settings: "Settings"
    event_log: Any
    memory_retriever: Any = None


class ReportEngine:
    def __init__(self, dependencies: ReportEngineDependencies, *,
                 user_id: str, cancel_event: CancelEvent = None):
        self.dependencies = dependencies
        self.settings = dependencies.settings
        self.user_id = user_id            # 发起者身份(审计归属;模型解析走 ContextVar)
        self.cancel_event = cancel_event

    @classmethod
    def from_repository(cls, repository, settings, cancel_event: CancelEvent = None):
        """Frozen-call-site adapter; extracts narrow ports and retains no facade."""
        engine = repository.report_execution.engine_factory(
            user_id=repository.current_user().id,
            cancel_event=cancel_event,
            settings=settings,
        )
        if cls is ReportEngine:
            return engine
        return cls(
            engine.dependencies,
            user_id=engine.user_id,
            cancel_event=engine.cancel_event,
        )

    # --- Stage A ---
    def _plan_outline(self, notebook_id: str, question: str, history: str) -> List[dict]:
        from app.services.prompts import report_outline_prompt, REPORT_OUTLINE_SCHEMA_HINT
        client = self.dependencies.model_clients.chat("report_outline")
        try:
            raw = client.chat_json(
                [{"role": "user", "content": report_outline_prompt(
                    question, max_sections=self.settings.report_max_sections,
                    history_block=history)}],
                REPORT_OUTLINE_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            sections = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    sections.append({"title": title,
                                     "scope": str(s.get("scope", "")).strip(),
                                     "sub_queries": subs[:4]})
            if sections:
                return sections
        except AskCancelled:
            raise
        except Exception:
            pass
        # 回退骨架:expand_query 的子查询平铺为单节(保证总能出报告)。
        from app.services.query_rewrite import expand_query
        ex = expand_query(self.dependencies.model_clients.chat("query_rewrite"),
                          question, history)
        return [{"title": "分析", "scope": question,
                 "sub_queries": [s.query for s in ex.sub_queries][:4] or [question]}]

    def _plan_intent_contract(self, question: str, history: str, *,
                              confirmation_mode: bool = False) -> dict:
        """Freeze user semantics before any corpus-derived planning signal exists."""
        from app.services.prompts import report_intent_prompt, REPORT_INTENT_SCHEMA_HINT

        topics: List[dict] = []
        data: dict = {}
        try:
            raw = self.dependencies.model_clients.chat("report_outline").chat_json(
                [{"role": "user", "content": report_intent_prompt(
                    question,
                    max_topics=self.settings.report_max_sections,
                    history_block=history,
                    confirmation_mode=confirmation_mode,
                )}],
                REPORT_INTENT_SCHEMA_HINT,
                cancel_event=self.cancel_event,
            )
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
            for index, topic in enumerate(
                (data.get("mandatory_topics") or [])[: self.settings.report_max_sections], 1
            ):
                if not isinstance(topic, dict):
                    continue
                topic_question = str(topic.get("question") or "").strip()
                title = str(topic.get("title") or topic_question).strip()
                queries = [
                    str(item).strip()
                    for item in (topic.get("retrieval_queries") or [])
                    if str(item).strip()
                ][:4]
                if not title or not topic_question:
                    continue
                topics.append({
                    "id": f"intent-{index}",
                    "title": title,
                    "question": topic_question,
                    "retrieval_queries": queries or [topic_question],
                })
        except AskCancelled:
            raise
        except Exception:
            data = {}

        if not topics:
            topics = [{
                "id": "intent-1",
                "title": question[:80] or "分析",
                "question": question,
                "retrieval_queries": [question],
            }]

        def _strings(key: str, limit: int = 8) -> List[str]:
            return [
                str(item).strip() for item in (data.get(key) or [])
                if str(item).strip()
            ][:limit]

        ambiguities: List[dict] = []
        if not confirmation_mode:
            for index, item in enumerate((data.get("ambiguities") or [])[:8], 1):
                if not isinstance(item, dict):
                    continue
                prompt = str(item.get("question") or "").strip()
                if not prompt:
                    continue
                ambiguities.append({
                    "id": f"ambiguity-{index}",
                    "question": prompt,
                    "reason": str(item.get("reason") or "").strip()[:300],
                    "required": item.get("required") is not False,
                    "options": [
                        str(option).strip() for option in (item.get("options") or [])
                        if str(option).strip()
                    ][:4],
                })

            deterministic_question = ""
            deterministic_reason = ""
            if not history.strip() and _UNRESOLVED_REFERENCE.search(question):
                deterministic_question = "你提到的对象具体是什么？请给出名称或简要背景。"
                deterministic_reason = "问题包含无法从当前报告上下文解析的指代。"
            elif _GENERIC_REQUEST.fullmatch(question.strip()):
                deterministic_question = "你希望研究的具体对象和最关心的问题是什么？"
                deterministic_reason = "当前输入缺少可确定报告主题的研究对象或目标。"
            if deterministic_question and not any(
                row["question"] == deterministic_question for row in ambiguities
            ):
                ambiguities.insert(0, {
                    "id": "ambiguity-input",
                    "question": deterministic_question,
                    "reason": deterministic_reason,
                    "required": True,
                    "options": [],
                })

            if bool(data.get("needs_clarification")) and not ambiguities:
                ambiguities.append({
                    "id": "ambiguity-1",
                    "question": "为了准确规划报告，还需要补充哪项关键信息？",
                    "reason": "问题理解模型判断当前请求仍存在会改变研究主题的歧义。",
                    "required": True,
                    "options": [],
                })

        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        normalized_question = str(data.get("normalized_question") or "").strip() or question
        intent_type = str(data.get("intent_type") or "other").strip().lower()
        if intent_type not in _INTENT_TYPES:
            intent_type = "other"

        return {
            # The original request is the authority.  A model-authored paraphrase is
            # useful only as decomposition metadata and must never replace it.
            "objective": question,
            "resolved_question": normalized_question,
            "intent_type": intent_type,
            "entities": _strings("entities"),
            "mandatory_topics": topics,
            "comparison_axes": _strings("comparison_axes"),
            "constraints": _strings("constraints"),
            "excluded_topics": _strings("excluded_topics"),
            "expected_output": str(data.get("expected_output") or "").strip(),
            "assumptions": _strings("assumptions"),
            "ambiguities": ambiguities,
            "confidence": confidence,
            "needs_clarification": any(
                row.get("required") is not False for row in ambiguities
            ),
            "confirmed": confirmation_mode,
        }

    def prepare_intent(self, notebook_id: str, rid: str, question: str,
                       history: str = "", *, auto_generate: bool = False) -> None:
        """Understand the request without touching notebook or mounted-base corpus."""
        reports = self.dependencies.reports
        try:
            reports.update_report(
                notebook_id, rid, status="planning", progress="理解研究问题中"
            )
            contract = self._plan_intent_contract(question, history)
            # Some compatible model clients cannot interrupt an in-flight call.
            # Re-check after it returns so a durable cancellation cannot be
            # followed by publishing a fresh intent_ready state.
            raise_if_cancelled(self.cancel_event)
            contract["auto_generate_requested"] = bool(auto_generate)
            progress = (
                "需要补充关键信息" if contract.get("needs_clarification")
                else "问题理解已就绪，请确认"
            )
            reports.update_report(
                notebook_id,
                rid,
                status="intent_ready",
                progress=progress,
                understanding=contract,
            )
        except AskCancelled:
            reports.update_report(
                notebook_id, rid, status="cancelled", progress="已取消"
            )
        except Exception as exc:
            reports.update_report(
                notebook_id, rid, status="failed",
                error=str(exc)[:500], progress="问题理解失败",
            )

    @staticmethod
    def _intent_catalog(intent_contract: dict) -> List[dict]:
        return [
            {
                "id": str(topic.get("id") or ""),
                "title": str(topic.get("title") or ""),
                "question": str(topic.get("question") or ""),
                "retrieval_queries": list(topic.get("retrieval_queries") or []),
            }
            for topic in (intent_contract.get("mandatory_topics") or [])
            if topic.get("id") and topic.get("question")
        ]

    def _finalize_confirmed_intent(self, seed: dict) -> dict:
        """Freeze the reviewed corpus-blind contract with the user's answers.

        Confirmation is a commit operation, not another interpretation pass.
        Re-running the LLM here could silently replace topics, constraints, or
        comparison axes after the user had reviewed them.  The visible wording
        and clarification answers are therefore overlaid deterministically on
        the exact contract that reached ``intent_ready``.
        """
        confirmed_input = seed.get("confirmed_input") or {}
        resolved_question = str(
            confirmed_input.get("resolved_question")
            or seed.get("resolved_question")
            or seed.get("objective")
            or ""
        ).strip()
        answers = [
            {
                "id": str(row.get("id") or "").strip(),
                "question": str(row.get("question") or "").strip(),
                "answer": str(row.get("answer") or "").strip(),
            }
            for row in (confirmed_input.get("answers") or [])
            if isinstance(row, dict) and str(row.get("answer") or "").strip()
        ][:8]
        final = dict(seed)
        final.update(
            resolved_question=resolved_question,
            ambiguities=[],
            needs_clarification=False,
            confirmed=True,
            clarification_answers=answers,
        )
        final.pop("confirmed_input", None)
        return final

    @staticmethod
    def _confirmed_research_question(intent_contract: dict, fallback: str) -> str:
        """Build the shared authoritative query without changing the visible title."""
        from app.services.query_intent import confirmed_research_question

        return confirmed_research_question(intent_contract, fallback)

    def _probe_queries(self, notebook_id: str, queries: List[str]) -> dict:
        seen, base, elements, sources = set(), set(), set(), set()
        for query in queries[:4]:
            try:
                for hit in self.dependencies.retrieval.federated_retrieve(
                    notebook_id, str(query)
                ):
                    seen.add(hit.object_id)
                    if getattr(hit, "tier", "") == "base":
                        base.add(hit.object_id)
            except Exception:
                pass
            try:
                for element in self.dependencies.retrieval.retrieve_elements(
                    notebook_id, str(query), limit=8
                ):
                    elements.add(element.element_id)
                    if element.source_id:
                        sources.add(element.source_id)
            except Exception:
                pass
        return {
            "hits": len(seen),
            "base_hits": len(base),
            "element_hits": len(elements),
            "source_hits": len(sources),
        }

    def _probe_intent_coverage(self, notebook_id: str, intent_contract: dict) -> List[dict]:
        out: List[dict] = []
        confirmed_question = self._confirmed_research_question(
            intent_contract,
            str(intent_contract.get("objective") or ""),
        )
        for topic in self._intent_catalog(intent_contract):
            queries = list(dict.fromkeys([
                confirmed_question,
                *(str(item).strip() for item in topic.get("retrieval_queries") or []),
                str(topic.get("question") or "").strip(),
            ]))
            counts = self._probe_queries(
                notebook_id,
                [query for query in queries if query],
            )
            out.append({
                "intent_id": topic["id"],
                "title": topic["title"],
                **counts,
            })
        return out

    # --- Stage A(STORM):Corpus map 0-LLM 语料侦察 ---
    _SCOUT_KG_N = 12
    _SCOUT_CHUNK_N = 8

    def _build_corpus_map(self, notebook_id: str, question: str) -> str:
        """0-LLM 语料侦察:来源标题 + federated KG 命中 + PPR chunk 来源·路径。
        给 STORM 规划接地(治盲规划)。任一子步失败静默降级为空段。"""
        deps = self.dependencies
        parts: List[str] = []
        try:
            rows = deps.source_query.report_source_rows(notebook_id)
            titles = [str(r["title"]).strip() for r in rows if str(r["title"]).strip()]
            if titles:
                parts.append("本 notebook 来源文件:\n" + "\n".join(f"- {t}" for t in titles))
        except Exception:
            pass
        try:
            kg = deps.retrieval.federated_retrieve(notebook_id, question)[: self._SCOUT_KG_N]
            if kg:
                parts.append("检索到的知识条目(name[type][tier]):\n" + "\n".join(
                    f"- {str(h.payload.get('name','')).strip()}"
                    f"[{h.object_type}][{getattr(h,'tier','personal')}]" for h in kg))
        except Exception:
            pass
        try:
            chunks = deps.retrieval.ppr_retrieve(notebook_id, question)[: self._SCOUT_CHUNK_N]
            if chunks:
                parts.append("相关原文所在(来源·章节,不含正文):\n" + "\n".join(
                    f"- {c.source_title} · {c.section_path}" for c in chunks))
        except Exception:
            pass
        try:
            memories = (
                deps.memory_retriever.notebook_memory_hits(
                    self.user_id, notebook_id, question, 8
                )
                if deps.memory_retriever is not None else []
            )
            if memories:
                parts.append("用户已确认 Memory:\n" + "\n".join(
                    f"- {item.title}: {item.text[:240]}" for item in memories
                ))
        except Exception:
            pass
        return ("\n\n".join(parts))[:4000] if parts else "(语料侦察无结果)"

    def _probe_sufficiency(self, notebook_id: str, sections: List[dict]) -> List[dict]:
        """0-LLM objective signal over both KG objects and raw SourceElements."""
        out = []
        for s in sections:
            counts = self._probe_queries(notebook_id, list(s.get("sub_queries") or []))
            out.append({"title": s.get("title", ""), **counts})
        return out

    # --- Stage A 编排:intent → intent probe → map → STORM → Judge → outline_ready ---
    def plan_outline(self, notebook_id, rid, question, history="",
                     intent_contract=None) -> None:
        reports = self.dependencies.reports
        try:
            reports.update_report(notebook_id, rid, status="planning", progress="按已确认问题规划中")
            if intent_contract:
                intent_contract = self._finalize_confirmed_intent(
                    dict(intent_contract)
                )
            else:
                intent_contract = (
                    self._plan_intent_contract(
                        question, history, confirmation_mode=True
                    )
                    if history
                    else self._plan_intent_contract(question, history)
                )
            research_question = self._confirmed_research_question(
                intent_contract, question
            )
            reports.update_report(
                notebook_id, rid, understanding=intent_contract
            )
            raise_if_cancelled(self.cancel_event)
            reports.update_report(notebook_id, rid, progress="按用户问题检查证据覆盖")
            intent_probe = self._probe_intent_coverage(notebook_id, intent_contract)
            raise_if_cancelled(self.cancel_event)
            reports.update_report(notebook_id, rid, status="planning", progress="侦察语料中")
            corpus_map = self._build_corpus_map(notebook_id, research_question)
            raise_if_cancelled(self.cancel_event)
            reports.update_report(notebook_id, rid, progress="多视角规划大纲中")
            sections = self._storm_outline(
                notebook_id, research_question, history, corpus_map,
                intent_contract=intent_contract, intent_probe=intent_probe,
            )
            sections = self._bind_outline_to_intent(
                sections, intent_contract, intent_probe
            )
            # 充分性:探针(0 LLM)+ Judge(flash)
            probe = self._probe_sufficiency(notebook_id, sections)
            sections = self._judge_sufficiency(research_question, sections, probe)
            reports.update_report(notebook_id, rid, outline=sections,
                                  status="outline_ready",
                                  progress=f"大纲就绪({len(sections)} 节),待确认")
        except AskCancelled:
            reports.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            reports.update_report(notebook_id, rid, status="failed",
                                  error=str(exc)[:500], progress="规划失败")

    def _storm_outline(self, notebook_id, question, history, corpus_map, *,
                       intent_contract=None, intent_probe=None) -> List[dict]:
        from app.services.prompts import report_storm_outline_prompt, REPORT_STORM_SCHEMA_HINT
        try:
            raw = self.dependencies.model_clients.chat("report_outline").chat_json(
                [{"role": "user", "content": report_storm_outline_prompt(
                    question, corpus_map, max_sections=self.settings.report_max_sections,
                    history_block=history,
                    intent_block=json.dumps(intent_contract or {}, ensure_ascii=False),
                    coverage_block=json.dumps(intent_probe or [], ensure_ascii=False),
                )}],
                REPORT_STORM_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            out = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    out.append({
                        "title": title, "scope": str(s.get("scope", "")).strip(),
                        "sub_queries": subs[:4],
                        "intent_ids": [str(item).strip() for item in
                                       (s.get("intent_ids") or []) if str(item).strip()],
                        "perspectives": [str(p).strip() for p in (s.get("perspectives") or []) if str(p).strip()],
                        "tensions": [str(t).strip() for t in (s.get("tensions") or []) if str(t).strip()]})
            if out:
                return out
        except AskCancelled:
            raise
        except Exception:
            pass
        return self._plan_outline(notebook_id, question, history)   # 回退现行骨架

    def _bind_outline_to_intent(self, sections: List[dict], intent_contract: dict,
                                intent_probe: List[dict]) -> List[dict]:
        """Validate model output and guarantee every mandatory intent is retained."""
        catalog = self._intent_catalog(intent_contract)
        known = {topic["id"]: topic for topic in catalog}
        coverage_by_id = {row.get("intent_id"): row for row in intent_probe}
        out = [dict(section) for section in sections[: self.settings.report_max_sections]]
        for section in out:
            normalized_ids = [
                str(item).strip() for item in (section.get("intent_ids") or [])
                if str(item).strip() in known
            ]
            section["intent_ids"] = list(dict.fromkeys(
                normalized_ids
            ))
        if len(catalog) == 1 and out and not any(s["intent_ids"] for s in out):
            out[0]["intent_ids"] = [catalog[0]["id"]]

        covered = {item for section in out for item in section["intent_ids"]}
        for topic in catalog:
            if topic["id"] in covered:
                continue
            replacement = {
                "title": topic["title"],
                "scope": topic["question"],
                "sub_queries": list(topic["retrieval_queries"])[:4] or [topic["question"]],
                "intent_ids": [topic["id"]],
                "perspectives": ["用户明确要求"],
                "tensions": [],
            }
            if len(out) < self.settings.report_max_sections:
                out.append(replacement)
            else:
                supplemental = next(
                    (index for index in range(len(out) - 1, -1, -1)
                     if not out[index]["intent_ids"]),
                    None,
                )
                if supplemental is not None:
                    out[supplemental] = replacement
                else:
                    target = min(range(len(out)), key=lambda i: len(out[i]["intent_ids"]))
                    out[target]["intent_ids"].append(topic["id"])
                    out[target]["sub_queries"] = list(dict.fromkeys(
                        list(out[target].get("sub_queries") or [])
                        + list(topic["retrieval_queries"])
                    ))[:4]
            covered.add(topic["id"])

        for section in out:
            ids = section.get("intent_ids") or []
            section["intent_questions"] = [known[item]["question"] for item in ids]
            rows = [coverage_by_id[item] for item in ids if item in coverage_by_id]
            section["coverage"] = {
                key: sum(int(row.get(key, 0)) for row in rows)
                for key in ("hits", "base_hits", "element_hits", "source_hits")
            }
            # Replicate the small catalog so user removal/reordering cannot erase the
            # final editor's knowledge of an originally mandatory topic.
            section["intent_catalog"] = catalog
            section["intent_contract"] = intent_contract
        return out

    def _judge_sufficiency(self, question, sections, probe) -> List[dict]:
        from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
        by_title = {p["title"]: p for p in probe}
        # 缺省:按探针命中给保守判定(Judge 失败也有充分性信号)
        for s in sections:
            h = by_title.get(s["title"], {"hits": 0, "base_hits": 0,
                                         "element_hits": 0, "source_hits": 0})
            total = h["hits"] + h.get("element_hits", 0)
            s["coverage"] = {key: int(h.get(key, 0)) for key in
                             ("hits", "base_hits", "element_hits", "source_hits")}
            s.setdefault("sufficiency", "充足" if total >= 3 else "薄弱" if total else "缺失")
            s.setdefault("gap_note", "")
            s.setdefault("action", "keep" if total >= 3 else "supplement" if total else "external")
        try:
            block = "\n".join(
                f"- {p['title']}: hits={p['hits']} base_hits={p['base_hits']} "
                f"element_hits={p.get('element_hits', 0)} source_hits={p.get('source_hits', 0)}"
                for p in probe
            )
            raw = self.dependencies.model_clients.chat("report_sufficiency").chat_json(
                [{"role": "user", "content": report_sufficiency_prompt(question, block)}],
                REPORT_SUFFICIENCY_SCHEMA_HINT, cancel_event=self.cancel_event)
            for v in (json.loads(raw).get("verdicts") or []):
                for s in sections:
                    if s["title"] == str(v.get("title", "")).strip():
                        if v.get("sufficiency"): s["sufficiency"] = str(v["sufficiency"])
                        if v.get("gap_note") is not None: s["gap_note"] = str(v.get("gap_note", ""))
                        if v.get("action"): s["action"] = str(v["action"])
        except AskCancelled:
            raise
        except Exception:
            pass
        return sections

    # --- Stage B(单节):完整 reasoning 深挖 ---
    def _deep_dive(self, notebook_id, section, question, depth=None, on_step=None):
        # 经模块属性取 ReasoningRetriever(冻结的测试替换位),端口化构造:
        # 深挖拿到的就是本引擎依赖里的同一批 retrieval/model/communities 端口。
        from app.services.reasoning_retrieval import ReasoningRetriever
        deps = self.dependencies
        intent_questions = list(section.get("intent_questions") or [])
        sec_question = (
            f"{question}\n[报告章节] {section['title']}: {section['scope']}\n"
            f"[本节必须回答] {'; '.join(intent_questions) or section['scope']}\n"
            f"[本节检索方向] " + "; ".join(section.get("sub_queries") or [section["title"]])
        )
        # 与 ask 走同一套流程:不传 top_n → run 按本节方面数自适应证据预算
        # (effective_top_n:floor=retrieval_top_n,横向对比节因兄弟子查询多而扩容)。
        # `limits` 按研究深度选档(PR-5):档名与 Ask 对齐,并让穷尽档在每节深挖里
        # 自动激活大纲便签与弱支撑边回喂(`outline_wiring_active` 判的就是它)——
        # 报告引擎因此不 import 任何大纲内部件。集合枚举保持不可达:构造 retriever
        # 时不传 collection_catalog/collection_enumeration,wiring 判空自动关闭。
        limits = report_retrieval_limits(depth)
        result = ReasoningRetriever(
            retrieval=deps.retrieval,
            model_clients=deps.model_clients,
            communities=deps.communities,
            settings=self.settings,
            cancel_event=self.cancel_event,
        ).run(notebook_id, sec_question, on_step=on_step, max_steps=depth,
              limits=limits)

        # The outline's approved retrieval directions are execution requirements,
        # not merely prose hints to the reasoning planner.  Merge their bounded
        # direct KG/element results so replanning cannot silently drop one.
        seen_objects = {hit.object_id for hit in result.top_hits}
        seen_elements = {item.element_id for item in result.elements}
        # 大纲绑上、却被最终相关性选集挤出 top_hits 的知识对象(仅穷尽档非空,见
        # reasoning_retrieval.outline_truncated_kg_evidence)。规则同 ask_service 的
        # 按节合成路径:它们是模型自己指名要的结构支撑,要能进本节上下文并计入
        # classify_evidence 的证据池 —— 否则「发现的结构」里指向它们的 [k] 解析不
        # 出来,而引用了它们的句子在分级眼里就是引了个不存在的东西。相关度已在
        # retriever 侧与选集同口径夹好,这里直接并入,零新查询。
        # 注意这里**只**做「在池子里」:它们的相关度低于选集,尾插的位置在大候选池
        # 下必然被上下文预算截掉。真正让它们进得了 prompt 的是 `_draft_section` 的
        # 两遍渲染(`knowledge_context_with_outline` 的子预算),不是这个 append。
        for hit in list(getattr(result, "outline_evidence", None) or []):
            if hit.object_id in seen_objects:
                continue
            result.top_hits.append(hit)
            seen_objects.add(hit.object_id)
        for query in list(section.get("sub_queries") or [])[:4]:
            new_count = 0
            try:
                hits = self.dependencies.retrieval.federated_retrieve(
                    notebook_id, str(query)
                )[: self.settings.retrieval_top_n]
                for hit in hits:
                    if hit.object_id not in seen_objects:
                        result.top_hits.append(hit)
                        seen_objects.add(hit.object_id)
                        new_count += 1
            except Exception:
                pass
            try:
                elements = self.dependencies.retrieval.retrieve_elements(
                    notebook_id, str(query), limit=8
                )
                for element in elements:
                    if element.element_id not in seen_elements:
                        result.elements.append(element)
                        seen_elements.add(element.element_id)
                        new_count += 1
            except Exception:
                pass
            if not any(str(row.get("query") or "") == str(query)
                       for row in result.attempted):
                result.attempted.append({"query": str(query), "new": new_count, "tries": 1})
        # 方向合并绕过了档位的最终选集上限 —— 每个方向照常执行(账目里的 `new` 记的
        # 是它真的找到多少),合并后再统一压回上限。
        clamp_merged_evidence(result, limits)
        return result

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result,
                       depth=None) -> dict:
        from app.services.prompts import report_section_prompt, REPORT_SECTION_SCHEMA_HINT
        deps = self.dependencies
        # 撰写上下文的预算同样随研究深度走(codex PR#418 R1 P2-2):档位贯通此前只
        # 到检索为止,而「概览」和「穷尽」此前拿到的是同一份 6000/20000 字符上下文
        # —— 检索按档缩放、装配却是定值,等于把档位的一半买空。`depth=None` 的调用方
        # (直接调 `_draft_section` 的测试与冻结调用点)保持旧固定值。
        limits = report_retrieval_limits(depth)
        chunk_budget = (limits.chunk_context_chars if limits is not None
                        else self.settings.report_section_chunk_budget)
        kg_budget = (limits.kg_context_chars if limits is not None
                     else self.settings.answer_context_budget_chars)
        chunk_block, chunk_map = deps.evidence_context.chunk_context(
            result.chunks, notebook_id=notebook_id, budget_chars=chunk_budget)
        # 本节深挖整理出的子大纲(仅穷尽档非空)。它有两个消费点,顺序不能倒:先决定
        # 谁进 KG 上下文(绑定证据的子预算),再据装配好的 id_map 渲染结构块。
        sub_outline = list(getattr(result, "outline", None) or [])
        kg_block, kg_map = knowledge_context_with_outline(
            deps.evidence_context, notebook_id, result.top_hits, sub_outline,
            id_offset=len(chunk_map), budget_chars=kg_budget)
        # 现场事实:chunk_context/knowledge_context 空输入返回 "(none)" 哨兵
        # (非空串),先归一再拼接,避免把哨兵当真实证据块。
        chunk_block = "" if chunk_block == "(none)" else chunk_block
        kg_block = "" if kg_block == "(none)" else kg_block
        # 直接原文段:条数按档位的 `answer_element_items`,择优规则与 Ask 侧同口径
        # (相关度降序、tie-break element_id)。
        # 字符预算与原文块**共享同一个分区上限**(codex PR#418 R2 P2):
        # `chunk_context_chars` 在 `AskRetrievalLimits` 里就是「结构化预览 + chunk +
        # 直接原文段」这一整个分区的上限,再在它之外加一份 1/3 等于自己宣布了一个
        # 上限又立刻超过它 —— 穷尽档下那是额外 40000 字符。取剩余额度的写法逐字照
        # `_answer_reasoning`(`max(0, chunk_budget - len(source_context))`)。
        # 不选档的调用方保持旧的 `max(2000, …//3)`,那条路径没有分区合同可言。
        elements = list(getattr(result, "elements", []) or [])
        if limits is not None:
            elements = rank_elements(elements, limits.answer_element_items)
        element_budget = (max(0, chunk_budget - len(chunk_block))
                          if limits is not None else max(2000, chunk_budget // 3))
        element_block, element_map = deps.evidence_context.element_context(
            elements,
            notebook_id=notebook_id,
            id_offset=4000,
            budget_chars=element_budget,
        )
        element_block = "" if element_block == "(none)" else element_block
        context_block = (f"{chunk_block}\n\n[Knowledge graph]\n{kg_block}"
                         if chunk_block else kg_block) or "(no evidence retrieved)"
        if element_block:
            context_block = f"{context_block}\n\n[Direct source elements]\n{element_block}"
        chain_map = {}
        if getattr(result, "chains", None):
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_map = render_follow_chain_context(
                result.chains, id_offset=2000, active_notebook_id=notebook_id)
            from app.services.kg.follow_chain import chain_anchor_relevances
            relation_relevances = chain_anchor_relevances(result.chains)
            for value in chain_map.values():
                value["relevance"] = float(
                    relation_relevances.get(value.get("object_id"), 0.0)
                )
            if chain_block and chain_block != "(none)":
                context_block = f"{context_block}\n\n{chain_block}"
        memory_map = {}
        try:
            memories = (
                deps.memory_retriever.notebook_memory_hits(
                    self.user_id, notebook_id,
                    f"{question} {section['title']} {' '.join(section['sub_queries'])}",
                    8,
                )
                if deps.memory_retriever is not None else []
            )
            memory_block, memory_map = (
                deps.memory_retriever.context(memories, id_offset=3000)
                if memories else ("(none)", {})
            )
            if memory_block and memory_block != "(none)":
                context_block = f"{context_block}\n\n[Confirmed Memory]\n{memory_block}"
        except Exception:
            memory_map = {}
        client = deps.model_clients.chat("report_section")
        id_map = {**chunk_map, **kg_map, **chain_map, **memory_map, **element_map}
        # 子大纲 → 有界「发现的结构」块。它只作用于本节内部的 `###` 组织,绝不回写
        # reports.outline_json 里用户确认过的大纲。两次重试共用同一份块:它与
        # id_map 一样在这轮里是常量。
        structure_block = outline_structure_block(sub_outline, id_map)
        # 思考型模型(deepseek-v4-pro)偶发把输出预算耗在 reasoning_content(思维链,被
        # _stream_chat_content 丢弃)上 → content 空 → chat_json 兜底 "{}" → markdown 空
        # (不抛异常)。原先空 markdown 会让本节在 _assemble 里静默消失(无标题/无提示)。
        # 有界重试一次("{}" 不入 LLM 缓存,真·重掷);仍空则标 failed(→渲染「本节生成失败」
        # note,不再静默)+ emit model_error(report_engine 原先零可观测)。章节更长/预算更小
        # (report_section_max_tokens 仅 answer 的一半),故比 ask 更易触发。
        markdown, llm_grounded = "", False
        for _ in range(2):
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": report_section_prompt(
                        section["title"], section["scope"], question, context_block,
                        allow_parametric=self.settings.report_allow_parametric,
                        discovered_structure=structure_block)}],
                    REPORT_SECTION_SCHEMA_HINT, cancel_event=self.cancel_event,
                    **cap_kwargs(client, "report_section_max_tokens"))
                data = json.loads(raw)
                if isinstance(data, dict):
                    markdown = str(data.get("markdown", "")).strip()
                    llm_grounded = bool(data.get("grounded", False))
            except AskCancelled:
                raise
            except Exception:
                markdown, llm_grounded = "", False
            if markdown:
                break
        anchors = deps.evidence_context.parse_anchors(markdown, id_map) if markdown else []
        from types import SimpleNamespace
        from app.services.retrieval import classify_evidence
        evidence_pool = [SimpleNamespace(
            object_id=str(value.get("object_id") or ""),
            relevance=float(value.get("relevance", 0.0) or 0.0),
        ) for value in id_map.values() if value.get("object_id")]
        evidence_level, top_relevance = classify_evidence(
            evidence_pool, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high,
        )
        base = {"title": section["title"], "scope": section["scope"],
                "markdown": markdown, "grounded": evidence_level == "grounded",
                "evidence_level": evidence_level, "top_relevance": top_relevance,
                "intent_ids": list(section.get("intent_ids") or []),
                "id_map": id_map,      # 节内 k -> ctx;仅供 _assemble 全局重编号,不入库
                "attempted": list(getattr(result, "attempted", []) or [])}
        if not markdown:
            try:
                deps.model_errors.note_model_error(
                    "report_section",
                    RuntimeError(
                        f"report section '{section['title']}' produced empty content after retry "
                        "(reasoning model likely spent output budget on discarded chain-of-thought)"
                    ),
                    workload_id="report_section",
                )
            except Exception:
                # Observability must not become a second failure channel after
                # the model request has already degraded to an empty section.
                pass
            base["failed"] = True
            base["error"] = "答案合成未产出内容(模型可能把输出预算耗在思维链上),已重试"
        return base

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id, rid, outline, question, depth):
        status = [{"title": s["title"], "phase": "排队", "step": 0} for s in outline]
        lock = threading.Lock()
        last = [0.0]

        def persist(force=False):
            now = time.monotonic()
            # 取快照与落库必须同在锁内:放到锁外会让快照顺序 ≠ 落库顺序 —— 先取
            # 快照的线程可能后写,用陈旧快照盖掉别节刚落库的完成态;而
            # _run_sections 之后再没人写 section_status,这份陈旧快照会永久留库
            # (报告已完成,进度视图却停在「规划」/「深挖」)。写被串行化,但
            # 非强制写有 2 秒节流、强制写每节仅 3 次,且都远短于节内 LLM 调用。
            with lock:
                if not force and now - last[0] < 2.0:
                    return
                last[0] = now
                snap = [dict(x) for x in status]
                done = sum(1 for x in snap if x["phase"] in ("完成", "失败"))
                running = sum(1 for x in snap if x["phase"] not in ("排队", "完成", "失败"))
                self.dependencies.reports.update_report(
                    notebook_id, rid, section_status=snap,
                    progress=f"章节 {done}/{len(outline)} 完成 · {running} 进行中")

        _PHASE = {"plan": "规划", "reflect": "深挖", "retrieve": "深挖", "expand": "深挖",
                  "ppr": "深挖", "follow_chain": "深挖", "fallback": "深挖"}
        # 本节深挖到目前为止整理出的大纲节数(仅穷尽档会非零)。深挖阶段的 phase
        # 文案据此细化;写入仍走既有的 2 秒节流 persist,不新增落库次数。
        outline_sections = [0] * len(outline)

        def _deep_phase(index):
            count = outline_sections[index]
            return f"深挖中（已整理大纲 {count} 节）" if count else "深挖"

        def _one(i, section):
            raise_if_cancelled(self.cancel_event)
            with lock:
                status[i]["phase"] = "规划"
            persist(force=True)

            def on_step(step, _i=i):
                with lock:
                    if step.step_type == "outline":
                        # 大纲步没有自己的阶段(它发生在深挖里),但它是这一节
                        # 唯一能看出「模型在整理结构」的时刻:立刻更新计数并改写
                        # 文案,不等下一个检索步来带 —— 大纲步完全可能是本节反思
                        # 循环的最后一个动作。
                        outline_sections[_i] = len(
                            (getattr(step, "detail", None) or {}).get("sections") or [])
                        status[_i]["phase"] = _deep_phase(_i)
                    ph = _PHASE.get(step.step_type)
                    if ph:
                        status[_i]["phase"] = _deep_phase(_i) if ph == "深挖" else ph
                    if step.step_type == "reflect":
                        status[_i]["step"] += 1
                persist()

            try:
                result = self._deep_dive(notebook_id, section, question, depth, on_step)
                with lock:
                    status[i]["phase"] = "撰写"
                persist(force=True)
                drafted = self._draft_section(notebook_id, section, question, result,
                                              depth)
                with lock:
                    status[i]["phase"] = "完成"
            except AskCancelled:
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                raise
            except Exception as exc:
                drafted = {"title": section["title"], "scope": section["scope"],
                           "markdown": "", "grounded": False, "failed": True,
                           "error": str(exc)[:300], "id_map": {},
                           "attempted": []}
                with lock:
                    status[i]["phase"] = "失败"
            persist(force=True)
            return drafted

        # One configured service owns capacity.  Do not create a second report
        # concurrency knob that can exceed the scheduler's physical limit.
        workers = max(
            1,
            min(
                len(outline),
                self.dependencies.model_clients.parallelism("report_section"),
            ),
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _one, i, s)
                       for i, s in enumerate(outline)]
            return [f.result() for f in futures]

    # --- 入口:Stage B/C/D(生成阶段)——读 outline_json → 深挖 → 汇总 → done ---
    def generate(self, notebook_id, rid, question, depth: int = 2) -> None:
        reports = self.dependencies.reports
        try:
            d = reports.get_report(notebook_id, rid)
            outline = d.get("outline") or []
            understanding = d.get("understanding") or {}
            display_question = str(
                understanding.get("resolved_question") or question
            ).strip()
            research_question = self._confirmed_research_question(
                understanding, question
            )
            if not outline:
                reports.update_report(notebook_id, rid, status="failed",
                                      error="no outline to generate", progress="无大纲")
                return
            reports.update_report(notebook_id, rid, status="generating",
                                  progress=f"章节 0/{len(outline)} 完成")
            sections = self._run_sections(
                notebook_id, rid, outline, research_question, depth
            )
            # 中间只写 progress:此刻 sections 仍含 id_map 账目,不落库。
            reports.update_report(notebook_id, rid, progress="汇总中")
            content_md, gaps, references = self._assemble(
                notebook_id, rid, research_question, outline, sections,
                display_question=display_question,
            )
            for s in sections:
                s.pop("id_map", None)          # 账目仅供 assemble,不入库
            reports.update_report(notebook_id, rid, sections=sections,
                                  content_md=content_md, gaps=gaps,
                                  references=references, status="done", progress="完成")
        except AskCancelled:
            reports.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            reports.update_report(notebook_id, rid, status="failed",
                                  error=str(exc)[:500], progress="失败")

    # --- 编排:规划(→outline_ready)+(auto_generate 时)生成,保留一键直出 ---
    def run(self, notebook_id, rid, question, history="", depth: int = 2,
            auto_generate: bool = False, intent_contract=None,
            require_intent_review: bool = False) -> None:
        if require_intent_review and intent_contract is None:
            self.prepare_intent(
                notebook_id, rid, question, history,
                auto_generate=auto_generate,
            )
            return
        self.plan_outline(
            notebook_id, rid, question, history,
            intent_contract=intent_contract,
        )
        if not auto_generate:
            return
        if self.dependencies.reports.get_report(notebook_id, rid).get("status") == "outline_ready":
            self.generate(notebook_id, rid, question, depth)

    # --- Stage D:汇总——执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
    def _assemble(self, notebook_id, rid, question, outline, sections, *,
                  display_question: str | None = None):
        from app.services.prompts import (
            report_summary_prompt, REPORT_SUMMARY_SCHEMA_HINT,
        )
        intent_contract = next(
            (dict(section.get("intent_contract") or {}) for section in outline
             if section.get("intent_contract")),
            {},
        )
        intent_catalog = list(intent_contract.get("mandatory_topics") or []) or next(
            (list(section.get("intent_catalog") or []) for section in outline
             if section.get("intent_catalog")),
            [],
        )
        intent_block = json.dumps(
            intent_contract or {"objective": question, "mandatory_topics": intent_catalog},
            ensure_ascii=False,
        )
        editor_coverage: Dict[str, dict] = {}
        contradictions: List[str] = []
        # 执行摘要 + 只读覆盖/冲突审校(容错:失败不拖垮报告,也不重写正文)。
        summary = ""
        try:
            sections_block = "\n\n".join(
                s["markdown"][:2000] for s in sections if s.get("markdown"))
            raw = self.dependencies.model_clients.chat("report_summary").chat_json(
                [{"role": "user", "content": report_summary_prompt(
                    question, sections_block, intent_block=intent_block
                )}],
                REPORT_SUMMARY_SCHEMA_HINT, cancel_event=self.cancel_event)
            data = json.loads(raw)
            summary = str(data.get("summary", "")).strip()
            known_intents = {str(item.get("id") or "") for item in intent_catalog}
            for row in (data.get("coverage") or []):
                if not isinstance(row, dict):
                    continue
                intent_id = str(row.get("intent_id") or "")
                if intent_id in known_intents and isinstance(row.get("covered"), bool):
                    editor_coverage[intent_id] = {
                        "covered": row.get("covered") is not False,
                        "note": str(row.get("note") or "").strip()[:300],
                    }
            contradictions = [
                str(item).strip()[:300] for item in (data.get("contradictions") or [])
                if str(item).strip()
            ][:8]
        except AskCancelled:
            raise
        except Exception:
            pass

        # --- 全局引用重编号(按具体证据锚点去重,不再把同源不同元素折叠) ---
        references: List[dict] = []
        ref_pos: Dict[str, int] = {}       # dedup key -> 全局 1-based
        citation_source_info = self.dependencies.evidence_context.citation_source_info(
            str(ctx.get("source_id") or "")
            for section in sections
            for ctx in (section.get("id_map") or {}).values()
        )

        def _source_title(ctx):
            source_id = str(ctx.get("source_id") or "")
            return (citation_source_info.get(source_id) or {}).get(
                "title", str(ctx.get("source_title") or "").strip()
            )

        def _source_file_name(ctx):
            source_id = str(ctx.get("source_id") or "")
            return (citation_source_info.get(source_id) or {}).get(
                "file_name", str(ctx.get("source_file_name") or "").strip()
            )

        def _dk(ctx):
            object_type = str(ctx.get("object_type") or "evidence")
            object_id = str(ctx.get("object_id") or "")
            if object_id:
                return f"{object_type}:{object_id}"
            element_id = str(ctx.get("element_id") or "")
            if element_id:
                return f"element:{element_id}"
            return "|".join((
                object_type,
                str(ctx.get("source_id") or ""),
                str(ctx.get("location_label") or ""),
                str(ctx.get("snippet") or "")[:120],
            ))

        def _label(ctx):
            if str(ctx.get("object_type") or "") == "relation":
                source = _source_title(ctx)
                relation = str(ctx.get("name") or ctx.get("object_id") or "").strip()
                return f"{source} · {relation}" if source and relation else (
                    source or relation or "(unnamed)")
            return (_source_title(ctx) or str(ctx.get("name")
                                              or ctx.get("object_id") or "").strip()
                    or "(unnamed)")

        remapped: Dict[int, str] = {}
        for si, s in enumerate(sections):
            id_map = s.get("id_map") or {}

            def _sub(m, _id_map=id_map):
                # 支持单 key [k1] 与逗号复合 [k1, k3](LLM 常不按 [k1][k3] 而吐逗号):
                # 逐 key 重映射到全局、bracket 内去重;全未知则整段剥除(幻觉/未知 marker)。
                # 复合 marker 是一个证据组：先完整验证，再产生任何全局编号副作用。
                local_keys = [_raw.strip() for _raw in m.group(1).split(",")]
                contexts = [_id_map.get(key) for key in local_keys]
                if any(not ctx for ctx in contexts):
                    return ""

                out_keys: List[str] = []
                for ctx in contexts:
                    dk = _dk(ctx)
                    if dk not in ref_pos:
                        ref_pos[dk] = len(references) + 1
                        references.append({
                            "key": f"k{ref_pos[dk]}",
                            "object_id": str(ctx.get("object_id") or ""),
                            "object_type": str(ctx.get("object_type") or ""),
                            "label": _label(ctx),
                            "name": str(ctx.get("name") or ""),
                            "source_title": _source_title(ctx),
                            "source_file_name": _source_file_name(ctx),
                            "location_label": str(ctx.get("location_label") or ""),
                            "source_id": str(ctx.get("source_id") or ""),
                            "element_id": str(ctx.get("element_id") or ""),
                            "snippet": str(ctx.get("snippet") or ""),
                            "tier": str(ctx.get("tier") or "personal"),
                            "provenance": dict(ctx.get("provenance") or {}),
                        })
                    _gk = f"k{ref_pos[dk]}"
                    if _gk not in out_keys:
                        out_keys.append(_gk)
                return ("[" + ", ".join(out_keys) + "]") if out_keys else ""

            remapped[si] = _MARKER.sub(_sub, s.get("markdown") or "")

        # --- 覆盖度信号:库内证据不足 + 必答主题缺口 + 跨节矛盾。 ---
        # 报告体例要求正文不堆砌诊断,这些信号统一落在结尾局限与 gaps 数据。
        # 已移除:①干涸子查询罗列(暴露英文子查询,内部机制)②跨节概念对连通性
        # 检查(大库 _retrieve_neighbors 开销 + claim 文本被当概念名 → 大面积噪音)。
        weak = [s["title"] for s in sections
                if s.get("markdown") and not s.get("grounded")]
        gaps = [f"「{t}」库内证据不足,内容偏推断/通识" for t in weak]
        intent_gaps: List[str] = []
        for intent in intent_catalog:
            intent_id = str(intent.get("id") or "")
            title = str(intent.get("title") or intent.get("question") or intent_id)
            structurally_covered = any(
                intent_id in (section.get("intent_ids") or [])
                and bool(section.get("markdown")) and not section.get("failed")
                for section in sections
            )
            review = editor_coverage.get(intent_id)
            if not structurally_covered:
                intent_gaps.append(f"必答主题「{title}」未形成有效正文")
            elif review and not review["covered"]:
                note = f":{review['note']}" if review["note"] else ""
                intent_gaps.append(f"必答主题「{title}」回答不完整{note}")
        gaps.extend(intent_gaps)
        gaps.extend(f"跨章节可能存在冲突:{item}" for item in contradictions)

        # --- 组装 content_md:执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
        parts = [f"# 深度报告:{display_question or question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        for si, s in enumerate(sections):
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif remapped.get(si):
                parts += [remapped[si], ""]
        if references:
            parts += ["## 参考文献", ""] + [
                f"- [{r['key']}] {r['label']}"
                + (f" · {r['location_label']}" if r["location_label"] else "")
                for r in references] + [""]
        limitations: List[str] = []
        if weak:
            limitations.append(
                f"{'、'.join(weak)} 库内证据有限,相关论述以推断/通识为主"
            )
        if intent_gaps:
            limitations.append(";".join(intent_gaps))
        if contradictions:
            limitations.append("发现需人工复核的跨章节冲突:" + ";".join(contradictions))
        if limitations:
            parts += [
                "> **范围与证据局限**:" + ";".join(limitations)
                + ",建议补充对应语料或调整大纲后重新生成。",
                "",
            ]
        return "\n".join(parts), gaps, references
