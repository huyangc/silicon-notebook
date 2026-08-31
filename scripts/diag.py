#!/usr/bin/env python3
"""silicon-notebook 生产诊断统一入口（七个命令，全部只读）。

    python3 scripts/diag.py                 # 裸跑 = slow(部署机慢因全量报告)
    python3 scripts/diag.py incident --pid <backend-pid>
    python3 scripts/diag.py slow --since 24 --deep
    python3 scripts/diag.py latency --log .local/logs/events.jsonl --last 500
    python3 scripts/diag.py locks --top 20
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
    locks        SQLite 写锁的 wait/hold 分布(按调用点),来自 events.jsonl 的
                 db_write_lock_slow(超阈值违规,rate-limited)与 db_write_lock_stats
                 (周期性全量快照,不做阈值过滤)。纯 stdlib、只读,不 import app。
    open         离线分析打开笔记本的查询与请求延迟；委托 diag_open_latency.py。
                 纯 stdlib、只读，不 import app。
    db           对 SQLite 源文件做有界、源端无副作用的只读快照诊断。
                 纯 stdlib，不 import app；委托 scripts/diag_db.py。
    base-recall  对 SQLite 源文件做有界、源端无副作用的参考库召回元数据诊断。
                 纯 stdlib，不 import app；不执行真实检索或输出查询/内容。

各诊断引擎保持原样并可单独运行，部署机、运维笔记和 cron 中的既有路径继续有效。
本入口自身零 DB 调用、零 app 依赖；七个命令均不 import app，且不得修改产品数据。

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
    + ["answer", "embed", "embedding", "extract", "graph", "index", "parse", "pipeline", "ppr", "report", "retrieval", "seed",
       "chunk_ann", "chunk_fts", "chunk_scale_index", "kg_candidates"]
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
# locks: offline db_write_lock_slow / db_write_lock_stats aggregation.
#
# Two event kinds, two different shapes, two different meanings — deliberately
# NOT merged into one aggregation path:
#   - db_write_lock_slow  : one record per threshold violation (site, wait_ms,
#     hold_ms), rate-limited to at most one per site per flush interval
#     (see WriteLockStats.record). It only shows the tail above
#     DB_WRITE_LOCK_WARN_MS — a site humming along just under the threshold,
#     or violating far more often than the rate limit allows, is invisible
#     here. Its "count" is "how many times we warned", not "how many writes
#     happened".
#   - db_write_lock_stats : a periodic FULL snapshot ({"sites": {site: {count,
#     wait_max_ms, hold_max_ms, wait_p99_ms, hold_p99_ms}}}) of every site
#     touched by that process since start/reset — unfiltered, real totals,
#     real bucketed percentiles. `count` is cumulative and monotonic within
#     one process's lifetime, so each new snapshot for a site is a strict
#     superset of the previous one; taking the max-count snapshot per site is
#     therefore "the most complete view of that process we've seen". Per
#     per-user-logs, EventLogger(per_user=True) routes emit() by whichever
#     user is active in the ContextVar *at flush time* — so one process's
#     snapshots can land across several different per-user files over its
#     lifetime, not just one. Reading only the file passed via --log would
#     silently show a near-empty report; diag_common.read_channel (the same
#     historical-layout reader `latency` uses) merges the global file, every
#     dated/gzipped day file, and every sibling per-user subdir file, so both
#     event kinds are actually seen.
#
# Both classes matter to a reader for different reasons (see module docstring
# on `locks`), so _cmd_locks prints both tables rather than picking one and
# silently dropping the other.
# ---------------------------------------------------------------------------

_LOCK_KINDS = ("db_write_lock_slow", "db_write_lock_stats")


def _read_lock_events(path: str, kinds=_LOCK_KINDS):
    """读 events 通道(全局 + per-user 子目录 + 按天/归档文件),只留写锁相关事件。

    走 diag_common.read_channel 而不是自己 glob/解压:该函数已经覆盖
    EventLogger 实际产出的全部布局(未加日期的历史文件、`events-YYYY-MM-DD.jsonl`
    以及后台归档出的 `.jsonl.gz`),并自带坏行容忍、去重与输入上限。--log 作为
    explicit hint 传入,保持既有调用习惯。
    """
    explicit = Path(path)
    return [
        rec for rec in diag_common.read_channel(
            explicit.parent, "events", explicit=explicit).records
        if rec.get("kind") in kinds
    ]


def _p99(values: list) -> float:
    """nearest-rank ceiling percentile,与 diag.py latency 的口径一致。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(len(ordered) * 0.99) - 1)])


def _aggregate_locks(records):
    """按 site 聚合 db_write_lock_slow 违规。返回
    [(site, count, wait_max, hold_max, wait_p99, hold_p99)],按 hold_max 降序 ——
    排最前的就是最该改的那处。

    这里的 count/p99 是「违规样本」的 count/p99,不是「全部写」的 count/p99 ——
    见模块顶部注释;真实总量/真实分布要看下面 _aggregate_lock_stats。
    """
    by_site: dict = {}
    for r in records:
        site = str(r.get("site") or "?")
        agg = by_site.setdefault(site, {"waits": [], "holds": []})
        agg["waits"].append(diag_common.finite_number(r.get("wait_ms")) or 0.0)
        agg["holds"].append(diag_common.finite_number(r.get("hold_ms")) or 0.0)

    rows = [
        (site, len(a["holds"]), max(a["waits"]), max(a["holds"]),
         _p99(a["waits"]), _p99(a["holds"]))
        for site, a in by_site.items()
    ]
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows


def _aggregate_lock_stats(records):
    """按 site 聚合 db_write_lock_stats 周期性快照。返回
    [(site, count, wait_max, hold_max, wait_p99, hold_p99)],按 hold_max 降序。

    每条快照都是该进程自启动/reset 以来的**完整**累计视图(非增量窗口),同一 site
    会在多条快照里反复出现且只会变大(count 单调不减)。取每个 site 下 count 最大
    的那条,即该 site 目前看到的最新/最完整状态;不跨快照相加 —— 累计值相加会重复
    计数,虚报出并不存在的总量。跨多进程部署时,这只反映「看到过的最完整的单进程
    视角」,不做跨进程求和(宁可少报,不凭空捏造)。
    """
    best: dict = {}
    for r in records:
        sites = r.get("sites")
        if not isinstance(sites, dict):
            continue
        for site, agg in sites.items():
            if not isinstance(agg, dict):
                continue
            cur = best.get(site)
            if cur is None or (diag_common.finite_number(agg.get("count")) or 0.0) >= (
                    diag_common.finite_number(cur.get("count")) or 0.0):
                best[site] = agg

    rows = [
        (site,
         int(diag_common.finite_number(a.get("count")) or 0),
         diag_common.finite_number(a.get("wait_max_ms")) or 0.0,
         diag_common.finite_number(a.get("hold_max_ms")) or 0.0,
         diag_common.finite_number(a.get("wait_p99_ms")) or 0.0,
         diag_common.finite_number(a.get("hold_p99_ms")) or 0.0)
        for site, a in best.items()
    ]
    rows.sort(key=lambda r: r[3], reverse=True)
    return rows


def _print_lock_table(rows, top) -> None:
    print(f"{'site':<44}{'n':>7}{'wait_max':>11}{'hold_max':>11}"
          f"{'wait_p99':>11}{'hold_p99':>11}")
    for site, n, wmax, hmax, wp99, hp99 in rows[:max(1, top)]:
        print(f"{site:<44}{n:>7}{wmax:>11.1f}{hmax:>11.1f}"
              f"{wp99:>11.1f}{hp99:>11.1f}")


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


def _cmd_locks(rest) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="diag.py locks",
        description="SQLite 写锁的 wait/hold 分布,来自 events.jsonl 的 "
                    "db_write_lock_slow / db_write_lock_stats 事件。wait=排队"
                    "(用户感知的卡顿),hold=持锁(谁害的)。注意:① "
                    "db_write_lock_slow 是超阈值+按 site 限流的样本(非全量),"
                    "其 max/p99 可能低估真实最坏情况;② db_write_lock_stats "
                    "按 site 取 count 最大的一条快照,多 worker 部署下这只是"
                    "其中一个 worker 的视角。")
    ap.add_argument("--log", default=str(_DEFAULT_EVENTS),
                    help=f"events JSONL 路径(默认 {_DEFAULT_EVENTS};自动并入"
                         "同级 per-user 子目录与按天/归档文件)")
    ap.add_argument("--top", type=int, default=20, metavar="N",
                    help="每张表只打印 hold_max 最大的 N 个调用点(默认 20)")
    args = ap.parse_args(rest)

    records = _read_lock_events(args.log)
    slow = [r for r in records if r.get("kind") == "db_write_lock_slow"]
    stats = [r for r in records if r.get("kind") == "db_write_lock_stats"]

    rows = _aggregate_locks(slow)
    if not rows:
        print("没有 db_write_lock_slow 事件 —— 要么没有超阈值的慢写,要么 "
              "DB_WRITE_LOCK_WARN_MS 设得太高。这只说明「没有违规」,不代表"
              "「没有写锁活动」,见下方 db_write_lock_stats 全量快照。")
    else:
        print("== db_write_lock_slow 违规(超 DB_WRITE_LOCK_WARN_MS 阈值,按 site 限流)==")
        _print_lock_table(rows, args.top)

    print()
    print("== db_write_lock_stats 全量快照(不做阈值过滤,每 site 取最新一条累计)==")
    stat_rows = _aggregate_lock_stats(stats)
    if not stat_rows:
        print("没有 db_write_lock_stats 事件(进程可能还没跑满一个 flush 周期)。")
    else:
        _print_lock_table(stat_rows, args.top)

    print()
    print("注:db_write_lock_slow 为限流抽样(非全量),max/p99 可能低估真实最坏"
          "情况;db_write_lock_stats 按 site 取 count 最大快照,多 worker 部署"
          "下只反映其中一个 worker 的视角。")
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
    "locks": _cmd_locks,
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
