# Knowhow 表 / Memory 跨 Notebook 复制与移动 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户把单张 knowhow 表、单/多条 confirmed memory 从一个 notebook 复制或移动到另一个已存在的 notebook。

**Architecture:** 后端两条独立链路。knowhow 走「新建 `KnowhowTransferStore`（单表快照 + 单事务插入 + 提交前校验）+ `services/knowhow/transfer.py` 编排（id 重映射复用整本拷贝的稳定-id 派生物方案 = K-1 零重嵌入 + 磁盘资产随迁 + 调度重投影）」，路由直接调编排函数（沿用 knowhow_api 的「routes→模块函数」惯例）。memory 走「`MemoryStore.create_copy_with_initial_revision`（单事务建 4 表 + 拷向量）+ `MemoryService.transfer`（复用 create/embedding/KG 调度）+ facade 一跳委托」。移动 = 复制成功并校验后删源。前端两个入口（knowhow 表工具栏、memory 单条+批量）共用一个目标笔记本选择器 modal。

**Tech Stack:** Python 3 / FastAPI / SQLite（`app/repositories/sqlite`）；pytest；Next.js/React/TypeScript（strict）；`node --test` + `node:assert/strict`。

## Global Constraints

- 无 schema 迁移、不 bump SCHEMA_VERSION、不需重启（全部复用现有表；import 信息进现有 `memory_provenance.payload_json`）。
- 所有原始 SQL 只能在 `backend/app/repositories/sqlite/` 下（`test_repository_callers_static.py` 硬约束）。
- facade 方法必须是单跳委托（无 SQL、无分支、单组件），否则 `test_repository_facade_contract.py` 的 `facade_body_violations` 失败。
- `facade_surface.json` 冻结在 `SOURCE_COMMIT="3334626"`，**新增 facade 成员不重生成它**，改为在 `backend/tests/test_repository_surface_manifest.py` 新增 `*_ALLOWED_NEW_MEMBERS` 集合并接入 pop 循环（`:3407-3422`）。**绝不 `--rebaseline`。**
- 新增端点后跑 `python scripts/generate_repository_contract_fixtures.py`（**repo 根的 scripts/，不带 --rebaseline**）重生成 `backend/tests/fixtures/repository_contract/api_contract.json`（活契约，无 baseline 守卫）。
- knowhow **无** `ports.py` Protocol（新增 knowhow 方法不改 ports.py）；memory 有 `MemoryRepository` + `MemoryStorePort`（新增 memory 方法要在这两个 Protocol 加 stub）。
- 新增 facade 委托方法 append 到 `SQLiteRepository` 类**尾部**；新增 `deps.py`/`knowhow/api.py` 内容 append 到**文件尾**——避免打断 `test_repository_callers_static.py` / `test_repository_surface_manifest.py` 的按行号 pin（`INDEPENDENT_PRIVATE_SITES` 等）。
- memory 传输：源与目标都必须是当前用户 owner（`user_can_access_notebook`）。knowhow 复制：源 `user_can_read_notebook`（owner∪reader）、目标 `user_can_access_notebook`；knowhow 移动：源、目标都 `user_can_access_notebook`。目标 == 源 → 400。
- 前端共享类型放 `workspace-model.ts`，不放 `page.tsx`（`architecture-boundaries.test.mjs` 约束）；新测试文件保持顶层 `app/*.test.mjs`；只测纯 helper 不测网络函数。tsc/typecheck = `npm run lint`。

后端测试命令统一在 `backend/` 下用项目解释器跑（示例用 `pytest`；worktree 无 .env，必要时从主 checkout 根跑，见 CLAUDE 记忆）。

---

## Phase A — Knowhow 表传输（后端）

Phase A 独立可交付：完成后 knowhow 表的复制/移动端点即可用。

### Task A1: `KnowhowTransferStore` — 单表快照 + 单事务插入 + 校验

**Files:**
- Create: `backend/app/repositories/sqlite/knowhow_transfer_store.py`
- Modify: `backend/app/services/repository_runtime.py`（append 一行构造 `self.knowhow_transfer_store`，放在既有 `self.knowhow_store = KnowhowStore(...)` 之后）
- Test: `backend/tests/test_knowhow_transfer_store.py`

**Interfaces:**
- Produces:
  - `class KnowhowTransferStore(database, *, new_id: Callable[[str],str], now: Callable[[],str])`
  - `snapshot_table(self, table_id: str) -> dict` — 返回 `{"table": dict, "columns": [dict], "rows": [dict], "cells": [dict], "cell_code": [dict], "elements": [dict], "chunks": [dict], "chunk_embeddings": [dict]}`；每个都是 `dict(sqlite3.Row)`（SELECT *）。`elements/chunks/chunk_embeddings` 仅当 `table.hidden_source_id` 非空时非空。raises `KeyError(table_id)` 若表不存在。
  - `insert_transfer(self, payload: dict, expected_counts: dict) -> None` — 单个 `database.write()` 事务内按 FK 序插入 `payload` 各表（键：`table`(单 dict)、`columns`、`rows`、`cells`、`cell_code`、`assets`、`source`(单 dict 或 None)、`elements`、`chunks`、`chunk_embeddings`），提交前把落库后的 columns/rows/cells/cell_code 计数与 `expected_counts`（源表快照的行数，由 A2 传入）比对，不一致 raise `RuntimeError`（回滚）。
- Consumes: `repo._runtime.database`、`repo._runtime.seams.new_id`、`repo._runtime.seams.now`（Task A2 构造时传入）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_knowhow_transfer_store.py
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def store(repo):
    rt = repo._runtime
    return KnowhowTransferStore(rt.database, new_id=rt.seams.new_id, now=rt.seams.now)

def _table(repo) -> str:
    nb = repo.create_notebook(NotebookCreate(name="KH", purpose="p", primary_domain="d")).id
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def test_snapshot_returns_business_rows(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    assert snap["table"]["id"] == tid
    assert {c["name"] for c in snap["columns"]} == {"违例类型", "现象识别"}
    assert len(snap["rows"]) == 1
    assert len(snap["cells"]) == 2
    # 未投影表没有派生产物
    assert snap["elements"] == [] and snap["chunks"] == []

def test_snapshot_missing_table_raises(store):
    with pytest.raises(KeyError):
        store.snapshot_table("khtbl-nope")

def test_insert_transfer_rejects_count_mismatch(repo, store):
    tid = _table(repo)
    snap = store.snapshot_table(tid)
    # 只插一个全新表行、零 cell，但 expected_counts 声称 cells=1 → 校验必须拒绝并回滚
    payload = {
        "table": {**snap["table"], "id": "khtbl-x"},
        "columns": [], "rows": [], "cells": [], "cell_code": [],
        "assets": [], "source": None, "elements": [], "chunks": [], "chunk_embeddings": [],
    }
    with pytest.raises(RuntimeError):
        store.insert_transfer(payload, {"columns": 0, "rows": 0, "cells": 1, "cell_code": 0})
    # 回滚生效：khtbl-x 未落库
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowhow_tables WHERE id='khtbl-x'").fetchone()[0] == 0
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_knowhow_transfer_store.py -x -q`
Expected: FAIL（`ModuleNotFoundError: knowhow_transfer_store`）

- [ ] **Step 3: 实现 store**

```python
# backend/app/repositories/sqlite/knowhow_transfer_store.py
"""单张 knowhow 表的跨 notebook 传输 SQL：快照 + 单事务插入 + 提交前校验。

镜像 SharingStore 对整本拷贝所做的事，但收窄到一张表 + 它隐藏源的派生产物。
所有 SQL 收在这里（callers_static 约束：原始 SQL 只在 repositories/sqlite 下）。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from app.repositories.sqlite.database import SqliteDatabase

# 插入 FK 顺序：表→列/行→资产→格/代码→隐藏源→元素→chunk→向量
_BUSINESS_ORDER = ("columns", "rows", "assets", "cells", "cell_code")
_DERIVED_ORDER = ("elements", "chunks", "chunk_embeddings")


def _insert_rows(db: sqlite3.Connection, table: str, rows: list) -> None:
    for row in rows:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )


# 逻辑表名 → 真实表名（键在 payload/校验里用逻辑名）
_TABLE_NAMES = {
    "columns": "knowhow_columns",
    "rows": "knowhow_rows",
    "assets": "notebook_assets",
    "cells": "knowhow_cells",
    "cell_code": "knowhow_cell_code",
    "elements": "source_elements",
    "chunks": "chunks",
    "chunk_embeddings": "chunk_embeddings",
}


class KnowhowTransferStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    def snapshot_table(self, table_id: str) -> dict:
        with self.database.connect() as db:
            table = db.execute(
                "SELECT * FROM knowhow_tables WHERE id = ?", (table_id,)
            ).fetchone()
            if table is None:
                raise KeyError(table_id)
            table = dict(table)

            def rows_for(sql: str, params: tuple) -> list:
                return [dict(r) for r in db.execute(sql, params).fetchall()]

            columns = rows_for(
                "SELECT * FROM knowhow_columns WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            rows = rows_for(
                "SELECT * FROM knowhow_rows WHERE table_id = ? ORDER BY position, id",
                (table_id,),
            )
            cells = rows_for(
                "SELECT c.* FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )
            cell_code = rows_for(
                "SELECT cc.* FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id "
                "WHERE r.table_id = ?",
                (table_id,),
            )

            hidden = table.get("hidden_source_id")
            elements: list = []
            chunks: list = []
            chunk_embeddings: list = []
            source = None
            if hidden:
                source_row = db.execute(
                    "SELECT * FROM sources WHERE id = ?", (hidden,)
                ).fetchone()
                source = dict(source_row) if source_row is not None else None
                if source is not None:
                    elements = rows_for(
                        "SELECT * FROM source_elements WHERE source_id = ?", (hidden,)
                    )
                    chunks = rows_for(
                        "SELECT * FROM chunks WHERE source_id = ?", (hidden,)
                    )
                    chunk_embeddings = rows_for(
                        "SELECT ce.* FROM chunk_embeddings ce "
                        "JOIN chunks c ON c.id = ce.chunk_id WHERE c.source_id = ?",
                        (hidden,),
                    )
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "cell_code": cell_code,
            "source": source,
            "elements": elements,
            "chunks": chunks,
            "chunk_embeddings": chunk_embeddings,
        }

    def insert_transfer(self, payload: dict, expected_counts: dict) -> None:
        table = payload["table"]
        new_table_id = table["id"]
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            _insert_rows(db, "knowhow_tables", [table])
            for key in _BUSINESS_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])
            if payload.get("source"):
                _insert_rows(db, "sources", [payload["source"]])
            for key in _DERIVED_ORDER:
                _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])

            # 提交前校验：落库计数须等于源表快照计数（不一致 → 抛错 → 回滚，不留半份副本）
            def count(sql: str) -> int:
                return int(db.execute(sql, (new_table_id,)).fetchone()[0])

            checks = {
                "columns": count("SELECT COUNT(*) FROM knowhow_columns WHERE table_id=?"),
                "rows": count("SELECT COUNT(*) FROM knowhow_rows WHERE table_id=?"),
                "cells": count(
                    "SELECT COUNT(*) FROM knowhow_cells c JOIN knowhow_rows r "
                    "ON r.id=c.row_id WHERE r.table_id=?"
                ),
                "cell_code": count(
                    "SELECT COUNT(*) FROM knowhow_cell_code cc JOIN knowhow_rows r "
                    "ON r.id=cc.row_id WHERE r.table_id=?"
                ),
            }
            expected = {k: int(expected_counts.get(k, 0)) for k in checks}
            if checks != expected:
                raise RuntimeError(f"knowhow transfer 校验失败：{checks} != {expected}")
```

Also append the wiring line in `repository_runtime.py` right after the `self.knowhow_store = KnowhowStore(...)` construction:

```python
        # knowhow 单表跨 notebook 传输的 SQL（快照+单事务插入+校验）
        from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
        self.knowhow_transfer_store = KnowhowTransferStore(
            self.database, new_id=self.seams.new_id, now=self.seams.now
        )
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_knowhow_transfer_store.py -x -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_transfer_store.py backend/app/services/repository_runtime.py backend/tests/test_knowhow_transfer_store.py
git commit -m "feat(knowhow): KnowhowTransferStore 单表快照+单事务插入+校验"
```

---

### Task A2: `transfer.py` — `copy_table` 编排（K-1 稳定-id 重映射 + 资产随迁 + 重投影）

**Files:**
- Create: `backend/app/services/knowhow/transfer.py`
- Test: `backend/tests/test_knowhow_transfer_service.py`

**Interfaces:**
- Consumes: `KnowhowTransferStore`（A1，经 `repo._runtime.knowhow_transfer_store`）；`element_id`/`cell_chunk_id`（`app.services.knowhow.projection`）；`_rewrite_asset_refs`/`_ASSET_REF_RE`/`ALLOWED_MIME_EXTENSIONS`（`app.services.notebook_sharing` / `app.services.knowhow.assets`）；`get_scheduler`（`app.services.knowhow.api`）；facade `repo.get_notebook_asset`。
- Produces:
  - `copy_table(repo, source_table_id: str, target_notebook_id: str, actor_id: str) -> str` — 返回新 `table_id`。单事务插入 + 事务后落资产磁盘 + 调度重投影。
  - `_remap(repo, snapshot, target_notebook_id, actor_id) -> tuple[dict, list[tuple[str,str,str,str]]]` — 纯计算，返回 `(payload, asset_files)`，`asset_files=[(old_asset_id, new_asset_id, mime, src_notebook_id)]`。（导出以便单测。）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_knowhow_transfer_service.py
import time
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.services.knowhow import transfer as kh_transfer

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

class _FakeEmbedder:
    dim = 3
    def __init__(self):
        self.call_count = 0
    def embed_texts(self, texts):
        self.call_count += len(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    r = SQLiteRepository(Settings())
    r.embedder = _FakeEmbedder()
    return r

def _nb(repo, name="KH"):
    return repo.create_notebook(NotebookCreate(name=name, purpose="p", primary_domain="d")).id

def _table_with_row(repo, nb):
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def _settle(repo, tid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = repo.get_knowhow_table(tid)
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return repo.get_knowhow_table(tid)

def _project(repo, tid):
    # store 层的 add_knowhow_row 不自动调度投影（那是路由/api 层的事）——测试里显式调度。
    from app.services.knowhow.api import get_scheduler
    get_scheduler(repo).schedule(tid)
    return _settle(repo, tid)

def test_copy_creates_independent_table_in_target(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)  # 不投影：业务表拷贝与投影无关

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert new_tid != src_tid
    dst = repo.get_knowhow_table(new_tid)
    assert dst["notebook_id"] == dst_nb
    assert dst["created_by"] == "user-x"
    assert {c["name"] for c in dst["columns"]} == {"违例类型", "现象识别"}
    assert len(dst["rows"]) == 1
    # 源不受影响
    assert repo.get_knowhow_table(src_tid)["notebook_id"] == src_nb

def test_copy_reprojection_reuses_vectors_zero_reembed(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)  # 先把源投影好，产出 chunks + chunk_embeddings
    repo.embedder.call_count = 0  # 归零，之后只观察 copy 引发的 embed

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")
    _settle(repo, new_tid)  # 等重投影落地（copy_table 自己已调度）

    # K-1：chunk_embeddings 已随拷贝以稳定 id 落库 → 重投影零重嵌入
    assert repo.embedder.call_count == 0
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_knowhow_transfer_service.py -x -q`
Expected: FAIL（`AttributeError: module ... has no attribute 'copy_table'`）

- [ ] **Step 3: 实现 `transfer.py`**

```python
# backend/app/services/knowhow/transfer.py
"""单张 knowhow 表跨 notebook 的复制/移动编排。

复用整本拷贝（notebook_sharing.copy_notebook）验证过的 K-1 稳定-id 派生物方案：
source_elements 用 element_id(new_row,new_col) 重算、chunks 用 cell_chunk_id 重算、
chunk_embeddings 随 chunk id 原样搬 → 拷完调度 project_table，text/section_path 未变
→ 零重嵌入。业务表 id 全新映射；cells 的 asset:// 引用改写；资产磁盘文件随迁。

routes 直接调本模块函数（沿用 knowhow_api 的「routes→模块函数(repo)」惯例）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.services.knowhow.assets import ALLOWED_MIME_EXTENSIONS
from app.services.knowhow.api import get_scheduler
from app.services.knowhow.projection import cell_chunk_id, element_id
from app.services.notebook_sharing import _ASSET_REF_RE, _rewrite_asset_refs


def _remap(
    repo: Any, snapshot: dict, target_notebook_id: str, actor_id: str
) -> tuple[dict, list]:
    seams = repo._runtime.seams
    new = seams.new_id
    now = seams.now()

    src_table = snapshot["table"]
    src_notebook_id = src_table["notebook_id"]

    khtbl_map = {src_table["id"]: new("khtbl")}
    khcol_map: dict[str, str] = {}
    khrow_map: dict[str, str] = {}
    asset_map: dict[str, str] = {}
    source_map: dict[str, str] = {}

    # 隐藏源（可能不存在：源表从未投影）
    source_out = None
    if snapshot["source"]:
        src_source = dict(snapshot["source"])
        new_source_id = new("src")
        source_map[src_source["id"]] = new_source_id
        src_source["id"] = new_source_id
        src_source["notebook_id"] = target_notebook_id
        src_source["memory_id"] = ""  # 防御：knowhow 隐藏源无 memory 关联
        source_out = src_source

    table_out = dict(src_table)
    table_out["id"] = khtbl_map[src_table["id"]]
    table_out["notebook_id"] = target_notebook_id
    table_out["created_by"] = actor_id
    table_out["created_at"] = now
    table_out["updated_at"] = now
    old_hidden = src_table.get("hidden_source_id")
    table_out["hidden_source_id"] = source_map.get(old_hidden) if old_hidden else None

    columns_out = []
    for col in snapshot["columns"]:
        col = dict(col)
        new_col = new("khcol")
        khcol_map[col["id"]] = new_col
        col["id"] = new_col
        col["table_id"] = khtbl_map[col["table_id"]]
        columns_out.append(col)

    rows_out = []
    for row in snapshot["rows"]:
        row = dict(row)
        new_row = new("khrow")
        khrow_map[row["id"]] = new_row
        row["id"] = new_row
        row["table_id"] = khtbl_map[row["table_id"]]
        row["projection_status"] = "pending"
        rows_out.append(row)

    # 收集本表 cells 引用到的资产（仅这些随迁）
    referenced: set[str] = set()
    for cell in snapshot["cells"]:
        for match in _ASSET_REF_RE.findall(cell["content_md"] or ""):
            referenced.add(match)
    asset_files = []
    assets_out = []
    for old_asset_id in sorted(referenced):
        asset = repo.get_notebook_asset(old_asset_id)
        if asset is None:
            continue
        new_asset_id = new("asset")
        asset_map[old_asset_id] = new_asset_id
        asset_files.append((old_asset_id, new_asset_id, asset["mime"], src_notebook_id))
        row = dict(asset)
        row["id"] = new_asset_id
        row["notebook_id"] = target_notebook_id
        assets_out.append(row)

    cells_out = []
    for cell in snapshot["cells"]:
        cell = dict(cell)
        cell["id"] = new("khcel")
        cell["row_id"] = khrow_map[cell["row_id"]]
        cell["column_id"] = khcol_map[cell["column_id"]]
        cell["content_md"] = _rewrite_asset_refs(cell.get("content_md") or "", asset_map)
        cells_out.append(cell)

    cell_code_out = []
    for code in snapshot["cell_code"]:
        code = dict(code)
        code["id"] = new("khcode")
        code["row_id"] = khrow_map[code["row_id"]]
        code["column_id"] = khcol_map[code["column_id"]]
        cell_code_out.append(code)

    # 派生产物：稳定 id 重算（零重嵌入的关键）
    element_map: dict[str, str] = {}
    element_row_new: dict[str, str] = {}
    elements_out = []
    for el in snapshot["elements"]:
        el = dict(el)
        old_id = el["id"]
        metadata = json.loads(el.get("metadata") or "{}")
        kh_meta = dict(metadata.get("knowhow") or {})
        new_row = khrow_map[kh_meta["row_id"]]
        new_col = khcol_map[kh_meta["column_id"]]
        new_el = element_id(new_row, new_col)
        element_map[old_id] = new_el
        element_row_new[old_id] = new_row
        kh_meta["table_id"] = khtbl_map.get(kh_meta.get("table_id"), kh_meta.get("table_id"))
        kh_meta["row_id"] = new_row
        kh_meta["column_id"] = new_col
        metadata["knowhow"] = kh_meta
        el["metadata"] = json.dumps(metadata, ensure_ascii=False)
        el["id"] = new_el
        el["source_id"] = source_map[el["source_id"]]
        elements_out.append(el)

    chunk_map: dict[str, str] = {}
    chunks_out = []
    for chunk in snapshot["chunks"]:
        chunk = dict(chunk)
        old_chunk_id = chunk["id"]
        old_element_ids = json.loads(chunk.get("element_ids") or "[]")
        if not old_element_ids:
            raise ValueError(f"knowhow chunk {old_chunk_id} 缺 element_ids，无法重算稳定 id")
        new_row = element_row_new[old_element_ids[0]]
        part = int(old_chunk_id.rsplit("-", 1)[-1])
        new_chunk_id = cell_chunk_id(new_row, part)
        chunk_map[old_chunk_id] = new_chunk_id
        chunk["id"] = new_chunk_id
        chunk["notebook_id"] = target_notebook_id
        chunk["source_id"] = source_map[chunk["source_id"]]
        chunk["element_ids"] = json.dumps(
            [element_map.get(e, e) for e in old_element_ids], ensure_ascii=False
        )
        chunks_out.append(chunk)

    vectors_out = []
    for vec in snapshot["chunk_embeddings"]:
        vec = dict(vec)
        vec["chunk_id"] = chunk_map[vec["chunk_id"]]
        vec["notebook_id"] = target_notebook_id
        vectors_out.append(vec)

    payload = {
        "table": table_out,
        "columns": columns_out,
        "rows": rows_out,
        "cells": cells_out,
        "cell_code": cell_code_out,
        "assets": assets_out,
        "source": source_out,
        "elements": elements_out,
        "chunks": chunks_out,
        "chunk_embeddings": vectors_out,
    }
    return payload, asset_files


def copy_table(
    repo: Any, source_table_id: str, target_notebook_id: str, actor_id: str
) -> str:
    store = repo._runtime.knowhow_transfer_store
    snapshot = store.snapshot_table(source_table_id)
    payload, asset_files = _remap(repo, snapshot, target_notebook_id, actor_id)
    expected_counts = {
        "columns": len(snapshot["columns"]),
        "rows": len(snapshot["rows"]),
        "cells": len(snapshot["cells"]),
        "cell_code": len(snapshot["cell_code"]),
    }
    store.insert_transfer(payload, expected_counts)  # 单事务 + 提交前校验

    # 事务提交后落资产磁盘文件（单文件失败跳过，不回滚已提交 DB）
    if asset_files:
        assets_root = Path(repo.storage_dir) / "assets"
        dest_dir = assets_root / target_notebook_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        for old_id, new_id, mime, src_nb in asset_files:
            ext = ALLOWED_MIME_EXTENSIONS.get(mime, "bin")
            src = assets_root / src_nb / f"{old_id}.{ext}"
            if src.is_file():
                shutil.copy2(src, dest_dir / f"{new_id}.{ext}")

    new_table_id = payload["table"]["id"]
    get_scheduler(repo).schedule(new_table_id)  # 后台重建 KG objects/relations
    return new_table_id
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_knowhow_transfer_service.py -x -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/knowhow/transfer.py backend/tests/test_knowhow_transfer_service.py
git commit -m "feat(knowhow): copy_table 编排（K-1 稳定id派生物+资产随迁+重投影）"
```

---

### Task A3: `transfer.py` — `move_table`（复制成功后删源）

**Files:**
- Modify: `backend/app/services/knowhow/transfer.py`（append `move_table` + `transfer_table` dispatch）
- Test: `backend/tests/test_knowhow_transfer_service.py`（append）

**Interfaces:**
- Consumes: `copy_table`（A2）；facade `repo.delete_knowhow_table`；`build_projector(repo).delete_table_projection(hidden_source_id)`（`app.services.knowhow.api.build_projector`）。
- Produces:
  - `move_table(repo, source_table_id, target_notebook_id, actor_id) -> str`
  - `transfer_table(repo, source_table_id, target_notebook_id, actor_id, mode: str) -> str` — `mode in {"copy","move"}`，否则 `ValueError`。

- [ ] **Step 1: 写失败测试**

```python
def test_move_deletes_source_table_and_projection(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    # 必须 _project（不是裸 _settle）：add_knowhow_row 不自动调度投影，裸 _settle 会让
    # hidden_source_id 为 None，下面的「投影已拆」断言就变成 source_id=NULL 恒不匹配的空转。
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未投影，删投影断言将空转"
    with repo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
    assert before > 0, "源表投影未产出 objects，删投影断言将空转"

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    # 目标有，新表可读
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    # 源表已删
    import pytest as _pytest
    with _pytest.raises(KeyError):
        repo.get_knowhow_table(src_tid)
    # 源隐藏源的投影已拆（objects 清空）
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
    assert remaining == 0

def test_transfer_table_rejects_bad_mode(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    import pytest as _pytest
    with _pytest.raises(ValueError):
        kh_transfer.transfer_table(repo, src_tid, dst_nb, "user-x", mode="teleport")
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_knowhow_transfer_service.py -k "move or bad_mode" -q`
Expected: FAIL（`AttributeError: move_table`）

- [ ] **Step 3: 实现（append 到 transfer.py 尾）**

```python
def move_table(
    repo: Any, source_table_id: str, target_notebook_id: str, actor_id: str
) -> str:
    # 先复制并校验通过，再删源：删源失败也绝不丢数据。
    # 删源内部顺序＝先拆投影、后删表行（反过来若拆投影失败，源的 chunks/chunks_fts/
    # chunk_embeddings/KO 会永久不可回收地留在源笔记本继续被检索到——拆投影的唯一
    # 调用方需要一个已被删掉的 knowhow_tables 行，而 hidden_source_id 只存在于该行）。
    hidden = repo.get_knowhow_table(source_table_id).get("hidden_source_id")
    new_table_id = copy_table(repo, source_table_id, target_notebook_id, actor_id)
    if hidden:
        build_projector(repo).delete_table_projection(hidden)
    repo.delete_knowhow_table(source_table_id)
    return new_table_id


def transfer_table(
    repo: Any,
    source_table_id: str,
    target_notebook_id: str,
    actor_id: str,
    mode: str,
) -> str:
    if mode == "copy":
        return copy_table(repo, source_table_id, target_notebook_id, actor_id)
    if mode == "move":
        return move_table(repo, source_table_id, target_notebook_id, actor_id)
    raise ValueError(f"unknown transfer mode: {mode}")
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_knowhow_transfer_service.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/knowhow/transfer.py backend/tests/test_knowhow_transfer_service.py
git commit -m "feat(knowhow): move_table = 复制成功后删源+拆源投影"
```

---

### Task A4: REST 端点 `POST /notebooks/{notebook_id}/knowhow/{table_id}/transfer`

**Files:**
- Modify: `backend/app/models/schemas.py`（append `KnowhowTransferRequest`）
- Modify: `backend/app/api/routes.py`（append 路由到文件尾）
- Test: `backend/tests/test_knowhow_transfer_routes.py`

**Interfaces:**
- Consumes: `transfer_table`（A3）；`notebook_access_repository()`（`app.api.deps`，= `repository()._runtime.sharing`，已在 allowlist）；`get_current_user`；`repository()`。
- Produces: `POST /notebooks/{notebook_id}/knowhow/{table_id}/transfer`，body `KnowhowTransferRequest{target_notebook_id: str, mode: Literal["copy","move"]}`，返回 `{"new_table_id": str}`。守卫：源 copy→read/move→write、目标 write、目标≠源、表属于源 nb。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_knowhow_transfer_routes.py
# 认证沿用 tests/test_notebook_share_readonly.py 的 _login 样板：真实注册+登录拿 Bearer，
# repo fixture 与 app 共享同一 tmp DB（autouse conftest 清 repository() lru_cache）。
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

COLUMNS = [{"name": "违例类型", "role": "anchor"}, {"name": "现象识别", "role": "procedure"}]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def client(repo):
    from app.main import app
    return TestClient(app)

def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}

def _table(repo, nb):
    tid = repo.create_knowhow_table(nb, "T", "d", COLUMNS, created_by="")
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲"})
    return tid

def test_copy_endpoint_creates_table_in_target(client, repo):
    h = _login(client, "a00000001")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    new_tid = resp.json()["new_table_id"]
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst

def test_transfer_to_same_notebook_rejected(client, repo):
    h = _login(client, "a00000002")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": src, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_knowhow_transfer_routes.py -x -q`
Expected: FAIL（404 route not found）

- [ ] **Step 3a: schema（append 到 schemas.py knowhow 区块附近的尾部）**

```python
class KnowhowTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_notebook_id: str
    mode: Literal["copy", "move"]
```

（`Literal` 已由 `from typing import ...` 引入；若无则在文件顶部 import 处补 `Literal`。）

- [ ] **Step 3b: 路由（append 到 routes.py 文件尾）**

```python
# --- knowhow 表跨 notebook 传输（复制/移动） -------------------------------
# 追加在文件尾：mode 决定源守卫（copy=read / move=write），无法用静态
# Depends(require_notebook_*)，故在处理器内手动核权（复用 deps 的访问仓库）。
# 用同步 def（同 create_report/create_object_schema）——FastAPI 自动放线程池跑，
# 无需 run_in_threadpool、无需在文件顶部加 import（避免打断行号 pin）。
from app.api.deps import notebook_access_repository as _kh_access  # noqa: E402
from app.models.schemas import KnowhowTransferRequest  # noqa: E402
from app.services.knowhow import transfer as _kh_transfer  # noqa: E402


@router.post("/notebooks/{notebook_id}/knowhow/{table_id}/transfer")
def transfer_knowhow_table(
    notebook_id: str,
    table_id: str,
    payload: KnowhowTransferRequest,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    if payload.target_notebook_id == notebook_id:
        raise HTTPException(status_code=400, detail="源与目标不能是同一个 notebook")
    repo = repository()
    access = _kh_access()
    source_check = (
        access.user_can_access_notebook
        if payload.mode == "move"
        else access.user_can_read_notebook
    )
    if not source_check(notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not access.user_can_access_notebook(payload.target_notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        table = repo.get_knowhow_table(table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    if table["notebook_id"] != notebook_id:
        raise HTTPException(status_code=404, detail="Table not found")
    try:
        new_table_id = _kh_transfer.transfer_table(
            repo, table_id, payload.target_notebook_id, user.id, payload.mode
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    return {"new_table_id": new_table_id}
```

（确认 `UserProfile`、`Depends`、`get_current_user`、`repository`、`HTTPException` 均已在 routes.py 顶部 import——它们是既有路由都在用的符号，无需新增顶部 import。）

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_knowhow_transfer_routes.py -x -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_knowhow_transfer_routes.py
git commit -m "feat(knowhow): 表传输 REST 端点（按 mode 核源/目标权限）"
```

---

### Task A5: knowhow 架构守卫 + 契约 fixture

**Files:**
- Modify: `backend/app/repositories/ownership_manifest.py`（`SurfaceMember` for `snapshot_table`/`insert_transfer`? — 见下；实际只需登记 facade **可见**新成员）
- Modify: `backend/tests/test_repository_surface_manifest.py`（新增 `KNOWHOW_TRANSFER_ALLOWED_NEW_MEMBERS` 并接入 pop 循环）
- Modify: `backend/tests/test_repository_callers_static.py`（若 `transfer.py` 新增 `repo._runtime` 私有访问站点 → 加 `INDEPENDENT_PRIVATE_SITES`）
- Regenerate: `backend/tests/fixtures/repository_contract/api_contract.json`

**Interfaces:**
- Consumes: A1–A4 的新符号。
- Produces: 全部架构守卫 + 契约测试通过。

> **判定说明（重要）**：knowhow 传输**没有**新增 `SQLiteRepository` facade 方法（路由直接调 `transfer.py` 模块函数，store 经 `repo._runtime.knowhow_transfer_store`）。因此 `test_repository_surface_manifest.py` 的静态 `repo.<member>` 扫描**不会**新增 facade 成员——除非某处写了 `repo.<新方法>`。本方案没有，所以大概率**无需** `*_ALLOWED_NEW_MEMBERS`。真正会触发的是：`transfer.py` 里 `repo._runtime.*`（新私有访问站点）和新端点导致的 `api_contract.json` 漂移。按下面步骤逐一验证并只改真正报错的守卫。

- [ ] **Step 1: 先跑守卫，看哪些真的红**

Run:
```bash
cd backend && pytest tests/test_repository_callers_static.py tests/test_repository_surface_manifest.py tests/test_repository_api_contract.py -q
```
Expected: 观察失败项。预期：
- `test_repository_api_contract.py::test_openapi_contract_is_byte_semantically_frozen` FAIL（新端点改了 openapi）。
- 可能 `test_repository_callers_static.py` 的 `INDEPENDENT_PRIVATE_SITES` FAIL（`transfer.py` 出现 `repo._runtime`）。

- [ ] **Step 2: 修 callers_static（仅当 private-site 报错）**

在 `backend/tests/test_repository_callers_static.py` 的 `INDEPENDENT_PRIVATE_SITES`（`:249` 起）按报错精确 `(file, line, attribute)` 追加条目，注释说明为何在产品仓库边界外可接受。示例格式（对齐既有条目）：
```python
    # knowhow 表传输编排：与 knowhow/api.py 同类，读 _runtime 取传输 store/seams。
    ("backend/app/services/knowhow/transfer.py", <行号>, "_runtime"),
```
按测试报出的**精确行号**填。`transfer.py` 内出现多处 `repo._runtime.*` 时，若守卫按 `(file, attr)` 去重则一条即可；若按行号 pin 则每处一条——以测试实际报出的期望为准。

- [ ] **Step 3: 重生成 api_contract.json**

Run:
```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/local-file-management-design-7c7a39
python scripts/generate_repository_contract_fixtures.py
```
Expected: 打印重生成的活契约；`git diff --stat` 只显示 `backend/tests/fixtures/repository_contract/api_contract.json` 变化（含新端点）。**若还改动了 facade_surface.json 或 baseline.db，撤销那些改动**（`git checkout -- <path>`）——它们是冻结产物，本任务不应动。

- [ ] **Step 4: 若确有 `repo.<新facade方法>` 被静态扫到（预期没有）**

只有当 Step 1 报出 surface manifest 新成员时才做：在 `test_repository_surface_manifest.py` 定义
```python
KNOWHOW_TRANSFER_ALLOWED_NEW_MEMBERS = {"<member>"}  # 说明：<why>
```
并加入 `:3407-3422` 的 pop 循环并集。否则跳过本步。

- [ ] **Step 5: 全量守卫 + knowhow 测试绿**

Run:
```bash
cd backend && pytest tests/test_repository_callers_static.py tests/test_repository_surface_manifest.py tests/test_repository_api_contract.py tests/test_repository_facade_contract.py tests/test_architecture_module_boundaries.py tests/test_knowhow_transfer_store.py tests/test_knowhow_transfer_service.py tests/test_knowhow_transfer_routes.py -q
```
Expected: PASS（全绿）

- [ ] **Step 6: 提交**

```bash
git add backend/tests/test_repository_callers_static.py backend/tests/fixtures/repository_contract/api_contract.json backend/tests/test_repository_surface_manifest.py
git commit -m "test(knowhow): 表传输过架构守卫+重生成 api 契约"
```

---

## Phase B — Memory 传输（后端）

Phase B 独立可交付。

### Task B1: `MemoryStore.create_copy_with_initial_revision`（单事务建 4 表 + 拷向量）

**Files:**
- Modify: `backend/app/repositories/sqlite/memory_store.py`（append 方法）
- Modify: `backend/app/repositories/ports.py`（`MemoryStorePort` 加 stub）
- Test: `backend/tests/test_memory_transfer_store.py`

**Interfaces:**
- Consumes: 既有 `_insert_memory_on`、`_ensure_initial_revision_on`（同文件私有）；`encode_vector`（已 import）。
- Produces: `create_copy_with_initial_revision(self, write: MemoryWrite, source_memory_id: str, changed_by: str, reason: str) -> MemoryRecord` — 单 `database.write()`+`BEGIN IMMEDIATE`：插入新 memory_items+provenance（`_insert_memory_on`）+ 初始 revision（`_ensure_initial_revision_on`）；再从 `memory_embeddings` 读 `source_memory_id` 的向量，存在则以新 `write.id` `INSERT OR REPLACE` 并把新 item `embedding_status='ready'`。返回新 `MemoryRecord`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_memory_transfer_store.py
import pytest
from types import SimpleNamespace
from app.core.config import Settings
from app.models.memory import MemoryWrite
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def alice(repo):
    return repo.create_user("a00123456", "pw")

def _nb(repo, user, name):
    tok = set_request_user(user)
    try:
        return repo.create_notebook(NotebookCreate(name=name)).id
    finally:
        reset_request_user(tok)

def test_create_copy_builds_four_tables_and_copies_vector(repo, alice):
    store = repo._runtime.memory_store
    src_nb, dst_nb = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    now = repo._runtime.seams.now()
    # 源用 create_candidate_with_initial_revision 建（会写 revision 1，replace_embedding 才认）
    src_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=src_nb,
        created_by=alice.id, origin="external_agent", status="candidate",
        title="T", content_md="B", tags=["x"], created_at=now, updated_at=now,
        provenance={"client_request_id": "r1"},
    )
    source = store.create_candidate_with_initial_revision(src_write, alice.id, "created")
    # 给源塞一条向量（revision 1 已存在）
    assert store.replace_embedding(source.id, 1, "TestModel", [0.1, 0.2, 0.3]) is True

    copy_write = MemoryWrite(
        id=repo._runtime.seams.new_id("memory"), notebook_id=dst_nb,
        created_by=alice.id, source_answer_id=None, origin="external_agent",
        status="confirmed", title="T", content_md="B", tags=["x"],
        created_at=now, updated_at=now, confirmed_by=alice.id, confirmed_at=now,
        provenance={"imported_from": {"notebook_id": src_nb, "memory_id": source.id, "action": "copy"}},
    )
    copied = store.create_copy_with_initial_revision(copy_write, source.id, alice.id, "copied")

    assert copied.id == copy_write.id
    assert copied.notebook_id == dst_nb
    assert copied.provenance["imported_from"]["memory_id"] == source.id
    assert copied.embedding_status == "ready"
    with repo._connect() as db:
        vec = db.execute(
            "SELECT dimension FROM memory_embeddings WHERE memory_id=?", (copied.id,)
        ).fetchone()
        rev = db.execute(
            "SELECT COUNT(*) FROM memory_revisions WHERE memory_id=?", (copied.id,)
        ).fetchone()[0]
    assert vec["dimension"] == 3
    assert rev == 1
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_memory_transfer_store.py -x -q`
Expected: FAIL（`AttributeError: create_copy_with_initial_revision`）

- [ ] **Step 3: 实现（append 到 memory_store.py 尾部，class 内）**

```python
    def create_copy_with_initial_revision(
        self,
        write: "MemoryWrite",
        source_memory_id: str,
        changed_by: str,
        reason: str,
    ) -> MemoryRecord:
        """把一条已有 memory 复制成 write（新 id/notebook）：单事务建 4 表 + 拷向量。

        source_answer_id 必须为 None（避免 idx_memory_answer_once 撞键）；向量随拷
        零重嵌入，源无向量则新 item 保持 embedding_status='pending'（服务侧补嵌）。
        """
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")
            item, created = self._insert_memory_on(db, write)
            if item.id != write.id:
                return item  # 幂等命中已有行（正常不会发生：copy 用全新 id）
            self._ensure_initial_revision_on(db, item, created, changed_by, reason)
            vec = db.execute(
                "SELECT model, dimension, vector FROM memory_embeddings WHERE memory_id=?",
                (source_memory_id,),
            ).fetchone()
            if vec is not None:
                db.execute(
                    "INSERT OR REPLACE INTO memory_embeddings "
                    "(memory_id,model,dimension,vector,updated_at) VALUES (?,?,?,?,?)",
                    (write.id, vec["model"], vec["dimension"], vec["vector"], self.now()),
                )
                db.execute(
                    "UPDATE memory_items SET embedding_status='ready',embedding_error='' "
                    "WHERE id=?",
                    (write.id,),
                )
                item = self._record(
                    db.execute(
                        f"SELECT {self._select_columns()} FROM memory_items m "
                        "LEFT JOIN memory_provenance p ON p.memory_id=m.id "
                        "WHERE m.id=? AND m.created_by=?",
                        (write.id, write.created_by),
                    ).fetchone()
                )
        return item
```

Add the stub to `MemoryStorePort` in `ports.py` (near the other memory store methods):
```python
    def create_copy_with_initial_revision(
        self, write: Any, source_memory_id: str, changed_by: str, reason: str
    ) -> MemoryRecord: ...
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_memory_transfer_store.py -x -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/memory_store.py backend/app/repositories/ports.py backend/tests/test_memory_transfer_store.py
git commit -m "feat(memory): create_copy_with_initial_revision 单事务建4表+拷向量"
```

---

### Task B2: `MemoryService.transfer`（复制/移动 + facade 委托）

**Files:**
- Modify: `backend/app/services/memory_service.py`（append `transfer`）
- Modify: `backend/app/repositories/ports.py`（`MemoryRepository` 加 stub）
- Modify: `backend/app/services/sqlite_repository.py`（append facade 委托 `transfer_memories`）
- Test: `backend/tests/test_memory_transfer_service.py`

**Interfaces:**
- Consumes: `create_copy_with_initial_revision`（B1）；既有 `self.store.memory_for_user`、`self._maybe_schedule_kg`、`self._schedule_embed`、`self.memory_kg.remove_memory_source`、`self.store.delete_memory`、`self.notebooks.user_can_access_notebook`、`self.now`、`self.new_id`、`self._event`；`MemoryWrite`。
- Produces:
  - `MemoryService.transfer(self, user_id: str, memory_ids: list[str], target_notebook_id: str, mode: str, extract_kg: bool = True) -> list[dict]` — 每条返回 `{"source_id","new_id","ok","error"}`。仅 `confirmed` 可传输；`source_answer_id=None`；`provenance` 注入 `imported_from`；`move` 复制成功后 `remove_memory_source(src)`+`delete_memory(src)`。目标须当前用户 owner，否则 `PermissionError`。
  - facade `SQLiteRepository.transfer_memories(self, user_id, memory_ids, target_notebook_id, mode, extract_kg=True) -> list[dict]`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_memory_transfer_service.py
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def alice(repo):
    return repo.create_user("a00123456", "pw")

@pytest.fixture
def bob(repo):
    return repo.create_user("b00654321", "pw")

def _nb(repo, user, name):
    tok = set_request_user(user)
    try:
        return repo.create_notebook(NotebookCreate(name=name)).id
    finally:
        reset_request_user(tok)

def _confirmed_memory(service, nb, user, title="T", content="B"):
    # 走 agent candidate → confirm，拿到一条 confirmed memory（不需 answer 夹具）
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None  # 关掉 KG 后台，测试聚焦复制
    cand = service.create_candidate(
        nb, user.id, None, f"req-{title}", title, content, [], "task"
    )
    return service.confirm(cand.id, user.id)

def test_copy_memory_into_target(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    results = repo.transfer_memories(alice.id, [mem.id], dst, "copy", extract_kg=False)
    assert len(results) == 1 and results[0]["ok"] is True
    new_id = results[0]["new_id"]
    copied = service.get(new_id, alice.id)
    assert copied.notebook_id == dst
    assert copied.source_answer_id is None
    assert copied.provenance["imported_from"]["memory_id"] == mem.id
    # 源仍在
    assert service.get(mem.id, alice.id).notebook_id == src

def test_move_memory_deletes_source(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    mem = _confirmed_memory(service, src, alice)
    results = repo.transfer_memories(alice.id, [mem.id], dst, "move", extract_kg=False)
    assert results[0]["ok"] is True
    with pytest.raises(KeyError):
        service.get(mem.id, alice.id)

def test_transfer_to_notebook_not_owned_rejected(repo, alice, bob):
    service = repo._runtime.memory_service
    src = _nb(repo, alice, "src")
    bob_nb = _nb(repo, bob, "bobs")
    mem = _confirmed_memory(service, src, alice)
    with pytest.raises(PermissionError):
        repo.transfer_memories(alice.id, [mem.id], bob_nb, "copy", extract_kg=False)

def test_non_confirmed_memory_not_transferable(repo, alice):
    service = repo._runtime.memory_service
    src, dst = _nb(repo, alice, "src"), _nb(repo, alice, "dst")
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None
    cand = service.create_candidate(src, alice.id, None, "req", "T", "B", [], "task")
    results = repo.transfer_memories(alice.id, [cand.id], dst, "copy", extract_kg=False)
    assert results[0]["ok"] is False and "confirmed" in results[0]["error"].lower()
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_memory_transfer_service.py -x -q`
Expected: FAIL（`AttributeError: transfer_memories`）

- [ ] **Step 3a: 实现 service（append 到 memory_service.py 尾部，class 内）**

```python
    def transfer(
        self,
        user_id: str,
        memory_ids: "Sequence[str]",
        target_notebook_id: str,
        mode: str,
        extract_kg: bool = True,
    ) -> list[dict]:
        if mode not in {"copy", "move"}:
            raise ValueError(f"unknown transfer mode: {mode}")
        # 目标必须当前用户 owner（memory 私有；两端都是我）
        if not self.notebooks.user_can_access_notebook(target_notebook_id, user_id):
            raise PermissionError(target_notebook_id)
        results: list[dict] = []
        for memory_id in memory_ids:
            try:
                source = self.store.memory_for_user(memory_id, user_id)
                if source.notebook_id == target_notebook_id:
                    raise ValueError("源与目标不能是同一个 notebook")
                if source.status != "confirmed":
                    raise ValueError("只能传输 confirmed 状态的 memory")
                now = self.now()
                provenance = {
                    "imported_from": {
                        "notebook_id": source.notebook_id,
                        "memory_id": source.id,
                        "action": mode,
                    }
                }
                write = MemoryWrite(
                    id=self.new_id("memory"),
                    notebook_id=target_notebook_id,
                    created_by=user_id,
                    source_answer_id=None,
                    origin=source.origin,
                    status="confirmed",
                    title=source.title,
                    content_md=source.content_md,
                    tags=list(source.tags),
                    confirmed_by=user_id,
                    confirmed_at=now,
                    created_at=now,
                    updated_at=now,
                    provenance=provenance,
                )
                copied = self.store.create_copy_with_initial_revision(
                    write, source.id, user_id, f"从 {source.notebook_id} {mode}"
                )
                self._event("memory_lifecycle", copied, action=f"transfer_{mode}")
                self._maybe_schedule_kg(copied, extract_kg)
                if copied.embedding_status != "ready":
                    self._schedule_embed(copied)
                if mode == "move":
                    if self.memory_kg is not None:
                        self.memory_kg.remove_memory_source(source.id)
                    self.store.delete_memory(source.id, user_id)
                results.append(
                    {"source_id": memory_id, "new_id": copied.id, "ok": True, "error": None}
                )
            except (KeyError, ValueError) as exc:
                results.append(
                    {"source_id": memory_id, "new_id": None, "ok": False, "error": str(exc)}
                )
        return results
```

（确认 `MemoryWrite` 已在 memory_service.py import；`Sequence` 已 import。`PermissionError` 越权是硬失败、逐条 KeyError/ValueError 是软失败——这是刻意的：目标越权应整体拒绝，单条问题不连坐其他。）

- [ ] **Step 3b: facade 委托（append 到 SQLiteRepository 类尾部）**

```python
    def transfer_memories(
        self,
        user_id: str,
        memory_ids: List[str],
        target_notebook_id: str,
        mode: str,
        extract_kg: bool = True,
    ) -> list[dict]:
        return self._runtime.memory_service.transfer(
            user_id, memory_ids, target_notebook_id, mode, extract_kg
        )
```

- [ ] **Step 3c: ports stub（`MemoryRepository` in ports.py）**

```python
    def transfer_memories(
        self, user_id: str, memory_ids: list[str], target_notebook_id: str,
        mode: str, extract_kg: bool = True,
    ) -> list[dict]: ...
```

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_memory_transfer_service.py -x -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/memory_service.py backend/app/services/sqlite_repository.py backend/app/repositories/ports.py backend/tests/test_memory_transfer_service.py
git commit -m "feat(memory): MemoryService.transfer 复制/移动+facade 委托"
```

---

### Task B3: REST 端点 `POST /memories/transfer`

**Files:**
- Modify: `backend/app/models/schemas.py`（append `MemoryTransferRequest`）
- Modify: `backend/app/api/memory_routes.py`（append 路由）
- Test: `backend/tests/test_memory_transfer_routes.py`

**Interfaces:**
- Consumes: facade `transfer_memories`（B2）；`get_current_user`、`memory_service` dep、`_memory_call`（同文件）。
- Produces: `POST /memories/transfer`，body `MemoryTransferRequest{memory_ids: list[str], target_notebook_id: str, mode: Literal["copy","move"], extract_kg: bool=True}`，返回 `{"results": [...]}`。`PermissionError`→404、`ValueError`→409（`_memory_call` 既有映射）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_memory_transfer_routes.py
# 同 A4：真实 _login，repo fixture 与 app 共享同一 tmp DB。
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def client(repo):
    from app.main import app
    return TestClient(app)

def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}

def test_transfer_endpoint_copies(client, repo):
    h = _login(client, "a00000001")
    uid = client.get("/api/me", headers=h).json()["id"]
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    # 用同一 tmp DB 的 fixture repo 造一条 confirmed memory（归属 uid）
    service = repo._runtime.memory_service
    service.embedding_scheduler = lambda fn, job: fn(job)
    service.kg_ingest_scheduler = lambda fn, key: None
    cand = service.create_candidate(src, uid, None, "req", "T", "B", [], "task")
    mem = service.confirm(cand.id, uid)
    resp = client.post(
        "/api/memories/transfer",
        json={"memory_ids": [mem.id], "target_notebook_id": dst, "mode": "copy", "extract_kg": False},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["ok"] is True
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd backend && pytest tests/test_memory_transfer_routes.py -x -q`
Expected: FAIL（404 route not found）

- [ ] **Step 3a: schema（append 到 schemas.py memory 区块尾部）**

```python
class MemoryTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_ids: List[str]
    target_notebook_id: str
    mode: Literal["copy", "move"]
    extract_kg: bool = True
```

- [ ] **Step 3b: 路由（append 到 memory_routes.py 尾部）**

```python
@memory_router.post("/memories/transfer")
async def transfer_memories(
    payload: MemoryTransferRequest,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> dict:
    results = await _memory_call(
        service.transfer_memories,
        user.id,
        payload.memory_ids,
        payload.target_notebook_id,
        payload.mode,
        payload.extract_kg,
    )
    return {"results": results}
```

（在 memory_routes.py 顶部的 schemas import 里补 `MemoryTransferRequest`。）

- [ ] **Step 4: 跑测试看通过**

Run: `cd backend && pytest tests/test_memory_transfer_routes.py -x -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/api/memory_routes.py backend/tests/test_memory_transfer_routes.py
git commit -m "feat(memory): /memories/transfer REST 端点"
```

---

### Task B4: memory 架构守卫 + 契约 fixture

**Files:**
- Modify: `backend/app/repositories/ownership_manifest.py`（`DELEGATE_OWNER_OVERRIDES` 加 `'transfer_memories': 'MemoryService'`）
- Modify: `backend/tests/test_memory_repository_boundaries.py`（把 `transfer_memories` 加进它断言的 service-方法列表 + `DELEGATE_OWNER_OVERRIDES` 检查列表）
- Modify: `backend/tests/test_repository_surface_manifest.py`（新增 `MEMORY_TRANSFER_ALLOWED_NEW_MEMBERS = {"transfer_memories"}` 并接入 pop 循环）
- Regenerate: `backend/tests/fixtures/repository_contract/api_contract.json`

**Interfaces:**
- Consumes: B1–B3 新符号。
- Produces: 全部守卫 + 契约测试通过。

> `transfer_memories` 是**新 facade 方法**且会被测试里 `repo.transfer_memories(...)` 静态扫到 → 必须登记 `DELEGATE_OWNER_OVERRIDES`（owner=`MemoryService`）+ `test_memory_repository_boundaries.py` 的两处列表 + `*_ALLOWED_NEW_MEMBERS`。

- [ ] **Step 1: 先跑守卫看红**

Run:
```bash
cd backend && pytest tests/test_memory_repository_boundaries.py tests/test_repository_surface_manifest.py tests/test_repository_api_contract.py -q
```
Expected: 观察失败（预期：boundaries 缺 `transfer_memories`、surface manifest 新成员、api_contract openapi 漂移）。

- [ ] **Step 2: 登记 ownership + boundaries**

在 `ownership_manifest.py` 的 `DELEGATE_OWNER_OVERRIDES`（`:480-493`）加：
```python
    'transfer_memories': 'MemoryService',
```
在 `test_memory_repository_boundaries.py` 把 `transfer_memories` 加进它断言「service 方法必在 `MemoryRepository.__dict__` 且必是 facade 委托」的列表（`:25-56` 与 `:105-120` 处的名字元组），保持与 `DELEGATE_OWNER_OVERRIDES` 一致。

- [ ] **Step 3: surface manifest allowlist**

在 `test_repository_surface_manifest.py` 加：
```python
MEMORY_TRANSFER_ALLOWED_NEW_MEMBERS = {"transfer_memories"}  # /memories/transfer 复制/移动
```
并把它并入 `:3407-3422` 的 pop 循环并集（与既有 `TASK2_MEMORY_ALLOWED_NEW_MEMBERS` 同法）。

- [ ] **Step 4: 重生成 api_contract.json**

Run:
```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/local-file-management-design-7c7a39
python scripts/generate_repository_contract_fixtures.py
```
Expected: 只 `api_contract.json` 变化（含 `/memories/transfer`）。撤销任何 `facade_surface.json`/`baseline.db` 误改。

- [ ] **Step 5: 全量守卫 + memory 测试绿**

Run:
```bash
cd backend && pytest tests/test_memory_repository_boundaries.py tests/test_repository_surface_manifest.py tests/test_repository_api_contract.py tests/test_repository_facade_contract.py tests/test_memory_transfer_store.py tests/test_memory_transfer_service.py tests/test_memory_transfer_routes.py -q
```
Expected: PASS（全绿）

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/ownership_manifest.py backend/tests/test_memory_repository_boundaries.py backend/tests/test_repository_surface_manifest.py backend/tests/fixtures/repository_contract/api_contract.json
git commit -m "test(memory): transfer_memories 过架构守卫+重生成 api 契约"
```

---

## Phase C — 前端（与后端同 PR）

在 `frontend/` 下操作。测试跑 `npm test`；类型检查 `npm run lint`。

### Task C1: 传输 API 客户端 + 纯 helper（含 wire 契约）

**Files:**
- Create: `frontend/app/transfer-model.ts`（共享纯 helper：`TransferMode`、`destinationNotebooks`、body 构造器）
- Create: `frontend/app/knowhow-transfer.ts`（网络客户端）
- Create: `frontend/app/memory-transfer.ts`（网络客户端）
- Test: `frontend/app/transfer-model.test.mjs`

**Interfaces:**
- Produces:
  - `transfer-model.ts`: `export type TransferMode = "copy" | "move";`
    `export const destinationNotebooks = (all, sourceId) => all.filter(n => n.id !== sourceId && n.access !== "reader");`
    `export const knowhowTransferBody = (targetNotebookId: string, mode: TransferMode) => ({ target_notebook_id: targetNotebookId, mode });`
    `export const memoryTransferBody = (memoryIds: string[], targetNotebookId: string, mode: TransferMode, extractKg: boolean) => ({ memory_ids: memoryIds, target_notebook_id: targetNotebookId, mode, extract_kg: extractKg });`
  - `knowhow-transfer.ts`: `export const transferKnowhowTable = (notebookId, tableId, targetNotebookId, mode) => Promise<{ new_table_id: string }>`
  - `memory-transfer.ts`: `export const transferMemories = (memoryIds, targetNotebookId, mode, extractKg) => Promise<{ results: TransferResult[] }>`
- Consumes: `authHeaders`（`./auth.ts`）；`NotebookSummary`（`./workspace-model.ts`）。

- [ ] **Step 1: 写失败测试**

```js
// frontend/app/transfer-model.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import {
  destinationNotebooks,
  knowhowTransferBody,
  memoryTransferBody,
} from "./transfer-model.ts";

test("destinationNotebooks: 排除源 + 排除只读", () => {
  const all = [
    { id: "n1", name: "A" },
    { id: "n2", name: "B", access: "reader" },
    { id: "n3", name: "C", access: "owner" },
  ];
  const out = destinationNotebooks(all, "n1");
  assert.deepEqual(out.map((n) => n.id), ["n3"]);
});

test("knowhowTransferBody: 锁字段名 target_notebook_id/mode", () => {
  assert.deepEqual(knowhowTransferBody("nb-2", "move"), {
    target_notebook_id: "nb-2",
    mode: "move",
  });
});

test("memoryTransferBody: 锁字段名 memory_ids/target_notebook_id/mode/extract_kg", () => {
  assert.deepEqual(memoryTransferBody(["m1", "m2"], "nb-2", "copy", false), {
    memory_ids: ["m1", "m2"],
    target_notebook_id: "nb-2",
    mode: "copy",
    extract_kg: false,
  });
});
```

- [ ] **Step 2: 跑测试看失败**

Run: `cd frontend && node --test app/transfer-model.test.mjs`
Expected: FAIL（`Cannot find module './transfer-model.ts'`）

- [ ] **Step 3: 实现三个模块**

```ts
// frontend/app/transfer-model.ts
import type { NotebookSummary } from "./workspace-model.ts";

export type TransferMode = "copy" | "move";

/** 目标笔记本候选：排除源自身 + 排除只读（reader）库。 */
export const destinationNotebooks = (
  all: readonly NotebookSummary[],
  sourceId: string
): NotebookSummary[] => all.filter((n) => n.id !== sourceId && n.access !== "reader");

export const knowhowTransferBody = (targetNotebookId: string, mode: TransferMode) => ({
  target_notebook_id: targetNotebookId,
  mode,
});

export const memoryTransferBody = (
  memoryIds: readonly string[],
  targetNotebookId: string,
  mode: TransferMode,
  extractKg: boolean
) => ({
  memory_ids: [...memoryIds],
  target_notebook_id: targetNotebookId,
  mode,
  extract_kg: extractKg,
});
```

```ts
// frontend/app/knowhow-transfer.ts
import { authHeaders } from "./auth.ts";
import { knowhowTransferBody, type TransferMode } from "./transfer-model.ts";

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

export const transferKnowhowTable = (
  notebookId: string,
  tableId: string,
  targetNotebookId: string,
  mode: TransferMode
): Promise<{ new_table_id: string }> =>
  apiFetch(`/notebooks/${notebookId}/knowhow/${tableId}/transfer`, {
    method: "POST",
    body: JSON.stringify(knowhowTransferBody(targetNotebookId, mode)),
  });
```

```ts
// frontend/app/memory-transfer.ts
import { authHeaders } from "./auth.ts";
import { memoryTransferBody, type TransferMode } from "./transfer-model.ts";

export type TransferResult = {
  source_id: string;
  new_id: string | null;
  ok: boolean;
  error: string | null;
};

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  if (res.status === 204) return null as T;
  return res.json() as Promise<T>;
}

export const transferMemories = (
  memoryIds: readonly string[],
  targetNotebookId: string,
  mode: TransferMode,
  extractKg: boolean
): Promise<{ results: TransferResult[] }> =>
  apiFetch(`/memories/transfer`, {
    method: "POST",
    body: JSON.stringify(memoryTransferBody(memoryIds, targetNotebookId, mode, extractKg)),
  });
```

- [ ] **Step 4: 跑测试 + tsc**

Run: `cd frontend && node --test app/transfer-model.test.mjs && npm run lint`
Expected: 测试 PASS（3）；tsc 无报错

- [ ] **Step 5: 提交**

```bash
git add frontend/app/transfer-model.ts frontend/app/knowhow-transfer.ts frontend/app/memory-transfer.ts frontend/app/transfer-model.test.mjs
git commit -m "feat(fe): 传输 API 客户端+纯 helper（锁 wire 字段名）"
```

---

### Task C2: 目标笔记本选择器 modal 组件

**Files:**
- Create: `frontend/app/transfer-picker.tsx`（`DestinationPicker` 组件，`utility-modal` 样式）

**Interfaces:**
- Produces: `export function DestinationPicker({ sourceNotebookId, allowMove, title, onCancel, onSubmit }: { sourceNotebookId: string; allowMove: boolean; title: string; onCancel: () => void; onSubmit: (targetNotebookId: string, mode: TransferMode) => Promise<void>; })` — 自己拉 `GET /notebooks`、`destinationNotebooks` 过滤、`<select>` 选目标 + 复制/移动切换（`allowMove=false` 时只给复制）+ 确认（pending/错误态）。
- Consumes: `authHeaders`；`NotebookSummary`（`./workspace-model.ts`）；`destinationNotebooks`、`TransferMode`（`./transfer-model.ts`）。

- [ ] **Step 1: 实现组件**

```tsx
// frontend/app/transfer-picker.tsx
import { useEffect, useState } from "react";
import { authHeaders } from "./auth.ts";
import type { NotebookSummary } from "./workspace-model.ts";
import { destinationNotebooks, type TransferMode } from "./transfer-model.ts";

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

export function DestinationPicker({
  sourceNotebookId,
  allowMove,
  title,
  onCancel,
  onSubmit,
}: {
  sourceNotebookId: string;
  allowMove: boolean;
  title: string;
  onCancel: () => void;
  onSubmit: (targetNotebookId: string, mode: TransferMode) => Promise<void>;
}) {
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState<TransferMode>("copy");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetch(API_BASE + "/notebooks", {
      headers: { ...authHeaders() },
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`${res.status}`))))
      .then((all: NotebookSummary[]) => setNotebooks(destinationNotebooks(all, sourceNotebookId)))
      .catch((err) => {
        if (err?.name !== "AbortError") setError("加载笔记本列表失败");
      });
    return () => controller.abort();
  }, [sourceNotebookId]);

  const submit = async () => {
    if (!target) {
      setError("请选择目标笔记本");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onSubmit(target, mode);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
      setBusy(false);
    }
  };

  return (
    <div className="utility-modal utility-modal-top" role="dialog" aria-modal="true" aria-label={title}>
      <div className="utility-modal-card">
        <h3>{title}</h3>
        <label>
          目标笔记本
          <select value={target} disabled={busy} onChange={(e) => setTarget(e.target.value)}>
            <option value="">选择…</option>
            {notebooks.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </label>
        {allowMove && (
          <div className="transfer-mode-toggle">
            <label>
              <input type="radio" name="mode" checked={mode === "copy"} disabled={busy}
                onChange={() => setMode("copy")} /> 复制
            </label>
            <label>
              <input type="radio" name="mode" checked={mode === "move"} disabled={busy}
                onChange={() => setMode("move")} /> 移动（会从源删除）
            </label>
          </div>
        )}
        {error && <p className="transfer-error" role="alert">{error}</p>}
        <div className="memory-dialog-actions">
          <button type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button type="button" disabled={busy || !target} onClick={submit}>
            {busy ? "处理中…" : "确认"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: tsc**

Run: `cd frontend && npm run lint`
Expected: 无报错

- [ ] **Step 3: 提交**

```bash
git add frontend/app/transfer-picker.tsx
git commit -m "feat(fe): DestinationPicker 目标笔记本选择器 modal"
```

---

### Task C3: knowhow 表工具栏接入「复制/移动到…」

**Files:**
- Modify: `frontend/app/knowhow-panel.tsx`（工具栏加按钮 + `transferOpen` 状态 + 条件挂 `DestinationPicker`）

**Interfaces:**
- Consumes: `DestinationPicker`（C2）；`transferKnowhowTable`（C1）；现有 `notebookId`、`detail`（含 `detail.id` 表 id）、`selectedTableId`、`loadDetail`、`canEdit`、以及表列表刷新（`loadTables` 或等价）。
- Produces: 表工具栏一个「复制/移动到…」按钮 → 选择器 → 调 `transferKnowhowTable` → 成功后刷新。

- [ ] **Step 1: 加状态 + import**（knowhow-panel.tsx 顶部 import 区 + 组件状态区，紧邻既有 `const [manageOpen, setManageOpen] = useState(false);`，line 166）

```tsx
import { DestinationPicker } from "./transfer-picker.tsx";
import { transferKnowhowTable } from "./knowhow-transfer.ts";
import type { TransferMode } from "./transfer-model.ts";
// …组件内，manageOpen 旁：
const [transferOpen, setTransferOpen] = useState(false);
```

- [ ] **Step 2: 工具栏按钮**（`knowhow-grid-toolbar-actions` 里，紧邻「管理」按钮，line ~2990。走 `canEdit` 门与其它按钮一致）

```tsx
<button type="button" className="sort-button knowhow-reproject-button"
  onClick={() => setTransferOpen(true)} disabled={!detail || deleting}
  title="把这张表复制或移动到另一个笔记本">
  <Copy size={14} /> 复制/移动到…
</button>
```

（`Copy` 从 `lucide-react` import——与既有图标同处；若命名冲突用 `CopyPlus`。此按钮需要把 `onTransfer` prop 透传到渲染工具栏的子组件，模式与既有 `onOpenManage` 完全一致：在子组件 props 声明加 `onTransfer: () => void;`，父级传 `onTransfer={() => setTransferOpen(true)}`，line ~971 附近。）

- [ ] **Step 3: 条件挂载选择器**（render 尾部，紧邻既有 `{manageOpen && …}`，line ~1102）

```tsx
{transferOpen && detail && (
  <DestinationPicker
    sourceNotebookId={notebookId}
    allowMove={canEdit}
    title={`复制/移动表：${detail.title}`}
    onCancel={() => setTransferOpen(false)}
    onSubmit={async (targetNotebookId, mode: TransferMode) => {
      await transferKnowhowTable(notebookId, detail.id, targetNotebookId, mode);
      setTransferOpen(false);
      if (mode === "move") {
        // 移动后源表没了：回到表列表
        setSelectedTableId(null);
        await loadTables();
      }
    }}
  />
)}
```

（`allowMove={canEdit}`：只有对源有写权（owner）才允许移动；只读源只给复制。`setSelectedTableId`/`loadTables` 用本组件既有的表列表状态与加载函数名——按文件实际命名对齐，参考 `handleManageChanged`（line 674）里刷新的写法。）

- [ ] **Step 4: tsc + 冒烟测试**

Run: `cd frontend && npm run lint`
Expected: 无报错。（组件交互留待收尾浏览器验证——见收尾。）

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-panel.tsx
git commit -m "feat(fe): knowhow 表工具栏接入复制/移动到…"
```

---

### Task C4: memory 单条 + 批量接入「复制/移动到…」

**Files:**
- Modify: `frontend/app/memory-panel.tsx`（per-item 与 bulk 各加入口 + `pendingTransfer` 状态 + 挂 `DestinationPicker`）

**Interfaces:**
- Consumes: `DestinationPicker`（C2）；`transferMemories`（C1）；现有 `selectedIds`、`items`、`busyId`、列表刷新（`reload`/`load` 等既有函数）、每条 `memory.notebook_id`、`memory.status`。
- Produces: confirmed memory 卡片「复制/移动到…」+ 批量栏「复制/移动到…（N）」→ 选择器 → `transferMemories` → 刷新。

- [ ] **Step 1: 加状态 + import**（顶部 import + 组件状态，紧邻 `selectedIds`，line 481）

```tsx
import { DestinationPicker } from "./transfer-picker.tsx";
import { transferMemories } from "./memory-transfer.ts";
import type { TransferMode } from "./transfer-model.ts";
// 组件内：kind 判别，单条带 memory，批量用当前 selectedIds
const [pendingTransfer, setPendingTransfer] =
  useState<{ ids: string[]; sourceNotebookId: string } | null>(null);
```

- [ ] **Step 2: per-item 按钮**（`memory-card-actions` 里，仅 `memory.status === "confirmed"` 时，紧邻「删除」，line ~918）

```tsx
<button type="button" className="memory-transfer-action" disabled={busy}
  onClick={() => setPendingTransfer({ ids: [memory.id], sourceNotebookId: memory.notebook_id })}>
  <Copy size={14} /> 复制/移动到…
</button>
```

- [ ] **Step 3: 批量按钮**（`memory-bulk-bar` 里，紧邻「删除选中」，line ~778。批量传输要求所选同源笔记本——用第一条的 notebook 作源，若跨源则禁用）

```tsx
<button type="button" className="memory-bulk-transfer"
  disabled={selectedIds.size === 0 || Boolean(busyId)}
  onClick={() => {
    const chosen = items.filter((m) => selectedIds.has(m.id));
    const sources = new Set(chosen.map((m) => m.notebook_id));
    if (sources.size !== 1) {
      window.alert("批量复制/移动要求所选 Memory 属于同一个笔记本");
      return;
    }
    setPendingTransfer({
      ids: chosen.map((m) => m.id),
      sourceNotebookId: chosen[0].notebook_id,
    });
  }}>
  <Copy size={14} /> 复制/移动到…（{selectedIds.size}）
</button>
```

- [ ] **Step 4: 挂载选择器**（render 尾部，紧邻既有 `{pendingDelete && …}`，line ~952）

```tsx
{pendingTransfer && (
  <DestinationPicker
    sourceNotebookId={pendingTransfer.sourceNotebookId}
    allowMove
    title={`复制/移动 ${pendingTransfer.ids.length} 条 Memory`}
    onCancel={() => setPendingTransfer(null)}
    onSubmit={async (targetNotebookId, mode: TransferMode) => {
      const { results } = await transferMemories(pendingTransfer.ids, targetNotebookId, mode, true);
      const failed = results.filter((r) => !r.ok);
      setPendingTransfer(null);
      setSelectedIds(new Set());
      await reload();
      if (failed.length) {
        window.alert(`${failed.length} 条未成功：${failed.map((f) => f.error).join("；")}`);
      }
    }}
  />
)}
```

（`Copy` 从 `lucide-react` import；`reload` 用 memory-panel 既有的列表加载函数名——按文件实际命名对齐，参考删除后刷新的调用。`extract_kg` 这里固定 `true`（沿用 confirm 默认开）；如需 UI 开关，后续迭代再加。）

- [ ] **Step 5: tsc**

Run: `cd frontend && npm run lint`
Expected: 无报错

- [ ] **Step 6: 提交**

```bash
git add frontend/app/memory-panel.tsx
git commit -m "feat(fe): memory 单条+批量接入复制/移动到…"
```

---

## 收尾（不单列 task，执行完 Phase A–C 后做）

- [ ] 后端全量：`cd backend && pytest -q`，确认全绿（含既有 3400+ 用例）。
- [ ] 前端全量：`cd frontend && npm test && npm run lint`。
- [ ] 浏览器真机验证（在主 checkout root 做，worktree 无 dev server；用 preview_start 起前端 + 后端从 backend/ 起）：①knowhow 表「复制到」另一库→目标出现独立副本、图片随迁、引用可跳；②knowhow「移动」→源表消失；③memory 单条/批量复制→目标出现、源仍在；④memory 移动→源消失。截图留证。
- [ ] 分支 rebase 到最新 master 保持线性 → push → `gh pr create --base master`（沿用 `dev-flow-finish-with-pr`、`pr-merge-is-rebase`）。PR 描述附：K-1 零重嵌入、无 schema 迁移、访问模型表、真机验证截图。

---

## 附录：关键复用点 file:line（实现时核对 live 码）

- 整本拷贝的 K-1 稳定-id 派生物重映射样板：`backend/app/services/notebook_sharing.py:294-378`（元素/chunk/embedding），资产磁盘拷贝 `:251-270`，`_rewrite_asset_refs`/`_ASSET_REF_RE` `:34-46`。
- 投影稳定 id：`backend/app/services/knowhow/projection.py` `element_id`(:122)、`cell_chunk_id`(:137)、`ensure_hidden_source`(:267)、`project_table`(:301)、`delete_table_projection`(:684)。
- 调度器：`backend/app/services/knowhow/api.py` `get_scheduler`(:381)、`build_projector`(:133)。
- knowhow store：`backend/app/repositories/sqlite/knowhow_store.py` `get_knowhow_table`(:344)、`delete_knowhow_table`(:411)、`insert_notebook_asset`(:690)。
- memory store：`backend/app/repositories/sqlite/memory_store.py` `_insert_memory_on`(:287)、`_ensure_initial_revision_on`(:373)、`replace_embedding`(:1363)、`_select_columns`(:262)、`_record`(:238)、`delete_memory`(:1240)；唯一键 `idx_memory_answer_once` `migrations.py:1068`。
- memory service：`backend/app/services/memory_service.py` `create_from_answer`(:572)、`_maybe_schedule_kg`(:136)、`_schedule_embed`(:494)、`deprecate`(:710)（`remove_memory_source` 调用样板）。
- 访问守卫：`backend/app/api/deps.py` `require_notebook_write`(:72)/`read`(:84)、`notebook_access_repository`(:33)；`user_can_access_notebook`/`user_can_read_notebook`（`notebook_sharing.py:638/645`）。
- 架构守卫：`ownership_manifest.py` `DELEGATE_OWNER_OVERRIDES`(:480)；`test_repository_surface_manifest.py` pop 循环(:3407)；`test_repository_callers_static.py` `INDEPENDENT_PRIVATE_SITES`(:249)；`test_memory_repository_boundaries.py`；生成器 `scripts/generate_repository_contract_fixtures.py`（repo 根）。
- 前端：API 客户端模板 `frontend/app/notebook-share.ts`；`utility-modal` 用例 `frontend/app/memory-panel.tsx:952`；notebook 列表加载 `page.tsx:1898` / `memory-panel.tsx:205`；`NotebookSummary` `workspace-model.ts:5`。
