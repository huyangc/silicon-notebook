#!/usr/bin/env python3
"""离线慢因诊断:从部署机日志/工件捞聚合证据,输出一段可直接粘贴的文本报告。

    python3 scripts/diag_slow.py                # 仓库根跑;默认回看 48h、慢阈值 3000ms
    python3 scripts/diag_slow.py --since 24 --slow-ms 5000
    python3 scripts/diag_slow.py --deep         # 额外做 DB 慢检查(typeof 全表扫,分钟级!)

设计约束(便于把输出直接交给外部分析,不泄敏感内容):
- 只读:不写任何文件、不改 DB(sqlite 以 file:...?mode=ro 打开,失败即跳过);
- 只聚合/截断:绝不打印问题原文、答案、evidence 文本;错误消息截 120 字符;
- 绝不打印 .env 中含 KEY/TOKEN/PASSWORD/SECRET 的值(只报「已配置/未配置」);
- 大文件流式逐行,单类延迟样本封顶 50k(分位数近似足够);
- 纯 stdlib,python3.8+ 可跑。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

MAX_SAMPLES = 50_000
SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
# 这些事件是历次事故修复埋的观测点,出现即是直接证据。
INTEREST_KINDS = (
    "scale_ppr_bailout", "ppr_fallback_refused", "graph_walk_refused",
    "relation_scoring_skipped", "tier2_skipped", "scale_index_build",
    "model_error", "ask_stage", "pipeline",
)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _fmt_ms(v):
    return f"{v/1000:.1f}s" if v >= 1000 else f"{int(v)}ms"


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:  # noqa: BLE001
        return None


def _iter_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        return


class Sampler:
    """有界延迟采样:count/max 精确,分位数在样本封顶后为近似。"""

    def __init__(self):
        self.count = 0
        self.max = 0
        self.samples = []

    def add(self, v):
        self.count += 1
        if v > self.max:
            self.max = v
        if len(self.samples) < MAX_SAMPLES:
            self.samples.append(v)

    def line(self):
        s = sorted(self.samples)
        return (f"n={self.count:<6} p50={_fmt_ms(_pct(s, 50)):<8} "
                f"p95={_fmt_ms(_pct(s, 95)):<8} max={_fmt_ms(self.max)}")


def section(title):
    print(f"\n=== {title} " + "=" * max(1, 68 - len(title)))


def report_requests(root, since, slow_ms):
    path = os.path.join(root, ".local", "logs", "requests.jsonl")
    section(f"HTTP 请求(近 {since} 内;慢阈值 {_fmt_ms(slow_ms)})")
    if not os.path.exists(path):
        print(f"(缺 {path})")
        return
    cutoff = datetime.now() - since
    per_path = defaultdict(Sampler)
    slow_list = []          # (ts, ms, path)
    long_reqs = []          # (start, end, path) 用于并发风暴检测,>10s 的才记
    total = 0
    for e in _iter_jsonl(path):
        ts = _parse_ts(e.get("ts", ""))
        if ts is None or ts < cutoff:
            continue
        ms = e.get("latency_ms")
        p = e.get("path", "?")
        if not isinstance(ms, (int, float)):
            continue
        total += 1
        # 路径归一:把 nb-xxx / src-xxx / 具体 id 折叠,避免每个 id 一行
        parts = []
        for seg in p.split("/"):
            if seg.startswith(("nb-", "src-", "ko-", "conv-", "K-", "KL-", "KF-", "KP-")) or (
                    len(seg) > 20 and any(c.isdigit() for c in seg)):
                parts.append("{id}")
            else:
                parts.append(seg)
        norm = "/".join(parts)
        per_path[norm].add(ms)
        if ms >= slow_ms:
            slow_list.append((e.get("ts", "")[:19], ms, norm))
        if ms >= 10_000:
            end = ts
            start = ts - timedelta(milliseconds=ms)
            long_reqs.append((start, end, norm, ms))
    print(f"窗口内请求总数 {total}")
    slow_paths = {k: v for k, v in per_path.items() if v.max >= slow_ms}
    if not slow_paths:
        print("没有任何路径出现慢请求 ✓")
    else:
        print("\n[按路径聚合 — 只列出现过慢请求的路径]")
        for k in sorted(slow_paths, key=lambda k: -slow_paths[k].max):
            print(f"  {slow_paths[k].line()}  {k}")
        slow_list.sort(key=lambda t: -t[1])
        print("\n[单次最慢 Top 15]")
        for ts, ms, p in slow_list[:15]:
            print(f"  {ts}  {_fmt_ms(ms):>8}  {p}")
    # 并发风暴:≥3 个 >10s 请求时间区间重叠(版本探针/建图踩踏的形状)
    long_reqs.sort(key=lambda t: t[0])
    storms = 0
    for i, (s1, e1, _, _) in enumerate(long_reqs):
        overlap = sum(1 for s2, e2, _, _ in long_reqs if s2 < e1 and e2 > s1)
        if overlap >= 3:
            storms += 1
    if storms:
        print(f"\n⚠ 并发慢请求重叠(≥3 个 >10s 互相重叠)出现 {storms} 次 — 踩踏形状,"
              f"重点看上面同一时刻的一簇路径")


def report_events(root, since):
    paths = sorted(set(
        glob.glob(os.path.join(root, ".local", "logs", "events.jsonl"))
        + glob.glob(os.path.join(root, ".local", "logs", "*", "events.jsonl"))))
    section(f"事件观测点(近 {since} 内;文件 {len(paths)} 个)")
    if not paths:
        print("(没有 events.jsonl)")
        return
    cutoff = datetime.now() - since
    kind_count = Counter()
    bail_reasons = Counter()
    refuse_sites = Counter()
    model_errors = Counter()
    last_bail_diag = []
    last_model_errors = []
    ask_stage = defaultdict(Sampler)
    pipe_stage = defaultdict(Sampler)
    last_build = None
    build_stages = {}
    for path in paths:
        for e in _iter_jsonl(path):
            k = e.get("kind", "")
            if k not in INTEREST_KINDS:
                continue
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            kind_count[k] += 1
            if k == "scale_ppr_bailout":
                bail_reasons[e.get("reason", "?")] += 1
                if e.get("reason") == "zero_reset":
                    diag = {kk: e.get(kk) for kk in
                            ("ann_seeds", "active_seeds", "chunk_seeds",
                             "embed_ok", "ann_sources_skipped") if kk in e}
                    last_bail_diag.append((e.get("ts", "")[:19], diag))
            elif k in ("ppr_fallback_refused", "graph_walk_refused",
                       "relation_scoring_skipped", "tier2_skipped"):
                refuse_sites[f"{k}:{e.get('site', e.get('reason', ''))}"] += 1
            elif k == "model_error":
                key = f"{e.get('stage','?')}/{e.get('model','?')}"
                model_errors[key] += 1
                msg = str(e.get("error", e.get("message", "")))[:120]
                last_model_errors.append((e.get("ts", "")[:19], key, msg))
            elif k == "ask_stage":
                ms = e.get("latency_ms")
                if isinstance(ms, (int, float)):
                    ask_stage[e.get("stage", "?")].add(ms)
            elif k == "pipeline":
                ms = e.get("latency_ms")
                if isinstance(ms, (int, float)) and e.get("status") == "done":
                    pipe_stage[e.get("stage", "?")].add(ms)
            elif k == "scale_index_build":
                st, ms = e.get("stage", "?"), e.get("latency_ms", 0)
                build_stages[st] = ms
                if st == "total":
                    last_build = (e.get("ts", "")[:19], dict(build_stages))
                    build_stages = {}
    if not kind_count:
        print("窗口内没有任何关注事件(慢因若存在,应结合 HTTP 段判断是否发生在更早窗口)")
        return
    print("[事件计数]")
    for k, c in kind_count.most_common():
        print(f"  {c:>6}  {k}")
    if bail_reasons:
        print("\n[scale_ppr_bailout 按原因]")
        for r, c in bail_reasons.most_common():
            print(f"  {c:>6}  {r}")
        for ts, d in last_bail_diag[-3:]:
            print(f"  最近 zero_reset 诊断 {ts}: {d}")
    if refuse_sites:
        print("\n[守卫拒绝/跳过 按位点]")
        for r, c in refuse_sites.most_common():
            print(f"  {c:>6}  {r}")
    if model_errors:
        print("\n[model_error 按 stage/model]")
        for r, c in model_errors.most_common(8):
            print(f"  {c:>6}  {r}")
        for ts, key, msg in last_model_errors[-5:]:
            print(f"  最近: {ts} {key} :: {msg}")
    if ask_stage:
        print("\n[ask 各阶段延迟 — 问答慢因归属的关键表]")
        for st in sorted(ask_stage, key=lambda s: -ask_stage[s].max):
            print(f"  {ask_stage[st].line()}  {st}")
    if pipe_stage:
        print("\n[摄取管线各阶段延迟]")
        for st in sorted(pipe_stage, key=lambda s: -pipe_stage[s].max):
            print(f"  {pipe_stage[st].line()}  {st}")
    if last_build:
        ts, stages = last_build
        print(f"\n[最近一次 scale 索引构建 {ts} 分段]")
        for st, ms in stages.items():
            print(f"  {_fmt_ms(ms):>10}  {st}")


def report_llm(root, since):
    paths = sorted(set(
        glob.glob(os.path.join(root, ".local", "logs", "llm.jsonl"))
        + glob.glob(os.path.join(root, ".local", "logs", "*", "llm.jsonl"))))
    section(f"LLM 调用(近 {since} 内;文件 {len(paths)} 个)")
    if not paths:
        print("(没有 llm.jsonl,或未开 LLM 日志)")
        return
    cutoff = datetime.now() - since
    per_model = defaultdict(Sampler)
    errors = Counter()
    for path in paths:
        for e in _iter_jsonl(path):
            ts = _parse_ts(e.get("ts", ""))
            if ts is None or ts < cutoff:
                continue
            model = e.get("model", "?")
            ms = e.get("latency_ms", e.get("duration_ms"))
            if isinstance(ms, (int, float)):
                per_model[model].add(ms)
            if e.get("status") not in (None, "ok", "done"):
                errors[model] += 1
    if not per_model:
        print("窗口内无带延迟的 LLM 记录")
        return
    for m in sorted(per_model, key=lambda m: -per_model[m].max):
        err = f"  errors={errors[m]}" if errors.get(m) else ""
        print(f"  {per_model[m].line()}  {m}{err}")


def report_artifacts(root, deep):
    section("DB / 索引工件")
    db = os.path.join(root, ".local", "silicon_notebook.db")
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            print(f"  {os.path.getsize(f)/1e9:8.2f} GB  {os.path.basename(f)}")
    if os.path.exists(db + "-wal") and os.path.getsize(db + "-wal") > 1e9:
        print("  ⚠ WAL > 1GB:有长期读快照挡住 checkpoint(常见=常驻服务长事务),重启服务后应回落")
    idx_root = os.path.join(root, ".local", "storage", "kg_index")
    for d in sorted(glob.glob(os.path.join(idx_root, "nb-*"))):
        m = os.path.join(d, "manifest.json")
        if not os.path.exists(m):
            continue
        try:
            mf = json.load(open(m))
        except Exception:  # noqa: BLE001
            print(f"  ⚠ manifest 损坏: {d}")
            continue
        size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(d, "*")))
        print(f"  {os.path.basename(d)}: nodes={mf.get('n_nodes')} ann={mf.get('n_ann')} "
              f"chunk_ann={mf.get('n_chunk_ann', 0)} relation_ann={mf.get('n_relation_ann', 0)} "
              f"工件 {size/1e9:.2f} GB")
        if mf.get("build_ms"):
            worst = sorted(mf["build_ms"].items(), key=lambda kv: -kv[1])[:3]
            print(f"    build_ms 最重三段: " + ", ".join(f"{k}={_fmt_ms(v)}" for k, v in worst))
    if not deep:
        print("  (向量 BLOB 迁移进度检查是全表扫描,分钟级 — 需要时加 --deep)")
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        for t in ("knowledge_embeddings", "chunk_embeddings",
                  "element_embeddings", "relation_embeddings"):
            try:
                rows = conn.execute(
                    f"SELECT typeof(vector) tp, COUNT(*) c FROM {t} GROUP BY 1").fetchall()
                print(f"  {t}: " + ", ".join(f"{r['tp']}={r['c']}" for r in rows)
                      + ("   ⚠ 仍有 text 行未迁移" if any(r["tp"] == "text" for r in rows) else " ✓"))
            except sqlite3.Error as exc:
                print(f"  {t}: 查询失败({exc})")
        conn.close()
    except sqlite3.Error as exc:
        print(f"  (DB 只读打开失败: {exc})")


def report_env(root):
    section(".env 关键开关(值含 KEY/TOKEN/PASSWORD/SECRET 的只报已配置)")
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        print("(无 .env)")
        return
    interesting = (
        "RELATION_RETRIEVAL_ENABLED", "CHUNK_KG_OVERLAY_ENABLED", "CHUNK_ANN_ENABLED",
        "SCALE_INDEX_AUTO_ENABLED", "SCALE_INDEX_AUTO_WHEN", "PPR_EMB_SYNONYM_ENABLED",
        "PPR_FACT_RERANK_ENABLED", "KG_AUTO_EXTRACT", "EMBED_MODEL", "EMBED_DIM",
        "OPENAI_COMPAT_MODEL", "REASONING_LLM_MODEL", "VECTOR_CACHE_MAX_ENTRIES",
        "SCALE_IDX_CACHE_MAX", "EDGE_CENTRALITY_MAX_NODES", "HNSW_EF_CONSTRUCTION",
        "SILICON_NOTEBOOK_STORAGE_DIR", "DATABASE_URL",
    )
    seen = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if any(m in k.upper() for m in SECRET_MARKERS):
            seen[k] = "<已配置,值不显示>" if v.strip() else "<空>"
        elif k in interesting:
            seen[k] = v.strip()
    for k in interesting:
        if k in seen:
            print(f"  {k}={seen[k]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="仓库根(默认当前目录)")
    ap.add_argument("--since", type=float, default=48, help="回看小时数(默认 48)")
    ap.add_argument("--slow-ms", type=int, default=3000, help="慢请求阈值 ms(默认 3000)")
    ap.add_argument("--deep", action="store_true",
                    help="额外做 DB 检查(typeof 全表扫,大库分钟级,只读)")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    since = timedelta(hours=args.since)
    print(f"silicon-notebook 慢因诊断  root={root}  窗口={args.since}h  "
          f"生成于 {datetime.now().isoformat(timespec='seconds')}")
    report_requests(root, since, args.slow_ms)
    report_events(root, since)
    report_llm(root, since)
    report_artifacts(root, args.deep)
    report_env(root)
    print("\n=== 完 — 把以上整段输出贴回即可 " + "=" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
