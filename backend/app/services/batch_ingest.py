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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, List, Mapping, Optional, Protocol, Sequence, Tuple

from app.core.config import Settings
from app.core.request_context import set_request_user, reset_request_user
from app.models.notebooks import NotebookCreate, NotebookSummary
from app.models.sources import SourceSummary
from app.models.identity import UserProfile
from app.repositories.sqlite.maintenance import VECTOR_TABLES as _VECTOR_TABLES
from app.services.repository import UploadedSourceFile
from app.services.sqlite_repository import SQLiteRepository
from app.services.model_concurrency import (
    ConcurrencySnapshot,
    ModelConcurrencyState,
    activate_model_concurrency,
    current_model_concurrency,
)
import sqlite3
from app.repositories.ports import (
    ExtractionProgress,
    IndexStageProgress,
    JsonChatClientPort,
    KGBuildResult,
    RebuildProgress,
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
    llm_client: JsonChatClientPort

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
    llm: int
    embedding: int
    workers_source: str
    llm_source: str
    embedding_source: str


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
        llm=_positive(
            args.llm_conc
            if args.llm_conc is not None
            else settings.kg_extract_workers,
            "--llm-conc",
        ),
        embedding=_positive(
            args.embed_conc
            if args.embed_conc is not None
            else settings.embed_concurrency,
            "--embed-conc",
        ),
        workers_source="cli" if args.workers is not None else "env",
        llm_source="cli" if args.llm_conc is not None else "env",
        embedding_source="cli" if args.embed_conc is not None else "env",
    )


@contextmanager
def _batch_concurrency_scope(
    repo: BatchIngestRepository,
    effective: EffectiveConcurrency,
) -> Iterator[ModelConcurrencyState]:
    from app.services.kg import scheduler

    old_settings = (
        repo.settings.kg_job_concurrency,
        repo.settings.kg_extract_workers,
        repo.settings.embed_concurrency,
    )
    old_window = scheduler.max_workers()
    old_job = scheduler.job_concurrency()
    phase_error: BaseException | None = None
    try:
        repo.settings.kg_job_concurrency = effective.workers
        repo.settings.kg_extract_workers = effective.llm
        repo.settings.embed_concurrency = effective.embedding
        scheduler.configure(
            window_workers=effective.llm,
            job_workers=effective.workers,
        )
        with activate_model_concurrency(
            llm_max=effective.llm,
            embed_max=effective.embedding,
        ) as state:
            yield state
    except BaseException as exc:
        phase_error = exc
        raise
    finally:
        cleanup_errors: list[tuple[str, BaseException]] = []
        for name, old_value in zip(
            (
                "kg_job_concurrency",
                "kg_extract_workers",
                "embed_concurrency",
            ),
            old_settings,
        ):
            try:
                setattr(repo.settings, name, old_value)
            except BaseException as restore_error:
                cleanup_errors.append(
                    (f"batch setting {name}", restore_error)
                )
        try:
            scheduler.configure(
                window_workers=old_window,
                job_workers=old_job,
            )
        except BaseException as restore_error:
            cleanup_errors.append(("KG scheduler", restore_error))

        if phase_error is not None:
            for label, restore_error in cleanup_errors:
                print(
                    f"warning: failed to restore {label} after phase error: "
                    f"{type(restore_error).__name__}: {restore_error}",
                    file=sys.stderr,
                )
        elif len(cleanup_errors) == 1:
            raise cleanup_errors[0][1]
        elif cleanup_errors:
            raise BaseExceptionGroup(
                "batch concurrency cleanup failed",
                [restore_error for _, restore_error in cleanup_errors],
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
    llm: ConcurrencySnapshot,
    embedding: ConcurrencySnapshot,
    done: int,
    total: int,
    label: str = "",
) -> str:
    """Render gate-backed model utilization plus the source job pool."""
    tail = f" · 源完成 {done}/{total}" if total > 0 else (f" · {label}" if label else "")
    return (
        f"[pool {ts}] LLM {llm.active}/{llm.maximum} waiting={llm.waiting}"
        f" · embedding {embedding.active}/{embedding.maximum} waiting={embedding.waiting}"
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
                state = current_model_concurrency()
                llm_snapshot = (
                    state.llm.snapshot()
                    if state is not None
                    else ConcurrencySnapshot(active=0, maximum=0, waiting=0)
                )
                embed_snapshot = (
                    state.embedding.snapshot()
                    if state is not None
                    else ConcurrencySnapshot(active=0, maximum=0, waiting=0)
                )
                line = _format_pool_snapshot(
                    time.strftime("%H:%M:%S"),
                    s,
                    llm=llm_snapshot,
                    embedding=embed_snapshot,
                    done=self.done,
                    total=self.total,
                    label=self.label,
                )
                print(line, flush=True)
                if self.log:
                    self.log({
                        "phase": "pool",
                        **s,
                        "llm_active": llm_snapshot.active,
                        "llm_max": llm_snapshot.maximum,
                        "llm_waiting": llm_snapshot.waiting,
                        "embed_active": embed_snapshot.active,
                        "embed_max": embed_snapshot.maximum,
                        "embed_waiting": embed_snapshot.waiting,
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


_IN_FLIGHT_PARSE = ("queued", "parsing", "extracting")


def source_id_in_flight(repo: BatchIngestRepository, source_id: str) -> bool:
    """这一刻该源的 parse 管线是否可能正在别处跑(主键点查,实时值)。

    run_ingest 的 in_flight 快照是进池前取的一次,重解析前用本函数就地复核,把
    「取快照之后才被服务端接手」的窗口压到最小。取值集合与
    maintenance.sources_in_flight_parse 必须一致。"""
    try:
        summary = repo.get_source(source_id)
    except KeyError:      # 期间被删了:也不该由本次运行去重解析
        return True
    return (summary.parse_status or summary.status) in _IN_FLIGHT_PARSE


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
    conc: int = 4,
    log: LogFn | None = None,
) -> dict[str, int]:
    """Phase 1:解析+分块(摄取期 EMBED 置空)+ 收尾低并发补 chunk 向量。无 LLM。

    跳过判据是「这个源的 parse 到底跑完没有」而非「hash 在不在库」。file_hash 是
    INSERT 时写的(早于 parse),所以一个 parse 中途被中断的源,hash 已经在库、内容却
    是空的:旧的二元判定(hash 命中即 skipped)会把它永久认账成已摄取,再也补不回来。
    两分流(ingest 是无 KG 阶段,没有「已有 KG → 跳过」那一档):
      hash 未命中           → upload_sources → uploaded
      命中且管线已跑完      → 真的已摄取     → skipped
      命中且**可能正在跑**  → 不抢,让出      → in_flight
      命中且管线没跑完      → process_source → reparsed / failed(按真实结果计)

    「已跑完」只认**管线终态**(sources_with_completed_parse),刻意不看「有没有
    element」。两个方向都会错:
      · 只看 element「有」→ 漏判。elements 是管线中段的原子写入,其后还有分块、
        向量、终态置位;崩在中间的源有 element 却没有 chunk,按「有 element 即完成」
        会被永远跳过,而收尾只嵌入已存在的 chunk、救不回来。
      · 只看 element「无」→ 误判。一次成功的 parse 完全可以产出零个 element
        (扫描版/纯图 PDF 没有文本层,'metadata-only' 导入源更是永远没有),按
        「无 element 即中断」会把它们每次重跑都再解析一遍,永远不收敛。
    """
    log = log or (lambda _e: None)
    counts = {"uploaded": 0, "skipped": 0, "reparsed": 0, "in_flight": 0, "failed": 0}
    orig_provider = repo.settings.embed_provider
    repo.settings.embed_provider = ""   # 摄取期零嵌入:parse+chunk 快、无 429

    # 进池前取一次快照。判据是**管线终态**,不是「有没有 element」:elements 的原子
    # 写入发生在管线中段(其后还有分块、向量、终态置位),所以「有 element」只证明跑到
    # 过那一步,不证明跑完了。崩在 elements 已写、chunks 未建之间的源就是这样——按
    # 「有 element 即完成」会永远跳过它,而收尾的 backfill_chunk_embeddings 只嵌入
    # **已存在**的 chunk,一个 chunk 都没有的源它救不回来。
    # 「已完成」= 有 chunks ∪ (管线终态 ∩ 没有 elements)。
    #
    # 这个式子是六种情形逼出来的,单看任何一个信号都会漏或误:
    #   ① elements 已写、未分块就崩   → 无 chunk、非终态       → 要重跑
    #   ② parse+分块成功、KG 抽取抛错 → 有 chunk               → 不重跑(外层 except 会
    #      把整源记成 failed,只按状态判就会每次清掉有效产物重来,LLM 一直挂就一直不收敛)
    #   ③ parse 本身失败              → 无 chunk、非终态       → 要重跑
    #   ④ 扫描版 PDF 零 element 成功  → 无 chunk,但终态且无 el → 不重跑
    #   ⑤ metadata-only 导入          → 同 ④                   → 不重跑
    #   ⑥ 分块失败但仍置 extracted    → 无 chunk、终态、有 el   → 要重跑
    #      (source_ingestion 把分块包在 best-effort try 里,失败不阻塞流水线,所以
    #       「终态」并不能证明分块成功——这是 ④ 与 ⑥ 必须靠 elements 才分得开的原因)
    #
    # 刻意**不**把「有 element」直接算完成:那是管线中段产物,①⑥ 都有 element 却零 chunk。
    mnt = repo.maintenance
    done = (mnt.sources_with_chunks(notebook_id)
            | (mnt.sources_with_completed_parse(notebook_id)
               - mnt.sources_with_elements(notebook_id)))
    # 可能正被别的进程处理的源(服务端在跑、或另一个批处理)。process_source 没有
    # source 级单飞守卫且会 clear/replace 抽取态,两边同时跑会互相覆盖——离线 CLI
    # 不该碰服务端进行中的行,与崩溃兜底只在服务端跑是同一条原则。
    in_flight = repo.maintenance.sources_in_flight_parse(notebook_id)
    # `parsed` 是快照,看不见本次运行刚产出的 elements:输入目录里若有两个内容相同的
    # 文件,第二个会 hash 命中(第一个刚建的源)却不在快照里 → 被误判成需要 reparse,
    # 白跑一遍 parse。用「本次已认领的内容哈希」集合挡掉(线程安全,_one 在池里并发跑)。
    # 按 digest 而非 sid 认领:认领发生在做任何事之前,不必等 upload 返回 sid,天然没有
    # 「先插行、后写 elements」的窗口期竞态;digest → source 本就是一一映射。
    claimed: set[str] = set()
    claim_lock = threading.Lock()

    def _one(path: Path) -> tuple[str, Path, str | None]:
        try:
            content = path.read_bytes()
            digest = sha256_bytes(content)
            with claim_lock:
                if digest in claimed:
                    return ("skipped", path, None)   # 本次运行内的重复文件
                claimed.add(digest)
            sid = source_id_by_hash(repo, notebook_id, digest)
            if sid is None:
                repo.upload_sources(
                    notebook_id,
                    [UploadedSourceFile(file_name=path.name, content_type="", content=content)],
                    scheduler=None,
                )
                return ("uploaded", path, None)
            if sid in done:
                return ("skipped", path, None)
            if sid in in_flight:
                return ("in_flight", path, None)   # 别人在处理,不抢
            # 快照是进池前取的一次:一个源完全可能在取快照之后才被服务端/另一个批
            # 处理接手。重解析前就地复核一次(主键点查,只对重解析候选发生,不是每文件
            # 一次全表扫)。窗口从「整轮运行」缩到「这一次点查到 process_source 之间」。
            # 真正的原子认领需要 source 级租约/锁,当前管线没有——在有之前,复核是能
            # 做到的最强收敛,且与「不抢别人正在处理的源」这条意图一致。
            if source_id_in_flight(repo, sid):
                return ("in_flight", path, None)
            # 已建源但 parse 没跑完(上次中断/未落地)→ 重新 parse。调的是
            # upload_sources(scheduler=None) 内部同一个 process_source,不引入新的模型暴露。
            #
            # ⚠ process_source 对解析异常是**自己吞掉**的:它在内部 except 里把源置
            # 'failed' 然后照常 return SourceSummary(source_ingestion.py 的管线 except
            # 分支不 re-raise)。所以外面这个 try/except 对普通解析失败根本不可达——
            # 必须查返回值的真实状态,否则失败会被计成 reparsed,汇总数字说谎。
            summary = repo.process_source(sid)
            if (summary.parse_status or summary.status) == "failed":
                # SourceSummary 不暴露 error_message 列,失败详情在 summary 文案里
                # (管线 except 写的 "Parsing failed; see source error.")。
                return ("failed", path, summary.summary or "parse failed")
            return ("reparsed", path, None)
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


def backfill_chunk_embeddings(
    repo: BatchIngestRepository,
    notebook_id: str,
    conc: int = 4,
    missing_only: bool = False,
) -> int:
    """补该 notebook 的 chunk 向量(低并发)。EMBED 未配则跳过。

    - missing_only=False(默认):遍历每个 source 调 _embed_chunks_for_source 全量重嵌
      (INSERT OR REPLACE upsert),返回处理的 *source* 数。
    - missing_only=True:只补缺向量的 chunk(NOT EXISTS),一次 _embed_chunks_batch,
      返回补的 *chunk* 数(无缺失则跳过返回 0)。
    """
    if not repo.settings.embedder_configured:
        return 0
    mnt = repo.maintenance
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    try:
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
    finally:
        repo.settings.embed_concurrency = orig_conc


def backfill_element_embeddings(
    repo: BatchIngestRepository,
    notebook_id: str,
    conc: int = 4,
) -> int:
    """补该 notebook 缺失的 element 向量(只补缺失,幂等),返回落库行数。EMBED 未配则跳过。

    与 chunk/节点两侧并列的第三条补齐路径:此前 element 侧只有「整源重跑」一条路,
    写了一半的源(部分 element 有向量、部分没有)没有任何增量修法。
    element_embeddings 走 INSERT OR REPLACE upsert,只补缺失不会动同源已有的向量。"""
    if not repo.settings.embedder_configured:
        return 0
    mnt = repo.maintenance
    rows = mnt.missing_element_embedding_rows(notebook_id)
    if not rows:
        print("[embed] 无缺失 element 向量,跳过", flush=True)
        return 0
    print(f"[embed] 补缺失 element 向量:{len(rows)} 个", flush=True)
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    try:
        return mnt.embed_elements_batch(
            notebook_id,
            [
                {"element_id": r["id"], "source_id": r["source_id"], "text": r["text"]}
                for r in rows
            ],
        )
    finally:
        repo.settings.embed_concurrency = orig_conc


def run_kg(repo: BatchIngestRepository, notebook_id: str,
           limit: int | None = None, conc: int = 4, log: LogFn | None = None,
           no_rebuild: bool = False, rebuild_only: bool = False, fresh: bool = False,
           report_interval: int = 15) -> dict[str, int]:
    """Phase 2:对尚无 KG 的 source 抽取(per-source 融合关)→ 一次 rebuild_unified_kg → 补节点向量。

    source job 与 LLM window 并发上限由外层 _batch_concurrency_scope 统一安装；
    本函数只向已配置的 scheduler 提交 source job，不在 phase 内重配任一 pool。
    conc 仅供本函数直接调用的 embedding helper 使用。

    Flags:
      rebuild_only=True  — 跳过抽取,直接 rebuild_unified_kg + 节点向量(含 scale index)。
      no_rebuild=True    — 只抽取,跳过 rebuild_unified_kg 和 scale index(大批量分批抽,最后一批再 rebuild)。
      两者互斥;互斥时抛 ValueError。
      fresh=True         — 清空 rebuild checkpoint,强制 merge 审查/概念描述全量重跑(模型/阈值换了、
                            数据没变时用);隐含 force=True(否则清完 checkpoint 仍会被 force=False 的
                            门控挡回缓存簇数,--fresh 变假 no-op)。

    Scale-index 自动联:rebuild 后若 notebook 为 base tier 或已存在 scale index,则调
    repo.build_scale_index(notebook_id) 使索引与新簇同步。
    """
    from app.services.kg.scheduler import submit_job

    if no_rebuild and rebuild_only:
        raise ValueError("no_rebuild 和 rebuild_only 互斥,不能同时为 True")

    log = log or (lambda _e: None)
    mnt = repo.maintenance
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
                    kg_total = mnt.count_sources_missing_kg(notebook_id)
                    with _PoolReporter(report_interval, total=kg_total, log=log) as reporter:
                        def _kg_progress(
                            i: int, n: int, sid: str, ok: bool
                        ) -> None:
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
                all_sids = mnt.source_ids(notebook_id)
                kgful = mnt.kg_covered_source_ids(notebook_id)
                targets = [s for s in all_sids if s not in kgful][:max(0, limit)]
                n_targets = len(targets)

                def _extract_one(sid: str) -> tuple[str, Exception | None]:
                    try:
                        mnt.set_source_status(sid, "extracting")
                        mnt.run_extraction(sid)
                        mnt.set_source_status(sid, "extracted")
                        return sid, None
                    except Exception as exc:  # noqa: BLE001 — 单源失败隔离
                        return sid, exc

                with _PoolReporter(report_interval, total=n_targets, log=log) as reporter:
                    futures = {
                        submit_job(_extract_one, sid): sid for sid in targets
                    }
                    for i, future in enumerate(as_completed(futures), 1):
                        sid, error = future.result()
                        if error is None:
                            res["extracted"] += 1
                            log({
                                "phase": "kg",
                                "source_id": sid,
                                "status": "extracted",
                                "progress": f"{i}/{n_targets}",
                            })
                        else:
                            res["failed"] += 1
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
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

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
            files: Iterable[Path], conc: int = 4,
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

    并发上限由调用方的 batch controller 统一安装；本函数不重配 scheduler，
    以免只传 job_workers 时把独立的 LLM window 上限重置为 Settings 默认值。
    conc 仍用于传给本函数直接调用的 embedding helpers。

    fresh=True — 透传给末尾的 rebuild_unified_kg:清空 rebuild checkpoint 并强制
      merge 审查/概念描述全量重跑(隐含 force=True,理由同 run_kg)。
    """
    from app.services.kg.scheduler import submit_job

    log = log or (lambda _e: None)
    orig_auto = repo.settings.kg_auto_extract
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    orig_embed_conc = repo.settings.embed_concurrency
    repo.settings.kg_auto_extract = True                 # 强制 process_source 走 extract 分支
    repo.settings.kg_incremental_fusion_enabled = False  # 批量期关 per-source 融合,收尾一次 rebuild
    repo.settings.embed_concurrency = conc               # process_source 后台 chunk embed 并发
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
            # 门控(force=fresh):自动收尾重聚,除非显式 --fresh。无新增/变更且非 fresh 时
            # (如重跑同一批)输入版本未变 → 跳过整段重聚,直接返回缓存簇数(本次优化点);
            # fresh=True 必须同时 force=True,否则清完 checkpoint 仍被门控挡回缓存,
            # --fresh 变假 no-op(同 run_kg)。
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=fresh, fresh=fresh)
            res["clusters"] = clusters
            log({"phase": "all", "status": "rebuilt", "clusters": clusters})
            print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

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
        repo.settings.embed_concurrency = orig_embed_conc
    return res


def run_reparse(repo: BatchIngestRepository, notebook_id: str,
                limit: int | None = None, conc: int = 4,
                log: LogFn | None = None, report_interval: int = 15,
                no_rebuild: bool = False) -> dict[str, int]:
    """对 notebook 内无 source_elements(未成功 parse)的存量源重跑 process_source 补
    elements,收尾一次 rebuild_unified_kg。

    修复历史 run_all resume 分流的存量:一个已建、parse_status 看似前进却没有
    source_elements 的源(上次 parse 中断/未落地),旧 run_all 会当成「已 parse、缺 KG」
    直接 extract_source 空抽——build_records 的接地校验没有 element 可对照 → LLM 抽出的
    节点被整源丢弃 → objects=0,且直接重抽永远补不出 KG。必须重新 parse 生成 elements。
    跨源并发提交到 KG job 池。有 elements 的源自动跳过(幂等)。

    source job 与 LLM window 并发上限由外层 _batch_concurrency_scope 统一安装；
    本函数只提交 job。conc 仅供 embedding helper/兼容设置使用。"""
    from app.services.kg.scheduler import submit_job

    log = log or (lambda _e: None)
    orig_auto = repo.settings.kg_auto_extract
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    orig_embed_conc = repo.settings.embed_concurrency
    repo.settings.kg_auto_extract = True                 # process_source 走到 extract
    # 批量期关 per-source 增量融合:否则每源抽完都触发 incremental_fuse_source,要加载整个
    # notebook 的 cluster_map + 写 concept_clusters,巨型库(几万源)O(N²) 会卡死(16 路争
    # vector_cache/写锁)。收尾的 rebuild_unified_kg 会做一次全量融合(与 run_all/run_kg 一致)。
    repo.settings.kg_incremental_fusion_enabled = False
    repo.settings.embed_concurrency = conc

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
            res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)
        return res
    finally:
        repo.settings.kg_auto_extract = orig_auto
        repo.settings.kg_incremental_fusion_enabled = orig_fusion
        repo.settings.embed_concurrency = orig_embed_conc


def run_index(repo: BatchIngestRepository, notebook_id: str) -> dict[str, int]:
    """Phase 3 (offline): build the scalable-retrieval index for a (base) notebook.
    Static base KGs should re-run this after a rebuild."""
    manifest = repo.build_scale_index(notebook_id, on_stage=_index_stage_progress)
    return {"indexed_nodes": manifest.get("n_nodes", 0)}


def backfill_node_embeddings(
    repo: BatchIngestRepository, notebook_id: str, conc: int = 4
) -> int:
    """补 KG 节点向量(复用 backfill_knowledge_embeddings;关系向量默认跳过)。EMBED 未配则跳过。"""
    if not repo.settings.embedder_configured:
        return 0
    mnt = repo.maintenance
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    try:
        def _p(done: int, total: int) -> None:
            end = "\n" if done >= total else "\r"
            print(f"  节点向量: {done}/{total}", end=end, flush=True)
        count = mnt.backfill_node_embeddings(notebook_id, progress=_p)
    finally:
        repo.settings.embed_concurrency = orig_conc
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
    repo: BatchIngestRepository, notebook_id: str, conc: int = 4
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

    chunks_embedded = backfill_chunk_embeddings(repo, notebook_id, conc, missing_only=True)
    elements_embedded = backfill_element_embeddings(repo, notebook_id, conc)
    nodes_embedded = backfill_node_embeddings(repo, notebook_id, conc)

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
    rows: Sequence[sqlite3.Row],
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
        rows: Sequence[sqlite3.Row],
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
    if not (
        getattr(repo.kg_llm_client, "configured", False)
        or getattr(repo.llm_client, "configured", False)
    ):
        print("error: LLM 未配置 → 无法抽取论文元数据。配置 OPENAI_COMPAT_* 或 "
              "KG_LLM_* 后重试(CLI 不静默降级)。", file=sys.stderr)
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
        prog="batch_ingest", description="离线批量摄取目录 → 项目 KG/向量库")
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
    p.add_argument("--llm-conc", type=int, default=None,
                   help="传统 LLM 全局并发硬上限；省略时继承 KG_EXTRACT_WORKERS")
    p.add_argument("--embed-conc", type=int, default=None,
                   help="embedding 全局并发硬上限；省略时继承 EMBED_CONCURRENCY，"
                        "与源/文档级并发独立")
    p.add_argument("--limit", type=int, default=None,
                   help="kg 阶段只抽前 N 个未抽源 / reparse 阶段只处理前 N 个缺 elements 源"
                        "(仅限制本次数量;最终 rebuild 仍覆盖全本 notebook)")
    p.add_argument("--no-rebuild", action="store_true",
                   help="kg/reparse 阶段:只抽取/重解析,跳过收尾 rebuild_unified_kg 和 scale index。"
                        "大批量分批抽取用法:重复 'kg --limit N --no-rebuild',最后一次 'kg --rebuild-only'。")
    p.add_argument("--rebuild-only", action="store_true",
                   help="kg 阶段:跳过抽取,直接 rebuild_unified_kg + 节点向量 + scale index(base tier 时)。")
    p.add_argument("--fresh", action="store_true",
                   help="清空 rebuild checkpoint,强制 merge 审查/概念描述全量重跑"
                        "(kg 与 all 阶段的收尾 rebuild 均适用;用于只换了 KG 模型/阈值、"
                        "数据没变的场景)。")
    p.add_argument("--allow-no-embed", action="store_true",
                   help="EMBED 未配置时显式允许无向量降级(默认拒绝,防静默产出无向量库)")
    p.add_argument("--force", action="store_true",
                   help="metadata phase: 已有元数据行的源也重抽(prompt/校验升级后刷新)")
    p.add_argument("--pool-report-interval", type=int, default=15,
                   help="每 N 秒自报线程池占用(KG-LLM/源/embed);0 关闭。"
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
            "concurrency: "
            f"source={effective.workers}({effective.workers_source}) "
            f"llm={effective.llm}({effective.llm_source}) "
            f"embedding={effective.embedding}({effective.embedding_source})",
            flush=True,
        )
        with _batch_concurrency_scope(repo, effective):
            return run_metadata(repo, args)

    # embed 子命令就是补向量:EMBED 未配直接报错,忽略 --allow-no-embed。
    allow_no_embed = args.allow_no_embed and args.phase != "embed"
    if not repo.settings.embedder_configured:
        if not allow_no_embed:
            extra = (
                "\n  注意:embed 子命令用于补向量,--allow-no-embed 对它无效。"
                if args.phase == "embed"
                else ""
            )
            print(
                "error: EMBED 未就绪 → 不会产出向量(chunk/节点),检索将失效。\n"
                f"  当前 EMBED_PROVIDER={(repo.settings.embed_provider or '').strip()!r}"
                "(目前仅支持 'dashscope',大小写不敏感),且需 "
                "EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL 都配齐。\n"
                "  若确认 .env 已配:.env 按「当前工作目录」加载——请从含 .env "
                "的仓库根(主 checkout,不是 worktree)运行。\n"
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
        "llm": effective.llm,
        "embedding": effective.embedding,
        "workers_source": effective.workers_source,
        "llm_source": effective.llm_source,
        "embedding_source": effective.embedding_source,
    })
    print(
        "concurrency: "
        f"source={effective.workers}({effective.workers_source}) "
        f"llm={effective.llm}({effective.llm_source}) "
        f"embedding={effective.embedding}({effective.embedding_source})",
        flush=True,
    )

    if args.phase == "index":
        print(f"phase=index notebook={notebook_id}", flush=True)
        _t = time.perf_counter()
        r = run_index(repo, notebook_id)
        print(f"index done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)
        return 0

    with _batch_concurrency_scope(repo, effective):
        if args.phase == "all":
            print("phase=all (pipelined)", flush=True)
            _t = time.perf_counter()
            r = run_all(
                repo,
                notebook_id,
                iter_files(args.input_dir),
                conc=effective.embedding,
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
                conc=effective.embedding,
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
            print(
                f"phase=kg limit={args.limit} no_rebuild={no_rebuild} "
                f"rebuild_only={rebuild_only} fresh={fresh}",
                flush=True,
            )
            _t = time.perf_counter()
            r = run_kg(
                repo,
                notebook_id,
                limit=args.limit,
                conc=effective.embedding,
                log=log,
                no_rebuild=no_rebuild,
                rebuild_only=rebuild_only,
                fresh=fresh,
                report_interval=args.pool_report_interval,
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
                conc=effective.embedding,
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
            r = run_embed(repo, notebook_id, conc=effective.embedding)
            print(f"embed done: {r} ({time.perf_counter() - _t:.1f}s)", flush=True)

    return 0


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

    repo = SQLiteRepository(settings)
    batch_user = (
        _resolve_owner_profile(repo, args.owner)
        if args.owner is not None
        else repo.current_user()
    )
    user_token = set_request_user(batch_user)
    try:
        return _dispatch_main(args, repo, effective)
    finally:
        reset_request_user(user_token)
