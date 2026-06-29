# 离线批量摄取(Part A)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供一个离线脚本,给定目录递归读取 `.md`(及偶发 `.pdf`),复用项目现有管线分两阶段(先 chunk+embedding、后 KG)构建进项目 SQLite db。

**Architecture:** 逻辑放 `backend/app/services/batch_ingest.py`(可单测的纯函数 + `main()`),`scripts/batch_ingest.py` 仅薄 CLI 包装(对齐既有 `reextract.py` / `scripts/reextract_notebook.py` 模式)。复用 `upload_sources`/`process_source`/`build_notebook_kg`/`rebuild_unified_kg`/嵌入 backfill,**不改任何现有后端代码**——批量期通过运行时改 `repo.settings`(EMBED 置空、`kg_incremental_fusion_enabled=False`)控制行为。

**Tech Stack:** Python 3.13, FastAPI 项目的 SQLiteRepository, pytest, `argparse`, `concurrent.futures.ThreadPoolExecutor`。

> 本计划只覆盖 **Part A**(spec 的 §4)。Part B 来源分页(spec §5)在 A 落地后单独出计划。
> 本机解释器见本地约定;计划中的 `python` 即该解释器,所有命令需 `PYTHONPATH=backend`。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| Create `backend/app/services/batch_ingest.py` | 全部逻辑:文件发现、去重、notebook 解析、ingest 阶段、kg 阶段、嵌入 backfill、`main()` |
| Create `scripts/batch_ingest.py` | 薄 CLI:`from app.services.batch_ingest import main; raise SystemExit(main())` |
| Create `backend/tests/test_batch_ingest.py` | 单测(不触网络:用 `FakeEmbedder`;kg 阶段 mock) |
| Modify `README.md` / `README_zh.md` | 「离线批量摄取」CLI 用法小节(产品通用口径) |

**规范函数签名(全程一致):**
```python
SUPPORTED_EXTS = {".md", ".markdown", ".pdf"}
iter_files(root: Path, exts: Optional[set] = None) -> List[Path]
sha256_bytes(content: bytes) -> str
already_ingested(repo, notebook_id: str, digest: str) -> bool
ensure_notebook(repo, notebook_id: Optional[str], name: str) -> str
run_ingest(repo, notebook_id: str, files: List[Path], workers: int = 4, conc: int = 4, log=None) -> dict
backfill_chunk_embeddings(repo, notebook_id: str, conc: int = 4) -> int
run_kg(repo, notebook_id: str, limit: Optional[int] = None, conc: int = 4, log=None) -> dict
backfill_node_embeddings(repo, notebook_id: str, conc: int = 4) -> int
main(argv: Optional[List[str]] = None) -> int
```

---

## Task 1: 模块骨架 + 文件发现

**Files:**
- Create: `backend/app/services/batch_ingest.py`
- Test: `backend/tests/test_batch_ingest.py`

- [ ] **Step 1: 写失败测试(含测试文件头:imports + fixtures,后续 Task 追加)**

```python
# backend/tests/test_batch_ingest.py
import json
import pytest
from pathlib import Path

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.services import batch_ingest as bi


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo + FakeEmbedder(embedder_configured=True)。镜像 test_chunk_embed.py。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _make_md_dir(tmp_path, n=2):
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    for i in range(n):
        (d / f"doc{i}.md").write_text(
            f"# Title {i}\n\nBody paragraph {i} " + "x" * 200, encoding="utf-8")
    (d / "sub" / "nested.md").write_text("# Nested\n\nNested body " + "y" * 200, encoding="utf-8")
    (d / "ignore.txt").write_text("ignore me", encoding="utf-8")
    return d


def test_iter_files_filters_and_sorts(tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    files = bi.iter_files(d)
    names = [p.name for p in files]
    assert names == sorted(names)          # 稳定排序
    assert "ignore.txt" not in names       # 非支持扩展名排除
    assert "nested.md" in names            # 递归子目录
    assert len([n for n in names if n.endswith(".md")]) == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py::test_iter_files_filters_and_sorts -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.batch_ingest'`

- [ ] **Step 3: 创建模块骨架 + `iter_files` / `sha256_bytes`**

```python
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
from app.services.sqlite_repository import SQLiteRepository

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
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py::test_iter_files_filters_and_sorts -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(batch-ingest): 模块骨架 + 文件发现 iter_files"
```

---

## Task 2: notebook 解析 + 去重

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Test: `backend/tests/test_batch_ingest.py`(追加)

- [ ] **Step 1: 追加失败测试**

```python
def test_ensure_notebook_owner_defaults_user_local(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    with repo._connect() as db:
        row = db.execute("SELECT created_by FROM notebooks WHERE id=?", (nb_id,)).fetchone()
    assert nb_id.startswith("nb-")
    assert row["created_by"] == "user-local"   # 无请求上下文 → current_user 回退 user-local


def test_ensure_notebook_existing_id_passthrough(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    same = bi.ensure_notebook(repo, nb_id, "ignored-name")
    assert same == nb_id


def test_already_ingested_detects_hash(repo):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    assert bi.already_ingested(repo, nb_id, "deadbeef") is False
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k "ensure_notebook or already_ingested" -v`
Expected: FAIL — `AttributeError: module 'app.services.batch_ingest' has no attribute 'ensure_notebook'`

- [ ] **Step 3: 追加 `already_ingested` / `ensure_notebook`**

```python
def already_ingested(repo: SQLiteRepository, notebook_id: str, digest: str) -> bool:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND file_hash=?",
            (notebook_id, digest),
        ).fetchone()
    return row is not None


def ensure_notebook(repo: SQLiteRepository, notebook_id: Optional[str], name: str) -> str:
    """返回目标 notebook_id:给定则校验存在,否则新建(owner=current_user,默认 user-local)。"""
    if notebook_id:
        repo.get_notebook(notebook_id)   # 不存在则 KeyError
        return notebook_id
    return repo.create_notebook(NotebookCreate(name=name)).id
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k "ensure_notebook or already_ingested" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(batch-ingest): notebook 解析 + file_hash 去重"
```

---

## Task 3: ingest 阶段(parse+chunk,EMBED 延迟 backfill)

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Test: `backend/tests/test_batch_ingest.py`(追加)

- [ ] **Step 1: 追加失败测试**

```python
def test_run_ingest_creates_sources_chunks_embeddings_no_kg(repo, tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    counts = bi.run_ingest(repo, nb_id, bi.iter_files(d), workers=2, conc=2)
    assert counts["uploaded"] == 3 and counts["skipped"] == 0 and counts["failed"] == 0
    with repo._connect() as db:
        def c(sql, *a): return db.execute(sql, a).fetchone()["c"]
        nsrc = c("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", nb_id)
        nel = c("SELECT COUNT(*) c FROM source_elements", )
        nch = c("SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", nb_id)
        nemb = c("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", nb_id)
        nko = c("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", nb_id)
    assert nsrc == 3
    assert nel > 0 and nch > 0
    assert nemb == nch     # 每 chunk 一向量(收尾 backfill 生效)
    assert nko == 0        # Phase 1 不抽 KG(_should_extract_kg=False)


def test_run_ingest_dedup_skips_on_rerun(repo, tmp_path):
    d = _make_md_dir(tmp_path, n=2)
    nb_id = bi.ensure_notebook(repo, None, "nb")
    files = bi.iter_files(d)
    bi.run_ingest(repo, nb_id, files, workers=1, conc=2)
    counts2 = bi.run_ingest(repo, nb_id, files, workers=1, conc=2)
    assert counts2["uploaded"] == 0 and counts2["skipped"] == 3
    with repo._connect() as db:
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?", (nb_id,)).fetchone()["c"]
    assert nsrc == 3       # 未重复创建
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k run_ingest -v`
Expected: FAIL — `AttributeError: ... has no attribute 'run_ingest'`

- [ ] **Step 3: 追加 `run_ingest` / `backfill_chunk_embeddings`**

```python
def run_ingest(repo, notebook_id, files, workers=4, conc=4, log=None) -> dict:
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


def backfill_chunk_embeddings(repo, notebook_id, conc=4) -> int:
    """补该 notebook 所有 source 的 chunk 向量(低并发)。EMBED 未配则跳过。返回处理的 source 数。"""
    if not repo.settings.embedder_configured:
        return 0
    repo.settings.embed_concurrency = conc
    with repo._connect() as db:
        sids = [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
    done = 0
    for sid in sids:
        try:
            repo._embed_chunks_for_source(sid)
            done += 1
        except Exception:   # noqa: BLE001 — best-effort;429 留人工重跑
            pass
    return done
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k run_ingest -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(batch-ingest): ingest 阶段 parse+chunk + 延迟 chunk 向量 backfill"
```

---

## Task 4: kg 阶段(抽取 + 关停 per-source 融合 + 全量重建 + 节点向量)

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Test: `backend/tests/test_batch_ingest.py`(追加)

- [ ] **Step 1: 追加失败测试(mock 掉 LLM 相关入口,避免真实抽取)**

```python
def test_run_kg_disables_fusion_and_rebuilds(repo, monkeypatch):
    nb_id = bi.ensure_notebook(repo, None, "nb")
    calls = {}

    def fake_build(nb):
        calls["fusion_flag_during"] = repo.settings.kg_incremental_fusion_enabled
        calls["build_nb"] = nb
        return {"built": ["s1", "s2"], "failed": [], "skipped": []}

    def fake_rebuild(nb):
        calls["rebuild_nb"] = nb
        return 7

    monkeypatch.setattr(repo, "build_notebook_kg", fake_build)
    monkeypatch.setattr(repo, "rebuild_unified_kg", fake_rebuild)
    res = bi.run_kg(repo, nb_id, limit=None, conc=2)
    assert res["extracted"] == 2 and res["failed"] == 0
    assert res["clusters"] == 7
    assert calls["fusion_flag_during"] is False    # 抽取期 per-source 融合已关
    assert calls["build_nb"] == nb_id and calls["rebuild_nb"] == nb_id
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py::test_run_kg_disables_fusion_and_rebuilds -v`
Expected: FAIL — `AttributeError: ... has no attribute 'run_kg'`

- [ ] **Step 3: 追加 `run_kg` / `backfill_node_embeddings`**

```python
def run_kg(repo, notebook_id, limit=None, conc=4, log=None) -> dict:
    """Phase 2:对尚无 KG 的 source 抽取(per-source 融合关)→ 一次 rebuild_unified_kg → 补节点向量。"""
    log = log or (lambda _e: None)
    repo.settings.kg_incremental_fusion_enabled = False   # 批量期关 per-source 融合,收尾一次全量
    res = {"extracted": 0, "failed": 0, "clusters": 0, "nodes_embedded": 0}

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
    return res


def backfill_node_embeddings(repo, notebook_id, conc=4) -> int:
    """补 KG 节点向量(复用 _backfill_knowledge_embeddings;关系向量默认跳过)。EMBED 未配则跳过。"""
    if not repo.settings.embedder_configured:
        return 0
    repo.settings.embed_concurrency = conc
    with repo._connect() as db:
        objects = [
            {"id": r["id"], "payload": json.loads(r["payload"] or "{}")}
            for r in db.execute(
                "SELECT id, payload FROM knowledge_objects "
                "WHERE notebook_id=? AND status!='deprecated'", (notebook_id,)).fetchall()
        ]
        repo._backfill_knowledge_embeddings(db, notebook_id, objects)
    return len(objects)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py::test_run_kg_disables_fusion_and_rebuilds -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(batch-ingest): kg 阶段抽取 + 关 per-source 融合 + 收尾全量重建/节点向量"
```

---

## Task 5: CLI(argparse + manifest + main)

**Files:**
- Modify: `backend/app/services/batch_ingest.py`
- Test: `backend/tests/test_batch_ingest.py`(追加)

- [ ] **Step 1: 追加失败测试**

```python
def test_main_dry_run_lists_files(repo, tmp_path, capsys):
    d = _make_md_dir(tmp_path, n=2)   # repo fixture 已把 env 指向 tmp DB
    rc = bi.main(["ingest", "--input-dir", str(d), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry-run" in out and "3 files" in out


def test_main_requires_input_dir_for_ingest(repo, capsys):
    rc = bi.main(["ingest"])
    assert rc == 2
    assert "input-dir" in capsys.readouterr().err


def test_main_all_ingests_then_runs_kg(repo, tmp_path, monkeypatch):
    d = _make_md_dir(tmp_path, n=2)
    monkeypatch.setenv("EMBED_PROVIDER", "")   # main 自建 repo;关 EMBED 避免触网
    monkeypatch.setattr(SQLiteRepository, "build_notebook_kg",
                        lambda self, nb: {"built": [], "failed": [], "skipped": []})
    monkeypatch.setattr(SQLiteRepository, "rebuild_unified_kg", lambda self, nb: 0)
    rc = bi.main(["all", "--input-dir", str(d), "--notebook-name", "X", "--workers", "1"])
    assert rc == 0
    r2 = SQLiteRepository(Settings())
    with r2._connect() as db:
        row = db.execute("SELECT id FROM notebooks WHERE name='X'").fetchone()
        assert row is not None
        nsrc = db.execute("SELECT COUNT(*) c FROM sources WHERE notebook_id=?",
                          (row["id"],)).fetchone()["c"]
    assert nsrc == 3
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k main -v`
Expected: FAIL — `AttributeError: ... has no attribute 'main'`

- [ ] **Step 3: 追加 `_make_logger` / `build_arg_parser` / `main`**

```python
def _make_logger(manifest_path: Optional[Path]) -> LogFn:
    if manifest_path is None:
        return lambda _e: None
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fh = manifest_path.open("a", encoding="utf-8")

    def _log(entry: dict) -> None:
        entry = dict(entry, ts=time.time())
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
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
    notebook_id = ensure_notebook(repo, args.notebook_id, nb_name)
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
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -k main -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(batch-ingest): CLI(ingest/kg/all + dry-run + manifest)"
```

---

## Task 6: 薄 CLI 脚本

**Files:**
- Create: `scripts/batch_ingest.py`

- [ ] **Step 1: 创建脚本**

```python
#!/usr/bin/env python3
"""离线批量摄取 CLI(薄包装)。逻辑见 app.services.batch_ingest。

用法:
  PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir DIR
  PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxx [--limit 50]
  PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir DIR --notebook-name NAME
"""
from app.services.batch_ingest import main

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 手动冒烟(--help 不需 DB)**

Run: `PYTHONPATH=backend python scripts/batch_ingest.py --help`
Expected: 打印 usage,含 `ingest`/`kg`/`all` 与各 `--opt`,退出码 0。

- [ ] **Step 3: 提交**

```bash
git add scripts/batch_ingest.py
git commit -m "feat(batch-ingest): 薄 CLI 脚本 scripts/batch_ingest.py"
```

---

## Task 7: README 文档(中英)

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`

- [ ] **Step 1: 在 `README_zh.md` 增加「离线批量摄取」小节**

在合适位置(如脚本/运维相关章节后)插入:

````markdown
### 离线批量摄取(目录 → KG)

把一个目录里的 Markdown(及偶发 PDF)离线复用现有管线灌进库。分两阶段:
先 `ingest`(无 LLM、快,chunk 问答即可用),再 `kg`(LLM 抽取,单独可恢复)。

```bash
# 1) 解析+分块+向量(无 LLM):新建 notebook,名取目录名
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir

# 2) 先小范围验证 KG 质量(只抽前 50 个未抽源)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) 整批抽 KG(幂等,跳过已抽;失败可重跑续抽)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# 或一条命令跑完(ingest 然后 kg)
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "我的库"
```

选项:`--workers`(文件并发)、`--embed-conc`(嵌入并发,避 429)、`--limit`(kg 子集验证)、`--dry-run`(只扫描预估)。

前置:`.env` 配好 EMBED 与 KG_LLM(否则向量/KG 步骤会跳过或报错);重复文件按内容哈希自动跳过;进度写 `<storage>/batch_ingest/<notebook>.jsonl`,中断后重跑自动续。
````

- [ ] **Step 2: 在 `README.md` 增加等价英文小节**

````markdown
### Offline batch ingestion (directory → KG)

Ingest a directory of Markdown (and the occasional PDF) through the existing
pipeline, in two phases: `ingest` (no LLM, fast — chunk Q&A works immediately),
then `kg` (LLM extraction, separately resumable).

```bash
# 1) parse + chunk + embeddings (no LLM); creates a notebook named after the dir
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir

# 2) validate KG quality on a subset first (extract only the first 50 un-extracted sources)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) extract KG for the whole notebook (idempotent; skips already-extracted; resumable)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# or run both phases in one command
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "My KB"
```

Options: `--workers` (file concurrency), `--embed-conc` (embedding concurrency, throttles 429s), `--limit` (kg subset), `--dry-run` (scan & estimate only).

Prereqs: configure EMBED and KG_LLM in `.env` (otherwise embedding/KG steps skip or error). Duplicate files are skipped by content hash; progress is written to `<storage>/batch_ingest/<notebook>.jsonl` and a re-run resumes automatically.
````

- [ ] **Step 3: 校验未引入中文弯引号问题 / 无机器特定路径**

Run: `git diff README.md README_zh.md | grep -nE "/opt/homebrew|miniconda|这台|:8000" || echo OK`
Expected: `OK`(无机器特定细节,遵守 committed-docs-stay-generic)

- [ ] **Step 4: 提交**

```bash
git add README.md README_zh.md
git commit -m "docs(batch-ingest): README 中英增加离线批量摄取 CLI 用法"
```

---

## Task 8: 全量验证

- [ ] **Step 1: 跑本特性全部单测**

Run: `PYTHONPATH=backend python -m pytest backend/tests/test_batch_ingest.py -v`
Expected: 全部 PASS(约 9 个用例)

- [ ] **Step 2: 跑项目检查脚本(py_compile + smoke + 前端 lint)**

Run: `scripts/check.sh`
Expected: 退出码 0,绿。

- [ ] **Step 3: 回归——确认未碰其它后端代码(应只新增文件 + README)**

Run: `git diff --stat origin/master -- backend/app | grep -v batch_ingest || echo "no other backend changes"`
Expected: `no other backend changes`(Part A 不改现有后端代码)

- [ ] **Step 4: 收尾提 PR(按 memory dev-flow-finish-with-pr / pr-merge-is-rebase)**

```bash
git fetch origin
git rebase origin/master          # 保持线性
git push -u origin HEAD
gh pr create --base master --title "feat: 离线批量摄取脚本(Part A)" \
  --body "目录 → notebook → chunk(+embed) → KG,两阶段复用现有管线。spec: docs/superpowers/specs/2026-06-27-offline-batch-ingest-and-sources-pagination-design.md"
```

---

## Self-Review(计划 vs spec §4)

- **§4.2 CLI**:`ingest/kg/all` + `--input-dir/--notebook-id/--notebook-name/--workers/--limit/--dry-run` → Task 5/6 全覆盖。(`--group-by-subdir`、`--owner`、`--embed-conc` 中:`--embed-conc` 已实现;**`--group-by-subdir` 与 `--owner` 本计划暂缓**——见下「范围裁剪」。)
- **§4.3 Phase 1**(EMBED 关→backfill、KG 不触发、去重、有界并发、失败隔离)→ Task 3 + Task 2。
- **§4.4 Phase 2**(build_notebook_kg / --limit 子集 / 关 per-source 融合 / 一次 rebuild / 节点向量、关系向量跳过)→ Task 4。
- **§4.5 去重/幂等/可恢复**(file_hash + manifest + build_notebook_kg 幂等跳过)→ Task 2/3/4/5。
- **§4.6 并发/限流**(workers / embed-conc 低并发)→ Task 3/4/5。
- **§4.7 失败处理/可观测**(逐文件 try/except + manifest + 汇总打印)→ Task 3/4/5。
- **§4.9 文档**(README 中英)→ Task 7。
- **Placeholder scan**:无 TODO/TBD;每步含完整代码与确切命令。
- **Type consistency**:`run_ingest`/`run_kg` 返回 dict 键、`build_notebook_kg` 返回 `{"built","failed","skipped"}`、`rebuild_unified_kg`→int、`_backfill_knowledge_embeddings(db,nb,objects)` 均与现有代码一致。

### 范围裁剪(YAGNI,记录决策)
- **`--group-by-subdir` 与 `--owner` 暂缓**:首版默认全进 1 个 notebook、owner=user-local(离线 admin 场景足够)。两者都是小增量,确有需要时各加 1 个 Task(`--owner` = 解析用户并 `set` `_REQUEST_USER` ContextVar 后再 `ensure_notebook`;`--group-by-subdir` = 对每个一级子目录循环 `ensure_notebook`+`run_ingest`)。若评审认为首版就要,告知我补上。
- **element 向量**:Phase 1 只补 chunk 向量(chunk-native 主路径);`_embed_source` 的 element 向量是否被检索消费留待实现期确认(spec §4.3),默认不补。
