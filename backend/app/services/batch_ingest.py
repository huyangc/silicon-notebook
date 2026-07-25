# backend/app/services/batch_ingest.py
"""SQLite-only 离线批量摄取(目录 → notebook → source/chunk(+embed) → KG → 概念簇)。

复用现有管线,不重写解析/分块/抽取。两阶段:
  ingest  upload_sources(同步 parse+chunk+系统调度 embedding) → 收尾补缺失向量
  kg      LLM:对尚无 KG 的 source 抽取 → 一次 rebuild_unified_kg → 补节点向量;per-source 融合关
CLI 见 scripts/batch_ingest.py 与 README「离线批量摄取」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple

from app.core.config import Settings
from app.core.database_url import database_identity
from app.core.request_context import set_request_user, reset_request_user
from app.models.notebooks import NotebookCreate, NotebookSummary
from app.models.sources import SourceSummary
from app.models.identity import UserProfile
from app.repositories.sqlite.maintenance import VECTOR_TABLES as _VECTOR_TABLES
from app.services.repository import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository
from app.repositories.ports import (
    ExtractionProgress,
    IndexStageProgress,
    KGBuildResult,
    KgBuildAlreadyRunning,
    RebuildProgress,
    RepositoryRow,
    ScaleBuildManifest,
    SQLiteMaintenancePort,
    SourceScheduler,
)

SUPPORTED_EXTS = {".md", ".markdown", ".pdf"}

LogFn = Callable[[dict[str, object]], None]


class BatchIngestRepository(Protocol):
    """Public facade surface used by the batch workflow."""

    settings: Settings
    storage_dir: Path
    maintenance: SQLiteMaintenancePort

    def configured(self, workload_id: str) -> bool: ...
    def current_user(self) -> UserProfile: ...
    def get_notebook(self, notebook_id: str) -> NotebookSummary: ...
    def create_notebook(self, payload: NotebookCreate) -> NotebookSummary: ...
    def upload_sources(
        self,
        notebook_id: str,
        files: Iterable[UploadedSourceFile],
        scheduler: SourceScheduler | None = None,
    ) -> list[SourceSummary]: ...
    def process_source(self, source_id: str) -> SourceSummary: ...
    def extract_source(self, source_id: str) -> None: ...
    def build_notebook_kg(
        self, notebook_id: str, *, progress: ExtractionProgress | None = None
    ) -> KGBuildResult: ...
    def rebuild_unified_kg(self, notebook_id: str,
                           progress: RebuildProgress | None = None,
                           force: bool = False, fresh: bool = False) -> int: ...
    def build_scale_index(self, notebook_id: str,
                          on_stage: IndexStageProgress | None = None
                          ) -> ScaleBuildManifest: ...


@dataclass(frozen=True)
class EffectiveConcurrency:
    workers: int
    workers_source: str


def _positive(value: int, name: str) -> int:
    value = int(value)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _resolve_effective_concurrency(
    args: argparse.Namespace, settings: Settings, phase: str
) -> EffectiveConcurrency:
    workers_default = (
        _BACKFILL_DEFAULT_WORKERS
        if phase == "vectors-to-blob"
        else settings.kg_job_concurrency
    )
    return EffectiveConcurrency(
        workers=_positive(
            args.workers if args.workers is not None else workers_default,
            "--workers",
        ),
        workers_source="cli" if args.workers is not None else "env",
    )


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
    chunk_matrix/relation_matrix/viz_arrays/persist/total), printed as it
    happens — the
    events logger doesn't print to the terminal, and a scale-index build on
    the 490k-object library can take tens of minutes, so real-time per-stage
    output is the only way to tell it isn't stuck. Generic over stage name/
    order, so pipeline reorders (e.g. Task 1's hnsw-build-once restructure)
    never require a change here."""
    print(f"  [index] {stage}: {latency_ms}ms", flush=True)


def _format_pool_snapshot(
    ts: str,
    s: Mapping[str, int],
    *,
    done: int,
    total: int,
    label: str = "",
) -> str:
    """Render producer-pool utilization; service admission is scheduler-owned."""
    tail = f" · 源完成 {done}/{total}" if total > 0 else (f" · {label}" if label else "")
    return (
        f"[pool {ts}] model-producer {s['window_active']}/{s['window_max']}"
        f" · source {s['job_active']}/{s['job_max']}"
        + tail
    )


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
                    time.strftime("%H:%M:%S"),
                    s,
                    done=self.done,
                    total=self.total,
                    label=self.label,
                )
                print(line, flush=True)
                if self.log:
                    self.log({
                        "phase": "pool",
                        **s,
                        "done": self.done,
                        "total": self.total,
                        "label": self.label,
                    })
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


def already_ingested(repo: BatchIngestRepository, notebook_id: str, digest: str) -> bool:
    return repo.maintenance.source_id_by_hash(notebook_id, digest) is not None


def source_id_by_hash(repo: BatchIngestRepository, notebook_id: str, digest: str) -> Optional[str]:
    """已按内容哈希摄取过则返回其 source id,否则 None(续跑/去重用)。"""
    return repo.maintenance.source_id_by_hash(notebook_id, digest)



def _resolve_owner_profile(
    repo: BatchIngestRepository, owner: Optional[str]
) -> UserProfile:
    """解析 notebook 属主 → UserProfile。owner=用户名(大小写不敏感);
    None → 默认取 admin 用户(role='admin' 中最早建的=seeded admin)。找不到 → SystemExit。"""
    profile = repo.maintenance.resolve_owner_profile(owner)
    if profile is None:
        raise SystemExit(f"error: owner not found: {owner if owner is not None else 'admin'}")
    return profile


def ensure_notebook(
    repo: BatchIngestRepository,
    notebook_id: Optional[str],
    name: str,
) -> str:
    """Resolve or create a notebook under the caller's active request user."""
    if notebook_id:
        repo.get_notebook(notebook_id)   # 不存在则 KeyError
        return notebook_id
    return repo.create_notebook(NotebookCreate(name=name)).id


def run_ingest(
    repo: BatchIngestRepository,
    notebook_id: str,
    files: Iterable[Path],
    workers: int = 4,
    log: LogFn | None = None,
) -> dict[str, int]:
    """Phase 1: parse, chunk and embed through the configured system service.

    跳过判据是内容哈希:命中即视为已摄取。这条判据**认账偏早**——file_hash 是
    INSERT 时写的(早于 parse),所以 parse 中途被中断的源 hash 已在库、内容却是空的,
    再跑 ingest 也不会补。刻意保持现状,不在这里做「有没有跑完」的推断:
    (elements, chunks, parse_status) 这三个可观测信号无法区分「分块失败但仍置
    extracted」与「纯标题 md 成功解析出零 chunk」——两者状态完全相同、正确答案相反;
    `parsed` 同样既是活跃过渡态又是中断残留态。要判准需要持久化的完成标记与活跃租约
    (schema 变更),见 docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md。
    存量补救走显式的 `reparse` 子命令(它按「有没有 source_elements」选目标)。
    """
    log = log or (lambda _e: None)
    counts = {"uploaded": 0, "skipped": 0, "failed": 0}

    # 同一次运行内的重复文件(输入目录里两个内容相同的文件)按内容哈希认领一次即可。
    # 主要作用不是省一次查库,而是挡住并发下的双重建源:没有它时两个同内容文件会同时
    # 查到「尚未摄取」、双双 upload,建出两个重复源。线程安全:_one 在池里并发跑。
    #
    # 已知边界(codex 第 8 轮 P2,经权衡后不修):并发处理时,若副本 B 在认领者 A 失败
    # **之前**就读到了认领并返回 skipped,A 事后交回认领也救不了 B —— 该内容本轮一份
    # 都没进来。之所以接受:A 失败时**什么都没提交**,内容不在库里,重跑 ingest 即补上;
    # 而彻底消除需要 per-digest 的完成条件变量(副本阻塞等认领者出结果),复杂度与收益
    # 不成比例。对照未引入认领集合时的行为(双双上传、建出重复源),本实现是净改善。
    # 保留行为由 test_run_ingest_same_run_duplicate_files_skip_not_reparse 与
    # test_run_ingest_releases_the_claim_when_the_claimant_fails 钉住。
    claimed: set[str] = set()
    claim_lock = threading.Lock()

    def _one(path: Path) -> tuple[str, Path, str | None]:
        digest: str | None = None
        try:
            content = path.read_bytes()
            digest = sha256_bytes(content)
            with claim_lock:
                if digest in claimed:
                    return ("skipped", path, None)
                claimed.add(digest)
            if already_ingested(repo, notebook_id, digest):
                return ("skipped", path, None)
            repo.upload_sources(
                notebook_id,
                [UploadedSourceFile(file_name=path.name, content_type="", content=content)],
                scheduler=None,
            )
            return ("uploaded", path, None)
        except Exception as exc:   # noqa: BLE001 — 单文件失败隔离
            # 认领失败就交回去:认领只代表「这个内容由我这一份来做」,不代表已摄取。
            # 不释放的话,后面同内容的副本会看到 digest 已被认领而直接 skipped——
            # 一次瞬时失败(存储/SQLite 抖动)就让这份内容整轮都进不来,而在有认领集合
            # 之前它本可以由下一个副本重试成功。
            if digest is not None:
                with claim_lock:
                    claimed.discard(digest)
            return ("failed", path, f"{type(exc).__name__}: {exc}")

    total = len(files)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, (status, path, err) in enumerate(pool.map(_one, files), 1):
            counts[status] += 1
            log({"phase": "ingest", "path": str(path), "status": status, "error": err})
            print(f"[ingest {i}/{total}] {Path(path).name}: {status}", flush=True)

    counts["sources_embedded"] = backfill_chunk_embeddings(
        repo, notebook_id, missing_only=True
    )
    return counts


def backfill_chunk_embeddings(
    repo: BatchIngestRepository,
    notebook_id: str,
    missing_only: bool = False,
) -> int:
    """补该 notebook 的 chunk 向量(低并发)。EMBED 未配则跳过。

    - missing_only=False(默认):遍历每个 source 调 _embed_chunks_for_source 全量重嵌
      (INSERT OR REPLACE upsert),返回处理的 *source* 数。
    - missing_only=True:只补缺向量的 chunk(NOT EXISTS),一次 _embed_chunks_batch,
      返回补的 *chunk* 数(无缺失则跳过返回 0)。
    """
    if not repo.configured("chunk_embedding"):
        return 0
    mnt = repo.maintenance
    if missing_only:
        rows = mnt.missing_chunk_embedding_rows(notebook_id)
        items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
        if not items:
            print("[embed] 无缺失 chunk 向量,跳过", flush=True)
            return 0
        print(f"[embed] 补缺失 chunk 向量:{len(items)} 个", flush=True)
        mnt.embed_chunks_batch(notebook_id, items)
        return len(items)

    done = 0
    sids = mnt.source_ids(notebook_id)
    n = len(sids)
    for i, sid in enumerate(sids, 1):
        try:
            mnt.embed_chunks_for_source(sid)
            done += 1
            print(f"[embed {i}/{n}] {sid}", flush=True)
        except Exception:   # noqa: BLE001 — best-effort;429 留人工重跑
            print(f"[embed {i}/{n}] {sid} ✗(留人工重跑)", flush=True)
    return done


def backfill_element_embeddings(
    repo: BatchIngestRepository,
    notebook_id: str,
) -> int:
    """补该 notebook 缺失的 element 向量(只补缺失,幂等),返回落库行数。EMBED 未配则跳过。

    与 chunk/节点两侧并列的第三条补齐路径:此前 element 侧只有「整源重跑」一条路,
    写了一半的源(部分 element 有向量、部分没有)没有任何增量修法。

    已知边界(codex 第 5/6/8 轮 P2,经权衡后本 PR 不修):待补行的**文本**是一次
    fetchall 全读进内存的,大库上这部分内存与待补文本总量成正比。之所以不在这里改:
    chunk 侧的 missing_chunk_embedding_rows 是**完全同构**的既有实现,只把 element 侧
    改成分页会让两侧行为不一致、给后续维护埋坑;要治应两侧一起改成 keyset 分页,
    作为独立改动提。本 PR 已把**向量**侧的内存做了分页——向量才是 GB 级的大头
    (几十万 element × 1024 维 float),文本通常小一到两个数量级。
    保留行为(向量确实分页落库)由
    test_backfill_element_embeddings_persists_before_all_embedding_finishes 钉住。
    element_embeddings 走 INSERT OR REPLACE upsert,只补缺失不会动同源已有的向量。"""
    if not repo.configured("source_element_embedding"):
        return 0
    mnt = repo.maintenance
    rows = mnt.missing_element_embedding_rows(notebook_id)
    if not rows:
        print("[embed] 无缺失 element 向量,跳过", flush=True)
        return 0
    print(f"[embed] 补缺失 element 向量:{len(rows)} 个", flush=True)
    return mnt.embed_elements_batch(
        notebook_id,
        [
            {"element_id": r["id"], "source_id": r["source_id"], "text": r["text"]}
            for r in rows
        ],
    )


def run_kg(repo: BatchIngestRepository, notebook_id: str,
           limit: int | None = None, log: LogFn | None = None,
           no_rebuild: bool = False, rebuild_only: bool = False, fresh: bool = False,
           report_interval: int = 15,
           retry_partial: bool = False) -> dict[str, int]:
    """Phase 2:对尚无 KG 的 source 抽取(per-source 融合关)→ 一次 rebuild_unified_kg → 补节点向量。

    Model admission is owned by the system provider; this phase only submits
    source jobs to the business orchestration pool.

    Flags:
      rebuild_only=True  — 跳过抽取,直接 rebuild_unified_kg + 节点向量(含 scale index)。
      no_rebuild=True    — 只抽取,跳过 rebuild_unified_kg 和 scale index(大批量分批抽,最后一批再 rebuild)。
      两者互斥;互斥时抛 ValueError。
      fresh=True         — 清空 rebuild checkpoint,强制 merge 审查/概念描述全量重跑(模型/阈值换了、
                            数据没变时用);隐含 force=True(否则清完 checkpoint 仍会被 force=False 的
                            门控挡回缓存簇数,--fresh 变假 no-op)。
      retry_partial=True — 正常补缺失 KG 时一并重试 latest run 的 failed_windows>0
                            来源；旧图保留到零失败窗口且非空的新图可事务替换为止。

    Scale-index 自动联:rebuild 后若 notebook 为 base tier 或已存在 scale index,则调
    repo.build_scale_index(notebook_id) 使索引与新簇同步。
    """
    from app.services.kg.scheduler import submit_job

    if no_rebuild and rebuild_only:
        raise ValueError("no_rebuild 和 rebuild_only 互斥,不能同时为 True")
    if retry_partial and rebuild_only:
        raise ValueError("retry_partial 和 rebuild_only 互斥,不能同时为 True")

    log = log or (lambda _e: None)
    mnt = repo.maintenance
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    repo.settings.kg_incremental_fusion_enabled = False   # 批量期关 per-source 融合,收尾一次全量
    res = {
        "extracted": 0,
        "failed": 0,
        "partial_retried": 0,
        "partial_failed_preserved": 0,
        "clusters": 0,
        "nodes_embedded": 0,
    }
    try:
        # ── 抽取阶段(rebuild_only 时跳过) ────────────────────────────────────
        # 周期自报业务 producer/source 池；模型容量由 service scheduler 观测。
        if not rebuild_only:
            llm_ok = repo.configured("kg_extract")
            if limit is None and not retry_partial:
                # no_rebuild=True 且无 LLM 时:跳过抽取(无法抽取,等 rebuild_only 阶段再合并)
                if llm_ok or not no_rebuild:
                    # 尚无 KG 的源数当 total(build_notebook_kg 内部自算目标,故 done 靠其
                    # progress 回调回填);查询很廉价。
                    kg_total = mnt.count_sources_missing_kg(notebook_id)
                    with _PoolReporter(report_interval, total=kg_total, log=log) as reporter:
                        def _kg_progress(
                            i: int, n: int, sid: str, ok: bool
                        ) -> None:
                            reporter.done = i
                            reporter.total = n
                            print(f"[kg {i}/{n}] {sid} {'✓' if ok else '✗ 失败'}", flush=True)
                        # 只在这一条路径上把 SIGTERM/SIGHUP 转成 KeyboardInterrupt:
                        # 唯有它建持久任务行(kg_build_jobs),也唯有它在中断时会取消
                        # 并排空在飞抽取。装宽了反而有害——本函数下面的 limit/
                        # retry_partial 分支以及 run_all/run_reparse/run_ingest 都各自
                        # 用池且不排空,把 SIGTERM 转成异常只会让 main() 返回 130 后卡在
                        # ThreadPoolExecutor 的 atexit join 里,worker 继续调模型写库,
                        # 等于拿掉了运维用 kill 停批处理的能力(它们本来就没有需要收尾的
                        # 持久状态)。Ctrl-C 在各阶段的行为不受此影响:SIGINT 本来就抛
                        # KeyboardInterrupt。
                        saved_signals = _install_termination_signals()
                        try:
                            out = repo.build_notebook_kg(  # 跨源并发抽取,逐源打印进度
                                notebook_id, progress=_kg_progress)
                        finally:
                            _restore_signals(saved_signals)
                    res["extracted"] = len(out["built"])
                    res["failed"] = len(out["failed"])
            else:
                if not llm_ok:
                    raise RuntimeError(
                        "kg_extract workload 未绑定系统模型服务")
                all_sids = mnt.source_ids(notebook_id)
                kgful = mnt.kg_covered_source_ids(notebook_id)
                partial = (
                    mnt.partial_kg_source_ids(notebook_id)
                    if retry_partial else set()
                )
                parsed = (
                    mnt.sources_with_elements(notebook_id)
                    if retry_partial else set(all_sids)
                )
                partial &= parsed
                targets = [
                    s for s in all_sids
                    if s in parsed and (s not in kgful or s in partial)
                ]
                if limit is not None:
                    targets = targets[:max(0, limit)]
                n_targets = len(targets)

                def _extract_one(
                    sid: str,
                ) -> tuple[str, bool, Exception | None]:
                    preserve_existing = sid in partial
                    try:
                        mnt.set_source_status(sid, "extracting")
                        if preserve_existing:
                            mnt.run_extraction(
                                sid, preserve_existing_until_complete=True
                            )
                        else:
                            mnt.run_extraction(sid)
                        mnt.set_source_status(sid, "extracted")
                        return sid, preserve_existing, None
                    except Exception as exc:  # noqa: BLE001 — 单源失败隔离
                        mnt.set_source_status(
                            sid, "extracted" if preserve_existing else "parsed"
                        )
                        return sid, preserve_existing, exc

                with _PoolReporter(report_interval, total=n_targets, log=log) as reporter:
                    futures = {
                        submit_job(_extract_one, sid): sid for sid in targets
                    }
                    for i, future in enumerate(as_completed(futures), 1):
                        sid, was_partial, error = future.result()
                        if error is None:
                            res["extracted"] += 1
                            if was_partial:
                                res["partial_retried"] += 1
                            log({
                                "phase": "kg",
                                "source_id": sid,
                                "status": "extracted",
                                "progress": f"{i}/{n_targets}",
                            })
                        else:
                            res["failed"] += 1
                            if was_partial:
                                res["partial_failed_preserved"] += 1
                            log({
                                "phase": "kg",
                                "source_id": sid,
                                "status": "failed",
                                "error": str(error),
                            })
                        reporter.done = i

        # ── no_rebuild:抽取后直接返回,不做 rebuild/scale-index ───────────────
        if no_rebuild:
            return res

        # ── Rebuild 阶段(有 LLM:概念描述/merge-review)→ 同样开 pool 自报 ────────
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        with _PoolReporter(report_interval, total=0, log=log, label="rebuild 阶段"):
            # force=(rebuild_only or fresh):rebuild_only 是显式「只重建」入口(用户主动重聚),
            # fresh 是显式「清 checkpoint 强制重裁」入口——两者都必须绕过跳过门,否则 fresh 清完
            # checkpoint 后仍被 force=False 的门控挡回缓存簇数,--fresh 变假 no-op。普通 kg 阶段两者
            # 皆 False 时走门控(force=False),无新文件时跳过重聚(本次优化点)。
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=(rebuild_only or fresh), fresh=fresh)
            res["clusters"] = clusters
            log({"phase": "kg", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id)

            # ── Scale-index 自动联(base tier 或已有索引) ─────────────────────
            nb = repo.get_notebook(notebook_id)
            is_base = (nb.tier == "base")
            has_index = mnt.has_scale_index(notebook_id)
            if is_base or has_index:
                manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
                scale_nodes = manifest.get("n_nodes", 0)
                res["scale_index_nodes"] = scale_nodes
                log({"phase": "kg", "status": "scale_index_built", "nodes": scale_nodes})
                print(f"scale index built (nodes={scale_nodes})", flush=True)

    finally:
        repo.settings.kg_incremental_fusion_enabled = orig_fusion   # 还原,避免污染 repo 实例
    return res


def run_all(repo: BatchIngestRepository, notebook_id: str,
            files: Iterable[Path],
            log: LogFn | None = None, report_interval: int = 15,
            fresh: bool = False) -> dict[str, int]:
    """流式 all:per-source 流水线(parse+embed+extract 在 process_source 内重叠),
    跨源并发提交到全局 KG job 池;**末尾一次** rebuild_unified_kg + 补节点向量(+ scale index)。

    - 新文件(hash 未见过)→ upload_sources(scheduler=submit_job(process_source)):
      parse + 后台 embed + extract 一次调用内并发完成。
    - 已 parse、缺 KG 的旧 source(同 hash 已摄取过)→ submit_job(extract_source):
      只补抽 KG(embed 在上次 ingest 已做,无需重嵌)。
    批量期强制 kg_auto_extract=True(让 process_source 走到 extract)且关 per-source 融合;
    finally 恢复两者原值。单 source 失败隔离,计入 failed,不连累其余。

    Provider throughput is fixed by the system model registry. This helper
    submits business jobs and never mutates provider capacity.

    fresh=True — 透传给末尾的 rebuild_unified_kg:清空 rebuild checkpoint 并强制
      merge 审查/概念描述全量重跑(隐含 force=True,理由同 run_kg)。
    """
    from app.services.kg.scheduler import submit_job

    log = log or (lambda _e: None)
    orig_auto = repo.settings.kg_auto_extract
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    repo.settings.kg_auto_extract = True                 # 强制 process_source 走 extract 分支
    repo.settings.kg_incremental_fusion_enabled = False  # 批量期关 per-source 融合,收尾一次 rebuild
    files = list(files)
    res = {"new": 0, "resumed": 0, "reparsed": 0, "extracted": 0, "failed": 0,
           "clusters": 0, "nodes_embedded": 0}
    try:
        # ── 分三批:新文件 / 已 parse 缺 KG 补抽 / 已存在但无 elements 需重新 parse ──
        # 关键:用「有没有 elements」而非「有没有 KG」判定是否已 parse。一个已建、
        # parse_status 看似前进、却没有 source_elements 的源(上次 parse 中断/未落地),
        # 若只走 extract_source 补抽,build_records 的接地校验没有 element 可对照 →
        # LLM 抽出的节点被整源丢弃 → objects=0,且重跑多少次都修不好(每次都空抽)。
        kgful = repo.maintenance.kg_covered_source_ids(notebook_id)
        parsed = repo.maintenance.sources_with_elements(notebook_id)
        new_files: List[Path] = []
        resume_sids: List[str] = []
        reparse_sids: List[str] = []
        for p in files:
            sid = source_id_by_hash(repo, notebook_id, sha256_bytes(p.read_bytes()))
            if sid is None:
                new_files.append(p)
            elif sid in kgful:
                pass                      # 已有 KG → 跳过(幂等,既不新建也不重抽)
            elif sid in parsed:           # 已 parse(有 elements)、缺 KG → 只需补抽
                resume_sids.append(sid)
            else:                         # 已存在但无 elements → 必须重新 parse(否则空抽)
                reparse_sids.append(sid)
        res["new"] = len(new_files)
        res["resumed"] = len(resume_sids)
        res["reparsed"] = len(reparse_sids)

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
        for sid in reparse_sids:
            futs[submit_job(repo.process_source, sid)] = sid   # 重新 parse 补 elements(+extract)
        for sid in resume_sids:
            futs[submit_job(repo.extract_source, sid)] = sid   # 只补抽 KG,不 embed

        # ── 等待全部 job,逐个打印进度、统计 producer/source 业务池 ──────
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
            # 门控(force=fresh):自动收尾重聚,除非显式 --fresh。无新增/变更且非 fresh 时
            # (如重跑同一批)输入版本未变 → 跳过整段重聚,直接返回缓存簇数(本次优化点);
            # fresh=True 必须同时 force=True,否则清完 checkpoint 仍被门控挡回缓存,
            # --fresh 变假 no-op(同 run_kg)。
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=fresh, fresh=fresh)
            res["clusters"] = clusters
            log({"phase": "all", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id)

            nb = repo.get_notebook(notebook_id)
            if nb.tier == "base" or repo.maintenance.has_scale_index(notebook_id):
                manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
                scale_nodes = manifest.get("n_nodes", 0)
                res["scale_index_nodes"] = scale_nodes
                log({"phase": "all", "status": "scale_index_built", "nodes": scale_nodes})
                print(f"scale index built (nodes={scale_nodes})", flush=True)
    finally:
        repo.settings.kg_auto_extract = orig_auto
        repo.settings.kg_incremental_fusion_enabled = orig_fusion
    return res


def run_reparse(repo: BatchIngestRepository, notebook_id: str,
                limit: int | None = None,
                log: LogFn | None = None, report_interval: int = 15,
                no_rebuild: bool = False) -> dict[str, int]:
    """对 notebook 内无 source_elements(未成功 parse)的存量源重跑 process_source 补
    elements,收尾一次 rebuild_unified_kg。

    修复历史 run_all resume 分流的存量:一个已建、parse_status 看似前进却没有
    source_elements 的源(上次 parse 中断/未落地),旧 run_all 会当成「已 parse、缺 KG」
    直接 extract_source 空抽——build_records 的接地校验没有 element 可对照 → LLM 抽出的
    节点被整源丢弃 → objects=0,且直接重抽永远补不出 KG。必须重新 parse 生成 elements。
    跨源并发提交到 KG job 池。有 elements 的源自动跳过(幂等)。

    Provider throughput is registry-owned; this function only submits jobs."""
    from app.services.kg.scheduler import submit_job

    log = log or (lambda _e: None)
    orig_auto = repo.settings.kg_auto_extract
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    repo.settings.kg_auto_extract = True                 # process_source 走到 extract
    # 批量期关 per-source 增量融合:否则每源抽完都触发 incremental_fuse_source,要加载整个
    # notebook 的 cluster_map + 写 concept_clusters,巨型库(几万源)O(N²) 会卡死(16 路争
    # vector_cache/写锁)。收尾的 rebuild_unified_kg 会做一次全量融合(与 run_all/run_kg 一致)。
    repo.settings.kg_incremental_fusion_enabled = False

    res = {"reparsed": 0, "failed": 0, "clusters": 0, "nodes_embedded": 0}
    try:
        mnt = repo.maintenance
        parsed = mnt.sources_with_elements(notebook_id)
        targets = [s for s in mnt.source_ids(notebook_id) if s not in parsed]
        if limit is not None:
            targets = targets[:max(0, limit)]
        res["targets"] = len(targets)
        if not targets:
            print("reparse: 无缺 elements 的源,跳过", flush=True)
            return res

        futs = {submit_job(repo.process_source, sid): sid for sid in targets}
        total = len(futs)
        with _PoolReporter(report_interval, total=total, log=log) as reporter:
            for i, fut in enumerate(as_completed(futs), 1):
                sid = futs[fut]
                try:
                    fut.result()
                    res["reparsed"] += 1
                    print(f"[reparse {i}/{total}] {sid} ✓", flush=True)
                    log({"phase": "reparse", "source_id": sid, "status": "ok", "progress": f"{i}/{total}"})
                except Exception as exc:   # noqa: BLE001 — 单源失败隔离
                    res["failed"] += 1
                    print(f"[reparse {i}/{total}] {sid} ✗ {type(exc).__name__}: {exc}", flush=True)
                    log({"phase": "reparse", "source_id": sid, "status": "failed", "error": str(exc)})
                reporter.done = i

        if no_rebuild:
            return res
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        with _PoolReporter(report_interval, total=0, log=log, label="rebuild 阶段"):
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=False, fresh=False)
            res["clusters"] = clusters
            log({"phase": "reparse", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id)
        return res
    finally:
        repo.settings.kg_auto_extract = orig_auto
        repo.settings.kg_incremental_fusion_enabled = orig_fusion


def run_index(repo: BatchIngestRepository, notebook_id: str) -> dict[str, int]:
    """Phase 3 (offline): build the scalable-retrieval index for a (base) notebook.
    Static base KGs should re-run this after a rebuild."""
    manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
    return {"indexed_nodes": manifest.get("n_nodes", 0)}


def backfill_node_embeddings(
    repo: BatchIngestRepository, notebook_id: str
) -> int:
    """补 KG 节点向量；未绑定 knowledge_object_embedding 时跳过。"""
    if not repo.configured("knowledge_object_embedding"):
        return 0
    mnt = repo.maintenance
    def _p(done: int, total: int) -> None:
        end = "\n" if done >= total else "\r"
        print(f"  节点向量: {done}/{total}", end=end, flush=True)
    count = mnt.backfill_node_embeddings(notebook_id, progress=_p)
    # Backfilling node vectors changes the ANN inputs → mark dirty so the cluster
    # version's kg_mutation_seq advances (a later force=False rebuild must not skip
    # on the strength of unchanged object/decided counts alone).
    mnt.mark_unified_kg_dirty(notebook_id)
    return count


def _count_missing_chunk_vectors(
    repo: BatchIngestRepository, notebook_id: str
) -> int:
    return repo.maintenance.count_missing_chunk_vectors(notebook_id)


def _count_missing_node_vectors(
    repo: BatchIngestRepository, notebook_id: str
) -> int:
    return repo.maintenance.count_missing_node_vectors(notebook_id)


def _count_missing_element_vectors(
    repo: BatchIngestRepository, notebook_id: str
) -> int:
    return repo.maintenance.count_missing_element_vectors(notebook_id)


def run_embed(
    repo: BatchIngestRepository, notebook_id: str
) -> dict[str, int]:
    """补该 notebook 缺失的 chunk + element + 节点向量(幂等,只补缺失)。

    先 SQL 盘点缺失数并打印,再 backfill_chunk_embeddings(missing_only=True) +
    backfill_element_embeddings + backfill_node_embeddings(节点本就只补缺失),
    最后打印 after 盘点。
    """
    chunk_missing = _count_missing_chunk_vectors(repo, notebook_id)
    element_missing = _count_missing_element_vectors(repo, notebook_id)
    node_missing = _count_missing_node_vectors(repo, notebook_id)
    print(f"embed: 缺失盘点 chunk={chunk_missing} element={element_missing} "
          f"node={node_missing}", flush=True)

    chunks_embedded = backfill_chunk_embeddings(repo, notebook_id, missing_only=True)
    elements_embedded = backfill_element_embeddings(repo, notebook_id)
    nodes_embedded = backfill_node_embeddings(repo, notebook_id)

    chunk_after = _count_missing_chunk_vectors(repo, notebook_id)
    element_after = _count_missing_element_vectors(repo, notebook_id)
    node_after = _count_missing_node_vectors(repo, notebook_id)
    print(f"embed done: 补 chunk={chunks_embedded} element={elements_embedded} "
          f"node(扫描)={nodes_embedded};剩余缺失 chunk={chunk_after} "
          f"element={element_after} node={node_after}", flush=True)
    return {
        "chunks_embedded": chunks_embedded,
        "elements_embedded": elements_embedded,
        "nodes_embedded": nodes_embedded,
        "chunk_missing_before": chunk_missing,
        "element_missing_before": element_missing,
        "node_missing_before": node_missing,
    }


# (table, id_column) for every embeddings table the BLOB backfill covers —
# canonical tuple lives with the maintenance adapter (imported above).

_BACKFILL_BATCH_SIZE = 5000
_BACKFILL_MAP_CHUNKSIZE = 256
_BACKFILL_DEFAULT_WORKERS = min(32, os.cpu_count() or 1)


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


def _parse_encode_batch_serial(
    rows: Sequence[RepositoryRow],
) -> List[Tuple[bytes, str, str]]:
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


def _backfill_table_to_blob(repo: BatchIngestRepository, notebook_id: Optional[str],
                            table: str, id_col: str,
                            batch_size: int = _BACKFILL_BATCH_SIZE,
                            workers: int = 1) -> dict[str, object]:
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
    SQLite stays single-writer, workers never open a connection (the
    per-batch SELECT+UPDATE transaction itself lives on the maintenance
    adapter; the encode callback runs inside it, exactly like the old inline
    `_write()` block). If the pool dies (BrokenProcessPool), the run falls
    back to the serial path for this batch and every subsequent one
    (fail-open: a crashed pool must never lose the run, just lose the
    parallel speedup)."""
    mnt = repo.maintenance

    # typeof() 无法走索引 → 这个 COUNT 是全表扫描,大表(如百万级 relation_embeddings)
    # 要几分钟。先出声,免得上一张表打完 N/N 后长时间静默被当成"卡死"。
    print(f"  [blob] {table}: 扫描待转行(大表可能数分钟,无输出≠卡死)…", flush=True)
    total = mnt.count_text_vector_rows(table, id_col, notebook_id)
    converted = 0
    skipped_bad = 0
    if total == 0:
        print(f"  [blob] {table}: 0/0 (无待转行)", flush=True)
        return {"table": table, "total": total, "converted": 0, "skipped_bad": 0}

    use_pool = workers > 1
    executor = None
    if use_pool:
        try:
            executor = ProcessPoolExecutor(max_workers=workers)
        except (OSError, PermissionError, NotImplementedError) as exc:
            # Restricted containers/macOS sandboxes may reject POSIX semaphore
            # discovery before the pool exists. This is the same availability
            # failure class as a pool that dies later: retain correctness and
            # lose only the parallel speedup.
            print(
                f"  [blob] {table}: 进程池不可用,回退串行(fallback to serial): {exc}",
                flush=True,
            )
            use_pool = False

    def _encode(
        rows: Sequence[RepositoryRow],
    ) -> list[tuple[bytes, str, str]]:
        """Parse+encode one selected batch — runs inside the adapter's write
        transaction (same boundary as the old inline block)."""
        nonlocal use_pool, executor
        if use_pool:
            try:
                pairs = [(r["vid"], r["vector"]) for r in rows]
                by_vid = {vid: blob for vid, blob in executor.map(
                    _parse_encode, pairs, chunksize=_BACKFILL_MAP_CHUNKSIZE)}
                return [(by_vid[r["vid"]], r["notebook_id"], r["vid"]) for r in rows]
            except BrokenProcessPool as exc:
                print(f"  [blob] {table}: 进程池崩溃,回退串行(fallback to serial): {exc}",
                      flush=True)
                executor.shutdown(wait=False, cancel_futures=True)
                executor = None
                use_pool = False
                return _parse_encode_batch_serial(rows)
        return _parse_encode_batch_serial(rows)

    try:
        while True:
            n_rows, bad = mnt.convert_text_vector_batch(
                table, id_col, notebook_id, batch_size, _encode)
            if n_rows == 0:
                break
            skipped_bad += bad
            converted += n_rows
            print(f"  [blob] {table}: {converted}/{total}", flush=True)
            if n_rows < batch_size:
                break
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
    return {"table": table, "total": total, "converted": converted, "skipped_bad": skipped_bad}


def run_vectors_to_blob(repo: BatchIngestRepository, notebook_id: Optional[str],
                        all_notebooks: bool = False,
                        workers: int = 1) -> dict[str, object]:
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


_KOS_BACKFILL_BATCH_SIZE = 2000


def _backfill_source_index_for_notebook(
    repo: BatchIngestRepository, notebook_id: str
) -> dict[str, object]:
    """Proactively populate knowledge_object_sources for ONE notebook (P0-4).

    Idempotent + restartable: clears any partial rows for this notebook first,
    then re-derives them from knowledge_objects.evidence in batches (bounded
    memory — never loads the whole notebook's evidence at once, unlike the
    legacy scan this table replaces), and marks
    unified_kg_state.source_index_backfilled=1 at the end so the online
    _clear_source_extraction_state fast path activates immediately (no need to
    wait for a source delete/reparse to trigger the first-use backfill)."""
    mnt = repo.maintenance
    total = mnt.clear_source_index(notebook_id)
    if total == 0:
        mnt.mark_source_index_backfilled(notebook_id)
        print(f"  [source-index] {notebook_id}: 0/0 (无 knowledge_objects)", flush=True)
        return {"notebook_id": notebook_id, "objects": 0, "rows": 0}

    processed = 0
    rows_written = 0
    last_id = ""
    while True:
        n_batch, wrote, last_id = mnt.backfill_source_index_batch(
            notebook_id, last_id, _KOS_BACKFILL_BATCH_SIZE)
        if n_batch == 0:
            break
        rows_written += wrote
        processed += n_batch
        print(f"  [source-index] {notebook_id}: {processed}/{total}", flush=True)
        if n_batch < _KOS_BACKFILL_BATCH_SIZE:
            break
    mnt.mark_source_index_backfilled(notebook_id)
    return {"notebook_id": notebook_id, "objects": processed, "rows": rows_written}


def run_backfill_source_index(repo: BatchIngestRepository, notebook_id: Optional[str],
                              all_notebooks: bool = False) -> dict[str, object]:
    """Proactive backfill CLI (P0-4 fast-follow): populate knowledge_object_sources
    ahead of the first source delete/reparse, so that operation is never the one
    paying the legacy full-evidence-scan cost online. Purely additive/idempotent —
    safe to re-run (each notebook's rows are cleared and rebuilt from the current
    knowledge_objects.evidence, then re-marked backfilled)."""
    if not notebook_id and not all_notebooks:
        raise ValueError("run_backfill_source_index: 需要 notebook_id 或 all_notebooks=True")
    if all_notebooks:
        targets = repo.maintenance.all_notebook_ids()
    else:
        repo.get_notebook(notebook_id)  # KeyError if missing
        targets = [notebook_id]
    print(f"backfill-source-index: scope={'全部 notebook (' + str(len(targets)) + ')' if all_notebooks else notebook_id}",
          flush=True)
    results = [_backfill_source_index_for_notebook(repo, nb_id) for nb_id in targets]
    total_objects = sum(r["objects"] for r in results)
    total_rows = sum(r["rows"] for r in results)
    print(f"backfill-source-index done: notebooks={len(results)} objects={total_objects} rows={total_rows}",
          flush=True)
    return {"notebooks": results, "objects": total_objects, "rows": total_rows}


def run_metadata(repo, args) -> int:
    """phase=metadata:补抽 notebook 内缺论文元数据的源(幂等可续跑;--force 重抽)。
    只作用于已解析的 academic_paper 源;原始 PDF 缺失也可跑(读 DB 内 elements)。"""
    if not repo.configured("paper_metadata"):
        print("error: paper_metadata workload 未绑定模型服务 → 无法抽取论文元数据。"
              "请配置 MODEL_SERVICES_CONFIG 后重试(CLI 不静默降级)。", file=sys.stderr)
        return 2
    if not args.notebook_id:
        print("error: --phase metadata 需要 --notebook-id(本 phase 不新建 notebook)。",
              file=sys.stderr)
        return 2

    def _progress(done: int, total: int, source_id: str, status: str) -> None:
        print(f"[meta {done}/{total}] {source_id} {status}", flush=True)

    counts = repo.backfill_paper_metadata(
        args.notebook_id, force=args.force, progress=_progress
    )
    print(f"[meta done] {json.dumps(counts, ensure_ascii=False)}", flush=True)
    return 0


def _make_logger(manifest_path: Optional[Path]) -> LogFn:
    if manifest_path is None:
        return lambda _e: None
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(entry: dict[str, object]) -> None:
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
        prog="batch_ingest",
        description="SQLite-only 离线批量摄取目录 → 项目 KG/向量库",
    )
    p.add_argument("phase", choices=["ingest", "kg", "index", "all", "embed", "vectors-to-blob",
                                      "backfill-source-index", "metadata", "reparse"])
    p.add_argument("--input-dir", type=Path, help="递归扫描的根目录(ingest/all 必填)")
    p.add_argument("--notebook-id", default=None, help="目标 notebook;省略则新建")
    p.add_argument("--all-notebooks", action="store_true",
                   help="vectors-to-blob / backfill-source-index 专用:作用于全库全部 notebook,忽略 --notebook-id")
    p.add_argument("--notebook-name", default=None,
                   help="新建 notebook 名(ingest/all 新建库时必填;不再默认用目录名)")
    p.add_argument("--owner", default=None,
                   help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    p.add_argument("--workers", type=int, default=None,
                   help="源/文档级并发；省略时继承 KG_JOB_CONCURRENCY。"
                        "vectors-to-blob 阶段为 json.loads/编码并行进程数"
                        f"(默认 min(32, CPU核数)={_BACKFILL_DEFAULT_WORKERS},1 走原串行路径,"
                        "不启动进程池;别到 64——单写 SQLite executemany + IPC 在 ~16-24 处封顶)")
    p.add_argument("--limit", type=int, default=None,
                   help="kg 阶段只抽前 N 个未抽源 / reparse 阶段只处理前 N 个缺 elements 源"
                        "(仅限制本次数量;最终 rebuild 仍覆盖全本 notebook)")
    p.add_argument("--no-rebuild", action="store_true",
                   help="kg/reparse 阶段:只抽取/重解析,跳过收尾 rebuild_unified_kg 和 scale index。"
                        "大批量分批抽取用法:重复 'kg --limit N --no-rebuild',最后一次 'kg --rebuild-only'。")
    p.add_argument("--rebuild-only", action="store_true",
                   help="kg 阶段:跳过抽取,直接 rebuild_unified_kg + 节点向量 + scale index(base tier 时)。")
    p.add_argument("--retry-partial", action="store_true",
                   help="kg 阶段:一并重试最新抽取记录 windows_failed>0 且已有部分 KG 的来源；"
                        "失败或空结果保留旧图，零失败窗口且非空后才事务替换。")
    p.add_argument("--fresh", action="store_true",
                   help="清空 rebuild checkpoint,强制 merge 审查/概念描述全量重跑"
                        "(kg 与 all 阶段的收尾 rebuild 均适用;用于只换了 KG 模型/阈值、"
                        "数据没变的场景)。")
    p.add_argument("--allow-no-embed", action="store_true",
                   help="chunk_embedding workload 未绑定时显式允许无向量降级"
                        "(默认拒绝,防静默产出无向量库)")
    p.add_argument("--force", action="store_true",
                   help="metadata phase: 已有元数据行的源也重抽(prompt/校验升级后刷新)")
    p.add_argument("--pool-report-interval", type=int, default=15,
                   help="每 N 秒自报 producer/source 业务线程池占用;0 关闭。"
                        "all/kg/reparse 阶段生效")
    p.add_argument("--dry-run", action="store_true", help="只扫描+报告,不写库")
    return p


def _dispatch_main(
    args: argparse.Namespace,
    repo: BatchIngestRepository,
    effective: EffectiveConcurrency,
) -> int:
    if args.phase == "vectors-to-blob":
        # 纯格式转换(已算好的向量 JSON→BLOB),不产出新向量,不需要 EMBED 就绪,
        # 也不走 ensure_notebook(不新建库;--notebook-id 必须是已存在的库)。
        _t = time.perf_counter()
        r = run_vectors_to_blob(
            repo,
            args.notebook_id,
            all_notebooks=args.all_notebooks,
            workers=effective.workers,
        )
        print(
            f"vectors-to-blob done: {r} ({time.perf_counter() - _t:.1f}s)",
            flush=True,
        )
        return 0

    if args.phase == "backfill-source-index":
        # 纯 SQL 派生索引重建(evidence JSON → knowledge_object_sources),不需要
        # EMBED 就绪,也不走 ensure_notebook(不新建库;--notebook-id 必须是已存在的库)。
        _t = time.perf_counter()
        r = run_backfill_source_index(
            repo,
            args.notebook_id,
            all_notebooks=args.all_notebooks,
        )
        print(
            f"backfill-source-index done: {r} "
            f"({time.perf_counter() - _t:.1f}s)",
            flush=True,
        )
        return 0

    if args.phase == "metadata":
        print(
            f"concurrency: source={effective.workers}({effective.workers_source})",
            flush=True,
        )
        return run_metadata(repo, args)

    # embed 子命令就是补向量:EMBED 未配直接报错,忽略 --allow-no-embed。
    allow_no_embed = args.allow_no_embed and args.phase != "embed"
    if not repo.configured("chunk_embedding"):
        if not allow_no_embed:
            extra = (
                "\n  注意:embed 子命令用于补向量,--allow-no-embed 对它无效。"
                if args.phase == "embed"
                else ""
            )
            print(
                "error: chunk_embedding 未绑定系统模型服务 → 不会产出向量，检索将失效。\n"
                "  请由维护人员检查 MODEL_SERVICES_CONFIG 中的服务与 workload 绑定。\n"
                "  确实要无向量导入,请显式加 --allow-no-embed。"
                + extra,
                file=sys.stderr,
            )
            return 2
        print(
            "[warn] --allow-no-embed:无向量模式,本次不产出 chunk/节点向量。",
            flush=True,
        )

    nb_name = args.notebook_name or "Batch Import"
    notebook_id = ensure_notebook(repo, args.notebook_id, nb_name)
    manifest = Path(repo.storage_dir) / "batch_ingest" / f"{notebook_id}.jsonl"
    log = _make_logger(manifest)
    print(f"notebook={notebook_id} manifest={manifest}", flush=True)
    log({
        "phase": "concurrency",
        "workers": effective.workers,
        "workers_source": effective.workers_source,
    })
    print(
        f"concurrency: source={effective.workers}({effective.workers_source})",
        flush=True,
    )

    if args.phase == "index":
        print(f"phase=index notebook={notebook_id}", flush=True)
        _t = time.perf_counter()
        r = run_index(repo, notebook_id)
        print(f"index done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)
        return 0

    if args.phase == "all":
        print("phase=all (pipelined)", flush=True)
        _t = time.perf_counter()
        r = run_all(
            repo,
            notebook_id,
            iter_files(args.input_dir),
            log=log,
            report_interval=args.pool_report_interval,
            fresh=getattr(args, "fresh", False),
        )
        print(f"all done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)
        return 0

    if args.phase == "ingest":
        files = iter_files(args.input_dir)
        print(f"phase=ingest files={len(files)}", flush=True)
        _t = time.perf_counter()
        c = run_ingest(
            repo,
            notebook_id,
            files,
            workers=effective.workers,
            log=log,
        )
        print(
            f"ingest done: {c} ({time.perf_counter() - _t:.1f}s)",
            flush=True,
        )

    if args.phase == "kg":
        no_rebuild = getattr(args, "no_rebuild", False)
        rebuild_only = getattr(args, "rebuild_only", False)
        fresh = getattr(args, "fresh", False)
        retry_partial = getattr(args, "retry_partial", False)
        print(
            f"phase=kg limit={args.limit} no_rebuild={no_rebuild} "
            f"rebuild_only={rebuild_only} fresh={fresh} "
            f"retry_partial={retry_partial}",
            flush=True,
        )
        _t = time.perf_counter()
        r = run_kg(
            repo,
            notebook_id,
            limit=args.limit,
            log=log,
            no_rebuild=no_rebuild,
            rebuild_only=rebuild_only,
            fresh=fresh,
            report_interval=args.pool_report_interval,
            retry_partial=retry_partial,
        )
        print(f"kg done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    if args.phase == "reparse":
        no_rebuild = getattr(args, "no_rebuild", False)
        print(
            f"phase=reparse limit={args.limit} no_rebuild={no_rebuild}",
            flush=True,
        )
        _t = time.perf_counter()
        r = run_reparse(
            repo,
            notebook_id,
            limit=args.limit,
            log=log,
            no_rebuild=no_rebuild,
            report_interval=args.pool_report_interval,
        )
        print(
            f"reparse done: {r} ({time.perf_counter() - _t:.1f}s)",
            flush=True,
        )

    if args.phase == "embed":
        print(f"phase=embed notebook={notebook_id}", flush=True)
        _t = time.perf_counter()
        r = run_embed(repo, notebook_id)
        print(f"embed done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    return 0


def _install_termination_signals() -> list[tuple[int, object]]:
    """把 SIGTERM/SIGHUP 转成 KeyboardInterrupt,让批处理被终止时走和 Ctrl-C 同一条
    收尾路径。对 kg 阶段是必需的:不收尾,kg_build_jobs 那行就永久停在 'running',
    该 notebook 之后的每次分析都会被单飞守卫挡成 KgBuildAlreadyRunning,而离线进程
    刻意无权自行清理(启动期兜底只属于服务端 lifespan)。

    返回 [(signum, 原 handler)] 供调用方还原。约束三条:
      * 只能在主线程装(signal.signal 的硬性限制),否则原样返回空表;
      * **已被设成忽略的信号保持忽略**——`nohup` 会把 SIGHUP 置为 SIG_IGN,把它抢
        回来会让本来能扛住掉线的长跑批处理在 SSH 断开时被杀掉;
      * SIGINT 不动(Python 默认已抛 KeyboardInterrupt)。
    SIGKILL / 掉电不可捕获,那类残留仍只能由后端启动时的崩溃兜底清理。
    """
    if threading.current_thread() is not threading.main_thread():
        return []

    def _raise_interrupt(signum, _frame):
        raise KeyboardInterrupt(f"terminated by signal {signum}")

    installed: list[tuple[int, object]] = []
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:  # pragma: no cover - 平台差异(Windows 无 SIGHUP)
            continue
        try:
            previous = signal.getsignal(sig)
            if previous is signal.SIG_IGN:
                continue
            signal.signal(sig, _raise_interrupt)
        except (OSError, ValueError):  # pragma: no cover - 平台限制
            continue
        installed.append((int(sig), previous))
    return installed


def _restore_signals(saved: Sequence[tuple[int, object]]) -> None:
    for signum, previous in saved:
        try:
            signal.signal(signum, previous)
        except (OSError, ValueError, TypeError):  # pragma: no cover
            continue


def _idle_since(updated_at: str) -> str:
    """把 job 的 updated_at 转成「距今」;解析不了就返回空串(只用于提示)。"""
    from datetime import datetime, timezone

    try:
        stamp = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return ""
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - stamp).total_seconds())
    if seconds < 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def _report_kg_build_already_running(
    repo: BatchIngestRepository, notebook_id: str
) -> None:
    """单飞守卫挡回时给出可操作的说明,而不是抛 sqlite 的 UNIQUE 约束 traceback。
    判据(最后更新是否停滞)交给操作者:离线进程无法确认那行是否属于一个仍活着的
    后端进程,所以只呈现事实 + 两条出路,绝不代为清理。"""
    print(
        "error: 该笔记本已有一个分析任务处于「进行中」，本次不启动"
        "（同一笔记本同时只允许一个）。",
        file=sys.stderr,
    )
    print(f"  笔记本: {notebook_id}", file=sys.stderr)
    job = None
    try:
        job = repo.get_notebook(notebook_id).kg_build
    except Exception:  # noqa: BLE001 - 诊断信息拿不到也要把出路打印出来
        job = None
    if job is not None:
        idle = _idle_since(job.updated_at)
        mode = {
            "incremental": "增量分析",
            "rebuild": "全部重新分析",
        }.get(job.mode, job.mode)
        print(
            f"  现存任务: id={job.job_id} 方式={mode} 阶段={job.stage} "
            f"完成 {job.completed_sources}/{job.total_sources} "
            f"失败 {job.failed_sources}",
            file=sys.stderr,
        )
        print(
            f"  最后更新: {job.updated_at}"
            + (f"（距今 {idle}）" if idle else ""),
            file=sys.stderr,
        )
    print(
        "  · 若它确实在运行（另一个 batch_ingest 进程，或网页端触发的分析），"
        "等它结束后重跑本命令即可。\n"
        "  · 若「最后更新」已停滞很久，它多半是上次被强制终止"
        "（kill -9 / 掉电 / 机器重启）留下的残留：重启后端服务即可清理"
        "（启动时会把这类进行中的任务统一落成「已中断」）。\n"
        "    离线命令刻意不代为清理——它无法确认这行是否属于一个仍在运行的后端。",
        file=sys.stderr,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

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

    if args.phase == "reparse" and not args.notebook_id:
        print("error: --notebook-id required for reparse (specify the notebook to re-parse)",
              file=sys.stderr)
        return 2

    if args.phase == "vectors-to-blob" and not args.notebook_id and not args.all_notebooks:
        print("error: vectors-to-blob 需要 --notebook-id 或 --all-notebooks", file=sys.stderr)
        return 2

    if args.phase == "backfill-source-index" and not args.notebook_id and not args.all_notebooks:
        print("error: backfill-source-index 需要 --notebook-id 或 --all-notebooks", file=sys.stderr)
        return 2

    settings = Settings()
    try:
        effective = _resolve_effective_concurrency(args, settings, args.phase)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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

    if database_identity(settings.database_url).scheme != "sqlite":
        print(
            "error: batch_ingest mutation phases are SQLite-only. "
            "For PostgreSQL, use the normal app/API upload and KG/reindex flows.",
            file=sys.stderr,
        )
        return 2

    repo = SQLiteRepository(settings)
    batch_user = (
        _resolve_owner_profile(repo, args.owner)
        if args.owner is not None
        else repo.current_user()
    )
    user_token = set_request_user(batch_user)
    # 终止信号的转换**不**在这里全局安装(见 run_kg 里 build_notebook_kg 那一处的说明:
    # 只有会排空的路径才配得上它)。这里只负责把中断收成干净的输出与 shell 惯例退出码。
    try:
        return _dispatch_main(args, repo, effective)
    except KgBuildAlreadyRunning as exc:
        _report_kg_build_already_running(
            repo, str(exc).strip() or (args.notebook_id or "")
        )
        return 2
    except KeyboardInterrupt:
        # 收尾已在被中断的那层各自完成(kg 会把 job 落成「已中断」终态);
        # 这里只负责不要用 traceback 糊住终端,并给出 shell 惯例的 130。
        print(
            "\n已中断。已完成的内容都已保留，可重跑同一命令继续未完成部分。",
            file=sys.stderr,
        )
        return 130
    finally:
        reset_request_user(user_token)
