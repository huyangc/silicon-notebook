# Knowhow 表版本管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 knowhow 表加版本管理——自动记录每次变更、可命名里程碑、可看时间线/单次 diff/两版对比/单格历史、可整表回退到任意历史点、可单格恢复。

**Architecture:** 变更流水（delta）方案。每次写事务内追加一条 `knowhow_changes`，存受影响实体的 before/after 加**变更后的整表指纹**。回退 = 从当前逆序把 before 写回，前后各校验一次指纹（对不上就中止/回滚）。里程碑是给流水序号起名的纯标签，零快照。

**Tech Stack:** Python 3.13 / FastAPI / SQLite（WAL）/ pytest；前端 Next.js + React + TypeScript，测试用 `node --test` + `.test.mjs`。

**设计文档：** `docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md`（下称"spec"，各任务会引用其小节号）。

---

## Global Constraints

这些是**每个任务都隐含要遵守**的项目级约束，值均从 spec 与实测复核而来：

1. **原始 SQL 只允许出现在 `backend/app/repositories/sqlite/` 下。** 别处写裸 SQL 会触发 `test_repository_dependency_contract.py` 的 `unreasoned SQL boundary` 断言。
2. **facade（`backend/app/services/sqlite_repository.py`）新增成员必须是纯一跳委托**：方法体只有一条 `return self._runtime.knowhow_store.xxx(...)`。`test_repository_facade_contract.py` 用 AST 强校验形状。
3. **所有会改动 knowhow 内容的路径，重投影一律走 `knowhow_api.get_scheduler(repo).schedule(table_id)`**，绝不直接 `background_jobs.submit`，也不直调 `build_projector(repo).project_table(...)`。`project_table` 本身永远是全量确定性重投影。
4. **面向用户的错误文案用 `user_error(status, message)`**（`backend/app/api/deps.py:286`），它会打 `X-User-Message: 1` 头。凡是拼了 `str(exc)` 的**不要**用它。
5. **前端 API 路径不带 `/api` 前缀** —— `API_BASE` 默认值已经是 `http://127.0.0.1:8000/api`。写成 `/notebooks/...`，PR#207 曾因双 `/api` 导致 404 且逃过全部测试。
6. **前端纯逻辑必须放 `.ts` 而非 `.tsx`** —— Node 原生类型剥离不支持 `.tsx`，`node --test` import 不了。这是既有 `knowhow-*.tsx` / `knowhow-*-logic.ts` 成对拆分的技术原因。
7. **前端新增 CSS 必须写进 `frontend/app/knowhow-panel.tsx` 里那个唯一的 `<style jsx global>` 块**（约 `L1475-3251`），不能在新组件文件里另开 `<style jsx>`：styled-jsx 的 global 样式绑定"声明它的组件是否渲染过"，`KnowhowPanel` 是唯一保证挂载的容器。
8. **后端测试命令**（仓库根目录）：`PYTHONPATH=backend python3 -m pytest backend/tests/test_xxx.py`。`backend/pytest.ini` 默认 `-n 12` 并行，调试加 `-n0`。
9. **前端测试命令**（`frontend/` 目录）：单文件 `node --test app/xxx.test.mjs`；全量 `npm test`。
10. **schema 迁移约定**：加表必须新增 `_migration_N` + bump `SCHEMA_VERSION`，绝不塞进已封版的 `_migration_1`。`migrate()` 靠 `getattr(self, f"_migration_{version}")` 反射调用，无需登记注册表。
11. **中文文案**：所有面向用户的字符串用中文。

---

## File Structure

**新建（后端）**
| 文件 | 职责 |
|---|---|
| `backend/app/repositories/sqlite/knowhow_fingerprint.py` | 从 `KnowhowTransferStore` 抽出的共享整表指纹（SQL 常量 + `fingerprint_on(db, table_id)`） |
| `backend/app/repositories/sqlite/knowhow_history_store.py` | 模块级 `record_change(db, ...)` + `KnowhowHistoryStore` 类（查询/里程碑/prune/回退重放） |
| `backend/app/services/knowhow/history.py` | 服务层：时间线整形、两版 diff 聚合（纯函数）、回退编排（校验 → 调 store → 触发重投影） |

**新建（前端）**
| 文件 | 职责 |
|---|---|
| `frontend/app/knowhow-history-logic.ts` | 纯函数：diff 计算、区间聚合、时间线按天分组、origin 徽章映射 |
| `frontend/app/knowhow-history-drawer.tsx` | 历史抽屉组件（仿 `knowhow-matrix-drawer.tsx` 骨架） |
| `frontend/app/knowhow-cell-history.tsx` | 格子浮窗第三态「历史」 |
| `frontend/app/knowhow-history-logic.test.mjs` | 上述纯函数的单测 |

**修改（后端）**
`migrations.py`（新迁移 + 版本）、`knowhow_store.py`（15 处挂钩）、`knowhow_transfer_store.py`（指纹改为 re-export）、`maintenance.py`（清扫器引用集）、`services/knowhow/api.py`（actor/origin 穿线 + transfer 创世流水）、`api/knowhow_routes.py`（8 个新端点 + `origin` 字段）、`services/sqlite_repository.py`（facade 委托）、`scripts/backfill_knowhow_md.py`（传 actor/origin）。

**修改（前端）**
`knowhow-panel.tsx`（历史抽屉槽位 + 工具栏按钮 + CSS + 三处清空）、`knowhow-cell-editor.tsx`（三态 + 入口按钮）、`knowhow-model.ts`（新 fetcher + `origin` 字段）、`knowhow-manage.tsx` / `knowhow-manage-logic.ts`（清理历史分区）。

---

## Task 1: 抽出共享指纹模块

把 `_FINGERPRINT_SQL` / `_fingerprint_on` 从 `KnowhowTransferStore` 搬到独立模块，让版本管理和传输两边共用。这是纯重构任务，**行为必须逐字节不变**。

**Files:**
- Create: `backend/app/repositories/sqlite/knowhow_fingerprint.py`
- Modify: `backend/app/repositories/sqlite/knowhow_transfer_store.py`（删除搬走的定义，改为 re-export 别名）
- Test: `backend/tests/test_knowhow_fingerprint.py`（新建）

**Interfaces:**
- Produces: `knowhow_fingerprint.FINGERPRINT_SQL: str`、`knowhow_fingerprint.GROUP_SEP: str`、`knowhow_fingerprint.fingerprint_on(db: sqlite3.Connection, table_id: str) -> str | None`
- Consumes: 无

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_fingerprint.py`：

```python
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


BASE_COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "修复方法", "role": "attribute"},
]


def test_transfer_store_alias_is_the_shared_module_object(repo):
    """搬迁不能改变 KnowhowTransferStore 的既有表面：别名必须是同一个对象。"""
    assert KnowhowTransferStore._FINGERPRINT_SQL is knowhow_fingerprint.FINGERPRINT_SQL
    assert KnowhowTransferStore._GROUP_SEP is knowhow_fingerprint.GROUP_SEP


def test_shared_helper_and_transfer_store_agree(repo, notebook_id):
    store = repo._runtime.knowhow_store
    table_id = store.create_knowhow_table(notebook_id, "t", "", BASE_COLUMNS)
    transfer = repo._runtime.knowhow_transfer_store

    with repo._runtime.database.connect() as db:
        shared = knowhow_fingerprint.fingerprint_on(db, table_id)

    assert shared == transfer.table_fingerprint(table_id)
    assert isinstance(shared, str) and len(shared) == 64


def test_missing_table_returns_none(repo):
    with repo._runtime.database.connect() as db:
        assert knowhow_fingerprint.fingerprint_on(db, "nope") is None
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_fingerprint.py -n0 -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.repositories.sqlite.knowhow_fingerprint'`

- [ ] **Step 3: 建新模块**

创建 `backend/app/repositories/sqlite/knowhow_fingerprint.py`。**把 `knowhow_transfer_store.py` 里 `_FINGERPRINT_SQL`（当前 `L308-331`）、`_GROUP_SEP`（`L339`）、`_fingerprint_on`（`L341-354`）三处的代码逐字节搬过来**（含其上方所有解释性注释——那些注释记录了三轮评审各补了哪个字段，是有价值的历史）：

```python
"""knowhow 单表整表指纹：跨 store 共享的唯一定义点。

原属 KnowhowTransferStore（为 move_table 的并发编辑守卫而写）。版本管理
（docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md）
把它同时用作**回退重放正确性的判据**，两个 store 都要用，故抽到这里，
KnowhowTransferStore 保留同对象别名不破坏既有引用。

⚠️ 改动 FINGERPRINT_SQL 前必读：它现在有两个身份。
  1. move_table 的"源表在复制后被改过吗"探针（原有）。
  2. knowhow_changes 每条流水记录的 fingerprint，回退前后各校验一次（新增）。
身份 2 依赖两个性质，动 SQL 时必须保住：
  · 覆盖范围 == 版本管理的"全盖"范围（表元/列/行/格子/代码附件）；
  · **不含 updated_at 时间戳** —— 回退会写新的时间戳，若指纹覆盖它，
    回退后的后置校验将永远失败，整个回退功能不成立。
"""
from __future__ import annotations

import hashlib
import sqlite3


#: <把 knowhow_transfer_store.py 原有的 #: 注释块逐字节搬来>
FINGERPRINT_SQL = (
    "SELECT t.title AS title, t.description AS description, "
    "(SELECT group_concat(sig, char(30)) FROM ("
    "  SELECT id || char(31) || name || char(31) || role || char(31) || position AS sig"
    "  FROM knowhow_columns WHERE table_id = t.id ORDER BY id"
    ")) AS columns_signal, "
    "(SELECT group_concat(sig, char(30)) FROM ("
    "  SELECT id || char(31) || position AS sig"
    "  FROM knowhow_rows WHERE table_id = t.id ORDER BY id"
    ")) AS rows_signal, "
    "(SELECT group_concat(sig, char(30)) FROM ("
    "  SELECT c.row_id || char(31) || c.column_id || char(31) || c.content_md AS sig"
    "  FROM knowhow_cells c JOIN knowhow_rows r ON r.id = c.row_id"
    "  WHERE r.table_id = t.id ORDER BY c.row_id, c.column_id"
    ")) AS cells_signal, "
    "(SELECT group_concat(sig, char(30)) FROM ("
    "  SELECT cc.row_id || char(31) || cc.column_id || char(31) || cc.code_text"
    "    || char(31) || cc.language || char(31) || cc.cell_content_hash"
    "    || char(31) || cc.updated_by AS sig"
    "  FROM knowhow_cell_code cc JOIN knowhow_rows r ON r.id = cc.row_id"
    "  WHERE r.table_id = t.id ORDER BY cc.row_id, cc.column_id"
    ")) AS cell_code_signal "
    "FROM knowhow_tables t WHERE t.id = ?"
)

#: ASCII group separator，连接五个顶层信号后再哈希。
GROUP_SEP = "\x1d"


def fingerprint_on(db: sqlite3.Connection, table_id: str) -> "str | None":
    """在调用方给定的连接上算一次整表指纹；表不存在返回 None。"""
    row = db.execute(FINGERPRINT_SQL, (table_id,)).fetchone()
    if row is None:
        return None
    parts = [
        row["title"],
        row["description"],
        row["columns_signal"] or "",
        row["rows_signal"] or "",
        row["cells_signal"] or "",
        row["cell_code_signal"] or "",
    ]
    canonical = GROUP_SEP.join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: 改 `knowhow_transfer_store.py` 为别名**

删掉搬走的三处定义，在类体内改成别名（保留 `table_fingerprint` 公开方法不动，它内部改调共享函数）：

```python
from app.repositories.sqlite import knowhow_fingerprint

# ... 类体内 ...
    #: 搬到 knowhow_fingerprint 后的同对象别名——既有引用（含测试）不受影响。
    #: 改 SQL 请去 knowhow_fingerprint.py，那里写明了它现在有两个身份。
    _FINGERPRINT_SQL = knowhow_fingerprint.FINGERPRINT_SQL
    _GROUP_SEP = knowhow_fingerprint.GROUP_SEP

    @classmethod
    def _fingerprint_on(cls, db: sqlite3.Connection, table_id: str) -> "str | None":
        return knowhow_fingerprint.fingerprint_on(db, table_id)
```

`table_fingerprint` 方法体保持原样（它调 `self._fingerprint_on`）。**把原来 `table_fingerprint` docstring 里那段"三轮评审各补一个字段"的历史保留原处**。

- [ ] **Step 5: 跑新测试 + 传输相关的全部既有测试**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_fingerprint.py \
  backend/tests/test_knowhow_transfer_store.py \
  backend/tests/test_knowhow_transfer_service.py \
  backend/tests/test_knowhow_transfer_routes.py -n0 -q
```
Expected: 全 PASS。**任何一条传输测试变红都说明搬迁不是等价重构，必须回到 Step 3 逐字节对照，不要改测试。**

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_fingerprint.py \
        backend/app/repositories/sqlite/knowhow_transfer_store.py \
        backend/tests/test_knowhow_fingerprint.py
git commit -m "refactor(knowhow): 抽出共享整表指纹模块"
```

---

## Task 2: 迁移 24 —— 两张新表

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py:15`（`SCHEMA_VERSION`）、`:1538` 后插入 `_migration_24`
- Test: `backend/tests/test_knowhow_history_schema.py`（新建）

**Interfaces:**
- Produces: 表 `knowhow_changes`、`knowhow_milestones`（DDL 见下），`SCHEMA_VERSION == 24`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_history_schema.py`：

```python
from __future__ import annotations

import pytest

from app.config import Settings
from app.repositories.sqlite import migrations as sqlite_migrations
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def test_schema_version_is_24():
    assert sqlite_migrations.SCHEMA_VERSION == 24


def _columns(repo, table: str) -> dict[str, str]:
    with repo._runtime.database.connect() as db:
        return {
            row["name"]: row["type"]
            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }


def test_knowhow_changes_table_shape(repo):
    columns = _columns(repo, "knowhow_changes")
    assert columns == {
        "id": "TEXT",
        "table_id": "TEXT",
        "seq": "INTEGER",
        "kind": "TEXT",
        "actor": "TEXT",
        "origin": "TEXT",
        "payload_json": "TEXT",
        "fingerprint": "TEXT",
        "note": "TEXT",
        "created_at": "TEXT",
    }


def test_knowhow_milestones_table_shape(repo):
    columns = _columns(repo, "knowhow_milestones")
    assert columns == {
        "id": "TEXT",
        "table_id": "TEXT",
        "seq": "INTEGER",
        "name": "TEXT",
        "note": "TEXT",
        "created_by": "TEXT",
        "created_at": "TEXT",
    }


def test_indexes_exist(repo):
    with repo._runtime.database.connect() as db:
        names = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_knowhow_changes_table" in names
    assert "idx_knowhow_milestones_table" in names


def test_changes_seq_is_unique_per_table(repo):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, description,"
            " created_at, updated_at) VALUES ('t1','nb','x','','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
            " fingerprint, created_at) VALUES ('c1','t1',1,'cell_update','{}','f','now')"
        )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
                " fingerprint, created_at) VALUES ('c2','t1',1,'cell_update','{}','f','now')"
            )


def test_changes_cascade_with_table(repo):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, description,"
            " created_at, updated_at) VALUES ('t2','nb','x','','now','now')"
        )
        db.execute(
            "INSERT INTO knowhow_changes (id, table_id, seq, kind, payload_json,"
            " fingerprint, created_at) VALUES ('c3','t2',1,'cell_update','{}','f','now')"
        )
        db.execute("DELETE FROM knowhow_tables WHERE id='t2'")
        left = db.execute(
            "SELECT COUNT(*) AS n FROM knowhow_changes WHERE table_id='t2'"
        ).fetchone()["n"]
    assert left == 0
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_schema.py -n0 -q
```
Expected: FAIL — `assert 23 == 24`，以及 `no such table: knowhow_changes`

- [ ] **Step 3: 写迁移**

`backend/app/repositories/sqlite/migrations.py:15` 改成 `SCHEMA_VERSION = 24`。

在 `_migration_23` 之后、`_recover_interrupted_jobs` 之前（约 `L1538` 与 `L1540` 之间）插入：

```python
    def _migration_24(self) -> None:
        """knowhow 表版本管理：变更流水 + 命名里程碑。

        设计见 docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md。
        与 _migration_16/_migration_17 同款两层写法（仅新建表，不改 _migration_1
        baseline；全新表无历史行，无列序顾虑）。

        knowhow_milestones.seq 刻意**不设** FK 到 knowhow_changes：流水被
        "清理历史"删除后里程碑要保留为"已失效标记"（灰显、不可回退），
        级联删掉用户亲手命名过的东西是不可接受的。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowhow_changes (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  actor TEXT NOT NULL DEFAULT '',
                  origin TEXT NOT NULL DEFAULT 'user',
                  payload_json TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, seq)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_changes_table
                  ON knowhow_changes(table_id, seq DESC);

                CREATE TABLE IF NOT EXISTS knowhow_milestones (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_knowhow_milestones_table
                  ON knowhow_milestones(table_id, seq DESC);
                """
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_schema.py -n0 -q
```
Expected: 6 passed

- [ ] **Step 5: 修所有硬编码 `== 23` 的断言**

逐个把 23 改成 24（函数名里的数字也要改）：

| 文件 | 位置 |
|---|---|
| `backend/tests/test_legacy_db_compat.py` | `:59`、`:74` |
| `backend/tests/test_memory_kg_schema.py` | `:53` 函数名 `test_schema_version_is_23`、`:63` 断言 |
| `backend/tests/test_multi_domain_bases.py` | `:24-25` |
| `backend/tests/test_sqlite_migrator_component.py` | `:16` 函数名 `test_schema_version_constant_is_v23`、`:27` 断言；函数上方的逐版本历史注释追加一句 v24 |
| `backend/tests/test_source_asset_migration.py` | `:48` |
| `backend/tests/test_repository_v9_fixture.py` | `:102` |

重刷 schema golden：

```bash
cd backend && UPDATE_SCHEMA_GOLDEN=1 python3 -m pytest tests/test_legacy_db_compat.py -k contract -n0 -q
```

- [ ] **Step 6: 加 `MIGRATION_MANIFEST` 的 (23,24) hop**

在 `scripts/verify_repository_snapshot.py` 现有 `MIGRATION_MANIFEST[(22, 23)] = {...}` 块之后追加。**SQL 文本必须与 `_migration_24` 的 DDL 逐字节一致但去掉 `IF NOT EXISTS`**（`sqlite_master.sql` 存的时候会剥掉它，比较是逐字节字符串比较）：

```python
# v24: knowhow 表版本管理——变更流水 + 命名里程碑。
KNOWHOW_HISTORY_TABLES = {
    "knowhow_changes": """CREATE TABLE knowhow_changes (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  actor TEXT NOT NULL DEFAULT '',
                  origin TEXT NOT NULL DEFAULT 'user',
                  payload_json TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, seq)
                )""",
    "knowhow_milestones": """CREATE TABLE knowhow_milestones (
                  id TEXT PRIMARY KEY,
                  table_id TEXT NOT NULL REFERENCES knowhow_tables(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  note TEXT NOT NULL DEFAULT '',
                  created_by TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(table_id, name)
                )""",
}
KNOWHOW_HISTORY_INDEXES = {
    "idx_knowhow_changes_table":
        """CREATE INDEX idx_knowhow_changes_table
                  ON knowhow_changes(table_id, seq DESC)""",
    "idx_knowhow_milestones_table":
        """CREATE INDEX idx_knowhow_milestones_table
                  ON knowhow_milestones(table_id, seq DESC)""",
}
MIGRATION_MANIFEST = {
    (key[0], 24, *key[2:]): {
        **manifest,
        "tables": {**manifest["tables"], **KNOWHOW_HISTORY_TABLES},
        "indexes": {**manifest["indexes"], **KNOWHOW_HISTORY_INDEXES},
    }
    for key, manifest in MIGRATION_MANIFEST.items()
}
MIGRATION_MANIFEST[(23, 24)] = {
    "tables": KNOWHOW_HISTORY_TABLES,
    "columns": {},
    "indexes": KNOWHOW_HISTORY_INDEXES,
    "triggers": {},
    "views": {},
}
```

- [ ] **Step 7: 修快照回放测试**

`backend/tests/test_repository_snapshot_verifier.py` 里 3 个既有回放测试的 rollback SQL 各补 4 条 DROP：

```python
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
```

要改的三个：`test_deployed_v13_database_verifies_through_migrations_14_to_23`、`test_deployed_v20_database_verifies_through_migrations_21_to_23`、`test_deployed_v21_database_verifies_through_migrations_22_and_23`（函数名里的 `_to_23` / `_and_23` 一并改成 24）。

再仿 `test_deployed_v22_database_verifies_through_model_service_status`（`:474-494`）新增：

```python
def test_deployed_v23_database_verifies_through_knowhow_history(tmp_path):
    module = _load_verifier()
    database, storage = _copy_fixture(tmp_path)

    upgraded = module.SQLiteRepository(
        module.offline_settings(database, tmp_path / "upgrade-storage")
    )
    upgraded.close_local()
    rollback = sqlite3.connect(database)
    try:
        rollback.execute("DROP INDEX idx_knowhow_milestones_table")
        rollback.execute("DROP INDEX idx_knowhow_changes_table")
        rollback.execute("DROP TABLE knowhow_milestones")
        rollback.execute("DROP TABLE knowhow_changes")
        rollback.execute("PRAGMA user_version = 23")
        rollback.commit()
    finally:
        rollback.close()

    result = module.verify_snapshot(database, storage)

    assert result.ok, result.discrepancies
    assert result.source_user_version == 23
    assert result.final_user_version == module.SCHEMA_VERSION
```

- [ ] **Step 8: 刷 v9 快照投影 + 跑全部 schema 相关测试**

```bash
PYTHONPATH=backend python3 scripts/generate_repository_contract_fixtures.py
PYTHONPATH=backend python3 -m pytest \
  backend/tests/test_knowhow_history_schema.py \
  backend/tests/test_legacy_db_compat.py \
  backend/tests/test_memory_kg_schema.py \
  backend/tests/test_multi_domain_bases.py \
  backend/tests/test_sqlite_migrator_component.py \
  backend/tests/test_source_asset_migration.py \
  backend/tests/test_repository_v9_fixture.py \
  backend/tests/test_repository_snapshot_verifier.py -q
```
Expected: 全 PASS

- [ ] **Step 9: 更文档版本号**

四处，把 `23` 改 `24`、`v10–v23` 改 `v10–v24`，并在枚举句尾追加 v24 的说明：

- `README.md:45-46`：`The current schema version is 24.` / `upgrades through migrations v10–v24`；句尾加 `v24 adds knowhow table change history and named milestones.`
- `AGENTS.md:159-160`：同英文
- `README_zh.md:45`：`当前 schema 版本为 24。…经由 v10–v24 migration 升级…`；句尾加 `v24 增加 knowhow 表变更流水与命名里程碑。`
- `architecture.md:47`：同中文

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_architecture_documentation.py -n0 -q
```
Expected: PASS

- [ ] **Step 10: 提交**

```bash
git add -A
git commit -m "feat(knowhow): 迁移 24 建变更流水与里程碑两表"
```

---

## Task 3: `record_change` 与历史 store 骨架

**Files:**
- Create: `backend/app/repositories/sqlite/knowhow_history_store.py`
- Test: `backend/tests/test_knowhow_history_store.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `knowhow_fingerprint.fingerprint_on`
- Produces:
  - `record_change(db, *, new_id, now, table_id, kind, payload, actor="", origin="user", note="") -> int`（返回 seq）
  - `KnowhowHistoryStore(database, *, new_id, now)`，方法：`list_changes(table_id, limit=50, before_seq=None) -> list[dict]`、`get_change(table_id, seq) -> dict | None`、`head_seq(table_id) -> int`、`cell_history(table_id, row_id, column_id, limit=50) -> list[dict]`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_history_store.py`：

```python
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_history_store as history
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


@pytest.fixture
def table_id(repo, notebook_id) -> str:
    return repo._runtime.knowhow_store.create_knowhow_table(
        notebook_id, "表", "", [{"name": "概念", "role": "anchor"}]
    )


@pytest.fixture
def store(repo) -> history.KnowhowHistoryStore:
    return repo._runtime.knowhow_history_store


def _record(repo, table_id, **kwargs):
    runtime = repo._runtime
    with runtime.database.write() as db:
        return history.record_change(
            db,
            new_id=runtime.knowhow_store.new_id,
            now=runtime.knowhow_store.now,
            table_id=table_id,
            **kwargs,
        )


def test_seq_starts_at_one_and_increments(repo, table_id):
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == 1
    assert _record(repo, table_id, kind="cell_update", payload={"cells": []}) == 2


def test_records_fingerprint_of_state_after_the_change(repo, table_id):
    from app.repositories.sqlite import knowhow_fingerprint

    _record(repo, table_id, kind="table_create", payload={})
    with repo._runtime.database.connect() as db:
        expected = knowhow_fingerprint.fingerprint_on(db, table_id)
        stored = db.execute(
            "SELECT fingerprint FROM knowhow_changes WHERE table_id=? AND seq=1",
            (table_id,),
        ).fetchone()["fingerprint"]
    assert stored == expected


def test_actor_origin_note_round_trip(repo, table_id, store):
    _record(
        repo, table_id,
        kind="cell_update", payload={"cells": []},
        actor="user-abc", origin="llm_reformat", note="批量规整",
    )
    change = store.get_change(table_id, 1)
    assert change["actor"] == "user-abc"
    assert change["origin"] == "llm_reformat"
    assert change["note"] == "批量规整"
    assert change["payload"] == {"cells": []}


def test_list_changes_is_newest_first_and_paginates(repo, table_id, store):
    for _ in range(5):
        _record(repo, table_id, kind="cell_update", payload={"cells": []})

    newest = store.list_changes(table_id, limit=2)
    assert [c["seq"] for c in newest] == [5, 4]

    older = store.list_changes(table_id, limit=2, before_seq=4)
    assert [c["seq"] for c in older] == [3, 2]


def test_head_seq_is_zero_for_a_table_with_no_history(repo, table_id, store):
    assert store.head_seq(table_id) == 0


def test_cell_history_filters_to_one_cell_newest_first(repo, table_id, store):
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": None, "after": "一"}]
    })
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r2", "column_id": "c1", "before": None, "after": "别的行"}]
    })
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [{"row_id": "r1", "column_id": "c1", "before": "一", "after": "二"}]
    })

    entries = store.cell_history(table_id, "r1", "c1")
    assert [e["seq"] for e in entries] == [3, 1]
    assert entries[0]["after"] == "二"
    assert entries[0]["before"] == "一"


def test_cell_history_finds_the_cell_inside_a_multi_cell_batch(repo, table_id, store):
    """合并格批量写是一条流水里多个 cells 条目——不能只看第一个。"""
    _record(repo, table_id, kind="cell_update", payload={
        "cells": [
            {"row_id": "rA", "column_id": "c1", "before": "旧A", "after": "新"},
            {"row_id": "rB", "column_id": "c1", "before": "旧B", "after": "新"},
        ]
    })
    entries = store.cell_history(table_id, "rB", "c1")
    assert len(entries) == 1
    assert entries[0]["before"] == "旧B"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_store.py -n0 -q
```
Expected: FAIL — `ModuleNotFoundError: ... knowhow_history_store`

- [ ] **Step 3: 写 store**

创建 `backend/app/repositories/sqlite/knowhow_history_store.py`：

```python
"""knowhow 表版本管理的持久化层：变更流水 + 命名里程碑 + 回退重放。

设计见 docs/superpowers/specs/2026-07-22-knowhow-table-version-control-design.md。

record_change 刻意做成**模块级函数而非类方法**：它必须在 KnowhowStore
已经打开的写事务里执行（流水与变更本体同生共死），做成类就要在组合根里
接线并让 KnowhowStore 持有引用。模块级函数零状态、零接线，把 new_id/now
当参数传进来即可。自带事务的操作（查询/里程碑/prune/回退）才归下面的类。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.database import SqliteDatabase


def record_change(
    db: sqlite3.Connection,
    *,
    new_id: Callable[[str], str],
    now: Callable[[], str],
    table_id: str,
    kind: str,
    payload: dict,
    actor: str = "",
    origin: str = "user",
    note: str = "",
) -> int:
    """在调用方已开的写事务里追加一条流水，返回它的 seq。

    **必须是写事务的最后一步** —— fingerprint 要反映本次变更之后的表状态。
    放在变更 DML 之前会记下变更前的指纹，让回退的前后置守卫全部失准。

    seq 用 ``COALESCE(MAX(seq),0)+1`` 现算：调用方已持写锁，同一张表上不会
    有第二个写事务同时算，UNIQUE(table_id, seq) 是最后一道保险。
    """
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM knowhow_changes WHERE table_id = ?",
        (table_id,),
    ).fetchone()
    seq = int(row["next"])
    db.execute(
        "INSERT INTO knowhow_changes "
        "(id, table_id, seq, kind, actor, origin, payload_json, fingerprint, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id("khchg"),
            table_id,
            seq,
            kind,
            actor or "",
            origin or "user",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            knowhow_fingerprint.fingerprint_on(db, table_id) or "",
            note or "",
            now(),
        ),
    )
    return seq


def _row_to_change(row: sqlite3.Row) -> dict:
    change = dict(row)
    change["payload"] = json.loads(change.pop("payload_json"))
    return change


class KnowhowHistoryStore:
    """流水/里程碑的读侧与自带事务的写侧。"""

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

    def head_seq(self, table_id: str) -> int:
        """当前最新流水序号；没有历史时返回 0。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
        return int(row["head"])

    def list_changes(
        self, table_id: str, limit: int = 50, before_seq: "int | None" = None
    ) -> list[dict]:
        """时间线：seq 倒序。``before_seq`` 用于向更旧翻页（严格小于）。"""
        sql = "SELECT * FROM knowhow_changes WHERE table_id = ?"
        params: list[Any] = [table_id]
        if before_seq is not None:
            sql += " AND seq < ?"
            params.append(int(before_seq))
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit))
        with self.database.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [_row_to_change(row) for row in rows]

    def get_change(self, table_id: str, seq: int) -> "dict | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, int(seq)),
            ).fetchone()
        return _row_to_change(row) if row is not None else None

    def changes_between(self, table_id: str, from_seq: int, to_seq: int) -> list[dict]:
        """区间 (from_seq, to_seq] 的流水，seq 升序（供 diff 聚合按时序折叠）。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq > ? AND seq <= ? "
                "ORDER BY seq ASC",
                (table_id, int(from_seq), int(to_seq)),
            ).fetchall()
        return [_row_to_change(row) for row in rows]

    def cell_history(
        self, table_id: str, row_id: str, column_id: str, limit: int = 50
    ) -> list[dict]:
        """一个格子的历次值，最新在前。

        先用 LIKE 把候选缩小到"payload 里提到过这个 row_id"的流水（索引不了
        JSON，但一张表的流水规模在百到千条量级，且 kind 过滤已挡掉结构类），
        再在 Python 侧精确匹配 (row_id, column_id) —— 合并格批量写把多个格子
        放在同一条流水的 cells 数组里，只看第一个会漏。
        """
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM knowhow_changes "
                "WHERE table_id = ? AND kind IN ('cell_update','import_append',"
                "'row_add','row_delete','column_delete','revert') "
                "AND payload_json LIKE ? "
                "ORDER BY seq DESC",
                (table_id, f"%{row_id}%"),
            ).fetchall()

        entries: list[dict] = []
        for row in rows:
            change = _row_to_change(row)
            for cell in change["payload"].get("cells", []):
                if cell.get("row_id") == row_id and cell.get("column_id") == column_id:
                    entries.append({
                        "seq": change["seq"],
                        "actor": change["actor"],
                        "origin": change["origin"],
                        "created_at": change["created_at"],
                        "before": cell.get("before"),
                        "after": cell.get("after"),
                    })
            if len(entries) >= limit:
                break
        return entries[:limit]
```

- [ ] **Step 4: 在组合根接线**

在 runtime 组合根里加 `knowhow_history_store`。先找到 `knowhow_store` 是在哪里构造的：

```bash
grep -rn "knowhow_store = \|knowhow_transfer_store = " backend/app --include="*.py"
```

照 `knowhow_store` 的构造方式在同一处加：

```python
        self.knowhow_history_store = KnowhowHistoryStore(
            self.database, new_id=_new_id, now=_now
        )
```

（`_new_id`/`_now` 用与 `knowhow_store` 构造时**完全相同**的两个可调用对象。）

- [ ] **Step 5: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_store.py -n0 -q
```
Expected: 8 passed

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_history_store.py \
        backend/tests/test_knowhow_history_store.py backend/app
git commit -m "feat(knowhow): 变更流水 record_change 与历史读取 store"
```

---

## Task 4: 挂钩格子类写方法

给 4 个格子写方法挂上流水。它们是最高频路径，也是唯一 `before` 已经现成的一批（两个 guarded 方法在 phase 1 已经读过当前内容）。

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_store.py`（`update_knowhow_cell` `:678`、`update_knowhow_cells` `:722`、`update_knowhow_cells_bulk_guarded` `:778`、`update_knowhow_cells_guarded_atomic` `:929`）
- Test: `backend/tests/test_knowhow_history_hooks.py`（新建）

**Interfaces:**
- Consumes: Task 3 的 `record_change`
- Produces: 上述 4 个方法新增关键字参数 `actor: str = ""`, `origin: str = "user"`；每次真实写入产生一条 `kind="cell_update"` 流水，payload 形如 `{"cells": [{"row_id","column_id","before","after"}, ...]}`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_history_hooks.py`：

```python
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


@pytest.fixture
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    anchor = detail["columns"][0]["id"]
    plain = detail["columns"][1]["id"]
    row_a = store.add_knowhow_row(table_id, {anchor: "A"})
    row_b = store.add_knowhow_row(table_id, {anchor: "A"})
    return {
        "id": table_id, "anchor": anchor, "plain": plain,
        "row_a": row_a, "row_b": row_b,
    }


def _cell_changes(hist, table_id):
    return [c for c in hist.list_changes(table_id, limit=100) if c["kind"] == "cell_update"]


def test_update_cell_records_before_and_after(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "第一版")
    store.update_knowhow_cell(table["row_a"], table["plain"], "第二版")

    changes = _cell_changes(hist, table["id"])
    assert len(changes) == 2
    latest = changes[0]["payload"]["cells"]
    assert latest == [{
        "row_id": table["row_a"], "column_id": table["plain"],
        "before": "第一版", "after": "第二版",
    }]
    first = changes[1]["payload"]["cells"]
    assert first[0]["before"] is None, "格子当时不存在，before 必须是 None 而非空串"
    assert first[0]["after"] == "第一版"


def test_update_cell_carries_actor_and_origin(store, hist, table):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], "x", actor="user-1", origin="llm_optimize"
    )
    change = _cell_changes(hist, table["id"])[0]
    assert change["actor"] == "user-1"
    assert change["origin"] == "llm_optimize"


def test_batch_write_is_one_change_with_every_row(store, hist, table):
    store.update_knowhow_cells(
        [table["row_a"], table["row_b"]], table["plain"], "共享值"
    )
    changes = _cell_changes(hist, table["id"])
    assert len(changes) == 1, "合并格批量写必须记一条，不是每行一条"
    cells = sorted(changes[0]["payload"]["cells"], key=lambda c: c["row_id"])
    assert [c["row_id"] for c in cells] == sorted([table["row_a"], table["row_b"]])
    assert all(c["after"] == "共享值" for c in cells)


def test_empty_batch_records_nothing(store, hist, table):
    store.update_knowhow_cells([], table["plain"], "x")
    assert _cell_changes(hist, table["id"]) == []


def test_guarded_atomic_records_one_change_on_success(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "基线")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_guarded_atomic(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], "基线", "新值")],
    )
    assert result["conflict"] is False
    changes = _cell_changes(hist, table["id"])
    assert len(changes) == before_count + 1
    assert changes[0]["payload"]["cells"][0]["before"] == "基线"
    assert changes[0]["payload"]["cells"][0]["after"] == "新值"


def test_guarded_atomic_conflict_records_nothing(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "真实值")
    before_count = len(_cell_changes(hist, table["id"]))

    result = store.update_knowhow_cells_guarded_atomic(
        _notebook_of(store, table["id"]),
        [(table["id"], table["row_a"], table["plain"], "过期基线", "新值")],
    )
    assert result["conflict"] is True
    assert len(_cell_changes(hist, table["id"])) == before_count, (
        "冲突时什么都没写，流水也不能有"
    )


def _notebook_of(store, table_id: str) -> str:
    return store.get_knowhow_table(table_id)["notebook_id"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py -n0 -q
```
Expected: FAIL — 流水为空（`assert 0 == 2`）

- [ ] **Step 3: 挂钩 `update_knowhow_cell`**

在 `backend/app/repositories/sqlite/knowhow_store.py` 顶部加 import：

```python
from app.repositories.sqlite.knowhow_history_store import record_change
```

改 `update_knowhow_cell`（签名加两个参数，事务里先读 before、最后记流水）：

```python
    def update_knowhow_cell(
        self,
        row_id: str,
        column_id: str,
        content_md: str,
        require_assets: Sequence[str] = (),
        actor: str = "",
        origin: str = "user",
    ) -> None:
        now = self.now()
        with self.database.write() as db:
            self._require_assets_exist(db, row_id, require_assets)
            before_row = db.execute(
                "SELECT content_md FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
                (row_id, column_id),
            ).fetchone()
            before = before_row["content_md"] if before_row is not None else None
            db.execute(
                "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(row_id, column_id) DO UPDATE SET "
                "content_md = excluded.content_md, updated_at = excluded.updated_at",
                (self.new_id("khcel"), row_id, column_id, content_md, now),
            )
            db.execute(
                "UPDATE knowhow_rows SET updated_at = ?, projection_status = 'pending' "
                "WHERE id = ?",
                (now, row_id),
            )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1 "
                "WHERE id = (SELECT table_id FROM knowhow_rows WHERE id = ?)",
                (row_id,),
            )
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_id,)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db,
                    new_id=self.new_id,
                    now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_update",
                    payload={"cells": [{
                        "row_id": row_id, "column_id": column_id,
                        "before": before, "after": content_md,
                    }]},
                    actor=actor,
                    origin=origin,
                )
```

**在原 docstring 末尾追加一段**说明流水挂钩（保留原有全部文字）：

```
        版本管理（spec §5）：本方法在同一事务的最后追加一条 ``cell_update``
        流水，``before`` 取写入前的 ``content_md``（格子当时不存在则为
        ``None``，与空串区分——回退时 ``None`` 意味着"把这格删掉"）。
```

- [ ] **Step 4: 挂钩 `update_knowhow_cells`**

同样加 `actor`/`origin` 参数；在循环前收集每行的 before，循环后记**一条**流水：

```python
        with self.database.write() as db:
            self._require_assets_exist(db, row_ids[0], require_assets)
            cells: list[dict] = []
            for row_id in row_ids:
                before_row = db.execute(
                    "SELECT content_md FROM knowhow_cells WHERE row_id = ? AND column_id = ?",
                    (row_id, column_id),
                ).fetchone()
                cells.append({
                    "row_id": row_id, "column_id": column_id,
                    "before": before_row["content_md"] if before_row is not None else None,
                    "after": content_md,
                })
                # ... 原有的两条 db.execute 保持不变 ...
            # ... 原有的 mutation_seq bump 保持不变 ...
            table_row = db.execute(
                "SELECT table_id FROM knowhow_rows WHERE id = ?", (row_ids[0],)
            ).fetchone()
            if table_row is not None:
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=table_row["table_id"],
                    kind="cell_update", payload={"cells": cells},
                    actor=actor, origin=origin,
                )
```

- [ ] **Step 5: 挂钩两个 guarded 方法**

这两个方法在 phase 1 已经把每个目标的当前内容读出来比对了 —— **复用那次读到的值当 before，不要再读一遍**。

`update_knowhow_cells_guarded_atomic`：phase 1 比对时把 `(table_id, row_id, column_id, expected_before, content_md)` 里已验证相等的 `expected_before` 收进一个 `by_table: dict[str, list[dict]]`；phase 2 全部写完、每张表 bump 完 `mutation_seq` 之后，**对每张被写的表各记一条流水**：

```python
            for written_table_id, cells in by_table.items():
                record_change(
                    db, new_id=self.new_id, now=self.now,
                    table_id=written_table_id,
                    kind="cell_update", payload={"cells": cells},
                    actor=actor, origin=origin,
                )
```

冲突路径在 phase 1 就 return 了，天然不记流水（测试 `test_guarded_atomic_conflict_records_nothing` 守这一点）。

`update_knowhow_cells_bulk_guarded` 同理，但它是"收集 skip 继续跑"的语义 —— **只把真正写成功的条目放进 payload**。

- [ ] **Step 6: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py \
  backend/tests/test_knowhow_store.py backend/tests/test_knowhow_editing_api.py -n0 -q
```
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_store.py backend/tests/test_knowhow_history_hooks.py
git commit -m "feat(knowhow): 格子写路径挂上变更流水"
```

---

## Task 5: 挂钩行与列的增删改

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_store.py`（`add_knowhow_row` `:573`、`delete_knowhow_row` `:634`、`add_knowhow_column` `:208`、`rename_knowhow_column` `:253`、`set_knowhow_column_kind` `:277`、`delete_knowhow_column` `:298`、`set_knowhow_anchor_column` `:161`）
- Test: `backend/tests/test_knowhow_history_hooks.py`（追加）

**Interfaces:**
- Produces: 各方法新增 `actor`/`origin` 关键字参数，产生 `row_add` / `row_delete` / `column_add` / `column_rename` / `column_kind` / `column_delete` / `anchor_set` 流水，payload 形状见 spec §4.4

- [ ] **Step 1: 写失败测试**（追加到 `test_knowhow_history_hooks.py`）

```python
def _kinds(hist, table_id):
    return [c["kind"] for c in hist.list_changes(table_id, limit=100)]


def test_delete_row_stores_whole_row_for_reversal(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "要被删掉的内容")
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "hash-x"
    )
    store.delete_knowhow_row(table["row_a"])

    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_delete"
    row = change["payload"]["rows"][0]
    assert row["row_id"] == table["row_a"]
    assert row["cells"][table["plain"]] == "要被删掉的内容"
    assert row["cells"][table["anchor"]] == "A"
    assert row["code"][0]["code_text"] == "print(1)", (
        "代码附件随行 CASCADE 消失，不存进 payload 就永远回不来"
    )
    assert isinstance(row["position"], int)


def test_delete_missing_row_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.delete_knowhow_row("khrow-does-not-exist")
    assert len(hist.list_changes(table["id"], limit=100)) == before


def test_delete_column_stores_column_and_all_its_cells(store, hist, table):
    store.update_knowhow_cell(table["row_a"], table["plain"], "甲")
    store.update_knowhow_cell(table["row_b"], table["plain"], "乙")
    store.delete_knowhow_column(table["plain"])

    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_delete"
    assert change["payload"]["column"]["id"] == table["plain"]
    assert change["payload"]["column"]["name"] == "做法"
    assert change["payload"]["column"]["role"] == "attribute"
    contents = {c["row_id"]: c["content_md"] for c in change["payload"]["cells"]}
    assert contents == {table["row_a"]: "甲", table["row_b"]: "乙"}


def test_add_row_records_its_cells(store, hist, table):
    row_id = store.add_knowhow_row(table["id"], {table["anchor"]: "新概念"})
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "row_add"
    assert change["payload"]["rows"][0]["row_id"] == row_id
    assert change["payload"]["rows"][0]["cells"][table["anchor"]] == "新概念"


def test_column_rename_records_before_after(store, hist, table):
    store.rename_knowhow_column(table["plain"], "新列名")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "column_rename"
    assert change["payload"] == {
        "column_id": table["plain"], "before": "做法", "after": "新列名",
    }


def test_renaming_to_the_same_name_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.rename_knowhow_column(table["plain"], "做法")
    assert len(hist.list_changes(table["id"], limit=100)) == before, (
        "同名改名是既有的静默成功语义，不该产生噪声流水"
    )


def test_anchor_move_records_both_columns(store, hist, table):
    store.set_knowhow_anchor_column(table["id"], table["plain"])
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "anchor_set"
    moves = {c["column_id"]: (c["before"], c["after"]) for c in change["payload"]["columns"]}
    assert moves[table["anchor"]] == ("anchor", "attribute")
    assert moves[table["plain"]] == ("attribute", "anchor")


def test_anchor_noop_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.set_knowhow_anchor_column(table["id"], table["anchor"])
    assert len(hist.list_changes(table["id"], limit=100)) == before
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py -n0 -q -k "row or column or anchor"
```
Expected: FAIL

- [ ] **Step 3: 挂钩删除类（最要紧，必须存全）**

`delete_knowhow_row`：

```python
    def delete_knowhow_row(self, row_id: str, actor: str = "", origin: str = "user") -> None:
        with self.database.write() as db:
            row = db.execute(
                "SELECT id, table_id, position FROM knowhow_rows WHERE id = ?", (row_id,)
            ).fetchone()
            if row is None:
                return  # 既有的静默 no-op 语义：没删成什么，也就没有变更可记
            cells = {
                c["column_id"]: c["content_md"]
                for c in db.execute(
                    "SELECT column_id, content_md FROM knowhow_cells WHERE row_id = ?",
                    (row_id,),
                ).fetchall()
            }
            code = [
                dict(c)
                for c in db.execute(
                    "SELECT column_id, code_text, language, updated_by, cell_content_hash "
                    "FROM knowhow_cell_code WHERE row_id = ?",
                    (row_id,),
                ).fetchall()
            ]
            db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row_id,))
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=row["table_id"],
                kind="row_delete",
                payload={"rows": [{
                    "row_id": row["id"], "position": row["position"],
                    "cells": cells, "code": code,
                }]},
                actor=actor, origin=origin,
            )
```

`delete_knowhow_column` 同款，但要多存该列在**所有行**上的格子与代码附件：

```python
    def delete_knowhow_column(self, column_id: str, actor: str = "", origin: str = "user") -> None:
        with self.database.write() as db:
            column = db.execute(
                "SELECT id, table_id, name, role, position FROM knowhow_columns WHERE id = ?",
                (column_id,),
            ).fetchone()
            if column is None:
                return
            cells = [
                dict(c)
                for c in db.execute(
                    "SELECT row_id, content_md FROM knowhow_cells WHERE column_id = ?",
                    (column_id,),
                ).fetchall()
            ]
            code = [
                dict(c)
                for c in db.execute(
                    "SELECT row_id, code_text, language, updated_by, cell_content_hash "
                    "FROM knowhow_cell_code WHERE column_id = ?",
                    (column_id,),
                ).fetchall()
            ]
            db.execute("DELETE FROM knowhow_columns WHERE id = ?", (column_id,))
            record_change(
                db, new_id=self.new_id, now=self.now, table_id=column["table_id"],
                kind="column_delete",
                payload={
                    "column": {
                        "id": column["id"], "name": column["name"],
                        "role": column["role"], "position": column["position"],
                    },
                    "cells": cells, "code": code,
                },
                actor=actor, origin=origin,
            )
```

- [ ] **Step 4: 挂钩新增与修改类**

- `add_knowhow_row`：在方法末尾（事务内）记 `row_add`，payload 的 `cells` 就是入参 `cells or {}`，`code` 为 `[]`（新行没有附件）。
- `add_knowhow_column`：记 `column_add`，payload `{"column": {"id","name","role","position"}}`。
- `rename_knowhow_column`：先读当前 `name`，**相等则不记**（既有静默成功语义），否则记 `column_rename`。
- `set_knowhow_column_kind`：先读当前 `role`，相等不记，否则记 `column_kind`（payload 同 rename 形状）。
- `set_knowhow_anchor_column`：`column_id == old_id` 的 no-op 分支**原样 return，不记**；真正移动时记 `anchor_set`，`columns` 数组含被降级的旧 anchor（`("anchor","attribute")`）与被提升的新列（`(旧role,"anchor")`）。旧列的 `before` role 要在 UPDATE 之前读出来。

- [ ] **Step 5: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py \
  backend/tests/test_knowhow_store.py backend/tests/test_knowhow_editing_api.py -n0 -q
```
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_store.py backend/tests/test_knowhow_history_hooks.py
git commit -m "feat(knowhow): 行列增删改挂上变更流水"
```

---

## Task 6: 挂钩建表、表元信息与代码附件

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_store.py`（`create_knowhow_table` `:66`、`update_knowhow_table_meta` `:124`、`upsert_knowhow_cell_code` `:1206`、`delete_knowhow_cell_code` `:1257`）
- Test: `backend/tests/test_knowhow_history_hooks.py`（追加）

**Interfaces:**
- Produces: `table_create` / `table_meta` / `cell_code_put` / `cell_code_delete` 流水

- [ ] **Step 1: 写失败测试**（追加）

```python
def test_create_table_records_genesis_change(store, hist, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "新表", "说明", [{"name": "概念", "role": "anchor"}]
    )
    changes = hist.list_changes(table_id, limit=10)
    assert len(changes) == 1
    assert changes[0]["kind"] == "table_create"
    assert changes[0]["seq"] == 1
    assert changes[0]["payload"]["table"] == {"title": "新表", "description": "说明"}
    assert changes[0]["payload"]["columns"][0]["name"] == "概念"
    assert changes[0]["payload"]["rows"] == []


def test_table_meta_records_before_after(store, hist, table):
    store.update_knowhow_table_meta(table["id"], title="改了标题")
    change = hist.list_changes(table["id"], limit=1)[0]
    assert change["kind"] == "table_meta"
    assert change["payload"]["before"]["title"] == "表"
    assert change["payload"]["after"]["title"] == "改了标题"
    assert change["payload"]["before"]["description"] == ""
    assert change["payload"]["after"]["description"] == ""


def test_table_meta_noop_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.update_knowhow_table_meta(table["id"])
    assert len(hist.list_changes(table["id"], limit=100)) == before


def test_cell_code_put_and_delete_round_trip(store, hist, table):
    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(1)", "python", "user-1", "h1"
    )
    put = hist.list_changes(table["id"], limit=1)[0]
    assert put["kind"] == "cell_code_put"
    assert put["payload"]["before"] is None
    assert put["payload"]["after"]["code_text"] == "print(1)"

    store.upsert_knowhow_cell_code(
        table["row_a"], table["plain"], "print(2)", "python", "user-1", "h2"
    )
    updated = hist.list_changes(table["id"], limit=1)[0]
    assert updated["payload"]["before"]["code_text"] == "print(1)"
    assert updated["payload"]["after"]["code_text"] == "print(2)"

    store.delete_knowhow_cell_code(table["row_a"], table["plain"])
    removed = hist.list_changes(table["id"], limit=1)[0]
    assert removed["kind"] == "cell_code_delete"
    assert removed["payload"]["before"]["code_text"] == "print(2)"
    assert removed["payload"]["after"] is None


def test_deleting_absent_cell_code_records_nothing(store, hist, table):
    before = len(hist.list_changes(table["id"], limit=100))
    store.delete_knowhow_cell_code(table["row_a"], table["plain"])
    assert len(hist.list_changes(table["id"], limit=100)) == before
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py -n0 -q -k "create_table or table_meta or cell_code"
```
Expected: FAIL

- [ ] **Step 3: 实现四处挂钩**

- `create_knowhow_table`：在插完表与列之后、事务内记 `table_create`，payload：
  ```python
  payload={
      "table": {"title": title, "description": description or ""},
      "columns": [
          {"id": cid, "name": n, "role": k, "position": p}
          for p, (cid, n, k) in enumerate(created_columns)
      ],
      "rows": [],
  }
  ```
  （`created_columns` 需要在插入循环里顺手收集 `(column_id, name, kind)`。）
- `update_knowhow_table_meta`：现有代码在 `if not sets: return` 处提前返回 —— 那条路径不记。进了事务后先 `SELECT title, description`，UPDATE 之后记 `table_meta`，`after` 用"before 叠加本次 patch"算出来（`title`/`description` 任一为 `None` 表示不动）。
- `upsert_knowhow_cell_code`：UPSERT 之前先 `SELECT code_text, language, updated_by, cell_content_hash`，之后记 `cell_code_put`。需要 `row_id → table_id` 解析（`SELECT table_id FROM knowhow_rows WHERE id = ?`）。
- `delete_knowhow_cell_code`：DELETE 前先 SELECT，为空则直接 return 不记；否则记 `cell_code_delete`，`after` 为 `None`。

四个方法都加 `actor: str = ""`, `origin: str = "user"` 关键字参数。

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_hooks.py \
  backend/tests/test_knowhow_store.py backend/tests/test_knowhow_code_isolation.py -n0 -q
```
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_store.py backend/tests/test_knowhow_history_hooks.py
git commit -m "feat(knowhow): 建表/表元/代码附件挂上变更流水"
```

---

## Task 7: 防漏挂钩的架构守卫（含变异验证）

**Files:**
- Test: `backend/tests/test_knowhow_history_coverage_guard.py`（新建）

**Interfaces:**
- Consumes: Task 4-6 的挂钩成果
- Produces: 一条守卫，`knowhow_store.py` 里每个写事务块要么调 `record_change`，要么方法名在豁免白名单里

- [ ] **Step 1: 写守卫**

创建 `backend/tests/test_knowhow_history_coverage_guard.py`：

```python
"""守卫：KnowhowStore 里每一个写事务都必须记流水，除非显式豁免。

白名单是"允许不记"的封闭集合 —— 将来新增的写方法默认**报红**，逼着
作者显式决定它算不算用户可见变更。这正是 anchor 特性（PR#281→#286）
那次"宽容默认把 wire 错误降级成静默失败"的反面教训。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.repositories.sqlite import knowhow_store


#: 允许不记流水的方法 —— 它们不改用户可见内容。
EXEMPT_METHODS = frozenset({
    "bump_knowhow_mutation_seq",          # 纯投影调度计数器
    "set_knowhow_row_projection",          # 投影状态机
    "set_knowhow_row_projection_if_table_seq",
    "set_knowhow_hidden_source",           # 隐藏合成源接线
    "insert_notebook_asset",               # 资产表，不属于 knowhow 表内容
    "delete_source_asset_rows",
    "delete_knowhow_table",                # 表连同流水一起 CASCADE 消失
})


def _method_nodes() -> dict[str, ast.FunctionDef]:
    source = Path(inspect.getsourcefile(knowhow_store)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "KnowhowStore"
    )
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _opens_write_transaction(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.With):
            continue
        for item in child.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "write"
            ):
                return True
    return False


def _calls_record_change(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name) and func.id == "record_change":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "record_change":
                return True
    return False


def test_every_write_transaction_records_history_or_is_exempt():
    methods = _method_nodes()
    writers = {
        name: node for name, node in methods.items() if _opens_write_transaction(node)
    }

    assert writers, "没扫到任何写事务——守卫自身失效了，先修守卫"

    missing = sorted(
        name
        for name, node in writers.items()
        if name not in EXEMPT_METHODS and not _calls_record_change(node)
    )
    assert missing == [], (
        f"这些 KnowhowStore 写方法没有记变更流水：{missing}。"
        "要么在其写事务的最后调用 record_change，要么把它加进 EXEMPT_METHODS "
        "并在那里写清为什么它不算用户可见变更。"
    )


def test_exempt_list_has_no_stale_entries():
    """白名单里不能留下已经不存在、或已经不再开写事务的方法名。"""
    methods = _method_nodes()
    writers = {
        name for name, node in methods.items() if _opens_write_transaction(node)
    }
    stale = sorted(EXEMPT_METHODS - writers)
    assert stale == [], f"EXEMPT_METHODS 里这些条目已过时：{stale}"
```

- [ ] **Step 2: 跑守卫确认它现在是绿的**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_coverage_guard.py -n0 -q
```
Expected: 2 passed

- [ ] **Step 3: 变异验证一 —— 删除挂钩必须报红**

```bash
cp backend/app/repositories/sqlite/knowhow_store.py /tmp/khstore.bak
python3 - <<'PY'
from pathlib import Path
p = Path("backend/app/repositories/sqlite/knowhow_store.py")
src = p.read_text(encoding="utf-8")
# 定位 delete_knowhow_row 方法体，把它那次 record_change 调用改名
start = src.index("    def delete_knowhow_row(")
end = src.index("\n    def ", start + 10)
body = src[start:end]
assert "record_change(" in body, "变异打空了：这个方法体里没有 record_change"
p.write_text(src[:start] + body.replace("record_change(", "_disabled_record(", 1) + src[end:], encoding="utf-8")
print("变异已注入")
PY
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_coverage_guard.py -n0 -q
```
Expected: **FAIL**，错误信息里点名 `delete_knowhow_row`。
若是 PASS，守卫无效，回到 Step 1 修守卫。

```bash
cp /tmp/khstore.bak backend/app/repositories/sqlite/knowhow_store.py
```

- [ ] **Step 4: 变异验证二 —— 把写事务"搬家"也必须报红**

只做删除变异不够：`ast.walk` 若作用域取错，会把邻居方法里的 `record_change` 算到本方法头上。

```bash
cp backend/app/repositories/sqlite/knowhow_store.py /tmp/khstore.bak
python3 - <<'PY'
from pathlib import Path
p = Path("backend/app/repositories/sqlite/knowhow_store.py")
src = p.read_text(encoding="utf-8")
marker = "    # -------------------------------------------------------------- cells"
assert marker in src, "锚点没找到，先确认文件结构"
new_method = '''    def _moved_writer_without_history(self, table_id: str) -> None:
        """变异探针：一个开了写事务却不记流水的新方法。"""
        with self.database.write() as db:
            db.execute("UPDATE knowhow_tables SET updated_at = ? WHERE id = ?",
                       (self.now(), table_id))

'''
p.write_text(src.replace(marker, new_method + marker, 1), encoding="utf-8")
print("变异已注入")
PY
grep -c "_moved_writer_without_history" backend/app/repositories/sqlite/knowhow_store.py
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_coverage_guard.py -n0 -q
```
Expected: `grep -c` 输出 ≥ 1（证明变异真的改到了文件），且测试 **FAIL** 并点名 `_moved_writer_without_history`。

```bash
cp /tmp/khstore.bak backend/app/repositories/sqlite/knowhow_store.py
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_coverage_guard.py -n0 -q
```
Expected: 恢复后 2 passed

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_knowhow_history_coverage_guard.py
git commit -m "test(knowhow): 加变更流水覆盖守卫（含两种变异验证）"
```

---

## Task 8: 回退引擎

整个回退在一个写事务里：陈旧校验 → 前置指纹守卫 → 逆序重放 → 后置指纹守卫 → 追加 revert 流水。

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_history_store.py`（加 `revert_to`）
- Test: `backend/tests/test_knowhow_revert.py`（新建）

**Interfaces:**
- Consumes: Task 3-6
- Produces:
  - `KnowhowHistoryStore.revert_to(table_id, target_seq, expected_head_seq, actor="") -> dict`，返回 `{"seq": <新 revert 流水的 seq>, "target_seq": int}`
  - 异常类 `HistoryStale`、`HistoryInconsistent`、`RevertVerifyFailed`（均定义在 `knowhow_history_store.py`）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_revert.py`：

```python
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.repositories.sqlite.knowhow_history_store import (
    HistoryInconsistent, HistoryStale, RevertVerifyFailed,
)
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id


@pytest.fixture
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store, notebook_id):
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    return {
        "id": table_id,
        "anchor": detail["columns"][0]["id"],
        "plain": detail["columns"][1]["id"],
    }
    # 刻意**不**预建行：本文件多条测试断言 rows[0] 或 rows == []，
    # fixture 里塞一行会让它们全部错位。要行的测试自己建。


def _fp(repo, table_id):
    with repo._runtime.database.connect() as db:
        return knowhow_fingerprint.fingerprint_on(db, table_id)


def test_revert_restores_cell_content(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "第一版")
    good_seq = hist.head_seq(table["id"])
    good_fp = _fp(repo, table["id"])

    store.update_knowhow_cell(row, table["plain"], "第二版")
    store.update_knowhow_cell(row, table["plain"], "第三版")

    hist.revert_to(table["id"], good_seq, hist.head_seq(table["id"]), actor="user-1")

    detail = store.get_knowhow_table(table["id"])
    assert detail["rows"][0]["cells"][table["plain"]] == "第一版"
    assert _fp(repo, table["id"]) == good_fp


def test_revert_appends_a_new_change_and_keeps_the_old_ones(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "旧")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "新")
    head_before = hist.head_seq(table["id"])

    result = hist.revert_to(table["id"], good, head_before, actor="user-1")

    assert result["seq"] == head_before + 1
    assert hist.get_change(table["id"], head_before) is not None, "旧流水必须保留"
    revert = hist.get_change(table["id"], result["seq"])
    assert revert["kind"] == "revert"
    assert revert["origin"] == "revert"
    assert revert["payload"]["target_seq"] == good


def test_revert_of_a_revert_returns_to_the_newer_state(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "旧")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "新")
    newer = hist.head_seq(table["id"])

    hist.revert_to(table["id"], good, newer)
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "旧"

    hist.revert_to(table["id"], newer, hist.head_seq(table["id"]))
    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "新"


def test_revert_rebuilds_a_deleted_row_with_the_same_id(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "内容")
    store.upsert_knowhow_cell_code(row, table["plain"], "print(1)", "python", "u", "h")
    good = hist.head_seq(table["id"])

    store.delete_knowhow_row(row)
    assert store.get_knowhow_table(table["id"])["rows"] == []

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    rows = store.get_knowhow_table(table["id"])["rows"]
    assert len(rows) == 1
    assert rows[0]["id"] == row, "row_id 必须原样复用——引用跳转与代码附件都挂在它上面"
    assert rows[0]["cells"][table["plain"]] == "内容"
    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code is not None and code["code_text"] == "print(1)"


def test_revert_rebuilds_a_deleted_column_with_all_its_cells(store, hist, table):
    row_a = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    row_b = store.add_knowhow_row(table["id"], {table["anchor"]: "B"})
    store.update_knowhow_cell(row_a, table["plain"], "甲")
    store.update_knowhow_cell(row_b, table["plain"], "乙")
    good = hist.head_seq(table["id"])

    store.delete_knowhow_column(table["plain"])
    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    detail = store.get_knowhow_table(table["id"])
    assert [c["id"] for c in detail["columns"]] == [table["anchor"], table["plain"]]
    by_row = {r["id"]: r["cells"] for r in detail["rows"]}
    assert by_row[row_a][table["plain"]] == "甲"
    assert by_row[row_b][table["plain"]] == "乙"


def test_revert_undoes_an_added_row(store, hist, table):
    good = hist.head_seq(table["id"])
    store.add_knowhow_row(table["id"], {table["anchor"]: "多余的行"})

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))
    assert store.get_knowhow_table(table["id"])["rows"] == []


def test_stale_head_is_rejected(store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "别人刚改的")

    with pytest.raises(HistoryStale):
        hist.revert_to(table["id"], good, good)  # 前端以为 head 还是 good


def test_out_of_band_edit_is_detected_and_refused(repo, store, hist, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "正常内容")
    good = hist.head_seq(table["id"])
    store.update_knowhow_cell(row, table["plain"], "再改一次")
    head = hist.head_seq(table["id"])

    # 绕过 store 直接改库——模拟"某条写路径漏挂钩"
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowhow_cells SET content_md = '偷偷改的' "
            "WHERE row_id = ? AND column_id = ?",
            (row, table["plain"]),
        )

    with pytest.raises(HistoryInconsistent):
        hist.revert_to(table["id"], good, head)

    assert store.get_knowhow_table(table["id"])["rows"][0]["cells"][table["plain"]] == "偷偷改的", (
        "拒绝回退时必须什么都不改"
    )


def test_revert_to_unknown_seq_raises_key_error(store, hist, table):
    with pytest.raises(KeyError):
        hist.revert_to(table["id"], 999, hist.head_seq(table["id"]))


def test_legacy_table_without_a_genesis_change_still_reverts(repo, store, hist, notebook_id):
    """存量表（本特性上线前建的）没有 table_create 流水（spec §7.2）。

    前置指纹守卫拿的是"最新流水的 fingerprint"，而那条流水的指纹本来就是
    那次编辑之后算的，所以在这类表上依然成立——回退最早只能到上线后第一条。
    """
    table_id = store.create_knowhow_table(
        notebook_id, "存量表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    column_id = store.get_knowhow_table(table_id)["columns"][1]["id"]
    row = store.add_knowhow_row(table_id, {})
    # 抹掉建表以来的全部流水，模拟"上线前就存在的表"
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM knowhow_changes WHERE table_id = ?", (table_id,))
    assert hist.head_seq(table_id) == 0

    store.update_knowhow_cell(row, column_id, "上线后第一次编辑")
    first = hist.head_seq(table_id)
    store.update_knowhow_cell(row, column_id, "第二次编辑")

    hist.revert_to(table_id, first, hist.head_seq(table_id))

    assert store.get_knowhow_table(table_id)["rows"][0]["cells"][column_id] == "上线后第一次编辑"


def test_reverting_cell_content_makes_its_code_attachment_fresh_again(store, hist, table):
    """代码附件新鲜度靠 cell_content_hash vs 当前净文本 hash 推导（spec §7.4）。

    回退格子内容后 hash 变回旧值，代码自动从 stale 回到 fresh——这是**正确**
    行为（内容回到当时，代码就重新对上了），这条测试把它钉死，防止将来有人
    把它当 bug"修"掉。
    """
    from app.services.knowhow.api import cell_content_hash

    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    store.update_knowhow_cell(row, table["plain"], "版本一")
    store.upsert_knowhow_cell_code(
        row, table["plain"], "print(1)", "python", "u", cell_content_hash("版本一"),
    )
    good = hist.head_seq(table["id"])

    store.update_knowhow_cell(row, table["plain"], "版本二")
    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code["cell_content_hash"] != cell_content_hash("版本二"), "此刻应为 stale"

    hist.revert_to(table["id"], good, hist.head_seq(table["id"]))

    code = store.get_knowhow_cell_code(row, table["plain"])
    assert code["cell_content_hash"] == cell_content_hash("版本一"), "回退后应重新对上"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_revert.py -n0 -q
```
Expected: FAIL — `ImportError: cannot import name 'HistoryStale'`

- [ ] **Step 3: 实现 `revert_to`**

在 `knowhow_history_store.py` 里加异常类与方法：

```python
class HistoryStale(Exception):
    """调用方看到的 head 已经不是当前 head（有人在这期间改过表）。"""


class HistoryInconsistent(Exception):
    """当前表内容与流水链对不上——有写路径漏挂钩，或有人直接改过库。"""


class RevertVerifyFailed(Exception):
    """逆序重放跑完，但结果指纹不等于目标点的指纹。已回滚。"""
```

`revert_to` 主体（全部在一个 `with self.database.write() as db:` 里）：

```python
    def revert_to(
        self, table_id: str, target_seq: int, expected_head_seq: int, actor: str = ""
    ) -> dict:
        """把整张表逆序重放回 ``target_seq`` 那一刻。见 spec §6.1。

        前置/后置两道指纹守卫是这个方法的核心：delta 重放的正确性不能靠
        "看起来对"，必须被独立判据证明。任一守卫不过就中止（前置）或
        整事务回滚（后置），绝不留下半改的表。
        """
        with self.database.write() as db:
            db.execute("BEGIN IMMEDIATE")

            head_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
            head = int(head_row["head"])
            if head != int(expected_head_seq):
                raise HistoryStale(f"head={head} expected={expected_head_seq}")

            target = db.execute(
                "SELECT seq, fingerprint FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, int(target_seq)),
            ).fetchone()
            if target is None:
                raise KeyError(target_seq)
            if head == int(target_seq):
                return {"seq": head, "target_seq": int(target_seq)}  # 已经在目标点

            head_change = db.execute(
                "SELECT fingerprint FROM knowhow_changes WHERE table_id = ? AND seq = ?",
                (table_id, head),
            ).fetchone()
            current = knowhow_fingerprint.fingerprint_on(db, table_id)
            if current != head_change["fingerprint"]:
                raise HistoryInconsistent(table_id)

            rows = db.execute(
                "SELECT * FROM knowhow_changes WHERE table_id = ? AND seq > ? "
                "ORDER BY seq DESC",
                (table_id, int(target_seq)),
            ).fetchall()

            undone: list[dict] = []
            for row in rows:
                change = _row_to_change(row)
                self._apply_before(db, table_id, change)
                undone.append(change)

            after = knowhow_fingerprint.fingerprint_on(db, table_id)
            if after != target["fingerprint"]:
                raise RevertVerifyFailed(
                    f"table={table_id} target={target_seq} got={after} want={target['fingerprint']}"
                )

            db.execute(
                "UPDATE knowhow_rows SET projection_status = 'pending' WHERE table_id = ?",
                (table_id,),
            )
            db.execute(
                "UPDATE knowhow_tables SET mutation_seq = mutation_seq + 1, updated_at = ? "
                "WHERE id = ?",
                (self.now(), table_id),
            )
            seq = record_change(
                db, new_id=self.new_id, now=self.now, table_id=table_id,
                kind="revert",
                payload=self._revert_payload(int(target_seq), undone),
                actor=actor, origin="revert",
                note=f"回退到 #{int(target_seq)}",
            )
        return {"seq": seq, "target_seq": int(target_seq)}
```

`_apply_before(db, table_id, change)` 按 `kind` 分派（spec §6.1 的逆操作表）：

```python
    def _apply_before(self, db, table_id: str, change: dict) -> None:
        kind = change["kind"]
        payload = change["payload"]
        now = self.now()

        if kind in ("cell_update",):
            for cell in payload.get("cells", []):
                self._write_cell(db, cell["row_id"], cell["column_id"], cell["before"], now)

        elif kind in ("row_add", "import_append"):
            for row in payload.get("rows", []):
                db.execute("DELETE FROM knowhow_rows WHERE id = ?", (row["row_id"],))

        elif kind == "row_delete":
            for row in payload.get("rows", []):
                self._rebuild_row(db, table_id, row, now)

        elif kind == "column_add":
            db.execute(
                "DELETE FROM knowhow_columns WHERE id = ?", (payload["column"]["id"],)
            )

        elif kind == "column_delete":
            self._rebuild_column(db, table_id, payload, now)

        elif kind in ("column_rename",):
            db.execute(
                "UPDATE knowhow_columns SET name = ? WHERE id = ?",
                (payload["before"], payload["column_id"]),
            )

        elif kind == "column_kind":
            db.execute(
                "UPDATE knowhow_columns SET role = ? WHERE id = ?",
                (payload["before"], payload["column_id"]),
            )

        elif kind == "anchor_set":
            for entry in payload.get("columns", []):
                db.execute(
                    "UPDATE knowhow_columns SET role = ? WHERE id = ?",
                    (entry["before"], entry["column_id"]),
                )

        elif kind == "table_meta":
            db.execute(
                "UPDATE knowhow_tables SET title = ?, description = ?, updated_at = ? "
                "WHERE id = ?",
                (payload["before"]["title"], payload["before"]["description"], now, table_id),
            )

        elif kind in ("cell_code_put", "cell_code_delete"):
            self._write_cell_code(
                db, payload["row_id"], payload["column_id"], payload["before"], now
            )

        elif kind == "revert":
            self._apply_revert_before(db, table_id, payload, now)

        elif kind == "table_create":
            raise RevertVerifyFailed("不能跨越建表流水")

        else:
            raise RevertVerifyFailed(f"未知的变更类型：{kind}")
```

三个私有写辅助（`_write_cell` / `_rebuild_row` / `_rebuild_column` / `_write_cell_code`）关键点：

- `_write_cell(db, row_id, column_id, content, now)`：`content is None` → `DELETE FROM knowhow_cells WHERE row_id=? AND column_id=?`；否则 UPSERT（用 `self.new_id("khcel")` 作为新行 id，冲突时走 DO UPDATE）。
- `_rebuild_row`：`INSERT INTO knowhow_rows (id, table_id, position, projection_status, created_at, updated_at) VALUES (?,?,?,'pending',?,?)`，**id 用 payload 里的 `row_id` 原样写回**；再插所有 `cells` 与 `code`。
- `_rebuild_column`：`INSERT INTO knowhow_columns (id, table_id, name, role, position)`，**id 原样**；再插该列的 `cells` 与 `code`。
- `_write_cell_code`：`before is None` → DELETE；否则 UPSERT 全部五个字段（`code_text`/`language`/`updated_by`/`cell_content_hash` 都要写，指纹覆盖它们）。

`_revert_payload(target_seq, undone)` 按 spec §4.4 的 `revert` 形状，把 `undone` 里各条的 before/after **对调**汇总（因为回退的 `before` = 回退前状态 = 各条的 `after`；回退的 `after` = 各条的 `before`）。

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_revert.py -n0 -q
```
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_history_store.py backend/tests/test_knowhow_revert.py
git commit -m "feat(knowhow): 回退引擎（逆序重放 + 前后置指纹守卫）"
```

---

## Task 9: 往返不变量测试（本计划的核心验证）

delta 完备性只能靠往返证明。挑几个点测证不出来。

**Files:**
- Test: `backend/tests/test_knowhow_history_roundtrip.py`（新建）

**Interfaces:**
- Consumes: Task 1-8 全部

- [ ] **Step 1: 写测试**

创建 `backend/tests/test_knowhow_history_roundtrip.py`：

```python
"""往返不变量：任意混合变更序列，逐点回退都必须精确复原。

这是 delta 方案正确性的唯一有效证明。用固定种子的伪随机序列而不是
手写几个用例——手写的用例只会覆盖作者想得到的组合，而漏挂钩/payload
存不全的 bug 恰恰藏在想不到的组合里（例如"删了列又删了行再回退"）。
"""
from __future__ import annotations

import random

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def _fp(repo, table_id):
    with repo._runtime.database.connect() as db:
        return knowhow_fingerprint.fingerprint_on(db, table_id)


def _snapshot(repo, table_id):
    return repo._runtime.knowhow_transfer_store.snapshot_table(table_id)


def _mutate(rng, store, table_id, state) -> None:
    """随机施加一次变更。所有分支都必须是"用户真能做到"的操作。"""
    detail = store.get_knowhow_table(table_id)
    columns = [c for c in detail["columns"]]
    rows = [r for r in detail["rows"]]
    choices = ["add_row", "add_column", "meta", "rename_column"]
    if rows and columns:
        choices += ["cell", "cell", "cell", "code"]
    if len(rows) > 1:
        choices.append("delete_row")
    if len(columns) > 2:
        choices.append("delete_column")
    if len(columns) > 1:
        choices += ["kind", "anchor"]

    action = rng.choice(choices)
    if action == "add_row":
        store.add_knowhow_row(table_id, {columns[0]["id"]: f"值{rng.randint(0, 99)}"})
    elif action == "add_column":
        state["col_n"] += 1
        store.add_knowhow_column(table_id, f"列{state['col_n']}", "attribute")
    elif action == "meta":
        store.update_knowhow_table_meta(table_id, title=f"标题{rng.randint(0, 99)}")
    elif action == "rename_column":
        state["col_n"] += 1
        store.rename_knowhow_column(rng.choice(columns)["id"], f"改名{state['col_n']}")
    elif action == "cell":
        store.update_knowhow_cell(
            rng.choice(rows)["id"], rng.choice(columns)["id"], f"内容{rng.randint(0, 999)}"
        )
    elif action == "code":
        store.upsert_knowhow_cell_code(
            rng.choice(rows)["id"], rng.choice(columns)["id"],
            f"print({rng.randint(0, 9)})", "python", "u", f"h{rng.randint(0, 9)}",
        )
    elif action == "delete_row":
        store.delete_knowhow_row(rng.choice(rows)["id"])
    elif action == "delete_column":
        non_anchor = [c for c in columns if c["role"] != "anchor"]
        store.delete_knowhow_column(rng.choice(non_anchor)["id"])
    elif action == "kind":
        non_anchor = [c for c in columns if c["role"] != "anchor"]
        store.set_knowhow_column_kind(
            rng.choice(non_anchor)["id"], rng.choice(["procedure", "entity", "attribute"])
        )
    elif action == "anchor":
        store.set_knowhow_anchor_column(table_id, rng.choice(columns)["id"])


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_every_history_point_is_exactly_reachable_by_revert(repo, seed):
    rng = random.Random(seed)
    store = repo._runtime.knowhow_store
    hist = repo._runtime.knowhow_history_store
    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )

    # 阶段一：随机演化，记下每个点的指纹与快照
    checkpoints: list[tuple[int, str, dict]] = [
        (hist.head_seq(table_id), _fp(repo, table_id), _snapshot(repo, table_id))
    ]
    state = {"col_n": 0}
    for _ in range(30):
        _mutate(rng, store, table_id, state)
        checkpoints.append(
            (hist.head_seq(table_id), _fp(repo, table_id), _snapshot(repo, table_id))
        )

    # 阶段二：从最新逐点回退，每一步都要精确落在当时的状态上
    for seq, want_fp, want_snapshot in reversed(checkpoints):
        hist.revert_to(table_id, seq, hist.head_seq(table_id))
        assert _fp(repo, table_id) == want_fp, f"seed={seed} seq={seq} 指纹不符"
        got = _snapshot(repo, table_id)
        for key in ("columns", "rows", "cells", "cell_code"):
            assert _normalize(got[key]) == _normalize(want_snapshot[key]), (
                f"seed={seed} seq={seq} 的 {key} 与当时不一致"
            )


def _normalize(rows):
    """快照比较要忽略 updated_at/created_at（回退必然写新时间戳）。"""
    ignored = {"updated_at", "created_at"}
    return sorted(
        (tuple(sorted((k, v) for k, v in dict(r).items() if k not in ignored))
         for r in rows),
        key=repr,
    )
```

- [ ] **Step 2: 跑测试**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_roundtrip.py -n0 -q
```
Expected: 5 passed

**如果某个 seed 挂了**：不要改测试或换 seed。失败信息会指出是哪个 `seq` 的哪个部分不一致——回到 Task 4-6 检查对应 `kind` 的 payload 是不是存漏了字段（最常见的是删除类没存全代码附件，或 `position` 没存）。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_knowhow_history_roundtrip.py
git commit -m "test(knowhow): 往返不变量测试（30 步随机演化 × 5 seed 逐点回退）"
```

---

## Task 10: 里程碑与清理历史（含只删前缀）

**Files:**
- Modify: `backend/app/repositories/sqlite/knowhow_history_store.py`
- Test: `backend/tests/test_knowhow_milestones.py`（新建）

**Interfaces:**
- Produces: `create_milestone(table_id, seq, name, note, created_by) -> dict`、`delete_milestone(table_id, milestone_id) -> None`、`list_milestones(table_id) -> list[dict]`、`prune(table_id, before_iso) -> dict`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_milestones.py`：

```python
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def store(repo):
    return repo._runtime.knowhow_store


@pytest.fixture
def hist(repo):
    return repo._runtime.knowhow_history_store


@pytest.fixture
def table(repo, store):
    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )
    detail = store.get_knowhow_table(table_id)
    return {
        "id": table_id,
        "anchor": detail["columns"][0]["id"],
        "plain": detail["columns"][1]["id"],
    }


def test_milestone_names_must_be_unique_per_table(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    seq = hist.head_seq(table["id"])
    hist.create_milestone(table["id"], seq, "评审前", "", "user-1")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        hist.create_milestone(table["id"], seq, "评审前", "", "user-1")


def test_prune_deletes_only_the_oldest_prefix(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    for i in range(5):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")
    head = hist.head_seq(table["id"])

    # 人为把前 3 条的时间戳改老
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq <= 3",
            (table["id"],),
        )

    hist.prune(table["id"], "2001-01-01T00:00:00")

    remaining = [c["seq"] for c in hist.list_changes(table["id"], limit=100)]
    assert remaining == sorted(remaining, reverse=True)
    assert min(remaining) == 4, "只能删最老的连续前缀"
    assert max(remaining) == head


def test_prune_never_removes_the_head(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    head = hist.head_seq(table["id"])
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ?",
            (table["id"],),
        )

    hist.prune(table["id"], "2099-01-01T00:00:00")

    remaining = [c["seq"] for c in hist.list_changes(table["id"], limit=100)]
    assert remaining == [head], (
        "head 必须留着——前置指纹守卫拿它当参照，删了整表回退就不可用了"
    )


def test_prune_uses_seq_not_timestamp_so_clock_skew_cannot_punch_holes(hist, store, table):
    """时钟回拨会让 created_at 局部乱序；按 seq 执行删除才保证删的是前缀。"""
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    for i in range(4):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")

    with hist.database.write() as db:
        # seq 1,2,4 老，seq 3 却是新的（时钟回拨）
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq IN (1,2,4)",
            (table["id"],),
        )
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2050-01-01T00:00:00' "
            "WHERE table_id = ? AND seq = 3",
            (table["id"],),
        )

    hist.prune(table["id"], "2001-01-01T00:00:00")

    remaining = sorted(c["seq"] for c in hist.list_changes(table["id"], limit=100))
    assert remaining == list(range(remaining[0], remaining[-1] + 1)), (
        f"流水链出现空洞：{remaining}"
    )


def test_milestone_pointing_at_a_pruned_seq_survives_as_stale(hist, store, table):
    row = store.add_knowhow_row(table["id"], {table["anchor"]: "A"})
    old_seq = hist.head_seq(table["id"])
    hist.create_milestone(table["id"], old_seq, "很久以前", "", "user-1")
    for i in range(3):
        store.update_knowhow_cell(row, table["plain"], f"第{i}版")

    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at = '2000-01-01T00:00:00' "
            "WHERE table_id = ? AND seq <= ?",
            (table["id"], old_seq),
        )
    hist.prune(table["id"], "2001-01-01T00:00:00")

    milestones = hist.list_milestones(table["id"])
    assert len(milestones) == 1
    assert milestones[0]["stale"] is True, "指向已删流水的里程碑要标记失效，但不能删"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_milestones.py -n0 -q
```
Expected: FAIL — `AttributeError: ... 'create_milestone'`

- [ ] **Step 3: 实现**

`create_milestone` / `delete_milestone` 是直白的 INSERT/DELETE。`list_milestones` 要 LEFT JOIN 流水算出 `stale`：

```python
    def list_milestones(self, table_id: str) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT m.*, (c.seq IS NULL) AS stale FROM knowhow_milestones m "
                "LEFT JOIN knowhow_changes c ON c.table_id = m.table_id AND c.seq = m.seq "
                "WHERE m.table_id = ? ORDER BY m.seq DESC",
                (table_id,),
            ).fetchall()
        return [{**dict(r), "stale": bool(r["stale"])} for r in rows]
```

`prune` —— **按 seq 删，不按 created_at 删**：

```python
    def prune(self, table_id: str, before_iso: str) -> dict:
        """删掉最老的连续前缀。见 spec §7.7。

        为什么按 seq 而不是直接 ``DELETE WHERE created_at < ?``：反向重放
        要求流水链从 head 起连续，中间挖洞会让重放走到缺口就断，而前置
        指纹守卫看的是 head、**发现不了这个洞**。先用时间求出 cutoff_seq、
        再按 seq 删，即便时钟回拨导致 created_at 局部乱序，删的也一定是前缀。

        head 永远保留：前置指纹守卫拿它当参照，删了整表回退直接不可用。
        """
        with self.database.write() as db:
            head_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM knowhow_changes WHERE table_id = ?",
                (table_id,),
            ).fetchone()
            head = int(head_row["head"])
            if head == 0:
                return {"removed": 0}
            cutoff_row = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS cutoff FROM knowhow_changes "
                "WHERE table_id = ? AND created_at < ?",
                (table_id, before_iso),
            ).fetchone()
            cutoff = min(int(cutoff_row["cutoff"]), head - 1)
            if cutoff <= 0:
                return {"removed": 0}
            cursor = db.execute(
                "DELETE FROM knowhow_changes WHERE table_id = ? AND seq <= ?",
                (table_id, cutoff),
            )
        return {"removed": cursor.rowcount}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_milestones.py -n0 -q
```
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add backend/app/repositories/sqlite/knowhow_history_store.py backend/tests/test_knowhow_milestones.py
git commit -m "feat(knowhow): 里程碑与只删前缀的历史清理"
```

---

## Task 11: 服务层 —— diff 聚合与回退编排

**Files:**
- Create: `backend/app/services/knowhow/history.py`
- Test: `backend/tests/test_knowhow_history_service.py`（新建）

**Interfaces:**
- Consumes: Task 3/8/10 的 store
- Produces:
  - `aggregate_diff(changes: list[dict]) -> dict`（纯函数，输入升序流水，输出 `{"cells": [...], "rows_added": [...], "rows_removed": [...], "columns": [...], "table_meta": {...} | None}`）
  - `revert_table(repo, notebook_id, table_id, target_seq, expected_head_seq, actor) -> dict`（编排：调 store 回退 → 触发重投影）

- [ ] **Step 1: 写失败测试**

```python
from app.services.knowhow.history import aggregate_diff


def test_aggregate_folds_repeated_cell_edits_into_one_net_change():
    changes = [
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "A", "after": "B"}]}},
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "B", "after": "C"}]}},
    ]
    result = aggregate_diff(changes)
    assert result["cells"] == [
        {"row_id": "r1", "column_id": "c1", "before": "A", "after": "C"}
    ]


def test_aggregate_drops_cells_that_ended_where_they_started():
    changes = [
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "A", "after": "B"}]}},
        {"kind": "cell_update", "payload": {"cells": [
            {"row_id": "r1", "column_id": "c1", "before": "B", "after": "A"}]}},
    ]
    assert aggregate_diff(changes)["cells"] == []


def test_row_added_then_deleted_cancels_out():
    changes = [
        {"kind": "row_add", "payload": {"rows": [{"row_id": "r9", "position": 0,
                                                  "cells": {}, "code": []}]}},
        {"kind": "row_delete", "payload": {"rows": [{"row_id": "r9", "position": 0,
                                                     "cells": {}, "code": []}]}},
    ]
    result = aggregate_diff(changes)
    assert result["rows_added"] == []
    assert result["rows_removed"] == []


def test_row_deleted_then_restored_cancels_out():
    changes = [
        {"kind": "row_delete", "payload": {"rows": [{"row_id": "r1", "position": 0,
                                                     "cells": {}, "code": []}]}},
        {"kind": "row_add", "payload": {"rows": [{"row_id": "r1", "position": 0,
                                                  "cells": {}, "code": []}]}},
    ]
    result = aggregate_diff(changes)
    assert result["rows_added"] == []
    assert result["rows_removed"] == []


def test_table_meta_takes_first_before_and_last_after():
    changes = [
        {"kind": "table_meta", "payload": {"before": {"title": "一", "description": ""},
                                           "after": {"title": "二", "description": ""}}},
        {"kind": "table_meta", "payload": {"before": {"title": "二", "description": ""},
                                           "after": {"title": "三", "description": ""}}},
    ]
    meta = aggregate_diff(changes)["table_meta"]
    assert meta["before"]["title"] == "一"
    assert meta["after"]["title"] == "三"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_service.py -n0 -q
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `backend/app/services/knowhow/history.py`。`aggregate_diff` 遍历升序流水，对每个 `(row_id, column_id)` 维护 `first_before` 与 `last_after`，最后丢掉两者相等的；行增删用一个 `dict[row_id] -> "added" | "removed"` 抵消（先 add 后 delete → 两边都清掉；先 delete 后 add → 同样清掉）。

`revert_table` 编排：

```python
def revert_table(
    repo: Any, notebook_id: str, table_id: str,
    target_seq: int, expected_head_seq: int, actor: str,
) -> dict:
    """回退 + 触发重投影。

    重投影走 knowhow_api.get_scheduler(repo).schedule(table_id) —— 与
    POST .../reproject 逃生口完全同一个入口。project_table 本身永远是
    全量确定性重投影，不存在需要绕开的增量路径；而绕过 ProjectionScheduler
    会丢掉 per-table 防抖与单飞，和同表并发编辑打架。
    """
    from app.services.knowhow import api as knowhow_api

    result = repo.revert_knowhow_table(table_id, target_seq, expected_head_seq, actor)
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return result
```

- [ ] **Step 4: 加 facade 一跳委托**

在 `backend/app/services/sqlite_repository.py` 里加（每个方法体只有一条 return）：

```python
    def knowhow_history_head_seq(self, table_id: str) -> int:
        return self._runtime.knowhow_history_store.head_seq(table_id)

    def list_knowhow_changes(
        self, table_id: str, limit: int = 50, before_seq: "int | None" = None
    ) -> list:
        return self._runtime.knowhow_history_store.list_changes(table_id, limit, before_seq)

    def get_knowhow_change(self, table_id: str, seq: int) -> "dict | None":
        return self._runtime.knowhow_history_store.get_change(table_id, seq)

    def knowhow_changes_between(self, table_id: str, from_seq: int, to_seq: int) -> list:
        return self._runtime.knowhow_history_store.changes_between(table_id, from_seq, to_seq)

    def knowhow_cell_history(
        self, table_id: str, row_id: str, column_id: str, limit: int = 50
    ) -> list:
        return self._runtime.knowhow_history_store.cell_history(
            table_id, row_id, column_id, limit
        )

    def revert_knowhow_table(
        self, table_id: str, target_seq: int, expected_head_seq: int, actor: str = ""
    ) -> dict:
        return self._runtime.knowhow_history_store.revert_to(
            table_id, target_seq, expected_head_seq, actor
        )

    def create_knowhow_milestone(
        self, table_id: str, seq: int, name: str, note: str, created_by: str
    ) -> dict:
        return self._runtime.knowhow_history_store.create_milestone(
            table_id, seq, name, note, created_by
        )

    def delete_knowhow_milestone(self, table_id: str, milestone_id: str) -> None:
        return self._runtime.knowhow_history_store.delete_milestone(table_id, milestone_id)

    def list_knowhow_milestones(self, table_id: str) -> list:
        return self._runtime.knowhow_history_store.list_milestones(table_id)

    def prune_knowhow_history(self, table_id: str, before_iso: str) -> dict:
        return self._runtime.knowhow_history_store.prune(table_id, before_iso)
```

- [ ] **Step 5: 跑测试 + 重生成 surface 契约**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_service.py -n0 -q
PYTHONPATH=backend python3 scripts/generate_repository_contract_fixtures.py --rebaseline-surface
PYTHONPATH=backend python3 -m pytest backend/tests/test_repository_surface_contract.py \
  backend/tests/test_repository_facade_contract.py -n0 -q
```
Expected: 全 PASS

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(knowhow): 历史服务层（diff 聚合 + 回退编排）与 facade 委托"
```

---

## Task 12: HTTP 端点

**Files:**
- Modify: `backend/app/api/knowhow_routes.py`、`backend/app/models/knowhow.py`（响应模型）
- Test: `backend/tests/test_knowhow_history_api.py`（新建）

**Interfaces:**
- Produces: spec §8.1 的 8 个端点

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_knowhow_history_api.py`。**fixture 惯例照抄 `test_knowhow_editing_api.py`（`:26-70`）—— 那里用的是 `_client`/`_login`/`_mk_notebook`/`_mk_table` 这组模块级 helper，不是 pytest fixture**，`repo` 才是 fixture：

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _setup(tmp_path, monkeypatch):
    """建一个 owner + notebook + 两列一行的表，返回常用句柄。"""
    client = _client(tmp_path, monkeypatch)
    owner = _login(client, "a00002080")
    nb = client.post("/api/notebooks", json={"name": "N"}, headers=owner).json()["id"]
    table = client.post(
        f"/api/notebooks/{nb}/knowhow",
        headers=owner,
        json={
            "title": "表",
            "columns": [{"name": "概念", "kind": "anchor"}, {"name": "做法", "kind": "attribute"}],
            "anchor_index": 0,
        },
    ).json()
    row = client.post(
        f"/api/notebooks/{nb}/knowhow/{table['id']}/rows", headers=owner, json={"cells": {}}
    ).json()
    return {
        "client": client, "owner": owner, "nb": nb, "table": table,
        "row_id": row["id"], "plain": table["columns"][1]["id"],
    }


def test_history_timeline_is_readable_by_a_read_only_member(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    bob = _login(ctx["client"], "b00002080")
    bob_id = ctx["client"].get("/api/me", headers=bob).json()["id"]
    repo.add_member(ctx["nb"], bob_id)

    response = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history", headers=bob
    )

    assert response.status_code == 200
    seqs = [c["seq"] for c in response.json()["changes"]]
    assert seqs == sorted(seqs, reverse=True)


def test_revert_is_refused_for_a_read_only_member(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    bob = _login(ctx["client"], "b00002081")
    bob_id = ctx["client"].get("/api/me", headers=bob).json()["id"]
    repo.add_member(ctx["nb"], bob_id)

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": 1, "expected_head_seq": 1}, headers=bob,
    )

    assert response.status_code == 404, "写守卫对非 owner 统一 404，不泄露存在性"


def _patch_cell(ctx, content, **extra):
    body = {"content_md": content, **extra}
    return ctx["client"].patch(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}"
        f"/rows/{ctx['row_id']}/cells/{ctx['plain']}",
        json=body, headers=ctx["owner"],
    )


def _head(ctx):
    return ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["head_seq"]


def test_stale_head_returns_409_with_error_code(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "第一版")
    good = _head(ctx)
    _patch_cell(ctx, "别人又改了")

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": good, "expected_head_seq": good}, headers=ctx["owner"],
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "knowhow_history_stale"
    assert "刷新" in response.json()["detail"]["message"]


def test_inconsistent_history_returns_400_with_error_code(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)
    _patch_cell(ctx, "正常")
    good = _head(ctx)
    _patch_cell(ctx, "再改一次")
    head = _head(ctx)

    with repo._runtime.database.write() as db:  # 绕过 store：模拟漏挂钩的写路径
        db.execute(
            "UPDATE knowhow_cells SET content_md='偷偷改的' WHERE row_id=?",
            (ctx["row_id"],),
        )

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": good, "expected_head_seq": head}, headers=ctx["owner"],
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "knowhow_history_inconsistent"


def test_unknown_target_seq_returns_404(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    response = ctx["client"].post(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/revert",
        json={"target_seq": 9999, "expected_head_seq": _head(ctx)}, headers=ctx["owner"],
    )

    assert response.status_code == 404


def test_cell_patch_accepts_and_records_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    assert _patch_cell(ctx, "恢复来的内容", origin="revert").status_code == 200

    changes = ctx["client"].get(
        f"/api/notebooks/{ctx['nb']}/knowhow/{ctx['table']['id']}/history",
        headers=ctx["owner"],
    ).json()["changes"]
    assert changes[0]["origin"] == "revert"


def test_cell_patch_rejects_an_unknown_origin(tmp_path, monkeypatch, repo):
    ctx = _setup(tmp_path, monkeypatch)

    response = _patch_cell(ctx, "x", origin="伪造来源")

    assert response.status_code == 400, (
        "宽容默认会把 wire 错误降级成静默失败——anchor 特性正是这么整个失效的（PR#281→#286）"
    )
```

（时间线响应体因此确定为 `{"head_seq": int, "changes": [...], "milestones": [...]}`，Step 3 的响应模型按这个形状定义。）

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_api.py -n0 -q
```
Expected: FAIL — 404 Not Found（端点不存在）

- [ ] **Step 3: 加请求/响应模型**

在 `backend/app/models/knowhow.py` 加：

```python
VALID_ORIGINS = frozenset({
    "user", "llm_optimize", "llm_reformat", "import", "agent", "revert", "backfill",
})


class KnowhowRevertRequest(BaseModel):
    target_seq: int
    expected_head_seq: int


class KnowhowMilestoneCreate(BaseModel):
    seq: int
    name: str
    note: str = ""


class KnowhowHistoryPruneRequest(BaseModel):
    before_days: int
```

给既有的格子 PATCH 请求模型加 `origin: str = "user"`，并在路由里校验：

```python
    if body.origin not in VALID_ORIGINS:
        raise user_error(400, "未知的变更来源")
```

- [ ] **Step 4: 加 8 个端点**

照 `knowhow_routes.py` 既有样式（读用 `dependencies=[Depends(require_notebook_read)]`，写用 `require_notebook_access`）。回退端点的错误映射：

```python
@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/revert",
    dependencies=[Depends(require_notebook_access)],
)
def revert_knowhow_table(
    notebook_id: str, table_id: str, body: KnowhowRevertRequest,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    repo = repository()
    try:
        knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    try:
        return knowhow_history.revert_table(
            repo, notebook_id, table_id,
            body.target_seq, body.expected_head_seq, user.id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Change not found")
    except HistoryStale:
        raise HTTPException(status_code=409, detail={
            "code": "knowhow_history_stale",
            "message": "这张表刚被其他人改过，请刷新后重试",
        })
    except HistoryInconsistent:
        raise HTTPException(status_code=400, detail={
            "code": "knowhow_history_inconsistent",
            "message": "表的当前内容与变更历史对不上，回退已中止",
        })
    except RevertVerifyFailed:
        raise HTTPException(status_code=500, detail={
            "code": "knowhow_revert_verify_failed",
            "message": "回退结果校验失败，已放弃本次回退，表未被改动",
        })
```

`prune` 端点在删完历史后**主动触发一次绕过节流的资产清扫**：

```python
    result = repo.prune_knowhow_history(table_id, cutoff_iso)
    knowhow_api.maybe_sweep_orphan_assets(repo, notebook_id, min_interval=0, background=True)
    return result
```

- [ ] **Step 5: 跑测试 + 刷 API 契约（只重算 openapi 段）**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_history_api.py -n0 -q
```

**⚠️ 不要跑 `generate_repository_contract_fixtures.py` 的默认模式。** 它当前是坏的，且**与本特性无关**：`_serialization_contract()` 里 `mock.patch.object(routes, "repository", ...)` 打的是 `app.api.routes`，而那个模块早已被重构成纯路由聚合器、不再有 `repository` 属性（在 master 上就是如此），必抛 `AttributeError`。

按仓库既有先例 commit `59bf99b1`（"scope api-contract golden regen to openapi key"）**只重算 `openapi` 段**，`serialization` 与 `source_commit` 逐字保留：

```bash
PYTHONPATH=backend python3 - <<'PY'
import json
from pathlib import Path
from app.main import app

path = Path("backend/tests/fixtures/repository_contract/api_contract.json")
contract = json.loads(path.read_text(encoding="utf-8"))
contract["openapi"] = app.openapi()          # 只动这一个键
path.write_text(
    json.dumps(contract, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("openapi 段已刷新；serialization / source_commit 未动")
PY
```

写回后**必须核对只有 `openapi` 段变了**：

```bash
git diff --stat backend/tests/fixtures/repository_contract/api_contract.json
python3 -c "
import json,subprocess
old=json.loads(subprocess.run(['git','show','HEAD:backend/tests/fixtures/repository_contract/api_contract.json'],capture_output=True,text=True).stdout)
new=json.load(open('backend/tests/fixtures/repository_contract/api_contract.json'))
assert old['serialization']==new['serialization'], 'serialization 被改了，不该动'
assert old['source_commit']==new['source_commit'], 'source_commit 被改了，它是 provenance 不是版本号'
print('只有 openapi 段变化 ✓')
"
PYTHONPATH=backend python3 -m pytest backend/tests/test_repository_api_contract.py -n0 -q
```
Expected: 断言全过，测试 PASS

- [ ] **Step 6: 提交**

```bash
git add -A
git commit -m "feat(knowhow): 历史查询/回退/里程碑/清理的 HTTP 端点"
```

---

## Task 13: actor/origin 穿线 + 资产清扫器扩展 + 传输创世流水

**Files:**
- Modify: `backend/app/api/knowhow_routes.py`（7 个路由传 `user.id`）、`backend/app/services/knowhow/api.py`（5 个服务函数）、`scripts/backfill_knowhow_md.py`、`backend/app/repositories/sqlite/maintenance.py`、`backend/app/services/knowhow/transfer.py`
- Test: `backend/tests/test_knowhow_asset_gc.py`（追加）、`backend/tests/test_knowhow_history_hooks.py`（追加）

- [ ] **Step 1: 写失败测试**

在 `test_knowhow_asset_gc.py` 追加：

```python
def test_asset_referenced_only_by_history_is_not_swept(repo, store, table, asset_id):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], f"![图](asset://{asset_id})"
    )
    store.update_knowhow_cell(table["row_a"], table["plain"], "图没了")

    repo.maintenance.sweep_orphan_assets(table["notebook_id"], min_age_seconds=0)

    assert repo._runtime.knowhow_store.get_notebook_asset(asset_id) is not None, (
        "历史里还引用着它——回收了就没法回退回带图的版本"
    )


def test_asset_becomes_collectable_after_history_is_pruned(repo, store, hist, table, asset_id):
    store.update_knowhow_cell(
        table["row_a"], table["plain"], f"![图](asset://{asset_id})"
    )
    store.update_knowhow_cell(table["row_a"], table["plain"], "图没了")
    with hist.database.write() as db:
        db.execute(
            "UPDATE knowhow_changes SET created_at='2000-01-01T00:00:00' WHERE table_id=?",
            (table["id"],),
        )
    hist.prune(table["id"], "2001-01-01T00:00:00")

    repo.maintenance.sweep_orphan_assets(table["notebook_id"], min_age_seconds=0)

    assert repo._runtime.knowhow_store.get_notebook_asset(asset_id) is None
```

在 `test_knowhow_history_hooks.py` 追加：

```python
def test_copied_table_gets_a_genesis_change_naming_its_source(repo, store, notebook_id, table):
    from app.services.knowhow import transfer as kh_transfer

    other = repo.create_notebook(
        NotebookCreate(name="目标", purpose="p", primary_domain="d")
    ).id
    new_id = kh_transfer.copy_table(repo, table["id"], other, "user-1")

    hist = repo._runtime.knowhow_history_store
    changes = hist.list_changes(new_id, limit=10)
    assert len(changes) == 1
    assert changes[0]["kind"] == "table_create"
    assert "复制" in changes[0]["note"]
    assert hist.list_changes(table["id"], limit=100), "源表历史不受影响"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_asset_gc.py backend/tests/test_knowhow_history_hooks.py -n0 -q
```
Expected: FAIL

- [ ] **Step 3: 扩展清扫器的存活引用集**

`backend/app/repositories/sqlite/maintenance.py` 有**两处**同样的引用判定（读阶段 `:794-800`、写事务内复核 `:904-913`），**两处都要改**。把

```sql
SELECT 1 FROM knowhow_cells c
JOIN knowhow_rows r ON r.id = c.row_id
JOIN knowhow_tables t ON t.id = r.table_id
WHERE t.notebook_id = ? AND c.content_md LIKE ? LIMIT 1
```

改成

```sql
SELECT 1 FROM (
  SELECT 1 FROM knowhow_cells c
  JOIN knowhow_rows r ON r.id = c.row_id
  JOIN knowhow_tables t ON t.id = r.table_id
  WHERE t.notebook_id = ? AND c.content_md LIKE ?
  UNION ALL
  SELECT 1 FROM knowhow_changes ch
  JOIN knowhow_tables t2 ON t2.id = ch.table_id
  WHERE t2.notebook_id = ? AND ch.payload_json LIKE ?
) LIMIT 1
```

参数相应变成 4 个。**同时改 `sweep_orphan_assets` docstring 里 "Reference scope is deliberately narrow" 那一段**，说明现在历史流水也算存活引用、以及这样做的代价（图片进过格子就基本不再自动回收，靠"清理历史"释放）——否则会跟 `test_knowhow_asset_gc.py` 里断言"只看当前 cell"的既有测试打架。

- [ ] **Step 4: 穿 actor/origin**

7 个路由处理器加 `user: UserProfile = Depends(get_current_user)` 并把 `user.id` 作为 `actor` 传给 store 调用：`patch_knowhow_cell`、`patch_knowhow_cells_batch`、`patch_knowhow_table`、`patch_knowhow_column`、`add_knowhow_row`、`delete_knowhow_row`、`add_knowhow_column`、`delete_knowhow_column`。

服务层：`create_table` / `import_table`（`origin="import"`）/ `commit_append`（`origin="import"`，且**整批新行记一条 `import_append`** 而不是每行一条 `row_add` —— 在 `commit_append` 里改成先收集全部新行再统一记）/ `put_cell_code` / `delete_cell_code`。

LLM 两条路径传各自 origin：格子优化回填 → `origin="llm_optimize"`；规整回填 → `origin="llm_reformat"`。

`scripts/backfill_knowhow_md.py` 传 `origin="backfill"`，`actor` 用它已有的"按 notebook 所有者解析"结果。

- [ ] **Step 5: 传输创世流水**

`backend/app/services/knowhow/transfer.py` 的 `copy_table` 在目标表提交后记一条 `table_create`，`note` 形如 `由《<源表标题>》复制而来`；`move_table` 同理但写"移动而来"。历史本身**不跟着走**（spec §7.3）。

- [ ] **Step 6: 跑测试**

```bash
PYTHONPATH=backend python3 -m pytest backend/tests/test_knowhow_asset_gc.py \
  backend/tests/test_knowhow_asset_gc_trigger.py \
  backend/tests/test_knowhow_history_hooks.py \
  backend/tests/test_knowhow_transfer_service.py \
  backend/tests/test_backfill_knowhow_md.py -n0 -q
```
Expected: 全 PASS

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(knowhow): actor/origin 穿线、历史保护图片、传输记来源流水"
```

---

## Task 14: 前端纯逻辑与 API 客户端

**Files:**
- Create: `frontend/app/knowhow-history-logic.ts`、`frontend/app/knowhow-history-logic.test.mjs`
- Modify: `frontend/app/knowhow-model.ts`
- Test: `frontend/app/knowhow-model.test.mjs`（追加 origin 契约测试）

**Interfaces:**
- Produces:
  - `knowhow-history-logic.ts`：`summarizeChange(change) -> string`、`originLabel(origin) -> string`、`groupChangesByDay(changes) -> {day, changes}[]`、`aggregateDiff(changes) -> DiffResult`、`isStaleHead(seen, actual) -> boolean`
  - `knowhow-model.ts`：`fetchKnowhowHistory`、`fetchKnowhowChange`、`fetchKnowhowHistoryDiff`、`fetchKnowhowCellHistory`、`revertKnowhowTable`、`createKnowhowMilestone`、`deleteKnowhowMilestone`、`pruneKnowhowHistory`

- [ ] **Step 1: 写失败测试**

创建 `frontend/app/knowhow-history-logic.test.mjs`：

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  summarizeChange, originLabel, groupChangesByDay, aggregateDiff, isStaleHead,
} from "./knowhow-history-logic.ts";

const chg = (over = {}) => ({
  seq: 1, kind: "cell_update", actor: "user-1", origin: "user",
  createdAt: "2026-07-22T14:30:00", payload: { cells: [] }, note: "", ...over,
});

test("summarizeChange: 单格改动说清改了几个格子", () => {
  const change = chg({ payload: { cells: [{ rowId: "r1", columnId: "c1" }] } });
  assert.equal(summarizeChange(change), "修改了 1 个格子");
});

test("summarizeChange: 合并格批量写按格子数汇总", () => {
  const change = chg({ payload: { cells: [{ rowId: "r1" }, { rowId: "r2" }, { rowId: "r3" }] } });
  assert.equal(summarizeChange(change), "修改了 3 个格子");
});

test("summarizeChange: 回退说明回到哪里", () => {
  const change = chg({ kind: "revert", payload: { targetSeq: 12 } });
  assert.equal(summarizeChange(change), "回退到 #12");
});

test("summarizeChange: 删列点名列名", () => {
  const change = chg({ kind: "column_delete", payload: { column: { name: "修复方法" } } });
  assert.equal(summarizeChange(change), "删除了列「修复方法」");
});

test("originLabel: 已知来源给中文标签", () => {
  assert.equal(originLabel("llm_reformat"), "格式规整");
  assert.equal(originLabel("llm_optimize"), "表达优化");
  assert.equal(originLabel("import"), "导入");
  assert.equal(originLabel("revert"), "回退");
});

test("originLabel: 普通用户编辑不加标签（避免每条都挂徽章）", () => {
  assert.equal(originLabel("user"), "");
});

test("originLabel: 未知来源原样回显而不是崩掉", () => {
  assert.equal(originLabel("something_new"), "something_new");
});

test("groupChangesByDay: 同一天聚成一组，按天倒序", () => {
  const groups = groupChangesByDay([
    chg({ seq: 3, createdAt: "2026-07-22T09:00:00" }),
    chg({ seq: 2, createdAt: "2026-07-21T18:00:00" }),
    chg({ seq: 1, createdAt: "2026-07-21T08:00:00" }),
  ]);
  assert.deepEqual(groups.map((g) => g.day), ["2026-07-22", "2026-07-21"]);
  assert.deepEqual(groups[1].changes.map((c) => c.seq), [2, 1]);
});

test("aggregateDiff: 反复编辑同一格折叠成一条净变化", () => {
  const result = aggregateDiff([
    chg({ seq: 1, payload: { cells: [{ rowId: "r1", columnId: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 2, payload: { cells: [{ rowId: "r1", columnId: "c1", before: "B", after: "C" }] } }),
  ]);
  assert.deepEqual(result.cells, [{ rowId: "r1", columnId: "c1", before: "A", after: "C" }]);
});

test("aggregateDiff: 改回原样的格子不出现在结果里", () => {
  const result = aggregateDiff([
    chg({ seq: 1, payload: { cells: [{ rowId: "r1", columnId: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 2, payload: { cells: [{ rowId: "r1", columnId: "c1", before: "B", after: "A" }] } }),
  ]);
  assert.deepEqual(result.cells, []);
});

test("isStaleHead: 看到的 head 落后于实际即为陈旧", () => {
  assert.equal(isStaleHead(50, 53), true);
  assert.equal(isStaleHead(53, 53), false);
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && node --test app/knowhow-history-logic.test.mjs
```
Expected: FAIL — `Cannot find module './knowhow-history-logic.ts'`

- [ ] **Step 3: 实现 `knowhow-history-logic.ts`**

```ts
// Knowhow 表版本管理 — 纯逻辑（无 JSX，可被 knowhow-history-logic.test.mjs 直接
// import）。knowhow-history-drawer.tsx / knowhow-cell-history.tsx 含 JSX，Node
// 原生 TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts 可被 node --test 直接 import），
// 故把变更摘要文案 / 来源徽章映射 / 按天分组 / 区间 diff 聚合 / 陈旧判定这些可测
// 纯逻辑单独抽出（镜像 knowhow-panel.tsx <-> knowhow-panel-logic.ts 的既有拆分）。

export type KnowhowChangeKind =
  | "table_create" | "table_meta" | "anchor_set"
  | "column_add" | "column_rename" | "column_kind" | "column_delete"
  | "row_add" | "row_delete" | "cell_update"
  | "cell_code_put" | "cell_code_delete" | "import_append" | "revert";

export interface KnowhowChange {
  seq: number;
  kind: KnowhowChangeKind | string;
  actor: string;
  origin: string;
  createdAt: string;
  note: string;
  payload: Record<string, any>;
}

const ORIGIN_LABELS: Record<string, string> = {
  user: "",
  llm_optimize: "表达优化",
  llm_reformat: "格式规整",
  import: "导入",
  agent: "Agent",
  revert: "回退",
  backfill: "批量回填",
};

export function originLabel(origin: string): string {
  return origin in ORIGIN_LABELS ? ORIGIN_LABELS[origin] : origin;
}

export function summarizeChange(change: KnowhowChange): string {
  const payload = change.payload ?? {};
  switch (change.kind) {
    case "cell_update":
      return `修改了 ${(payload.cells ?? []).length} 个格子`;
    case "row_add":
      return `新增了 ${(payload.rows ?? []).length} 行`;
    case "import_append":
      return `导入追加了 ${(payload.rows ?? []).length} 行`;
    case "row_delete":
      return `删除了 ${(payload.rows ?? []).length} 行`;
    case "column_add":
      return `新增了列「${payload.column?.name ?? ""}」`;
    case "column_delete":
      return `删除了列「${payload.column?.name ?? ""}」`;
    case "column_rename":
      return `列改名：${payload.before} → ${payload.after}`;
    case "column_kind":
      return "修改了列的内容类型";
    case "anchor_set":
      return "修改了行标题列";
    case "table_meta":
      return "修改了表信息";
    case "cell_code_put":
      return "更新了格子代码";
    case "cell_code_delete":
      return "删除了格子代码";
    case "table_create":
      return change.note || "建表";
    case "revert":
      return `回退到 #${payload.targetSeq}`;
    default:
      return "修改";
  }
}

export function groupChangesByDay(
  changes: KnowhowChange[],
): { day: string; changes: KnowhowChange[] }[] {
  const groups: { day: string; changes: KnowhowChange[] }[] = [];
  for (const change of changes) {
    const day = (change.createdAt ?? "").slice(0, 10);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.changes.push(change);
    else groups.push({ day, changes: [change] });
  }
  return groups;
}

export interface DiffCell {
  rowId: string;
  columnId: string;
  before: string | null;
  after: string | null;
}

export interface DiffResult {
  cells: DiffCell[];
  rowsAdded: string[];
  rowsRemoved: string[];
}

export function aggregateDiff(changes: KnowhowChange[]): DiffResult {
  const first = new Map<string, string | null>();
  const last = new Map<string, string | null>();
  const rowState = new Map<string, "added" | "removed">();

  for (const change of changes) {
    for (const cell of change.payload?.cells ?? []) {
      const key = `${cell.rowId} ${cell.columnId}`;
      if (!first.has(key)) first.set(key, cell.before ?? null);
      last.set(key, cell.after ?? null);
    }
    if (change.kind === "row_add" || change.kind === "import_append") {
      for (const row of change.payload?.rows ?? []) {
        if (rowState.get(row.rowId) === "removed") rowState.delete(row.rowId);
        else rowState.set(row.rowId, "added");
      }
    }
    if (change.kind === "row_delete") {
      for (const row of change.payload?.rows ?? []) {
        if (rowState.get(row.rowId) === "added") rowState.delete(row.rowId);
        else rowState.set(row.rowId, "removed");
      }
    }
  }

  const cells: DiffCell[] = [];
  for (const [key, before] of first) {
    const after = last.get(key) ?? null;
    if (before === after) continue;
    const [rowId, columnId] = key.split(" ");
    cells.push({ rowId, columnId, before, after });
  }

  return {
    cells,
    rowsAdded: [...rowState].filter(([, s]) => s === "added").map(([id]) => id),
    rowsRemoved: [...rowState].filter(([, s]) => s === "removed").map(([id]) => id),
  };
}

export function isStaleHead(seenHeadSeq: number, actualHeadSeq: number): boolean {
  return seenHeadSeq !== actualHeadSeq;
}
```

- [ ] **Step 4: 加 API 客户端函数**

在 `frontend/app/knowhow-model.ts` 追加（路径**不带 `/api`**）：

```ts
export const fetchKnowhowHistory = (
  notebookId: string, tableId: string, beforeSeq?: number,
): Promise<KnowhowHistoryPage> => {
  const query = beforeSeq === undefined ? "" : `?before_seq=${beforeSeq}`;
  return requestJson<WireKnowhowHistoryPage>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history${query}`,
    { tag: "knowhow" },
  ).then(mapHistoryPage);
};

export const revertKnowhowTable = (
  notebookId: string, tableId: string, targetSeq: number, expectedHeadSeq: number,
): Promise<KnowhowRevertResult> =>
  requestJson<WireKnowhowRevertResult>(
    `/notebooks/${notebookId}/knowhow/${tableId}/revert`,
    {
      method: "POST",
      body: JSON.stringify({ target_seq: targetSeq, expected_head_seq: expectedHeadSeq }),
      tag: "knowhow",
    },
  ).then(mapRevertResult);
```

其余 6 个照同一形状写，签名逐一列明（**路径一律不带 `/api`**）：

```ts
export const fetchKnowhowChange = (
  notebookId: string, tableId: string, seq: number,
): Promise<KnowhowChange> =>
  requestJson<WireKnowhowChange>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/${seq}`, { tag: "knowhow" },
  ).then(mapChange);

export const fetchKnowhowHistoryDiff = (
  notebookId: string, tableId: string, fromSeq: number, toSeq: number,
): Promise<KnowhowHistoryDiff> =>
  requestJson<WireKnowhowHistoryDiff>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/diff?from=${fromSeq}&to=${toSeq}`,
    { tag: "knowhow" },
  ).then(mapHistoryDiff);

export const fetchKnowhowCellHistory = (
  notebookId: string, tableId: string, rowId: string, columnId: string,
): Promise<KnowhowCellHistoryEntry[]> =>
  requestJson<WireKnowhowCellHistoryEntry[]>(
    `/notebooks/${notebookId}/knowhow/${tableId}/rows/${rowId}/cells/${columnId}/history`,
    { tag: "knowhow" },
  ).then((entries) => entries.map(mapCellHistoryEntry));

export const createKnowhowMilestone = (
  notebookId: string, tableId: string, seq: number, name: string, note = "",
): Promise<KnowhowMilestone> =>
  requestJson<WireKnowhowMilestone>(
    `/notebooks/${notebookId}/knowhow/${tableId}/milestones`,
    { method: "POST", body: JSON.stringify({ seq, name, note }), tag: "knowhow" },
  ).then(mapMilestone);

export const deleteKnowhowMilestone = (
  notebookId: string, tableId: string, milestoneId: string,
): Promise<void> =>
  requestVoid(
    `/notebooks/${notebookId}/knowhow/${tableId}/milestones/${milestoneId}`,
    { method: "DELETE", tag: "knowhow" },
  );

export const pruneKnowhowHistory = (
  notebookId: string, tableId: string, beforeDays: number,
): Promise<{ removed: number }> =>
  requestJson<{ removed: number }>(
    `/notebooks/${notebookId}/knowhow/${tableId}/history/prune`,
    { method: "POST", body: JSON.stringify({ before_days: beforeDays }), tag: "knowhow" },
  );
```

同时给 `patchKnowhowCell` 加第 7 个位置参数 `origin?: string`（接在 `expectedBefore` 之后），`batchPatchKnowhowCells` 的 `KnowhowCellsBatchPatchInput` 加可选字段 `origin?: string`。两者都按既有"省略时不进 body"手法装配 —— 那个行为被 `knowhow-model.test.mjs` 的既有断言锁死了：

```ts
// patchKnowhowCell 内部
body: JSON.stringify({
  content_md: contentMd,
  ...(expectedBefore === undefined ? {} : { expected_before: expectedBefore }),
  ...(origin === undefined ? {} : { origin }),
}),

// batchPatchKnowhowCells 内部，接在既有的逐键装配之后
if (input.origin !== undefined) body.origin = input.origin;
```

- [ ] **Step 5: 加 wire 契约测试**（追加到 `knowhow-model.test.mjs`，仿既有 `withFetchStub`/`bodyOf` 样板）

```js
test("patchKnowhowCell: 省略 origin 时请求体不带该字段", () => {
  const wire = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wire, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "x" });
  });
});

test("patchKnowhowCell: 传 origin 时请求体带 origin（从历史恢复要能区分来源）", () => {
  const wire = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wire, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x", undefined, "revert");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "x", origin: "revert" });
  });
});

test("revertKnowhowTable: POST 到 /revert，请求体锁定字段名", () => {
  return withFetchStub({ seq: 54, target_seq: 12 }, async (calls) => {
    await revertKnowhowTable("nb-1", "t1", 12, 53);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/revert$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { target_seq: 12, expected_head_seq: 53 });
  });
});
```

- [ ] **Step 6: 跑测试**

```bash
cd frontend && node --test app/knowhow-history-logic.test.mjs app/knowhow-model.test.mjs && npx tsc --noEmit
```
Expected: 全 PASS，tsc 无输出

- [ ] **Step 7: 提交**

```bash
git add frontend/app/knowhow-history-logic.ts frontend/app/knowhow-history-logic.test.mjs \
        frontend/app/knowhow-model.ts frontend/app/knowhow-model.test.mjs
git commit -m "feat(knowhow): 前端历史纯逻辑与 API 客户端"
```

---

## Task 15: 历史抽屉

**Files:**
- Create: `frontend/app/knowhow-history-drawer.tsx`
- Modify: `frontend/app/knowhow-panel.tsx`（状态槽位 + 三处清空 + 工具栏按钮 + CSS）

**Interfaces:**
- Consumes: Task 14 的纯函数与 fetcher
- Produces: `<KnowhowHistoryDrawer notebookId tableId canEdit onClose onReverted />`

- [ ] **Step 1: 写组件**

创建 `frontend/app/knowhow-history-drawer.tsx`，骨架**逐处照抄 `knowhow-matrix-drawer.tsx`**（`kh-modal-overlay` → `kh-modal-card` → `kh-modal-header`（含 `kh-modal-header-top`、面包屑、全屏切换、关闭）→ `kh-modal-body` → 可选 `kh-modal-footer` → `kh-modal-resize-handle`）：

```tsx
const HISTORY_DRAWER_FULLSCREEN_STORAGE_KEY = "knowhow.historyDrawer.fullscreen";

export function KnowhowHistoryDrawer({
  notebookId, tableId, tableTitle, canEdit, onClose, onReverted,
}: {
  notebookId: string; tableId: string; tableTitle: string;
  canEdit: boolean; onClose: () => void; onReverted: () => void;
}) {
  const [fullscreen, toggleFullscreen] = useFullscreenToggle(
    HISTORY_DRAWER_FULLSCREEN_STORAGE_KEY,
  );
  const floating = useFloatingWindow({
    storageKey: "knowhow.historyDrawer.window", disabled: fullscreen,
  });

  const [changes, setChanges] = useState<KnowhowChange[]>([]);
  const [milestones, setMilestones] = useState<KnowhowMilestone[]>([]);
  const [headSeq, setHeadSeq] = useState(0);
  const [expandedSeq, setExpandedSeq] = useState<number | null>(null);
  const [compareFrom, setCompareFrom] = useState<number | null>(null);
  const [compareTo, setCompareTo] = useState<number | null>(null);
  const [milestonesOnly, setMilestonesOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const reload = useCallback(() => {
    fetchKnowhowHistory(notebookId, tableId)
      .then((page) => {
        setChanges(page.changes);
        setMilestones(page.milestones);
        setHeadSeq(page.headSeq);
      })
      .catch(() => setError("加载历史失败，请重试"));
  }, [notebookId, tableId]);

  useEffect(reload, [reload]);

  const days = useMemo(
    () => groupChangesByDay(
      milestonesOnly
        ? changes.filter((c) => milestones.some((m) => m.seq === c.seq))
        : changes,
    ),
    [changes, milestones, milestonesOnly],
  );

  async function handleRevert(targetSeq: number) {
    setPending(true);
    setError(null);
    try {
      await revertKnowhowTable(notebookId, tableId, targetSeq, headSeq);
      reload();
      onReverted();
    } catch (err) {
      // 409 陈旧：自动刷新到最新，让用户在新事实上重新决定，而不是盲目重试
      reload();
      setError(extractErrorMessage(err, "回退失败，请重试"));
    } finally {
      setPending(false);
    }
  }
  // ↓ 渲染部分见 Step 1 正文描述的骨架
}
```

要点：
- **全屏 sessionStorage 键必须新开一个**（不要复用矩阵抽屉的 `CONCEPT_DRAWER_FULLSCREEN_STORAGE_KEY`），两个弹窗的全屏选择互不影响。
- 回退按钮走二次确认（仿 `ManageRowItem` 的 `confirming` 局部状态），确认框里显示"将影响 N 行、M 个格子"（用 `aggregateDiff` 现算）。
- **只读成员看得到时间线和 diff，但 `canEdit=false` 时不渲染回退/里程碑按钮**。
- 回退成功后调 `onReverted()`，由 panel 去重拉表详情。
- 陈旧 409 的处理：捕获错误后自动重拉历史并提示"这张表刚被其他人改过，已刷新，请重新确认"。

- [ ] **Step 2: 在 panel 里接线**

`frontend/app/knowhow-panel.tsx`：

1. 加状态：`const [historyOpen, setHistoryOpen] = useState(false);`
2. **三处清空各补一行 `setHistoryOpen(false);`** —— `openTable()`（约 `L533-556`）、`backToList()`（约 `L558-575`）、notebook 切换 `useEffect`（约 `L446-466`）。漏了会出现"切到另一张表，历史抽屉还开着但内容是上一张表的"。
3. JSX 挂载：**排在 `cellModal` 渲染之前**（与 `KnowhowMatrixDrawer` 同一规则：共用 `.kh-modal-overlay`，DOM 顺序决定层叠，抽屉内若开 cellModal 必须能盖在上面）。
4. `KnowhowTableGrid` 的 props 类型（约 `L3355-3438`）加 `onOpenHistory: () => void`，工具栏（`knowhow-grid-toolbar-actions`，约 `L3468-3529`）加按钮。**仿"复制/移动到…"那样不整体挂 `canEdit`**：

```tsx
  <button type="button" className="sort-button knowhow-reproject-button"
    onClick={onOpenHistory} disabled={!detail || deleting} title="查看这张表的变更历史">
    <History size={14} />
    历史
  </button>
```

（`History` 从 `lucide-react` 引入，加进该文件既有的 import 列表。）

- [ ] **Step 3: 加 CSS**

在 `knowhow-panel.tsx` 那**唯一**的 `<style jsx global>` 块里追加 `.kh-history-*` 样式（时间线条目、origin 徽章、diff 红绿块、里程碑旗标）。不要在新组件文件里另开 `<style jsx>`。

- [ ] **Step 4: 验证**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Expected: tsc 无输出，测试全绿

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-history-drawer.tsx frontend/app/knowhow-panel.tsx
git commit -m "feat(knowhow): 历史抽屉（时间线 + diff + 两版对比 + 回退）"
```

---

## Task 16: 格子浮窗历史页签

**Files:**
- Create: `frontend/app/knowhow-cell-history.tsx`
- Modify: `frontend/app/knowhow-panel.tsx`（`cellModal` 类型扩三态 + 渲染分支）、`frontend/app/knowhow-cell-editor.tsx`（两处入口按钮 + 模式徽标）

- [ ] **Step 1: 扩三态**

`knowhow-panel.tsx` 约 `L282-284`：

```tsx
const [cellModal, setCellModal] = useState<{
  rowId: string; columnId: string; mode: "preview" | "edit" | "history";
} | null>(null);
```

渲染处（约 `L1244-1284`）从二选一改三选一：

```tsx
{cellModal && cellModalRow && cellModalColumn && detail && (
  cellModal.mode === "edit" ? (
    <KnowhowCellEditor ... />
  ) : cellModal.mode === "history" ? (
    <KnowhowCellHistory ... onBack={() => setCellModal((c) => c ? { ...c, mode: "preview" } : c)} />
  ) : (
    <KnowhowCellPreview ... />
  )
)}
```

- [ ] **Step 2: 写 `KnowhowCellHistory`**

仿 `KnowhowCellPreview`（`knowhow-cell-editor.tsx:465-564`）复用同一套外壳。**`useFloatingWindow` 的 storageKey 必须仍是 `"knowhow.cellModal.window"`**，`useFullscreenToggle` 仍用 `FULLSCREEN_STORAGE_KEY` —— 三态共用同一个键，切页签时浮窗位置才不会跳（既有两态就是这么做的，文件里有注释说明）。

内容：调 `fetchKnowhowCellHistory` 列出这一格历次值，每条显示时间/操作者/origin 徽章 + 内容预览（`KnowhowMarkdown` 渲染），带「恢复此版本」按钮（`canEdit` 门控）。恢复走既有 `patchKnowhowCell(..., origin: "revert")`。

- [ ] **Step 3: 加入口按钮与模式徽标**

- `KnowhowCellPreview` 的 header actions（约 `L529-533`）在「编辑」旁加「历史」按钮。
- `KnowhowCellEditor` 的 header actions（约 `L1381-1394`）同样加一个。
- 两处的 `kh-mode-tag` 徽标（预览态 `L524-526`、编辑态 `L1374-1376`）加第三种文案；新增常量 `const HISTORY_MODE_TAG = "历史";` 放在既有 `PREVIEW_MODE_TAG` / `EDITING_MODE_TAG`（`L288-289`）旁边。

- [ ] **Step 4: 验证**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Expected: tsc 无输出，测试全绿

- [ ] **Step 5: 提交**

```bash
git add frontend/app/knowhow-cell-history.tsx frontend/app/knowhow-panel.tsx frontend/app/knowhow-cell-editor.tsx
git commit -m "feat(knowhow): 格子浮窗历史页签与单格恢复"
```

---

## Task 17: 表管理面板「清理历史」

**Files:**
- Modify: `frontend/app/knowhow-manage.tsx`、`frontend/app/knowhow-manage-logic.ts`

- [ ] **Step 1: 加文案常量**

`knowhow-manage-logic.ts`（`L221-222` 旁）：

```ts
export const HISTORY_PRUNE_CONFIRM =
  "清理后将无法回退到该时间点之前，同时会释放这些历史引用的图片";
```

- [ ] **Step 2: 加第五个分区**

`knowhow-manage.tsx` 的四个 `<section className="knowhow-manage-section">` 之后（约 `L596`）插入「历史管理」分区：天数输入（默认 180）+「清理」按钮 + 二次确认（仿 `ManageRowItem` 的 `confirming` 局部状态，`L710-751`）。写操作走既有统一执行器：

```tsx
run(() => pruneKnowhowHistory(notebookId, detail.id, beforeDays))
```

- [ ] **Step 3: 验证**

```bash
cd frontend && npx tsc --noEmit && npm test
```
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
git add frontend/app/knowhow-manage.tsx frontend/app/knowhow-manage-logic.ts
git commit -m "feat(knowhow): 表管理面板加清理历史"
```

---

## Task 18: 收尾 —— 文档、全量门禁、真机验证

- [ ] **Step 1: 补文档**

`architecture.md` 与 `AGENTS.md` 的 knowhow 章节各加一段版本管理说明（变更流水 + 指纹守卫 + 回退语义 + 里程碑零存储 + 历史保护图片的代价）。

- [ ] **Step 2: 跑全量后端门禁**

```bash
bash scripts/check_backend.sh
```
Expected: 全绿。**如有失败先确认是不是 `backend-suite-known-flakes` 里记的既有 flake**（`kg_merge` 2s 性能硬阈值真脆），别急着认领。

- [ ] **Step 3: 跑全量前端门禁**

```bash
cd frontend && npm test && npx tsc --noEmit
```
Expected: 全绿

- [ ] **Step 4: 复核派生物全部刷过**

```bash
PYTHONPATH=backend python3 -m pytest \
  backend/tests/test_repository_surface_contract.py \
  backend/tests/test_repository_dependency_contract.py \
  backend/tests/test_repository_api_contract.py \
  backend/tests/test_repository_facade_contract.py \
  backend/tests/test_architecture_documentation.py \
  backend/tests/test_repository_snapshot_verifier.py -q
```
Expected: 全绿

- [ ] **Step 5: 真机走查**

后端需**重启**（SCHEMA_VERSION 23→24）。用户自己启服务（不代劳）。走查清单：

1. 打开一张已有 knowhow 表 → 工具栏有「历史」按钮 → 点开是空时间线 + 上线断层提示
2. 改一个格子 → 历史里出现一条，点开有红绿 diff
3. 用「表达优化」改一格 → 时间线上带「表达优化」徽章
4. 给某条打里程碑 → 旗标显示
5. 删一行 → 回退 → 行回来了、行内代码附件也回来了
6. 格子浮窗「历史」页签 → 恢复一个旧版本 → 时间线上出现「回退」来源的新条目
7. 格子里放图 → 删掉图 → 等清扫 → 图还在（历史保护）→ 清理历史 → 图被回收
8. 只读成员账号打开 → 能看时间线和 diff，看不到回退/里程碑/清理按钮

- [ ] **Step 6: rebase 并提 PR**

```bash
git fetch origin && git rebase origin/master
bash scripts/check_backend.sh    # rebase 后重跑，防合并语义冲突
git push -u origin claude/knowhow-table-version-control-4162d4
gh pr create --base master --title "feat(knowhow): 表版本管理（变更流水 + 回退）" --body "..."
```

---

## 附录：本计划依赖的已核实事实

写计划时逐一验证过，实现时可以直接信：

| 事实 | 出处 |
|---|---|
| `migrate()` 靠 `getattr(self, f"_migration_{version}")` 反射，无注册表 | `migrations.py:1661-1672` |
| 结构性列变更**不** bump `mutation_seq` | `knowhow_store.py` 各列方法 + `knowhow_transfer_store.py:378` 注释 |
| 指纹**不含** `updated_at` —— 后置守卫成立的前提 | `knowhow_transfer_store.py:308-331` |
| 两个 guarded 写方法 phase 1 已读出 before | `knowhow_store.py:929+` docstring |
| 行号钉死的守卫已被 #307 删除，新守卫按语义身份 | `test_repository_surface_contract.py:38-63` |
| `_assert_baseline_sources` 只挡 `--rebaseline` | `generate_repository_contract_fixtures.py:2596-2607` |
| 清扫器的引用判定有**两处**，都要改 | `maintenance.py:794-800` 与 `:904-913` |
| `project_table` 永远是全量；一律走 `get_scheduler().schedule()` | `services/knowhow/api.py:641-667`、`knowhow_routes.py:204-221` |
| `API_BASE` 默认值已含 `/api` | `frontend/app/api-config.ts:1-4` |
| `.tsx` 不能被 `node --test` import | 三个 `-logic.ts` 文件头注释 |
| styled-jsx global 样式必须留在 `knowhow-panel.tsx` | `knowhow-matrix-drawer.tsx:14-20` |
| 新 modal 状态要在三处清空 | `knowhow-panel.tsx:446-466`、`533-556`、`558-575` |
