#!/usr/bin/env python3
"""reflect v2 开闸前 T0 · 离线聚合:闭集投影 JSONL → markdown 表 + JSON 明细。

设计真源 §2.2/§4:
`docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md`

零 DB、零模型、零网络:它只吃 `scripts/export_reasoning_traces.py` 与
`scripts/reflect_shadow_rig.py` 写出的 JSONL。除标准库外只 import
`app.domain.reasoning_trace_stats` 的闭集常量(用来把非法键挡在聚合之外)。

    python scripts/analyze_reasoning_trace.py a.jsonl b.jsonl \
        --group-by consumer,policy_version,effort --out-md t0.md --out-json t0.json

三条纪律,和导出侧同源:

* **unknown 是一等值**:每个指标各报自己的 `n_observed`,缺失不折成 0。
* **小样本不出分位数**:`n_observed < --min-samples` 的格子只报计数。
* **对照成对**:legacy 与 v2 只在 `question_key` + `corpus_cell` + `effort`
  三者相同时配对(§4.2);线上导出的 legacy 没有 `question_key`,只进基线表。
  臂有**两条轴**(`ARM_DIMENSIONS`):`policy_version`(legacy/v2)与
  `optimization`(`off` / 前缀复用各变体),各出一张成对表,不合成四象限。
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
    RUN_PROJECTION_KEYS,
    UNKNOWN,
)

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

#: 沿 `optimization` 配对时的基线臂。`off` 是「测量开着但一处优化都没上」的那一
#: 臂(计划 §5 Q2:测量开关与 `optimization` 正交),所有变体都与它比。
OPTIMIZATION_BASELINE = "off"


def load_rows(paths: Sequence[str]) -> list[dict]:
    """读 JSONL,并在入口就把闭集外的键挡掉。

    输入可能来自更老/更新的一次导出。多出来的键**不是**「向前兼容地忽略」,而
    是一个必须让人看见的事实:聚合器不知道它的口径,更不该把它印进表里。所以
    直接拒绝整次运行,而不是安静地丢掉那一列。
    """
    rows: list[dict] = []
    for path in paths:
        for number, line in enumerate(Path(path).read_text("utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            extra = set(row) - RUN_PROJECTION_KEYS
            if extra:
                raise SystemExit(
                    f"{path}:{number} 带闭集外的键: {', '.join(sorted(extra))}"
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
        key = tuple(str(row.get(dim) or UNKNOWN) for dim in PAIR_DIMENSIONS)
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


def optimization_pair_table(rows: Sequence[dict]) -> list[dict]:
    """`off` / 各优化变体的成对对照(T-PS4 的第二维臂)。

    与 `pair_table` 的分工见 `ARM_DIMENSIONS`:这一张沿 `optimization` 配,
    `policy_version` **固定**——所以格的依据是 `PAIR_DIMENSIONS` 再加
    `policy_version`,而 `optimization` 是臂。

    `unknown` 不当臂:一条没声明 optimization 的 run(线上导出、或 rig 忘了传)
    在这条轴上没有身份,拿它当一臂等于给差值找了个不知道是什么的对照面。基线恒
    为 `off`,每个变体各出一行;同一格里出现两个变体就是两行,各自与 `off` 比。

    两侧在 JSON 里的键名是**对称**的 `baseline` / `variant`,而不是
    `off` / `variant`:这两格的语义是「哪一侧」,不是「哪一臂」——臂已经由
    `variant_arm` 与侧内的 `optimization` 分布各说了一遍,再把基线臂名当键名,
    读的人就得先知道基线是谁才能取到那一格。基线那一侧的汇总在同一格里只算
    一次,不在变体循环里重复计算。
    """
    cells: dict[tuple, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        if str(row.get("question_key") or UNKNOWN) == UNKNOWN:
            continue
        optimization = str(row.get("optimization") or UNKNOWN)
        if optimization == UNKNOWN:
            continue
        key = tuple(
            str(row.get(dim) or UNKNOWN)
            for dim in (*PAIR_DIMENSIONS, "policy_version")
        )
        cells[key][optimization].append(row)

    table: list[dict] = []
    for key, arms in sorted(cells.items()):
        baseline = arms.get(OPTIMIZATION_BASELINE)
        if not baseline:
            continue
        baseline_side = _pair_side(baseline)
        for label, side in sorted(arms.items()):
            if label == OPTIMIZATION_BASELINE:
                continue
            entry: dict[str, Any] = dict(
                zip((*PAIR_DIMENSIONS, "policy_version"), key)
            )
            entry["arm_dimension"] = "optimization"
            entry["variant_arm"] = label
            entry["baseline"] = baseline_side
            entry["variant"] = _pair_side(side)
            table.append(entry)
    return table


#: 每一侧都报的那几个数。前缀三格与 `model_calls_real` 在这里而不是只在分组表
#: 里:配对差值表是 T-PS4 唯一要回答的那个问题(「前缀复用换到了什么」)的落点。
PAIR_SIDE_METRICS: tuple[str, ...] = (
    "reflect_turns", "total_ms", "anchors", "run_wall_ms", "model_calls_real",
    "prefix_bytes_median", "prefix_turns",
)


#: 上面那几项里**稀疏**的那四个:只有 rig 写侧(`run_wall_ms`)或开了测量的 run
#: (其余三个)才有值,所以它们必须额外报出各自的观测数(评审 P2)。一侧三条 run
#: 里只有一条量到了 `run_wall_ms` 时,`_mean` 报的就是那一条的值,而它在表里长得
#: 和「三条都量到的均值」一模一样;`n_runs` 也救不了——那一格数的是 run,不是观测。
#: 于是「1 条比 3 条」这种对照会被当成同等重量的差值读走。
#:
#: `reflect_turns` / `total_ms` / `anchors` 不在这里:它们从轨迹本身来,一条跑成
#: 的 run 必有(`anchors` 在只检索不合成的 run 上缺,但那种 run 由 `trace_source`
#: 单独分格、整侧一起缺,不是同一侧内部的参差)。
SPARSE_PAIR_SIDE_METRICS: tuple[str, ...] = (
    "run_wall_ms", "model_calls_real", "prefix_bytes_median", "prefix_turns",
)


def _pair_side(rows: Sequence[dict]) -> dict:
    """一侧(一条臂)的汇总。

    `optimization` 的分布**如实报出**:`pair_table` 拆行后 v2 侧恒只有一个键
    (那就是这一行的 v2 臂身份),legacy 侧不拆,有几个键就报几个。空着它,读表
    的人没法从这一行自证自己在看哪一臂。

    `n_measured` 给稀疏那四项各报一个观测数,见 `SPARSE_PAIR_SIDE_METRICS`。
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


def _md_table(header: Sequence[str], body: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += [
        "| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |"
        for row in body
    ]
    return lines


def render_markdown(report: dict) -> str:
    dimensions = report["group_by"]
    lines = ["# reflect T0 轨迹聚合", "",
             f"- 输入行数:{report['n_rows']}",
             f"- 分组维度:`{','.join(dimensions)}`",
             f"- 分位数门槛:`min_samples={report['min_samples']}`", ""]

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

    lines += ["", "## off / 优化变体对照(成对,同 policy_version)", ""]
    if report["optimization_pairs"]:
        lines += _md_table(
            # 表头用基线臂的名字(读表的人认 `off`,不认 `baseline`),但从常量
            # 拼,免得基线臂哪天换了而表头还写着 `off`。
            [*PAIR_DIMENSIONS, "policy_version", "variant",
             f"{OPTIMIZATION_BASELINE} n", "variant n",
             f"{OPTIMIZATION_BASELINE} run_wall_ms(n)",
             "variant run_wall_ms(n)",
             f"{OPTIMIZATION_BASELINE} model_calls_real(n)",
             "variant model_calls_real(n)",
             "variant prefix_bytes_median(n)"],
            [
                [*(pair[dim] for dim in PAIR_DIMENSIONS),
                 pair["policy_version"], pair["variant_arm"],
                 pair["baseline"]["n_runs"], pair["variant"]["n_runs"],
                 *(
                     _fmt_pair_metric(pair[label], metric)
                     for metric in ("run_wall_ms", "model_calls_real")
                     for label in ("baseline", "variant")
                 ),
                 _fmt_pair_metric(pair["variant"], "prefix_bytes_median")]
                for pair in report["optimization_pairs"]
            ],
        )
    else:
        lines.append(
            "(没有成对样本:输入里没有同题同格、`optimization` 一侧为 `off`"
            " 另一侧为变体的 run)"
        )
    return "\n".join(lines) + "\n"


def _fmt_pair_metric(side: dict, metric: str) -> str:
    """一侧一个**稀疏**指标的单元格:`均值(n=观测数)`。

    n 必须和均值同格出现(评审 P2)。`run_wall_ms=6000.0` 这一格,底下是三条 run
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
) -> dict:
    grouped = group_rows(rows, group_by)
    return {
        "n_rows": len(rows),
        "group_by": list(group_by),
        "min_samples": min_samples,
        "groups": [
            {"key": list(key), "summary": summarize_group(members, min_samples)}
            for key, members in grouped.items()
        ],
        "pairs": pair_table(rows),
        "optimization_pairs": optimization_pair_table(rows),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="一个或多个投影 JSONL")
    parser.add_argument("--group-by", default=DEFAULT_GROUP_BY)
    parser.add_argument("--min-samples", type=_positive_int, default=5)
    parser.add_argument("--out-md")
    parser.add_argument("--out-json")
    args = parser.parse_args(argv)

    dimensions = [part.strip() for part in args.group_by.split(",") if part.strip()]
    unknown_dimensions = set(dimensions) - RUN_PROJECTION_KEYS
    if unknown_dimensions:
        parser.error(
            "--group-by 只接受投影闭集里的键,未知维度: "
            + ", ".join(sorted(unknown_dimensions))
        )

    rows = load_rows(args.inputs)
    report = build_report(
        rows, group_by=dimensions, min_samples=args.min_samples,
    )
    markdown = render_markdown(report)
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
