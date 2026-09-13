#!/usr/bin/env python3
"""离线聚合检索轨迹：闭集投影 JSONL → Markdown 表和 JSON 明细。

零数据库、零模型、零网络。输入由 export_reasoning_traces.py 或本机检索测试生成。
缺失指标独立计入 n_missing，不按零处理；样本不足时不出分位数。

    python scripts/analyze_reasoning_trace.py a.jsonl b.jsonl \
        --group-by consumer,effort --out-md traces.md --out-json traces.json
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
    RUN_PROJECTION_KEYS, UNKNOWN, assert_closed, assert_projection_values,
)

DEFAULT_GROUP_BY = "consumer,mode,effort,kg_in_scope"

NUMERIC_METRICS: tuple[str, ...] = (
    "reflect_turns", "trace_steps", "total_ms", "fallback_count",
    "shared_hits", "stale_max", "anchors",
    "candidates_kg", "candidates_chunks", "candidates_elements",
    "included_kg", "included_chunks", "included_elements",
    "section_total", "report_depth", "attempted", "attempted_failed",
    "top_relevance", "run_wall_ms",
)
BOOLEAN_METRICS: tuple[str, ...] = (
    "stale_breaker", "trace_truncated", "has_intent_contract",
    "grounded", "failed", "scope_narrowed",
)
CATEGORICAL_METRICS: tuple[str, ...] = (
    "termination_reason", "termination_inferred", "evidence_level",
    "trace_source", "status",
)
COUNTER_METRICS: tuple[str, ...] = (
    "actions_by_type", "seed_actions_by_type", "empty_actions_by_type",
    "skip_reasons", "fallback_reasons", "durations_ms",
)


def load_rows(paths: Sequence[str]) -> list[dict]:
    """拒绝闭集外的键和自由文本值，避免把原文带入统计报告。"""
    rows: list[dict] = []
    for path in paths:
        for number, line in enumerate(Path(path).read_text("utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("projection must be an object")
                assert_closed(row)
                assert_projection_values(row)
            except (TypeError, ValueError):
                raise SystemExit(f"{path}:{number} 不是有效的闭集投影行") from None
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
        and math.isfinite(value)
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
                    if (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value)):
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


def _md_table(header: Sequence[str], body: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += [
        "| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |"
        for row in body
    ]
    return lines


def render_markdown(report: dict) -> str:
    """渲染已聚合的统计，不重算任何指标。"""
    dimensions = report["group_by"]
    lines = ["# 检索轨迹聚合", "",
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
            lines += ["", "引用贡献:", ""]
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
    """按相同工作负载维度聚合，保留每个指标自己的观测样本数。"""
    grouped = group_rows(rows, group_by)
    return {
        "n_rows": len(rows),
        "group_by": list(group_by),
        "min_samples": min_samples,
        "groups": [
            {"key": list(key), "summary": summarize_group(members, min_samples)}
            for key, members in grouped.items()
        ],
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
    if set(dimensions) - RUN_PROJECTION_KEYS:
        parser.error("--group-by 只接受投影闭集里的键")
    rows = load_rows(args.inputs)
    report = build_report(rows, group_by=dimensions, min_samples=args.min_samples)
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
