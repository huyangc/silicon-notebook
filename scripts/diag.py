#!/usr/bin/env python3
"""统一慢因诊断入口:一个命令 + 子命令,分发到既有的三个诊断引擎。

    python3 scripts/diag.py                 # 裸跑 = slow(部署机慢因全量报告)
    python3 scripts/diag.py slow --since 24 --deep
    python3 scripts/diag.py latency --log .local/logs/events.jsonl --last 500
    python3 scripts/diag.py base-recall [active_notebook_id] [查询词]

子命令:
    slow         离线从 .local 日志/工件捞慢因证据,出可粘贴文本报告。
                 纯 stdlib、只读、脱敏 —— 委托 scripts/diag_slow.py(不 import app)。
    latency      从 events.jsonl 的 ask_stage 事件出每阶段 P50/P95/max。
                 纯 stdlib(聚合口径与 app/eval/ask_latency.py 完全一致,有测试守漂移),
                 不 import app,可在持有 .local/ 的裸机上直接跑。
    base-recall  活体连真实库/env,诊断「深度报告/reasoning 为何不引用 base 库」。
                 需要能 import app —— 懒加载:只有跑这个子命令才拉起 app。

设计:三个引擎(diag_slow.py / ask_latency.py / diag_base_report.py)保持原样、各自
仍可单独运行(部署机/运维笔记/cron 里的老路径不受影响);本文件只做「统一入口」——
自身零 DB 调用、零 app 依赖,离线子命令绝不 import app(懒加载仅在 base-recall 生效)。

纯 stdlib,python3.8+ 可跑。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # <repo>/scripts
_REPO_ROOT = _HERE.parent                          # <repo>
_DEFAULT_EVENTS = _REPO_ROOT / ".local" / "logs" / "events.jsonl"


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


def _aggregate_stage(records) -> dict:
    """stage -> {count, p50, p95, max}. Mirrors ask_latency.aggregate_stage_latencies."""
    buckets: dict = {}
    for rec in records:
        if rec.get("kind") != "ask_stage":
            continue
        stage = rec.get("stage")
        if not stage:
            continue
        latency = rec.get("latency_ms")
        if latency is None:
            continue
        buckets.setdefault(stage, []).append(float(latency))

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


def _expand_channel_paths(path: str):
    """Global events file + per-user subdir files (`<dir>/*/<basename>`).

    Stdlib mirror of app.services.log_reader.expand_channel_paths so the offline
    latency view sees the same per-user-partitioned logs (see per-user-logs).
    """
    p = Path(path)
    paths = [p]
    parent, base = p.parent, p.name
    if parent.is_dir():
        for sub in sorted(parent.iterdir()):
            if sub.is_dir():
                paths.append(sub / base)
    return paths


def _read_ask_stage(path: str, last_n):
    """Stream ask_stage records from global + per-user JSONL files."""
    parsed: list = []
    for p in _expand_channel_paths(path):
        try:
            raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except (FileNotFoundError, OSError, IsADirectoryError):
            continue
        for line in raw:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if rec.get("kind") != "ask_stage":
                continue
            parsed.append(rec)
    if last_n is not None:
        parsed = parsed[-last_n:]
    yield from parsed


_STAGE_ORDER = ["load_indexes", "score", "expand", "answer_llm", "total"]


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
                    help=f"events JSONL 路径(默认 {_DEFAULT_EVENTS};自动并入同级 per-user 子目录)")
    ap.add_argument("--last", type=int, default=None, metavar="N",
                    help="只统计最近 N 条 ask_stage 记录")
    args = ap.parse_args(rest)
    records = list(_read_ask_stage(args.log, args.last))
    _print_latency(_aggregate_stage(records))
    return 0


def _cmd_base_recall(rest) -> int:
    """Lazily delegate to scripts/diag_base_report.py.

    diag_base_report imports app at module top, so it is imported HERE (inside
    the handler) — importing diag.py or running the offline subcommands never
    pulls app. diag_base_report.main() reads sys.argv[1]=notebook_id, [2]=query.
    """
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    import diag_base_report  # noqa: E402 — lazy: this is the only app-importing path
    saved = sys.argv
    sys.argv = ["diag_base_report.py", *rest]
    try:
        return int(diag_base_report.main() or 0)
    finally:
        sys.argv = saved


SUBCOMMANDS = {
    "slow": _cmd_slow,
    "latency": _cmd_latency,
    "base-recall": _cmd_base_recall,
}


def _print_help(stream=None) -> None:
    (stream or sys.stdout).write(__doc__)


def main(argv=None) -> int:
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
        sys.stderr.write(f"未知子命令: {cmd!r}\n\n")
        _print_help(sys.stderr)
        return 2
    return handler(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
