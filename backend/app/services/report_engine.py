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
from app.services.report_corpus_profile import (
    PROFILE_FAILED,
    PROFILE_SCOPE_RESTRICTED,
    ReportCorpusProfileService,
    base_reference_source_count,
    corpus_profile_available,
    corpus_profile_planner_block,
    corpus_profile_reader_markdown,
    unavailable_profile,
)
from app.services.report_synthesis import (
    annotate_trend_evidence,
    blueprint_for_section,
    exclusive_frame_conflicts,
    fair_editor_context,
    localize_blueprint_evidence,
    normalize_claim_ledger,
    normalize_report_frame,
    normalize_synthesis_blueprint,
    report_synthesis_requested,
    synthesis_evidence_payload,
)

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.repositories.ports import (
        CommunityQueryPort, EvidenceContextPort, ModelClientProvider,
        ModelErrorSink, ReportRepository, ReportSourceQueryPort, RetrievalPort,
    )

_MARKER = re.compile(r"\[(k\d+(?:\s*,\s*k\d+)*)\]")   # 节内 [k_i] 或 [k_i, k_j] 引用标记(全局重编号用)
_HIGH_RISK_NUMBER = re.compile(
    # Python's ``\w`` includes CJK, so a ``(?<!\w)`` guard silently misses
    # ordinary Chinese prose such as “降低了30%”.  Exclude only ASCII
    # identifier continuations; Chinese characters are valid sentence context —
    # with one exception: ``第`` marks an ordinal (“第3层” names a layer, it does
    # not assert a quantity), so it is excluded both adjacent and across one
    # space (“第 3 层”).  Without this the audit denominator counts structural
    # self-references as high-risk claims, which is exactly the miscalibration
    # the opt-in downgrade flag is waiting on real-run distributions to tune.
    r"(?<![A-Za-z0-9_第])(?<!第\s)\d+(?:[.,]\d+)?\s*(?:%|％|秒|分钟|小时|天|万|亿|参数|倍|层|篇|份|个|"
    r"(?:ms|s|KB|MB|GB|TB|K|M|B|tokens?)(?![A-Za-z]))",
    re.IGNORECASE,
)
_HIGH_RISK_COMPLEXITY = re.compile(r"\bO\s*\([^\n)]{1,80}\)", re.IGNORECASE)
_HIGH_RISK_ASSERTION_CJK = re.compile(
    r"(?:最(?:高|低|快|慢|强|弱|优|差|大|小)|唯一|全部|所有|必然|"
    r"显著(?:高于|低于|优于|劣于)|优于|劣于|高于|低于|排名|排序)"
)
_HIGH_RISK_ASSERTION_EN = re.compile(
    # ``(?<!-)all(?!-)``: the standalone quantifier is high-risk (“all models
    # …”), but ``all-reduce``/``all-to-all`` are operation names, not claims —
    # the lookbehind covers the TRAILING all in ``all-to-all``, which the
    # lookahead alone lets through (codex #427 r1).
    r"\b(?:best|worst|fastest|slowest|(?<!-)all(?!-)|always|never|"
    r"significantly\s+(?:better|worse|higher|lower)|outperform(?:s|ed)?)\b",
    re.IGNORECASE,
)


def audit_high_risk_assertions(markdown: str, id_map: dict[str, dict], *,
                               max_unsupported_ratio: float) -> dict[str, Any]:
    """Audit citation presence for deterministically recognisable risky prose.

    This is intentionally narrower than semantic fact checking: it catches
    numbers, complexity, absolute and ranking assertions, and only credits a
    citation marker in the same sentence/Markdown table row whose keys all bind
    to the writer's evidence map.
    """
    assertions: list[str] = []
    unsupported_samples: list[str] = []
    supported = 0
    in_code = False
    for raw_line in str(markdown or "").splitlines():
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            in_code = not in_code
            continue
        if (
            not line or in_code or line.startswith("#")
            or line.startswith("$$") or line.startswith("\\[")
            or "（推断）" in line or "【通识】" in line
            or re.fullmatch(r"[-:|\s]+", line)
        ):
            continue
        # Preserve a Markdown table row as one assertion unit; prose uses
        # punctuation boundaries so a citation on the next sentence cannot
        # accidentally support the previous one.
        units = (
            [line]
            if line.startswith("|")
            else re.split(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+", line)
        )
        for sentence in units:
            sentence = sentence.strip()
            if not sentence:
                continue
            without_markers = _MARKER.sub("", sentence)
            if not (
                _HIGH_RISK_COMPLEXITY.search(without_markers)
                or _HIGH_RISK_NUMBER.search(without_markers)
                or _HIGH_RISK_ASSERTION_CJK.search(without_markers)
                or _HIGH_RISK_ASSERTION_EN.search(without_markers)
            ):
                continue
            assertions.append(sentence[:500])
            markers = _MARKER.findall(sentence)
            is_supported = False
            if markers:
                keys = [key.strip() for group in markers for key in group.split(",")]
                if keys and all(key in id_map for key in keys):
                    supported += 1
                    is_supported = True
            if not is_supported and len(unsupported_samples) < 5:
                unsupported_samples.append(sentence[:500])
    total = len(assertions)
    unsupported = total - supported
    ratio = unsupported / total if total else 0.0
    threshold = max(0.0, min(1.0, float(max_unsupported_ratio)))
    return {
        "high_risk_assertions": total,
        "supported": supported,
        "unsupported": unsupported,
        "support_rate": (supported / total if total else 1.0),
        "unsupported_ratio": ratio,
        "threshold": threshold,
        "threshold_exceeded": bool(total and ratio > threshold),
        "downgrade_applied": False,
        "unsupported_samples": unsupported_samples,
    }


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

    **两种「绑定」待遇不同**(codex PR#418 R5 P2):

    * `outline_evidence` 那一批是**上限之外**的补集 —— retriever 已经把它们判在选集
      之外了(相关度被刻意夹到选集最低分以下,见 `outline_truncated_kg_evidence`),
      让它们参与同一次排序等于把它们再删一次。Ask 侧同样把它们放在 `top_hits` 之外
      (`outline_pool_extra` 直接进分类池),所以这是同一条规则,不是报告的例外。
    * 已经在选集里、又被大纲绑上的对象**照常占用上限席位**,只是**优先占**:把它们
      整体移出分母的话,「96 条选集 + 8 条绑定 + 8 条方向补检索」会得到 104 条而
      clamp 什么都不做 —— 上限就成了摆设。优先占位保证绑定不会被方向补检索挤掉,
      同时总数仍守在 `cap + len(outline_evidence)`。

    元素同理:绑定的元素先占 `answer_element_items` 的席位,其余按 `(-score, id)` 补满。
    """
    if limits is None:
        return
    # 「大纲绑定」= 子大纲各节绑定的候选键 ∪ retriever 带出的截断补集。前者是必须
    # 的:绑定键横跨知识对象/元素/原文段三个 id 空间,只看后者(它只装知识对象)会
    # 让绑定的**元素**照常被条数上限截掉 —— 那一条子话题的 [k] 同样解析不出来
    # (codex PR#418 R3 P1)。三个 id 空间互不相交,一个集合足够。
    bound = outline_bound_evidence_keys(getattr(result, "outline", None) or ())
    supplemental = {
        str(getattr(hit, "object_id", "") or "")
        for hit in (getattr(result, "outline_evidence", None) or [])
    }
    bound |= supplemental

    def _object_id(hit) -> str:
        return str(getattr(hit, "object_id", "") or "")

    within = [hit for hit in result.top_hits if _object_id(hit) not in supplemental]
    if len(within) > limits.ranked_final_cap:
        bound_within = [hit for hit in within if _object_id(hit) in bound]
        others = [hit for hit in within if _object_id(hit) not in bound]
        room = max(0, limits.ranked_final_cap - len(bound_within))
        keep = {_object_id(hit) for hit in bound_within}
        keep |= {
            _object_id(hit) for hit in sorted(
                others, key=lambda hit: -float(getattr(hit, "relevance", 0.0) or 0.0)
            )[:room]
        }
        result.top_hits = sorted(
            [hit for hit in within if _object_id(hit) in keep],
            key=lambda hit: -float(getattr(hit, "relevance", 0.0) or 0.0),
        ) + [hit for hit in result.top_hits if _object_id(hit) in supplemental]

    def _element_id(element) -> str:
        return str(getattr(element, "element_id", "") or "")

    if len(result.elements) > limits.answer_element_items:
        # 绑定的元素**优先占**席位,但上限是**闭**的(codex PR#418 R6 P2):一份大纲
        # 最多能绑 96 个键,而穷尽档的元素上限是 16 —— 全部豁免就等于宣布了一个
        # 「至多 16 条」又送进去几十条。绑定内部同样按 (-score, id) 择优。
        bound_elements = rank_elements(
            [element for element in result.elements if _element_id(element) in bound],
            limits.answer_element_items,
        )
        others = [
            element for element in result.elements if _element_id(element) not in bound
        ]
        room = max(0, limits.answer_element_items - len(bound_elements))
        result.elements = bound_elements + rank_elements(others, room)


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
    corpus_profile: Any = None
    generation_gate: Any = None
    selected_source_graph: Any = None
    scale_version: Any = None
    selected_graph_hydrate: Any = None


class ReportEngine:
    def __init__(self, dependencies: ReportEngineDependencies, *,
                 user_id: str, cancel_event: CancelEvent = None):
        self.dependencies = dependencies
        self.settings = dependencies.settings
        self.user_id = user_id            # 发起者身份(审计归属;模型解析走 ContextVar)
        self.cancel_event = cancel_event

    def _activate_selected_source_graph(self, notebook_id: str, result: Any) -> None:
        service = self.dependencies.selected_source_graph
        if service is None:
            return
        baseline = list(getattr(result, "chunks", None) or [])
        object_seeds = {
            str(hit.object_id): float(getattr(hit, "relevance", 0.0) or 0.0)
            for hit in (getattr(result, "top_hits", None) or [])
            if str(getattr(hit, "object_id", "") or "")
        }
        chunk_seeds = {
            str(chunk.chunk_id): float(getattr(chunk, "relevance", 0.0) or 0.0)
            for chunk in baseline
            if str(getattr(chunk, "chunk_id", "") or "")
        }

        try:
            activated = service.run(
                notebook_id,
                baseline,
                object_seeds=object_seeds,
                chunk_seeds=chunk_seeds,
                source_titles=self.dependencies.source_query.source_titles,
                hydrate_chunk_ids=self.dependencies.selected_graph_hydrate,
                parent_version=(
                    (lambda: self.dependencies.scale_version(notebook_id))
                    if self.dependencies.scale_version is not None else None
                ),
                max_results=self.settings.ppr_top_chunks,
                unsafe_scope_drift=lambda: bool(
                    getattr(
                        self.dependencies.retrieval,
                        "unsafe_source_scope_restricted",
                        lambda _nb: False,
                    )(notebook_id)
                ),
            )
        except Exception:
            # Report retrieval has already frozen B.  Graph/version/scope I/O
            # may only degrade the optional enrichment lane.
            activated = service.fail_closed(
                notebook_id, baseline, "activation_seam_failed"
            )
        if activated.status.state == "historical":
            return
        result.baseline_chunks = baseline
        result.chunks = list(activated.chunks)
        result.source_graph_status = activated.status
        if activated.status is not None:
            from app.models.ask import TraceStep
            result.trace.append(TraceStep(
                step_type="source_subgraph",
                summary=(
                    f"来源子图：{activated.status.state}，"
                    f"新增 {activated.status.enrichment_count} 条证据"
                ),
                detail=activated.status.public_dict(),
            ))

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
        """Freeze user semantics through the shared query-intent parser."""
        from app.services.query_intent import finalize_query_intent, plan_query_intent

        contract = plan_query_intent(
            self.dependencies.model_clients.chat("report_outline"),
            question,
            history,
            max_topics=self.settings.report_max_sections,
            purpose="deep report",
            cancel_event=self.cancel_event,
        )
        if not confirmation_mode:
            return contract
        # Legacy direct-generation callers have already confirmed outside the
        # two-stage UI.  Keep the shared normalisation/scope result but suppress
        # a second clarification gate, matching the historical route behaviour.
        legacy = dict(contract)
        legacy.update(ambiguities=[], needs_clarification=False)
        return finalize_query_intent(legacy)

    def prepare_intent(self, notebook_id: str, rid: str, question: str,
                       history: str = "", *, auto_generate: bool = False,
                       source_scope=None, base_scope=None) -> None:
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
            # The freshly planned contract REPLACES understanding_json wholesale
            # (report_store.update_report writes `understanding_json = ?`, it
            # does not merge), so every scope dimension the create endpoint
            # persisted must be re-attached here or it is silently lost at
            # intent_ready -- after which confirm/generate read None and every
            # mounted reference library participates again regardless of the
            # user's checkboxes.
            if source_scope is None:
                from app.services.source_scope import current_source_scope_payload

                source_scope = current_source_scope_payload()
            if source_scope is not None:
                contract["source_scope"] = dict(source_scope)
            if base_scope is None:
                from app.services.source_scope import current_base_scope_payload

                base_scope = current_base_scope_payload()
            if base_scope is not None:
                contract["base_scope"] = dict(base_scope)
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
        from app.services.query_intent import finalize_query_intent

        confirmed_input = seed.get("confirmed_input") or {}
        return finalize_query_intent(
            seed,
            resolved_question=str(confirmed_input.get("resolved_question") or ""),
            answers=confirmed_input.get("answers") or [],
        )

    @staticmethod
    def _confirmed_research_question(intent_contract: dict, fallback: str) -> str:
        """Build the shared authoritative query without changing the visible title."""
        from app.services.query_intent import confirmed_research_question

        return confirmed_research_question(
            intent_contract, fallback, include_assumptions=False
        )

    def _resolve_source_families(self, source_ids: Sequence[str]) -> dict:
        service = self.dependencies.corpus_profile or ReportCorpusProfileService(
            self.dependencies.source_query
        )
        return service.resolve_families(source_ids)

    def _probe_queries(self, notebook_id: str, queries: List[str]) -> dict:
        seen, base, elements, sources = set(), set(), set(), set()
        # One support unit is one distinct relevant KG object or source element.
        # Repeated retrieval of the same object through multiple sub-queries does
        # not inflate either relevant_items or a family's numerator.
        relevant_units: Dict[str, set[str]] = {}

        for query in queries[:4]:
            try:
                for hit in self.dependencies.retrieval.federated_retrieve(
                    notebook_id, str(query)
                ):
                    unit = f"knowledge:{hit.object_id}"
                    seen.add(hit.object_id)
                    if getattr(hit, "tier", "") == "base":
                        base.add(hit.object_id)
                    relevant = float(
                        getattr(hit, "relevance", 0.0) or 0.0
                    ) >= float(self.settings.evidence_tau_low)
                    unit_sources = {
                        str(getattr(evidence, "source_id", "") or "")
                        for evidence in (getattr(hit, "evidence", None) or [])
                        if str(getattr(evidence, "source_id", "") or "")
                    }
                    sources.update(unit_sources)
                    if relevant:
                        relevant_units.setdefault(unit, set()).update(unit_sources)
            except Exception:
                pass
            try:
                for element in self.dependencies.retrieval.retrieve_elements(
                    notebook_id, str(query), limit=8
                ):
                    unit = f"element:{element.element_id}"
                    elements.add(element.element_id)
                    relevant = float(
                        getattr(element, "score", 0.0) or 0.0
                    ) >= float(
                        self.settings.evidence_tau_low
                    )
                    source_id = str(element.source_id or "")
                    if source_id:
                        sources.add(source_id)
                    if relevant:
                        relevant_units.setdefault(unit, set()).update(
                            [source_id] if source_id else []
                        )
            except Exception:
                pass
        relevant_source_ids = sorted({
            source_id for unit_sources in relevant_units.values()
            for source_id in unit_sources
        })
        try:
            resolution = self._resolve_source_families(relevant_source_ids)
        except Exception:
            resolution = {
                "family_by_source": {},
                "uncertain_source_ids": relevant_source_ids,
                "unresolved_source_ids": relevant_source_ids,
            }
        family_by_source = dict(resolution.get("family_by_source") or {})
        uncertain = set(resolution.get("uncertain_source_ids") or [])
        unresolved = set(resolution.get("unresolved_source_ids") or [])
        # Identity is deliberately handled on two distinct axes:
        #
        # * resolved families may contribute independent support;
        # * an unresolved identity must never pretend to be independent, but
        #   must also never dilute a possible single-family concentration.
        #
        # Treat every relevant unit with at least one unresolved source as a
        # possible extra support for the largest resolved family.  This yields
        # a conservative Top-1 upper bound rather than an attractive-but-false
        # diversity score from an ``identity:unverified`` denominator bucket.
        family_support: Dict[str, int] = {}
        unknown_supports = 0
        unidentified_source_ids: set[str] = set(uncertain | unresolved)
        for unit_sources in relevant_units.values():
            unit_families = set()
            unit_has_unknown = False
            for source_id in unit_sources:
                family = family_by_source.get(source_id)
                if source_id in uncertain or source_id in unresolved or not family:
                    unit_has_unknown = True
                    unidentified_source_ids.add(source_id)
                else:
                    unit_families.add(family)
            for family in unit_families:
                family_support[family] = family_support.get(family, 0) + 1
            if unit_has_unknown:
                unknown_supports += 1
        relevant_supports = sum(family_support.values()) + unknown_supports
        largest_verified_support = max(family_support.values()) if family_support else 0
        top_family_share = (
            (largest_verified_support + unknown_supports) / relevant_supports
            if relevant_supports else 1.0
        )
        return {
            "hits": len(seen),
            "base_hits": len(base),
            "element_hits": len(elements),
            "source_hits": len(sources),
            "relevant_items": len(relevant_units),
            "relevant_supports": relevant_supports,
            "relevant_family_count": len(family_support),
            "unknown_supports": unknown_supports,
            "top_family_share": top_family_share,
            "source_identity_uncertain": len(unidentified_source_ids),
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

    def _corpus_profile(self, notebook_id: str, *, result_scope: str = "ranked") -> dict:
        service = self.dependencies.corpus_profile or ReportCorpusProfileService(
            self.dependencies.source_query
        )
        return service.build(notebook_id, result_scope=result_scope)

    def _unsafe_source_scope_restricted(self, notebook_id: str) -> bool:
        from app.services.source_scope import source_scope_restricted

        probe = getattr(
            self.dependencies.retrieval,
            "unsafe_source_scope_restricted",
            None,
        )
        return (
            bool(probe(notebook_id))
            if callable(probe) else source_scope_restricted()
        )

    def _build_corpus_map(self, notebook_id: str, question: str,
                          profile: Optional[dict] = None) -> str:
        """0-LLM 语料侦察:来源标题 + federated KG 命中 + PPR chunk 来源·路径。
        给 STORM 规划接地(治盲规划)。任一子步失败静默降级为空段。"""
        deps = self.dependencies
        parts: List[str] = []
        from app.services.source_scope import source_scope_restricted

        unsafe_scope = self._unsafe_source_scope_restricted(notebook_id)
        if not unsafe_scope:
            try:
                active_profile = (
                    profile or getattr(self, "_planning_corpus_profile", None)
                )
                # An unavailable marker is a non-empty dict and would no longer
                # fall through a plain `or` chain, so re-probe explicitly —
                # exactly what a bare `{}` used to do.  Without this the marker
                # reaches the planner block, which renders every count as 0 and
                # feeds a corpus summary that was never measured into planning.
                if not corpus_profile_available(active_profile):
                    active_profile = self._corpus_profile(notebook_id)
                # Defensive: `_corpus_profile` either returns a built profile or
                # raises, so this holds today.  It is a cheap invariant on a hook
                # that subclasses and tests replace.
                if corpus_profile_available(active_profile):
                    parts.append(corpus_profile_planner_block(active_profile))
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
        if not unsafe_scope:
            try:
                chunks = deps.retrieval.ppr_retrieve(
                    notebook_id, question
                )[: self._SCOUT_CHUNK_N]
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
                if deps.memory_retriever is not None and not source_scope_restricted()
                else []
            )
            if memories:
                parts.append("用户已确认 Memory:\n" + "\n".join(
                    f"- {item.title}: {item.text[:240]}" for item in memories
                ))
        except Exception:
            pass
        return ("\n\n".join(parts))[:6000] if parts else "(语料侦察无结果)"

    def _probe_sufficiency(self, notebook_id: str, sections: List[dict]) -> List[dict]:
        """0-LLM objective signal over both KG objects and raw SourceElements."""
        out = []
        for s in sections:
            counts = self._probe_queries(
                notebook_id, list(s.get("sub_queries") or [])
            )
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
            try:
                corpus_profile = (
                    # A scoped run has no whole-collection profile to state, and
                    # that is a deliberate skip rather than a failure.  Marking
                    # it keeps the reader disclosure from blaming a breakage.
                    unavailable_profile(PROFILE_SCOPE_RESTRICTED)
                    if self._unsafe_source_scope_restricted(notebook_id)
                    else self._corpus_profile(
                        notebook_id,
                        result_scope=str(
                            intent_contract.get("result_scope") or "ranked"
                        ),
                    )
                )
            except Exception:
                # Profiling is additive governance.  A malformed row/projection
                # must not make the report fail, but its absence must be
                # observable: the reader disclosure already renders the empty
                # profile as unavailable, and operations receives a safe event.
                try:
                    self.dependencies.event_log.emit({
                        "kind": "report_corpus_profile_failed",
                        "notebook_id": notebook_id,
                        "report_id": rid,
                    })
                except Exception:
                    pass
                corpus_profile = unavailable_profile(PROFILE_FAILED)
            intent_contract = dict(intent_contract)
            intent_contract["corpus_profile"] = corpus_profile
            self._planning_corpus_profile = corpus_profile
            self._planning_result_scope = str(
                intent_contract.get("result_scope") or "ranked"
            )
            self._planning_completeness_required = bool(
                intent_contract.get("completeness_required")
            )
            reports.update_report(notebook_id, rid, understanding=intent_contract)
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
            report_frame = next(
                (dict(section.get("report_frame") or {}) for section in sections
                 if section.get("report_frame")),
                normalize_report_frame(intent_contract.get("report_frame")),
            )
            if report_frame:
                intent_contract = dict(intent_contract)
                intent_contract["report_frame"] = report_frame
                reports.update_report(
                    notebook_id, rid, understanding=intent_contract
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
            intent_type = str((intent_contract or {}).get("intent_type") or "")
            frame_shape = (
                intent_type in {"compare", "review"}
                or len((intent_contract or {}).get("entities") or []) >= 2
                or bool((intent_contract or {}).get("comparison_axes"))
            )
            report_frame = (
                normalize_report_frame(data.get("frame")) if frame_shape else None
            )
            out = []
            for s in (data.get("sections") or [])[: self.settings.report_max_sections]:
                title = str(s.get("title", "")).strip()
                subs = [str(q).strip() for q in (s.get("sub_queries") or []) if str(q).strip()]
                if title and subs:
                    section = {
                        "title": title, "scope": str(s.get("scope", "")).strip(),
                        "sub_queries": subs[:4],
                        "intent_ids": [str(item).strip() for item in
                                       (s.get("intent_ids") or []) if str(item).strip()],
                        "perspectives": [str(p).strip() for p in (s.get("perspectives") or []) if str(p).strip()],
                        "tensions": [str(t).strip() for t in (s.get("tensions") or []) if str(t).strip()]}
                    if report_frame:
                        section["report_frame"] = report_frame
                    out.append(section)
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
            if intent_contract.get("report_frame"):
                section["report_frame"] = intent_contract["report_frame"]
        return out

    def _judge_sufficiency(self, question, sections, probe, *,
                           result_scope: str = "ranked",
                           completeness_required: bool = False) -> List[dict]:
        from app.services.prompts import report_sufficiency_prompt, REPORT_SUFFICIENCY_SCHEMA_HINT
        by_title = {p["title"]: p for p in probe}
        result_scope = str(
            getattr(self, "_planning_result_scope", result_scope) or result_scope
        )
        completeness_required = bool(
            getattr(self, "_planning_completeness_required", completeness_required)
        )
        deterministic_by_title: Dict[str, str] = {}
        # Fail-open judge fallback, but fail-closed evidence semantics: volume of
        # graph objects alone is never sufficient.  Require relevant items from
        # multiple independently identifiable document families and reject a
        # heavily single-family distribution.
        for s in sections:
            h = by_title.get(s["title"], {"hits": 0, "base_hits": 0,
                                         "element_hits": 0, "source_hits": 0})
            relevant = int(h.get("relevant_items", 0))
            families = int(h.get("relevant_family_count", 0))
            top_share = float(h.get("top_family_share", 1.0) or 1.0)
            required_families = 3 if completeness_required else 2
            enough = relevant >= 3 and families >= required_families and top_share <= 0.8
            thin = relevant > 0 and families > 0
            fallback = "充足" if enough else "薄弱" if thin else "缺失"
            deterministic_by_title[s["title"]] = fallback
            s["coverage"] = {
                key: (float(h.get(key, 0)) if key == "top_family_share"
                      else int(h.get(key, 0)))
                for key in (
                    "hits", "base_hits", "element_hits", "source_hits",
                    "relevant_items", "relevant_supports", "relevant_family_count",
                    "unknown_supports", "top_family_share", "source_identity_uncertain",
                )
            }
            s.setdefault("sufficiency", fallback)
            s.setdefault("gap_note", "")
            s.setdefault("action", "keep" if enough else "supplement" if thin else "external")
        try:
            block = "\n".join(
                f"- {p['title']}: hits={p['hits']} base_hits={p['base_hits']} "
                f"element_hits={p.get('element_hits', 0)} "
                f"relevant_items={p.get('relevant_items', 0)} "
                f"relevant_supports={p.get('relevant_supports', 0)} "
                f"independent_families={p.get('relevant_family_count', 0)} "
                f"unknown_supports={p.get('unknown_supports', 0)} "
                f"identity_uncertain={p.get('source_identity_uncertain', 0)} "
                f"top_family_share={float(p.get('top_family_share', 1.0)):.3f}"
                for p in probe
            )
            raw = self.dependencies.model_clients.chat("report_sufficiency").chat_json(
                [{"role": "user", "content": report_sufficiency_prompt(
                    question,
                    block,
                    result_scope=result_scope,
                    completeness_required=completeness_required,
                )}],
                REPORT_SUFFICIENCY_SCHEMA_HINT, cancel_event=self.cancel_event)
            for v in (json.loads(raw).get("verdicts") or []):
                for s in sections:
                    if s["title"] == str(v.get("title", "")).strip():
                        verdict = str(v.get("sufficiency") or "")
                        # The judge may call a zero-hit probe "薄弱" (for example
                        # when the planned scope is still partially answerable),
                        # but it cannot manufacture the family diversity required
                        # for "充足".
                        order = {"缺失": 0, "薄弱": 1, "充足": 2}
                        ceiling = deterministic_by_title.get(s["title"], "缺失")
                        if verdict in order and (
                            verdict != "充足" or ceiling == "充足"
                        ):
                            s["sufficiency"] = verdict
                        if v.get("gap_note") is not None:
                            s["gap_note"] = str(v.get("gap_note", ""))
                        if v.get("action"):
                            s["action"] = str(v["action"])
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
        # 每个方向取多少,也按档位走(codex PR#418 R5 P2):方向照常逐条执行,但
        # 「每查询取几条」在档位表里就是 `ranked_per_query_take`(概览 4、穷尽 16),
        # 而这里此前恒取 `retrieval_top_n`(20)。只在合并后截断的话,低档位仍然付了
        # 高档位的取数与 hydration 成本,截断只是把多拿的再扔掉。
        direction_take = (limits.ranked_per_query_take if limits is not None
                          else self.settings.retrieval_top_n)
        direction_elements = (min(8, limits.answer_element_items)
                              if limits is not None else 8)
        for query in list(section.get("sub_queries") or [])[:4]:
            new_count = 0
            try:
                hits = self.dependencies.retrieval.federated_retrieve(
                    notebook_id, str(query)
                )[:direction_take]
                for hit in hits:
                    if hit.object_id not in seen_objects:
                        result.top_hits.append(hit)
                        seen_objects.add(hit.object_id)
                        new_count += 1
            except Exception:
                pass
            try:
                elements = self.dependencies.retrieval.retrieve_elements(
                    notebook_id, str(query), limit=direction_elements
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
        from app.services.retrieval_baseline import merge_baseline_candidates
        prior_baseline_manifest = getattr(result, "baseline_manifest", None)
        prior_candidate_knowledge = (
            prior_baseline_manifest.candidate_knowledge
            if prior_baseline_manifest is not None else ()
        )
        prior_candidate_chunks = (
            prior_baseline_manifest.candidate_chunks
            if prior_baseline_manifest is not None else ()
        )
        prior_candidate_elements = (
            prior_baseline_manifest.candidate_elements
            if prior_baseline_manifest is not None else ()
        )
        # Freeze B_candidates before the final policy clamp.  The clamp owns
        # B_selected; candidates rejected there must remain visible to the
        # no-regression oracle so future enrichment cannot change direct recall
        # or ordering without being detected.
        baseline_candidate_knowledge = merge_baseline_candidates(
            "knowledge", prior_candidate_knowledge, result.top_hits
        )
        baseline_candidate_chunks = merge_baseline_candidates(
            "chunk", prior_candidate_chunks, result.chunks
        )
        baseline_candidate_elements = merge_baseline_candidates(
            "element", prior_candidate_elements, result.elements
        )
        # 方向合并绕过了档位的最终选集上限 —— 每个方向照常执行(账目里的 `new` 记的
        # 是它真的找到多少),合并后再统一压回上限。
        clamp_merged_evidence(result, limits)
        from app.services.retrieval_baseline import (
            build_retrieval_baseline_manifest,
        )
        result.baseline_manifest = build_retrieval_baseline_manifest(
            notebook_id=notebook_id,
            query=sec_question,
            mode="report",
            settings=self.settings,
            candidate_knowledge=baseline_candidate_knowledge,
            candidate_chunks=baseline_candidate_chunks,
            candidate_elements=baseline_candidate_elements,
            prior_manifest=prior_baseline_manifest,
            baseline_step_usage=len(result.trace),
        )
        # B has now been frozen in the manifest. G is appended afterwards and
        # never participates in the historical candidate/selection contract.
        self._activate_selected_source_graph(notebook_id, result)
        return result

    # --- Stage C(单节):撰写 ---
    def _draft_section(self, notebook_id: str, section: dict, question: str, result,
                       depth=None, *, report_frame: Optional[dict] = None,
                       synthesis: Optional[dict] = None,
                       synthesis_requested: bool = False) -> dict:
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
        # 本节深挖整理出的子大纲(仅穷尽档非空)。它有三个消费点,顺序不能倒:先决定
        # 谁进来源分区、再决定谁进 KG 上下文,最后才据装配好的 id_map 渲染结构块。
        sub_outline = list(getattr(result, "outline", None) or [])
        bound_keys = (outline_bound_evidence_keys(sub_outline)
                      if limits is not None else set())
        # 来源分区(chunk + 直接原文段)同样要给大纲绑定留位置(codex PR#418 R4):
        # 它是一份**共享**预算,chunk 按输入序吃满之后,绑在后面的原文段拿到的
        # 就是 0,绑定照样从结构块里消失 —— 那正是前两轮修 KG 侧要防的事。
        # 两件事:绑定的 chunk 排到最前(chunk_context 按输入序渲染,逐 chunk 独立,
        # 重排是安全的);绑定的原文段按它们**自己的实际长度**预留额度(上限仍是
        # 分区的一半,与 KG 侧的子预算同一个比例),chunk 只吃预留之外的部分。
        chunks = list(getattr(result, "chunks", None) or [])
        elements = list(getattr(result, "elements", []) or [])
        bound_elements = [
            element for element in elements
            if str(getattr(element, "element_id", "") or "") in bound_keys
        ]
        if bound_keys:
            chunks = [
                chunk for chunk in chunks
                if str(getattr(chunk, "chunk_id", "") or "") in bound_keys
            ] + [
                chunk for chunk in chunks
                if str(getattr(chunk, "chunk_id", "") or "") not in bound_keys
            ]
        element_reserve = min(
            chunk_budget // _OUTLINE_KG_BUDGET_DIVISOR,
            # 每条 ≈ 正文 + 前缀(`k4001: [source-element][tier] 标题 · 位置 — `)。
            # 估多了只是少给 chunk 几百字符,估少了才会让绑定进不来。
            sum(len(str(getattr(element, "text", "") or "")) + 120
                for element in bound_elements),
        )
        chunk_block, chunk_map = deps.evidence_context.chunk_context(
            chunks, notebook_id=notebook_id,
            budget_chars=max(0, chunk_budget - element_reserve))
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
        # 大纲绑定的元素在这里同样豁免条数上限,并排在最前(codex PR#418 R3 P1):
        # 绑定键横跨三个 id 空间,一个被 `answer_element_items` 截掉的绑定元素,在
        # `outline_structure_block` 里就是一个解析不出的绑定,子话题退成裸标题。
        # 排最前是因为字符预算仍按输入序消耗 —— 与 KG 那边的优先前缀同一个理由。
        if limits is not None:
            # 与 `clamp_merged_evidence` 同一条规则:绑定优先占席位,但上限是闭的。
            kept_bound = rank_elements(bound_elements, limits.answer_element_items)
            others = [
                element for element in elements
                if str(getattr(element, "element_id", "") or "") not in bound_keys
            ]
            elements = kept_bound + rank_elements(
                others, max(0, limits.answer_element_items - len(kept_bound)))
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
        # KG 分区的剩余额度:`kg_context_chars` 在 AskRetrievalLimits 里是「KG 对象/
        # 关系 + confirmed Memory + 查询期推导链」这一整个分区的上限(codex PR#418
        # R3 P2)。推导链与 Memory 因此不再无条件接在 KG 块之后,而是按剩余额度
        # **整块**准入:两者都自带硬上限(Memory 6000 字符封顶、推导链条数由本 run 的
        # follow_chain 次数封顶),所以整块准入不会把常见情形挡在门外;而截半块会把
        # 一行 `[k]` 标记切断,那比少一块更糟。没被准入的块也**不**并进 id_map ——
        # 模型没看见的东西不能算进 classify_evidence 的证据池。
        # 不选档的调用方保持接入前的「无条件追加」。
        kg_room = (max(0, kg_budget - len(kg_block)) if limits is not None else None)

        def _admit_block(block: str, heading: str) -> str:
            """返回要拼进 context_block 的那一段(含前缀);装不下就是空串。"""
            nonlocal kg_room
            if not block or block == "(none)":
                return ""
            prefix = f"\n\n[{heading}]\n" if heading else "\n\n"
            if kg_room is None:
                return prefix + block
            if len(prefix) + len(block) > kg_room:
                return ""
            kg_room -= len(prefix) + len(block)
            return prefix + block

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
            admitted = _admit_block(chain_block, "")
            if admitted:
                context_block = f"{context_block}{admitted}"
            elif limits is not None:
                chain_map = {}
        memory_map = {}
        try:
            from app.services.source_scope import source_scope_restricted

            memories = (
                deps.memory_retriever.notebook_memory_hits(
                    self.user_id, notebook_id,
                    f"{question} {section['title']} {' '.join(section['sub_queries'])}",
                    8,
                )
                if deps.memory_retriever is not None and not source_scope_restricted()
                else []
            )
            memory_block, memory_map = (
                deps.memory_retriever.context(memories, id_offset=3000)
                if memories else ("(none)", {})
            )
            admitted = _admit_block(memory_block, "Confirmed Memory")
            if admitted:
                context_block = f"{context_block}{admitted}"
            elif limits is not None:
                memory_map = {}
        except Exception:
            memory_map = {}
        client = deps.model_clients.chat("report_section")
        id_map = {**chunk_map, **kg_map, **chain_map, **memory_map, **element_map}
        from app.services.retrieval_baseline import build_retrieval_baseline_manifest
        candidate_manifest = getattr(result, "baseline_manifest", None)
        result.baseline_manifest = build_retrieval_baseline_manifest(
            notebook_id=notebook_id,
            query=f"{question}\n{section.get('title') or ''}",
            mode="report",
            settings=self.settings,
            candidate_knowledge=(
                candidate_manifest.candidate_knowledge
                if candidate_manifest is not None else result.top_hits
            ),
            candidate_chunks=(
                candidate_manifest.candidate_chunks
                if candidate_manifest is not None else chunks
            ),
            candidate_elements=(
                candidate_manifest.candidate_elements
                if candidate_manifest is not None else elements
            ),
            selected_knowledge=result.top_hits,
            selected_chunks=(
                getattr(result, "baseline_chunks", None) or chunks
            ),
            selected_elements=elements,
            final_context_block=context_block,
            final_id_map=id_map,
            final_ordered_handles=(
                *tuple(chunk_map),
                *tuple(kg_map),
                *tuple(element_map),
                *tuple(chain_map),
                *tuple(memory_map),
            ),
            final_budget_chars=chunk_budget + kg_budget,
            prior_manifest=candidate_manifest,
            baseline_step_usage=len(getattr(result, "trace", None) or []),
        )
        localized_synthesis = localize_blueprint_evidence(synthesis, id_map)
        synthesis_block = (
            json.dumps(localized_synthesis, ensure_ascii=False)
            if localized_synthesis else ""
        )
        frame_block = (
            json.dumps(report_frame, ensure_ascii=False) if report_frame else ""
        )
        # 子大纲 → 有界「发现的结构」块。它只作用于本节内部的 `###` 组织,绝不回写
        # reports.outline_json 里用户确认过的大纲。两次重试共用同一份块:它与
        # id_map 一样在这轮里是常量。
        structure_block = outline_structure_block(sub_outline, id_map)
        # 思考型模型(deepseek-v4-pro)偶发把输出预算耗在 reasoning_content(思维链,被
        # _stream_chat_content 丢弃)上 → content 空 → chat_json 兜底 "{}" → markdown 空
        # (不抛异常)。原先空 markdown 会让本节在 _assemble 里静默消失(无标题/无提示)。
        # 有界重试一次("{}" 不入 LLM 缓存,真·重掷);仍空则标 failed(→渲染「本节生成失败」
        # note,不再静默)+ emit model_error(report_engine 原先零可观测)。报告章节会使用
        # 独立的 report_section_max_tokens 上限，但思考型模型仍可能把该预算耗在 reasoning 上。
        markdown, llm_grounded, raw_claims = "", False, None
        for _ in range(2):
            try:
                raw = client.chat_json(
                    [{"role": "user", "content": report_section_prompt(
                        section["title"], section["scope"], question, context_block,
                        allow_parametric=self.settings.report_allow_parametric,
                        discovered_structure=structure_block,
                        assumptions="；".join(
                            str(item) for item in
                            ((section.get("intent_contract") or {}).get("assumptions") or [])
                            if str(item).strip()
                        )[:1000],
                        report_frame=frame_block,
                        synthesis_commitment=synthesis_block,
                    )}],
                    REPORT_SECTION_SCHEMA_HINT, cancel_event=self.cancel_event,
                    **cap_kwargs(client, "report_section_max_tokens"))
                data = json.loads(raw)
                if isinstance(data, dict):
                    markdown = str(data.get("markdown", "")).strip()
                    llm_grounded = bool(data.get("grounded", False))
                    raw_claims = data.get("claims")
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
        citation_audit = audit_high_risk_assertions(
            markdown,
            id_map,
            max_unsupported_ratio=self.settings.report_high_risk_unsupported_ratio,
        )
        if (
            self.settings.report_high_risk_downgrade_enabled
            and citation_audit["threshold_exceeded"]
            and evidence_level == "grounded"
        ):
            evidence_level = "overview"
            citation_audit["downgrade_applied"] = True
        blueprint_claim_ids = (
            {str(row.get("id") or "") for row in localized_synthesis.get("claims") or []}
            if localized_synthesis else None
        )
        claims, claim_ledger_status = normalize_claim_ledger(
            raw_claims,
            markdown=markdown,
            legal_anchor_keys=set(id_map),
            blueprint_claim_ids=blueprint_claim_ids,
            frame=report_frame,
        )
        claim_source_ids = {
            str(context.get("source_id") or "")
            for context in id_map.values()
            if str(context.get("source_id") or "")
        }
        try:
            family_resolution = self._resolve_source_families(claim_source_ids)
        except Exception:
            family_resolution = {
                "family_by_source": {},
                "uncertain_source_ids": list(claim_source_ids),
                "unresolved_source_ids": list(claim_source_ids),
            }
        claims = annotate_trend_evidence(
            claims,
            id_map=id_map,
            family_by_source=family_resolution.get("family_by_source") or {},
            uncertain_source_ids=set(
                family_resolution.get("uncertain_source_ids") or []
            ) | set(family_resolution.get("unresolved_source_ids") or []),
        )
        base = {"title": section["title"], "scope": section["scope"],
                "markdown": markdown, "grounded": evidence_level == "grounded",
                "evidence_level": evidence_level, "top_relevance": top_relevance,
                "citation_audit": citation_audit,
                "claims": claims,
                "claim_ledger_status": claim_ledger_status,
                "synthesis_used": bool(localized_synthesis),
                # Decided by the caller from the section count; this method sees
                # one section and cannot evaluate a report-wide predicate.
                "synthesis_requested": bool(synthesis_requested),
                "intent_ids": list(section.get("intent_ids") or []),
                "id_map": id_map,      # 节内 k -> ctx;仅供 _assemble 全局重编号,不入库
                "attempted": list(getattr(result, "attempted", []) or [])}
        source_graph_status = getattr(result, "source_graph_status", None)
        if source_graph_status is not None:
            base["source_graph"] = source_graph_status.public_dict()
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

    def _synthesize_report_blueprint(self, outline: Sequence[dict], results: Sequence[Any],
                                     question: str, report_frame: Optional[dict]) -> tuple[
                                         Optional[dict], str, Optional[Exception]
                                     ]:
        """Use the one detailed-tier report-wide call, then validate atomically.

        The status is deliberately separate from the optional blueprint so the
        UI can distinguish an evidence-free skip from model/validation failure.
        """
        from app.services.prompts import (
            REPORT_SYNTHESIS_SCHEMA_HINT,
            report_synthesis_prompt,
        )

        evidence_sections, legal_evidence_ids = synthesis_evidence_payload(
            outline, results
        )
        if not legal_evidence_ids:
            return None, "skipped_no_evidence", None
        intent_contract = next(
            (dict(section.get("intent_contract") or {}) for section in outline
             if section.get("intent_contract")),
            {},
        )
        try:
            client = self.dependencies.model_clients.chat("report_summary")
            raw = client.chat_json(
                [{"role": "user", "content": report_synthesis_prompt(
                    question,
                    json.dumps(intent_contract, ensure_ascii=False),
                    json.dumps(report_frame or {}, ensure_ascii=False),
                    json.dumps(evidence_sections, ensure_ascii=False),
                    )}],
                    REPORT_SYNTHESIS_SCHEMA_HINT,
                    cancel_event=self.cancel_event,
                    **cap_kwargs(client, "report_synthesis_max_tokens"),
                )
        except AskCancelled:
            raise
        except Exception as exc:
            return None, "failed_model", exc
        try:
            blueprint = normalize_synthesis_blueprint(
                json.loads(raw), outline=outline,
                legal_evidence_ids=legal_evidence_ids,
                frame=report_frame,
            )
            if not blueprint:
                return None, "failed_validation", ValueError(
                    "report synthesis blueprint failed validation"
                )
            return blueprint, "available", None
        except AskCancelled:
            raise
        except Exception as exc:
            return None, "failed_validation", exc

    def _emit_stage_timing(self, notebook_id: str, rid: str, stage: str,
                           started: float, **extra: Any) -> None:
        """Record one wall-clock stage so slow reports can be attributed.

        Model latency is already recoverable from the LLM log, but retrieval is
        not: a production report showed ~59 minutes of wall clock against ~27
        minutes of parallel model time, and nothing in the record said where the
        other half went.  Carries indices and durations only — never a section
        title, question, or evidence text.
        """
        try:
            self.dependencies.event_log.emit({
                "kind": "report_stage_timing",
                "notebook_id": notebook_id,
                "report_id": rid,
                "stage": stage,
                "ms": int((time.monotonic() - started) * 1000),
                **extra,
            })
        except Exception:
            # Timing is diagnostics; it must never become a second failure
            # channel for a report that is otherwise fine.
            pass

    # --- Stage B+C 并行编排 ---
    def _run_sections(self, notebook_id, rid, outline, question, depth):
        status = [{"title": s["title"], "phase": "排队", "step": 0} for s in outline]
        # One report-wide decision, read by every site below.  Depth no longer
        # gates synthesis; it selects retrieval budgets only.
        synthesis_on = report_synthesis_requested(len(outline))
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

        def _retrieve_one(i, section):
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
                # 大纲步强制落库(codex PR#418 R6 P2):它紧跟在同一轮的 reflect 步
                # 之后发出,2 秒节流几乎总是刚被那一步推过 → 这次写被跳过;而大纲
                # 步又完全可能是本节的最后一个推理动作,`_deep_dive` 一返回,强制写
                # 的「撰写」就把内存里的文案盖掉,用户从头到尾看不到「已整理大纲 N
                # 节」。每节至多 `_MAX_OUTLINE_UPDATES` 次,新增写入是有界的。
                persist(force=step.step_type == "outline")

            started = time.monotonic()
            try:
                result = self._deep_dive(notebook_id, section, question, depth, on_step)
                self._emit_stage_timing(
                    notebook_id, rid, "retrieve", started, section_index=i,
                )
                with lock:
                    status[i]["phase"] = "待综合" if synthesis_on else "待撰写"
                persist(force=True)
                return result, None
            except AskCancelled:
                # A cancelled stage is exactly the one worth attributing: the
                # user gave up because it ran long.  Record before re-raising.
                self._emit_stage_timing(
                    notebook_id, rid, "retrieve", started, section_index=i,
                    cancelled=True,
                )
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                raise
            except Exception as exc:
                self._emit_stage_timing(
                    notebook_id, rid, "retrieve", started, section_index=i,
                    failed=True,
                )
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                return None, exc

        report_frame = next((frame for frame in (
            normalize_report_frame(section.get("report_frame"))
            for section in outline if section.get("report_frame")
        ) if frame), None)

        def _draft_one(i, section, retrieved_row, blueprint):
            result, retrieval_error = retrieved_row
            if retrieval_error is not None or result is None:
                return {
                    "title": section["title"], "scope": section["scope"],
                    "markdown": "", "grounded": False, "failed": True,
                    "error": str(retrieval_error or "retrieval failed")[:300],
                    "id_map": {}, "attempted": [], "claims": [],
                    "claim_ledger_status": "missing", "synthesis_used": False,
                    "synthesis_requested": synthesis_on,
                }
            raise_if_cancelled(self.cancel_event)
            with lock:
                status[i]["phase"] = "撰写"
            persist(force=True)
            drafting_started = time.monotonic()
            try:
                draft_options: Dict[str, Any] = {}
                if report_frame:
                    draft_options["report_frame"] = report_frame
                section_synthesis = blueprint_for_section(blueprint, i)
                if section_synthesis:
                    draft_options["synthesis"] = section_synthesis
                drafted = self._draft_section(
                    notebook_id, section, question, result, depth,
                    synthesis_requested=synthesis_on, **draft_options
                )
                with lock:
                    status[i]["phase"] = (
                        "失败" if drafted.get("failed") else "完成"
                    )
            except AskCancelled:
                self._emit_stage_timing(
                    notebook_id, rid, "draft", drafting_started, section_index=i,
                    cancelled=True,
                )
                with lock:
                    status[i]["phase"] = "失败"
                persist(force=True)
                raise
            except Exception as exc:
                drafted = {
                    "title": section["title"], "scope": section["scope"],
                    "markdown": "", "grounded": False, "failed": True,
                    "error": str(exc)[:300], "id_map": {}, "attempted": [],
                    "claims": [], "claim_ledger_status": "missing",
                    "synthesis_used": False, "synthesis_requested": synthesis_on,
                }
                with lock:
                    status[i]["phase"] = "失败"
            self._emit_stage_timing(
                notebook_id, rid, "draft", drafting_started, section_index=i,
                failed=bool(drafted.get("failed")),
            )
            persist(force=True)
            return drafted

        # The model service capacity is an upper bound, not a database fan-out
        # target.  Reserve two pool slots for interactive reads/writes and cap
        # one report independently so concurrent reports cannot multiply the
        # service's model capacity into pool exhaustion.
        database_worker_cap = max(
            1, int(self.settings.postgres_pool_max_size) - 2
        )
        workers = max(
            1,
            min(
                len(outline),
                self.dependencies.model_clients.parallelism("report_section"),
                int(self.settings.report_section_concurrency),
                database_worker_cap,
            ),
        )

        # Only a report that cannot have cross-section inconsistency keeps the
        # retrieve→draft pipeline.  Every multi-section report now takes the
        # barrier below, trading per-section streaming for one synthesis pass.
        if not synthesis_on:
            def _pipeline_one(i, section):
                return _draft_one(i, section, _retrieve_one(i, section), None)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _pipeline_one, i, section)
                    for i, section in enumerate(outline)
                ]
                return [future.result() for future in futures]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(contextvars.copy_context().run, _retrieve_one, i, s)
                       for i, s in enumerate(outline)]
            retrieved = [future.result() for future in futures]

        results = [row[0] for row in retrieved]
        blueprint = None
        synthesis_status = "not_requested"
        with lock:
            for index, (result, error) in enumerate(retrieved):
                if result is not None and error is None:
                    status[index]["phase"] = "整合全篇证据"
        persist(force=True)
        synthesis_started = time.monotonic()
        try:
            outcome = self._synthesize_report_blueprint(
                outline, results, question, report_frame
            )
        except AskCancelled:
            self._emit_stage_timing(
                notebook_id, rid, "synthesis", synthesis_started,
                sections=len(outline), cancelled=True,
            )
            raise
        self._emit_stage_timing(
            notebook_id, rid, "synthesis", synthesis_started,
            sections=len(outline),
        )
        if isinstance(outcome, tuple) and len(outcome) == 3:
            blueprint, synthesis_status, synthesis_error = outcome
        else:
            # Compatibility for tests/extensions that replace the hook and
            # return the historical optional blueprint.
            blueprint = outcome
            synthesis_status = "available" if blueprint else "failed_validation"
            synthesis_error = None
        if synthesis_status in {"failed_model", "failed_validation"}:
            error = synthesis_error or RuntimeError(
                f"report synthesis ended with {synthesis_status}"
            )
            try:
                self.dependencies.model_errors.note_model_error(
                    "report_synthesis", error, workload_id="report_summary"
                )
            except Exception:
                pass
            try:
                self.dependencies.event_log.emit({
                    "kind": "report_synthesis_failed",
                    "notebook_id": notebook_id,
                    "report_id": rid,
                    "status": synthesis_status,
                })
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(
                contextvars.copy_context().run,
                _draft_one, i, section, retrieved[i], blueprint,
            )
                       for i, section in enumerate(outline)]
            drafted_sections = [future.result() for future in futures]
        if drafted_sections:
            # Ephemeral hand-off to assembly; generate() removes both fields
            # before sections are persisted.
            drafted_sections[0]["_synthesis_status"] = synthesis_status
            if blueprint:
                drafted_sections[0]["_synthesis_blueprint"] = blueprint
        return drafted_sections

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
            gate = self.dependencies.generation_gate

            def _queued():
                reports.update_report(
                    notebook_id, rid, status="generating",
                    progress="生成任务排队中",
                )

            from contextlib import nullcontext
            slot = (
                gate.slot(cancel_event=self.cancel_event, on_wait=_queued)
                if gate is not None else nullcontext()
            )
            with slot:
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
                successful_sections = [
                    section for section in sections
                    if str(section.get("markdown") or "").strip()
                    and not section.get("failed")
                ]
                for s in sections:
                    s.pop("id_map", None)          # 账目仅供 assemble,不入库
                    s.pop("_synthesis_blueprint", None)
                    s.pop("_synthesis_status", None)
                if not successful_sections:
                    reports.update_report(
                        notebook_id, rid, sections=sections,
                        content_md=content_md, gaps=gaps,
                        references=references, status="failed",
                        error="所有章节均未产出有效正文，可从已确认大纲重试生成",
                        progress="生成失败",
                    )
                    return
                reports.update_report(
                    notebook_id, rid, sections=sections,
                    content_md=content_md, gaps=gaps,
                    references=references, status="done", progress="完成",
                )
        except AskCancelled:
            reports.update_report(notebook_id, rid, status="cancelled", progress="已取消")
        except Exception as exc:
            reports.update_report(notebook_id, rid, status="failed",
                                  error=str(exc)[:500], progress="失败")

    # --- 编排:规划(→outline_ready)+(auto_generate 时)生成,保留一键直出 ---
    def run(self, notebook_id, rid, question, history="", depth: int = 2,
            auto_generate: bool = False, intent_contract=None,
            require_intent_review: bool = False, source_scope=None,
            base_scope=None) -> None:
        if require_intent_review and intent_contract is None:
            self.prepare_intent(
                notebook_id, rid, question, history,
                auto_generate=auto_generate,
                source_scope=source_scope,
                base_scope=base_scope,
            )
            return
        self.plan_outline(
            notebook_id, rid, question, history,
            intent_contract=intent_contract,
        )
        if not auto_generate:
            return
        if self.dependencies.reports.claim_report_generation(notebook_id, rid):
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
        # The section-level value crossed the user confirmation boundary and
        # therefore wins over the embedded planner-era compatibility copy.
        report_frame = next((frame for frame in (
            normalize_report_frame(section.get("report_frame"))
            for section in outline if section.get("report_frame")
        ) if frame), None) or normalize_report_frame(
            intent_contract.get("report_frame")
        )
        synthesis_blueprint = next(
            (dict(section.get("_synthesis_blueprint") or {}) for section in sections
             if section.get("_synthesis_blueprint")),
            None,
        )
        synthesis_status = next(
            (str(section.get("_synthesis_status") or "") for section in sections
             if section.get("_synthesis_status")),
            "not_requested",
        )
        editor_coverage: Dict[str, dict] = {}
        contradictions: List[str] = []
        # 执行摘要 + 只读覆盖/冲突审校(容错:失败不拖垮报告,也不重写正文)。
        summary = ""
        has_successful_body = any(
            str(section.get("markdown") or "").strip()
            and not section.get("failed")
            for section in sections
        )
        try:
            if not has_successful_body:
                raise ValueError("no successful report section to summarize")
            sections_block = fair_editor_context(
                sections, frame=report_frame, blueprint=synthesis_blueprint
            )
            summary_client = self.dependencies.model_clients.chat("report_summary")
            raw = summary_client.chat_json(
                [{"role": "user", "content": report_summary_prompt(
                    question, sections_block, intent_block=intent_block
                )}],
                REPORT_SUMMARY_SCHEMA_HINT, cancel_event=self.cancel_event,
                **cap_kwargs(
                    summary_client, "report_summary_max_tokens",
                ))
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
        corpus_profile = dict(intent_contract.get("corpus_profile") or {})
        citation_source_ids = list(dict.fromkeys(
            str(ctx.get("source_id") or "")
            for section in sections
            for ctx in (section.get("id_map") or {}).values()
            if str(ctx.get("source_id") or "")
        ))
        try:
            family_resolution = self._resolve_source_families(citation_source_ids)
        except Exception:
            family_resolution = {
                "family_by_source": {},
                "uncertain_source_ids": citation_source_ids,
                "unresolved_source_ids": citation_source_ids,
            }
        family_by_source = dict(family_resolution.get("family_by_source") or {})
        uncertain_source_ids = set(
            family_resolution.get("uncertain_source_ids") or []
        ) | set(family_resolution.get("unresolved_source_ids") or [])
        citation_source_info = self.dependencies.evidence_context.citation_source_info(
            citation_source_ids
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

        def _family_key(ctx):
            source_id = str(ctx.get("source_id") or "")
            if source_id:
                # Display groups retain each unresolved source separately.  A
                # conservative uncertainty bucket is used only for the Top-1
                # audit below; reusing it here would hide source labels from
                # the reader and merge unrelated bibliography entries.
                return family_by_source.get(source_id, f"source:{source_id}")
            # Legacy/test evidence can lack source_id.  Keep distinct named
            # sources distinct rather than collapsing every missing id.
            title = " ".join(_source_title(ctx).casefold().split())
            return f"source-title:{title}" if title else f"evidence:{_dk(ctx)}"

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
                            "family_key": _family_key(ctx),
                            "family_identity_uncertain": (
                                not str(ctx.get("source_id") or "")
                                or str(ctx.get("source_id") or "") in uncertain_source_ids
                            ),
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
        frame_conflicts = exclusive_frame_conflicts(sections, report_frame)
        gaps.extend(f"分析框架冲突:{item}" for item in frame_conflicts)
        trend_wording_violations = sum(
            bool(claim.get("trend_wording_violation"))
            for section in sections for claim in (section.get("claims") or [])
        )
        if trend_wording_violations:
            gaps.append(
                f"{trend_wording_violations} 条趋势判断的确定语气超过可区分资料数量所支持的等级"
            )
        unsupported_assertions = sum(
            int((section.get("citation_audit") or {}).get("unsupported") or 0)
            for section in sections
        )
        if unsupported_assertions:
            gaps.append(f"{unsupported_assertions} 句高风险事实断言缺少同句引证")

        family_counts: Dict[str, int] = {}
        verified_family_counts: Dict[str, int] = {}
        for reference in references:
            family = str(reference.get("family_key") or "source:unknown")
            family_counts[family] = family_counts.get(family, 0) + 1
            if not reference.get("family_identity_uncertain"):
                verified_family_counts[family] = (
                    verified_family_counts.get(family, 0) + 1
                )
        uncertain_reference_count = sum(
            bool(reference.get("family_identity_uncertain")) for reference in references
        )
        largest_verified_anchor_count = max(verified_family_counts.values(), default=0)
        # Every identity-uncertain anchor could belong to the already-largest
        # verified family.  Report the resulting upper bound instead of
        # allowing unresolved identities to make a concentrated bibliography
        # look diverse.
        top_family_count = largest_verified_anchor_count + uncertain_reference_count
        verified_families = {
            str(reference.get("family_key") or "") for reference in references
            if not reference.get("family_identity_uncertain")
        }
        credibility = {
            "citation_anchors": len(references),
            "independent_cited_families": len(verified_families),
            "distinguishable_cited_families": len(family_counts),
            "identity_uncertain_anchors": uncertain_reference_count,
            "top1_family_anchors": top_family_count,
            "top1_family_share": (
                top_family_count / len(references) if references else 0.0
            ),
            # Stable UI aliases; values still describe conservative document
            # groups, not semantic/canonical deduplication.
            "anchor_count": len(references),
            "independent_documents": len(verified_families),
            "top1_share": (
                top_family_count / len(references) if references else 0.0
            ),
            # This is a display-family statistic: unknown sources remain
            # visible as distinct bibliography groups, while concentration is
            # calculated separately by the conservative upper bound above.
            "anchor_inflation": max(0, len(references) - len(family_counts)),
            "unsupported_high_risk_assertions": unsupported_assertions,
            "high_risk_assertions": sum(
                int((section.get("citation_audit") or {}).get("high_risk_assertions") or 0)
                for section in sections
            ),
            "synthesis_status": synthesis_status,
            "claim_ledgers_available": sum(
                section.get("claim_ledger_status") == "available"
                for section in sections
            ),
            "claim_ledgers_total": len(sections),
            "trend_wording_violations": trend_wording_violations,
        }
        persisted_understanding = dict(intent_contract)
        if report_frame:
            persisted_understanding["report_frame"] = report_frame
        persisted_understanding["credibility"] = credibility
        try:
            self.dependencies.reports.update_report(
                notebook_id, rid, understanding=persisted_understanding
            )
        except Exception:
            # Reporting disclosure must not become a terminal write failure; the
            # same statistics remain derivable from persisted references.
            pass

        # --- 组装 content_md:执行摘要 + 章节 + 参考文献 +(结尾)局限 ---
        parts = [f"# 深度报告:{display_question or question}", ""]
        if summary:
            parts += ["## 执行摘要", "", summary, ""]
        parts += corpus_profile_reader_markdown(
            corpus_profile,
            # References are already assembled here, so the reference-library
            # disclosure costs no query and reports what was actually cited.
            base_reference_sources=base_reference_source_count(references),
        )
        for si, s in enumerate(sections):
            if s.get("failed"):
                parts += [f"## {s['title']}", "", f"（本节生成失败:{s.get('error','')}）", ""]
            elif remapped.get(si):
                parts += [remapped[si], ""]
        if references:
            grouped: Dict[str, List[dict]] = {}
            for reference in references:
                grouped.setdefault(str(reference.get("family_key") or ""), []).append(reference)
            bibliography = []
            for family_references in grouped.values():
                keys = " ".join(f"[{row['key']}]" for row in family_references)
                locations = list(dict.fromkeys(
                    str(row.get("location_label") or "").strip()
                    for row in family_references if str(row.get("location_label") or "").strip()
                ))
                bibliography.append(
                    f"- {keys} {family_references[0]['label']}"
                    + (f" · {'；'.join(locations[:4])}" if locations else "")
                )
            parts += ["## 参考文献", ""] + bibliography + [""]
            parts += [
                f"> 引证分布：{len(references)} 处证据锚点来自 {len(family_counts)} 个可见来源组；"
                f"占比最高的单一文档族上界为 {credibility['top1_family_share']:.1%}；"
                f"同族多锚点形成的锚点膨胀为 {credibility['anchor_inflation']}。"
                + (
                    f"其中 {uncertain_reference_count} 处锚点的来源身份不足；它们不计入独立来源数，"
                    "并在集中度上界中按最不利分配计入占比最高的来源组。"
                    if uncertain_reference_count else ""
                ),
                "",
            ]
        limitations: List[str] = []
        if weak:
            limitations.append(
                f"{'、'.join(weak)} 库内证据有限,相关论述以推断/通识为主"
            )
        if intent_gaps:
            limitations.append(";".join(intent_gaps))
        if contradictions:
            limitations.append("发现需人工复核的跨章节冲突:" + ";".join(contradictions))
        if frame_conflicts:
            limitations.append("发现需人工复核的分析框架冲突:" + ";".join(frame_conflicts))
        if unsupported_assertions:
            limitations.append(
                f"{unsupported_assertions} 句含数字、复杂度或绝对/排序表述的高风险断言缺少同句引证"
            )
        if limitations:
            parts += [
                "> **范围与证据局限**:" + ";".join(limitations)
                + ",建议补充对应语料或调整大纲后重新生成。",
                "",
            ]
        return "\n".join(parts), gaps, references
