#!/usr/bin/env python3
"""诊断:打开大 notebook 仍卡几秒的残余延迟花在哪(#245 的 A/B/D/F/E 落地后)。

主机侧只读诊断,纯 stdlib、不 import app(与 diag_slow.py 同款,可在持有 .local/ 的
部署机上直接跑,不需要 app 依赖)。sqlite 以 mode=ro 打开,绝不改数据。示例:

    python3 scripts/diag_open_latency.py                 # 仓库根跑,自动取最大 notebook
    python3 scripts/diag_open_latency.py --notebook nb-xxx
    python3 scripts/diag_open_latency.py --root /path/to/repo

把整段输出贴回来即可。测量「打开一个 notebook」真实触发的关键查询耗时 + 生产请求
日志里各端点的真实 P50/P95/max,用来定位 A(计数缓存)之后剩下的几秒是:①计数缓存
【冷未命中】的 2M GROUP BY 成本(重启后 / 该 nb 每次 KG 变更后首开重付);②from_row 里
A 没缓存的子查询(pending_kg_source_count 相关子查询等);③别的端点在吃秒;④后台在建/
摄取导致 kg_mutation_seq 一直变、缓存永远命不中(每次都冷算)。

复用 diag_slow.py 的 .local 定位 / 日志迭代 / 分位数助手(同目录,纯 stdlib)。"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import diag_slow  # noqa: E402 — stdlib sibling host diagnostic; reuse its helpers

USABLE = ("approved", "reviewed", "project_specific", "conflict")


def _fmt(ms: float) -> str:
    return f"{ms/1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _expand_request_files(local_dir: str):
    """requests.jsonl(全局 + per-user 子目录),与 log_reader.expand_channel_paths 同义。"""
    logs = os.path.join(local_dir, "logs")
    out = [os.path.join(logs, "requests.jsonl")]
    if os.path.isdir(logs):
        for sub in sorted(os.listdir(logs)):
            p = os.path.join(logs, sub)
            if os.path.isdir(p):
                out.append(os.path.join(p, "requests.jsonl"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="仓库根(默认当前目录)")
    ap.add_argument("--local", default="", help="显式指定 .local 目录(默认自动探测)")
    ap.add_argument("--notebook", default="", help="目标 notebook id(默认自动取最大库)")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    local_dir = os.path.abspath(args.local) if args.local else diag_slow.resolve_local(root)

    print("=" * 70)
    print("== 打开延迟诊断(#245 落地后残余卡顿定位;只读 mode=ro)==")
    db_path = os.path.join(local_dir, "silicon_notebook.db")
    if not os.path.exists(db_path):
        print(f"(缺 {db_path};用 --local 指定 .local)")
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"(DB 只读打开失败: {exc})")
        return 0
    try:
        def _run(sql, params=()):
            return conn.execute(sql, params)   # 唯一 conn.execute 站点(允许表钉死)

        def _timed(sql, params=()):
            t0 = time.perf_counter()
            rows = _run(sql, params).fetchall()
            return rows, (time.perf_counter() - t0) * 1000.0

        nb = args.notebook
        if not nb:
            try:
                r = _run("SELECT notebook_id nb, COUNT(*) c FROM knowledge_objects "
                         "GROUP BY 1 ORDER BY c DESC LIMIT 1").fetchone()
                nb = r["nb"] if r else ""
            except sqlite3.Error as exc:
                print(f"(选最大 notebook 失败: {exc})")
                return 0
        if not nb:
            print("(库中无 knowledge_objects)")
            return 0

        # 规模 + seq/dirty 状态
        by_type = {r["ot"]: r["c"] for r in _run(
            "SELECT object_type ot, COUNT(*) c FROM knowledge_objects "
            "WHERE notebook_id=? GROUP BY 1", (nb,)).fetchall()}
        n_src = _run("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb,)).fetchone()["c"]
        try:
            st = _run("SELECT COALESCE(kg_mutation_seq,0) ks, COALESCE(cluster_mutation_seq,0) cs, "
                      "COALESCE(mention_seq,-1) ms, dirty FROM unified_kg_state WHERE notebook_id=?",
                      (nb,)).fetchone()
        except sqlite3.Error:
            st = None
        print(f"\n目标 notebook = {nb}  (用 --notebook <id> 指定)")
        print("  规模: KO %d(%s) sources=%d" % (
            sum(by_type.values()),
            " ".join(f"{k}={v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])), n_src))
        if st:
            print(f"  unified_kg_state: kg_seq={st['ks']} cluster_seq={st['cs']} "
                  f"mention_seq={st['ms']} dirty={st['dirty']}"
                  + ("   ⚠dirty=1:有未 rebuild 的 KG 变更,计数/图缓存可能反复失效" if st["dirty"] else ""))
        else:
            print("  unified_kg_state: (无行;任何 KG 写会建行并推进 seq)")

        # [1] 计数缓存冷成本(A 缓存的那条 GROUP BY;热态 memo≈0,此为冷未命中/每次重算成本)
        print("\n[1] 计数缓存(A)冷成本 —— 热态 memo 命中≈0;此为「首开 / 每次 KG 变更后」重付")
        _, t_in = _timed("SELECT object_type, status, COUNT(*) c FROM knowledge_objects "
                         "WHERE notebook_id=? GROUP BY object_type, status", (nb,))
        print(f"    GROUP BY object_type,status(A 缓存的原始查询): {_fmt(t_in)}"
              + ("   ⚠冷成本高:若你「每次都卡」看上面 dirty 与下面 seq churn" if t_in >= 1000 else ""))

        # [2] from_row 里 A 没覆盖的残余子查询
        print("\n[2] from_row 残余子查询(A 只缓存了 type_counts,以下每次打开仍现算)")
        residual = [
            ("sources COUNT", "SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb,)),
            ("pending_kg_source_count(相关子查询·头号嫌疑)",
             "SELECT COUNT(*) c FROM sources s WHERE s.notebook_id=? "
             "AND EXISTS(SELECT 1 FROM source_elements e WHERE e.source_id=s.id) "
             "AND NOT EXISTS(SELECT 1 FROM knowledge_objects k WHERE k.source_id=s.id AND k.source_id!='')",
             (nb,)),
            ("base_notebook_info(全局 tier=base)",
             "SELECT nb.name, EXISTS(SELECT 1 FROM knowledge_objects ko JOIN notebooks b "
             "ON b.id=ko.notebook_id WHERE b.tier='base') FROM notebooks nb WHERE nb.tier='base' "
             "ORDER BY nb.created_at ASC LIMIT 1", ()),
            ("has_kg EXISTS", "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id=?)", (nb,)),
        ]
        for label, sql, params in residual:
            try:
                _, t = _timed(sql, params)
                mstr, flag = _fmt(t), ("  ⚠残余大头" if t >= 500 else "")
            except sqlite3.Error as exc:
                mstr, flag = f"err:{exc}", ""
            print(f"    {label:44} {mstr:>9}{flag}")

        # [3] 生产请求日志真相 —— 该 nb 各端点 P50/P95/max(你实际体验到的)
        print("\n[3] 生产请求日志(requests.jsonl)—— 该 notebook 各端点真实延迟")
        buckets = {}
        seen = 0
        for f in _expand_request_files(local_dir):
            for rec in diag_slow._iter_jsonl(f):
                if rec.get("kind") != "http":
                    continue
                path = str(rec.get("path", ""))
                if nb not in path:
                    continue
                lat = rec.get("latency_ms")
                if not isinstance(lat, (int, float)):
                    continue
                seen += 1
                key = f"{rec.get('method','')} {path.replace(nb, '{id}')}"
                buckets.setdefault(key, []).append(float(lat))
        if not seen:
            print(f"    (requests.jsonl 无该 nb 记录;local={local_dir}。确认该 nb 近期被打开过、"
                  f"且 REQUEST 日志开着)")
        else:
            print(f"    {'endpoint':50} {'n':>5} {'P50':>8} {'P95':>8} {'max':>9}")
            for ep in sorted(buckets, key=lambda k: -diag_slow._pct(sorted(buckets[k]), 95)):
                v = sorted(buckets[ep])
                print(f"    {ep[:50]:50} {len(v):>5} {_fmt(diag_slow._pct(v,50)):>8} "
                      f"{_fmt(diag_slow._pct(v,95)):>8} {_fmt(v[-1]):>9}")

        print("\n判别提示:")
        print("  · [1] 冷成本大 + [3] 里 GET /notebooks/{id} 的 P95 也是秒级 + dirty=1/seq 常变")
        print("    → 缓存被后台 KG 变更反复冲掉,每次打开都冷算;根治=让 kg_mutation_seq 稳定/物化计数")
        print("  · [2] pending_kg_source_count 是残余大头 → 那条相关子查询(A 未缓存)是下一刀")
        print("  · [3] 秒级的不是 /notebooks/{id} 而是别的端点 → 打那个端点(/analytics /index-status /graph …)")
    finally:
        conn.close()
    print("\n=== 完 — 把以上整段贴回即可 " + "=" * 34)
    return 0


if __name__ == "__main__":
    sys.exit(main())
