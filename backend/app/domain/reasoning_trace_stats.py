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
import re
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.domain.retrieval_experience import (
    SITUATION_RETRIEVAL_EFFORTS,
    SITUATION_UNKNOWN,
    closed_value,
)
from app.domain.retrieval_termination import (
    ASPECT_REJECTION_REASONS,
    ASSESSMENT_SKIP_REASON_PREFIX,
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
    # rig 的 `search`/`ask` 侧声明:这道题是不是按显式来源身份收窄了检索范围
    # (codex #700 R3 P2)。`True`=收窄且已解析到位,`False`=题目本来就没有
    # 声明范围,`None`=声明了范围但解析不到唯一匹配、这次 run 没跑
    # (`status=failed`)。不是从轨迹反推的——`search`/`ask` 是唯一手上有
    # 「这道题声明了哪些来源标题」这件事实的调用方,线上导出恒不写这个键。
    "scope_narrowed",
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
    "assessment_rejections",
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
    # --- reflect 前缀复用测量(T-PS4;稀疏,缺一律 None) ---
    # 一次 run 的**墙钟**。轨迹里没有这件事实(`total_ms` 是各步耗时之和,漏掉
    # 排队与步与步之间的空隙),只有 rig 手上有,所以导出侧恒 `None`、由 rig
    # 就地写入(计划 §3 T-PS5)。
    "run_wall_ms",
    # 这次 run **真正发出去**的模型请求数(reflect 步 `call_attempts` 之和)。
    # 与 A/B 的 `model_calls`(日志行数)不是同一个数:静默 fallback 会多发一次
    # 请求却不留日志行(见 `app/core/llm.py` 的 `response_format` / `stream_options`
    # 两处重建)。任一 reflect 步缺这个观测 ⇒ 整列 unknown,不按现有的几步求和。
    "model_calls_real",
    # 上面那个和「全不全」。`True` = 每一条 reflect 步都带 `call_attempts`;
    # `False` = 只带了一部分(此时和是下界,不出数);`None` = 一条都没带(旧
    # 轨迹 / 测量关)。它与 `trace_truncated` **解耦**(见 `_reflect_attempts`):
    # 那一列说的是某一步的 id 列表被 `TRACE_RESULT_IDS_MAX` 截了,不是轨迹缺轮。
    #
    # ⚠ 与 `app/core/llm.py` 的 `attempts_observed` **同名而不同层**:那边是
    # **单次调用**的「这个 transport 自己在传输层数了请求数」(写进 sink 时恒
    # `True`,duck-typed 客户端留下空 sink 才叫没量);这一列是**一次 run** 的
    # 「每一轮 reflect 都把那个数落进轨迹了吗」。一轮的 `True` 不保证整 run 的
    # `True`——少落一轮就是 `False`。
    "attempts_observed",
    # rig 声明的第二维实验臂(`OPTIMIZATIONS`)。与 `policy_version` 并列而不同
    # 义:那个是从轨迹反推的证据,这个是声明——线上导出恒不写,于是恒 unknown。
    "optimization",
    # 「最后一轮」各上下文块的规模:短码 → 数值。`s/c/k/d/t` 是五个块的**字符**
    # 数,`bytes_total` 是整条消息的**字节**数(来自 `ctx_bytes_total`)——两种
    # 单位挤在同一张表里,见 `REFLECT_CONTEXT_DETAIL_KEYS` 的说明。
    "context_chars",
    # 逐轮公共前缀字节的三格聚合(计划 §5 Q6:逐轮细节只在 rig 的 per-call 表)。
    # 首轮没有可比的上一轮,写侧记 `None`,这里不计入。
    "prefix_bytes_median",
    "prefix_bytes_min",
    "prefix_turns",
    # 各 reflect 步交还给调用方的正文字符数之和(不经日志截断)。与
    # `model_calls_real` 同一条「任一步缺 ⇒ unknown」的口径。
    "response_chars_total",
    # --- reflect 前缀复用测量(计划 PR-3 T-PD2;`prefix_delta` 专属) ---
    # 一次 run 里各 reflect 步「重建了几次 K」的**最大值**(max-over-present,
    # 同 `_stale` 的口径,不是 `_reflect_sum` 的「任一步缺 ⇒ unknown」):重建
    # 次数是单调不减的运行期计数,不是逐步各自独立的观测量,取最大值才是「这次
    # run 总共重建了几次」。全部 reflect 步都缺这个观测 ⇒ `None`。⚠
    # `context_rebuilds == 0`(跑了 delta、一次都没重建)与 `None`(非 delta 臂,
    # 或测量口径不适用)必须分得开:前者是「量到了,答案是零」,后者是「没量」。
    "context_rebuilds",
    # 一次 run 里「本 run 剩余轮回退到 P 的有界选择」这件事是否发生过——任一
    # reflect 步为真 ⇒ 整列真(回退按计划 §2 拍板 Q4 不可逆,发生过就不会撤销)。
    # 一步都没带这个观测 ⇒ `None`,不是 `False`。
    "context_fallback",
    # --- reflect 前缀复用测量(计划 PR-4 T-PL2) ---
    # 各 reflect 步「本轮实际落账的方面自评行数」之和(拍板 Q8/Q9)。门是测量
    # 开关(`measures_messages`),**四臂通写**——`off`/`prefix_snapshot`/
    # `prefix_delta` 下模型仍会重述全量,这一列同样有意义,不是
    # `prefix_delta_lean` 专属的行为计数。**sum-over-present**(质量评审修正
    # P2-2,`_reflect_assessment` 口径),不是 `_reflect_sum` 的「任一步缺 ⇒
    # 整列 unknown」:provider fail-open 的那一轮不进 `_absorb_assessment`,
    # 该轮的 reflect 步压根不带这个键,但它仍是一条正常记的 reflect 步——沿用
    # 全或无口径会让一次偶发 provider 抖动把整条 run 读成 unknown。全部步都缺
    # ⇒ `None`。这一列的"全不全"由下面的 `assessment_observed` 单独披露。
    "assessment_rows_total",
    # 上面那个和「全不全」。`True` = 每一条 reflect 步都带 `assessment_rows`;
    # `False` = 只带了一部分(此时和是**真实**的部分和,不是下界——见
    # `_reflect_assessment` 的口径说明,这一点与 `attempts_observed` 不同);
    # `None` = 一条都没带(旧轨迹 / 测量关)。照
    # `attempts_observed`(`_reflect_attempts`)的先例登记:一次 provider
    # fail-open 抖动会让这一轮缺席,用这一列披露"缺了一轮"而不是让
    # `assessment_rows_total` 整列 unknown。
    "assessment_observed",
    # v2 终态 skip 步 detail 上的「模型自己收尾时,还有几个方面它一次都没判断
    # 过」(拍板 Q5/Q9)。v2 全部四臂无条件写(拍板 Q10 已知偏离:这是本期唯一
    # 允许偏离三臂字节等价的字段,写在终态 detail 上,不改共用 prompt 字节;
    # legacy 没有方面账,恒 `None`)。`0`(问过了、没有遗漏)与缺席(没量到/
    # legacy/未到终态)必须分得开。
    "aspects_unassessed",
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
#: reflect 前缀复用的实验第二维(`REASONING_REFLECT_OPTIMIZATION` 的取值域)。
#: 与 `POLICY_VERSIONS` 并列:一条 run 的身份是 `(policy_version, optimization)`
#: 这一对,不是其中任何一个。
#:
#: 这份词表**刻意在 domain 本地写死**,不 import `app.core.config`。架构守卫
#: (`scripts/check_architecture_boundaries.py` 的 `FORBIDDEN_DOMAIN_PREFIXES`)
#: 其实没禁 domain → core,但两条理由让本地定义更对:(1) 这个模块的硬约束是
#: 「零 I/O、不读 Settings」,把整个 `config` 拉进来只为一个元组,等于把
#: pydantic-settings 的加载面接进一份纯投影;(2) `config.py` 上的
#: `reasoning_reflect_optimization` 由计划 §3 T-PS6 在另一条支线上落地,这里先
#: 落地闭集会更早,两处都存在时由
#: `test_reasoning_trace_stats.py::test_optimizations_match_the_settings_literal`
#: 逐字对齐——分叉当场打红,而不是悄悄放宽一侧。
OPTIMIZATIONS: tuple[str, ...] = (
    "off", "prefix_snapshot", "prefix_delta", "prefix_delta_lean",
)
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
    "profile", "reflect", "rerank", "retrieve", "search_chunks", "skip",
    "spreadsheet", "synthesis",
)
STEP_TYPE_OTHER = "other"

#: 「不是一次检索动作」的 step_type。它们各有自己的指标(reflect_turns /
#: skip_reasons / candidates_* / included_*),不进 `action_seq` 与
#: `actions_by_type`,否则「哪类动作最常空手」会被一堆记账步稀释。
#: `rerank`(T-BF6 的收尾重排)在这里而不在动作侧:它不是模型选的一次检索动作,
#: 是服务端每次收尾都会做的一段记账,进 `action_seq` 会给每条 v2 轨迹尾巴上挂
#: 一个恒定项,把「模型挑了哪些动作」这份序列稀释掉。它要交代的只有耗时——那由
#: `durations_ms` 按 step_type 自动承接,不需要新投影键。
NON_ACTION_STEP_TYPES: frozenset[str] = frozenset({
    "answer", "experience", "intent", "plan", "profile", "reflect", "rerank",
    "skip", "synthesis",
})

#: v2 终态步的稳定原因码(= `app.services.reasoning_aspects.TERMINATION_SKIP_REASON`)。
#: 这里刻意写字面量而不是 import:domain 不许 import services。两处分叉会被
#: `test_reasoning_trace_stats.py::test_termination_skip_reason_matches_service`
#: 当场打红。
TERMINATION_SKIP_REASON = "retrieval_termination"

#: 「这条 skip 是一次**逐方面**被拒的自评」的原因码全集。整份形状不成立的那一族
#: 共用同一个前缀但后缀不在这里,所以两者分得开(见
#: `app.domain.retrieval_termination.ASPECT_REJECTION_REASONS`)。这一份是
#: import 来的,不是抄的字面量:同为 domain,没有 `TERMINATION_SKIP_REASON`
#: 那条「domain 不许 import services」的顾虑。
ASSESSMENT_REJECTION_REASONS: frozenset[str] = frozenset(
    f"{ASSESSMENT_SKIP_REASON_PREFIX}{code}"
    for code in ASPECT_REJECTION_REASONS
)

#: 无图披露步的原因码。`kg_gap_unavailable`(缺口回想通道不可用)刻意不在其中:
#: 那是一个动作通道的可用性,不是「这个库有没有图」。
KG_UNAVAILABLE_REASONS: frozenset[str] = frozenset({"kg_unavailable"})

#: 图形状的检索步:出现过就是「这一轮确实动用了图」的正面证据。`retrieve`
#: 不在其中——它同时服务图检索与原文检索,单看 step_type 分不清,只能按
#: detail 里的产出键区分(见 `_is_kg_shaped_retrieve`)。
KG_SHAPED_STEP_TYPES: frozenset[str] = frozenset({
    "expand", "ppr", "follow_chain", "expand_community",
})

#: 无图原文半留下的印记。`_search_passages_if_graphless`
#: (`reasoning_retrieval.py`)只在 `state.kg_in_scope` 为假时才把这个键写进
#: 同一条 `retrieve` 步的 detail——它一在场,这一步的 `new` 就只可能是空手的
#: 图查询,不能拿来当「图在场」的证据。
_GRAPHLESS_RETRIEVE_MARK = "chunks_found"

STALE_BREAKER_REASON = "stale_circuit_breaker"
NO_EXECUTABLE_ACTION_REASON = "no_executable_action"

#: legacy 反推出来的「模型自己说够了」。v2 闭集里没有同义项——v2 会把它进一步
#: 分成 model_sufficient / model_partial(要读方面账才分得出来),legacy 轨迹里
#: 没有方面账,所以只能停在这个更粗的码上。
TERMINATION_MODEL_END = "model_end"
#: legacy 反推出来的「反思调用失败后 fail-open 兜底收尾」。与 v2 闭集同一个码
#: (逐字抄自 `app.domain.retrieval_termination.TERMINATION_MODEL_DEGRADED`)——
#: 都是模型/JSON 调用本身出了问题,不是模型判断证据够/不够。fail-open 兜底会把
#: `next_action` 写成 `answer`(见 `reasoning_retrieval._reflect_fallback`),
#: 与真正的「模型自己说够了」在 `next_action`/`sufficient` 这两个字段上完全
#: 同形;legacy 轨迹里唯一能把两者分开的信号,是末尾 reflect 步 detail 自己带
#: 没带 `fallback_reason` 键。
TERMINATION_MODEL_DEGRADED = "model_degraded"
#: 投影可能写出的全部 termination 码:v2 的闭集(逐字抄自
#: `app.domain.retrieval_termination.TERMINATION_REASONS`,由用例钉住)+ 上面
#: 那个 legacy-only 的粗码。
TERMINATION_REASON_VALUES: tuple[str, ...] = (
    "model_sufficient", "model_partial", "step_budget", "stale",
    "no_executable_action", TERMINATION_MODEL_DEGRADED, "retrieval_degraded",
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

#: `context_chars` 的短码 → reflect 步 detail 里的键名。
#:
#: ⚠ **`bytes_total` 的单位和另外五个不一样。** `s/c/k/d/t` 是五个上下文块各自的
#: **字符**数(`ctx_chars_*`),`bytes_total` 是整条 provider-facing 消息的**字节**
#: 数(`ctx_bytes_total`)。字符数与字节数在中文上差三倍,所以它既不是前五项之
#: 和、也不该拿去和它们比大小。短码里带上 `bytes_` 就是为了让这件事在读表时不必
#: 回来查注释——原名 `total` 会被顺手读成「总字符数」(评审后改名)。
#:
#: 它**能不能**当 `prefix_bytes_*` 的分母(「这一轮 N 字节里有多少落进了公共
#: 前缀」),取决于写侧让 `ctx_bytes_total` 等于什么:只有它等于
#: `len(serialize_provider_messages(provider_messages(...)))`——即这一轮最终消息
#: 的全部字节,含 wrapper 与帧开销——那道除法两边才同为字节、同一个口径。写侧只
#: 统计各块正文之和(漏掉 wrapper 与帧)的话,分子里的公共前缀含着分母没算的
#: 字节,比例甚至可能大于 1;那时这个分母不成立,别做那道除法。这条等式记在计划
#: §3 T-PS3 里,由写侧负责。
#:
#: ⚠ **五个短码是稳定性类别,不是块标题——两条臂同形同尺。** 读这五个数的人容易
#: 按 `prefix_snapshot` 的块名去认它们("问题与契约" / "本轮当前状态"),于是以为
#: `off` 行里的同一个短码指着另一段文本。实际上 `S/K/D` 直取(system 段 / 证据卡 /
#: 观察账),而 `C` 与 `T` 是**差值**:`C` = user 段减去整个材料块,`T` = 材料块减
#: 去 K 与 D。所以 `off` 行的 `ctx_chars_c` 同样非零(问题原文与引号规则),而它的
#: `ctx_chars_t` 量的是那块**排在最前**的服务器状态摘要(P 臂里同一格排在最末,并
#: 且多了本轮动作清单)——位置不同,量的是同一类东西。两条臂因此可以直接比大小。
#:
#: 短码集合由**本模块**定义,计划 §3 T-PS4 钉住的只是 detail 侧那几个键名
#: (`ctx_chars_*` / `ctx_bytes_total`)。要改短码就同 diff 改掉读侧全部消费点。
REFLECT_CONTEXT_DETAIL_KEYS: Mapping[str, str] = {
    "s": "ctx_chars_s",
    "c": "ctx_chars_c",
    "k": "ctx_chars_k",
    "d": "ctx_chars_d",
    "t": "ctx_chars_t",
    "bytes_total": "ctx_bytes_total",
}

#: reflect 步 detail 上的**稀疏**测量键全集(计划 §3 T-PS3 写入,T-PS4 读)。
#:
#: 稀疏 = 只有 v2 且 `REASONING_REFLECT_MEASURE_CONTEXT` 打开的 run 才会出现。
#: `legacy` 与 `off`(测量关)的 reflect 步 detail 里一个都不该有——这一点由
#: `test_reasoning_trace_stats.py` 的冻结基线用例从读侧钉住:它们全缺席时,本
#: 模块新增的那几列必须逐个是 `None`,而不是 0。
#:
#: `cards_shown` / `cards_omitted` / `call_wall_ms` 登记在这里但**没有**对应的
#: 顶层投影列:逐轮细节按计划 §5 Q6 只留在 rig 的 per-call 表里,投影这一层只
#: 承接能压成一个 run 一格的量。登记它们是为了让「写侧有哪些键」在读侧也有一份
#: 可查的清单,而不是让下一个人从 T-PS3 的代码里反推。`delta_blocks`(本轮消息
#: 里 D 的块数,PR-3 计划 §2 拍板 Q2)同组:登记但不进 `RUN_PROJECTION_KEYS`。
#:
#: `context_rebuilds` / `context_fallback`(PR-3 计划 §3 T-PD2)是这批稀疏键里
#: 的两个例外:稀疏键的通例是「只有 v2 且测量开关打开的 run 才会出现」,但按
#: 计划 §2 拍板 Q7,这两键在 `prefix_delta` 臂下**无条件**随 `ReflectMeasurement`
#: 出现——回退与重建是行为事实,测量关只关字节序列化那半,不关这两个计数本身。
#:
#: `assessment_rows` / `assessment_absent`(PR-4 计划 §3 T-PL2 拍板 Q8)回到
#: 普通稀疏键的通例:门是测量开关(`measures_messages`),不是行为事实,所以
#: 不享有上面那条"无条件"待遇。它们与 `context_rebuilds`/`context_fallback`
#: 的另一处不同是**臂的宽窄**:这两个键**四臂通写**(`off`/`prefix_snapshot`/
#: `prefix_delta`/`prefix_delta_lean`,只要测量打开就都有),不是
#: `prefix_delta_lean` 专属——`assessment_rows` 是本轮实际落账的方面自评行数
#: (`len(outcome.accepted)`,整份越界写 0),`assessment_absent` 是这一轮载荷
#: 压根没带 `assessment` 键;四臂通写是为了让 D↔L 这一对在同一把尺子上比
#: "省了多少重述"。
REFLECT_MEASUREMENT_DETAIL_KEYS: frozenset[str] = frozenset({
    *REFLECT_CONTEXT_DETAIL_KEYS.values(),
    "message_prefix_bytes",
    "cards_shown",
    "cards_omitted",
    "call_wall_ms",
    "call_attempts",
    "response_chars",
    "context_rebuilds",
    "context_fallback",
    "delta_blocks",
    "assessment_rows",
    "assessment_absent",
})


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
    """数值 → `int`;非数、`bool`、**负数**一律 `None`(= unknown)。

    负数当缺失,是因为这个模块读的每一个整数都是计数、字节数或毫秒数,没有一个的
    定义域含负值。写侧真给出一个 `-1`(哨兵值、减法算反了、时钟回拨),那是一次
    观测**失败**而不是一个观测:放它进去,`sum` / `min` / 中位数会得出一个物理上
    不可能的数并一路进报表;当缺失至少会在那一列的 `n_observed` 上如实少一格。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw < 0:
        return None
    return int(raw)


def _float(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _bool(raw: object) -> bool | None:
    """严格布尔读取器,`_int` 的孪生:只收 `bool`,其余(含 0/1、字符串)一律
    `None`(= unknown)。收 0/1 不会被读侧的隐私守卫拦下——`_is_scalar_value`
    本来就放行 `int`,`_reflect_context_fallback` 的 `any()` 也会把整数结果
    归一成 `bool`,两道都拦不住写侧手滑传进来的整数。真正的风险是:一旦这里
    宽松收 int,`context_fallback` 会把「没量到」(unknown)悄悄伪造成
    「量到了、答案是 0/1」这一次观测,而不是被拦下报错。"""
    return raw if isinstance(raw, bool) else None


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
    """来源数 → 闭集桶。`None` / 非数 / 负数 → unknown(不是 `1`)。

    负数由 `_int` 统一折成 `None`,这里不再单独判一次。
    """
    value = _int(count)
    if value is None:
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


def citation_contribution(steps: Sequence[Mapping]) -> tuple[dict, int | None]:
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
    # 锚点集合不可信(只跑检索的 search run 没有 synthesis 步;或带截断标)时,
    # 每一步都绕过了共享命中的记账——此时 0 不是观测值,是「没法数」
    # (codex #700 R18 P2):回 None,与 `anchors`/`cited_hits` 的缺失口径一致。
    return contribution, (shared_hits if usable else None)


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


def _reflect_fallback_reason(step: Mapping) -> str | None:
    """一条 reflect 步的 `fallback_reason`,不是 reflect 步就是 `None`。

    这是**模型兜底**唯一的真源(`reasoning_retrieval._reflect_fallback` 写的
    那个键)。`step_type == "fallback"` 是另一件事——那是 `search_elements`
    补查原文的路由决策(初检索空手后的动作,见同文件
    `not (collected or elements or chunks)` 那处 `record`),与「反思调用失败
    后 fail-open 收尾」毫无关系,只是恰好撞了同一个英文词。原因码本身透传、
    不闭集(生产侧的码见 `_reflect_fallback_reason` 的 docstring,如
    `provider_unavailable`/`invalid_enum`/`malformed_response`),空字符串按
    「没有」处理而不是折成 unknown——没有这个键就压根不是兜底步。
    """
    if step["step_type"] != "reflect":
        return None
    raw = step["detail"].get("fallback_reason")
    return raw if isinstance(raw, str) and raw else None


def infer_legacy_termination(
    steps: Sequence[Mapping], effort: str, *, step_ceiling: int | None = None,
) -> str | None:
    """legacy 轨迹的结束原因反推。返回 `None` = unknown。

    `step_ceiling` 是调用方**已知的有效**反思轮上限(报告逐节深挖:引擎传
    `max_steps=depth`,检索器取 `min(depth, 档位上限)`;codex #700 R10 P2)。
    给了就用它,不给才退回档位表 `MAX_REFLECT_STEPS[effort]`——一份 depth=2 的
    报告两轮就停,按 standard 档的 8 轮上限去判会得到 unknown 而不是 step_budget。

    次序刻意让**记录下来的事实**先于推断:熔断步、「无可执行动作」步、以及末尾
    reflect 步自己 detail 里的 `fallback_reason` 都是执行处当场写下的,读到就
    是它;只有这些都没有时才去读末尾 reflect 的「模型说够了」决定,再不行才用
    「reflect 轮数已经顶到档位上限」这条最弱的推断。`fallback_reason` 必须排在
    `next_action == "answer"` 之前判——fail-open 兜底本身就会把 `next_action`
    写成 `answer`(`reasoning_retrieval._reflect_fallback`),不优先读它就会把
    「反思调用失败后兜底收尾」误判成「模型自己说够了」。四条判据在真实轨迹里
    互斥(熔断/无动作/兜底都会让当轮 reflect 的决定不是模型真判定),排序因此
    不改变结论,只固定了「同一条 run 只算一次」。
    """
    if _has_skip_reason(steps, STALE_BREAKER_REASON):
        return "stale"
    if _has_skip_reason(steps, NO_EXECUTABLE_ACTION_REASON):
        return NO_EXECUTABLE_ACTION_REASON
    reflects = [step for step in steps if step["step_type"] == "reflect"]
    if reflects:
        last = reflects[-1]
        if _reflect_fallback_reason(last) is not None:
            return TERMINATION_MODEL_DEGRADED
        detail = last["detail"]
        if detail.get("next_action") == "answer" or detail.get("sufficient"):
            return TERMINATION_MODEL_END
    ceiling = step_ceiling if step_ceiling is not None else MAX_REFLECT_STEPS.get(effort)
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


# --- reflect 前缀复用测量(计划 §3 T-PS4) ----------------------------------


def _reflect_steps(steps: Sequence[Mapping]) -> list[Mapping]:
    return [step for step in steps if step["step_type"] == "reflect"]


def _reflect_attempts(
    reflects: Sequence[Mapping],
) -> tuple[int | None, bool | None]:
    """→ (`model_calls_real`, `attempts_observed`)。

    三条口径,都是「不许把缺失读成一个数」的同一件事:

    1. **一条 reflect 步都没带 `call_attempts`** ⇒ `(None, None)`。旧轨迹、
       legacy run、测量关的 `off` run 全落这里;`0` 会被读成「一次模型都没调」,
       而 legacy 明明调了。没有 reflect 步的 run(chunk 模式)同样落这里。
    2. **只带了一部分** ⇒ `(None, False)`。手上的和是几步的部分和,不是这次
       run 的真实请求数,报出去就是一句偏低的假话;但「观测不全」这件事本身
       是真的,所以 `attempts_observed` 出 `False` 而不是跟着 unknown。
    3. **每一条都带了** ⇒ `(sum, True)`。

    `attempts_observed` 因此**只**回答一件事:「每一条 reflect 步都带了这个观测
    吗」。它与 `trace_truncated` 解耦(评审 P1)。曾经的写法是「全带了但轨迹带
    截断标 ⇒ `False`」,那是借错了标:`_truncated` 判的是某一步自己的
    `result_ids` / `anchor_evidence_ids` 被 `app.models.ask.TRACE_RESULT_IDS_MAX`
    那条 20 条上限截了,**不是**轨迹少了几轮——两条读路(`ask_trace_steps` 子表
    与 legacy 的整列 JSON)都是整步读、整步给,截断只发生在一步内部的 id 列表
    上。拿它去否证「每轮都量到了 `call_attempts`」,会把一条观测完整的 run 报成
    观测不全,而那一列本该是聚合侧判「`model_calls_real` 是真值还是下界」的唯一
    依据。轨迹自身完不完整仍由 `trace_truncated` 单独回答,两列各说各的事。
    """
    values = [_int(step["detail"].get("call_attempts")) for step in reflects]
    present = [value for value in values if value is not None]
    if not present:
        return None, None
    if len(present) != len(values):
        return None, False
    return sum(present), True


def _reflect_sum(reflects: Sequence[Mapping], key: str) -> int | None:
    """全部 reflect 步都带 `key` 才求和,任一步缺 ⇒ unknown。

    与 `_reflect_attempts` 同一条口径,只是不额外报「全不全」:一步不缺就出和,
    缺一步就 unknown。读的人要判这个和是不是整 run 的真实总量,看同一行的
    `trace_truncated`(轨迹本身有没有被切过)。
    """
    values = [_int(step["detail"].get(key)) for step in reflects]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _reflect_context_rebuilds(reflects: Sequence[Mapping]) -> int | None:
    """→ `context_rebuilds`:各 reflect 步 `context_rebuilds` 的**最大值**。

    max-over-present,同 `_stale` 的口径——`context_rebuilds` 是一次 run 内单调
    不减的运行期计数,不是每步各自独立、要求全带齐才敢求和的观测量(那是
    `_reflect_sum` 的口径,`response_chars_total` 那类「逐步各自贡献一段」才适
    用)。过滤掉缺席的步之后取最大值;全部步都缺 ⇒ `None`(= unknown,不是 0)。
    """
    values = [
        value for value in (
            _int(step["detail"].get("context_rebuilds")) for step in reflects
        )
        if value is not None
    ]
    return max(values) if values else None


def _reflect_context_fallback(reflects: Sequence[Mapping]) -> bool | None:
    """→ `context_fallback`:任一 reflect 步为真 ⇒ 真。

    回退按计划 §2 拍板 Q4 不可逆——本 run 剩余轮全部回退,所以「发生过一次」
    与「一直在发生」是同一件事,`any()` 就是全部信息。带这个观测的步一步都
    没有 ⇒ `None`,不是 `False`:那是「没量」,不是「量到了、答案是没回退」。
    """
    values = [
        value for value in (
            _bool(step["detail"].get("context_fallback")) for step in reflects
        )
        if value is not None
    ]
    return any(values) if values else None


def _reflect_assessment(
    reflects: Sequence[Mapping],
) -> tuple[int | None, bool | None]:
    """→ (`assessment_rows_total`, `assessment_observed`)(PR-4 计划 §3 T-PL2
    质量评审修正:P2-2)。

    **sum-over-present**,同 `_reflect_context_rebuilds` 的口径,**不是**
    `_reflect_sum` 的「任一步缺 ⇒ 整列 unknown」。理由是写侧的一个真实缺口:
    provider fail-open 的那一轮不进 `_absorb_assessment`
    (`reasoning_retrieval.py` 的 `if not decision.fallback: decision =
    self._absorb_assessment(...)`),所以该轮的 reflect 步 detail 压根不带
    `assessment_rows` 这个键——但它仍然是一条正常记的 reflect 步,不是「轨迹缺了
    一轮」。若沿用 `_reflect_sum`,一次偶发的 provider 抖动(生产实测 118 次调用
    24 次正文为空,不是边角)就会把整条 run 的这一列读成 unknown,把它从 D↔L
    「省了多少重述」的配对样本里整条挤出去。所以改成过滤掉缺席的步之后求和;
    `assessment_observed` 是那一件"全不全"的伴生披露,用它披露不全,而不是让
    整列 unknown——照 `attempts_observed`(`_reflect_attempts`)的先例:

    1. 一步都没带 ⇒ `(None, None)`——没有这个观测,不是零。
    2. 部分带 ⇒ `(sum(present), False)`——和是**真实**的、不是下界:
       `assessment_rows` 是逐步各自贡献的一段(这一轮落账了几行),不是运行期
       单调计数,present 的那些值本身就是真值,只是没有覆盖到全部轮次。
    3. 全带齐 ⇒ `(sum(present), True)`。
    """
    values = [_int(step["detail"].get("assessment_rows")) for step in reflects]
    present = [value for value in values if value is not None]
    if not present:
        return None, None
    return sum(present), len(present) == len(values)


def _aspects_unassessed(steps: Sequence[Mapping]) -> int | None:
    """→ `aspects_unassessed`:终态 skip 步 detail 上「模型收尾时一次都没判断
    过的方面数」(计划 PR-4 T-PL2 拍板 Q5/Q9)。

    读点与 `_v2_termination` 同一条 skip 步(`reason == TERMINATION_SKIP_REASON`
    ——一次 run 只会记一条),不是任意一步:那是唯一记录终态事实的地方。v2 全部
    四臂无条件写这个键(拍板 Q10 已知偏离,legacy 没有方面账,恒 `None`);用
    `_int` 而不是 `bool`/`len` 兜底,是为了让 `0`(问过了、没有遗漏)与缺席
    (没量到/legacy/未到终态)分得开——`0` 不能被读成"没有这个观测"。
    """
    for step in steps:
        if (step["step_type"] == "skip"
                and _reason(step["detail"]) == TERMINATION_SKIP_REASON):
            return _int(step["detail"].get("aspects_unassessed"))
    return None


def _context_chars(reflects: Sequence[Mapping]) -> dict[str, int] | None:
    """各上下文块的**最后一轮**规模。一格都没有 ⇒ `None`(不是空字典)。

    取最后一轮而不是求和或取均值:这几个数是「一次调用的上下文有多大」,跨轮
    相加得到的是一个谁也没见过的数。逐码独立取:某一码在最后一轮缺席、在更早
    的轮里有,就用那个更早的值——写侧一旦开了测量就每轮全写,真出现逐码参差
    只可能是轨迹被切过,此时保住看得见的观测比对齐轮次更有用。
    """
    latest: dict[str, int] = {}
    for step in reflects:
        detail = step["detail"]
        for code, key in REFLECT_CONTEXT_DETAIL_KEYS.items():
            value = _int(detail.get(key))
            if value is not None:
                latest[code] = value
    ordered = {
        code: latest[code]
        for code in REFLECT_CONTEXT_DETAIL_KEYS
        if code in latest
    }
    return ordered or None


def _prefix_bytes(
    reflects: Sequence[Mapping],
) -> tuple[int | None, int | None, int | None]:
    """→ (`prefix_bytes_median`, `prefix_bytes_min`, `prefix_turns`)。

    计划 §5 Q6:逐轮前缀字节**不进**闭集投影(一条 run 的轮数不定,进来就是一
    个变长列表),退成中位数/最小值/轮数三格;逐轮细节留在 rig 的 per-call 表。

    首轮没有可比的上一轮,写侧记 `None`,这里不计入 —— 所以 `prefix_turns` 是
    「有前缀可算的轮数」= reflect 轮数 − 1(测量全程开着时),不是 reflect 轮数。
    一格都没有 ⇒ 三个都 `None`,`prefix_turns` 尤其不折 0:0 会被读成「量过了,
    一轮都没复用」。

    中位数取**最近秩**(偶数条取偏小的那个),不取两数平均:平均出来的字节数不
    是任何一轮真实的前缀长度。秩的算法与 `scripts/analyze_reasoning_trace.py`
    的 `_nearest(values, 0.50)` 逐字等价(`ceil(0.5n) - 1 == (n - 1) // 2`),
    两处对同一批数不会给出两个「中位数」。
    """
    values = sorted(
        value for value in (
            _int(step["detail"].get("message_prefix_bytes"))
            for step in reflects
        )
        if value is not None
    )
    if not values:
        return None, None, None
    return values[(len(values) - 1) // 2], values[0], len(values)


def _optimization(tags: Mapping) -> str:
    """rig 声明的第二维臂。缺席 / 闭集外 ⇒ unknown。

    **只从 `rig_tags` 读。** 曾经还有一条 `answer_payload` 兜底分支,那是一个没有
    写侧、也没有用例的猜测:`AskResponse` 从来不带这个字段,线上导出永远走不到它;
    而它顺手把「tags 里给了个空串」这种传参错误遮蔽成「那就去 payload 找找」,于是
    一次错误的声明会静默落成 unknown 而看不出是谁的错。删掉之后这一列只有一个
    产地:rig 从 `client_request_id` 解码出来的标签。

    `_closed_exact` 而不是 `closed_value`:后者会先 `lower()`,那会把一个大小写
    写错的声明(`Prefix_Snapshot`)悄悄纠正成合法臂,于是「rig 传错了参数」这件
    事在数据里看不出来。线上导出不写这个标签 ⇒ 恒 unknown。
    """
    return _closed_exact(tags.get("optimization"), OPTIMIZATIONS)


# --- run 级投影 -------------------------------------------------------------


def _consumer(steps: Sequence[Mapping]) -> str:
    for step in steps:
        if step["step_type"] != "synthesis":
            continue
        detail = step["detail"]
        if "section_total" in detail or _int(detail.get("outline_sections")):
            return "ask_sectioned"
    return "ask_single"


def _is_kg_shaped_retrieve(step_type: str, detail: Mapping) -> bool:
    """`retrieve` 步是不是「图有产出」的正面证据。

    只有 `new`/`found`(补种 / `add_subquery` 的方向级检索,写侧只在实际有
    命中时才写这两个键之一,且值只计 KG 候选)算数;首轮"初检索"步只写
    `count`(图与原文混合抓取的粗计数,分不清就不算,§3);带
    `_GRAPHLESS_RETRIEVE_MARK` 的步是无图原文半自己的印记——它一出现,这一步
    的 `new` 就只可能是空手的图查询,不能倒过来当「图在场」证据。
    """
    if step_type != "retrieve" or _GRAPHLESS_RETRIEVE_MARK in detail:
        return False
    for key in ("new", "found"):
        value = _int(detail.get(key))
        if value is not None and value > 0:
            return True
    return False


def _kg_in_scope(
    steps: Sequence[Mapping], payload: Mapping, mode: str,
) -> bool | None:
    """图是否在这一轮的检索范围内(§3)。只认**正面证据**。

    `AskResponse.kg_required` 默认 `False` 且总被序列化——早退 / chunk 轨迹
    会因此被误判成「不在范围」以外的东西都读不出来,所以它**不再**单独把结果
    判成 `True`;`kg_required=True` 仍是「这条库没图」的另一种写法,继续叠加
    进 `False` 的判据。
    """
    if any(
        step["step_type"] == "skip"
        and _reason(step["detail"]) in KG_UNAVAILABLE_REASONS
        for step in steps
    ):
        return False
    if payload.get("kg_required") is True:
        return False
    if mode in ("reasoning", "auto→reasoning") and any(
        step["step_type"] in KG_SHAPED_STEP_TYPES
        or _is_kg_shaped_retrieve(step["step_type"], step["detail"])
        for step in steps
    ):
        return True
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
    """`fallback_count`/`fallback_reasons` 只数**模型兜底**(reflect 步 detail
    里带 `fallback_reason` 键),不数 `search_elements` 那个同名的 `fallback`
    step_type——那是初检索空手后的路由决策,继续像以前一样只在
    `actions_by_type` 里以 `fallback` 计(见 `_reflect_fallback_reason`)。
    """
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
        fallback_reason = _reflect_fallback_reason(step)
        if fallback_reason is not None:
            fallbacks[fallback_reason] += 1
        if step_type in NON_ACTION_STEP_TYPES:
            continue
        key = _action_key(step_type, step["seed"])
        sequence.append(key)
        (seeds if step["seed"] else actions)[step_type] += 1
        if step["count"] == 0:
            empty[key] += 1
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
    step_ceiling: int | None = None,
) -> dict:
    """一次 Ask run → 一行闭集投影(§3)。

    `step_ceiling`:调用方已知的有效反思轮上限,透传给 legacy 终态推断(报告
    逐节深挖用;Ask 不传,按档位表)。

    `job_row` 只被读 `mode` / `status`;`answer_payload` 只被读
    `mode` / `retrieval_effort` / `intent` / `kg_required`(有没有、是不是
    bool)——这四个字段就是这一层碰过的全部 payload 面,问题原文、答案正文、引用卡
    一律不碰,`optimization` 也**不**从 payload 读(见 `_optimization`)。
    `rig_tags` 是 rig 侧按 `client_request_id` 解码出来的编号(`corpus_cell` /
    `question_key` / `requested_mode` / `consumer` / `effort` / `trace_source` /
    `policy`(或 `policy_version`)/ `optimization`),线上导出传空 ⇒ 那几个维度
    恒为 unknown。
    """
    tags = rig_tags or {}
    payload = _mapping(answer_payload) or {}
    normalized = normalize_steps(steps)
    mode = _mode(job_row, payload, tags)

    v2_reason = _v2_termination(normalized)
    effort = closed_value(
        payload.get("retrieval_effort"), SITUATION_RETRIEVAL_EFFORTS
    )
    if v2_reason is not None:
        termination, inferred = v2_reason, False
    else:
        termination = infer_legacy_termination(
            normalized, effort, step_ceiling=step_ceiling
        )
        inferred = None if termination is None else True
    status = closed_value(job_row.get("status"), JOB_STATUSES)
    # 没跑到底的 run(failed/cancelled)没有协议证据,也常常没有落答案:按证据判
    # 会把 v2 的失败记成 legacy、档位记成 unknown,失败就从 v2 的对照列里消失
    # (codex #700 R10 P2)。这时用 rig 声明的标签(过闭集);跑成的 run 仍以证据
    # 为准,声明只补 unknown 不改证据。
    declared_policy = _closed_exact(
        tags.get("policy") or tags.get("policy_version"), POLICY_VERSIONS
    )
    if v2_reason is not None:
        policy_version = "v2"
    elif status in ("failed", "cancelled") and declared_policy != UNKNOWN:
        policy_version = declared_policy
    else:
        policy_version = "legacy"
    if effort == UNKNOWN:
        effort = closed_value(tags.get("effort"), SITUATION_RETRIEVAL_EFFORTS)
    breaker, stale_max = _stale(normalized)
    contribution, shared = citation_contribution(normalized)
    counters = _counters(normalized)
    total_ms = sum(counters["durations_ms"].values())
    intent = payload.get("intent")
    truncated = _truncated(normalized)
    reflects = _reflect_steps(normalized)
    model_calls_real, attempts_observed = _reflect_attempts(reflects)
    assessment_rows_total, assessment_observed = _reflect_assessment(reflects)
    prefix_median, prefix_min, prefix_turns = _prefix_bytes(reflects)

    row: dict[str, Any] = {
        # rig 的报告轨迹用同一条投影(逐节深挖跑的就是 `ReasoningRetriever.run`),
        # 但它的消费者不是 Ask,轨迹里也没有 synthesis 步可以据以判定,所以只能由
        # rig 显式声明。声明值仍过闭集:`consumer=<自由文本>` 进不来。
        "consumer": _closed_exact(tags.get("consumer"), CONSUMERS)
        if tags.get("consumer") else _consumer(normalized),
        "mode": mode,
        "effort": effort,
        "kg_in_scope": _kg_in_scope(normalized, payload, mode),
        "policy_version": policy_version,
        # payload 缺席(失败/取消前没落答案)时从轨迹回收:`intent` 步只在按确认
        # 后的契约开跑时才记(`ask_service._prepare_reasoning_ask` /
        # `_run_reasoning_stage`),读到它就是用了契约(codex #700 R16 P2)。否则
        # 失败 run 恒记 false,配对身份含此键后就与同契约的成功 run 配不上。
        "has_intent_contract": bool(intent) or any(
            step["step_type"] == "intent" for step in normalized
        ),
        "corpus_cell": _closed_exact(tags.get("corpus_cell"), CORPUS_CELLS),
        "question_key": str(tags.get("question_key") or "") or UNKNOWN,
        "notebook_bucket": source_bucket(sources_count),
        "status": status,
        "trace_source": closed_value(
            tags.get("trace_source") or "trace_steps", TRACE_SOURCES
        ),
        # `_reflect_steps` 已经把这一批筛出来了(前缀三格与 `call_attempts` 都读
        # 它),再数一遍就是两份「什么算一轮 reflect」的判据。
        "reflect_turns": len(reflects),
        "termination_reason": termination,
        "termination_inferred": inferred,
        "stale_breaker": breaker,
        "stale_max": stale_max,
        "aspects_total": _terminal_detail(normalized, "aspects_total"),
        "aspects_pending": _terminal_detail(normalized, "aspects_pending"),
        "aspects_undelivered": _terminal_detail(normalized, "aspects_undelivered"),
        "assessment_rejections": (
            _assessment_rejections(normalized) if policy_version == "v2"
            else None),
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
        "trace_truncated": truncated,
        # --- reflect 前缀复用测量(T-PS4) ---
        # 一次 run 的墙钟只有 rig 手上有(`total_ms` 是各步耗时之和,不含排队与
        # 步间空隙),导出这条路上恒 unknown;rig 拿到 `elapsed_ms` 后就地覆写。
        "run_wall_ms": None,
        "model_calls_real": model_calls_real,
        "attempts_observed": attempts_observed,
        "optimization": _optimization(tags),
        "context_chars": _context_chars(reflects),
        "prefix_bytes_median": prefix_median,
        "prefix_bytes_min": prefix_min,
        "prefix_turns": prefix_turns,
        "response_chars_total": _reflect_sum(reflects, "response_chars"),
        # --- reflect 前缀复用测量(PR-3 T-PD2;`prefix_delta` 专属) ---
        "context_rebuilds": _reflect_context_rebuilds(reflects),
        "context_fallback": _reflect_context_fallback(reflects),
        # --- reflect 前缀复用测量(PR-4 T-PL2) ---
        "assessment_rows_total": assessment_rows_total,
        "assessment_observed": assessment_observed,
        "aspects_unassessed": _aspects_unassessed(normalized),
    }
    row.update(counters)
    assert_closed(row)
    assert_projection_values(row)
    return row


def _assessment_rejections(steps: Sequence[Mapping]) -> int:
    """这次 run 里**逐方面**被拒的自评条数(一个方面一条)。

    ⚠ **口径与 `skip_reasons` 有意重叠,但两者数的东西不同,合计时不要相加。**
    逐方面被拒的自评**每轮只记一条 skip 步**(见
    `services.reasoning_retrieval._note_assessment_rejections`:一轮里 16 个方面
    全写错也只有一条,条数在 detail 的 `count` 里)。因此:

    * 逐方面拒了几个**方面** = 这一列(按 `count` 累加);
    * 逐方面拒过的**轮数** = `skip_reasons` 里后缀属于 `ASPECT_REJECTION_REASONS`
      的那几项之和;
    * 因为自评而**整轮作废**了几次 = `skip_reasons` 里全部 `invalid_assessment:*`
      之和 − 上一条。

    分成两列而不是换一套原因码,是为了让 T-BF7 前后的 `skip_reasons` 词面仍然
    连续,同时又能把「一个方面没被采纳」与「一整轮白烧」分开数——T-BF7 之前它们
    是同一个数,之后不是。

    `count` 缺席的行按 1 计:T-BF7 与本次评审修复之间落盘的轨迹是「一个方面一条
    skip」的旧形状,那时一条就是一个方面,按 1 计正好等价。

    legacy run 走不到这里(调用方按 `policy_version` 判),所以这里恒返回一个
    真实计数,不返回 unknown。
    """
    return sum(
        _rejection_count(step["detail"]) for step in steps
        if step["step_type"] == "skip"
        and _reason(step["detail"]) in ASSESSMENT_REJECTION_REASONS
    )


def _rejection_count(detail: Mapping) -> int:
    """一条逐方面拒绝 skip 步代表几个方面。旧形状(无 `count`)恒 1。"""
    raw = _int(detail.get("count"))
    return raw if raw is not None and raw > 0 else 1


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


#: rig 的 `search` 子命令(进程内**只跑检索**、不合成)写出的两个固定标签。
#: 那条路不经 Ask 的 durable job,轨迹里也没有 synthesis 步可以据以判定消费者,
#: 所以两者都只能由 rig 声明;声明值仍过 `project_run` 的闭集。
SEARCH_CONSUMER = "ask_single"
SEARCH_TRACE_SOURCE = "in_process"

#: `search` 行里因为**没跑合成**而恒为 unknown 的键。它们全部由合成/装配阶段
#: 写进 synthesis 步的 detail(锚点、进 prompt 的三类证据数、每动作引用贡献),
#: 一条 run 只检索不合成时它们**不存在**,不是「零」——0 会被读成「一条证据都
#: 没进 prompt / 一个锚点都没有」,那是一句关于合成的假话。
SYNTHESIS_ONLY_KEYS: tuple[str, ...] = (
    "anchors", "included_kg", "included_chunks", "included_elements",
    "citation_contribution",
)


def project_search_run(
    steps: Sequence[object],
    *,
    result: object,
    effort: str,
    policy: str,
    question_key: str,
    corpus_cell: str,
    kg_in_scope: bool | None,
    mode: str = "reasoning",
    has_intent_contract: bool = False,
    sources_count: object = None,
) -> dict:
    """rig 的**进程内检索 run** → 一行闭集投影(设计规格 §2.3 `search`)。

    与 Ask 导出走的是**同一条** `project_run`(检索跑的就是
    `ReasoningRetriever.run`,换一份投影只会让两边口径分叉);这里只补三件
    `project_run` 从轨迹里读不到、而进程内调用方手上有确凿事实的东西:

    1. **结束事实**。v2 的权威是 `result.termination` 这个 DTO 本身
       (`reason` / `unresolved_aspect_ids` / `unrecovered_channels` /
       `aspects`),不是轨迹里那条披露步——DTO 是构造期就过了闭集守卫的,而
       轨迹步只是它的一份渲染。`termination is None` 就是 legacy(那个字段
       **v2-only**,见 `_run_termination`),此时照 `project_run` 已经做过的
       `infer_legacy_termination` 反推,不二次加工。
       **方面账两个数也在这里补**:`project_run` 是从 synthesis/answer 步的
       detail 里读它们的,而那是 Ask 合成阶段写的,只检索的 run 里没有。
    2. **图在不在范围内**。调用方拿的是 `kg_in_scope_for` 的直接判定(一次
       EXISTS),比从轨迹形状反推的正面证据更硬,所以直接盖掉。
    3. **没跑合成的那几列**(`SYNTHESIS_ONLY_KEYS`)一律落 unknown。

    `policy` 是 rig 的**声明**,只在这里过一次闭集校验、**不进投影**:
    `policy_version` 只认这次 run 自己留下的证据。声明与证据不一致(声明 v2、
    却一个 `termination` 都没有)恰恰是最该被看见的那件事,把声明写进去等于
    把它抹平;让它响亮失败是调用方的事(rig 在第一个 run 之后就核对)。

    `steps` 必须是**已经归一成 dict 的**轨迹步(`TraceStep` 数据类不是
    Mapping,`normalize_steps` 解不了它),与 `_report_rows` 那条路同形。
    """
    if policy not in POLICY_VERSIONS:
        raise ValueError(f"unknown policy: {policy!r}")
    rows = list(steps)
    row = project_run(
        {"mode": mode, "status": "done"},
        rows,
        {
            "mode": mode,
            "retrieval_effort": effort,
            # `project_run` 只读它的真假(`has_intent_contract`)。契约内容是
            # 自由文本,这一层从头到尾不碰,所以这里放一个布尔而不是契约本身。
            "intent": True if has_intent_contract else None,
        },
        sources_count=sources_count,
        rig_tags={
            "consumer": SEARCH_CONSUMER,
            "trace_source": SEARCH_TRACE_SOURCE,
            "corpus_cell": corpus_cell,
            "question_key": question_key,
        },
    )
    row["kg_in_scope"] = kg_in_scope if isinstance(kg_in_scope, bool) else None
    termination = getattr(result, "termination", None)
    if termination is not None:
        row["policy_version"] = "v2"
        row["termination_reason"] = _closed_exact(
            getattr(termination, "reason", ""), TERMINATION_REASON_VALUES
        )
        row["termination_inferred"] = False
        row["aspects_total"] = len(getattr(termination, "aspects", ()) or ())
        row["aspects_pending"] = len(
            getattr(termination, "unresolved_aspect_ids", ()) or ()
        )
        row["unrecovered_channels_count"] = len(
            getattr(termination, "unrecovered_channels", ()) or ()
        )
    if not _has_synthesis_step(rows):
        for key in SYNTHESIS_ONLY_KEYS:
            row[key] = None
    assert_closed(row)
    assert_projection_values(row)
    return row


def _has_synthesis_step(steps: Sequence[object]) -> bool:
    """轨迹里有没有 synthesis 步。判据与 `normalize_steps` 同一个解码点。"""
    for raw in steps:
        step = _mapping(raw)
        if step is not None and _step_type(step) == "synthesis":
            return True
    return False


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
    assert_projection_values(row)
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


#: 「短码」字符串的字符集与长度上限。`action_seq` / `corpus_cell` /
#: `question_key` 这类编号都落在这个集合里;一句人写的话(空格、中文标点、
#: 换行)进不来。长度上限只是再加一道保险——短码不该无限长。
#: `→` 是唯一放行的非 ASCII 字符:`MODES` 的闭集成员 `auto→chunk` /
#: `auto→reasoning`(§3)拿它当分隔符,是词表本身的一部分,不是自由文本。
_SHORT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_:\-.+→]+$")
_SHORT_CODE_MAX_LEN = 64


def _is_short_code(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _SHORT_CODE_MAX_LEN:
        return False
    if any(ch.isspace() for ch in value):
        return False
    return _SHORT_CODE_PATTERN.match(value) is not None


def _is_scalar_value(value: object) -> bool:
    """标量:`bool`/`int`/`float`/`None`,或一个短码字符串。"""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    return _is_short_code(value)


def _is_numeric_leaf(value: object) -> bool:
    """字典的「值」半只到数值这一层——`bool`/`int`/`float`/`None`,不再放行
    字符串(字符串值属于短码标量,走的是 `_is_scalar_value` 那条分支)。"""
    return value is None or isinstance(value, (bool, int, float))


def assert_projection_values(row: Mapping) -> None:
    """投影行的**结构性**值校验。

    `assert_closed` 只挡「多了一个闭集外的键」,挡不住「往一个允许的键里塞
    一段自由文本值」——例如新写一处直接把 `scope: "看 Qwen-VL 和 DeepSeek"`
    塞进某个允许的键。这里逐值校验**形状**,不看键名,所以自由文本不管挂在
    闭集内哪个键下都会被拦住。

    允许的值形状(闭集,不允许别的):

    - 标量:`bool` / `int` / `float` / `None`,或一个短码字符串(≤ 64 字符、
      不含空白、只由 ``[A-Za-z0-9_:\\-.+]`` 组成);
    - 短码列表(`action_seq`):每一项都是短码字符串;
    - 短码 → 数值字典(`actions_by_type` / `skip_reasons` / `durations_ms`
      这类计数表):键是短码,值是 `bool`/`int`/`float`/`None`;
    - 短码 → {短码 → 数值} 两层字典:`citation_contribution` 的形状。
    """
    for key, value in row.items():
        _assert_projection_value(key, value)


def _assert_projection_value(key: str, value: object) -> None:
    if _is_scalar_value(value):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            if not _is_short_code(item):
                raise ValueError(
                    f"projection value at {key!r} carries a non-short-code "
                    f"list item: {item!r}"
                )
        return
    if isinstance(value, Mapping):
        for inner_key, inner_value in value.items():
            if not _is_short_code(inner_key):
                raise ValueError(
                    f"projection value at {key!r} carries a non-short-code "
                    f"dict key: {inner_key!r}"
                )
            if _is_numeric_leaf(inner_value):
                continue
            if isinstance(inner_value, Mapping):
                for leaf_key, leaf_value in inner_value.items():
                    if not _is_short_code(leaf_key):
                        raise ValueError(
                            f"projection value at {key!r}.{inner_key!r} "
                            f"carries a non-short-code dict key: {leaf_key!r}"
                        )
                    if not _is_numeric_leaf(leaf_value):
                        raise ValueError(
                            f"projection value at {key!r}.{inner_key!r}."
                            f"{leaf_key!r} is not a scalar number: "
                            f"{leaf_value!r}"
                        )
                continue
            raise ValueError(
                f"projection value at {key!r}.{inner_key!r} is neither a "
                f"number nor a nested short-code dict: {inner_value!r}"
            )
        return
    raise ValueError(
        f"projection value at {key!r} has an unsupported shape: {value!r}"
    )
