#!/usr/bin/env python3
"""silicon-notebook 生产诊断统一入口（六个命令，全部只读）。

    python3 scripts/diag.py                 # 裸跑 = slow(部署机慢因全量报告)
    python3 scripts/diag.py incident --pid <backend-pid>
    python3 scripts/diag.py slow --since 24 --deep
    python3 scripts/diag.py latency --log .local/logs/events.jsonl --last 500
    python3 scripts/diag.py open --local .local
    python3 scripts/diag.py db --db .local/silicon_notebook.db
    python3 scripts/diag.py base-recall [active_notebook_id]

子命令:
    incident     主要的线上即时采集命令：在统一截止时间内输出有界、脱敏报告。
                 纯 stdlib，不 import app；委托 scripts/diag_incident.py。
    slow         离线从 .local 日志/工件捞慢因证据,出可粘贴文本报告。
                 纯 stdlib、只读、脱敏 —— 委托 scripts/diag_slow.py(不 import app)。
    latency      从 events.jsonl 的 ask_stage 事件出每阶段 P50/P95/max。
                 纯 stdlib(聚合口径与 app/eval/ask_latency.py 完全一致,有测试守漂移),
                 不 import app,可在持有 .local/ 的裸机上直接跑。
    open         离线分析打开笔记本的查询与请求延迟；委托 diag_open_latency.py。
                 纯 stdlib、只读，不 import app。
    db           对 SQLite 源文件做有界、源端无副作用的只读快照诊断。
                 纯 stdlib，不 import app；委托 scripts/diag_db.py。
    base-recall  对 SQLite 源文件做有界、源端无副作用的参考库召回元数据诊断。
                 纯 stdlib，不 import app；不执行真实检索或输出查询/内容。

各诊断引擎保持原样并可单独运行，部署机、运维笔记和 cron 中的既有路径继续有效。
本入口自身零 DB 调用、零 app 依赖；六个命令均不 import app，且不得修改产品数据。

纯 stdlib,python3.8+ 可跑。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # <repo>/scripts
_REPO_ROOT = _HERE.parent                          # <repo>
_DEFAULT_EVENTS = _REPO_ROOT / ".local" / "logs" / "events.jsonl"

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import diag_common  # noqa: E402 — stdlib sibling historical log reader


# ---------------------------------------------------------------------------
# latency: self-contained stdlib ask_stage aggregation.
# Kept byte-for-byte equivalent to app/eval/ask_latency.py's
# aggregate_stage_latencies (nearest-rank ceiling); test_diag_unified.py
# ::test_latency_aggregation_matches_ask_latency guards against drift.
# Reimplemented here (not imported) so this offline path never touches app.
# ---------------------------------------------------------------------------

def _percentile_nearest(values: list, p: float) -> float:
    """Nearest-rank (ceiling) percentile on a *sorted* list."""
    n = len(values)
    if n == 0:
        return 0.0
    idx = min(n - 1, max(0, math.ceil(p * n) - 1))
    return float(values[idx])


_STAGE_ORDER = ["load_indexes", "score", "expand", "answer_llm", "total"]
_SAFE_STAGES = frozenset(
    _STAGE_ORDER
    + ["answer", "embed", "embedding", "extract", "graph", "index", "parse", "pipeline", "ppr", "report", "retrieval", "seed"]
)


def _aggregate_stage(records) -> dict:
    """stage -> {count, p50, p95, max}. Mirrors ask_latency.aggregate_stage_latencies."""
    buckets: dict = {}
    for rec in records:
        if rec.get("kind") != "ask_stage":
            continue
        raw_stage = rec.get("stage")
        if not isinstance(raw_stage, str) or not raw_stage:
            continue
        stage = raw_stage.lower() if raw_stage.lower() in _SAFE_STAGES else "other"
        latency = diag_common.finite_number(rec.get("latency_ms"))
        if latency is None:
            continue
        buckets.setdefault(stage, []).append(latency)

    result: dict = {}
    for stage, vals in buckets.items():
        vals.sort()
        result[stage] = {
            "count": len(vals),
            "p50": _percentile_nearest(vals, 0.50),
            "p95": _percentile_nearest(vals, 0.95),
            "max": float(vals[-1]),
        }
    return result


def _read_ask_stage(path: str, last_n):
    """Read ask stages from all historical events layouts, retaining --log as a hint."""
    explicit = Path(path)
    parsed = [rec for rec in diag_common.read_channel(
        explicit.parent, "events", explicit=explicit).records
        if rec.get("kind") == "ask_stage"]
    if last_n is not None:
        parsed = parsed[-last_n:]
    yield from parsed


def _print_latency(stats: dict) -> None:
    print("=" * 60)
    print("== ask 每阶段延迟(P50/P95/max,来自 events.jsonl 的 ask_stage) ==")
    if not stats:
        print("  (无 ask_stage 事件 —— 若确有 ask 流量,检查是否埋点/日志路径)")
        return
    known = [s for s in _STAGE_ORDER if s in stats]
    extras = sorted(s for s in stats if s not in _STAGE_ORDER)
    header = f"{'stage':<16}  {'count':>6}  {'P50 ms':>8}  {'P95 ms':>8}  {'max ms':>8}"
    print(header)
    print("-" * len(header))
    for stage in known + extras:
        s = stats[stage]
        print(f"{stage:<16}  {s['count']:>6}  "
              f"{s['p50']:>8.1f}  {s['p95']:>8.1f}  {s['max']:>8.1f}")


# ---------------------------------------------------------------------------
# subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_incident(rest) -> int:
    """Delegate in-process to scripts/diag_incident.py (stdlib, app-free)."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_incident  # noqa: E402 — stdlib sibling, no app import
    saved = sys.argv
    sys.argv = ["diag_incident.py", *rest]
    try:
        return int(diag_incident.main() or 0)
    finally:
        sys.argv = saved

def _cmd_slow(rest) -> int:
    """Delegate in-process to scripts/diag_slow.py (stdlib, app-free)."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_slow  # noqa: E402 — stdlib sibling, no app import
    saved = sys.argv
    sys.argv = ["diag_slow.py", *rest]
    try:
        return int(diag_slow.main() or 0)
    finally:
        sys.argv = saved


def _cmd_latency(rest) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="diag.py latency",
        description="每阶段 ask 延迟(P50/P95/max),来自 events.jsonl 的 ask_stage 事件。")
    ap.add_argument("--log", default=str(_DEFAULT_EVENTS),
                    help="events JSONL 路径(默认 <repo>/.local/logs/events.jsonl;"
                         "自动并入同级 per-user 子目录)")
    ap.add_argument("--last", type=int, default=None, metavar="N",
                    help="只统计最近 N 条 ask_stage 记录")
    args = ap.parse_args(rest)
    records = list(_read_ask_stage(args.log, args.last))
    _print_latency(_aggregate_stage(records))
    return 0


def _cmd_open(rest) -> int:
    """Delegate in-process to scripts/diag_open_latency.py (stdlib, app-free)."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_open_latency  # noqa: E402 — stdlib sibling, no app import
    saved = sys.argv
    sys.argv = ["diag_open_latency.py", *rest]
    try:
        return int(diag_open_latency.main() or 0)
    finally:
        sys.argv = saved


def _cmd_db(rest) -> int:
    """Delegate in-process to scripts/diag_db.py (stdlib, app-free)."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_db  # noqa: E402 — stdlib sibling, no app import
    saved = sys.argv
    sys.argv = ["diag_db.py", *rest]
    try:
        return int(diag_db.main() or 0)
    finally:
        sys.argv = saved


def _cmd_base_recall(rest) -> int:
    """Delegate to the stdlib, app-free snapshot metadata diagnosis."""
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_base_report  # noqa: E402 — stdlib sibling, no app import
    saved = sys.argv
    sys.argv = ["diag_base_report.py", *rest]
    try:
        return int(diag_base_report.main() or 0)
    finally:
        sys.argv = saved


SUBCOMMANDS = {
    "incident": _cmd_incident,
    "slow": _cmd_slow,
    "latency": _cmd_latency,
    "open": _cmd_open,
    "db": _cmd_db,
    "base-recall": _cmd_base_recall,
}


def _print_help(stream=None) -> None:
    (stream or sys.stdout).write(__doc__)


def _main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # explicit help -> stdout (so `diag.py --help | less` works).
    if argv and argv[0] in ("-h", "--help", "help"):
        _print_help()
        return 0

    # bare invocation, or leading flag (e.g. `diag.py --since 24`) -> slow,
    # preserving muscle memory of the old `diag_slow.py [flags]` entry.
    if not argv or argv[0].startswith("-"):
        return _cmd_slow(argv)

    cmd = argv[0]
    handler = SUBCOMMANDS.get(cmd)
    if handler is None:
        # error diagnostics -> stderr.
        sys.stderr.write("未知子命令\n\n")
        _print_help(sys.stderr)
        return 2
    return handler(argv[1:])


def main(argv=None) -> int:
    return diag_common.run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    sys.exit(main())
