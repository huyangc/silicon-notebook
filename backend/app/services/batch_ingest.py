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
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.repository import UploadedSourceFile
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)

SUPPORTED_EXTS = {".md", ".markdown", ".pdf"}

LogFn = Callable[[dict], None]


def _rebuild_progress(phase: str, i: int, n: int) -> None:
    """CLI progress printer for rebuild_unified_kg sub-phases (e.g. concept_desc:
    LLM description gen). Overwrites in place until the last item, then newline."""
    end = "\n" if i >= n else "\r"
    print(f"  {phase}: {i}/{n}", end=end, flush=True)


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
           no_rebuild: bool = False, rebuild_only: bool = False) -> dict:
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
        if not rebuild_only:
            llm_ok = (repo.settings.kg_llm_configured
                      or getattr(repo.llm_client, "configured", False))
            if limit is None:
                # no_rebuild=True 且无 LLM 时:跳过抽取(无法抽取,等 rebuild_only 阶段再合并)
                if llm_ok or not no_rebuild:
                    out = repo.build_notebook_kg(  # 跨源并发抽取,逐源打印进度
                        notebook_id,
                        progress=lambda i, n, sid, ok: print(
                            f"[kg {i}/{n}] {sid} {'✓' if ok else '✗ 失败'}", flush=True),
                    )
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

        # ── no_rebuild:抽取后直接返回,不做 rebuild/scale-index ───────────────
        if no_rebuild:
            return res

        # ── Rebuild 阶段 ──────────────────────────────────────────────────────
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress)
        res["clusters"] = clusters
        log({"phase": "kg", "status": "rebuilt", "clusters": clusters})
        print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
        res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

        # ── Scale-index 自动联(base tier 或已有索引) ─────────────────────────
        nb = repo.get_notebook(notebook_id)
        is_base = (nb.tier == "base")
        has_index = (repo._scale_index(notebook_id) is not None)
        if is_base or has_index:
            manifest = repo.build_scale_index(notebook_id)
            scale_nodes = manifest.get("n_nodes", 0)
            res["scale_index_nodes"] = scale_nodes
            log({"phase": "kg", "status": "scale_index_built", "nodes": scale_nodes})
            print(f"scale index built (nodes={scale_nodes})", flush=True)

    finally:
        repo.settings.kg_incremental_fusion_enabled = orig_fusion   # 还原,避免污染 repo 实例
    return res


def run_all(repo: SQLiteRepository, notebook_id, files, workers=4, conc=4, log=None) -> dict:
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

        # ── 等待全部 job,逐个打印进度、统计 ─────────────────────────────────────
        total = len(futs)
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

        # ── 末尾一次:跨文档聚类 → 补节点向量 →(base/已建索引)scale index ────────
        print("rebuild: 跨文档聚类中(概念多时较慢,无输出≠卡死)…", flush=True)
        clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress)
        res["clusters"] = clusters
        log({"phase": "all", "status": "rebuilt", "clusters": clusters})
        print(f"rebuild done: clusters={clusters};补 KG 节点向量…", flush=True)
        res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)

        nb = repo.get_notebook(notebook_id)
        if nb.tier == "base" or repo._scale_index(notebook_id) is not None:
            manifest = repo.build_scale_index(notebook_id)
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
    manifest = repo.build_scale_index(notebook_id)
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
    p.add_argument("phase", choices=["ingest", "kg", "index", "all", "embed"])
    p.add_argument("--input-dir", type=Path, help="递归扫描的根目录(ingest/all 必填)")
    p.add_argument("--notebook-id", default=None, help="目标 notebook;省略则新建")
    p.add_argument("--notebook-name", default=None,
                   help="新建 notebook 名(ingest/all 新建库时必填;不再默认用目录名)")
    p.add_argument("--owner", default=None,
                   help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    p.add_argument("--workers", type=int, default=4,
                   help="all 阶段同时抽取的文档数(覆盖 KG_JOB_CONCURRENCY);"
                        "其余阶段为文件级并发。默认 4")
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
    p.add_argument("--dry-run", action="store_true", help="只扫描+报告,不写库")
    return p


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
                    workers=args.workers, conc=args.embed_conc, log=log)
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
                   no_rebuild=no_rebuild, rebuild_only=rebuild_only)
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
