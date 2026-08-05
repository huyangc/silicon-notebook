#!/usr/bin/env python3
"""诊断:打开大 notebook 仍卡几秒的残余延迟花在哪(#245 的 A/B/D/F/E 落地后)。

主机侧只读诊断,纯 stdlib、不 import app(与 diag_slow.py 同款,可在持有 .local/ 的
部署机上直接跑,不需要 app 依赖)。sqlite 以 mode=ro 打开,绝不改数据。示例:

    python3 scripts/diag_open_latency.py                 # 仓库根跑,自动取最大 notebook
    python3 scripts/diag_open_latency.py --notebook nb-xxx
    python3 scripts/diag_open_latency.py --root /path/to/repo

把整段输出贴回来即可。测量「打开一个 notebook」真实触发的关键查询耗时 + 生产请求
日志里各端点的真实 P50/P95/max,用来定位 A(计数缓存)之后剩下的几秒是:①计数缓存
【冷未命中】的 2M GROUP BY 成本(重启后 / 该 nb 每次 KG 变更后首开重付);②打开必发的
scale-index/status 数 chunks 全表 + sources 物化 + pending 相关子查询(均未缓存);③别的端点吃秒;④后台在建/
摄取导致 kg_mutation_seq 一直变、缓存永远命不中(每次都冷算)。

复用 diag_slow.py 的 .local 定位 / 日志迭代 / 分位数助手(同目录,纯 stdlib)。"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import diag_common  # noqa: E402 — stdlib sibling; database-target resolution
import diag_slow  # noqa: E402 — stdlib sibling host diagnostic; reuse its helpers
import diag_common  # noqa: E402 — bounded historical offline log reader

USABLE = ("approved", "reviewed", "project_specific", "conflict")


def _fmt(ms: float) -> str:
    return f"{ms/1000:.2f}s" if ms >= 1000 else f"{ms:.0f}ms"


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="仓库根(默认当前目录)")
    ap.add_argument("--local", default="", help="显式指定 .local 目录(默认自动探测)")
    ap.add_argument("--notebook", default="", help="目标 notebook id(默认自动取最大库)")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    local_dir = os.path.abspath(args.local) if args.local else diag_slow.resolve_local(root)

    print("=" * 70)
    print("== 打开延迟诊断(#245 落地后残余卡顿定位;只读 mode=ro)==")
    target = diag_common.resolve_database_target(root)
    if not target.is_sqlite:
        print(target.skip_note("本诊断"))
        return 0
    # 解析出的文件优先:确认后端是 SQLite 还不够,非默认路径下固定路径同样陈旧。
    db_path = target.resolve_sqlite_file(local_dir)
    if not db_path or not os.path.exists(db_path):
        print("(缺 SQLite database;用 --local 指定 local data directory)")
        return 0
    try:
        # 文件名可能含 `?`(配置里的 query 是服务打开的那个名字的一部分),
        # 裸拼会被 SQLite 当成 URI 参数,所以走统一的编码构造点。
        conn = sqlite3.connect(
            target.sqlite_readonly_uri(local_dir), uri=True, timeout=60
        )
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        print("(DB 只读打开失败: sqlite_error)")
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
            except sqlite3.Error:
                print("(选最大 notebook 失败: sqlite_error)")
                return 0
        if not nb:
            print("(库中无 knowledge_objects)")
            return 0

        # 规模 + seq/dirty 状态
        by_type = {}
        for row in _run(
                "SELECT object_type ot, COUNT(*) c FROM knowledge_objects "
                "WHERE notebook_id=? GROUP BY 1", (nb,)).fetchall():
            object_type = (row["ot"] if row["ot"] in {
                "concept", "claim", "formula", "procedure"
            } else "other")
            by_type[object_type] = by_type.get(object_type, 0) + int(row["c"])
        n_src = _run("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb,)).fetchone()["c"]
        try:
            st = _run("SELECT COALESCE(kg_mutation_seq,0) ks, COALESCE(cluster_mutation_seq,0) cs, "
                      "COALESCE(mention_seq,-1) ms, dirty FROM unified_kg_state WHERE notebook_id=?",
                      (nb,)).fetchone()
        except sqlite3.Error:
            st = None
        print(f"\n目标 notebook = {diag_common.pseudonym('notebook', nb)}  "
              "(用 --notebook <id> 指定)")
        print("  规模: KO %d(%s) sources=%d" % (
            sum(by_type.values()),
            " ".join(f"{k}={v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])), n_src))
        if st:
            kg_seq = int(diag_common.finite_number(st["ks"]) or 0)
            cluster_seq = int(diag_common.finite_number(st["cs"]) or 0)
            mention_seq = int(diag_common.finite_number(st["ms"], minimum=-1) or 0)
            dirty = st["dirty"] in {1, True, "1", "true"}
            print(f"  unified_kg_state: kg_seq={kg_seq} cluster_seq={cluster_seq} "
                  f"mention_seq={mention_seq} dirty={int(dirty)}"
                  + ("   ⚠dirty=1:有未 rebuild 的 KG 变更,计数/图缓存可能反复失效" if dirty else ""))
        else:
            print("  unified_kg_state: (无行;任何 KG 写会建行并推进 seq)")

        # [1] 计数缓存冷成本(A 缓存的那条 GROUP BY;热态 memo≈0,此为冷未命中/每次重算成本)
        print("\n[1] 计数缓存(A)冷成本 —— 热态 memo 命中≈0;此为「首开 / 每次 KG 变更后」重付")
        _, t_in = _timed("SELECT object_type, status, COUNT(*) c FROM knowledge_objects "
                         "WHERE notebook_id=? GROUP BY object_type, status", (nb,))
        print(f"    GROUP BY object_type,status(A 缓存的原始查询): {_fmt(t_in)}"
              + ("   ⚠冷成本高:若你「每次都卡」看上面 dirty 与下面 seq churn" if t_in >= 1000 else ""))

        # [2] 打开/看板/知识库各面真正触发的查询逐条计时(faithful mirror 生产 SQL,只读)。
        #     ⚠OPEN=选中 notebook 必付(与 rebuild 无关、每次都冷算);其余按点击面分组。
        print("\n[2] 各面真实查询计时(mirror 生产 SQL;⚠OPEN=每次打开必付)")

        def _row(label, sql, params=(), flag=""):
            try:
                _, t = _timed(sql, params)
                mstr = _fmt(t)
                if not flag and t >= 500:
                    flag = "  ⚠秒级"
            except sqlite3.Error:
                mstr, flag = "err:sqlite_error", ""
            print(f"    {label:54} {mstr:>9}{flag}")

        _PENDING = ("SELECT COUNT(*) c FROM sources s WHERE s.notebook_id=? "
                    "AND EXISTS(SELECT 1 FROM source_elements e WHERE e.source_id=s.id) "
                    "AND NOT EXISTS(SELECT 1 FROM knowledge_objects k "
                    "WHERE k.source_id=s.id AND k.source_id!='')")
        _PAGE50 = ("(SELECT id FROM sources WHERE notebook_id=? "
                   "ORDER BY created_at ASC LIMIT 50)")

        print("  ── OPEN:每次打开必付(openNotebook + [currentNotebookId] effect)──")
        _row("scale-index/status: chunks COUNT(*)  ←头号",
             "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (nb,), "  ⚠OPEN")
        _row(f"scale-index/status: sources id 全量物化(N={n_src})",
             "SELECT id FROM sources WHERE notebook_id=?", (nb,), "  ⚠OPEN")
        _row("notebooks/{id}: pending_kg_source_count(相关子查询)",
             _PENDING, (nb,), "  ⚠OPEN")
        _row("notebooks/{id}+sources: sources COUNT",
             "SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb,))
        _row("sources: kg_extracted 水合(最新50源→KO DISTINCT)",
             "SELECT DISTINCT source_id FROM knowledge_objects "
             "WHERE source_id IN " + _PAGE50 + " AND source_id!=''", (nb,))

        print("  ── OPEN·冷簇:重启后 / 该 nb 每次 KG 写后首开重付,之后 memo 命中 ──")
        _row("version_facts: knowledge_objects COUNT+MAX",
             "SELECT COUNT(*) c, COALESCE(MAX(updated_at),'') ts "
             "FROM knowledge_objects WHERE notebook_id=?", (nb,))
        _row("version_facts: concept_clusters COUNT+MAX",
             "SELECT COUNT(*) c, COALESCE(MAX(created_at),'') ts "
             "FROM concept_clusters WHERE notebook_id=?", (nb,))
        print("    (chunks 冷簇再数一遍见上;[1] 的 GROUP BY 是另一条冷聚合)")

        print("  ── 看板:点「看板」才发(非打开)──")
        _row("analytics: sources parse_status GROUP BY",
             "SELECT parse_status, COUNT(*) c FROM sources WHERE notebook_id=? "
             "GROUP BY parse_status", (nb,), "  ⚠无(nb,parse_status)索引")
        # COUNT(DISTINCT canonical_id):线上仅当 unified_kg_state.cluster_count 未持久化才付
        # (55min 元凶);已持久化(非0)线上跳过。concept_clusters 巨大时 diag 不实跑(会挂几十秒~分钟)。
        try:
            _ccrow = _run("SELECT COALESCE(cluster_count,0) cc FROM unified_kg_state "
                          "WHERE notebook_id=?", (nb,)).fetchone()
            _cc = _ccrow["cc"] if _ccrow else 0
        except sqlite3.Error:
            _cc = 0
        try:
            _members = _run("SELECT COUNT(*) c FROM concept_clusters WHERE notebook_id=?",
                            (nb,)).fetchone()["c"]
        except sqlite3.Error:
            _members = 0
        _lbl = "index-status→unified_kg: COUNT(DISTINCT canonical_id)"
        if _cc:
            print(f"    {_lbl}  (线上跳过:cluster_count={_cc} 已持久化)")
        elif _members >= 1_000_000:
            print(f"    {_lbl}  ⚠未持久化·线上每次全跑于 {_members} 行(=55min 元凶);"
                  "diag 不实跑(会挂)")
        else:
            _row(_lbl + " ←55min元凶",
                 "SELECT COUNT(DISTINCT canonical_id) c FROM concept_clusters "
                 "WHERE notebook_id=?", (nb,), "  ⚠未持久化·线上每次付")

        print("  ── 知识库 tab:点「知识库」才发(非打开)──")
        for _ot in ("concept", "claim"):
            _row(f"knowledge: list COUNT object_type={_ot}",
                 "SELECT COUNT(*) c FROM knowledge_objects "
                 "WHERE notebook_id=? AND object_type=?", (nb, _ot))

        # [3] 生产请求日志真相 —— 该 nb 各端点 P50/P95/max(你实际体验到的)
        print("\n[3] 生产请求日志(requests.jsonl)—— 该 notebook 各端点真实延迟")
        buckets = {}
        seen = 0
        request_read = diag_common.read_channel(Path(local_dir) / "logs", "requests")
        for rec in request_read.records:
            if rec.get("kind") != "http":
                continue
            path = str(rec.get("path", ""))
            if nb not in path:
                continue
            latency = diag_common.finite_number(rec.get("latency_ms"))
            if latency is not None:
                seen += 1
                method = str(rec.get("method", "")).upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                    method = "OTHER"
                key = f"{method} {diag_common.normalize_http_path(path)}"
                buckets.setdefault(key, []).append(latency)
        print("    " + diag_slow._scan_summary(request_read.stats))
        if not seen:
            print("    (requests.jsonl 无该 notebook 记录;确认该 notebook 近期被打开过、"
                  "且 REQUEST 日志开着)")
        else:
            print(f"    {'endpoint':50} {'n':>5} {'P50':>8} {'P95':>8} {'max':>9}")
            for ep in sorted(buckets, key=lambda k: -diag_slow._pct(sorted(buckets[k]), 95)):
                v = sorted(buckets[ep])
                print(f"    {ep[:50]:50} {len(v):>5} {_fmt(diag_slow._pct(v,50)):>8} "
                      f"{_fmt(diag_slow._pct(v,95)):>8} {_fmt(v[-1]):>9}")

        print("\n判别提示:")
        print("  · 「每次打开都卡 5-6s、rebuild 完照样卡」= [2] 的 ⚠OPEN 几条之和")
        print("    (chunks COUNT + sources 物化 + pending_kg_source_count);无缓存、只随规模走,")
        print("    与 rebuild 无关;根治=给这几条按 kg_mutation_seq 上计数缓存(同 A),非加索引")
        print("  · 「点看板卡」= [2] 看板组:parse_status GROUP BY 缺 (nb,parse_status) 索引;")
        print("    COUNT(DISTINCT canonical_id) 未持久化时=55min 元凶(finish_rebuild 落 cluster_count 才免)")
        print("  · 「点知识库卡」= [2] 知识库组 list COUNT(concept 桶最大)")
        print("  · [3] 用生产真实 P50/P95 对照:哪个端点秒级先打哪个")
    finally:
        conn.close()
    print("\n=== 完 — 把以上整段贴回即可 " + "=" * 34)
    return 0


def main(argv=None) -> int:
    return diag_common.run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    sys.exit(main())
