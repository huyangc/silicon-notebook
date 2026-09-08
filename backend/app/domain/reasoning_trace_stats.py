"""reflect v2 开闸前 T0:一次 run 的**闭集投影**(纯函数、零 I/O)。

设计真源:`docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md`
(§3 投影、§4 口径),隐私纪律上游是
`docs/superpowers/specs/2026-09-07-retrieval-reflect-final-design_zh.md` §9.1。

这个模块是 `scripts/export_reasoning_traces.py`(导出)、
`scripts/analyze_reasoning_trace.py`(聚合)与 `scripts/reflect_shadow_rig.py`
(影子 run)**共用的同一份投影**,理由与 `app.domain.retrieval_experience`
的 `project_trace_step` 逐字相同:SQLite 把 `ask_trace_steps.step_json` 存成
TEXT、PostgreSQL 存成 jsonb,同一行到 Python 里是两种类型;一份 *收窄* 规则写
两遍,就可能只在一侧悄悄放宽。

**它输出什么**:每个 run 一行,键取自闭集 `RUN_PROJECTION_KEYS`,值只允许是
字符串闭集成员、bool、数值、`None`(= unknown),或「闭集键 → 数值」的字典。
**它不输出什么**:问题原文、来源标题、证据正文、模型 reason、trace summary、
任何数据库 id。`merge_key` 是单向哈希,不是 id(见其常量说明)。

**`None` 是一等值**:旧轨迹缺字段就是 unknown,不折成 0/false。聚合侧据此为
每个指标分别报「可观察样本数」,而不是用 0 把缺失掺进分母。
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.domain.retrieval_experience import (
    SITUATION_RETRIEVAL_EFFORTS,
    SITUATION_UNKNOWN,
    closed_value,
)

UNKNOWN = SITUATION_UNKNOWN

#: 投影行允许出现的**全部**顶层键。隐私守卫(见
#: `backend/tests/test_reasoning_trace_stats.py`)断言 `set(row) ⊆` 这个集合,
#: 所以往投影里加一个 `question` / `summary` / `*_id` 键会直接把用例打红。
#: 一行不必带满:Ask 行不带报告段的键,报告段行不带 Ask 轨迹的键。
RUN_PROJECTION_KEYS: frozenset[str] = frozenset({
    # --- 维度(§3 上半表) ---
    "consumer",
    "mode",
    "effort",
    "kg_in_scope",
    "policy_version",
    "has_intent_contract",
    "corpus_cell",
    "question_key",
    "notebook_bucket",
    "status",
    "trace_source",
    "merge_key",
    # --- Ask 轨迹指标(§3 下半表) ---
    "reflect_turns",
    "action_seq",
    "actions_by_type",
    "seed_actions_by_type",
    "empty_actions_by_type",
    "skip_reasons",
    "fallback_count",
    "fallback_reasons",
    "stale_breaker",
    "stale_max",
    "termination_reason",
    "termination_inferred",
    "aspects_total",
    "aspects_pending",
    "aspects_undelivered",
    "unrecovered_channels_count",
    "candidates_kg",
    "candidates_chunks",
    "candidates_elements",
    "included_kg",
    "included_chunks",
    "included_elements",
    "anchors",
    "citation_contribution",
    "shared_hits",
    "durations_ms",
    "total_ms",
    "trace_steps",
    "trace_truncated",
    # --- 报告段的结果级字段(consumer == "report_section") ---
    "section_index",
    "section_total",
    "report_depth",
    "attempted",
    "attempted_failed",
    "evidence_level",
    "grounded",
    "failed",
    "top_relevance",
})

# --- 闭集词表 ---------------------------------------------------------------

CONSUMERS: tuple[str, ...] = ("ask_single", "ask_sectioned", "report_section")
POLICY_VERSIONS: tuple[str, ...] = ("legacy", "v2")
JOB_STATUSES: tuple[str, ...] = ("running", "done", "failed", "cancelled")
CORPUS_CELLS: tuple[str, ...] = ("A_kg", "A_nokg", "B_kg", "B_nokg")
TRACE_SOURCES: tuple[str, ...] = ("trace_steps", "legacy_column", "in_process")
EVIDENCE_LEVELS: tuple[str, ...] = ("grounded", "overview", "inferred")
#: 请求侧的 mode 词表。`auto` 在落库前就被解析成 chunk/reasoning(见
#: `ask_state_store` 的 `UPDATE ask_jobs SET mode=...`),所以 `auto→*` 只能由
#: rig 的 `requested_mode` 标出来;线上导出永远只看到解析后的那一个。
MODES: tuple[str, ...] = (
    "chunk", "reasoning", "auto→chunk", "auto→reasoning",
)

#: 服务层实际发出的 `TraceStep.step_type` 全集(2026-09-08 对
#: `backend/app/services/**.py` 的 `step_type=` 全量 grep)。闭集之外的值一律
#: 折成 `other`,而不是原样带出去——新增一个 step_type 不该悄悄扩大投影面。
STEP_TYPES: tuple[str, ...] = (
    "answer", "consult_memory", "enumerate", "exact_lookup", "expand",
    "expand_community", "experience", "fallback", "follow_chain",
    "gap_consult", "intent", "memory", "outline", "plan", "plugin", "ppr",
    "profile", "reflect", "retrieve", "search_chunks", "skip", "spreadsheet",
    "synthesis",
)
STEP_TYPE_OTHER = "other"

#: 「不是一次检索动作」的 step_type。它们各有自己的指标(reflect_turns /
#: skip_reasons / candidates_* / included_*),不进 `action_seq` 与
#: `actions_by_type`,否则「哪类动作最常空手」会被一堆记账步稀释。
NON_ACTION_STEP_TYPES: frozenset[str] = frozenset({
    "answer", "experience", "intent", "plan", "profile", "reflect", "skip",
    "synthesis",
})

#: v2 终态步的稳定原因码(= `app.services.reasoning_aspects.TERMINATION_SKIP_REASON`)。
#: 这里刻意写字面量而不是 import:domain 不许 import services。两处分叉会被
#: `test_reasoning_trace_stats.py::test_termination_skip_reason_matches_service`
#: 当场打红。
TERMINATION_SKIP_REASON = "retrieval_termination"

#: 无图披露步的原因码。`kg_gap_unavailable`(缺口回想通道不可用)刻意不在其中:
#: 那是一个动作通道的可用性,不是「这个库有没有图」。
KG_UNAVAILABLE_REASONS: frozenset[str] = frozenset({"kg_unavailable"})

STALE_BREAKER_REASON = "stale_circuit_breaker"
NO_EXECUTABLE_ACTION_REASON = "no_executable_action"

#: legacy 反推出来的「模型自己说够了」。v2 闭集里没有同义项——v2 会把它进一步
#: 分成 model_sufficient / model_partial(要读方面账才分得出来),legacy 轨迹里
#: 没有方面账,所以只能停在这个更粗的码上。
TERMINATION_MODEL_END = "model_end"
#: 投影可能写出的全部 termination 码:v2 的闭集(逐字抄自
#: `app.domain.retrieval_termination.TERMINATION_REASONS`,由用例钉住)+ 上面
#: 那个 legacy-only 的粗码。
TERMINATION_REASON_VALUES: tuple[str, ...] = (
    "model_sufficient", "model_partial", "step_budget", "stale",
    "no_executable_action", "model_degraded", "retrieval_degraded",
    TERMINATION_MODEL_END,
)

#: 来源数分桶(§3)。桶而不是计数:一个库有几个来源在小样本里就是准 id。
_SOURCE_BUCKETS: tuple[tuple[int, str], ...] = (
    (1, "1"), (5, "2-5"), (20, "6-20"),
)
SOURCE_BUCKET_TOP = "21+"

#: 各档位的 reflect 轮数硬上限,legacy 反推 `step_budget` 用。读的是
#: `ask_retrieval_policy` 那一份(唯一真源),不在这里抄一张会分叉的表。
MAX_REFLECT_STEPS: Mapping[str, int] = {
    effort: limits.max_reasoning_steps
    for effort, limits in ASK_RETRIEVAL_LIMITS.items()
}


# --- 基础归一 ---------------------------------------------------------------


def _mapping(raw: object) -> Mapping | None:
    """`step_json` 两种持久化类型的唯一解码点(SQLite TEXT / PG jsonb)。"""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return raw if isinstance(raw, Mapping) else None


def _detail(step: Mapping) -> Mapping:
    detail = step.get("detail")
    return detail if isinstance(detail, Mapping) else {}


def _int(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _float(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _reason(detail: Mapping) -> str:
    raw = detail.get("reason")
    return raw if isinstance(raw, str) and raw else UNKNOWN


def _step_type(step: Mapping) -> str:
    raw = str(step.get("step_type") or "")
    return raw if raw in STEP_TYPES else STEP_TYPE_OTHER


def _closed_exact(raw: object, vocabulary: tuple[str, ...]) -> str:
    """`closed_value` 的**大小写敏感**孪生。

    `closed_value` 会先 `lower()`,那对 `chunk`/`deep` 这类全小写词表无害,但
    `corpus_cell` 的成员是 `A_kg` / `B_nokg`——过一次 `lower()` 就全部落成
    unknown。两个函数各管各的词表,不合并。
    """
    text = str(raw or "").strip()
    return text if text in vocabulary else UNKNOWN


def source_bucket(count: object) -> str:
    """来源数 → 闭集桶。`None` / 非数 → unknown(不是 `1`)。"""
    value = _int(count)
    if value is None or value < 0:
        return UNKNOWN
    for ceiling, label in _SOURCE_BUCKETS:
        if value <= ceiling:
            return label
    return SOURCE_BUCKET_TOP


def merge_key(*parts: object) -> str:
    """把「哪一行」压成一个不可逆的 16 位十六进制串。

    只用来把两份 JSONL 拼起来(报告的结果级行来自 `reports.sections_json`,
    逐节轨迹来自 rig 的进程内捕获,§2.1/§2.3)。它不是 id:sha256 截断后既读
    不回原值,也不能反查库——但相同输入恒等,所以两侧各算一次就能对上。
    """
    raw = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --- 单步归一 ---------------------------------------------------------------


def _is_seed(step: Mapping, detail: Mapping, seen_reflect: bool) -> bool:
    """这一步属于首轮播种,还是反思循环里模型选的动作?

    首选写侧的显式信号 `detail.phase == "seed"`(PPR / 精查 / 原文播种三处都
    写)。旧轨迹没有这个键,退回**位置**判据:第一条 `reflect` 步之前的一切都
    发生在模型做出任何选择之前,按定义就是播种。
    """
    phase = detail.get("phase")
    if isinstance(phase, str) and phase:
        return phase == "seed"
    return not seen_reflect


def _action_key(step_type: str, seed: bool) -> str:
    return f"seed:{step_type}" if seed else step_type


def _step_count(step_type: str, detail: Mapping) -> int | None:
    """这一步「拿回来多少」。口径与 `project_trace_step` 同源。"""
    keys = ("count",) if step_type == "retrieve" else (
        "count", "found", "returned_total", "new"
    )
    for key in keys:
        value = _int(detail.get(key))
        if value is not None:
            return value
    return None


def normalize_steps(steps: Iterable[object]) -> list[dict]:
    """把持久化的轨迹行归一成投影内部用的中间形态(仍是纯数据)。

    解码不了的行直接丢弃,而不是让整次导出失败——`read_trace` 从子表存在起就
    是这个容忍度,导出侧没有理由更严。
    """
    normalized: list[dict] = []
    seen_reflect = False
    for raw in steps:
        step = _mapping(raw)
        if step is None:
            continue
        detail = _detail(step)
        step_type = _step_type(step)
        seed = _is_seed(step, detail, seen_reflect)
        if step_type == "reflect":
            seen_reflect = True
        normalized.append({
            "step_type": step_type,
            "seed": seed,
            "detail": detail,
            "duration_ms": _int(step.get("duration_ms")),
            "count": _step_count(step_type, detail),
        })
    return normalized


# --- 引用贡献(§4.4) --------------------------------------------------------


def _anchor_evidence(steps: Sequence[Mapping]) -> tuple[set[str], bool]:
    """终步锚点集合 + 「这个集合可不可信」。

    可信 = 至少有一条 synthesis/answer 步**带** `anchor_evidence_ids` 键,且
    没有任何一条带截断标。缺键不是「零锚点」:按 seq 截尾的轨迹里,真正带锚点
    的那条 synthesis 步完全可能被切掉(见 `project_run_step` 的同款说明)。
    """
    anchors: set[str] = set()
    present = False
    truncated = False
    for step in steps:
        if step["step_type"] not in ("synthesis", "answer"):
            continue
        detail = step["detail"]
        if "anchor_evidence_ids" not in detail:
            continue
        present = True
        raw = detail.get("anchor_evidence_ids")
        if isinstance(raw, (list, tuple)):
            anchors.update(item for item in raw if isinstance(item, str) and item)
        if detail.get("anchor_evidence_ids_truncated"):
            truncated = True
    return anchors, present and not truncated


def citation_contribution(steps: Sequence[Mapping]) -> tuple[dict, int]:
    """每动作引用贡献 + 跨动作重复命中数(§4.4)。

    三条规则,一条都不许放松:

    1. 某步没有 `result_ids` 键、或带 `result_ids_truncated`、或整轮锚点不可
       信 → 该步计入 `unknown_steps`,**不**拿整轮 `anchors` 数顶替。一个动作
       如果一步都不可判,它的 `cited_hits` 是 `None`(unknown),不是 0。
    2. 同一个证据 id 被多步命中 → 按**首次**命中归因(轨迹按 seq 有序,所以
       首轮 seed 天然先于循环动作),重复的那几次只累进 `shared_hits`,不均摊。
    3. `seed:ppr` 与 `ppr` 是两行。
    """
    anchors, usable = _anchor_evidence(steps)
    entries: dict[str, dict] = {}
    attributed: set[str] = set()
    shared_hits = 0
    for step in steps:
        step_type = step["step_type"]
        if step_type in NON_ACTION_STEP_TYPES:
            continue
        key = _action_key(step_type, step["seed"])
        entry = entries.setdefault(
            key,
            {"steps": 0, "steps_with_ids": 0, "unknown_steps": 0, "hits": 0,
             "resolvable": 0},
        )
        entry["steps"] += 1
        detail = step["detail"]
        has_ids = "result_ids" in detail
        if has_ids:
            entry["steps_with_ids"] += 1
        if not has_ids or detail.get("result_ids_truncated") or not usable:
            entry["unknown_steps"] += 1
            continue
        entry["resolvable"] += 1
        raw = detail.get("result_ids")
        for item in raw if isinstance(raw, (list, tuple)) else ():
            if not isinstance(item, str) or item not in anchors:
                continue
            if item in attributed:
                shared_hits += 1
                continue
            attributed.add(item)
            entry["hits"] += 1
    contribution = {
        key: {
            "steps": entry["steps"],
            "steps_with_ids": entry["steps_with_ids"],
            "unknown_steps": entry["unknown_steps"],
            "cited_hits": entry["hits"] if entry["resolvable"] else None,
        }
        for key, entry in sorted(entries.items())
    }
    return contribution, shared_hits


# --- 结束原因(§3 / §4.5) ---------------------------------------------------


def _v2_termination(steps: Sequence[Mapping]) -> str | None:
    for step in steps:
        detail = step["detail"]
        if step["step_type"] == "skip" and _reason(detail) == TERMINATION_SKIP_REASON:
            raw = detail.get("termination")
            if isinstance(raw, str) and raw in TERMINATION_REASON_VALUES:
                return raw
            return UNKNOWN
    for step in steps:
        raw = step["detail"].get("termination_reason")
        if isinstance(raw, str) and raw in TERMINATION_REASON_VALUES:
            return raw
    return None


def _has_skip_reason(steps: Sequence[Mapping], reason: str) -> bool:
    return any(
        step["step_type"] == "skip" and _reason(step["detail"]) == reason
        for step in steps
    )


def infer_legacy_termination(
    steps: Sequence[Mapping], effort: str,
) -> str | None:
    """legacy 轨迹的结束原因反推。返回 `None` = unknown。

    次序刻意让**记录下来的事实**先于推断:熔断步与「无可执行动作」步是执行处
    当场写下的,读到就是它;只有都没有时才去读末尾 reflect 的决定,再不行才用
    「reflect 轮数已经顶到档位上限」这条最弱的推断。三条判据在真实轨迹里互斥
    (熔断/无动作都会让当轮 reflect 的决定不是 answer),排序因此不改变结论,
    只固定了「同一条 run 只算一次」。
    """
    if _has_skip_reason(steps, STALE_BREAKER_REASON):
        return "stale"
    if _has_skip_reason(steps, NO_EXECUTABLE_ACTION_REASON):
        return NO_EXECUTABLE_ACTION_REASON
    reflects = [step for step in steps if step["step_type"] == "reflect"]
    if reflects:
        detail = reflects[-1]["detail"]
        if detail.get("next_action") == "answer" or detail.get("sufficient"):
            return TERMINATION_MODEL_END
    ceiling = MAX_REFLECT_STEPS.get(effort)
    if ceiling is not None and len(reflects) >= ceiling:
        return "step_budget"
    return None


def _stale(steps: Sequence[Mapping]) -> tuple[bool, int | None]:
    """熔断是否触发 + 轨迹里出现过的最大 stale 计数。

    `stale` 计数只在 reflect 步的 detail 里(以及熔断步自己)出现;一条都没有
    (旧轨迹)就是 unknown,不是 0。
    """
    breaker = _has_skip_reason(steps, STALE_BREAKER_REASON)
    values = [
        value for value in (_int(step["detail"].get("stale")) for step in steps)
        if value is not None
    ]
    return breaker, max(values) if values else None


# --- run 级投影 -------------------------------------------------------------


def _consumer(steps: Sequence[Mapping]) -> str:
    for step in steps:
        if step["step_type"] != "synthesis":
            continue
        detail = step["detail"]
        if "section_total" in detail or _int(detail.get("outline_sections")):
            return "ask_sectioned"
    return "ask_single"


def _kg_in_scope(steps: Sequence[Mapping], payload: Mapping) -> bool | None:
    if any(
        step["step_type"] == "skip"
        and _reason(step["detail"]) in KG_UNAVAILABLE_REASONS
        for step in steps
    ):
        return False
    required = payload.get("kg_required")
    if isinstance(required, bool):
        return not required
    return None


def _mode(job_row: Mapping, payload: Mapping, rig_tags: Mapping) -> str:
    raw = payload.get("mode") or job_row.get("mode")
    resolved = closed_value(raw, ("chunk", "reasoning"))
    if resolved == UNKNOWN:
        return UNKNOWN
    if str(rig_tags.get("requested_mode") or "").strip().lower() == "auto":
        return f"auto→{resolved}"
    return resolved


def _terminal_detail(steps: Sequence[Mapping], key: str) -> int | None:
    """最后一条带 `key` 的 synthesis/answer 步上的那个整数。"""
    value = None
    for step in steps:
        if step["step_type"] in ("synthesis", "answer"):
            found = _int(step["detail"].get(key))
            if found is not None:
                value = found
    return value


def _counters(steps: Sequence[Mapping]) -> dict[str, dict]:
    actions: Counter = Counter()
    seeds: Counter = Counter()
    empty: Counter = Counter()
    skips: Counter = Counter()
    fallbacks: Counter = Counter()
    durations: Counter = Counter()
    sequence: list[str] = []
    for step in steps:
        step_type = step["step_type"]
        duration = step["duration_ms"]
        if duration is not None:
            durations[step_type] += duration
        if step_type == "skip":
            skips[_reason(step["detail"])] += 1
            continue
        if step_type in NON_ACTION_STEP_TYPES:
            continue
        key = _action_key(step_type, step["seed"])
        sequence.append(key)
        (seeds if step["seed"] else actions)[step_type] += 1
        if step["count"] == 0:
            empty[key] += 1
        if step_type == "fallback":
            fallbacks[_reason(step["detail"])] += 1
    return {
        "action_seq": sequence,
        "actions_by_type": dict(sorted(actions.items())),
        "seed_actions_by_type": dict(sorted(seeds.items())),
        "empty_actions_by_type": dict(sorted(empty.items())),
        "skip_reasons": dict(sorted(skips.items())),
        "fallback_count": sum(fallbacks.values()),
        "fallback_reasons": dict(sorted(fallbacks.items())),
        "durations_ms": dict(sorted(durations.items())),
    }


def project_run(
    job_row: Mapping,
    steps: Iterable[object],
    answer_payload: object = None,
    *,
    sources_count: object = None,
    rig_tags: Mapping | None = None,
) -> dict:
    """一次 Ask run → 一行闭集投影(§3)。

    `job_row` 只被读 `mode` / `status`;`answer_payload` 只被读
    `mode` / `retrieval_effort` / `intent` / `kg_required`(有没有、是不是
    bool),问题原文、答案正文、引用卡一律不碰。`rig_tags` 是 rig 侧按
    `client_request_id` 解码出来的编号(`corpus_cell` / `question_key` /
    `requested_mode`),线上导出传空 ⇒ 那几个维度恒为 unknown。
    """
    tags = rig_tags or {}
    payload = _mapping(answer_payload) or {}
    normalized = normalize_steps(steps)

    v2_reason = _v2_termination(normalized)
    effort = closed_value(
        payload.get("retrieval_effort"), SITUATION_RETRIEVAL_EFFORTS
    )
    if v2_reason is not None:
        termination, inferred = v2_reason, False
    else:
        termination = infer_legacy_termination(normalized, effort)
        inferred = None if termination is None else True
    breaker, stale_max = _stale(normalized)
    contribution, shared = citation_contribution(normalized)
    counters = _counters(normalized)
    total_ms = sum(counters["durations_ms"].values())
    intent = payload.get("intent")

    row: dict[str, Any] = {
        # rig 的报告轨迹用同一条投影(逐节深挖跑的就是 `ReasoningRetriever.run`),
        # 但它的消费者不是 Ask,轨迹里也没有 synthesis 步可以据以判定,所以只能由
        # rig 显式声明。声明值仍过闭集:`consumer=<自由文本>` 进不来。
        "consumer": _closed_exact(tags.get("consumer"), CONSUMERS)
        if tags.get("consumer") else _consumer(normalized),
        "mode": _mode(job_row, payload, tags),
        "effort": effort,
        "kg_in_scope": _kg_in_scope(normalized, payload),
        "policy_version": "v2" if v2_reason is not None else "legacy",
        "has_intent_contract": bool(intent),
        "corpus_cell": _closed_exact(tags.get("corpus_cell"), CORPUS_CELLS),
        "question_key": str(tags.get("question_key") or "") or UNKNOWN,
        "notebook_bucket": source_bucket(sources_count),
        "status": closed_value(job_row.get("status"), JOB_STATUSES),
        "trace_source": closed_value(
            tags.get("trace_source") or "trace_steps", TRACE_SOURCES
        ),
        "reflect_turns": sum(
            1 for step in normalized if step["step_type"] == "reflect"
        ),
        "termination_reason": termination,
        "termination_inferred": inferred,
        "stale_breaker": breaker,
        "stale_max": stale_max,
        "aspects_total": _terminal_detail(normalized, "aspects_total"),
        "aspects_pending": _terminal_detail(normalized, "aspects_pending"),
        "aspects_undelivered": _terminal_detail(normalized, "aspects_undelivered"),
        "unrecovered_channels_count": _unrecovered_channels(normalized),
        "candidates_kg": _terminal_detail(normalized, "kg"),
        # ⚠ 今天的 `answer` 步 detail 只有 `kg` / `elements` / `chains` /
        # `enumerations`(见 reasoning_retrieval.py 的 `answer_detail`),**没有**
        # 原文段候选数,所以这一列在现行 schema 上恒为 unknown。保留它是为了不
        # 让「候选池三分」在写侧补齐那天变成一次投影键集变更;它绝不折成 0——
        # 0 会被读成「一段原文候选都没有」。
        "candidates_chunks": _terminal_detail(normalized, "chunks"),
        "candidates_elements": _terminal_detail(normalized, "elements"),
        "included_kg": _terminal_detail(normalized, "included_kg"),
        "included_chunks": _terminal_detail(normalized, "included_chunks"),
        "included_elements": _terminal_detail(normalized, "included_elements"),
        "anchors": _terminal_detail(normalized, "anchors"),
        "citation_contribution": contribution,
        "shared_hits": shared,
        "total_ms": total_ms if counters["durations_ms"] else None,
        "trace_steps": len(normalized),
        "trace_truncated": _truncated(normalized),
    }
    row.update(counters)
    assert_closed(row)
    return row


def _unrecovered_channels(steps: Sequence[Mapping]) -> int | None:
    for step in steps:
        raw = step["detail"].get("unrecovered_channels")
        if isinstance(raw, (list, tuple)):
            return len(raw)
    return None


def _truncated(steps: Sequence[Mapping]) -> bool:
    return any(
        step["detail"].get("result_ids_truncated")
        or step["detail"].get("anchor_evidence_ids_truncated")
        for step in steps
    )


def project_report_section(
    section: object,
    *,
    section_index: int,
    section_total: int | None = None,
    report_depth: object = None,
    report_id: object = None,
    rig_tags: Mapping | None = None,
) -> dict:
    """`reports.sections_json` 的一节 → 一行**结果级**投影(§2.1)。

    逐节轨迹不落库,所以这一行只回答「这一节最后长什么样」;轨迹那一半由 rig
    的进程内 JSONL 出,两者按 `merge_key` 对上。节标题、正文、claims 一律不
    读——它们是自由文本。
    """
    tags = rig_tags or {}
    data = _mapping(section) or {}
    attempted = data.get("attempted")
    rows = attempted if isinstance(attempted, (list, tuple)) else ()
    parsed = [row for row in (_mapping(item) for item in rows) if row is not None]
    row = {
        "consumer": "report_section",
        "corpus_cell": _closed_exact(tags.get("corpus_cell"), CORPUS_CELLS),
        "question_key": str(tags.get("question_key") or "") or UNKNOWN,
        "policy_version": closed_value(
            tags.get("policy_version"), POLICY_VERSIONS
        ),
        "merge_key": merge_key(report_id, section_index),
        "section_index": section_index,
        "section_total": section_total,
        "report_depth": _int(report_depth),
        "attempted": len(rows),
        "attempted_failed": sum(1 for row in parsed if row.get("failed")),
        "evidence_level": closed_value(
            data.get("evidence_level"), EVIDENCE_LEVELS
        ),
        "grounded": bool(data.get("grounded")) if "grounded" in data else None,
        "failed": bool(data.get("failed")),
        "top_relevance": _float(data.get("top_relevance")),
    }
    assert_closed(row)
    return row


def assert_closed(row: Mapping) -> None:
    """投影行的形状自检。导出与 rig 在写每一行之前都调它一次。

    这是运行期的第二道闸(第一道是用例里的隐私守卫):脚本侧任何时候往行里塞
    了一个闭集外的键,写出去之前就会炸,而不是安静地把一列自由文本落进 JSONL。
    """
    extra = set(row) - RUN_PROJECTION_KEYS
    if extra:
        raise ValueError(
            "projection row carries keys outside RUN_PROJECTION_KEYS: "
            + ", ".join(sorted(extra))
        )
