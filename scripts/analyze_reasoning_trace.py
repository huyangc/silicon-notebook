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
    # 三值同理:`True`=每轮都量到了、`False`=只量到一部分或轨迹被截断、
    # `None`=压根没量。后两者分得开才能判 `model_calls_real` 是真值还是下界。
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
    # 最后一轮各上下文块的规模(T-PS4)。和 `durations_ms` 一样按格**跨 run 求
    # 和**——想看每 run 平均就除这一格的 `n_runs`。两点提醒:短码 `total` 的单位
    # 是**字节**(其余五个是字符,见 `REFLECT_CONTEXT_DETAIL_KEYS`);没开测量的
    # run 一格都不带,所以这一项的和只覆盖开了测量的那些 run,不是整格。
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
    """
    cells: dict[tuple, dict[str, list[dict]]] = defaultdict(
        lambda: {"legacy": [], "v2": []}
    )
    for row in rows:
        if str(row.get("question_key") or UNKNOWN) == UNKNOWN:
            continue
        policy = str(row.get("policy_version") or UNKNOWN)
        if policy not in ("legacy", "v2"):
            continue
        key = tuple(str(row.get(dim) or UNKNOWN) for dim in PAIR_DIMENSIONS)
        cells[key][policy].append(row)

    table: list[dict] = []
    for key, sides in sorted(cells.items()):
        if not (sides["legacy"] and sides["v2"]):
            continue
        entry = dict(zip(PAIR_DIMENSIONS, key))
        for policy in ("legacy", "v2"):
            side = sides[policy]
            entry[policy] = {
                "n_runs": len(side),
                "reflect_turns": _mean(side, "reflect_turns"),
                "total_ms": _mean(side, "total_ms"),
                "anchors": _mean(side, "anchors"),
                "termination_reason": dict(sorted(Counter(
                    UNKNOWN if row.get("termination_reason") is None
                    else str(row["termination_reason"]) for row in side
                ).items())),
            }
        table.append(entry)
    return table


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
            [*PAIR_DIMENSIONS, "legacy n", "v2 n", "legacy reflect_turns",
             "v2 reflect_turns", "legacy total_ms", "v2 total_ms"],
            [
                [*(pair[dim] for dim in PAIR_DIMENSIONS),
                 pair["legacy"]["n_runs"], pair["v2"]["n_runs"],
                 pair["legacy"]["reflect_turns"], pair["v2"]["reflect_turns"],
                 pair["legacy"]["total_ms"], pair["v2"]["total_ms"]]
                for pair in report["pairs"]
            ],
        )
    else:
        lines.append("(没有成对样本:输入里没有同时带 `question_key` 的两侧 run)")

    return "\n".join(lines) + "\n"


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
