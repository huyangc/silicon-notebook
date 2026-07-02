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

.local 定位:历史上服务从 backend/ 起时数据落在 backend/.local(「双 .local」坑,
后被 config._ROOT_DIR 锚定修复但存量数据可能没搬家)。本脚本对 root/.local 与
root/backend/.local 都探测,按 silicon_notebook.db 的 mtime 取最新者为准并打印判定。
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
    "relation_scoring_skipped", "tier2_skipped", "chunk_bruteforce_skipped",
    "kg_bruteforce_refused", "scale_index_build",
    "model_error", "ask_stage", "pipeline",
)
# 「大库」画像阈值:对象+chunk 超过它才打印逐项诊断旗标
BIG_NB_ROWS = 20_000
_IN_CHUNK = 800   # 只读 IN(...) 批大小,留余量避开 SQLITE_MAX_VARIABLE_NUMBER


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


def resolve_local(root):
    """探测 root/.local 与 root/backend/.local,按 DB mtime 取最新者。
    返回 local_dir(可能不存在——各段自行降级)。"""
    section(".local 定位(双 .local 坑探测)")
    cands = []
    for rel in (".local", os.path.join("backend", ".local")):
        d = os.path.join(root, rel)
        db = os.path.join(d, "silicon_notebook.db")
        if os.path.exists(db):
            st = os.stat(db)
            cands.append((st.st_mtime, d, st.st_size))
    if not cands:
        d = os.path.join(root, ".local")
        print(f"(两处都没有 silicon_notebook.db,按默认 {d} 继续 — 请确认 --root 传的是仓库根)")
        return d
    cands.sort(reverse=True)
    for mtime, d, size in cands:
        mark = " ← 采用(mtime 最新)" if d == cands[0][1] else ""
        print(f"  {d}: db {size/1e9:.2f} GB, mtime {datetime.fromtimestamp(mtime).isoformat(timespec='seconds')}{mark}")
    if len(cands) > 1:
        print("  ⚠ 两个 .local 同时存在:服务实际用哪个取决于部署版本(新版锚定仓库根)。"
              "若此报告的库规模与你的认知不符,用 --local 显式指定另一处重跑")
    return cands[0][1]


def report_requests(local_dir, since, slow_ms):
    path = os.path.join(local_dir, "logs", "requests.jsonl")
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
    print("(注意:仍在挂起/未完成的请求不会出现在此日志 — 正在冻结的那次 ask 看不到,"
          "要靠 py-spy dump 抓活栈)")
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


def report_events(local_dir, since):
    paths = sorted(set(
        glob.glob(os.path.join(local_dir, "logs", "events.jsonl"))
        + glob.glob(os.path.join(local_dir, "logs", "*", "events.jsonl"))))
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
                       "relation_scoring_skipped", "tier2_skipped",
                       "chunk_bruteforce_skipped", "kg_bruteforce_refused"):
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
        print("\n[model_error 按 stage/model] — embed 失败会让 KG 检索退回全量暴力,重点看")
        for r, c in model_errors.most_common(8):
            print(f"  {c:>6}  {r}")
        for ts, key, msg in last_model_errors[-5:]:
            print(f"  最近: {ts} {key} :: {msg}")
    if ask_stage:
        print("\n[ask 各阶段延迟 — 仅 chunk 模式有埋点;reasoning/graph 模式无 ask_stage(观测盲区)]")
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


def report_llm(local_dir, since):
    paths = sorted(set(
        glob.glob(os.path.join(local_dir, "logs", "llm.jsonl"))
        + glob.glob(os.path.join(local_dir, "logs", "*", "llm.jsonl"))))
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


def _vec_dim(raw):
    """单行向量的维度:BLOB→字节数/4(float32);TEXT→json 数组长度。失败返回 None。"""
    try:
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return len(bytes(raw)) // 4
        if isinstance(raw, str):
            return len(json.loads(raw))
    except Exception:  # noqa: BLE001
        return None
    return None


def _sample_typeof(conn, table):
    """最旧/最新各采 1 行 typeof(vector) — O(1),不做全表扫。
    旧行是 text 说明有未迁移残留(确切计数需 --deep)。"""
    out = []
    for order in ("ASC", "DESC"):
        try:
            r = conn.execute(
                f"SELECT typeof(vector) t FROM {table} ORDER BY rowid {order} LIMIT 1"
            ).fetchone()
            out.append(r["t"] if r else "-")
        except sqlite3.Error:
            out.append("?")
    return out  # [oldest, newest]


def report_scale_profile(local_dir):
    """per-notebook 规模画像 + scale 索引健康诊断(本段是「严格推理慢」的主证据):
    - 无索引的大库 → KG 对象检索走全量暴力(json 解析 55w payload+全量分词+GB 级矩阵)
    - 索引在但 delta 大 → KG 对象侧 delta 每次查询无条件暴力(chunk 侧默认关,对象侧没开关)
    - manifest dim 与库内向量维度失配 → ANN 静默失效,悄悄回退全量
    只读、每查询都是聚合/LIMIT 1,秒级。"""
    section("per-notebook 规模画像 + scale 索引诊断")
    db = os.path.join(local_dir, "silicon_notebook.db")
    if not os.path.exists(db):
        print(f"(缺 {db})")
        return
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        print(f"(DB 只读打开失败: {exc})")
        return
    try:
        tiers = {r["id"]: (r["tier"] if "tier" in r.keys() else "personal")
                 for r in conn.execute("SELECT * FROM notebooks").fetchall()}
        base_nbs = [n for n, t in tiers.items() if t == "base"]
        print(f"notebook 总数 {len(tiers)},其中 tier=base {len(base_nbs)} 个"
              f"{'(federated 检索会把每个 base 都并进每次查询)' if len(base_nbs) > 1 else ''}")

        def group_count(sql):
            try:
                return {r["nb"]: r["c"] for r in conn.execute(sql).fetchall()}
            except sqlite3.Error:
                return {}

        ko_by_type = defaultdict(dict)
        try:
            for r in conn.execute(
                    "SELECT notebook_id nb, object_type ot, COUNT(*) c "
                    "FROM knowledge_objects GROUP BY 1, 2").fetchall():
                ko_by_type[r["nb"]][r["ot"]] = r["c"]
        except sqlite3.Error:
            pass
        chunks = group_count("SELECT notebook_id nb, COUNT(*) c FROM chunks GROUP BY 1")
        rels = group_count("SELECT notebook_id nb, COUNT(*) c FROM knowledge_relations GROUP BY 1")
        srcs = group_count("SELECT notebook_id nb, COUNT(*) c FROM sources GROUP BY 1")
        emb = {}
        for t in ("knowledge_embeddings", "chunk_embeddings",
                  "element_embeddings", "relation_embeddings"):
            emb[t] = group_count(f"SELECT notebook_id nb, COUNT(*) c FROM {t} GROUP BY 1")

        def total_rows(nb):
            return sum(ko_by_type.get(nb, {}).values()) + chunks.get(nb, 0)

        idx_root = os.path.join(local_dir, "storage", "kg_index")
        ordered = sorted(tiers, key=lambda n: -total_rows(n))
        for nb in ordered[:12]:
            kos = ko_by_type.get(nb, {})
            n_ko = sum(kos.values())
            if n_ko + chunks.get(nb, 0) == 0:
                continue
            ko_desc = " ".join(f"{k}={v}" for k, v in sorted(kos.items(), key=lambda kv: -kv[1]))
            print(f"\n  {nb} tier={tiers[nb]}: KO {n_ko}({ko_desc}) "
                  f"chunks={chunks.get(nb, 0)} edges={rels.get(nb, 0)} sources={srcs.get(nb, 0)}")
            print("    embeddings: " + " ".join(
                f"{t.split('_')[0]}={emb[t].get(nb, 0)}" for t in emb))
            if total_rows(nb) < BIG_NB_ROWS:
                continue
            # —— 大库:逐项诊断旗标 ——
            mpath = os.path.join(idx_root, nb, "manifest.json")
            if not os.path.exists(mpath):
                print("    ⚠ 大库但没有 scale 索引 manifest → KG 对象检索/种子全部走"
                      "全量暴力路径(全表 json 解析+分词+GB 级矩阵),reasoning 首查可达数十分钟")
                continue
            try:
                mf = json.load(open(mpath))
            except Exception:  # noqa: BLE001
                print(f"    ⚠ manifest 损坏: {mpath} → 索引加载失败=同「无索引」,全量暴力")
                continue
            dim = mf.get("dim")
            print(f"    索引: n_nodes={mf.get('n_nodes')} n_ann={mf.get('n_ann')} "
                  f"chunk_ann={mf.get('n_chunk_ann', 0)} relation_ann={mf.get('n_relation_ann', 0)} "
                  f"dim={dim}")
            if not mf.get("has_chunk_ann"):
                print("    ⚠ 索引无 chunk_ann → PPR 种子/chunk 检索对全部 chunk 逐条分词打分"
                      "(每次查询重付)")
            # 维度失配探测:ANN 静默失效的头号来源(换 embed 模型后没重建索引)
            r = conn.execute(
                "SELECT vector FROM knowledge_embeddings WHERE notebook_id=? LIMIT 1",
                (nb,)).fetchone()
            live_dim = _vec_dim(r["vector"]) if r else None
            if dim and live_dim and int(dim) != int(live_dim):
                print(f"    ⚠⚠ 维度失配: manifest dim={dim} vs 库内向量 dim={live_dim} → "
                      f"ANN 每次静默跳过(无事件!),KG 对象检索悄悄回退全量暴力 — 需重建索引")
            # 水位 delta:对象侧 delta 暴力没有开关,每次查询都付
            wm = set(mf.get("watermark_sources", []))
            all_src = [r["id"] for r in conn.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (nb,)).fetchall()]
            delta_src = [s for s in all_src if s not in wm]
            if delta_src:
                covered = 0
                wml = list(wm)
                for i in range(0, len(wml), _IN_CHUNK):
                    batch = wml[i:i + _IN_CHUNK]
                    ph = ",".join("?" for _ in batch)
                    covered += conn.execute(
                        f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=? "
                        f"AND source_id IN ({ph})", (nb, *batch)).fetchone()["c"]
                delta_chunks = max(0, chunks.get(nb, 0) - covered)
                print(f"    ⚠ 索引水位后新增 {len(delta_src)} 个 source(约 {delta_chunks} chunk)"
                      f"未收进索引 → KG 对象侧 delta 每次查询无条件暴力(现查现建矩阵、无缓存);"
                      f"chunk 侧默认只搜已索引部分。建议手动触发 fold/重建,别等低峰窗口")
            else:
                print("    索引水位新鲜(无 delta source) ✓")
        # 向量 text 残留快速采样(O(1);确切计数用 --deep)
        print("\n  [向量存储格式采样: 每表最旧/最新各 1 行 typeof]")
        for t in ("knowledge_embeddings", "chunk_embeddings",
                  "element_embeddings", "relation_embeddings"):
            old, new = _sample_typeof(conn, t)
            flag = ("   ⚠ 最旧行仍是 text — 有未迁移 JSON 残留,全量矩阵加载=分钟~数十分钟级"
                    "(49w 行实测 ~36min),用 --deep 拿确切计数" if old == "text" else " ✓")
            print(f"    {t}: oldest={old} newest={new}{flag}")
    finally:
        conn.close()


def report_artifacts(local_dir, deep):
    section("DB / 索引工件")
    db = os.path.join(local_dir, "silicon_notebook.db")
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            print(f"  {os.path.getsize(f)/1e9:8.2f} GB  {os.path.basename(f)}")
    if os.path.exists(db + "-wal") and os.path.getsize(db + "-wal") > 1e9:
        print("  ⚠ WAL > 1GB:有长期读快照挡住 checkpoint(常见=常驻服务长事务),重启服务后应回落")
    idx_root = os.path.join(local_dir, "storage", "kg_index")
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
        # 双 .local 同款坑:老部署 .env 可能在 backend/ 下
        alt = os.path.join(root, "backend", ".env")
        if os.path.exists(alt):
            path = alt
            print(f"(根下无 .env,改用 {alt})")
        else:
            print("(无 .env)")
            return
    interesting = (
        "RELATION_RETRIEVAL_ENABLED", "CHUNK_KG_OVERLAY_ENABLED", "CHUNK_ANN_ENABLED",
        "CHUNK_BRUTEFORCE_MAX_CHUNKS",
        "SCALE_INDEX_AUTO_ENABLED", "SCALE_INDEX_AUTO_WHEN",
        "SCALE_SEARCH_INCLUDE_DELTA", "SCALE_AUTO_FOLD_ON_ADD",
        "SCALE_INDEX_OFFPEAK_START_HOUR", "SCALE_INDEX_OFFPEAK_END_HOUR",
        "INDEX_STALE_DELTA_THRESHOLD",
        "PPR_EMB_SYNONYM_ENABLED", "PPR_FACT_RERANK_ENABLED", "GRAPH_PPR_ENABLED",
        "PPR_TOP_CHUNKS", "PPR_KG_SEED_TOP_N", "PPR_CHUNK_SEED_TOP_N",
        "REASONING_MAX_STEPS", "REASONING_STALE_LIMIT", "REASONING_MAX_SUBQUERIES",
        "REASONING_TIMEOUT_SECONDS", "REASONING_QUOTA_ENABLED",
        "NOTEBOOK_COPY_MAX_BYTES", "NOTEBOOK_COPY_MAX_ROWS",
        "KG_AUTO_EXTRACT", "EMBED_MODEL", "EMBED_DIM",
        "OPENAI_COMPAT_MODEL", "REASONING_LLM_MODEL", "VECTOR_CACHE_MAX_ENTRIES",
        "SCALE_IDX_CACHE_MAX", "EDGE_CENTRALITY_MAX_NODES", "HNSW_EF_CONSTRUCTION",
        "CHUNK_RECALL", "SILICON_NOTEBOOK_STORAGE_DIR", "DATABASE_URL",
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
    print("  (未列出的 = .env 没写,走代码默认值;关键默认: SCALE_INDEX_AUTO_WHEN=idle→"
          "只在低峰 2-6 点建索引、CHUNK_ANN_ENABLED=true、SCALE_SEARCH_INCLUDE_DELTA=false)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="仓库根(默认当前目录)")
    ap.add_argument("--local", default="", help="显式指定 .local 目录(默认自动探测)")
    ap.add_argument("--since", type=float, default=48, help="回看小时数(默认 48)")
    ap.add_argument("--slow-ms", type=int, default=3000, help="慢请求阈值 ms(默认 3000)")
    ap.add_argument("--deep", action="store_true",
                    help="额外做 DB 检查(typeof 全表扫,大库分钟级,只读)")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    since = timedelta(hours=args.since)
    print(f"silicon-notebook 慢因诊断  root={root}  窗口={args.since}h  "
          f"生成于 {datetime.now().isoformat(timespec='seconds')}")
    local_dir = os.path.abspath(args.local) if args.local else resolve_local(root)
    report_requests(local_dir, since, args.slow_ms)
    report_events(local_dir, since)
    report_llm(local_dir, since)
    report_scale_profile(local_dir)
    report_artifacts(local_dir, args.deep)
    report_env(root)
    print("\n=== 完 — 把以上整段输出贴回即可 " + "=" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
