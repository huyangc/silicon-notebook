# backend/app/services/batch_ingest.py
"""离线批量摄取(目录 → notebook → source/chunk(+embed) → KG → 概念簇)。

复用现有管线,不重写解析/分块/抽取。两阶段:
  ingest  无 LLM:upload_sources(同步 parse+chunk),摄取期 EMBED 置空 → 收尾低并发补 chunk 向量
  kg      LLM:对尚无 KG 的 source 抽取 → 一次 rebuild_unified_kg → 补节点向量;per-source 融合关
CLI 见 scripts/batch_ingest.py 与 README「离线批量摄取」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.repository import UploadedSourceFile
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)

SUPPORTED_EXTS = {".md", ".markdown", ".pdf"}

LogFn = Callable[[dict], None]


def _rebuild_progress(phase: str, i: int, n: int) -> None:
    """CLI progress printer for rebuild_unified_kg sub-phases. Two shapes:
      - stage banner (n == 0): print the phase alone on its own line;
      - item progress (n > 0, e.g. concept_desc LLM gen): overwrite in place
        until the last item, then newline."""
    if n == 0:
        print(f"  {phase}", flush=True)
        return
    end = "\n" if i >= n else "\r"
    print(f"  {phase}: {i}/{n}", end=end, flush=True)


def _index_stage_progress(stage: str, latency_ms: int) -> None:
    """CLI progress printer for build_scale_index's on_stage callback: one
    line per stage (kg_matrix/ann_build/synonym/gather/transition/
    chunk_matrix/viz_arrays/persist/total), printed as it happens — the
    events logger doesn't print to the terminal, and a scale-index build on
    the 490k-object library can take tens of minutes, so real-time per-stage
    output is the only way to tell it isn't stuck. Generic over stage name/
    order, so pipeline reorders (e.g. Task 1's hnsw-build-once restructure)
    never require a change here."""
    print(f"  [index] {stage}: {latency_ms}ms", flush=True)


def _live_embed_thread_counts() -> Counter:
    """Snapshot of live pool threads by name convention:
      - `embed-<sid>` per-source background embed daemons → "bg"
      - `emb-el`/`emb-ck`/`emb-kg`/`emb-rel` embed pool workers → "pool"
      - `kg-desc` 概念描述生成 LLM 池 → "desc"(rebuild 阶段;不经 scheduler window 池)
      - `kg-review` merge-review 预审 LLM 池 → "review"(rebuild 阶段;同上)
    Best-effort observability only; racy by nature (threads come and go)."""
    c: Counter = Counter()
    for t in threading.enumerate():
        n = t.name or ""
        if n.startswith("embed-"):
            c["bg"] += 1
        elif n.startswith("emb-"):
            c["pool"] += 1
        elif n.startswith("kg-desc"):
            c["desc"] += 1
        elif n.startswith("kg-review"):
            c["review"] += 1
    return c


def _format_pool_snapshot(ts: str, s: dict, embed: Counter, done: int, total: int,
                          label: str = "") -> str:
    """Pure one-line snapshot of pool utilization. `ts` is the wall-clock time of
    the snapshot (so it lines up with the model-call logs); `s` is
    scheduler.stats(); `embed` is _live_embed_thread_counts(). Shows KG-LLM(window)
    vs embed concurrency side by side so a shared-compute model service can be
    confirmed to run both pools at once. 抽取期(total>0)显示「源完成 done/total」;
    其它有 LLM 的阶段(如 rebuild,total=0)显示阶段名 label。
    rebuild 期的概念描述/merge-review LLM 走独立线程池(非 scheduler window),按线程名单列
    「概念描述(LLM) N」「merge-review(LLM) N」——仅在活跃(>0)时出现,否则不加噪。"""
    tail = f" · 源完成 {done}/{total}" if total > 0 else (f" · {label}" if label else "")
    llm = ""
    if embed.get("desc", 0):
        llm += f" · 概念描述(LLM) {embed['desc']}"
    if embed.get("review", 0):
        llm += f" · merge-review(LLM) {embed['review']}"
    return (f"[pool {ts}] KG-LLM(window) {s['window_active']}/{s['window_max']}"
            f" · 源(job) {s['job_active']}/{s['job_max']}"
            f" · embed {embed.get('bg', 0)}bg+{embed.get('pool', 0)}pool"
            + llm + tail)


class _PoolReporter:
    """Background daemon that periodically prints live pool utilization + emits a
    structured log line. interval<=0 disables it entirely (no thread started).

    Exception-guarded: a bad stats read / print / log never breaks ingest.
    Update `.done` from the driving loop; `__exit__` stops and joins (timeout)."""

    def __init__(self, interval: float, total: int, log: Optional[LogFn] = None,
                 label: str = ""):
        self.interval = interval
        self.total = total
        self.log = log
        self.label = label   # total=0 的阶段(如 rebuild)在快照尾部显示的阶段名
        self.done = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "_PoolReporter":
        if self.interval and self.interval > 0:
            self._thread = threading.Thread(
                target=self._loop, name="pool-report", daemon=True)
            self._thread.start()
        return self

    def _loop(self) -> None:
        from app.services.kg import scheduler as _sched
        while not self._stop.wait(self.interval):
            try:
                s = _sched.stats()
                line = _format_pool_snapshot(
                    time.strftime("%H:%M:%S"), s,
                    _live_embed_thread_counts(), self.done, self.total, self.label)
                print(line, flush=True)
                if self.log:
                    self.log({"phase": "pool", **s, "done": self.done,
                              "total": self.total, "label": self.label})
            except Exception:   # noqa: BLE001 — observability must never break ingest
                pass

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False


def iter_files(root: Path, exts: Optional[set] = None) -> List[Path]:
    """递归收集 root 下受支持的文件,按路径稳定排序(保证可恢复遍历顺序)。"""
    allowed = {e.lower() for e in (exts or SUPPORTED_EXTS)}
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in allowed
    )


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def already_ingested(repo: SQLiteRepository, notebook_id: str, digest: str) -> bool:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND file_hash=?",
            (notebook_id, digest),
        ).fetchone()
    return row is not None


def source_id_by_hash(repo: SQLiteRepository, notebook_id: str, digest: str) -> Optional[str]:
    """已按内容哈希摄取过则返回其 source id,否则 None(续跑/去重用)。"""
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND file_hash=?",
            (notebook_id, digest),
        ).fetchone()
    return row["id"] if row else None


def _resolve_owner_profile(repo: SQLiteRepository, owner: Optional[str]):
    """解析 notebook 属主 → UserProfile。owner=用户名(大小写不敏感);
    None → 默认取 admin 用户(role='admin' 中最早建的=seeded admin)。找不到 → SystemExit。"""
    with repo._connect() as db:
        if owner is not None:
            from app.services.auth_utils import normalize_username
            user = db.execute(
                "SELECT * FROM users WHERE username=?", (normalize_username(owner),)).fetchone()
            who = owner
        else:
            user = db.execute(
                "SELECT * FROM users WHERE role='admin' ORDER BY created_at ASC LIMIT 1").fetchone()
            who = "admin"
        if user is None:
            raise SystemExit(f"error: owner not found: {who}")
        profile = db.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user["id"],)).fetchone()
    return repo._user_profile(user, profile)


def ensure_notebook(repo: SQLiteRepository, notebook_id: Optional[str], name: str,
                    owner: Optional[str] = None) -> str:
    """返回目标 notebook_id:给定则校验存在,否则以解析出的属主新建。
    owner=用户名(默认= admin 用户);notebook.created_by 记其 user id。"""
    if notebook_id:
        repo.get_notebook(notebook_id)   # 不存在则 KeyError
        return notebook_id
    profile = _resolve_owner_profile(repo, owner)
    token = set_request_user(profile)
    try:
        return repo.create_notebook(NotebookCreate(name=name)).id
    finally:
        reset_request_user(token)


def run_ingest(repo: SQLiteRepository, notebook_id, files, workers=4, conc=4, log=None) -> dict:
    """Phase 1:解析+分块(摄取期 EMBED 置空)+ 收尾低并发补 chunk 向量。无 LLM。"""
    log = log or (lambda _e: None)
    counts = {"uploaded": 0, "skipped": 0, "failed": 0}
    orig_provider = repo.settings.embed_provider
    repo.settings.embed_provider = ""   # 摄取期零嵌入:parse+chunk 快、无 429

    def _one(path: Path):
        try:
            content = path.read_bytes()
            if already_ingested(repo, notebook_id, sha256_bytes(content)):
                return ("skipped", path, None)
            repo.upload_sources(
                notebook_id,
                [UploadedSourceFile(file_name=path.name, content_type="", content=content)],
                scheduler=None,
            )
            return ("uploaded", path, None)
        except Exception as exc:   # noqa: BLE001 — 单文件失败隔离
            return ("failed", path, f"{type(exc).__name__}: {exc}")

    total = len(files)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for i, (status, path, err) in enumerate(pool.map(_one, files), 1):
                counts[status] += 1
                log({"phase": "ingest", "path": str(path), "status": status, "error": err})
                print(f"[ingest {i}/{total}] {Path(path).name}: {status}", flush=True)
    finally:
        repo.settings.embed_provider = orig_provider   # 恢复,供 backfill 使用

    counts["sources_embedded"] = backfill_chunk_embeddings(repo, notebook_id, conc)
    return counts


def backfill_chunk_embeddings(repo: SQLiteRepository, notebook_id, conc=4,
                              missing_only=False) -> int:
    """补该 notebook 的 chunk 向量(低并发)。EMBED 未配则跳过。

    - missing_only=False(默认):遍历每个 source 调 _embed_chunks_for_source 全量重嵌
      (INSERT OR REPLACE upsert),返回处理的 *source* 数。
    - missing_only=True:只补缺向量的 chunk(NOT EXISTS),一次 _embed_chunks_batch,
      返回补的 *chunk* 数(无缺失则跳过返回 0)。
    """
    if not repo.settings.embedder_configured:
        return 0
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    try:
        if missing_only:
            with repo._connect() as db:
                rows = db.execute(
                    "SELECT c.id, c.text FROM chunks c WHERE c.notebook_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
                    "WHERE e.chunk_id=c.id)", (notebook_id,)).fetchall()
            items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
            if not items:
                print("[embed] 无缺失 chunk 向量,跳过", flush=True)
                return 0
            print(f"[embed] 补缺失 chunk 向量:{len(items)} 个", flush=True)
            repo._embed_chunks_batch(notebook_id, items)
            return len(items)

        done = 0
        with repo._connect() as db:
            sids = [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
        n = len(sids)
        for i, sid in enumerate(sids, 1):
            try:
                repo._embed_chunks_for_source(sid)
                done += 1
                print(f"[embed {i}/{n}] {sid}", flush=True)
            except Exception:   # noqa: BLE001 — best-effort;429 留人工重跑
                print(f"[embed {i}/{n}] {sid} ✗(留人工重跑)", flush=True)
        return done
    finally:
        repo.settings.embed_concurrency = orig_conc


def run_kg(repo: SQLiteRepository, notebook_id, limit=None, conc=4, log=None,
           no_rebuild: bool = False, rebuild_only: bool = False,
           report_interval: int = 15) -> dict:
    """Phase 2:对尚无 KG 的 source 抽取(per-source 融合关)→ 一次 rebuild_unified_kg → 补节点向量。

    Flags:
      rebuild_only=True  — 跳过抽取,直接 rebuild_unified_kg + 节点向量(含 scale index)。
      no_rebuild=True    — 只抽取,跳过 rebuild_unified_kg 和 scale index(大批量分批抽,最后一批再 rebuild)。
      两者互斥;互斥时抛 ValueError。

    Scale-index 自动联:rebuild 后若 notebook 为 base tier 或已存在 scale index,则调
    repo.build_scale_index(notebook_id) 使索引与新簇同步。
    """
    if no_rebuild and rebuild_only:
        raise ValueError("no_rebuild 和 rebuild_only 互斥,不能同时为 True")

    log = log or (lambda _e: None)
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    repo.settings.kg_incremental_fusion_enabled = False   # 批量期关 per-source 融合,收尾一次全量
    res = {"extracted": 0, "failed": 0, "clusters": 0, "nodes_embedded": 0}
    try:
        # ── 抽取阶段(rebuild_only 时跳过) ────────────────────────────────────
        # 抽取驱动 KG-LLM window 池,故用 _PoolReporter 周期自报 KG-LLM vs embed 并发。
        if not rebuild_only:
            llm_ok = (repo.settings.kg_llm_configured
                      or getattr(repo.llm_client, "configured", False))
            if limit is None:
                # no_rebuild=True 且无 LLM 时:跳过抽取(无法抽取,等 rebuild_only 阶段再合并)
                if llm_ok or not no_rebuild:
                    # 尚无 KG 的源数当 total(build_notebook_kg 内部自算目标,故 done 靠其
                    # progress 回调回填);查询很廉价。
                    with repo._connect() as db:
                        kg_total = db.execute(
                            "SELECT COUNT(*) c FROM sources s WHERE s.notebook_id=? "
                            "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
                            "WHERE k.source_id=s.id AND k.source_id!='')",
                            (notebook_id,)).fetchone()["c"]
                    with _PoolReporter(report_interval, total=kg_total, log=log) as reporter:
                        def _kg_progress(i, n, sid, ok):
                            reporter.done = i
                            reporter.total = n
                            print(f"[kg {i}/{n}] {sid} {'✓' if ok else '✗ 失败'}", flush=True)
                        out = repo.build_notebook_kg(  # 跨源并发抽取,逐源打印进度
                            notebook_id, progress=_kg_progress)
                    res["extracted"] = len(out["built"])
                    res["failed"] = len(out["failed"])
            else:
                if not llm_ok:
                    raise RuntimeError(
                        "KG LLM 未配置(KG_LLM_* 或主 LLM 均未配):--limit 抽取只会产出 no-llm 空结果")
                with repo._connect() as db:
                    all_sids = [r["id"] for r in db.execute(
                        "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
                    kgful = {r["source_id"] for r in db.execute(
                        "SELECT DISTINCT source_id FROM knowledge_objects "
                        "WHERE notebook_id=? AND source_id!=''", (notebook_id,)).fetchall()}
                targets = [s for s in all_sids if s not in kgful][:max(0, limit)]
                n_targets = len(targets)
                with _PoolReporter(report_interval, total=n_targets, log=log) as reporter:
                    for i, sid in enumerate(targets, 1):
                        try:
                            repo._set_source_status(sid, "extracting")
                            repo._run_extraction(sid)
                            repo._set_source_status(sid, "extracted")
                            res["extracted"] += 1
                            log({"phase": "kg", "source_id": sid, "status": "extracted",
                                 "progress": f"{i}/{n_targets}"})
                        except Exception as exc:   # noqa: BLE001 — 单源失败隔离
                            res["failed"] += 1
                            log({"phase": "kg", "source_id": sid, "status": "failed", "error": str(exc)})
                        reporter.done = i

        # ── no_rebuild:抽取后直接返回,不做 rebuild/scale-index ───────────────
        if no_rebuild:
            return res

        # ── Rebuild 阶段(有 LLM:概念描述/merge-review)→ 同样开 pool 自报 ────────
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        with _PoolReporter(report_interval, total=0, log=log, label="rebuild 阶段"):
            # force=rebuild_only:rebuild_only 是显式「只重建」入口(用户主动重聚),
            # 必须重算;普通 kg 阶段走门控(force=False),无新文件时跳过重聚(本次优化点)。
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=rebuild_only)
            res["clusters"] = clusters
            log({"phase": "kg", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

            # ── Scale-index 自动联(base tier 或已有索引) ─────────────────────
            nb = repo.get_notebook(notebook_id)
            is_base = (nb.tier == "base")
            has_index = (repo._scale_index(notebook_id) is not None)
            if is_base or has_index:
                manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
                scale_nodes = manifest.get("n_nodes", 0)
                res["scale_index_nodes"] = scale_nodes
                log({"phase": "kg", "status": "scale_index_built", "nodes": scale_nodes})
                print(f"scale index built (nodes={scale_nodes})", flush=True)

    finally:
        repo.settings.kg_incremental_fusion_enabled = orig_fusion   # 还原,避免污染 repo 实例
    return res


def run_all(repo: SQLiteRepository, notebook_id, files, workers=4, conc=4, log=None,
            report_interval: int = 15) -> dict:
    """流式 all:per-source 流水线(parse+embed+extract 在 process_source 内重叠),
    跨源并发提交到全局 KG job 池;**末尾一次** rebuild_unified_kg + 补节点向量(+ scale index)。

    - 新文件(hash 未见过)→ upload_sources(scheduler=submit_job(process_source)):
      parse + 后台 embed + extract 一次调用内并发完成。
    - 已 parse、缺 KG 的旧 source(同 hash 已摄取过)→ submit_job(extract_source):
      只补抽 KG(embed 在上次 ingest 已做,无需重嵌)。
    批量期强制 kg_auto_extract=True(让 process_source 走到 extract)且关 per-source 融合;
    finally 恢复两者原值。单 source 失败隔离,计入 failed,不连累其余。

    并发旋钮(本函数内生效,finally 复原):
      workers → scheduler.configure(job_workers=workers) 覆盖 KG_JOB_CONCURRENCY
        (= 同时抽几篇文档)。scheduler 池容量读独立 Settings(),不会被 repo.settings
        传导,故必须显式 configure。
      conc    → repo.settings.embed_concurrency=conc 覆盖 EMBED_CONCURRENCY
        (process_source 内的后台 chunk embed 用它)。
    """
    from app.services.kg import scheduler as _sched
    from app.services.kg.scheduler import submit_job

    log = log or (lambda _e: None)
    orig_auto = repo.settings.kg_auto_extract
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    orig_embed_conc = repo.settings.embed_concurrency
    repo.settings.kg_auto_extract = True                 # 强制 process_source 走 extract 分支
    repo.settings.kg_incremental_fusion_enabled = False  # 批量期关 per-source 融合,收尾一次 rebuild
    repo.settings.embed_concurrency = conc               # process_source 后台 chunk embed 并发
    # KG job 池读独立 Settings(),repo.settings 改不到它 → 显式 configure 覆盖 KG_JOB_CONCURRENCY
    _sched.configure(job_workers=max(1, workers))

    files = list(files)
    res = {"new": 0, "resumed": 0, "extracted": 0, "failed": 0,
           "clusters": 0, "nodes_embedded": 0}
    try:
        # ── 分两批:新文件 vs 已 parse 缺 KG 的续抽源 ─────────────────────────────
        with repo._connect() as db:
            kgful = {r["source_id"] for r in db.execute(
                "SELECT DISTINCT source_id FROM knowledge_objects "
                "WHERE notebook_id=? AND source_id!=''", (notebook_id,)).fetchall()}
        new_files: List[Path] = []
        resume_sids: List[str] = []
        for p in files:
            sid = source_id_by_hash(repo, notebook_id, sha256_bytes(p.read_bytes()))
            if sid is None:
                new_files.append(p)
            elif sid not in kgful:        # 已 parse、缺 KG → 只需补抽
                resume_sids.append(sid)
            # else: 已有 KG → 跳过(幂等,既不新建也不重抽)
        res["new"] = len(new_files)
        res["resumed"] = len(resume_sids)

        # ── 提交并发 job、收集 futures ───────────────────────────────────────────
        futs = {}

        def _sched(sid: str) -> None:
            futs[submit_job(repo.process_source, sid)] = sid

        if new_files:
            repo.upload_sources(
                notebook_id,
                [UploadedSourceFile(file_name=p.name, content_type="", content=p.read_bytes())
                 for p in new_files],
                scheduler=_sched,                 # 每个新 source 作为 process_source job 并发
            )
        for sid in resume_sids:
            futs[submit_job(repo.extract_source, sid)] = sid   # 只补抽 KG,不 embed

        # ── 等待全部 job,逐个打印进度、统计(周期自报 KG-LLM vs embed 并发) ──────
        total = len(futs)
        with _PoolReporter(report_interval, total=total, log=log) as reporter:
            for i, fut in enumerate(as_completed(futs), 1):
                sid = futs[fut]
                try:
                    fut.result()
                    res["extracted"] += 1
                    print(f"[pipeline {i}/{total}] {sid} ✓", flush=True)
                    log({"phase": "all", "source_id": sid, "status": "ok", "progress": f"{i}/{total}"})
                except Exception as exc:   # noqa: BLE001 — 单 source 失败隔离
                    res["failed"] += 1
                    print(f"[pipeline {i}/{total}] {sid} ✗ {type(exc).__name__}: {exc}", flush=True)
                    log({"phase": "all", "source_id": sid, "status": "failed", "error": str(exc)})
                reporter.done = i

        # ── 末尾一次(有 LLM:概念描述/merge-review)→ 同样开 pool 自报 ───────────
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        with _PoolReporter(report_interval, total=0, log=log, label="rebuild 阶段"):
            # 门控(force=False):自动收尾重聚。无新增/变更时(如重跑同一批)输入版本
            # 未变 → 跳过整段重聚,直接返回缓存簇数(本次优化点)。
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=False)
            res["clusters"] = clusters
            log({"phase": "all", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

            nb = repo.get_notebook(notebook_id)
            if nb.tier == "base" or repo._scale_index(notebook_id) is not None:
                manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
                scale_nodes = manifest.get("n_nodes", 0)
                res["scale_index_nodes"] = scale_nodes
                log({"phase": "all", "status": "scale_index_built", "nodes": scale_nodes})
                print(f"scale index built (nodes={scale_nodes})", flush=True)
    finally:
        repo.settings.kg_auto_extract = orig_auto
        repo.settings.kg_incremental_fusion_enabled = orig_fusion
        repo.settings.embed_concurrency = orig_embed_conc
    return res


def run_index(repo: SQLiteRepository, notebook_id: str) -> dict:
    """Phase 3 (offline): build the scalable-retrieval index for a (base) notebook.
    Static base KGs should re-run this after a rebuild."""
    manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
    return {"indexed_nodes": manifest.get("n_nodes", 0)}


def backfill_node_embeddings(repo: SQLiteRepository, notebook_id, conc=4) -> int:
    """补 KG 节点向量(复用 _backfill_knowledge_embeddings;关系向量默认跳过)。EMBED 未配则跳过。"""
    if not repo.settings.embedder_configured:
        return 0
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    try:
        with repo._connect() as db:
            objects = [
                {"id": r["id"], "payload": json.loads(r["payload"] or "{}")}
                for r in db.execute(
                    "SELECT id, payload FROM knowledge_objects "
                    "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,)).fetchall()
            ]
            repo._backfill_knowledge_embeddings(db, notebook_id, objects)
    finally:
        repo.settings.embed_concurrency = orig_conc
    # Backfilling node vectors changes the ANN inputs → mark dirty so the cluster
    # version's kg_mutation_seq advances (a later force=False rebuild must not skip
    # on the strength of unchanged object/decided counts alone).
    repo._mark_unified_kg_dirty(notebook_id)
    return len(objects)


def _count_missing_chunk_vectors(repo: SQLiteRepository, notebook_id) -> int:
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM chunks c WHERE c.notebook_id=? AND NOT EXISTS "
            "(SELECT 1 FROM chunk_embeddings e WHERE e.chunk_id=c.id)",
            (notebook_id,)).fetchone()["c"]


def _count_missing_node_vectors(repo: SQLiteRepository, notebook_id) -> int:
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects o WHERE o.notebook_id=? "
            "AND o.status!='deprecated' AND NOT EXISTS "
            "(SELECT 1 FROM knowledge_embeddings e WHERE e.object_id=o.id)",
            (notebook_id,)).fetchone()["c"]


def run_embed(repo: SQLiteRepository, notebook_id, conc=4) -> dict:
    """补该 notebook 缺失的 chunk + 节点向量(幂等,只补缺失)。

    先 SQL 盘点缺失数并打印,再 backfill_chunk_embeddings(missing_only=True) +
    backfill_node_embeddings(节点本就只补缺失),最后打印 after 盘点。
    """
    chunk_missing = _count_missing_chunk_vectors(repo, notebook_id)
    node_missing = _count_missing_node_vectors(repo, notebook_id)
    print(f"embed: 缺失盘点 chunk={chunk_missing} node={node_missing}", flush=True)

    chunks_embedded = backfill_chunk_embeddings(repo, notebook_id, conc, missing_only=True)
    nodes_embedded = backfill_node_embeddings(repo, notebook_id, conc)

    chunk_after = _count_missing_chunk_vectors(repo, notebook_id)
    node_after = _count_missing_node_vectors(repo, notebook_id)
    print(f"embed done: 补 chunk={chunks_embedded} node(扫描)={nodes_embedded};"
          f"剩余缺失 chunk={chunk_after} node={node_after}", flush=True)
    return {
        "chunks_embedded": chunks_embedded,
        "nodes_embedded": nodes_embedded,
        "chunk_missing_before": chunk_missing,
        "node_missing_before": node_missing,
    }


# (table, id_column) for every embeddings table the BLOB backfill covers.
_VECTOR_TABLES = (
    ("chunk_embeddings", "chunk_id"),
    ("knowledge_embeddings", "object_id"),
    ("element_embeddings", "element_id"),
    ("relation_embeddings", "relation_id"),
)

_BACKFILL_BATCH_SIZE = 5000
_BACKFILL_MAP_CHUNKSIZE = 256
_BACKFILL_DEFAULT_WORKERS = min(8, os.cpu_count() or 1)


def _parse_encode(pair: Tuple[str, str]) -> Tuple[str, bytes]:
    """Module-level, spawn-safe (top-level def, no closures) worker for the
    vectors-to-blob ProcessPoolExecutor: parse one legacy JSON-text vector row
    and re-encode it as a raw float32 BLOB. `pair` is (row_id, vector_raw_text).

    Deliberately light imports (orjson + numpy only, no repo/settings/db
    modules) — this function is pickled and sent to a fresh worker process, so
    importing the full app graph here would be slow and pointless (workers
    never touch the DB).

    Never raises: on any parse/encode failure (corrupt JSON, empty text, wrong
    shape) it returns the same empty-bytes sentinel the serial path already
    used for `decode_vector`-failures — this preserves the dead-loop guarantee
    that every selected row moves out of typeof(vector)='text', and it means
    a single bad row in a chunksize batch can never blow up the whole pool
    task (BrokenProcessPool is reserved for real crashes, not bad rows)."""
    vid, raw = pair
    try:
        import orjson
        import numpy as np

        if raw is None or raw == "":
            return vid, b""
        vec = orjson.loads(raw)
        arr = np.asarray(vec, dtype=np.float32)
        if arr.size == 0:
            return vid, b""
        return vid, arr.tobytes()
    except Exception:  # noqa: BLE001 — any malformed row becomes the sentinel
        return vid, b""


def _parse_encode_batch_serial(rows) -> List[Tuple[bytes, str, str]]:
    """Serial parse+encode of a batch of rows — the exact pre-parallel code
    path. Returns [(blob, notebook_id, vid), ...] ready for executemany."""
    from app.services.vector_index import decode_vector, encode_vector

    updates = []
    for r in rows:
        try:
            arr = decode_vector(r["vector"])
        except Exception:  # noqa: BLE001 — malformed row, treat as bad
            arr = None
        if arr is None:
            blob = b""  # sentinel: still moves the row to typeof='blob'
        else:
            blob = encode_vector(arr)
        updates.append((blob, r["notebook_id"], r["vid"]))
    return updates


def _backfill_table_to_blob(repo: SQLiteRepository, notebook_id: Optional[str],
                            table: str, id_col: str,
                            batch_size: int = _BACKFILL_BATCH_SIZE,
                            workers: int = 1) -> dict:
    """把一个 embeddings 表里仍是 JSON TEXT 的 vector 行原地转成 float32 BLOB
    (encode_vector),分批事务提交 + 打印进度。幂等:每轮只选
    typeof(vector)='text' 的行(SQLite 原生类型探测,O(1) 判定、不逐行反序列化),
    跑第二遍时天然 0 行可转、直接返回——可安全重跑/中断重启。
    notebook_id=None 时覆盖全表(--all-notebooks)。

    一行都转不动的批次(全部 decode_vector 失败,如损坏的 JSON 文本)也必须
    UPDATE 成 blob(空 b'' 哨兵,decode_vector 读回 None——与旧代码里空/无效
    JSON 行的读侧语义一致),否则这些行永远停留在 typeof='text'、
    下一轮 LIMIT 会重复选中同一批 → 死循环。

    workers<=1 (default) takes the exact original serial path — zero
    multiprocessing machinery constructed. workers>1 parses+encodes each batch
    in a ProcessPoolExecutor (module-level `_parse_encode` worker, light
    imports only); the main process still owns 100% of the DB reads/writes —
    SQLite stays single-writer, workers never open a connection. If the pool
    dies (BrokenProcessPool), the run falls back to the serial path for this
    batch and every subsequent one (fail-open: a crashed pool must never lose
    the run, just lose the parallel speedup)."""
    where = "WHERE typeof(vector)='text'"
    params: tuple = ()
    if notebook_id is not None:
        where += " AND notebook_id=?"
        params = (notebook_id,)

    # typeof() 无法走索引 → 这个 COUNT 是全表扫描,大表(如百万级 relation_embeddings)
    # 要几分钟。先出声,免得上一张表打完 N/N 后长时间静默被当成"卡死"。
    print(f"  [blob] {table}: 扫描待转行(大表可能数分钟,无输出≠卡死)…", flush=True)
    with repo._connect() as db:
        total = db.execute(f"SELECT COUNT(*) c FROM {table} {where}", params).fetchone()["c"]
    converted = 0
    skipped_bad = 0
    if total == 0:
        print(f"  [blob] {table}: 0/0 (无待转行)", flush=True)
        return {"table": table, "total": total, "converted": 0, "skipped_bad": 0}

    use_pool = workers > 1
    executor = ProcessPoolExecutor(max_workers=workers) if use_pool else None
    try:
        while True:
            with repo._write() as db:
                rows = db.execute(
                    f"SELECT {id_col} AS vid, notebook_id, vector FROM {table} {where} "
                    f"LIMIT {int(batch_size)}", params).fetchall()
                if not rows:
                    break
                if use_pool:
                    try:
                        pairs = [(r["vid"], r["vector"]) for r in rows]
                        by_vid = {vid: blob for vid, blob in executor.map(
                            _parse_encode, pairs, chunksize=_BACKFILL_MAP_CHUNKSIZE)}
                        updates = [(by_vid[r["vid"]], r["notebook_id"], r["vid"]) for r in rows]
                    except BrokenProcessPool as exc:
                        print(f"  [blob] {table}: 进程池崩溃,回退串行(fallback to serial): {exc}",
                              flush=True)
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = None
                        use_pool = False
                        updates = _parse_encode_batch_serial(rows)
                else:
                    updates = _parse_encode_batch_serial(rows)
                skipped_bad += sum(1 for blob, _nb, _vid in updates if blob == b"")
                db.executemany(
                    f"UPDATE {table} SET vector=? WHERE notebook_id=? AND {id_col}=?",
                    updates)
            converted += len(rows)
            print(f"  [blob] {table}: {converted}/{total}", flush=True)
            if len(rows) < batch_size:
                break
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    return {"table": table, "total": total, "converted": converted, "skipped_bad": skipped_bad}


def run_vectors_to_blob(repo: SQLiteRepository, notebook_id: Optional[str],
                        all_notebooks: bool = False, workers: int = 1) -> dict:
    """向量存储 BLOB 化一次性 backfill:把指定 notebook(或 --all-notebooks 时全库)
    embeddings 表里的旧 JSON TEXT 行原地转成 float32 BLOB(encode_vector)。
    分批事务(每批 5000 行)+ 打印进度;typeof(vector)='blob' 的行已跳过 →
    可安全重跑(第二遍 0 行可转)。转换不改 created_at,故 _vector_matrix 的
    (COUNT, MAX created_at) 版本键不变——缓存的矩阵内容仍等价(同向量,只是
    换了编码),不会因 backfill 变脏失效;下次自然过期时重建即得 BLOB 直载收益。

    workers>1 offloads the json.loads/np.tobytes parse+encode stage (the
    single-core bottleneck at scale) to a ProcessPoolExecutor per table/batch;
    all DB access (SELECT/UPDATE) stays in this (main) process — see
    `_backfill_table_to_blob`.
    """
    if not notebook_id and not all_notebooks:
        raise ValueError("run_vectors_to_blob: 需要 notebook_id 或 all_notebooks=True")
    scope = "全部 notebook" if all_notebooks else notebook_id
    print(f"vectors-to-blob: scope={scope} workers={workers}", flush=True)
    results = []
    for table, id_col in _VECTOR_TABLES:
        results.append(_backfill_table_to_blob(
            repo, None if all_notebooks else notebook_id, table, id_col, workers=workers))
    total_converted = sum(r["converted"] for r in results)
    total_bad = sum(r["skipped_bad"] for r in results)
    print(f"vectors-to-blob done: converted={total_converted} skipped_bad={total_bad}", flush=True)
    return {"tables": results, "converted": total_converted, "skipped_bad": total_bad}


def _make_logger(manifest_path: Optional[Path]) -> LogFn:
    if manifest_path is None:
        return lambda _e: None
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(entry: dict) -> None:
        entry = dict(entry, ts=time.time())
        with manifest_path.open("a", encoding="utf-8") as fh:  # 每条 open/close,避免句柄泄漏
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if entry.get("status") == "failed":
            print(f"[{entry.get('phase')}] FAILED "
                  f"{entry.get('path') or entry.get('source_id') or ''}: "
                  f"{entry.get('error')}", flush=True)

    return _log


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="batch_ingest", description="离线批量摄取目录 → 项目 KG/向量库")
    p.add_argument("phase", choices=["ingest", "kg", "index", "all", "embed", "vectors-to-blob"])
    p.add_argument("--input-dir", type=Path, help="递归扫描的根目录(ingest/all 必填)")
    p.add_argument("--notebook-id", default=None, help="目标 notebook;省略则新建")
    p.add_argument("--all-notebooks", action="store_true",
                   help="vectors-to-blob 专用:转换全库全部 notebook,忽略 --notebook-id")
    p.add_argument("--notebook-name", default=None,
                   help="新建 notebook 名(ingest/all 新建库时必填;不再默认用目录名)")
    p.add_argument("--owner", default=None,
                   help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    p.add_argument("--workers", type=int, default=None,
                   help="all 阶段同时抽取的文档数(覆盖 KG_JOB_CONCURRENCY,其余摄取阶段为"
                        "文件级并发,默认 4);vectors-to-blob 阶段为 json.loads/编码并行进程数"
                        f"(默认 min(8, CPU核数)={_BACKFILL_DEFAULT_WORKERS},<=1 走原串行路径,"
                        "不启动进程池)")
    p.add_argument("--embed-conc", type=int, default=4,
                   help="embedding 并发(覆盖 EMBED_CONCURRENCY;all 阶段峰值≈workers×此值,"
                        "注意 429)。默认 4")
    p.add_argument("--limit", type=int, default=None,
                   help="kg 阶段只抽前 N 个未抽源(仅限制本次抽取数量;最终 rebuild 仍覆盖全本 notebook)")
    p.add_argument("--no-rebuild", action="store_true",
                   help="kg 阶段:只抽取,跳过 rebuild_unified_kg 和 scale index。"
                        "大批量分批抽取用法:重复 'kg --limit N --no-rebuild',最后一次 'kg --rebuild-only'。")
    p.add_argument("--rebuild-only", action="store_true",
                   help="kg 阶段:跳过抽取,直接 rebuild_unified_kg + 节点向量 + scale index(base tier 时)。")
    p.add_argument("--allow-no-embed", action="store_true",
                   help="EMBED 未配置时显式允许无向量降级(默认拒绝,防静默产出无向量库)")
    p.add_argument("--pool-report-interval", type=int, default=15,
                   help="每 N 秒自报线程池占用(KG-LLM/源/embed);0 关闭。all/kg 阶段生效")
    p.add_argument("--dry-run", action="store_true", help="只扫描+报告,不写库")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # --workers has a phase-dependent default (argparse default is None so we
    # can tell "omitted" from "explicitly 4"): doc-extraction concurrency for
    # ingest/all/kg defaults to 4; vectors-to-blob's parse/encode pool defaults
    # to min(8, cpu_count()) and is resolved separately below.
    if args.workers is None and args.phase != "vectors-to-blob":
        args.workers = 4

    if args.phase in {"ingest", "all"} and not args.input_dir:
        print("error: --input-dir required for ingest/all", file=sys.stderr)
        return 2

    if args.phase == "index" and not args.notebook_id:
        print("error: --notebook-id required for index (specify the base notebook to index)",
              file=sys.stderr)
        return 2

    if args.phase == "embed" and not args.notebook_id:
        print("error: --notebook-id required for embed (specify the notebook to backfill vectors)",
              file=sys.stderr)
        return 2

    if args.phase == "vectors-to-blob" and not args.notebook_id and not args.all_notebooks:
        print("error: vectors-to-blob 需要 --notebook-id 或 --all-notebooks", file=sys.stderr)
        return 2

    if args.dry_run:
        files = iter_files(args.input_dir) if args.input_dir else []
        print(f"[dry-run] {len(files)} files under {args.input_dir}", flush=True)
        for p in files[:20]:
            print(f"  {p}", flush=True)
        if len(files) > 20:
            print(f"  ... (+{len(files) - 20} more)", flush=True)
        return 0

    if args.phase in {"ingest", "all"} and not args.notebook_id and not args.notebook_name:
        print("error: 新建 notebook 需用 --notebook-name 指定名字(不再默认用目录名)",
              file=sys.stderr)
        return 2

    repo = SQLiteRepository(Settings())

    if args.phase == "vectors-to-blob":
        # 纯格式转换(已算好的向量 JSON→BLOB),不产出新向量,不需要 EMBED 就绪,
        # 也不走 ensure_notebook(不新建库;--notebook-id 必须是已存在的库)。
        blob_workers = args.workers if args.workers is not None else _BACKFILL_DEFAULT_WORKERS
        _t = time.perf_counter()
        r = run_vectors_to_blob(repo, args.notebook_id, all_notebooks=args.all_notebooks,
                                workers=blob_workers)
        print(f"vectors-to-blob done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)
        return 0

    # embed 子命令就是补向量:EMBED 未配直接报错,忽略 --allow-no-embed。
    allow_no_embed = args.allow_no_embed and args.phase != "embed"
    if not repo.settings.embedder_configured:
        if not allow_no_embed:
            extra = ("\n  注意:embed 子命令用于补向量,--allow-no-embed 对它无效。"
                     if args.phase == "embed" else "")
            print(
                f"error: EMBED 未就绪 → 不会产出向量(chunk/节点),检索将失效。\n"
                f"  当前 EMBED_PROVIDER={(repo.settings.embed_provider or '').strip()!r}"
                "(目前仅支持 'dashscope',大小写不敏感),且需 EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL 都配齐。\n"
                "  若确认 .env 已配:.env 按「当前工作目录」加载——请从含 .env 的仓库根(主 checkout,不是 worktree)运行。\n"
                "  确实要无向量导入,请显式加 --allow-no-embed。" + extra,
                file=sys.stderr,
            )
            return 2
        print("[warn] --allow-no-embed:无向量模式,本次不产出 chunk/节点向量。", flush=True)
    nb_name = args.notebook_name or "Batch Import"  # 新建路径已强制 --notebook-name;append（带 id）时不使用
    notebook_id = ensure_notebook(repo, args.notebook_id, nb_name, owner=args.owner)
    manifest = Path(repo.storage_dir) / "batch_ingest" / f"{notebook_id}.jsonl"
    log = _make_logger(manifest)
    print(f"notebook={notebook_id} manifest={manifest}", flush=True)

    if args.phase == "all":
        print("phase=all (pipelined)", flush=True)
        _t = time.perf_counter()
        r = run_all(repo, notebook_id, iter_files(args.input_dir),
                    workers=args.workers, conc=args.embed_conc, log=log,
                    report_interval=args.pool_report_interval)
        print(f"all done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)
        return 0

    if args.phase == "ingest":
        files = iter_files(args.input_dir)
        print(f"phase=ingest files={len(files)}", flush=True)
        _t = time.perf_counter()
        c = run_ingest(repo, notebook_id, files, workers=args.workers, conc=args.embed_conc, log=log)
        print(f"ingest done: {c} ({time.perf_counter() - _t:.1f}s)", flush=True)

    if args.phase == "kg":
        no_rebuild = getattr(args, "no_rebuild", False)
        rebuild_only = getattr(args, "rebuild_only", False)
        print(f"phase=kg limit={args.limit} no_rebuild={no_rebuild} rebuild_only={rebuild_only}",
              flush=True)
        _t = time.perf_counter()
        r = run_kg(repo, notebook_id, limit=args.limit, conc=args.embed_conc, log=log,
                   no_rebuild=no_rebuild, rebuild_only=rebuild_only,
                   report_interval=args.pool_report_interval)
        print(f"kg done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    if args.phase == "index":
        print(f"phase=index notebook={notebook_id}", flush=True)
        _t = time.perf_counter()
        r = run_index(repo, notebook_id)
        print(f"index done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    if args.phase == "embed":
        print(f"phase=embed notebook={notebook_id}", flush=True)
        _t = time.perf_counter()
        r = run_embed(repo, notebook_id, conc=args.embed_conc)
        print(f"embed done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    return 0
