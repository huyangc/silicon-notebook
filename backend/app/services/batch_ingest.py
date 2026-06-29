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
from concurrent.futures import ThreadPoolExecutor
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

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for status, path, err in pool.map(_one, files):
                counts[status] += 1
                log({"phase": "ingest", "path": str(path), "status": status, "error": err})
    finally:
        repo.settings.embed_provider = orig_provider   # 恢复,供 backfill 使用

    counts["sources_embedded"] = backfill_chunk_embeddings(repo, notebook_id, conc)
    return counts


def backfill_chunk_embeddings(repo: SQLiteRepository, notebook_id, conc=4) -> int:
    """补该 notebook 所有 source 的 chunk 向量(低并发)。EMBED 未配则跳过。返回处理的 source 数。"""
    if not repo.settings.embedder_configured:
        return 0
    orig_conc = repo.settings.embed_concurrency
    repo.settings.embed_concurrency = conc
    done = 0
    try:
        with repo._connect() as db:
            sids = [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
        for sid in sids:
            try:
                repo._embed_chunks_for_source(sid)
                done += 1
            except Exception:   # noqa: BLE001 — best-effort;429 留人工重跑
                pass
    finally:
        repo.settings.embed_concurrency = orig_conc
    return done


def run_kg(repo: SQLiteRepository, notebook_id, limit=None, conc=4, log=None) -> dict:
    """Phase 2:对尚无 KG 的 source 抽取(per-source 融合关)→ 一次 rebuild_unified_kg → 补节点向量。"""
    log = log or (lambda _e: None)
    orig_fusion = repo.settings.kg_incremental_fusion_enabled
    repo.settings.kg_incremental_fusion_enabled = False   # 批量期关 per-source 融合,收尾一次全量
    res = {"extracted": 0, "failed": 0, "clusters": 0, "nodes_embedded": 0}
    try:
        if limit is None:
            out = repo.build_notebook_kg(notebook_id)   # 只抽尚无 KG 的 source,幂等,失败隔离
            res["extracted"] = len(out["built"])
            res["failed"] = len(out["failed"])
        else:
            with repo._connect() as db:
                all_sids = [r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
                kgful = {r["source_id"] for r in db.execute(
                    "SELECT DISTINCT source_id FROM knowledge_objects "
                    "WHERE notebook_id=? AND source_id!=''", (notebook_id,)).fetchall()}
            targets = [s for s in all_sids if s not in kgful][:max(0, limit)]
            for sid in targets:
                try:
                    repo._set_source_status(sid, "extracting")
                    repo._run_extraction(sid)
                    repo._set_source_status(sid, "extracted")
                    res["extracted"] += 1
                except Exception as exc:   # noqa: BLE001 — 单源失败隔离
                    res["failed"] += 1
                    log({"phase": "kg", "source_id": sid, "status": "failed", "error": str(exc)})

        res["clusters"] = repo.rebuild_unified_kg(notebook_id)
        res["nodes_embedded"] = backfill_node_embeddings(repo, notebook_id, conc)
    finally:
        repo.settings.kg_incremental_fusion_enabled = orig_fusion   # 还原,避免污染 repo 实例
    return res


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
    p.add_argument("phase", choices=["ingest", "kg", "all"])
    p.add_argument("--input-dir", type=Path, help="递归扫描的根目录(ingest/all 必填)")
    p.add_argument("--notebook-id", default=None, help="目标 notebook;省略则新建")
    p.add_argument("--notebook-name", default=None, help="新建 notebook 名(默认取目录名)")
    p.add_argument("--owner", default=None,
                   help="notebook 属主用户名(大小写不敏感);默认= admin 用户")
    p.add_argument("--workers", type=int, default=4, help="文件级并发(默认 4)")
    p.add_argument("--embed-conc", type=int, default=4, help="嵌入 backfill 并发(默认 4,避 429)")
    p.add_argument("--limit", type=int, default=None, help="kg 阶段只抽前 N 个未抽源(子集验证)")
    p.add_argument("--dry-run", action="store_true", help="只扫描+报告,不写库")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.phase in {"ingest", "all"} and not args.input_dir:
        print("error: --input-dir required for ingest/all", file=sys.stderr)
        return 2

    if args.dry_run:
        files = iter_files(args.input_dir) if args.input_dir else []
        print(f"[dry-run] {len(files)} files under {args.input_dir}", flush=True)
        for p in files[:20]:
            print(f"  {p}", flush=True)
        if len(files) > 20:
            print(f"  ... (+{len(files) - 20} more)", flush=True)
        return 0

    repo = SQLiteRepository(Settings())
    nb_name = args.notebook_name or (args.input_dir.name if args.input_dir else "Batch Import")
    notebook_id = ensure_notebook(repo, args.notebook_id, nb_name, owner=args.owner)
    manifest = Path(repo.storage_dir) / "batch_ingest" / f"{notebook_id}.jsonl"
    log = _make_logger(manifest)
    print(f"notebook={notebook_id} manifest={manifest}", flush=True)

    if args.phase in {"ingest", "all"}:
        files = iter_files(args.input_dir)
        print(f"phase=ingest files={len(files)}", flush=True)
        c = run_ingest(repo, notebook_id, files, workers=args.workers, conc=args.embed_conc, log=log)
        print(f"ingest done: {c}", flush=True)

    if args.phase in {"kg", "all"}:
        print(f"phase=kg limit={args.limit}", flush=True)
        r = run_kg(repo, notebook_id, limit=args.limit, conc=args.embed_conc, log=log)
        print(f"kg done: {r}", flush=True)

    return 0
