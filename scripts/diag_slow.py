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
import importlib.util
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import diag_common

MAX_SAMPLES = 50_000
DEFAULT_REPORT_OUTPUT_BYTES = 32 * 1024
REQUEST_REPORT_OUTPUT_BYTES = 32 * 1024
_REQUEST_PATH_ROWS_BYTES = 20 * 1024
_REQUEST_SLOW_ROWS_BYTES = 8 * 1024
SECRET_MARKERS = ("KEY", "TOKEN", "PASSWORD", "SECRET")
# 这些事件是历次事故修复埋的观测点,出现即是直接证据。
INTEREST_KINDS = (
    "scale_ppr_bailout", "ppr_fallback_refused", "graph_walk_refused",
    "relation_scoring_skipped", "tier2_skipped", "chunk_bruteforce_skipped",
    "kg_bruteforce_refused", "element_scoring_skipped", "dim_mismatch",
    "scale_fold_refused", "viz_lazy_build_refused",
    "scale_index_build", "scale_ppr_stage",
    "model_error", "ask_stage", "pipeline",
)
# 「大库」画像阈值:对象+chunk 超过它才打印逐项诊断旗标
BIG_NB_ROWS = 20_000
_IN_CHUNK = 800   # 只读 IN(...) 批大小,留余量避开 SQLITE_MAX_VARIABLE_NUMBER
_SAFE_STAGES = frozenset({
    "answer", "answer_llm", "embed", "embedding", "expand", "extract", "graph", "index", "load_indexes",
    "parse", "pipeline", "ppr", "report", "retrieval", "score", "seed", "total",
    "chunk_ann", "chunk_fts", "chunk_scale_index", "kg_candidates",
})
_SAFE_OBJECT_TYPES = frozenset({"concept", "claim", "formula", "procedure"})
_ENV_BOOL_KEYS = frozenset({
    "RELATION_RETRIEVAL_ENABLED", "CHUNK_KG_OVERLAY_ENABLED", "CHUNK_ANN_ENABLED",
    "SCALE_INDEX_AUTO_ENABLED", "SCALE_SEARCH_INCLUDE_DELTA", "SCALE_AUTO_FOLD_ON_ADD",
    "PPR_EMB_SYNONYM_ENABLED", "PPR_FACT_RERANK_ENABLED", "GRAPH_PPR_ENABLED",
    "REASONING_QUOTA_ENABLED", "KG_AUTO_EXTRACT",
})
_ENV_ENUMS = {"SCALE_INDEX_AUTO_WHEN": frozenset({"idle", "now"})}
_ENV_READ_LIMIT_BYTES = 64 * 1024
_MANIFEST_READ_LIMIT_BYTES = 1024 * 1024


class _BoundedOutput:
    """Write a complete default report within a byte envelope."""

    _MARKER = "\n[output_truncated=True report_budget={budget} bytes]\n"

    def __init__(self, stream, budget):
        self._stream = stream
        self._budget = max(1, int(budget))
        self._marker = self._MARKER.format(budget=self._budget)
        self._content_budget = max(0, self._budget - len(self._marker.encode("utf-8")))
        self._used = 0
        self._truncated = False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def write(self, text):
        if self._truncated:
            return len(text)
        encoded = str(text).encode("utf-8", "replace")
        remaining = self._content_budget - self._used
        if len(encoded) <= remaining:
            self._stream.write(str(text))
            self._used += len(encoded)
            return len(text)
        if remaining > 0:
            clipped = encoded[:remaining].decode("utf-8", "ignore")
            self._stream.write(clipped)
            self._used += len(clipped.encode("utf-8"))
        self._stream.write(self._marker)
        self._truncated = True
        return len(text)

    def flush(self):
        return self._stream.flush()

    def isatty(self):
        return self._stream.isatty()


def _redact_database_url(raw):
    """Load the isolated core URL redactor without importing the app package."""
    module_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "backend",
        "app",
        "core",
        "database_url.py",
    )
    try:
        spec = importlib.util.spec_from_file_location("silicon_notebook_database_url", module_path)
        if spec is None or spec.loader is None:
            return "<invalid/redacted database URL>"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.redact_database_url(raw)
    except Exception:  # diagnostics must fail closed rather than print the raw URL
        return "<invalid/redacted database URL>"


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = min(len(sorted_vals) - 1, max(0, int(round((p / 100.0) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def _fmt_ms(v):
    number = diag_common.finite_number(v)
    if number is None:
        return "invalid"
    return f"{number/1000:.1f}s" if number >= 1000 else f"{int(number)}ms"


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except Exception:  # noqa: BLE001
        return None


def _iter_jsonl(path):
    """Compatibility wrapper for callers that consume JSON records only."""
    for record, bad, _ in diag_common.iter_jsonl_file(Path(path)):
        if not bad and record is not None:
            yield record


def _scan_summary(stats):
    return (f"日志 files={stats.files} matched={stats.matched} malformed={stats.malformed} "
            f"duplicates={stats.duplicates} retained={stats.retained} truncated={stats.truncated}")


def _safe_stage(value):
    stage = str(value).lower()
    return stage if stage in _SAFE_STAGES else "other"


def _safe_presence(value):
    return "present" if value not in (None, "") else "absent"


def _safe_object_type(value):
    return value if value in _SAFE_OBJECT_TYPES else "other"


def _safe_count(value, default=0):
    number = diag_common.finite_number(value)
    return int(number) if number is not None else default


def _safe_tier(value):
    return value if value in {"base", "personal"} else "unknown"


def _read_manifest(path):
    try:
        with open(path, "rb") as handle:
            payload = handle.read(_MANIFEST_READ_LIMIT_BYTES + 1)
        if len(payload) > _MANIFEST_READ_LIMIT_BYTES:
            return {}
        parsed = json.loads(payload.decode("utf-8", "strict"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _print_bounded_request_rows(lines, byte_budget, label):
    """Print only the useful prefix of a request table within a byte budget."""
    used = 0
    omitted = 0
    reserve = 160
    for line in lines:
        size = len((line + "\n").encode("utf-8", "replace"))
        if used + size <= byte_budget - reserve:
            print(line)
            used += size
        else:
            omitted += 1
    if omitted:
        print(f"  ... {omitted} {label} omitted; output_truncated=True "
              f"budget={byte_budget} bytes")


def _env_path(root):
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        return path
    alt = os.path.join(root, "backend", ".env")
    return alt if os.path.exists(alt) else ""


def _read_env(root):
    path = _env_path(root)
    seen = {}
    if not path:
        return seen
    try:
        with open(path, "rb") as handle:
            payload = handle.read(_ENV_READ_LIMIT_BYTES + 1)
        if len(payload) > _ENV_READ_LIMIT_BYTES:
            return seen
        for line in payload.decode("utf-8", "strict").splitlines()[:512]:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            seen[k.strip()] = v.strip()
    except (OSError, UnicodeError):
        return seen
    return seen


def _env_bool(env, key, default):
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Sampler:
    """有界延迟采样:count/max 精确,分位数在样本封顶后为近似。"""

    def __init__(self):
        self.count = 0
        self.max = 0
        self.samples = []

    def add(self, v):
        v = diag_common.finite_number(v)
        if v is None:
            return
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
            label = "root-local" if rel == ".local" else "backend-local"
            cands.append((st.st_mtime, d, st.st_size, label))
    if not cands:
        d = os.path.join(root, ".local")
        print("(两处都没有 silicon_notebook.db,按默认 root-local 继续 — 请确认 --root 传的是仓库根)")
        return d
    cands.sort(reverse=True)
    for mtime, d, size, label in cands:
        mark = " ← 采用(mtime 最新)" if d == cands[0][1] else ""
        print(f"  {label}: db {size/1e9:.2f} GB, "
              f"mtime {datetime.fromtimestamp(mtime).isoformat(timespec='seconds')}{mark}")
    if len(cands) > 1:
        print("  ⚠ 两个 .local 同时存在:服务实际用哪个取决于部署版本(新版锚定仓库根)。"
              "若此报告的库规模与你的认知不符,用 --local 显式指定另一处重跑")
    return cands[0][1]


def report_requests(local_dir, since, slow_ms):
    request_read = diag_common.read_channel(
        Path(local_dir) / "logs", "requests", since_hours=since.total_seconds() / 3600)
    section(f"HTTP 请求(近 {since} 内;慢阈值 {_fmt_ms(slow_ms)})")
    print(_scan_summary(request_read.stats))
    if not request_read.stats.files:
        print("(没有 requests 日志)")
        return
    per_path = defaultdict(Sampler)
    slow_list = []          # (ts, ms, path)
    long_reqs = []          # (start, end, path) 用于并发风暴检测,>10s 的才记
    total = 0
    for e in request_read.records:
        ts = _parse_ts(e.get("ts", ""))
        if ts is None:
            continue
        ms = diag_common.finite_number(e.get("latency_ms"))
        p = e.get("path", "?")
        if ms is None:
            continue
        total += 1
        norm = diag_common.normalize_http_path(p)
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
        _print_bounded_request_rows(
            (f"  {slow_paths[k].line()}  {k}"
             for k in sorted(slow_paths, key=lambda k: -slow_paths[k].max)),
            _REQUEST_PATH_ROWS_BYTES,
            "path rows",
        )
        slow_list.sort(key=lambda t: -t[1])
        print("\n[单次最慢 Top 15]")
        _print_bounded_request_rows(
            (f"  {ts}  {_fmt_ms(ms):>8}  {p}" for ts, ms, p in slow_list[:15]),
            _REQUEST_SLOW_ROWS_BYTES,
            "slow rows",
        )
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
    event_read = diag_common.read_channel(
        Path(local_dir) / "logs", "events", since_hours=since.total_seconds() / 3600)
    section(f"事件观测点(近 {since} 内;文件 {event_read.stats.files} 个)")
    print(_scan_summary(event_read.stats))
    if not event_read.stats.files:
        print("(没有 events 日志)")
        return
    kind_count = Counter()
    bail_reasons = Counter()
    refuse_sites = Counter()
    model_errors = Counter()
    last_bail_diag = []
    ask_stage = defaultdict(Sampler)
    pipe_stage = defaultdict(Sampler)
    scale_ppr_stage = defaultdict(Sampler)
    last_build = None
    build_stages = {}
    for e in event_read.records:
            k = e.get("kind", "")
            if k not in INTEREST_KINDS:
                continue
            ts = _parse_ts(e.get("ts", ""))
            if ts is None:
                continue
            kind_count[k] += 1
            if k == "scale_ppr_bailout":
                reason = "zero_reset" if e.get("reason") == "zero_reset" else "other"
                bail_reasons[reason] += 1
                if e.get("reason") == "zero_reset":
                    diag = {}
                    for kk in ("ann_seeds", "active_seeds", "chunk_seeds",
                               "embed_ok", "ann_sources_skipped"):
                        if kk not in e:
                            continue
                        if isinstance(e.get(kk), bool):
                            diag[kk] = e[kk]
                        else:
                            number = diag_common.finite_number(e.get(kk))
                            diag[kk] = int(number) if number is not None else "present"
                    last_bail_diag.append((e.get("ts", "")[:19], diag))
            elif k in ("ppr_fallback_refused", "graph_walk_refused",
                       "relation_scoring_skipped", "tier2_skipped",
                       "chunk_bruteforce_skipped", "kg_bruteforce_refused",
                       "element_scoring_skipped", "dim_mismatch",
                       "scale_fold_refused", "viz_lazy_build_refused"):
                refuse_sites[k] += 1
            elif k == "model_error":
                key = f"{_safe_stage(e.get('stage'))}/{_safe_presence(e.get('error', e.get('message')))}"
                model_errors[key] += 1
            elif k == "ask_stage":
                ms = diag_common.finite_number(e.get("latency_ms"))
                if ms is not None:
                    ask_stage[_safe_stage(e.get("stage"))].add(ms)
            elif k == "pipeline":
                ms = diag_common.finite_number(e.get("latency_ms"))
                if ms is not None and e.get("status") == "done":
                    pipe_stage[_safe_stage(e.get("stage"))].add(ms)
            elif k == "scale_ppr_stage":
                ms = diag_common.finite_number(e.get("latency_ms"))
                if ms is not None:
                    scale_ppr_stage[_safe_stage(e.get("stage"))].add(ms)
            elif k == "scale_index_build":
                st = _safe_stage(e.get("stage"))
                ms = diag_common.finite_number(e.get("latency_ms"))
                if ms is not None:
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
        print("\n[model_error 按安全分类] — 不输出 provider 或异常文本")
        for r, c in model_errors.most_common(8):
            print(f"  {c:>6}  {r}")
    if ask_stage:
        print("\n[ask 各阶段延迟 — 仅 chunk 模式有埋点;reasoning/graph 模式无 ask_stage(观测盲区)]")
        for st in sorted(ask_stage, key=lambda s: -ask_stage[s].max):
            print(f"  {ask_stage[st].line()}  {st}")
    if pipe_stage:
        print("\n[摄取管线各阶段延迟]")
        for st in sorted(pipe_stage, key=lambda s: -pipe_stage[s].max):
            print(f"  {pipe_stage[st].line()}  {st}")
    if scale_ppr_stage:
        print("\n[scale_ppr 各阶段延迟]")
        for st in sorted(scale_ppr_stage, key=lambda s: -scale_ppr_stage[s].max):
            print(f"  {scale_ppr_stage[st].line()}  {st}")
    if last_build:
        ts, stages = last_build
        print(f"\n[最近一次 scale 索引构建 {ts} 分段]")
        for st, ms in stages.items():
            print(f"  {_fmt_ms(ms):>10}  {st}")


def report_llm(local_dir, since):
    llm_read = diag_common.read_channel(
        Path(local_dir) / "logs", "llm", since_hours=since.total_seconds() / 3600)
    section(f"LLM 调用(近 {since} 内;文件 {llm_read.stats.files} 个)")
    print(_scan_summary(llm_read.stats))
    if not llm_read.stats.files:
        print("(没有 llm 日志,或未开 LLM 日志)")
        return
    per_model = defaultdict(Sampler)
    errors = Counter()
    for e in llm_read.records:
            ts = _parse_ts(e.get("ts", ""))
            if ts is None:
                continue
            model = "configured" if e.get("model") not in (None, "") else "unspecified"
            ms = diag_common.finite_number(e.get("latency_ms", e.get("duration_ms")))
            if ms is not None:
                per_model[model].add(ms)
            if e.get("status") not in (None, "ok", "done"):
                errors[model] += 1
    if not per_model:
        print("窗口内无带延迟的 LLM 记录")
        return
    for m in sorted(per_model, key=lambda m: -per_model[m].max):
        err = f"  errors={errors[m]}" if errors.get(m) else ""
        print(f"  {per_model[m].line()}  model={m}{err}")


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


def _db_target(local_dir, root=None):
    """Which database the deployment serves from.

    Diagnostics that can only speak SQLite must ask this before opening a file:
    on a PostgreSQL deployment that file is stale, and answering from it is a
    wrong diagnosis dressed as a real one.

    The caller's ``--root`` wins whenever it is known.  Deriving the root from
    ``local_dir`` only works when .local sits under the repository; with an
    explicit ``--local /somewhere/else`` that guess lands next to the custom
    directory, finds no .env, and defaults straight back to SQLite.
    """
    if root:
        return diag_common.resolve_database_target(os.path.abspath(root))
    d = os.path.abspath(local_dir)
    derived = os.path.dirname(d)
    if os.path.basename(derived) == "backend":
        derived = os.path.dirname(derived)
    return diag_common.resolve_database_target(derived)


def report_scale_profile(local_dir, runtime_dim=0, root=None):
    """per-notebook 规模画像 + scale 索引健康诊断(本段是「严格推理慢」的主证据):
    - 无索引的大库 → KG 对象检索走全量暴力(json 解析 55w payload+全量分词+GB 级矩阵)
    - 索引在但 delta 大 → KG 对象侧 delta 每次查询无条件暴力(chunk 侧默认关,对象侧没开关)
    - manifest dim 失配 → ANN 静默失效,悄悄回退全量(runtime_dim>0 时判据=manifest应==运行时维,
      库内向量恒为存储维属正常)
    只读、每查询都是聚合/LIMIT 1,秒级。"""
    _runtime_dim = runtime_dim
    section("per-notebook 规模画像 + scale 索引诊断")
    target = _db_target(local_dir, root)
    if not target.is_sqlite:
        print(target.skip_note())
        return
    # 用 DATABASE_URL 解析出的那个文件,而不是固定路径:非默认 SQLite 路径下,
    # 固定路径同样指向一份陈旧库。
    db = target.resolve_sqlite_file(local_dir)
    if not db or not os.path.exists(db):
        print("(缺 SQLite database)")
        return
    db_uri = target.sqlite_readonly_uri(local_dir)
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        print("(DB 只读打开失败: sqlite_error)")
        return
    try:
        try:
            tiers = {
                r["id"]: _safe_tier(r["tier"] if "tier" in r.keys() else "personal")
                for r in conn.execute("SELECT * FROM notebooks").fetchall()
            }
        except sqlite3.Error:
            print("(notebooks schema unavailable: sqlite_error)")
            return
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
                object_type = _safe_object_type(r["ot"])
                ko_by_type[r["nb"]][object_type] = (
                    ko_by_type[r["nb"]].get(object_type, 0) + _safe_count(r["c"])
                )
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
            print(f"\n  {diag_common.pseudonym('notebook', nb)} tier={tiers[nb]}: KO {n_ko}({ko_desc}) "
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
            mf = _read_manifest(mpath)
            if not mf:
                print("    ⚠ manifest 损坏或过大 → 索引加载失败=同「无索引」,全量暴力")
                continue
            dim = _safe_count(mf.get("dim"))
            print(f"    索引: n_nodes={_safe_count(mf.get('n_nodes'))} "
                  f"n_ann={_safe_count(mf.get('n_ann'))} "
                  f"chunk_ann={_safe_count(mf.get('n_chunk_ann'))} "
                  f"relation_ann={_safe_count(mf.get('n_relation_ann'))} "
                  f"dim={dim}")
            if mf.get("has_chunk_ann") is not True:
                print("    ⚠ 索引无 chunk_ann → PPR 种子/chunk 检索对全部 chunk 逐条分词打分"
                      "(每次查询重付)")
            # 维度失配探测:ANN 静默失效的头号来源(换 embed 模型后没重建索引)。
            # 运行时截断(EMBED_RUNTIME_DIM)开启后,库内向量恒为存储维(如 4096)而
            # manifest/查询恒为运行时维(如 1024)—— 此为**健康**,不是失配。判据改为
            # 「manifest dim 应 == 运行时生效维」,而非 == 库内存储维。
            r = conn.execute(
                "SELECT vector FROM knowledge_embeddings WHERE notebook_id=? LIMIT 1",
                (nb,)).fetchone()
            live_dim = _vec_dim(r["vector"]) if r else None
            rt = _runtime_dim  # 运行时生效维(0=关,见 report_env 解析)
            if rt and dim and dim != int(rt):
                print(f"    ⚠⚠ 维度失配: manifest dim={dim} vs 运行时维 EMBED_RUNTIME_DIM={rt} → "
                      f"索引建在旧空间,ANN 每次静默跳过 — 需 mode=full 重建")
            elif not rt and dim and live_dim and dim != int(live_dim):
                print(f"    ⚠⚠ 维度失配: manifest dim={dim} vs 库内向量 dim={live_dim} → "
                      f"ANN 每次静默跳过(无事件!),KG 对象检索悄悄回退全量暴力 — 需重建索引")
            # 水位 delta:对象侧 delta 暴力没有开关,每次查询都付
            raw_watermark = mf.get("watermark_sources")
            wm = set(raw_watermark) if (
                isinstance(raw_watermark, list)
                and len(raw_watermark) <= 100_000
                and all(isinstance(value, str) for value in raw_watermark)
            ) else set()
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
                      f"chunk 侧不做 delta 向量暴力,但 FTS 词法补召回仍可能命中全库 chunk。"
                      f"建议手动触发 fold/重建,别等低峰窗口")
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


def report_reasoning_ppr_audit(local_dir, root):
    """Explain the strict-reasoning hot path without running retrieval/PPR.

    Reasoning mode prints "初检索得到 X 个候选节点" and then immediately runs a
    deterministic PPR seed pass when GRAPH_PPR_ENABLED=true. A hang after that
    trace line is therefore usually inside _ppr_retrieve/scale_ppr. This audit
    reads only DB aggregates and scale-index manifests to show whether each
    large notebook stays on the persisted index core, falls back to bounded FTS,
    or has a shape that can still touch full active vectors.
    """
    section("strict reasoning / PPR 路径审计(只读,不跑 PPR)")
    target = _db_target(local_dir, root)
    if not target.is_sqlite:
        print(target.skip_note())
        return
    # 用 DATABASE_URL 解析出的那个文件,而不是固定路径:非默认 SQLite 路径下,
    # 固定路径同样指向一份陈旧库。
    db = target.resolve_sqlite_file(local_dir)
    if not db or not os.path.exists(db):
        print("(缺 SQLite database)")
        return
    db_uri = target.sqlite_readonly_uri(local_dir)
    env = _read_env(root)
    graph_ppr = _env_bool(env, "GRAPH_PPR_ENABLED", True)
    include_delta = _env_bool(env, "SCALE_SEARCH_INCLUDE_DELTA", False)
    chunk_ann_enabled = _env_bool(env, "CHUNK_ANN_ENABLED", True)
    emb_syn = _env_bool(env, "PPR_EMB_SYNONYM_ENABLED", True)
    print(f"开关: GRAPH_PPR_ENABLED={graph_ppr} "
          f"SCALE_SEARCH_INCLUDE_DELTA={include_delta} "
          f"CHUNK_ANN_ENABLED={chunk_ann_enabled} "
          f"PPR_EMB_SYNONYM_ENABLED={emb_syn}")
    if graph_ppr:
        print("提示: reasoning 初检索 trace 之后会先跑一次 PPR seed pass;"
              "若日志停在「初检索得到...」,重点看本段。")
    else:
        print("GRAPH_PPR_ENABLED=false: reasoning 不会进入 PPR seed pass。")
        return
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        print("(DB 只读打开失败: sqlite_error)")
        return
    try:
        try:
            tiers = {r["id"]: _safe_tier(r["tier"]) for r in conn.execute(
                "SELECT id, tier FROM notebooks").fetchall()}
        except sqlite3.Error:
            print("(notebooks schema unavailable: sqlite_error)")
            return
        base_nbs = [n for n, t in tiers.items() if t == "base"]

        def group_count(sql):
            try:
                return {r["nb"]: r["c"] for r in conn.execute(sql).fetchall()}
            except sqlite3.Error:
                return {}

        ko = group_count("SELECT notebook_id nb, COUNT(*) c FROM knowledge_objects GROUP BY 1")
        chunks = group_count("SELECT notebook_id nb, COUNT(*) c FROM chunks GROUP BY 1")
        rels = group_count("SELECT notebook_id nb, COUNT(*) c FROM knowledge_relations GROUP BY 1")
        kemb = group_count("SELECT notebook_id nb, COUNT(*) c FROM knowledge_embeddings GROUP BY 1")
        remb = group_count("SELECT notebook_id nb, COUNT(*) c FROM relation_embeddings GROUP BY 1")
        cemb = group_count("SELECT notebook_id nb, COUNT(*) c FROM chunk_embeddings GROUP BY 1")

        idx_root = os.path.join(local_dir, "storage", "kg_index")

        def manifest(nb):
            mpath = os.path.join(idx_root, nb, "manifest.json")
            if not os.path.exists(mpath):
                return None
            value = _read_manifest(mpath)
            return value or {"_broken": True}

        def pct(a, b):
            return "n/a" if not b else f"{(100.0 * a / b):.1f}%"

        def graph_size_gb(nb):
            gpath = os.path.join(idx_root, nb, "graph.npz")
            return os.path.getsize(gpath) / 1e9 if os.path.exists(gpath) else 0.0

        ordered = sorted(tiers, key=lambda n: -(ko.get(n, 0) + chunks.get(n, 0)))
        shown = 0
        for nb in ordered:
            total = ko.get(nb, 0) + chunks.get(nb, 0)
            if total < BIG_NB_ROWS:
                continue
            shown += 1
            mf = manifest(nb)
            has_self_idx = bool(mf and not mf.get("_broken"))
            print(f"\n  active={diag_common.pseudonym('notebook', nb)} "
                  f"tier={tiers.get(nb, 'unknown')} "
                  f"KO={ko.get(nb, 0)} chunks={chunks.get(nb, 0)} "
                  f"relations={rels.get(nb, 0)}")

            if not has_self_idx:
                if mf and mf.get("_broken"):
                    print("    ⚠ self scale index manifest 损坏或过大")
                else:
                    print("    ⚠ self 无 scale index: KG 初检索在大库下会拒绝暴力,"
                          "改走 FTS 有界兜底; PPR 会依赖 base participant,"
                          "否则拒绝 rustworkx 全图 fallback")
            else:
                n_nodes = _safe_count(mf.get("n_nodes"))
                n_kg_nodes = _safe_count(mf.get("n_kg_nodes"))
                n_chunks = _safe_count(mf.get("n_chunks"))
                n_ann = _safe_count(mf.get("n_ann"))
                print(f"    self index: n_nodes={n_nodes} "
                      f"n_kg_nodes={n_kg_nodes} n_chunks={n_chunks} "
                      f"n_ann={n_ann}({pct(n_ann, kemb.get(nb, 0))} of knowledge_embeddings) "
                      f"chunk_ann={_safe_count(mf.get('n_chunk_ann'))} "
                      f"relation_ann={_safe_count(mf.get('n_relation_ann'))} "
                      f"graph.npz={graph_size_gb(nb):.2f}GB")
                if n_ann < kemb.get(nb, 0):
                    print("    注意: KG ANN 只覆盖 manifest.n_ann 这部分 embedding。"
                          "SCALE_SEARCH_INCLUDE_DELTA=false 时,语义 KG 检索/PPR self core"
                          "按设计不搜索未入 ANN 的对象。")
                if chunk_ann_enabled and mf.get("has_chunk_ann") is not True:
                    print("    ⚠ self index 缺 chunk_ann: PPR chunk seed 会走 FTS 有界降级,"
                          "不会全量向量扫,但召回/延迟会受 FTS 影响")
                elif chunk_ann_enabled and mf.get("has_chunk_ann") is True:
                    print("    chunk seed: 语义候选走 chunk_ann;另有 FTS 词法补召回"
                          "(有界,但可命中未进 chunk_ann 的 chunk)")
                if mf.get("has_relation_ann") is not True and remb.get(nb, 0):
                    print("    ⚠ self index 缺 relation_ann: relation 检索在大库冷矩阵时会跳过;"
                          "若进程里关系矩阵已暖,仍可能检索全量 relation_embeddings")
                if include_delta:
                    delta_ko = max(0, ko.get(nb, 0) - n_kg_nodes)
                    delta_chunks = max(0, chunks.get(nb, 0) - n_chunks)
                    print(f"    ⚠ SCALE_SEARCH_INCLUDE_DELTA=true: 查询会尝试纳入索引水位后的"
                          f" delta(约 KO={delta_ko}, chunks={delta_chunks}),这会破坏"
                          "「只搜已索引部分」的性能假设")
                else:
                    print("    delta 策略: SCALE_SEARCH_INCLUDE_DELTA=false → "
                          "KG/PPR self delta 不暴力补搜,等待 fold/重建进索引")

            participants = []
            for bid in base_nbs:
                if bid == nb:
                    continue
                bmf = manifest(bid)
                if bmf and not bmf.get("_broken"):
                    participants.append((bid, bmf))
            if has_self_idx:
                participants.append((nb, mf))
            if not participants:
                print("    PPR participants=0 → scale_ppr no_participants;"
                      "大库会直接 ppr_fallback_refused,不再 rustworkx 全图构建")
                continue

            p_nodes = sum(_safe_count(pm.get("n_nodes")) for _pid, pm in participants)
            p_graph_gb = sum(graph_size_gb(pid) for pid, _pm in participants)
            p_desc = ", ".join(
                f"{diag_common.pseudonym('notebook', pid)}"
                f"{'(self)' if pid == nb else '(base)'}:{_safe_count(pm.get('n_nodes'))} nodes"
                for pid, pm in participants)
            print(f"    PPR participants: {p_desc}")
            print(f"    PPR indexed core estimate: nodes={p_nodes} graph.npz={p_graph_gb:.2f}GB; "
                  "personalized_ppr 会在这个 CSR core 上做最多 100 次稀疏迭代"
                  "(这是 indexed-only,但不是 top-k 子图)")

            external = [pid for pid, _pm in participants if pid != nb]
            if emb_syn and external and total >= BIG_NB_ROWS:
                print("    ⚠ 潜在慢点: active 大库 + 外部 base participant + "
                      "PPR_EMB_SYNONYM_ENABLED=true 时,_scale_xlayer_bridge_edges "
                      "会为跨库 synonym bridge 加载 active 的全量 knowledge_embeddings。"
                      "若卡在初检索 trace 之后且 CPU/内存飙升,优先核查这里。")
            elif emb_syn and external:
                print("    cross-layer bridge: 会加载 active embedding 域与外部 base ANN 对齐;"
                      "active 若是小个人库通常可接受。")
            if len(participants) > 1:
                print("    cold-cache 组合图: 多 participant 会 reconstruct/splice CSR;"
                      "首次查询慢、后续同版本应由 vector_cache 命中。")
            if shown >= 12:
                break
        if shown == 0:
            print("没有超过大库阈值的 notebook。")
    finally:
        conn.close()


def report_artifacts(local_dir, deep, root=None):
    section("DB / 索引工件")
    target = _db_target(local_dir, root)
    # 索引工件(scale index)与后端无关,继续报;SQLite 文件那部分整段收回——说了
    # 「跳过」却仍打印那个文件的体积和 WAL 警告,等于把陈旧库重新摆上来。
    db = target.resolve_sqlite_file(local_dir)
    if db is None:
        print(f"  (当前部署是 {target.explain()} — 跳过 SQLite 文件/向量迁移检查，"
              "下面的索引工件仍然有效)")
    else:
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
        mf = _read_manifest(m)
        if not mf:
            print("  ⚠ manifest 损坏或过大")
            continue
        size = sum(os.path.getsize(f) for f in glob.glob(os.path.join(d, "*")))
        print(f"  {diag_common.pseudonym('notebook', os.path.basename(d))}: "
              f"nodes={_safe_count(mf.get('n_nodes'))} ann={_safe_count(mf.get('n_ann'))} "
              f"chunk_ann={_safe_count(mf.get('n_chunk_ann'))} "
              f"relation_ann={_safe_count(mf.get('n_relation_ann'))} "
              f"工件 {size/1e9:.2f} GB")
        raw_build = mf.get("build_ms")
        if isinstance(raw_build, dict):
            build = {}
            for raw_stage, raw_ms in raw_build.items():
                number = diag_common.finite_number(raw_ms)
                if number is not None:
                    stage = _safe_stage(raw_stage)
                    build[stage] = max(build.get(stage, 0), number)
            worst = sorted(build.items(), key=lambda kv: -kv[1])[:3]
            if worst:
                print("    build_ms 最重三段: "
                      + ", ".join(f"{k}={_fmt_ms(v)}" for k, v in worst))
    if not deep:
        print("  (向量 BLOB 迁移进度检查是全表扫描,分钟级 — 需要时加 --deep)")
        return
    db_uri = target.sqlite_readonly_uri(local_dir)
    if not db_uri:
        return
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        for t in ("knowledge_embeddings", "chunk_embeddings",
                  "element_embeddings", "relation_embeddings"):
            try:
                rows = conn.execute(
                    f"SELECT typeof(vector) tp, COUNT(*) c FROM {t} GROUP BY 1").fetchall()
                print(f"  {t}: " + ", ".join(f"{r['tp']}={r['c']}" for r in rows)
                      + ("   ⚠ 仍有 text 行未迁移" if any(r["tp"] == "text" for r in rows) else " ✓"))
            except sqlite3.Error:
                print(f"  {t}: 查询失败(sqlite_error)")
        conn.close()
    except sqlite3.Error:
        print("  (DB 只读打开失败: sqlite_error)")


def _read_env_int(root, key):
    """从 root/.env(或 backend/.env)读一个整型开关;缺失/非法 → 0。"""
    return _safe_count(_read_env(root).get(key))


def report_env(root):
    section(".env 关键开关(值含 KEY/TOKEN/PASSWORD/SECRET 的只报已配置)")
    path = _env_path(root)
    if not path:
        print("(无 .env)")
        return
    if path == os.path.join(root, "backend", ".env"):
        print("(根下无 .env,改用 backend dotenv)")
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
        "KG_AUTO_EXTRACT", "MODEL_SERVICES_CONFIG", "EMBED_DIM", "EMBED_RUNTIME_DIM",
        "VECTOR_CACHE_MAX_ENTRIES",
        "SCALE_IDX_CACHE_MAX", "EDGE_CENTRALITY_MAX_NODES", "HNSW_EF_CONSTRUCTION",
        "CHUNK_RECALL", "SILICON_NOTEBOOK_STORAGE_DIR", "DATABASE_URL",
    )
    raw = _read_env(root)
    seen = {}
    for k, v in raw.items():
        if any(m in k.upper() for m in SECRET_MARKERS):
            seen[k] = "<已配置,值不显示>" if v.strip() else "<空>"
        elif k == "DATABASE_URL":
            # 用 redact_database_url 露出去凭据后的库身份(host/db),而非整条隐藏——
            # 身份对诊断有用且不含密码(#337 提供了该工具,报告里补上使用)。
            seen[k] = _redact_database_url(v) if v.strip() else "<空>"
        elif k in interesting:
            stripped = v.strip()
            if k in _ENV_BOOL_KEYS:
                lowered = stripped.lower()
                if lowered in {"1", "true", "yes", "on"}:
                    seen[k] = "true"
                elif lowered in {"0", "false", "no", "off"}:
                    seen[k] = "false"
                else:
                    seen[k] = "<invalid>"
            elif k in _ENV_ENUMS:
                seen[k] = stripped if stripped in _ENV_ENUMS[k] else "<invalid>"
            elif k in {"EMBED_MODEL", "OPENAI_COMPAT_MODEL", "REASONING_LLM_MODEL",
                       "SILICON_NOTEBOOK_STORAGE_DIR"}:
                seen[k] = "<已配置,值不显示>" if stripped else "<空>"
            else:
                number = diag_common.finite_number(stripped)
                seen[k] = str(int(number)) if number is not None else "<invalid>"
    for k in interesting:
        if k in seen:
            print(f"  {k}={seen[k]}")
    print("  (未列出的 = .env 没写,走代码默认值;关键默认: SCALE_INDEX_AUTO_WHEN=idle→"
          "只在低峰 2-6 点建索引、CHUNK_ANN_ENABLED=true、SCALE_SEARCH_INCLUDE_DELTA=false)")


# USABLE_STATUSES 谓词(knowledge_contracts.USABLE_STATUSES);from_row/analytics 的
# per-type GROUP BY 用它。与 status!='deprecated' 并列打印,实证 planner 计划一致。
_USABLE_STATUSES = ("approved", "reviewed", "project_specific", "conflict")


def report_notebook_count_hotpaths(local_dir, notebook_id="", deep=False, root=None):
    """打开 / 看板 / 索引状态卡顿的逐条自证(仅 --deep,因本段会真跑几条 2M 级扫描)。

    这些计数在每次「打开 notebook」「点看板」「取索引状态」时被同步重算、且无缓存
    (计数只在 ingest/rebuild 变,却每次请求重扫 ~2M 覆盖索引条目)。本段对最大(或
    --notebook 指定的)notebook 逐条跑 EXPLAIN QUERY PLAN + 实测执行一次,报告:
    用了哪个索引 / 是否全表 SCAN / 是否 TEMP B-TREE 排序 / 耗时 ms / 计数结果,
    并核对关键覆盖索引存在性。只读(mode=ro)、无 DML。脱敏:只打计数/计划/ms,绝不
    打 object 文本或名。参见 docs/large-notebook-latency-analysis.md。"""
    if not deep:
        section("notebook 计数热路径(加 --deep 逐条 EXPLAIN+计时,会真跑 2M 级扫描)")
        print("(跳过:未加 --deep — 本段会执行几条 ~2M 行覆盖扫描,单核秒级)")
        return
    section("notebook 计数热路径(打开/看板/索引状态卡顿逐条自证)")
    target = _db_target(local_dir, root)
    if not target.is_sqlite:
        print(target.skip_note())
        return
    # 用 DATABASE_URL 解析出的那个文件,而不是固定路径:非默认 SQLite 路径下,
    # 固定路径同样指向一份陈旧库。
    db = target.resolve_sqlite_file(local_dir)
    if not db or not os.path.exists(db):
        print("(缺 SQLite database)")
        return
    db_uri = target.sqlite_readonly_uri(local_dir)
    try:
        conn = sqlite3.connect(db_uri, uri=True, timeout=60)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        print("(DB 只读打开失败: sqlite_error)")
        return
    try:
        def _run(sql, params=()):
            return conn.execute(sql, params)   # 唯一 conn.execute 站点(允许表钉死)

        def _timed(sql, params=()):
            t0 = datetime.now()
            rows = _run(sql, params).fetchall()
            return rows, (datetime.now() - t0).total_seconds() * 1000.0

        nb = notebook_id
        if not nb:
            try:
                r = _run("SELECT notebook_id nb, COUNT(*) c FROM knowledge_objects "
                         "GROUP BY 1 ORDER BY c DESC LIMIT 1").fetchone()
                nb = r["nb"] if r else ""
            except sqlite3.Error:
                print("(选最大 notebook 失败: sqlite_error)")
                return
        if not nb:
            print("(库中无 knowledge_objects)")
            return
        print(f"目标 notebook = {diag_common.pseudonym('notebook', nb)}  "
              "(用 --notebook <id> 指定)")

        ph = ",".join("?" for _ in _USABLE_STATUSES)
        hot = [
            ("type_counts[status IN usable]",   # from_row(打开)/概览
             f"SELECT object_type, COUNT(*) c FROM knowledge_objects "
             f"WHERE notebook_id=? AND status IN ({ph}) GROUP BY object_type",
             (nb, *_USABLE_STATUSES)),
            ("type_counts[status!=deprecated]",  # analytics(看板)
             "SELECT object_type, COUNT(*) c FROM knowledge_objects "
             "WHERE notebook_id=? AND status!='deprecated' GROUP BY object_type",
             (nb,)),
            ("relation_count",
             "SELECT COUNT(*) c, MAX(created_at) m FROM knowledge_relations WHERE notebook_id=?",
             (nb,)),
            ("embedding_count",
             "SELECT COUNT(*) c, MAX(created_at) m FROM knowledge_embeddings WHERE notebook_id=?",
             (nb,)),
            ("cluster_distinct_canonical",       # index-status,无缓存 O(N) DISTINCT
             "SELECT COUNT(DISTINCT canonical_id) c FROM concept_clusters WHERE notebook_id=?",
             (nb,)),
            ("community_count[cheap ctrl]",      # 命中 idx_communities_nb_level,便宜对照
             "SELECT COUNT(*) c FROM communities WHERE notebook_id=?",
             (nb,)),
            ("chunk_total",                      # index-status
             "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
             (nb,)),
        ]
        print("\n  [逐条: EXPLAIN QUERY PLAN + 实测执行一次]")
        print(f"    {'query':32} {'ms':>9}  plan / 计数")
        ms_by = {}
        for name, sql, params in hot:
            try:
                plan_rows = _run("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                details = [str(r["detail"]) for r in plan_rows]
            except sqlite3.Error:
                print(f"    {name:32} plan? (sqlite_error)")
                continue
            full = any(d.strip().startswith("SCAN") and "USING" not in d for d in details)
            sort = any("TEMP B-TREE" in d.upper() for d in details)
            flag = ("⚠全表SCAN" if full else "idx") + ("+排序" if sort else "")
            try:
                rows, ms = _timed(sql, params)
                ms_by[name] = ms
                if rows and "object_type" in rows[0].keys():
                    grouped = defaultdict(int)
                    for row in rows:
                        grouped[_safe_object_type(row["object_type"])] += _safe_count(row["c"])
                    res = ",".join(f"{key}={value}" for key, value in sorted(grouped.items()))
                elif rows:
                    res = " ".join(
                        f"{k}={_safe_count(rows[0][k])}"
                        for k in rows[0].keys()
                    )
                else:
                    res = "(空)"
                mstr = _fmt_ms(ms)
            except sqlite3.Error:
                mstr, res = "err:sqlite_error", ""
            print(f"    {name:32} {mstr:>9}  [{flag}] {'; '.join(details)}  → {res}")

        # 复合放大:一次「打开」= from_row 跑 2 遍(GET /notebooks/{id} 与
        # /scale-index/status→get_notebook 各一次);感知开销 ≈ 2× type_counts。
        base = ms_by.get("type_counts[status IN usable]")
        if base is not None:
            print(f"\n  打开路径 from_row ×2(GET /notebooks/{{id}} + /scale-index/status)"
                  f" → 单次打开 ≈ {_fmt_ms(base * 2)}(且 KG 构建中每 6s 轮询再乘一轮)")

        print("\n  [关键覆盖索引存在性]")
        have = set()
        try:
            for r in _run("SELECT name FROM sqlite_master WHERE type='index'").fetchall():
                have.add(r["name"])
        except sqlite3.Error:
            pass
        want = [
            ("idx_knowledge_objects_nb_type_status", "打开/看板 count-by-type 覆盖(应命中)"),
            ("idx_knowledge_objects_nb_status", "status 过滤"),
            ("idx_chunks_nb", "chunk 计数"),
            ("idx_clusters_nb", "cluster(仅 notebook_id,不含 canonical_id→DISTINCT 扫全成员行)"),
            ("idx_communities_nb_level", "community 计数(便宜对照)"),
        ]
        for name, why in want:
            print(f"    [{'✓' if name in have else '缺'}] {name}  — {why}")
    finally:
        conn.close()


def _main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="仓库根(默认当前目录)")
    ap.add_argument("--local", default="", help="显式指定 .local 目录(默认自动探测)")
    ap.add_argument("--since", type=float, default=48, help="回看小时数(默认 48)")
    ap.add_argument("--slow-ms", type=int, default=3000, help="慢请求阈值 ms(默认 3000)")
    ap.add_argument("--deep", action="store_true",
                    help="额外做 DB 检查(typeof 全表扫 + 计数热路径 EXPLAIN/计时,大库分钟级,只读)")
    ap.add_argument("--notebook", default="",
                    help="计数热路径诊断的目标 notebook id(默认自动取最大)")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    since_hours = diag_common.finite_number(args.since, maximum=24 * 366)
    if since_hours is None:
        since_hours = 48.0
    slow_ms = diag_common.finite_number(args.slow_ms, maximum=10**9)
    if slow_ms is None:
        slow_ms = 3000.0
    since = timedelta(hours=since_hours)
    original_stdout = sys.stdout
    sys.stdout = _BoundedOutput(original_stdout, DEFAULT_REPORT_OUTPUT_BYTES)
    try:
        print(f"silicon-notebook 慢因诊断  root=<configured>  窗口={since_hours:g}h  "
              f"生成于 {datetime.now().isoformat(timespec='seconds')}")
        local_dir = os.path.abspath(args.local) if args.local else resolve_local(root)
        report_requests(local_dir, since, slow_ms)
        report_events(local_dir, since)
        report_llm(local_dir, since)
        report_scale_profile(local_dir, runtime_dim=_read_env_int(root, "EMBED_RUNTIME_DIM"), root=root)
        report_reasoning_ppr_audit(local_dir, root)
        report_artifacts(local_dir, args.deep, root=root)
        report_notebook_count_hotpaths(local_dir, notebook_id=args.notebook, deep=args.deep, root=root)
        report_env(root)
        print("\n=== 完 — 把以上整段输出贴回即可 " + "=" * 40)
        return 0
    finally:
        sys.stdout.flush()
        sys.stdout = original_stdout


def main(argv=None):
    return diag_common.run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    sys.exit(main())
