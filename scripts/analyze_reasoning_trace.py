#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 离线聚合:闭集投影 JSONL → markdown 表 + JSON 明细。

设计真源 §2.2/§4:
`docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md`

零 DB、零模型、零网络:它只吃 `scripts/export_reasoning_traces.py` 与
`scripts/reflect_shadow_rig.py` 写出的 JSONL。除标准库外只 import 两处**闭集
常量**(用来把非法键挡在聚合之外):`app.domain.reasoning_trace_stats` 的 T0
键集与词表,以及 `app.eval.reflect_ab` 的 `AB_PROJECTION_KEYS`(那个模块自己也
是纯逻辑、零 I/O、零模型)。

    python scripts/analyze_reasoning_trace.py a.jsonl b.jsonl \
        --group-by consumer,policy_version,effort --out-md t0.md --out-json t0.json

四条纪律,和导出侧同源:

* **unknown 是一等值**:每个指标各报自己的 `n_observed`,缺失不折成 0。
* **小样本不出分位数**:`n_observed < --min-samples` 的格子只报计数。
* **对照成对**:legacy 与 v2 只在 `question_key` + `corpus_cell` + `effort`
  三者相同时配对(§4.2);线上导出的 legacy 没有 `question_key`,只进基线表。
  臂有**两条轴**(`ARM_DIMENSIONS`):`policy_version`(legacy/v2)与
  `optimization`(`off` / 前缀复用各变体),各出一张成对表,不合成四象限。
* **默认参数下的输出是一份合同**:`--baseline-arm` / `--pair-rows` /
  `--key-set` 三个开关(PR-5 T-EX9)各自的默认值逐字保持接入前的行为——同一份
  行喂进来,JSON 与 markdown 逐字节相同。存档过的报告因此仍与新报告可 diff。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.domain.reasoning_trace_stats import (  # noqa: E402
    OPTIMIZATIONS,
    RUN_PROJECTION_KEYS,
    UNKNOWN,
)
from app.eval.reflect_ab import AB_PROJECTION_KEYS  # noqa: E402

# `optimization` 在默认分组里,而不是只当一个可选维度:一条 run 的身份是
# `(policy_version, optimization)` 这一对(T-PS4)。不按它分格,`off` 与
# `prefix_snapshot` 两臂会被摊进同一格的均值里——那个均值不描述任何一臂。
# 线上导出恒不写这个键,那时整批都落在 `unknown` 一格,分组结果与从前逐字相同。
DEFAULT_GROUP_BY = (
    "consumer,mode,effort,kg_in_scope,policy_version,optimization"
)

#: 数值指标:报 n_observed / 均值 / P50 / P95。
NUMERIC_METRICS: tuple[str, ...] = (
    "reflect_turns", "trace_steps", "total_ms", "fallback_count",
    "shared_hits", "stale_max", "anchors",
    "candidates_kg", "candidates_chunks", "candidates_elements",
    "included_kg", "included_chunks", "included_elements",
    "aspects_total", "aspects_pending", "aspects_undelivered",
    # 「这次 run 有几个方面的自评没被服务端采纳」(v2-only,legacy 恒 unknown ⇒
    # 落进 n_missing 而不是被当成 0)。它与 `skip_reasons` 里的
    # `invalid_assessment:*` 不是同一个数:那边数轮,这边数方面。
    "assessment_rejections",
    "unrecovered_channels_count",
    "section_total", "report_depth", "attempted", "attempted_failed",
    "top_relevance",
    # --- reflect 前缀复用测量(T-PS4) ---
    # `run_wall_ms` 只有 rig 写(导出侧恒 unknown ⇒ 落进 n_missing);
    # `model_calls_real` 与 `total_ms` 不是同一件事,前者数请求、后者数毫秒;
    # 三格前缀聚合各报自己的 n_observed —— 一批里只有一部分 run 开了测量时,
    # 「有几条 run 真的量到了前缀」本身就是要看的第一个数。
    "run_wall_ms", "model_calls_real",
    "prefix_bytes_median", "prefix_bytes_min", "prefix_turns",
    "response_chars_total",
    # `prefix_delta` 专属(T-PD2):一次 run 里各 reflect 步「重建了几次 K」的
    # 最大值。`0` 是「跑了 delta、一次都没重建」的真观测,不是 unknown——投影侧
    # 已经把两者分开,这里只是接上聚合的 n_observed/均值/分位数。
    "context_rebuilds",
    # --- reflect 前缀复用测量(T-PL2) ---
    # 各 reflect 步「本轮实际落账的方面自评行数」之和(拍板 Q8),四臂通写:
    # `off`/`prefix_snapshot`/`prefix_delta` 下模型仍在重述全量,`0` 只在
    # `prefix_delta_lean` 下才常态出现——把它摆在同一组数值指标里,才看得出
    # D↔L 这一对「省了多少重述」。
    "assessment_rows_total",
    # v2 终态 skip 步 detail 上的「模型收尾时还剩几个方面没被它判断过」
    # (拍板 Q5/Q9)。四臂无条件写(Q10 已知偏离),`0` 是「问过了、没有遗漏」的
    # 真观测,legacy 恒 unknown ⇒ 落进 n_missing。
    "aspects_unassessed",
)
#: 布尔指标:报 n_observed 与 true 占比。
BOOLEAN_METRICS: tuple[str, ...] = (
    "stale_breaker", "trace_truncated", "has_intent_contract",
    "grounded", "failed",
    # 三值(True/False/None)照样进得来:`None` 不是 bool,所以它落进 n_missing
    # 而不是被当成 False——「这道题没声明范围」与「声明了但解析不到」因此分得开。
    # 线上导出恒不写这个键,那时整组的 n_observed 是 0,不影响任何别的指标。
    "scope_narrowed",
    # 三值同理:`True`=每一轮 reflect 都量到了 `call_attempts`、`False`=只量到
    # 一部分、`None`=压根没量。三者分得开才能判 `model_calls_real` 是不是全量:
    # `True` 时是,`False` 时那一列恒 unknown(投影侧不出部分和),`None` 时只该
    # 报 n_missing。它**不**含「轨迹有没有被截断」——那是 `trace_truncated` 单独
    # 回答的另一件事(截的是某一步的 id 列表,不是轮数)。
    "attempts_observed",
    # `prefix_delta` 专属(T-PD2):本 run 是否曾回退到 P 的有界选择——回退不可
    # 逆(计划 §2 拍板 Q4),`True` 就是「发生过」。`None` = 一条 reflect 步都没
    # 带这个观测(非 delta 臂/未测量),与「量到了、答案是没回退」的 `False`
    # 分得开。
    "context_fallback",
    # T-PL2 质量评审修正 P2-2:`assessment_rows_total` 那个和「全不全」。
    # `True` = 每一条 reflect 步都带 `assessment_rows`;`False` = 只带了一部分
    # (此时那个和**是**真实的部分和,不是下界,与 `attempts_observed=False`
    # 那一格的语义不同——见 `_reflect_assessment` 的口径说明);`None` = 一条
    # 都没带。provider fail-open 的那一轮不进 `_absorb_assessment`,靠这一列
    # 披露"缺了一轮",而不是让 `assessment_rows_total` 整列 unknown。
    "assessment_observed",
)
#: 枚举指标:报取值分布(含 unknown 一格)。
CATEGORICAL_METRICS: tuple[str, ...] = (
    "termination_reason", "termination_inferred", "evidence_level",
    "trace_source", "status",
    # 同时也是默认分组维度:格子内恒定,但单臂重跑/混批时这一格的分布能立刻
    # 看出「这批里到底有几条是哪个臂」。
    "optimization",
)
#: 「闭集键 → 计数」的字典指标:跨 run 逐键求和。
COUNTER_METRICS: tuple[str, ...] = (
    "actions_by_type", "seed_actions_by_type", "empty_actions_by_type",
    "skip_reasons", "fallback_reasons", "durations_ms",
    # 最后一轮各上下文块的规模(T-PS4)。和 `durations_ms` 一样按格**跨 run 逐键
    # 求和**。
    #
    # ⚠ **这一项的和不可以除 `n_runs`。** 它只覆盖开了测量的那几条 run(没开的
    # 一格都不带),而 `n_runs` 数的是整格;两者分母不同,商是一个偏低到没有意义
    # 的数。同格里「有几条 run 真的量到了」这个分母,四组标量指标里没有哪一格能
    # 精确顶替(`prefix_turns` 的 `n_observed` 少算首轮就停的 run,
    # `attempts_observed` 数的是另一个键),所以要看每 run 平均只有两条正路:按
    # `optimization` 分格后只看确实开了测量的那一臂,或者去 rig 的 per-call 表按
    # 轮取数。短码本身的单位另见 `REFLECT_CONTEXT_DETAIL_KEYS`(`bytes_total` 是
    # 字节,其余五个是字符)。
    "context_chars",
)
# 配对还要按**工作负载**分格(codex #700 R11 P2):`search-*.jsonl` 是只跑检索的
# 进程内 run(`trace_source=in_process`),导出的 Ask run 是检索+合成的完整 run;
# 同题同格同档的 legacy 检索 run 与 v2 完整 Ask run 摆在同一行,比的是检索耗时
# 对检索+合成耗时。`consumer` / `mode` / `trace_source` 三个键一起把它们分开。
# `has_intent_contract` 也是工作负载的一部分(codex #700 R15 P2):`--no-intent`
# 的 run 没有冻结契约,首轮查询规划与 v2 的方面账都不同,不能与带契约的 run 配对。
PAIR_DIMENSIONS: tuple[str, ...] = (
    "question_key", "corpus_cell", "effort", "consumer", "mode", "trace_source",
    "has_intent_contract",
)

#: 一条 run 的**臂身份**是这两个键的一对(T-PS4)。它们刻意都不在
#: `PAIR_DIMENSIONS` 里:配对的定义就是「同一格里两条只在臂上不同的 run」,把臂
#: 写进分格依据会让每一格只剩一条 run,一对都配不出来。
#:
#: 两条轴各自配一张表,而不是合成一个四象限:
#:
#: * `pair_table` 沿 `policy_version` 配(legacy vs v2)。**不**要求两侧
#:   `optimization` 相同——`optimization` 对 legacy 结构上不成立(v2 总闸关时
#:   `reflect_optimization()` 恒 `off`),要求相同就等于永远配不出
#:   「legacy vs v2+prefix_snapshot」这一对,而那恰恰是最终要看的那个对照。
#:   但 v2 那一侧**按 `optimization` 拆行**(legacy 侧整批重复):不拆就会把
#:   `off` 与 `prefix_snapshot` 两批 run 摊进同一个均值,那个均值不描述任何一
#:   臂。每一侧仍**如实报出自己的 `optimization` 分布**(见 `_pair_side`)——
#:   拆行后 v2 侧那一格恒只有一个键,即这一行的 v2 臂身份。
#: * `optimization_pair_table` 沿 `optimization` 配(`off` vs 各变体),此时
#:   `policy_version` 是**固定**的:拿 legacy 的 `off` 去比 v2 的
#:   `prefix_snapshot`,差值里混着两处改动,谁也归因不了。
ARM_DIMENSIONS: tuple[str, ...] = ("policy_version", "optimization")

#: 沿 `optimization` 配对时的**默认**基线臂。`off` 是「测量开着但一处优化都没上」
#: 的那一臂(计划 §5 Q2:测量开关与 `optimization` 正交)。
#:
#: 它是 `--baseline-arm` 的默认值,而不再是一处硬编码(PR-5 T-EX9)。硬编码的代价
#: 是实测到的:一批里只有 D(`prefix_delta`)与 L(`prefix_delta_lean`)两臂时
#: `off` 那一格空着 ⇒ 整张配对表全空,而 §10.2-4「若 P 与 D/L 的配对中位收益差
#: 不足 5% 则选 P」这条采用门恰恰要读 D↔L 这一对。
#:
#: 基线换成谁由操作者显式声明,报告的 JSON 里**不另存**一个「我用的基线是谁」的
#: 字段:基线那一侧已经如实报出自己的 `optimization` 分布(见 `_pair_side`),它
#: 就是这一行基线臂身份的自证;markdown 的节标题与列名从参数拼(见
#: `render_markdown`),所以换了基线的报告不会还印着 `off`。
OPTIMIZATION_BASELINE = "off"

#: `load_rows` 拿哪一份闭集当判据。两份都是**别处已经闭上**的键集,这里只挑一份:
#:
#: * `t0` = `RUN_PROJECTION_KEYS`,`export_reasoning_traces.py` 与 rig 的
#:   `search-*.jsonl` 写的形状;
#: * `ab` = `AB_PROJECTION_KEYS`,rig 的 `ab-runs.jsonl` 形状(T0 键集 + A/B
#:   设计 §7.1 那一批完整 Ask 专属键)。
#:
#: 默认**不**自动放行 `ab`:「多出来的键必须让人看见」是 `load_rows` 的原话,把它
#: 换成按行自动嗅探等于取消它。显式 `--key-set ab` 是操作者的知情声明——他知道这
#: 一批是完整 Ask 的 A/B 行,而不是把两种形状不小心拌在了一起。
#:
#: 放行**不等于**聚合:A/B 专属列(`answer_chars` / `latency_ms_total` /
#: `gold_facts_*` / `human_*` …)进得来,但四组标量指标一个都不吃它们。把它们加
#: 进 `NUMERIC_METRICS` 会让每一份 T0 报告都多出一批恒 `n_observed=0` 的格子,
#: 默认输出就不再逐字节稳定了。§10.2-1 质量门要的那几列由 `quality_evidence`
#: 直接读;其余几列本期只过闸不入表,是登记在案的已知缺口。
KEY_SETS: dict[str, frozenset[str]] = {
    "t0": RUN_PROJECTION_KEYS,
    "ab": AB_PROJECTION_KEYS,
}
DEFAULT_KEY_SET = "t0"


def load_rows(
    paths: Sequence[str], *, key_set: str = DEFAULT_KEY_SET,
) -> list[dict]:
    """读 JSONL,并在入口就把闭集外的键挡掉。

    输入可能来自更老/更新的一次导出。多出来的键**不是**「向前兼容地忽略」,而
    是一个必须让人看见的事实:聚合器不知道它的口径,更不该把它印进表里。所以
    直接拒绝整次运行,而不是安静地丢掉那一列。

    `key_set` 只换判据用的**那一份**闭集(见 `KEY_SETS`),不放宽上面这条纪律:
    选了 `t0` 的一批里混进一行 A/B 行,照旧整批拒绝。
    """
    allowed = KEY_SETS[key_set]
    rows: list[dict] = []
    for path in paths:
        for number, line in enumerate(Path(path).read_text("utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            extra = set(row) - allowed
            if extra:
                raise SystemExit(
                    f"{path}:{number} 带 `{key_set}` 闭集外的键: "
                    + ", ".join(sorted(extra))
                )
            rows.append(row)
    return rows


def _nearest(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def _numeric(values: Iterable[Any]) -> list[float]:
    return [
        float(value) for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]


def _positive_int(raw: str) -> int:
    """`--min-samples` 必须 ≥ 1:0 或负数会让「没有观测」的指标也去算分位数
    (codex #700 R22 P3)。"""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"--min-samples 必须 ≥ 1,给的是 {raw!r}")
    return value


def summarize_group(rows: Sequence[dict], min_samples: int) -> dict:
    summary: dict[str, Any] = {"n_runs": len(rows)}
    for metric in NUMERIC_METRICS:
        values = _numeric(row.get(metric) for row in rows)
        entry: dict[str, Any] = {
            "n_observed": len(values),
            "n_missing": len(rows) - len(values),
        }
        if values:
            entry["mean"] = round(sum(values) / len(values), 3)
        if values and len(values) >= min_samples:
            # 分位数只在样本够的时候出:三条样本的 P95 是一个看起来精确的谎。
            # `values` 非空另判一次:门槛被设成 0/负数时,空观测也会进这条分支
            # 而 `_nearest([])` 抛 IndexError(codex #700 R22 P3)。
            entry["p50"] = round(_nearest(values, 0.50), 3)
            entry["p95"] = round(_nearest(values, 0.95), 3)
        summary.setdefault("numeric", {})[metric] = entry
    for metric in BOOLEAN_METRICS:
        values = [row[metric] for row in rows
                  if isinstance(row.get(metric), bool)]
        summary.setdefault("boolean", {})[metric] = {
            "n_observed": len(values),
            "n_missing": len(rows) - len(values),
            "n_true": sum(1 for value in values if value),
        }
    for metric in CATEGORICAL_METRICS:
        counts: Counter = Counter()
        for row in rows:
            if metric not in row:
                continue
            value = row[metric]
            counts[UNKNOWN if value is None else str(value)] += 1
        if counts:
            summary.setdefault("categorical", {})[metric] = dict(
                sorted(counts.items())
            )
    for metric in COUNTER_METRICS:
        totals: Counter = Counter()
        for row in rows:
            payload = row.get(metric)
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[str(key)] += value
        if totals:
            summary.setdefault("counters", {})[metric] = dict(sorted(totals.items()))
    contribution = _contribution(rows)
    if contribution:
        summary["citation_contribution"] = contribution
    return summary


def _contribution(rows: Sequence[dict]) -> dict:
    """每动作引用贡献的跨 run 汇总(§4.4)。

    `cited_hits` 只对**可判**的 run 累加,并单独报 `runs_unknown`(该动作在这
    个格子里有多少条 run 完全判不了)。把不可判当 0 加进去,会让「查到了但没被
    引用」和「压根不知道有没有被引用」在同一列上长得一样。
    """
    merged: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        payload = row.get("citation_contribution")
        if not isinstance(payload, dict):
            continue
        for action, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            bucket = merged[str(action)]
            for key in ("steps", "steps_with_ids", "unknown_steps"):
                value = entry.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    bucket[key] += value
            hits = entry.get("cited_hits")
            if isinstance(hits, int) and not isinstance(hits, bool):
                bucket["cited_hits"] += hits
                bucket["runs_observed"] += 1
            else:
                bucket["runs_unknown"] += 1
    return {action: dict(sorted(bucket.items()))
            for action, bucket in sorted(merged.items())}


def group_rows(rows: Sequence[dict], dimensions: Sequence[str]) -> dict:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        key = tuple(
            UNKNOWN if row.get(dim) is None else str(row.get(dim, UNKNOWN))
            for dim in dimensions
        )
        grouped[key].append(row)
    return dict(sorted(grouped.items()))


def pair_table(rows: Sequence[dict]) -> list[dict]:
    """legacy / v2 成对对照(§4.2)。

    只有 `question_key` 不是 unknown 的 run 才有资格配对——线上导出的 legacy
    轨迹没有题号,把它和一条 v2 影子 run 摆在同一行是无中生有的对照。

    v2 那一侧按 `optimization` **拆行**,legacy 侧整批对每个 v2 臂重复一次
    (与 `optimization_pair_table` 的基线重复写法同构)。不拆的代价是实测到的:
    一格里同时有 `off` 与 `prefix_snapshot` 两批 v2 run 时,那一侧的每个均值都
    是两臂的混合值——它不描述任何一臂,而这张表的每一行本该是「同一格里两条只
    在臂上不同的 run」。侧内的 `optimization` 分布仍照报(见 `_pair_side`),
    拆行后它恒只有一格,正好也是这一行 v2 臂身份的自证。

    拆行**不**改「两侧 optimization 不必相同」这条(见 `ARM_DIMENSIONS`):
    legacy 在这条轴上结构性地没有身份,所以它整批与每个 v2 臂各配一行,而不是
    也跟着拆。一批 run 全没声明 optimization(线上导出)时 v2 侧只有一个臂,
    行数与拆行前逐行相同。
    """
    cells: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"legacy": [], "v2": defaultdict(list)}
    )
    for row in rows:
        if str(row.get("question_key") or UNKNOWN) == UNKNOWN:
            continue
        policy = str(row.get("policy_version") or UNKNOWN)
        if policy not in ("legacy", "v2"):
            continue
        key = tuple(
            # 与 `_optimization_cells` 同口径:布尔维度的 `False` 不折成 unknown
            # (质量评审 P3-6)。
            UNKNOWN if row.get(dim) is None else str(row[dim])
            for dim in PAIR_DIMENSIONS
        )
        if policy == "legacy":
            cells[key]["legacy"].append(row)
        else:
            cells[key]["v2"][str(row.get("optimization") or UNKNOWN)].append(row)

    table: list[dict] = []
    for key, sides in sorted(cells.items()):
        arms = sides["v2"]
        if not (sides["legacy"] and arms):
            continue
        # legacy 侧每行都是同一批 run,只算一次(每个变体重复一次那份汇总)。
        legacy_side = _pair_side(sides["legacy"])
        for _arm, v2_rows in sorted(arms.items()):
            entry: dict[str, Any] = dict(zip(PAIR_DIMENSIONS, key))
            entry["arm_dimension"] = "policy_version"
            entry["legacy"] = legacy_side
            entry["v2"] = _pair_side(v2_rows)
            table.append(entry)
    return table


#: 沿 `optimization` 配对的两张表共用的**格键**:`PAIR_DIMENSIONS` 再加
#: `policy_version`(见 `optimization_pair_table` 的口径说明)。
OPTIMIZATION_CELL_DIMENSIONS: tuple[str, ...] = (
    *PAIR_DIMENSIONS, "policy_version",
)

#: 写侧回填的**配对准入位**(A/B 设计 §7.1;`reflect_ab.mark_paired` 的原话:
#: 单臂 `--arms` / `--only-policy` 重跑出来的行只有一侧,`paired=False`,**不进
#: 配对差值表**,只进单臂基线表)。它在 `AB_PROJECTION_KEYS` 里的唯一存在理由就
#: 是这道准入,所以读侧必须真的读它(质量评审 P1)。
PAIRED_KEY = "paired"


def _optimization_cells(
    rows: Sequence[dict],
) -> tuple[dict[tuple, dict[str, list[dict]]], dict[tuple, Counter]]:
    """按 `OPTIMIZATION_CELL_DIMENSIONS` 分格、格内按 `optimization` 分臂。

    两张沿 `optimization` 配对的表——`optimization_pair_table`(每侧一个均值)
    与 `optimization_pair_rows`(格内先对重复取中位数再出配对差值)——共用这一个
    函数(各调一次)。分开各写一遍的代价不是重复代码,而是**两张表在同一批输入上
    可能配出不同的对照面**:它们本该是同一批配对的两种读法,格的依据一旦分叉,
    读的人没有任何办法从报告里看出来。

    三条入格资格,前两条与 `pair_table` 同源:`question_key` 是 unknown 的 run
    没资格配对(线上导出的 legacy 没有题号);`optimization` 是 unknown 的 run 在
    这条轴上没有身份(线上导出、或 rig 忘了传),拿它当一臂等于给差值找了个不知道
    是什么的对照面。

    第三条读写侧同口径:行带 `paired` 键而它是 `False` ⇒ 不入格。这条只在
    `--key-set ab` 的行上真的成立(T0 键集结构上没有这个键,那些行一条都不受影
    响)。失败场景是实测到的:老批两臂各跑 repeat 1–2(`paired=True`),修完再用
    单臂 `--arms` 补跑 repeat 3–5(`paired=False`),两份 `ab-runs.jsonl` 拼起来
    读 ⇒ 基线侧 2 个观测对变体侧 5 个,差值与比值都是一个数量级错的头条数字。
    `paired` 是 `None`(`mark_paired` 还没跑过)时仍然入格:那种批里**每一行**都是
    `None`,把它们也排除等于整张表凭空空掉,而那比一个偏斜的差值更难被发现。

    被这条挡掉的行按 `(格, 臂)` 计数一起返回:排除本身必须在报告面上有一列具名的
    数(`n_unpaired`,见 `_pair_row_side`),否则「这一侧有几条没有对臂」只能靠
    主动去比两侧的 n 才看得出来。
    """
    cells: dict[tuple, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    unpaired: dict[tuple, Counter] = defaultdict(Counter)
    for row in rows:
        if str(row.get("question_key") or UNKNOWN) == UNKNOWN:
            continue
        optimization = str(row.get("optimization") or UNKNOWN)
        if optimization == UNKNOWN:
            continue
        key = tuple(
            # 布尔维度的 `False` 不是 unknown:`or UNKNOWN` 会把 `--no-intent`
            # 的 run(`has_intent_contract=False`)和「压根没记这一列」的 run 折
            # 进同一格,而 `PAIR_DIMENSIONS` 的原话是它们不能配对(质量评审 P3-6)。
            UNKNOWN if row.get(dim) is None else str(row[dim])
            for dim in OPTIMIZATION_CELL_DIMENSIONS
        )
        if row.get(PAIRED_KEY) is False:
            unpaired[key][optimization] += 1
            continue
        cells[key][optimization].append(row)
    return cells, unpaired


def optimization_pair_table(
    rows: Sequence[dict], *, baseline: str = OPTIMIZATION_BASELINE,
) -> list[dict]:
    """基线臂 / 各优化变体的成对对照(T-PS4 的第二维臂)。

    与 `pair_table` 的分工见 `ARM_DIMENSIONS`:这一张沿 `optimization` 配,
    `policy_version` **固定**——所以格的依据是 `PAIR_DIMENSIONS` 再加
    `policy_version`,而 `optimization` 是臂。

    `baseline` 默认 `off`(见 `OPTIMIZATION_BASELINE`),由 `--baseline-arm` 改。
    换基线不改这张表的任何一条别的口径:`unknown` 仍然不当臂,基线那一格空着就
    一对都配不出来,每个非基线变体各出一行;同一格里出现两个变体就是两行,各自
    与基线比。

    `paired=False` 的行由 `_optimization_cells` 一并挡在格外(那道准入是写侧定
    的),但这张表**不**报被挡掉的行数:它在默认参数下就出,而多一个键会让每一
    份存档过的报告都变成「有差异」(见 `build_report` 那条形状纪律)。要看那个数
    就加 `--pair-rows` —— 逐题配对表按侧报 `n_unpaired`。

    两侧在 JSON 里的键名是**对称**的 `baseline` / `variant`,而不是
    `off` / `variant`:这两格的语义是「哪一侧」,不是「哪一臂」——臂已经由
    `variant_arm` 与侧内的 `optimization` 分布各说了一遍,再把基线臂名当键名,
    读的人就得先知道基线是谁才能取到那一格。基线可配之后这一条从「读起来别扭」
    升级成「必须如此」:键名跟着参数变的话,同一份脚本两次运行出来的 JSON 就没
    法用同一段代码读。基线那一侧的汇总在同一格里只算一次,不在变体循环里重复
    计算。
    """
    table: list[dict] = []
    cells, _unpaired = _optimization_cells(rows)
    for key, arms in sorted(cells.items()):
        baseline_rows = arms.get(baseline)
        if not baseline_rows:
            continue
        baseline_side = _pair_side(baseline_rows)
        for label, side in sorted(arms.items()):
            if label == baseline:
                continue
            entry: dict[str, Any] = dict(
                zip(OPTIMIZATION_CELL_DIMENSIONS, key)
            )
            entry["arm_dimension"] = "optimization"
            entry["variant_arm"] = label
            entry["baseline"] = baseline_side
            entry["variant"] = _pair_side(side)
            table.append(entry)
    return table


#: 每一侧都报的那几个数。前缀三格与 `model_calls_real` 在这里而不是只在分组表
#: 里:配对差值表是 T-PS4 唯一要回答的那个问题(「前缀复用换到了什么」)的落点;
#: `context_rebuilds`(T-PD2)同理——它是 `prefix_delta` 臂的核心代价量,对这一
#: 臂来说那个问题就是「重建次数换到了什么」(评审 P3-2)。`assessment_rows_total`
#: (T-PL2)同理落在这里:它是 D↔L 这一对**唯一只差自评合同**的臂上的机制读数
#: ——「省了多少重述」这件事只有把两侧摆在一起才看得出来。
PAIR_SIDE_METRICS: tuple[str, ...] = (
    "reflect_turns", "total_ms", "anchors", "run_wall_ms", "model_calls_real",
    "prefix_bytes_median", "prefix_turns", "context_rebuilds",
    "assessment_rows_total",
)


#: 上面那几项里**稀疏**的那几个(见下面 `SPARSE_PAIR_SIDE_METRICS` 的成员):
#: 只有 rig 写侧(`run_wall_ms`)、开了测量的 run(其余几个),或 `prefix_delta`
#: 臂(`context_rebuilds`)才有值,所以它们必须额外报出各自的观测数(评审 P2)。
#: 不在这里数具体几个——`SPARSE_PAIR_SIDE_METRICS` 本身就是那份清单,数字写在
#: 散文里只会在下一次加键时变成一句过期的话(质量评审 P3-2)。一侧三条 run 里
#: 只有一条量到了 `run_wall_ms` 时,`_mean` 报的就是那一条的值,而它在表里长得
#: 和「三条都量到的均值」一模一样;`n_runs` 也救不了——那一格数的是 run,不是
#: 观测。于是「1 条比 3 条」这种
#: 对照会被当成同等重量的差值读走。`off` 基线臂结构上不带 `context_rebuilds`
#: (它是 `prefix_delta` 专属的行为计数,不是「off 也测了、只是恰好没量到」),
#: 所以基线那一侧这一格恒 `n_measured=0`——这不是缺陷,是「这个问题对 `off` 臂
#: 不成立」的如实呈现,不需要为它命一个「命中率」之类的名字。
#:
#: `reflect_turns` / `total_ms` / `anchors` 不在这里:它们从轨迹本身来,一条跑成
#: 的 run 必有(`anchors` 在只检索不合成的 run 上缺,但那种 run 由 `trace_source`
#: 单独分格、整侧一起缺,不是同一侧内部的参差)。
#:
#: `assessment_rows_total`(T-PL2)同样稀疏(只有开了测量的 run 才有值),但与
#: `context_rebuilds` 那道"基线臂结构上没有"的分工不同:它**四臂通写**(拍板
#: Q8),`off` 基线侧这一格恒有真实观测,不是恒 `n_measured=0`——两侧都报
#: `n_measured` 才能看出"这一批基线/变体分别有多少条 run 真的量到了"。
SPARSE_PAIR_SIDE_METRICS: tuple[str, ...] = (
    "run_wall_ms", "model_calls_real", "prefix_bytes_median", "prefix_turns",
    "context_rebuilds", "assessment_rows_total",
)


def _pair_side(rows: Sequence[dict]) -> dict:
    """一侧(一条臂)的汇总。

    `optimization` 的分布**如实报出**:`pair_table` 拆行后 v2 侧恒只有一个键
    (那就是这一行的 v2 臂身份),legacy 侧不拆,有几个键就报几个。空着它,读表
    的人没法从这一行自证自己在看哪一臂。

    `n_measured` 给 `SPARSE_PAIR_SIDE_METRICS` 里那几项各报一个观测数。
    """
    side: dict[str, Any] = {"n_runs": len(rows)}
    for metric in PAIR_SIDE_METRICS:
        side[metric] = _mean(rows, metric)
    side["n_measured"] = {
        metric: len(_numeric(row.get(metric) for row in rows))
        for metric in SPARSE_PAIR_SIDE_METRICS
    }
    side["termination_reason"] = _distribution(rows, "termination_reason")
    side["optimization"] = _distribution(rows, "optimization")
    return side


def _distribution(rows: Sequence[dict], metric: str) -> dict:
    return dict(sorted(Counter(
        UNKNOWN if row.get(metric) is None else str(row[metric])
        for row in rows
    ).items()))


def _mean(rows: Sequence[dict], metric: str) -> float | None:
    values = _numeric(row.get(metric) for row in rows)
    return round(sum(values) / len(values), 3) if values else None


# --- 逐题配对差值表(`--pair-rows`,§10.1 的主指标)-------------------------

#: 逐题配对差值量的指标。两项都是**毫秒量**,所以 `delta_ms` 的单位是 ms、
#: `ratio` 无量纲——把一个非时间列摆进来,那两个名字就都成了假话。
#:
#: 但两项的口径**不同**,不是「两个墙钟毫秒」(质量评审 P3-2):`run_wall_ms` 是
#: rig 量的整次 run 墙钟;`total_ms` 是轨迹里**各步耗时之和**(口径见
#: `reasoning_trace_stats` 里 `total_ms` 自己的说明),漏掉排队与步间空隙,所以它
#: 是墙钟的下界而不是墙钟本身。两列各自成对比较,不互相顶替。
#:
#: 留着 `total_ms` 的理由不是「线上导出行也能配对」——那种行的 `question_key` 与
#: `optimization` 都是 unknown,压根进不了格。真正的场景是**导出 rig 打过标的
#: job**:那一批带 `question_key` + `optimization`(配得出格),而导出侧不写
#: `run_wall_ms`(恒 `None`),只有 `total_ms` 可读。少了它,那种输入会配出一张列
#: 全是 unknown 的表。
PAIR_ROW_METRICS: tuple[str, ...] = ("run_wall_ms", "total_ms")

#: 成功 / 删失 / 失败 / 未完成四格的口径(§10.1)。
#:
#: 成功是**白名单**:只有 `status == "done"` 才贡献中位数。黑名单(「不是
#: `cancelled` 也不是 `failed`」)会把 `JOB_STATUSES` 的第四格 `running`——以及
#: 将来任何新状态——当成跑成的 run 读走(规格评审 P2-1),而一条卡在 `running` 的
#: job 的 `total_ms` 只是**已跑步骤的部分和**:把它读进中位数就是「把没跑完的时间
#: 说成真实完成耗时」。这条路径不是构造的:`export_reasoning_traces.py` 不按
#: `status` 过滤,而崩掉/放弃的 job 会永久停在 `running`。
#:
#: `cancelled` 是**删失观察**:它是被整批墙钟预算或用户取消掐断的那一刻,不是这条
#: run 真实的完成耗时。`failed` 压根没跑完。两者都不进中位数,但**各自单列计数**:
#: 「不把失败丢掉后只比较成功者」是 §10.1 逐字点名不许做的事,所以这张成功配对表
#: 只有在几列计数摆在同一行时才读得懂。
#:
#: 四格计数**可加**:`n_runs == n_success + n_censored + n_failed +
#: n_unfinished`。可加性是这几格能被读懂的前提——任何一条 run 都必须落在某一格
#: 里,否则一批 run 会在四个计数全是 0 的同时悄悄影响中位数。
SUCCESS_STATUS = "done"
CENSORED_STATUS = "cancelled"
FAILED_STATUS = "failed"


def _pair_row_side(rows: Sequence[dict], *, n_unpaired: int = 0) -> dict:
    """一侧(一条臂)在一格里的配对读数:**先对重复取中位数**。

    这是这张表与 `_pair_side` 的唯一实质分工:那边每一项走 `_mean`,格内的重复
    被摊成一个均值之后,配对身份就丢了(§10.1「先对同题同档的重复汇总,再按
    题型/有图无图/档位比较」)。这边取中位数,于是同一格的三次重复只贡献**一个**
    配对观测,而不是三个独立观测。

    中位数只从**成功**(`status == "done"`)的 run 上取,删失与失败各自单列计数,
    余下的(`running`,以及任何将来新增的状态)进 `n_unfinished` —— 四格可加,
    见 `SUCCESS_STATUS`。`n_measured` 沿用 `_fmt_pair_metric` 认的那个形状,好让
    markdown 那一格照旧是 `中位数(n=观测数)`。

    `n_unpaired` 是**被挡在这一格外面**的行数(`paired=False`,见
    `_optimization_cells`),由调用方数好了传进来。它刻意**不**参与那四格的可加性
    ——那四格分的是 `n_runs`,而这些行压根没进 `n_runs`;它回答的是另一个问题:
    「这一侧有几条 run 没有对臂,所以没被读进这个差值」。
    """
    statuses = Counter(str(row.get("status") or UNKNOWN) for row in rows)
    success = [
        row for row in rows
        if str(row.get("status") or UNKNOWN) == SUCCESS_STATUS
    ]
    side: dict[str, Any] = {
        "n_runs": len(rows),
        "n_success": len(success),
        "n_censored": statuses[CENSORED_STATUS],
        "n_failed": statuses[FAILED_STATUS],
        # 减出来而不是再数一遍:这样它与前三格的可加性是**结构性**的,而不是一条
        # 要靠「状态闭集有没有被扩过」来维持的巧合。
        "n_unfinished": (
            len(rows) - len(success)
            - statuses[CENSORED_STATUS] - statuses[FAILED_STATUS]
        ),
        "n_unpaired": n_unpaired,
        "optimization": _distribution(rows, "optimization"),
    }
    measured: dict[str, int] = {}
    for metric in PAIR_ROW_METRICS:
        values = _numeric(row.get(metric) for row in success)
        side[metric] = round(_nearest(values, 0.50), 3) if values else None
        measured[metric] = len(values)
    side["n_measured"] = measured
    return side


def _paired_delta(baseline: dict, variant: dict, metric: str) -> dict:
    """一格一个指标的配对差值与比值。

    两侧中位数**任一**缺席 ⇒ 两格都是 `None`(unknown),不折 0:「没量到」和
    「差值恰好是 0」是两句不同的话,而后者在这张表上是一条结论。

    基线中位数为 0 时 `ratio` 同样 `None`——除以 0 得不到一个可报告的比值,而
    `delta_ms` 仍然成立,所以只把 `ratio` 那一格记 unknown,不连坐差值。
    """
    base_value, variant_value = baseline.get(metric), variant.get(metric)
    if base_value is None or variant_value is None:
        return {"delta_ms": None, "ratio": None}
    return {
        "delta_ms": round(variant_value - base_value, 3),
        "ratio": (round(variant_value / base_value, 4)
                  if base_value else None),
    }


def optimization_pair_rows(
    rows: Sequence[dict], *,
    baseline: str = OPTIMIZATION_BASELINE, min_samples: int,
) -> dict:
    """逐题配对差值表(§10.1 的主指标),`--pair-rows` 才出。

    与 `optimization_pair_table` 是同一批配对的两种读法(格的依据共用
    `_optimization_cells`),差别只在格内怎么汇总:那边一个均值,这边**先对重复
    取中位数、再出 `delta_ms` 与 `ratio`**。

    `rollup` 里的 `ratio_p50` 是**逐格配对比值的中位数**,不是两组独立 P50 的
    比值。这两个数在「一半题变慢一倍、另一半不动」这种输入上会给出完全相反的
    结论,而 §10.1 逐字点名:不用两组独立 P50 的比值冒充配对比值。

    `paired=False` 的行不入格(见 `_optimization_cells`),两侧各报自己被挡掉了
    几条(`n_unpaired`)——那一列在逐格表与 rollup 上都印(见
    `ROLLUP_SIDE_COUNTS`)。

    一条臂在某一格里**全部**是 `paired=False` 时那一格没有行可挂,`n_unpaired`
    也就无处可报:那种输入在这张表上的呈现与「这一臂压根没跑过这一格」相同,而
    两者都读作「没有这一对」。分组概览里那一格的 `optimization` 分布会如实数到
    这些 run(它不看 `paired`),两处对不上就是这种输入的信号。
    """
    cells: list[dict] = []
    grid, unpaired = _optimization_cells(rows)
    for key, arms in sorted(grid.items()):
        baseline_rows = arms.get(baseline)
        if not baseline_rows:
            continue
        baseline_side = _pair_row_side(
            baseline_rows, n_unpaired=unpaired[key][baseline],
        )
        for label, side in sorted(arms.items()):
            if label == baseline:
                continue
            entry: dict[str, Any] = dict(
                zip(OPTIMIZATION_CELL_DIMENSIONS, key)
            )
            entry["arm_dimension"] = "optimization"
            entry["variant_arm"] = label
            entry["baseline"] = baseline_side
            entry["variant"] = _pair_row_side(
                side, n_unpaired=unpaired[key][label],
            )
            entry["metrics"] = {
                metric: _paired_delta(baseline_side, entry["variant"], metric)
                for metric in PAIR_ROW_METRICS
            }
            cells.append(entry)
    return {
        "cells": cells,
        "rollup": _pair_row_rollup(cells, min_samples),
        "rollup_by": {
            facet: _pair_row_rollup(cells, min_samples, facet=facet)
            for facet in ROLLUP_FACETS
        },
    }


#: rollup 除了那层全局桶,再按这几条维度各出一层桶(规格评审 P2-4)。
#:
#: §10.2-3 那条采用门读的是「各主要题型/档位的系统性慢化超过 10% 需逐题解释」,
#: 而全局 `ratio_p50` 会把两档相反的偏离抵消掉:一批里 `deep` 慢三成、`standard`
#: 快两成,逐格比值抵消后全局中位比能正好落在 1.0 附近。逐格数据本来就在 `cells`
#: 里(`effort` / `corpus_cell` 都在 `PAIR_DIMENSIONS`),缺的只是聚合口径——否则
#: 报告里唯一的按档位数字是**分组概览**里两组独立 P50,而 §10.1 逐字禁止拿它当
#: 配对比值,操作者要么误用被禁的数,要么手工分拆 JSONL 重跑。
ROLLUP_FACETS: tuple[str, ...] = ("effort", "corpus_cell")

#: rollup 上**分两侧**印的那几个计数(质量评审 P3-1)。
#:
#: 两侧相加会丢掉「哪一侧更容易崩」,而那正是 §10.2-2 那条稳定性门要读的。逐格表
#: 那几列本来就标着 `(基线/变体)`,rollup 跟上同一口径。
#:
#: `n_unpaired` 与前三格不同源:它数的是**没进这一格**的行(`paired=False`,见
#: `_optimization_cells`),不是 `n_runs` 的一个切片。摆在同一排的理由是它回答的是
#: 同一类问题——「这个差值底下少了什么」;不摆出来,一批「两臂各 2 条配对 + 单臂
#: 补跑 3 条」的数据在报告面上与一批干净的 2↔2 对照长得一模一样(质量评审 P1)。
ROLLUP_SIDE_COUNTS: tuple[str, ...] = (
    "n_censored", "n_failed", "n_unfinished", "n_unpaired",
)

#: **逐格**表上分两侧印的那几个计数:比 rollup 那一排多一格 `n_success`。
#:
#: 少了它,一格「6 条 run 全 `done`、其中只有 1 条量到了 `run_wall_ms`」在 md 上与
#: 一格「只有 1 条 run」长得一模一样:值那一格是 `1000.0(n=1)`,而删失/失败/未完成
#: 三列全是 `0`(质量评审 P3-5)。`n_success` 与 `_fmt_pair_metric` 那个 `n=` 是两个
#: 数——前者数跑成的 run,后者数其中量到了这个指标的 run。
#:
#: rollup 那一层不印它:那一层已经按 `n_pairs`(出得来配对差值的**格**数)说过样本
#: 量,再加一个跨格求和的 run 数只会与它撞口径。
PAIR_ROW_SIDE_COUNTS: tuple[str, ...] = ("n_success", *ROLLUP_SIDE_COUNTS)


def _pair_row_rollup(
    cells: Sequence[dict], min_samples: int, *, facet: str | None = None,
) -> list[dict]:
    """把逐格配对按 `(policy_version, variant_arm)` 汇总成一行。

    `facet` 非空时再加一维分桶(见 `ROLLUP_FACETS`):同一批 cells 换一个更细的
    桶键读一遍,不重算任何逐格数——桶只决定哪些格摆在一行里。

    `n_pairs` 数的是**出得来配对差值的格数**,不是 run 数:一格里三次重复只
    贡献一个配对观测(见 `_pair_row_side`)。分位数受 `min_samples` 约束,并与
    `n_pairs` 摆在同一格里——「尾部分位数标明样本数」(§10.1)。

    `*_max` 不受那道门约束:§10.1 要求小样本仍展示逐题差值、中位数和最大值,而
    最大值不是分位数。取的是**最大**(最慢)那一格,不是绝对值最大的那一格:
    这一列读的是「尾部有没有可重复的恶化」。

    `n_ratio_pairs` 单独报:基线中位数为 0 的格子出得来 `delta_ms` 而出不来
    `ratio`,两个分母不同,合成一个数会让某一侧的样本量看着比实际大。

    删失/失败/未完成/没有对臂四个计数**分基线与变体两侧**印(见
    `ROLLUP_SIDE_COUNTS`)。这几格是跨格求和,而基线那一侧的汇总在同格的多个变体
    行上是同一份对象——所以一格里有两个变体时,基线的这几个数在这一行里被加了两
    次。要读单侧的绝对条数就去 `cells`,这一排读的是「哪一侧更容易崩」这个相对
    问题。
    """
    bucket_keys = ("policy_version", "variant_arm",
                   *((facet,) if facet else ()))
    buckets: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for cell in cells:
        buckets[tuple(cell[key] for key in bucket_keys)].append(cell)

    rollup: list[dict] = []
    for bucket_key, members in sorted(buckets.items()):
        entry: dict[str, Any] = dict(zip(bucket_keys, bucket_key))
        entry["n_cells"] = len(members)
        for field in ROLLUP_SIDE_COUNTS:
            for side in ("baseline", "variant"):
                entry[f"{field}_{side}"] = sum(
                    member[side][field] for member in members
                )
        entry["metrics"] = {
            metric: _rollup_metric(members, metric, min_samples)
            for metric in PAIR_ROW_METRICS
        }
        rollup.append(entry)
    return rollup


def _rollup_metric(
    members: Sequence[dict], metric: str, min_samples: int,
) -> dict:
    """一条 rollup 行上一个指标的分位数与极值。见 `_pair_row_rollup` 的口径。"""
    deltas = [
        member["metrics"][metric]["delta_ms"] for member in members
        if member["metrics"][metric]["delta_ms"] is not None
    ]
    ratios = [
        member["metrics"][metric]["ratio"] for member in members
        if member["metrics"][metric]["ratio"] is not None
    ]
    stat: dict[str, Any] = {
        "n_pairs": len(deltas), "n_ratio_pairs": len(ratios),
    }
    if deltas:
        stat["delta_ms_max"] = round(max(deltas), 3)
    if ratios:
        stat["ratio_max"] = round(max(ratios), 4)
    if len(deltas) >= min_samples:
        stat["delta_ms_p50"] = round(_nearest(deltas, 0.50), 3)
        stat["delta_ms_p95"] = round(_nearest(deltas, 0.95), 3)
    if len(ratios) >= min_samples:
        stat["ratio_p50"] = round(_nearest(ratios, 0.50), 4)
        stat["ratio_p95"] = round(_nearest(ratios, 0.95), 4)
    return stat


# --- §10.2-1 质量门有没有证据 -----------------------------------------------

#: §10.2-1 里**确定性**那半边的计数列(计划 M5 的对账表:这几列 **T-AB2 已
#: 实现**,不是待做项)。三列各报「有几条 run 量到了」与**总和**:门 1 问的是
#: 「有没有新增越范围引用 / 解析不到的锚点」,而那是一个数,不是一个存在位。
#:
#: 总和在零观测时是 `None` 而不是 0——「一条都没量到」与「量了、一条都没有」在这
#: 一节上是两句不同的话,后者才是门 1 的通过条件。
QUALITY_COUNT_KEYS: tuple[str, ...] = (
    "citations_out_of_scope", "anchors_unresolved", "anchors_on_gold",
)

#: 确定性那半边里**不是计数**的两列:完整性声明是短码枚举(门 1 的「虚报完整性」
#: 读它),空答案是布尔。各报自己的取值分布(含 unknown 一格),不折成一个比率。
QUALITY_LABEL_KEYS: tuple[str, ...] = ("completeness_claim", "answer_empty")

#: 判分模型与人工盲审那半边的证据列。第一个是 gold 命中的分母,后三个是人工盲审
#: 的记录。它们全都恒 `None`(T-AB1/T-AB3 一格未动,见 A/B 设计 §12 与本期计划
#: M6),所以 `verdict` **只**看这几列:确定性那几列在 ab 批上恒有观测,把它们算进
#: 结论会让每一批都自称「有记录」。
QUALITY_JUDGED_KEYS: tuple[str, ...] = (
    "gold_facts_total",
    "human_factual_error",
    "human_completeness_false",
    "human_citation_bad",
)

#: 质量门这一节读的**全部**列。它同时是这一节出不出的判据(见 `build_report`):
#: 这几列全是 `AB_PROJECTION_KEYS` 专属,T0 键集结构上一列都没有。
#:
#: **不新增任何投影列**(本期硬纪律):这几个名字都已经在
#: `AB_PROJECTION_KEYS` 里,这里只是把它们接上读侧。
QUALITY_EVIDENCE_KEYS: tuple[str, ...] = (
    *QUALITY_COUNT_KEYS, *QUALITY_LABEL_KEYS, *QUALITY_JUDGED_KEYS,
)


def quality_evidence(rows: Sequence[dict]) -> dict:
    """这一批有没有 §10.2-1 要的质量证据,以及确定性那半边到底是几。

    §10.2 明说**未验证 ≠ 通过**,而一份只有时间列漂亮的报告最容易被读成通过
    (本期计划 §5 风险 6)。所以缺证据的时候要印一行显式的「未验证」,而不是把
    那一节留空——空白会被读成「这一条没问题」。

    但「未验证」只对**判分与人工**那半边成立(见 `QUALITY_JUDGED_KEYS`)。确定性
    那半边 T-AB2 已经实现,数据集里就有观测:一批 D 臂新增三条越范围引用、还虚报
    了完整性,报告要是既不列它、又断言「这一批读不出质量那一条」,那句话就把关于
    **工具**的事实说成了关于**这一批**的事实(规格评审 P2-3)。所以这里把那几列
    的计数/分布一起报出来。

    `verdict` 只有两格短码,不产出任何比率:`unverified`(gold 与盲审记录都没有)
    与 `records_present`(至少有一列有观测,仍要由人工盲审下结论)。
    """
    observed = {
        key: sum(1 for row in rows if row.get(key) is not None)
        for key in QUALITY_EVIDENCE_KEYS
    }
    totals: dict[str, Any] = {}
    for key in QUALITY_COUNT_KEYS:
        values = _numeric(row.get(key) for row in rows)
        totals[key] = {
            "n_observed": len(values),
            "total": round(sum(values), 3) if values else None,
        }
    return {
        "observed": observed,
        "deterministic_totals": totals,
        "deterministic_distributions": {
            key: _distribution(rows, key) for key in QUALITY_LABEL_KEYS
        },
        "verdict": ("records_present"
                    if any(observed[key] for key in QUALITY_JUDGED_KEYS)
                    else "unverified"),
    }


def _has_quality_columns(rows: Sequence[dict], key_set: str) -> bool:
    """质量门那一节出不出:**键集允许** + **这批行里真有那几列**。

    两条都要。只看键集名的代价是实测到的:`AB_PROJECTION_KEYS ⊃
    RUN_PROJECTION_KEYS`,所以一批 `search-*.jsonl` 的 T0 行喂 `--key-set ab`
    (操作者为了 `--group-by arm` 才选的 ab,手滑喂错了文件)照样过闸,报告于是对
    一份压根没有质量列的数据集印出「无 gold / 无盲审记录 ⇒ 未验证」——而
    `build_report` 的原话是那本身就是一句假话(质量评审 P3-7)。

    第二条判的是**键在不在**,不是值非 None:今天真跑出来的 A/B 行那几列恒
    `None`(T-AB1/T-AB3 一格未动),而「有这几列、一条记录都没填」正是 `verdict`
    要报 `unverified` 的那种批,不能被这道闸挡掉。
    """
    if not set(QUALITY_EVIDENCE_KEYS) <= KEY_SETS[key_set]:
        return False
    return any(key in row for row in rows for key in QUALITY_EVIDENCE_KEYS)


def _md_table(header: Sequence[str], body: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += [
        "| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |"
        for row in body
    ]
    return lines


def render_markdown(
    report: dict, *, baseline_arm: str = OPTIMIZATION_BASELINE,
) -> str:
    """把报告渲染成 markdown。

    `baseline_arm` 只影响**标题与列名**——那两处必须说出这一份报告真正用的基线
    是谁,否则一份 `--baseline-arm prefix_delta` 的报告会顶着 `off` 的表头被人
    引用。数据本身在 `report` 里已经定型,这里一个数都不重算。

    质量门那一节与逐题配对那一节都只在 `report` 里有对应键时才渲染(见
    `build_report`),所以默认参数下这份 markdown 与接入前逐字节相同。
    """
    dimensions = report["group_by"]
    lines = ["# reflect T0 轨迹聚合", "",
             f"- 输入行数:{report['n_rows']}",
             f"- 分组维度:`{','.join(dimensions)}`",
             f"- 分位数门槛:`min_samples={report['min_samples']}`", ""]
    if report.get("quality_evidence"):
        lines += _render_quality(report["quality_evidence"])

    lines += ["## 分组概览", ""]
    lines += _md_table(
        [*dimensions, "n_runs", "reflect_turns(n/均值)", "total_ms(P50/P95)",
         "stale_breaker", "trace_truncated"],
        [
            [
                *group["key"],
                group["summary"]["n_runs"],
                _fmt_numeric_short(group["summary"], "reflect_turns"),
                _fmt_quantiles(group["summary"], "total_ms"),
                _fmt_boolean(group["summary"], "stale_breaker"),
                _fmt_boolean(group["summary"], "trace_truncated"),
            ]
            for group in report["groups"]
        ],
    )

    for group in report["groups"]:
        label = " / ".join(group["key"])
        summary = group["summary"]
        lines += ["", f"### {label}", "", f"- n_runs: {summary['n_runs']}"]
        for metric in CATEGORICAL_METRICS:
            counts = summary.get("categorical", {}).get(metric)
            if counts:
                rendered = ", ".join(f"{k}={v}" for k, v in counts.items())
                lines.append(f"- {metric}: {rendered}")
        for metric in COUNTER_METRICS:
            counts = summary.get("counters", {}).get(metric)
            if counts:
                rendered = ", ".join(f"{k}={v}" for k, v in counts.items())
                lines.append(f"- {metric}: {rendered}")
        contribution = summary.get("citation_contribution")
        if contribution:
            lines += ["", "引用贡献(§4.4):", ""]
            lines += _md_table(
                ["action", "steps", "steps_with_ids", "unknown_steps",
                 "cited_hits", "runs_observed", "runs_unknown"],
                [
                    [action, entry.get("steps", 0), entry.get("steps_with_ids", 0),
                     entry.get("unknown_steps", 0), entry.get("cited_hits", 0),
                     entry.get("runs_observed", 0), entry.get("runs_unknown", 0)]
                    for action, entry in contribution.items()
                ],
            )

    lines += ["", "## legacy / v2 对照(成对)", ""]
    if report["pairs"]:
        lines += _md_table(
            [*PAIR_DIMENSIONS, "v2 optimization", "legacy n", "v2 n",
             "legacy reflect_turns", "v2 reflect_turns",
             "legacy total_ms", "v2 total_ms"],
            [
                [*(pair[dim] for dim in PAIR_DIMENSIONS),
                 _pair_arm(pair["v2"]),
                 pair["legacy"]["n_runs"], pair["v2"]["n_runs"],
                 pair["legacy"]["reflect_turns"], pair["v2"]["reflect_turns"],
                 pair["legacy"]["total_ms"], pair["v2"]["total_ms"]]
                for pair in report["pairs"]
            ],
        )
    else:
        lines.append("(没有成对样本:输入里没有同时带 `question_key` 的两侧 run)")

    lines += ["", f"## {baseline_arm} / 优化变体对照(成对,同 policy_version)",
              ""]
    if report["optimization_pairs"]:
        lines += _md_table(
            # 表头用基线臂的名字(读表的人认 `off`,不认 `baseline`),但从参数
            # 拼,免得换了基线而表头还写着 `off`。
            [*PAIR_DIMENSIONS, "policy_version", "variant",
             f"{baseline_arm} n", "variant n",
             f"{baseline_arm} run_wall_ms(n)",
             "variant run_wall_ms(n)",
             f"{baseline_arm} model_calls_real(n)",
             "variant model_calls_real(n)",
             # `context_rebuilds`(T-PD2):`off` 臂结构上不带这个观测,所以这一
             # 列的基线格恒走 `_fmt_pair_metric` 既有的缺值显示(`unknown(n=0)`)
             # ——两侧都印,而不是像 `prefix_bytes_median` 那样只印 variant 侧,
             # 好让「off 上这件事压根不成立」在表面上看得见(评审 P3-2)。
             f"{baseline_arm} context_rebuilds(n)",
             "variant context_rebuilds(n)",
             # `assessment_rows_total`(T-PL2):四臂通写(拍板 Q8),`off` 基线
             # 侧这一格是**真实观测**,不是结构性缺席——两侧都印才回答得出
             # D↔L 这一对「省了多少重述」这个问题(同 T-PD2 修正轮的双列范式)。
             f"{baseline_arm} assessment_rows_total(n)",
             "variant assessment_rows_total(n)",
             "variant prefix_bytes_median(n)"],
            [
                [*(pair[dim] for dim in PAIR_DIMENSIONS),
                 pair["policy_version"], pair["variant_arm"],
                 pair["baseline"]["n_runs"], pair["variant"]["n_runs"],
                 *(
                     _fmt_pair_metric(pair[label], metric)
                     for metric in ("run_wall_ms", "model_calls_real",
                                    "context_rebuilds", "assessment_rows_total")
                     for label in ("baseline", "variant")
                 ),
                 _fmt_pair_metric(pair["variant"], "prefix_bytes_median")]
                for pair in report["optimization_pairs"]
            ],
        )
    else:
        lines.append(
            "(没有成对样本:输入里没有同题同格、`optimization` 一侧为"
            f" `{baseline_arm}` 另一侧为变体的 run)"
        )
    if report.get("optimization_pair_rows"):
        lines += _render_pair_rows(
            report["optimization_pair_rows"], baseline_arm,
        )
    return "\n".join(lines) + "\n"


def _render_quality(evidence: dict) -> list[str]:
    """§10.2-1 质量门那一节。见 `quality_evidence` 的口径。

    确定性那几列先印,再印判分/人工那半边的结论:那句结论的作用域只到「判分与
    人工」为止,不能顺手把已经读出来的确定性列也说成读不出(规格评审 P2-3)。
    """
    lines = ["## 质量门(§10.2-1)", ""]
    lines.append("- 确定性列(T-AB2 已实现):" + ", ".join(
        f"{key}={_fmt_unknown(stat['total'])}(n={stat['n_observed']})"
        for key, stat in evidence["deterministic_totals"].items()
    ))
    lines.append("- 确定性分布:" + " ; ".join(
        f"{key}: " + ", ".join(f"{value}={count}"
                               for value, count in distribution.items())
        for key, distribution in evidence["deterministic_distributions"].items()
    ))
    if evidence["verdict"] == "unverified":
        lines.append(
            "- 质量:无 gold / 无盲审记录 ⇒ **判分与人工那半边未验证**"
            "(确定性列见上)。§10.2 明说未验证 ≠ 通过:这一批不足以据它宣布采用。"
        )
    else:
        rendered = ", ".join(
            f"{key}={evidence['observed'][key]}" for key in QUALITY_JUDGED_KEYS
        )
        lines.append(
            f"- 质量:有记录({rendered})。结论仍按 §10.2-1 由人工盲审下,"
            "这几列只说明证据在不在,不代替它。"
        )
    return [*lines, ""]


def _render_pair_rows(payload: dict, baseline_arm: str) -> list[str]:
    """逐题配对差值那一节(`--pair-rows`)。

    逐格表 + 全局 rollup + 每条 `ROLLUP_FACETS` 各一张 rollup。节头那两句是
    §10.1 的原话,不是装饰——成功配对表可单列,但不能独自决定上线,所以跑成、
    删失、失败、未完成与没有对臂那几列必须先被读到(逐格表印
    `PAIR_ROW_SIDE_COUNTS`,rollup 印 `ROLLUP_SIDE_COUNTS`)。

    零配对时印一句话而不是几张只有表头的空表:与隔壁 `optimization_pairs` 同
    口径(质量评审 P3-4),空表会被读成「配出来了,只是数都没量到」。
    """
    lines = ["", f"## 逐题配对差值(基线 `{baseline_arm}`)", ""]
    if not payload["cells"]:
        return [*lines,
                "(没有成对样本:输入里没有同题同格、`optimization` 一侧为"
                f" `{baseline_arm}` 另一侧为变体的 run)"]
    lines += ["> 格内**先对重复取中位数**,再出 `Δms` 与 `ratio`;rollup 的"
              " `ratio p50` 是逐格配对比值的中位数,**不是**两组独立 P50 的比值"
              "(§10.1)。",
              "> 成功配对表可单列,但**不能独自决定上线**(§10.1):先读"
              " `n_success`(跑成的 run 数——它与值那一格的 `n=` 是两个数,后者只"
              "数其中量到了这个指标的)、`n_censored`(删失,`cancelled`——那是被"
              "截止时长掐断的一刻,不是真实完成耗时)、`n_failed`、"
              "`n_unfinished`(`running` 等没跑完的状态,它们的耗时是部分和,不进"
              "中位数)与 `n_unpaired`(写侧标了 `paired=false`、没有对臂,压根没"
              "进这一格),再读 Δ。", ""]
    lines += _md_table(
        [*OPTIMIZATION_CELL_DIMENSIONS, "variant",
         *(
             column
             for metric in PAIR_ROW_METRICS
             # 表头逐字说出这一格是**中位数**:`optimization_pair_table` 那张表的
             # 同名列给的是 `_mean` 的均值,两张表用同一个表头印两个不同的统计量
             # 时,读的人没有任何列名能分开它们(规格评审 P2-2)。
             for column in (f"{baseline_arm} {metric} P50(n)",
                            f"variant {metric} P50(n)",
                            f"{metric} Δms", f"{metric} ratio")
         ),
         *(f"{field}(基线/变体)" for field in PAIR_ROW_SIDE_COUNTS)],
        [
            [*(cell[dim] for dim in OPTIMIZATION_CELL_DIMENSIONS),
             cell["variant_arm"],
             *(
                 value
                 for metric in PAIR_ROW_METRICS
                 for value in (
                     _fmt_pair_metric(cell["baseline"], metric),
                     _fmt_pair_metric(cell["variant"], metric),
                     _fmt_unknown(cell["metrics"][metric]["delta_ms"]),
                     _fmt_unknown(cell["metrics"][metric]["ratio"]),
                 )
             ),
             *(_fmt_sides(cell, field) for field in PAIR_ROW_SIDE_COUNTS)]
            for cell in payload["cells"]
        ],
    )
    lines += ["", "配对汇总(分位数受 `min_samples` 约束,与 `n_pairs` 同格):",
              ""]
    lines += _rollup_table(payload["rollup"], None)
    for facet in ROLLUP_FACETS:
        lines += ["", f"按 `{facet}` 分桶(§10.2-3:各主要题型/档位各读自己的配对"
                      "中位比——全局那一行会把两档相反的偏离抵消掉):", ""]
        lines += _rollup_table(payload["rollup_by"][facet], facet)
    return lines


def _rollup_table(rollup: Sequence[dict], facet: str | None) -> list[str]:
    """一张 rollup 表。`facet` 非空时多印一列桶维度(见 `ROLLUP_FACETS`)。"""
    return _md_table(
        ["policy_version", "variant", *((facet,) if facet else ()),
         "metric", "n_pairs", "n_ratio_pairs",
         "Δms p50", "Δms p95", "Δms max", "ratio p50", "ratio p95",
         "ratio max",
         *(f"{field}(基线/变体)" for field in ROLLUP_SIDE_COUNTS)],
        [
            [entry["policy_version"], entry["variant_arm"],
             *((entry[facet],) if facet else ()),
             metric, stat["n_pairs"], stat["n_ratio_pairs"],
             *(_fmt_unknown(stat.get(field)) for field in (
                 "delta_ms_p50", "delta_ms_p95", "delta_ms_max",
                 "ratio_p50", "ratio_p95", "ratio_max")),
             *(_fmt_rollup_sides(entry, field)
               for field in ROLLUP_SIDE_COUNTS)]
            for entry in rollup
            for metric, stat in ((m, entry["metrics"][m])
                                 for m in PAIR_ROW_METRICS)
        ],
    )


def _fmt_sides(cell: dict, field: str) -> str:
    """一格里两侧的同一个计数,印成 `基线/变体`。见 `_render_pair_rows` 的表头。"""
    return f"{cell['baseline'][field]}/{cell['variant'][field]}"


def _fmt_rollup_sides(entry: dict, field: str) -> str:
    """一条 rollup 行上两侧的同一个计数,印成 `基线/变体`(质量评审 P3-1)。

    与 `_fmt_sides` 读的是两种形状:逐格表那一层两侧各是一个子 dict,rollup 这一层
    已经跨格求过和,两侧摊成 `<field>_baseline` / `<field>_variant` 两个键。
    """
    return f"{entry[f'{field}_baseline']}/{entry[f'{field}_variant']}"


def _fmt_unknown(value: Any) -> str:
    """`None` 印 `unknown` 而不是空格。

    理由与 `_fmt_pair_metric` 那条相同:空格会被读成「这一列不适用」,而这里的
    `None` 说的是「两侧有一侧没量到」或「基线中位数是 0,比值算不出来」——两句
    都是关于这一格的确切事实,不该长得像一个排版空白。
    """
    return UNKNOWN if value is None else str(value)


def _fmt_pair_metric(side: dict, metric: str) -> str:
    """一侧一个**稀疏**指标的单元格:`值(n=观测数)`。

    值是哪个统计量由调用方定,并且**由列名说出来**:`optimization_pair_table` 那
    张表给的是 `_mean` 的均值(列名 `… {metric}(n)`),逐题配对表给的是格内先对
    重复取的中位数(列名 `… {metric} P50(n)`)。两张表曾经用逐字相同的表头印这
    两个不同的统计量(规格评审 P2-2),所以这里不再自称「均值」。

    n 必须和值同格出现(评审 P2)。`run_wall_ms=6000.0` 这一格,底下是三条 run
    都量到了、还是三条里只有一条量到了,决定的是这个差值有没有意义;把 n 丢在
    JSON 里而 markdown 只印均值,等于让最容易被引用的那份输出恰好少了判断依据。
    没有任何观测时印 `unknown(n=0)` 而不是空格——空格会被读成「这一列不适用」。
    """
    value = side.get(metric)
    observed = (side.get("n_measured") or {}).get(metric, 0)
    return f"{UNKNOWN if value is None else value}(n={observed})"


def _pair_arm(side: dict) -> str:
    """一侧的 `optimization` 臂标签,给 markdown 表用。

    读的是 `_pair_side` 已经报出来的那份分布,不另存一个字段:`pair_table` 把
    v2 侧按 `optimization` 拆行之后,那一格恒只有一个键,它就是这一行的臂。多于
    一个键就是**没**拆干净(或读的是不拆的 legacy 侧),此时把几个键都拼出来而
    不是挑一个——一格里混了两臂这件事必须在表面上看得见,而不是被一个看着单一
    的标签盖住。
    """
    distribution = side.get("optimization") or {}
    return "+".join(distribution) or UNKNOWN


def _fmt_numeric_short(summary: dict, metric: str) -> str:
    entry = summary.get("numeric", {}).get(metric, {})
    return f"{entry.get('n_observed', 0)}/{entry.get('mean', UNKNOWN)}"


def _fmt_quantiles(summary: dict, metric: str) -> str:
    entry = summary.get("numeric", {}).get(metric, {})
    if "p50" not in entry:
        return f"n={entry.get('n_observed', 0)}({UNKNOWN})"
    return f"{entry['p50']}/{entry['p95']}"


def _fmt_boolean(summary: dict, metric: str) -> str:
    entry = summary.get("boolean", {}).get(metric, {})
    return f"{entry.get('n_true', 0)}/{entry.get('n_observed', 0)}"


def build_report(
    rows: Sequence[dict], *, group_by: Sequence[str], min_samples: int,
    baseline_arm: str = OPTIMIZATION_BASELINE,
    pair_rows: bool = False,
    key_set: str = DEFAULT_KEY_SET,
) -> dict:
    """聚合成一份报告。

    三个 PR-5 开关的默认值让这份 dict 的**键集**与接入前逐字节相同:
    `optimization_pair_rows` 只在 `--pair-rows` 下出现,`quality_evidence` 只在
    键集允许**且这批行里真有**那几列时出现(见 `_has_quality_columns`:T0 键集结
    构上没有 `gold_facts_total`,而对一份压根没有质量列的数据集印一句质量结论,
    本身就是一句假话)。`baseline_arm` 一个新键都不加:基线那一侧已经如实报出自己
    的 `optimization` 分布(见 `OPTIMIZATION_BASELINE`)。

    这条形状纪律不是为了好看:存档过的报告要能与新报告直接 diff,而无条件多一个
    键会让每一份旧报告都变成「有差异」。
    """
    grouped = group_rows(rows, group_by)
    report: dict[str, Any] = {
        "n_rows": len(rows),
        "group_by": list(group_by),
        "min_samples": min_samples,
        "groups": [
            {"key": list(key), "summary": summarize_group(members, min_samples)}
            for key, members in grouped.items()
        ],
        "pairs": pair_table(rows),
        "optimization_pairs": optimization_pair_table(
            rows, baseline=baseline_arm,
        ),
    }
    if _has_quality_columns(rows, key_set):
        report["quality_evidence"] = quality_evidence(rows)
    if pair_rows:
        report["optimization_pair_rows"] = optimization_pair_rows(
            rows, baseline=baseline_arm, min_samples=min_samples,
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="一个或多个投影 JSONL")
    parser.add_argument("--group-by", default=DEFAULT_GROUP_BY)
    parser.add_argument("--min-samples", type=_positive_int, default=5)
    # `unknown` 不在 `OPTIMIZATIONS` 里,所以「拿一条没声明 optimization 的 run
    # 当基线」在命令行这一层就被挡住了(见 `_optimization_cells`)。
    parser.add_argument(
        "--baseline-arm", default=OPTIMIZATION_BASELINE, choices=OPTIMIZATIONS,
        help="沿 `optimization` 配对时的基线臂(默认 off);只有 D/L 两臂的一批"
             "要显式指定,否则配对表全空",
    )
    parser.add_argument(
        "--pair-rows", action="store_true",
        help="额外出一张逐题配对差值表(格内先对重复取中位数,再出 Δms 与 ratio)",
    )
    parser.add_argument(
        "--key-set", default=DEFAULT_KEY_SET, choices=tuple(KEY_SETS),
        help="行的闭集判据:t0(默认,投影/search 行)或 ab(完整 Ask 的"
             " ab-runs.jsonl)。选 ab 是知情声明,不是自动嗅探",
    )
    parser.add_argument("--out-md")
    parser.add_argument("--out-json")
    args = parser.parse_args(argv)

    dimensions = [part.strip() for part in args.group_by.split(",") if part.strip()]
    # 分格维度跟着键集走:`--key-set ab` 下 `arm` / `repeat` 也是合法维度,而
    # `t0` 下它们仍然是未知维度(那份行里压根没有这两列)。
    unknown_dimensions = set(dimensions) - KEY_SETS[args.key_set]
    if unknown_dimensions:
        parser.error(
            "--group-by 只接受投影闭集里的键,未知维度: "
            + ", ".join(sorted(unknown_dimensions))
        )

    rows = load_rows(args.inputs, key_set=args.key_set)
    report = build_report(
        rows, group_by=dimensions, min_samples=args.min_samples,
        baseline_arm=args.baseline_arm, pair_rows=args.pair_rows,
        key_set=args.key_set,
    )
    markdown = render_markdown(report, baseline_arm=args.baseline_arm)
    if args.out_md:
        Path(args.out_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_md).write_text(markdown, encoding="utf-8")
    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if not args.out_md:
        print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
