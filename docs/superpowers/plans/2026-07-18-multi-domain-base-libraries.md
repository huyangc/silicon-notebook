# 多领域基准库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「全局唯一的隐式基准库」换成「每个笔记本显式声明的参考库挂载集合」，使不同半导体子领域各有独立的公共知识库，用户可为自己的笔记本挂 0..N 个参考库。

**Architecture:** 新增关联表 `notebook_bases` 记录挂载边。检索侧原本就是集合形状的（`NotebookStore.participant_*` 返回 `[active] + 所有 tier='base' 的库`），本次只把那个谓词换成「查本库挂载了谁」，三个函数的签名与返回形状保持不变，下游全部免费兼容。`tier='base'` 保留但不再唯一，降格为「admin 发布的公共知识库」标记。

**Tech Stack:** Python 3.13 / FastAPI / pydantic v2 / SQLite（手写迁移，无 Alembic）/ pytest；Next.js + React + TypeScript，前端纯逻辑单测用 `node --test`。

## Global Constraints

以下约束适用于**每一个** task，不再逐条重复：

- **迁移双写**：新表/新列必须**同时**写进 `migrations.py` 的 `_migration_1` baseline（服务全新库）**和**新的 `_migration_20`（服务已部署库），并把 `SCHEMA_VERSION` 从 19 bump 到 20。只写其一会让已部署库漏建表——`user_version >= 1` 时 `_migration_1` 被版本闸短路，`CREATE TABLE IF NOT EXISTS` 根本执行不到（`migrations.py:807-816` 有踩坑记录）。
- **不改既有表的物理列序**：新列一律 `ALTER TABLE ADD COLUMN` 追加在末尾。
- **可挂范围**：公共知识库（`tier='base'`）+ **被挂笔记本的 `created_by` 等于挂载方笔记本的 `created_by`**。刻意排除只读分享（`notebook_members`）——对方撤销分享后边仍在会成为越权通道。
- **边不是授权凭证**：有效性在**解析时**实时判定，不是挂载时判定一次。失效的边**保留不删**，仅跳过。
- **前端弯引号**：`page.tsx` 中文文案里的 `“”` 是合法 JSX 文本，禁止批量替换为直引号。校验：`git diff | grep -c '^-.*[“”]'` 必须为 `0`。
- **新增 UI 文案用定稿词汇**：`tier='base'` → 「公共知识库」；挂载关系 → 「参考库」。不主动清理存量「基准库」措辞（属词汇整改 PR B 范围），除非该处措辞因本次语义变化而**错误**。
- **一个 PR**：两条实施流程（A 后端、B 前端）合入同一分支、同一 PR。A 的每个 task 独立可测、可提交；B 紧随其后。不得只交付 A。
- **挂载谓词只有一个定义点**：`backend/app/repositories/sqlite/mount_sql.py`（Task 2 建）。任何用到 `MOUNT_JOIN` / `MOUNT_VALID` / `MOUNT_VALID_EXPR` / `MOUNT_ORDER` / `MOUNTED_BASE_IDS_SUBQUERY` 的文件，一律 `from app.repositories.sqlite.mount_sql import ...`。**禁止手写或复制这个谓词** —— 五份逐字副本里任何一份漂移，都会让「能检索到」与「界面显示挂着」不一致，且没有测试会自然抓到。
- **本机 Python**：`/opt/homebrew/Caskroom/miniconda/base/bin/python`（共享 conda，带依赖）。后端测试从 `backend/` 目录跑。

---

## File Structure

**新建**

| 文件 | 职责 |
| --- | --- |
| `backend/tests/test_multi_domain_bases.py` | 本特性的全部后端测试（解析、权限、失效边、迁移、治理） |
| `frontend/app/notebook-bases.ts` | 参考库客户端：类型 + fetch 封装 + 纯逻辑（可挂候选分组、成本提示阈值） |
| `frontend/app/notebook-bases.test.mjs` | 上者的 `node --test` 单测 |

**修改**

| 文件 | 改什么 |
| --- | --- |
| `backend/app/repositories/sqlite/migrations.py` | baseline 加表、`_migration_20`、`SCHEMA_VERSION=20` |
| `backend/app/repositories/sqlite/notebook_store.py` | 挂载边 CRUD + `resolve_participants` + 改写三个 `participant_*` + `set_tier` 去唯一性 |
| `backend/app/repositories/sqlite/knowledge_store.py` | `any_base_has_kg*` 收 notebook_id；follow_chain 起点门 |
| `backend/app/repositories/sqlite/query_store.py` | `base_notebook_info_row` → per-notebook 的挂载列表 |
| `backend/app/repositories/sqlite/unified_kg_store.py` | `first_base_notebook_id` → `mounted_base_ids`（集合） |
| `backend/app/repositories/sqlite/governance_store.py` | 晋升目标读 `target_base_id` |
| `backend/app/services/notebook_catalog.py` | `base_notebook_info` → `mounted_bases` |
| `backend/app/services/scale_artifact_runtime.py` + `notebook_scale.py` | eligible 追加「被挂载」分支 |
| `backend/app/services/sqlite_repository.py` | 新增 facade 一跳委托 |
| `backend/app/models/schemas.py` | `NotebookRef`/`MountedBase`/`SetBasesRequest`；`NotebookSummary.base_notebooks` |
| `backend/app/api/routes.py` | 三个新端点 + tier 端点文案 |
| `scripts/merge_dbs.py` | 多 base 时明确报错退出 |
| `README.md` / `README_zh.md` / `AGENTS.md` | 四处失效叙述 |
| `frontend/app/notebook-tier.ts` | 三态 → 二态（删 `replace`） |
| `frontend/app/page.tsx` | 编辑表单参考库多选、唯一性文案、引导文案 |
| `frontend/app/answer-panel.tsx` / `report-view.tsx` | 徽章带库名、分布徽章文案 |
| `backend/tests/test_two_tier_federated.py` | 反转唯一性断言、改写 `base_notebook_name` 用例 |
| `backend/tests/test_architecture_hardening.py` | `test_federated_large_guard_includes_base_notebooks` 补挂载 |

---

# 流程 A：后端 + 契约

## Task 1: schema — `notebook_bases` 表与 `target_base_id` 列

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py:14`（`SCHEMA_VERSION`）、`:81-90`（notebooks baseline 附近加表）、文件末尾 `_migration_19` 之后
- Test: `backend/tests/test_multi_domain_bases.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: 表 `notebook_bases(notebook_id, base_notebook_id, created_at, created_by)`，主键 `(notebook_id, base_notebook_id)`，索引 `idx_notebook_bases_base`；列 `promotion_candidates.target_base_id TEXT NOT NULL DEFAULT ''`；`SCHEMA_VERSION == 20`

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_multi_domain_bases.py`：

```python
"""多领域基准库 —— 挂载集合取代全局唯一 base。"""
import sqlite3

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


class TestSchema:
    def test_schema_version_is_20(self):
        assert SCHEMA_VERSION == 20

    def test_fresh_db_has_notebook_bases(self, repo):
        with repo._connect() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(notebook_bases)")}
        assert cols == {"notebook_id", "base_notebook_id", "created_at", "created_by"}

    def test_promotion_candidates_has_target_base_id(self, repo):
        with repo._connect() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(promotion_candidates)")}
        assert "target_base_id" in cols

    def test_self_mount_rejected_by_check(self, repo):
        nb = repo.create_notebook(NotebookCreate(name="a"))
        with pytest.raises(sqlite3.IntegrityError):
            with repo.database.write() as db:
                db.execute(
                    "INSERT INTO notebook_bases"
                    "(notebook_id, base_notebook_id, created_at, created_by)"
                    " VALUES (?,?,?,?)",
                    (nb.id, nb.id, "2026-07-18T00:00:00Z", "user-local"),
                )

    def test_deleting_mounted_notebook_cascades_edge(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b = repo.create_notebook(NotebookCreate(name="b"))
        with repo.database.write() as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "INSERT INTO notebook_bases"
                "(notebook_id, base_notebook_id, created_at, created_by)"
                " VALUES (?,?,?,?)",
                (a.id, b.id, "2026-07-18T00:00:00Z", "user-local"),
            )
        repo.delete_notebook(b.id)
        with repo._connect() as db:
            left = db.execute(
                "SELECT COUNT(*) FROM notebook_bases WHERE notebook_id=?", (a.id,)
            ).fetchone()[0]
        assert left == 0

    def test_migration_20_backfills_deployed_db(self, tmp_path, monkeypatch):
        """已部署库(user_version=19)升级后必须建出表 —— baseline 会被版本闸短路。"""
        db_path = tmp_path / "old.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        SQLiteRepository(Settings())  # 建到最新
        with sqlite3.connect(db_path) as raw:  # 人为退回 19 并删表，模拟老库
            raw.execute("DROP TABLE IF EXISTS notebook_bases")
            raw.execute("PRAGMA user_version = 19")
        SQLiteRepository(Settings())  # 重新迁移
        with sqlite3.connect(db_path) as raw:
            names = {
                r[0] for r in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            version = raw.execute("PRAGMA user_version").fetchone()[0]
        assert "notebook_bases" in names
        assert version == 20
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py -q
```

Expected: FAIL —— `assert 19 == 20`，以及 `PRAGMA table_info(notebook_bases)` 返回空导致 `cols == set()`。

- [ ] **Step 3: bump SCHEMA_VERSION**

`migrations.py:14`，把 `SCHEMA_VERSION = 19` 改成：

```python
SCHEMA_VERSION = 20
```

- [ ] **Step 4: 写进 baseline**

`migrations.py`，在 `CREATE TABLE IF NOT EXISTS notebooks (...)` 那段之后（`:90` 收尾的 `);` 之后）插入：

```sql
                CREATE TABLE IF NOT EXISTS notebook_bases (
                  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  created_by TEXT REFERENCES users(id),
                  PRIMARY KEY (notebook_id, base_notebook_id),
                  CHECK (notebook_id != base_notebook_id)
                );
```

并在同一 baseline 块里已有的 `CREATE INDEX` 群落中追加：

```sql
                CREATE INDEX IF NOT EXISTS idx_notebook_bases_base
                  ON notebook_bases(base_notebook_id);
```

- [ ] **Step 5: 写 `_migration_20`**

`migrations.py`，紧接 `_migration_19` 之后（`:1425` 之后、`_recover_interrupted_jobs` 之前）：

```python
    def _migration_20(self) -> None:
        """多领域基准库：参考库挂载边 notebook_bases + 晋升目标 target_base_id。

        基准库不再全局唯一——每个 notebook 显式声明挂载哪些库(0..N)，检索参与集
        由本表解析而非 `WHERE tier='base'`。已部署库(user_version>=1 时
        _migration_1 短路)靠本迁移补建，与 _migration_2/_migration_4/_migration_19
        同款。CREATE TABLE/INDEX IF NOT EXISTS + PRAGMA 列存在性守卫保证可重入。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS notebook_bases (
                  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  created_by TEXT REFERENCES users(id),
                  PRIMARY KEY (notebook_id, base_notebook_id),
                  CHECK (notebook_id != base_notebook_id)
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebook_bases_base "
                "ON notebook_bases(base_notebook_id)"
            )
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(promotion_candidates)").fetchall()
            }
            if "target_base_id" not in cols:
                db.execute(
                    "ALTER TABLE promotion_candidates "
                    "ADD COLUMN target_base_id TEXT NOT NULL DEFAULT ''"
                )
```

- [ ] **Step 6: 把 `target_base_id` 也写进 baseline**

在 `migrations.py` 的 `CREATE TABLE IF NOT EXISTS promotion_candidates (...)`（约 `:416` 附近，`base_match_id` 那一列旁）末尾追加一列：

```sql
                  target_base_id TEXT NOT NULL DEFAULT '',
```

放在该表**最后一个字段**位置，与 `_migration_20` 的 `ALTER ... ADD COLUMN` 得到的物理列序一致。

- [ ] **Step 7: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py -q
```

Expected: 6 passed。

- [ ] **Step 8: 跑既有 schema 相关测试确认未回归**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/ -q -k "schema or migration"
```

Expected: 全绿。若有断言 `SCHEMA_VERSION == 19` 的用例，改为 20。

- [ ] **Step 9: 提交**

```bash
git add backend/app/repositories/sqlite/migrations.py backend/tests/test_multi_domain_bases.py
git commit -m "feat(schema): notebook_bases 挂载表 + target_base_id，SCHEMA_VERSION 20"
```

---

## Task 2: 解析层 —— 挂载边 CRUD 与 `participant_*` 改写

这是整个特性的语义定义点。三个 `participant_*` 的**签名与返回形状保持不变**，只换内部谓词，下游全部免费兼容。

**Files:**
- Modify: `backend/app/repositories/sqlite/notebook_store.py:17-86`（类文档 + 三个 participant 函数）、新增挂载 CRUD
- Test: `backend/tests/test_multi_domain_bases.py`

**Interfaces:**
- Consumes: Task 1 的 `notebook_bases` 表
- Produces:
  - **`backend/app/repositories/sqlite/mount_sql.py`** —— 挂载有效性谓词的**唯一定义点**，导出 `MOUNT_JOIN` / `MOUNT_VALID` / `MOUNT_ORDER` / `MOUNTED_BASE_IDS_SUBQUERY`。Task 4/5/7 一律 import，**禁止再手写这个谓词**（五份逐字副本里任何一份漂移，都会让「能检索到」与「界面显示挂着」不一致）
  - `NotebookStore.resolve_participants(db, active_notebook_id) -> list[tuple[str, str]]` —— `[(notebook_id, tier)]`，首项恒为 active 本身
  - `NotebookStore.participant_ids(db, active) -> list[str]`（形状不变）
  - `NotebookStore.participant_rows(db, active) -> (active_row, base_rows)`（形状不变）
  - `NotebookStore.participant_tiers(db, active) -> (list[str], dict[str, str])`（形状不变）
  - `NotebookStore.list_mount_edges(db, notebook_id) -> list[dict]` —— 含 `id/name/tier/active/inactive_reason`
  - `NotebookStore.replace_mounts(notebook_id, base_ids, created_by) -> None`
  - `NotebookStore.mountable_notebooks(db, notebook_id) -> list[dict]` —— `id/name/tier`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_multi_domain_bases.py`：

```python
def _mount(repo, notebook_id, base_ids):
    repo.replace_notebook_bases(notebook_id, base_ids, "user-local")


class TestResolve:
    def test_no_mount_means_only_self(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        with repo._connect() as db:
            got = repo.notebooks.resolve_participants(db, a.id)
        assert got == [(a.id, "personal")], "未挂载就不该吃到任何 base"

    def test_mounted_public_base_participates(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        with repo._connect() as db:
            got = dict(repo.notebooks.resolve_participants(db, a.id))
        assert got == {a.id: "personal", base.id: "base"}

    def test_multi_mount(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        _mount(repo, a.id, [b1.id, b2.id])
        with repo._connect() as db:
            ids = repo.notebooks.participant_ids(db, a.id)
        assert set(ids) == {a.id, b1.id, b2.id}

    def test_mounting_own_personal_notebook_keeps_personal_tier(self, repo):
        a = repo.create_notebook(NotebookCreate(name="项目"))
        mine = repo.create_notebook(NotebookCreate(name="我的模拟笔记"))
        _mount(repo, a.id, [mine.id])
        with repo._connect() as db:
            got = dict(repo.notebooks.resolve_participants(db, a.id))
        assert got[mine.id] == "personal", "挂自己的库不应被提升为 base"

    def test_mounting_is_not_transitive(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b = repo.create_notebook(NotebookCreate(name="b"))
        c = repo.create_notebook(NotebookCreate(name="c"))
        repo.mark_notebook_base(c.id)
        _mount(repo, b.id, [c.id])
        _mount(repo, a.id, [b.id])
        with repo._connect() as db:
            ids = repo.notebooks.participant_ids(db, a.id)
        assert set(ids) == {a.id, b.id}, "只看直接挂的一跳，C 不应出现"

    def test_edge_to_other_owners_personal_notebook_is_skipped(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        theirs = repo.create_notebook(NotebookCreate(name="别人的"))
        _mount(repo, a.id, [theirs.id])
        with repo.database.write() as db:  # 模拟易主
            db.execute(
                "UPDATE notebooks SET created_by='someone-else' WHERE id=?", (theirs.id,)
            )
        with repo._connect() as db:
            ids = repo.notebooks.participant_ids(db, a.id)
        assert ids == [a.id], "边不是授权凭证，易主后必须跳过"

    def test_demoted_public_base_is_skipped_then_restored(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        with repo.database.write() as db:  # 公共库不是我的，且被降级
            db.execute(
                "UPDATE notebooks SET created_by='someone-else' WHERE id=?", (base.id,)
            )
        repo.set_notebook_personal(base.id)
        with repo._connect() as db:
            assert repo.notebooks.participant_ids(db, a.id) == [a.id]
        repo.mark_notebook_base(base.id)  # 重新发布 → 边自动恢复（从未删除）
        with repo._connect() as db:
            assert set(repo.notebooks.participant_ids(db, a.id)) == {a.id, base.id}

    def test_list_mount_edges_reports_inactive(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        _mount(repo, a.id, [base.id])
        with repo.database.write() as db:
            db.execute(
                "UPDATE notebooks SET created_by='someone-else' WHERE id=?", (base.id,)
            )
        repo.set_notebook_personal(base.id)
        edges = repo.list_notebook_bases(a.id)
        assert len(edges) == 1
        assert edges[0]["active"] is False
        assert edges[0]["inactive_reason"]

    def test_replace_mounts_is_full_replacement(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="b1"))
        b2 = repo.create_notebook(NotebookCreate(name="b2"))
        _mount(repo, a.id, [b1.id])
        _mount(repo, a.id, [b2.id])
        with repo._connect() as db:
            assert set(repo.notebooks.participant_ids(db, a.id)) == {a.id, b2.id}

    def test_mountable_excludes_self_and_others_personal(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        mine = repo.create_notebook(NotebookCreate(name="mine"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        theirs = repo.create_notebook(NotebookCreate(name="theirs"))
        repo.mark_notebook_base(base.id)
        with repo.database.write() as db:
            db.execute(
                "UPDATE notebooks SET created_by='someone-else' WHERE id=?", (theirs.id,)
            )
        got = {n["id"] for n in repo.mountable_notebooks(a.id)}
        assert got == {mine.id, base.id}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestResolve -q
```

Expected: FAIL —— `AttributeError: 'SQLiteRepository' object has no attribute 'replace_notebook_bases'`。

- [ ] **Step 3a: 建共享 SQL 片段模块**

新建 `backend/app/repositories/sqlite/mount_sql.py`：

```python
"""参考库挂载(notebook_bases)的 SQL 片段 —— 「哪些挂载边有效」的唯一定义点。

参与集解析、KG 可用性门、summary 投影、社区扩展、晋升目标这五处都要按挂载筛选。
若各自手写谓词,任何一份副本漂移都会让「能检索到」与「界面显示挂着」不一致,而且
这种不一致没有任何测试会自然抓到。故谓词只在这里定义一次,五处一律 import。

「有效」是解析时的实时判定而非挂载时的一次性校验:挂载边不是授权凭证。可挂范围=
公共知识库(tier='base') 或与挂载方同 owner 的库;被挂库易主、或公共库被降级后,
边保留但不生效(降级/转让常是临时的,静默删掉用户配置无法撤销),重新满足条件即
自动恢复。owner 取「挂载方笔记本的 created_by」而非请求用户,使参与集与「谁在
提问」无关 —— 只读共享的访客与库主必须看到同一个参与集。

用法:全部片段都恰好消费**一个**位置参数(挂载方 notebook_id)。
"""

# 挂载边的 join 骨架(不含有效性过滤)—— 需要连失效边一起看的场景直接用它。
MOUNT_JOIN = (
    "FROM notebook_bases e "
    "JOIN notebooks b ON b.id = e.base_notebook_id "
    "JOIN notebooks a ON a.id = e.notebook_id "
    "WHERE e.notebook_id = ? AND b.id != e.notebook_id"
)

# 有效性谓词。作为布尔表达式单独取用(如 list_mount_edges 的 active 标记)。
MOUNT_VALID_EXPR = "(b.tier = 'base' OR b.created_by = a.created_by)"

# 追加到 MOUNT_JOIN 之后的有效性过滤。
MOUNT_VALID = " AND " + MOUNT_VALID_EXPR

# 统一次序:公共知识库在前,组内按名字。
MOUNT_ORDER = " ORDER BY b.tier DESC, b.name"

# 供 `IN (...)` 内联的 id 子查询(子查询里 ORDER BY 无意义,故不带)。
MOUNTED_BASE_IDS_SUBQUERY = "SELECT b.id " + MOUNT_JOIN + MOUNT_VALID
```

- [ ] **Step 3b: 实现解析与 CRUD**

`notebook_store.py` 顶部加 import：

```python
from app.repositories.sqlite.mount_sql import (
    MOUNT_JOIN, MOUNT_ORDER, MOUNT_VALID, MOUNT_VALID_EXPR,
)
```

把 `:54-86` 的三个 `participant_*` 整段替换为下面这段（`tier_map` 保持不动）。注意下方代码里的 `NotebookStore._MOUNT_JOIN` / `._MOUNT_VALID` 一律改用 import 进来的模块级常量 `MOUNT_JOIN` / `MOUNT_VALID`，**不要**在类上再定义一份：

```python
    # ---------------------------------------------------------------- 参考库挂载
    # 参与集 = [本库] + 本库「有效」挂载的库(notebook_bases)。基准库不再全局唯一,
    # 也不再隐式参与 —— 必须显式挂载。有效性的定义见 mount_sql 模块。

    @staticmethod
    def resolve_participants(
        db: sqlite3.Connection, active_notebook_id: str
    ) -> list[tuple[str, str]]:
        """[(notebook_id, tier)] —— 首项恒为 active 本身。唯一的参与集定义点。"""
        active = db.execute(
            "SELECT tier FROM notebooks WHERE id=?", (active_notebook_id,)
        ).fetchone()
        out = [(
            active_notebook_id,
            (active["tier"] if active is not None else "personal") or "personal",
        )]
        rows = db.execute(
            "SELECT b.id AS id, b.tier AS tier "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        out.extend((row["id"], row["tier"] or "personal") for row in rows)
        return out

    def participant_notebook_ids(self, active_notebook_id: str) -> list[str]:
        with self.database.connect() as db:
            return self.participant_ids(db, active_notebook_id)

    @staticmethod
    def participant_ids(db: sqlite3.Connection, active_notebook_id: str) -> list[str]:
        return [nb_id for nb_id, _ in NotebookStore.resolve_participants(db, active_notebook_id)]

    @staticmethod
    def participant_rows(db: sqlite3.Connection, active_notebook_id: str):
        """(active_row, base_rows) —— 形状与全局唯一 base 时代一致,消费方无需改动。"""
        base_rows = db.execute(
            "SELECT b.id AS id, b.tier AS tier "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (active_notebook_id,),
        ).fetchall()
        active_row = db.execute(
            "SELECT id, tier FROM notebooks WHERE id=?", (active_notebook_id,),
        ).fetchone()
        return active_row, base_rows

    @staticmethod
    def participant_tiers(db: sqlite3.Connection, active_notebook_id: str):
        pairs = NotebookStore.resolve_participants(db, active_notebook_id)
        return [nb_id for nb_id, _ in pairs], dict(pairs)

    @staticmethod
    def list_mount_edges(db: sqlite3.Connection, notebook_id: str) -> list[dict]:
        """全部挂载边(含失效的)。失效边保留展示 + 置灰,不能假装它还在工作。"""
        rows = db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            + MOUNT_VALID_EXPR + " AS ok "
            + MOUNT_JOIN + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        out = []
        for row in rows:
            active = bool(row["ok"])
            out.append({
                "id": row["id"],
                "name": row["name"],
                "tier": row["tier"] or "personal",
                "active": active,
                "inactive_reason": "" if active else "该库已不是公共知识库，且不属于你",
            })
        return out

    @staticmethod
    def mountable_notebooks(db: sqlite3.Connection, notebook_id: str) -> list[dict]:
        """可挂候选 = 所有公共知识库 ∪ 与本库同 owner 的库，排除本库自己。

        公共知识库对普通用户的常规列表是隐藏的,故此处专门放行 id/name/tier 三个
        字段——这是用户发现领域库的唯一入口。刻意不含只读分享(notebook_members)
        进来的库:对方撤销分享后边仍在会成为越权通道。"""
        rows = db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier "
            "FROM notebooks b JOIN notebooks a ON a.id = ? "
            "WHERE b.id != a.id AND (b.tier = 'base' OR b.created_by = a.created_by) "
            "ORDER BY b.tier DESC, b.name",
            (notebook_id,),
        ).fetchall()
        return [
            {"id": r["id"], "name": r["name"], "tier": r["tier"] or "personal"}
            for r in rows
        ]

    def replace_mounts(
        self, notebook_id: str, base_notebook_ids: Sequence[str], created_by: str
    ) -> None:
        """全量替换本库的挂载集合(幂等)。自挂与重复项在写入前剔除,与 CHECK/PK 双保险。"""
        wanted = [
            nb_id for nb_id in dict.fromkeys(base_notebook_ids)
            if nb_id and nb_id != notebook_id
        ]
        now = self.now()
        with self.database.write() as db:
            db.execute("DELETE FROM notebook_bases WHERE notebook_id=?", (notebook_id,))
            for base_id in wanted:
                db.execute(
                    "INSERT INTO notebook_bases"
                    "(notebook_id, base_notebook_id, created_at, created_by)"
                    " VALUES (?,?,?,?)",
                    (notebook_id, base_id, now, created_by),
                )
```

同时把类文档（`:18-21`）第一行改为：

```python
    """SQLite notebooks-table row persistence: CRUD, tier transitions, 参考库挂载边
    (notebook_bases) 与检索参与集解析, and row deletion (including the orphan
    knowledge-embedding cleanup that the schema's missing FK makes necessary).
    Row-level only — summary projection and orchestration live in
    app.services.notebook_catalog."""
```

- [ ] **Step 4: 加 facade 一跳委托**

`sqlite_repository.py`，在 `set_notebook_personal`（约 `:1128`）之后插入：

```python
    def list_notebook_bases(self, notebook_id: str) -> list[dict]:
        with self._connect() as db:
            return self._runtime.notebooks.list_mount_edges(db, notebook_id)

    def mountable_notebooks(self, notebook_id: str) -> list[dict]:
        with self._connect() as db:
            return self._runtime.notebooks.mountable_notebooks(db, notebook_id)

    def replace_notebook_bases(
        self, notebook_id: str, base_notebook_ids: list[str], created_by: str
    ) -> None:
        return self._runtime.notebooks.replace_mounts(
            notebook_id, base_notebook_ids, created_by
        )
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py -q
```

Expected: 全部 passed（16 个左右）。

- [ ] **Step 6: 跑联邦检索既有测试，观察预期的红**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py -q
```

Expected: **部分 FAIL** —— `test_federated_retrieve_*` 系列因为「不挂载就不参与」而不再返回 base 命中。这是**正确的行为变更**，Task 3 修测试。先记下失败清单。

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/notebook_store.py backend/app/services/sqlite_repository.py backend/tests/test_multi_domain_bases.py
git commit -m "feat(bases): 参与集改由挂载边解析，participant_* 形状不变"
```

---

## Task 3: 去掉 base 全局唯一性

**Files:**
- Modify: `backend/app/repositories/sqlite/notebook_store.py:170-191`（`set_tier`）
- Modify: `backend/tests/test_two_tier_federated.py:36-62`

**Interfaces:**
- Consumes: Task 2 的 `replace_mounts`
- Produces: `set_tier(id, "base")` 不再降级其它 base

- [ ] **Step 1: 反转唯一性测试**

`test_two_tier_federated.py:36-44`，把 `test_mark_notebook_base_is_globally_unique` 整个替换为：

```python
    def test_multiple_base_notebooks_coexist(self, repo):
        """多领域基准库:base 不再全局唯一,设 B 为 base 不应降级 A。

        领域各有独立公共知识库(模拟/物理设计/数字前端),谁参与检索由笔记本的
        挂载边决定,不再由「哪个是那个唯一的 base」决定。"""
        a = repo.create_notebook(NotebookCreate(name="模拟"))
        b = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(a.id)
        repo.mark_notebook_base(b.id)
        assert repo.get_notebook(a.id).tier == "base"
        assert repo.get_notebook(b.id).tier == "base"
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask1::test_multiple_base_notebooks_coexist -q
```

Expected: FAIL —— A 被降级成 `personal`。

- [ ] **Step 3: 删掉降级逻辑**

`notebook_store.py:170-191`，把 `set_tier` 整个替换为：

```python
    def set_tier(
        self, notebook_id: str, tier: Literal["base", "personal"]
    ) -> None:
        """tier='base': 发布为公共知识库(admin 动作)。**不再全局唯一** —— 每个领域
        可以有自己的公共知识库,谁参与某次检索由 notebook_bases 挂载边决定。
        tier='personal': 撤回发布。两者幂等。

        降级为 personal 时不清理指向它的挂载边:边保留但解析时跳过(见
        resolve_participants),重新发布即自动恢复。"""
        now = self.now()
        with self.database.write() as db:
            db.execute(
                "UPDATE notebooks SET tier=?, updated_at=? WHERE id=?",
                (tier, now, notebook_id),
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask1 -q
```

Expected: `test_multiple_base_notebooks_coexist` PASS；`test_base_notebook_name_visible_from_any_summary` 仍 FAIL（Task 5 处理）。

- [ ] **Step 5: 修联邦检索测试的挂载前置**

`test_two_tier_federated.py`，`TestTask3`/`TestTask4` 里凡是 `repo.mark_notebook_base(base_nb.id)` 之后期望 base 参与检索的用例，在其后补一行挂载。例如 `:82` 附近：

```python
        repo.mark_notebook_base(base_nb.id)
        repo.replace_notebook_bases(personal_nb.id, [base_nb.id], "user-local")
```

对 `:135` 的 `test_federated_retrieve_ranks_base_first_on_score_tie` 做同样处理。逐个跑到绿为止。

- [ ] **Step 6: 跑全套确认**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py -q
```

Expected: 除 `test_base_notebook_name_visible_from_any_summary` 外全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/notebook_store.py backend/tests/test_two_tier_federated.py
git commit -m "feat(bases): 去掉基准库全局唯一约束"
```

---

## Task 4: KG 可用性门与 follow_chain 起点门按挂载判定

**Files:**
- Modify: `backend/app/repositories/sqlite/knowledge_store.py:255-272`、`:465`
- Modify: `backend/app/services/sqlite_repository.py:1045-1047`（`_any_base_notebook_has_kg`）
- Modify: 全部 caller（用 grep 定位）
- Test: `backend/tests/test_multi_domain_bases.py`

**Interfaces:**
- Consumes: Task 2 的 `mount_sql` 共享片段（**import，不要手写谓词**）
- Produces: `KnowledgeStore.any_mounted_has_kg_on(db, notebook_id) -> bool`；facade `_any_base_notebook_has_kg(notebook_id, db=None) -> bool`（**签名新增必填 notebook_id**）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_multi_domain_bases.py`：

```python
class TestKgGate:
    def test_unmounted_base_with_kg_does_not_open_gate(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.create_knowledge_object(
            base.id, "concept", {"name": "Gain", "definition": "增益"}
        )
        assert repo.get_notebook(a.id).base_kg_available is False

    def test_mounted_base_with_kg_opens_gate(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)
        repo.create_knowledge_object(
            base.id, "concept", {"name": "Gain", "definition": "增益"}
        )
        repo.replace_notebook_bases(a.id, [base.id], "user-local")
        assert repo.get_notebook(a.id).base_kg_available is True
```

> **注意**：`create_knowledge_object` 的确切签名以仓库现状为准。实现前先跑
> `grep -n "def create_knowledge_object" backend/app/services/sqlite_repository.py`
> 确认参数，并按实际签名调整这两个测试。若该 API 需要 embedding，用 fixture 里已装好的 `FakeEmbedder`。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestKgGate -q
```

Expected: 第一个 FAIL（`True != False`，未挂载却开了门）。

- [ ] **Step 3: 改 KnowledgeStore**

`knowledge_store.py:255-272`，把三个 `any_base_has_kg*` 整段替换为：

```python
    @staticmethod
    def any_mounted_has_kg_on(db: sqlite3.Connection, notebook_id: str) -> bool:
        """本库挂载的参考库中是否有任一已建 KG —— 驱动前端严格推理门控。
        未挂载 → False(即便系统里存在有图的公共知识库)。"""
        return bool(db.execute(
            "SELECT EXISTS(SELECT 1 " + MOUNT_JOIN + MOUNT_VALID
            + " AND EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id))",
            (notebook_id,),
        ).fetchone()[0])

    def any_mounted_has_kg(self, notebook_id: str) -> bool:
        with self.database.connect() as db:
            return self.any_mounted_has_kg_on(db, notebook_id)

    def any_mounted_has_kg_compat(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> bool:
        return (
            self.any_mounted_has_kg_on(db, notebook_id) if db is not None
            else self.any_mounted_has_kg(notebook_id)
        )
```

- [ ] **Step 4: 改 facade 与全部 caller**

`sqlite_repository.py:1045-1047`：

```python
    def _any_base_notebook_has_kg(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> bool:
        """True iff 该 notebook 挂载的参考库里有任一已建 KG。"""
        return self._runtime.knowledge.any_mounted_has_kg_compat(notebook_id, db)
```

定位并逐个修正 caller：

```bash
grep -rn "_any_base_notebook_has_kg\|any_base_has_kg" backend/app backend/tests
```

已知四处消费点必须传入 active notebook：`retrieval_candidates.py:1471`、`retrieval_candidates.py:1494`、`ask_service.py:961`、`ask_service.py:1141`；包装层 `retrieval_candidates.py:180-183`、`retrieval_service.py:102-103` 同步加参数。每处传的都是**当前正在提问的 notebook id**（这些函数的上下文里都已有该变量）。

- [ ] **Step 5: 改 follow_chain 起点门**

`knowledge_store.py:465`，把 `AND (ko.notebook_id=? OR n.tier='base')` 改为按挂载：

```python
            "AND (ko.notebook_id=? OR ko.notebook_id IN ("
            + MOUNTED_BASE_IDS_SUBQUERY + "))"
```

对应的参数元组要多传一个 notebook_id。改完读一遍该函数确认占位符数量与参数数量一致。

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py -q
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/ -q -k "follow_chain or chain"
```

Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app/repositories/sqlite/knowledge_store.py backend/app/services/ backend/tests/test_multi_domain_bases.py
git commit -m "feat(bases): KG 可用性门与 follow_chain 起点门按挂载判定"
```

---

## Task 5: 消除单例查找 —— `base_notebooks` 契约

**Files:**
- Modify: `backend/app/repositories/sqlite/query_store.py:90-97`
- Modify: `backend/app/services/notebook_catalog.py:123-138`、`:166`
- Modify: `backend/app/repositories/sqlite/unified_kg_store.py:632-637`；`backend/app/services/communities.py:21-22`、`:117-118`
- Modify: `backend/app/models/schemas.py:394-399`
- Modify: `backend/tests/test_two_tier_federated.py:46-62`

**Interfaces:**
- Consumes: Task 2、Task 4
- Produces:
  - `NotebookRef` pydantic 模型 `{id, name, tier}`
  - `NotebookSummary.base_notebooks: List[NotebookRef]`（**取代** `base_notebook_name: str`）
  - `NotebookSummary.base_kg_available: bool`（语义改为按挂载）
  - `UnifiedKgStore.mounted_base_ids(active_nb) -> list[str]`（**取代** `first_base_notebook_id`）

- [ ] **Step 1: 改写 summary 测试**

`test_two_tier_federated.py:46-62`，把 `test_base_notebook_name_visible_from_any_summary` 整个替换为：

```python
    def test_base_notebooks_reflect_mounts(self, repo):
        """base_notebooks 是「本库挂了哪些参考库」,不再是全局那一个的名字。"""
        base = repo.create_notebook(NotebookCreate(name="模拟IC教材"))
        other = repo.create_notebook(NotebookCreate(name="my notes"))
        repo.mark_notebook_base(base.id)
        # 已发布但未挂载 → 看不到
        assert repo.get_notebook(other.id).base_notebooks == []
        repo.replace_notebook_bases(other.id, [base.id], "user-local")
        names = [b.name for b in repo.get_notebook(other.id).base_notebooks]
        assert names == ["模拟IC教材"]
        # 空库无 KG → 门仍关
        assert repo.get_notebook(other.id).base_kg_available is False
        # base 自己没挂任何东西 → 空
        assert repo.get_notebook(base.id).base_notebooks == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py::TestTask1::test_base_notebooks_reflect_mounts -q
```

Expected: FAIL —— `AttributeError: 'NotebookSummary' object has no attribute 'base_notebooks'`。

- [ ] **Step 3: 加 schema**

`schemas.py`，在 `NotebookSummary` 定义之前加：

```python
class NotebookRef(BaseModel):
    """轻量 notebook 引用 —— 参考库挂载相关接口共用。"""
    id: str
    name: str
    tier: str = "personal"


class MountedBase(NotebookRef):
    """一条挂载边。active=False 表示边还在但当前不生效(被挂库易主 / 公共库被降级),
    前端须置灰并说明,不能假装它还在工作。"""
    active: bool = True
    inactive_reason: str = ""
```

然后把 `schemas.py:394-399` 两个字段替换为：

```python
    # 本 notebook 挂载的参考库中是否有任一已建 KG。即便本 notebook 无图,挂了有图的
    # 参考库也可进行严格推理(reasoning/graph)。前端门控:requiresKg → (kg_ready 或
    # base_kg_available)。未挂载 → False。
    base_kg_available: bool = False
    # 本 notebook 挂载的参考库列表(0..N)。基准库不再全局唯一,也不再隐式参与检索。
    base_notebooks: List[NotebookRef] = Field(default_factory=list)
```

- [ ] **Step 4: 改 query_store**

`query_store.py:90-97`，把 `base_notebook_info_row` 替换为：

```python
    @staticmethod
    def mounted_bases_row(db: sqlite3.Connection, notebook_id: str):
        """本库挂载的有效参考库 + 各自是否有 KG —— 一次查询同时供 NotebookSummary 的
        base_notebooks 与 base_kg_available。"""
        return db.execute(
            "SELECT b.id AS id, b.name AS name, b.tier AS tier, "
            "EXISTS(SELECT 1 FROM knowledge_objects ko WHERE ko.notebook_id = b.id) AS has_kg "
            + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
```

- [ ] **Step 5: 改 notebook_catalog**

`notebook_catalog.py:123-138`，把 `base_notebook_info` 替换为：

```python
    def mounted_bases(
        self, notebook_id: str, db: "sqlite3.Connection | None" = None
    ) -> "tuple[list[NotebookRef], bool]":
        """(参考库列表, 是否任一有 KG) —— 一次查询同时供 NotebookSummary 的
        base_notebooks 与 base_kg_available,避免每条 summary 各查一次。
        未挂载 → ([], False)。"""
        if db is not None:
            rows = self.queries.mounted_bases_row(db, notebook_id)
        else:
            with self.database.connect() as conn:
                rows = self.queries.mounted_bases_row(conn, notebook_id)
        refs = [
            NotebookRef(id=r["id"], name=r["name"], tier=r["tier"] or "personal")
            for r in rows
        ]
        return (refs, any(bool(r["has_kg"]) for r in rows))
```

在该文件顶部的 schemas import 里加上 `NotebookRef`。

`notebook_catalog.py:166`：

```python
        base_refs, base_has_kg = self.mounted_bases(row["id"], connection)
```

并把下方 `NotebookSummary(...)` 构造里的 `base_notebook_name=base_name` 改为 `base_notebooks=base_refs`（`base_kg_available=base_has_kg` 不变）。

- [ ] **Step 6: 改 unified_kg_store 与 communities**

`unified_kg_store.py:632-637`：

```python
    def mounted_base_ids(self, active_nb: str) -> list[str]:
        """本库挂载的有效参考库 id —— 社区对比检索的扩展域。原
        first_base_notebook_id 的全局 LIMIT 1 在多领域下无意义。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT b.id AS id " + MOUNT_JOIN + MOUNT_VALID + MOUNT_ORDER,
                (active_nb,)).fetchall()
        return [row["id"] for row in rows]
```

然后：

```bash
grep -rn "first_base_notebook_id" backend/app backend/tests
```

`communities.py:21-22` 与 `:117-118` 的包装改名并返回 list；两个 caller（`ask_service.py:750`、`reasoning_retrieval.py:741`）原本拿单个 id 做一次扩展，改为**遍历列表**依次扩展。若原逻辑是 `if base_id: expand(base_id)`，改为 `for base_id in base_ids: expand(base_id)`。

- [ ] **Step 7: 清掉 `base_notebook_name` 的残留引用**

```bash
grep -rn "base_notebook_name\|base_notebook_info" backend/ frontend/
```

后端应清零（前端留到流程 B）。MCP 侧若有暴露（`mcp_server.py:669`/`:708`）一并改为 `base_notebooks`。

- [ ] **Step 8: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_two_tier_federated.py tests/test_multi_domain_bases.py -q
```

Expected: 全绿。

- [ ] **Step 9: 提交**

```bash
git add backend/app backend/tests
git commit -m "feat(bases): NotebookSummary.base_notebooks 取代全局唯一的 base_notebook_name"
```

---

## Task 6: scale 索引 eligible 扩展

没有这一步，「挂自己的大笔记本」会因为拿不到 scale 索引而在推理模式下走 `ppr_fallback_refused` 直接返回空 —— 静默失效，比报错难查得多。

**Files:**
- Modify: `backend/app/services/scale_artifact_runtime.py:140-158`
- Modify: `backend/app/services/notebook_scale.py:37` 附近（镜像实现）
- Test: `backend/tests/test_multi_domain_bases.py`

**Interfaces:**
- Consumes: Task 1 的表
- Produces: eligible 判定新增「被任何笔记本挂载」分支

- [ ] **Step 1: 写失败测试**

```python
class TestScaleEligible:
    def test_mounted_personal_notebook_becomes_index_eligible(self, repo):
        a = repo.create_notebook(NotebookCreate(name="项目"))
        mine = repo.create_notebook(NotebookCreate(name="我的大笔记"))
        runtime = repo.retrieval.scale
        assert runtime.scale_index_eligible(mine.id) is False
        repo.replace_notebook_bases(a.id, [mine.id], "user-local")
        assert runtime.scale_index_eligible(mine.id) is True
```

> 实现前先确认 eligible 函数的真实名字与访问路径：
> `grep -n "def .*eligible" backend/app/services/scale_artifact_runtime.py backend/app/services/notebook_scale.py`
> 并按实际路径调整 `repo.retrieval.scale` 这个取法。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestScaleEligible -q
```

Expected: FAIL —— 挂载后仍是 `False`。

- [ ] **Step 3: 实现**

`scale_artifact_runtime.py:152`，把 `if tier == "base" or exists:` 改为：

```python
        if tier == "base" or exists or self._is_mounted_by_anyone(notebook_id):
            return True
```

并在该类里加：

```python
    def _is_mounted_by_anyone(self, notebook_id: str) -> bool:
        """被任何笔记本当作参考库挂着 —— 本身即构成建索引资格。否则挂一个大的个人
        笔记本会因为没有 scale 索引而在 PPR 侧被大库守卫拒绝(返回空),静默失效。"""
        with self.database.connect() as db:
            return bool(db.execute(
                "SELECT EXISTS(SELECT 1 FROM notebook_bases WHERE base_notebook_id=?)",
                (notebook_id,),
            ).fetchone()[0])
```

> `self.database` 的实际属性名以该类现状为准；若该类没有直连 database，走它已有的 store/projections 依赖加一个同义方法。

`notebook_scale.py:37` 的镜像实现做**同样**的修改，保持两处一致（它们是刻意的镜像，不一致会导致建索引与用索引的判定分叉）。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestScaleEligible -q
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/ -q -k "scale"
```

Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/scale_artifact_runtime.py backend/app/services/notebook_scale.py backend/tests/test_multi_domain_bases.py
git commit -m "feat(bases): 被挂载即获得 scale 索引资格，避免挂大库时 PPR 静默返空"
```

---

## Task 7: 晋升目标显式化

**Files:**
- Modify: `backend/app/repositories/sqlite/governance_store.py:645-651`、`:699-702`、`:808` 附近
- Modify: `backend/app/services/knowledge_governance.py`（`propose_promotion` 入口）
- Test: `backend/tests/test_multi_domain_bases.py`

**Interfaces:**
- Consumes: Task 1 的 `target_base_id` 列、Task 2 的挂载解析
- Produces: `GovernanceStore.mounted_public_base_ids(db, notebook_id) -> list[str]`；晋升写侧读 `promotion_candidates.target_base_id`

- [ ] **Step 1: 写失败测试**

```python
class TestPromotionTarget:
    def test_promote_without_mounted_public_base_is_rejected(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        obj = repo.create_knowledge_object(
            a.id, "concept", {"name": "Gain", "definition": "增益"}
        )
        with pytest.raises(ValueError, match="参考库"):
            repo.propose_promotion(obj["id"], target_base_id="")

    def test_promote_records_explicit_target(self, repo):
        a = repo.create_notebook(NotebookCreate(name="a"))
        b1 = repo.create_notebook(NotebookCreate(name="模拟"))
        b2 = repo.create_notebook(NotebookCreate(name="物理设计"))
        repo.mark_notebook_base(b1.id)
        repo.mark_notebook_base(b2.id)
        repo.replace_notebook_bases(a.id, [b1.id, b2.id], "user-local")
        obj = repo.create_knowledge_object(
            a.id, "concept", {"name": "Gain", "definition": "增益"}
        )
        cand = repo.propose_promotion(obj["id"], target_base_id=b2.id)
        with repo._connect() as db:
            row = db.execute(
                "SELECT target_base_id FROM promotion_candidates WHERE id=?",
                (cand["id"],),
            ).fetchone()
        assert row["target_base_id"] == b2.id
```

> `propose_promotion` / `create_knowledge_object` 的真实签名以仓库现状为准：
> `grep -n "def propose_promotion" backend/app/services/knowledge_governance.py backend/app/services/sqlite_repository.py`
> 按实际签名调整测试与实现（新增的 `target_base_id` 作为**关键字参数**）。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestPromotionTarget -q
```

Expected: FAIL —— `propose_promotion() got an unexpected keyword argument 'target_base_id'`。

- [ ] **Step 3: 实现取候选与校验**

`governance_store.py:645-651`，把 `first_base_notebook_row` 替换为：

```python
    @staticmethod
    def mounted_public_base_ids(
        connection: sqlite3.Connection, notebook_id: str
    ) -> list[str]:
        """本库挂载的**公共知识库** id —— 晋升只能进公共知识库,不能进别人的个人库。
        原 first_base_notebook_row 的全局 LIMIT 1 在多领域下语义已不成立。"""
        rows = connection.execute(
            "SELECT b.id AS id " + MOUNT_JOIN + " AND b.tier = 'base'" + MOUNT_ORDER,
            (notebook_id,),
        ).fetchall()
        return [row["id"] for row in rows]
```

在 `propose_promotion` 的服务层入口加校验（`knowledge_governance.py`）：

```python
        allowed = self.stores.governance.mounted_public_base_ids(db, notebook_id)
        if not allowed:
            raise ValueError("该笔记本尚未挂载任何公共知识库，无法提交晋升")
        target = (target_base_id or "").strip()
        if not target:
            if len(allowed) > 1:
                raise ValueError("挂载了多个公共知识库，请指定晋升目标")
            target = allowed[0]
        if target not in allowed:
            raise ValueError("晋升目标必须是本笔记本已挂载的公共知识库")
```

并把 `target` 写进 `promotion_candidates.target_base_id`。

- [ ] **Step 4: 改审批侧读目标**

`governance_store.py:699-702`，把：

```python
        base_row = self.first_base_notebook_row(connection)
        if base_row is None:
            raise ValueError("no base notebook — mark one with mark_notebook_base() first")
        base_nb_id = str(base_row["id"])
```

替换为：

```python
        base_nb_id = str(cand["target_base_id"] or "")
        if not base_nb_id:
            raise ValueError("晋升候选缺少目标公共知识库(target_base_id)")
```

`:808` 附近的另一处 `first_base_notebook_row` 调用做同样替换。改完确认：

```bash
grep -rn "first_base_notebook_row" backend/
```

应清零。

- [ ] **Step 5: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestPromotionTarget -q
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/ -q -k "promotion or governance"
```

Expected: 全绿。既有晋升测试若依赖「全局唯一 base 自动成为目标」，补上挂载与显式 target。

- [ ] **Step 6: 提交**

```bash
git add backend/app backend/tests
git commit -m "feat(bases): 晋升目标显式化为 target_base_id"
```

---

## Task 8: API 端点

**Files:**
- Modify: `backend/app/api/routes.py:1341-1359` 附近
- Modify: `backend/app/models/schemas.py`（`SetBasesRequest`）
- Test: `backend/tests/test_multi_domain_bases.py`

**Interfaces:**
- Consumes: Task 2 的 facade 三方法、Task 5 的 `MountedBase`/`NotebookRef`
- Produces:
  - `GET /api/notebooks/{id}/bases -> List[MountedBase]`
  - `PUT /api/notebooks/{id}/bases -> List[MountedBase]`（全量替换）
  - `GET /api/notebooks/{id}/mountable -> List[NotebookRef]`

- [ ] **Step 1: 写失败测试**

```python
from fastapi.testclient import TestClient


class TestApi:
    def test_bases_roundtrip(self, repo, monkeypatch):
        from app.api.app_factory import create_app  # 路径以仓库现状为准

        monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
        client = TestClient(create_app())
        a = repo.create_notebook(NotebookCreate(name="a"))
        base = repo.create_notebook(NotebookCreate(name="base"))
        repo.mark_notebook_base(base.id)

        got = client.get(f"/api/notebooks/{a.id}/mountable").json()
        assert base.id in [n["id"] for n in got]
        assert a.id not in [n["id"] for n in got]

        put = client.put(
            f"/api/notebooks/{a.id}/bases", json={"base_notebook_ids": [base.id]}
        )
        assert put.status_code == 200
        assert [b["id"] for b in put.json()] == [base.id]
        assert client.get(f"/api/notebooks/{a.id}/bases").json()[0]["active"] is True

        client.put(f"/api/notebooks/{a.id}/bases", json={"base_notebook_ids": []})
        assert client.get(f"/api/notebooks/{a.id}/bases").json() == []
```

> `create_app` 的 import 路径与 `repo` fixture 和 app 共用同一个 DB 的接法以仓库现状为准。
> 先看一个既有的 API 测试（`grep -rln "TestClient" backend/tests | head -3`）照抄它的搭法。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py::TestApi -q
```

Expected: FAIL —— 404。

- [ ] **Step 3: 加请求模型**

`schemas.py`，在 `SetTierRequest` 旁：

```python
class SetBasesRequest(BaseModel):
    """全量替换本 notebook 的参考库挂载集合。空数组 = 取消全部挂载。"""
    base_notebook_ids: List[str] = Field(default_factory=list)
```

- [ ] **Step 4: 加端点**

`routes.py`，在 `set_notebook_tier`（`:1341-1359`）之后插入：

```python
@router.get("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_access)])
def list_notebook_bases_route(notebook_id: str) -> List[MountedBase]:
    """本 notebook 挂载的参考库。含 active=False 的失效边(被挂库易主 / 公共库被
    降级),前端置灰展示——边保留是为了对方恢复后自动生效。"""
    return [MountedBase(**edge) for edge in notebook_catalog_repository().list_notebook_bases(notebook_id)]


@router.put("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_access)])
def set_notebook_bases_route(
    notebook_id: str, payload: SetBasesRequest,
    user: UserProfile = Depends(get_current_user),
) -> List[MountedBase]:
    """全量替换挂载集合。只接受本 notebook 的可挂候选(公共知识库 ∪ 同 owner 的库),
    其余一律 400 —— 挂载边不是授权凭证,写入侧也要挡。"""
    catalog = notebook_catalog_repository()
    if not catalog.can_write_notebook(notebook_id):
        raise HTTPException(status_code=403, detail="仅笔记本所有者可设置参考库")
    allowed = {n["id"] for n in catalog.mountable_notebooks(notebook_id)}
    wanted = [nb_id for nb_id in dict.fromkeys(payload.base_notebook_ids) if nb_id]
    invalid = [nb_id for nb_id in wanted if nb_id not in allowed]
    if invalid:
        raise HTTPException(status_code=400, detail="包含不可挂载的知识库")
    catalog.replace_notebook_bases(notebook_id, wanted, user.id)
    return [MountedBase(**edge) for edge in catalog.list_notebook_bases(notebook_id)]


@router.get("/notebooks/{notebook_id}/mountable", response_model=List[NotebookRef],
            dependencies=[Depends(require_notebook_access)])
def mountable_notebooks_route(notebook_id: str) -> List[NotebookRef]:
    """可挂候选 = 所有公共知识库 ∪ 与本库同 owner 的库。

    刻意挂在 {notebook_id} 下而非 /notebooks/mountable —— 后者会与既有的
    /notebooks/{notebook_id} 争路由匹配(FastAPI 按声明序,静态段必须先注册)。"""
    return [NotebookRef(**n) for n in notebook_catalog_repository().mountable_notebooks(notebook_id)]
```

在文件顶部 import 里补 `MountedBase, NotebookRef, SetBasesRequest`。

> `can_write_notebook` 的真实名字以仓库现状为准：
> `grep -rn "def can_write_notebook\|require_notebook_write\|isReader" backend/app | head`
> 若已有写权限依赖（如 `require_notebook_write`），改用 `dependencies=[Depends(require_notebook_write)]` 并删掉函数体里的手工判断。

- [ ] **Step 4b: 补上晋升目标的 API 通路（计划补记，2026-07-19）**

Task 7 落地后暴露一个计划漏排：spec §6 要求「挂 >1 个公共知识库时，提交晋升由用户选目标」，
但服务层的 `target_base_id` 参数**没有任何 HTTP 通路**。现状是挂 >1 个时端点稳定 400，用户无从解决。
Memory 侧更重——整条栈（`memory_routes.py:334` → `MemoryService` → facade → service）没有一层接受目标参数。

三处一起补：

1. **知识对象晋升端点**：请求体加可选 `target_base_id: str = ""`，透传到服务层（服务层参数已存在，只差路由）。
2. **Memory 晋升端点**：`memory_routes.py` 同样加可选 `target_base_id`，并把它一路透传到
   `propose_memory_promotion`（`MemoryService` 与 facade 各加一个关键字参数）。
3. **晋升队列的读接口暴露目标**：`knowledge_governance.py:65-80` 的 `promotion_row_to_dict`
   字段表补 `target_base_id`。多领域启用后，「这个候选要进哪个库」正是策展人最需要区分的信息，
   队列里看不到它就没法审。

三处都要有测试。第 1、2 项的测试形状：挂 2 个公共库 → 不传 target 得 400、传合法 target 得 200
且候选行的 `target_base_id` 正确、传不在挂载集合里的 target 得 400。

- [ ] **Step 5: 改 tier 端点文案**

`routes.py:1343-1347`：

```python
    """Set a notebook's federation tier: 'base'(发布为公共知识库,可被任何笔记本
    挂载为参考库) 或 'personal'(撤回发布)。**不再全局唯一** —— 每个领域可以有自己
    的公共知识库。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可发布公共知识库")
```

- [ ] **Step 6: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_multi_domain_bases.py -q
```

Expected: 全绿。

- [ ] **Step 7: 提交**

```bash
git add backend/app backend/tests
git commit -m "feat(api): 参考库挂载三端点"
```

---

## Task 9: merge_dbs 边界报错 + 文档同步

**Files:**
- Modify: `scripts/merge_dbs.py`
- Modify: `README.md:24`、`:263`、`:642-645`、`:934` 段落；`README_zh.md:24`、`:239`；`AGENTS.md:159`

**Interfaces:**
- Consumes: 无
- Produces: 任一侧多于一个 `tier='base'` 时明确报错退出

- [ ] **Step 1: 加边界报错**

`scripts/merge_dbs.py`，在读取两侧 base 统计的地方（`--keep-base` 相关逻辑处）加：

```python
def _sole_base_id(conn, side: str) -> str:
    rows = conn.execute("SELECT id, name FROM notebooks WHERE tier='base'").fetchall()
    if len(rows) > 1:
        names = "、".join(str(r[1]) for r in rows)
        raise SystemExit(
            f"{side} 侧存在 {len(rows)} 个公共知识库（{names}）。本工具只支持"
            f"「两边共享恰好一个公共知识库」的场景，多领域库请勿使用 --keep-base 猜测。"
        )
    if not rows:
        raise SystemExit(f"{side} 侧没有公共知识库")
    return str(rows[0][0])
```

在两侧各调用一次，取代原先隐式取第一个的写法。

- [ ] **Step 2: 改文档**

四处叙述：

1. `README.md:24` —「is the only user who can mark a notebook as the base KG」改为「is the only user who can publish a notebook as a public knowledge base」；「Base notebooks are hidden from regular users' lists」后补「but are discoverable through each notebook's reference-library picker」；删掉「still used as authoritative retrieval context at ask time」的**隐式**含义，改为「participate in retrieval only for notebooks that explicitly mount them」。
2. `README_zh.md:24` — 同义中文改写：「唯一可将 notebook 发布为公共知识库的用户」；「公共知识库对普通用户的列表隐藏，但可在每个笔记本的参考库选择器里发现」；「仅对显式挂载了它们的笔记本参与检索」。
3. `README.md:263` / `README_zh.md:239` — 分析菜单三动作里的「mark-base / mark-personal tier toggle」改为「publish / unpublish public knowledge base」、「基准库/个人层切换」改为「发布/撤回公共知识库」。
4. `README.md:642-645` — tier row 与 `mark_notebook_base()` 段落，删掉唯一性叙述，改为说明挂载集合。
5. `AGENTS.md:159` —「the active notebook plus every participating base notebook」改为「the active notebook plus every mounted reference library」。
6. `README.md:934` 的 `merge_dbs` 段落补一句：多于一个公共知识库时工具会直接报错退出。

- [ ] **Step 2b: 存量待批晋升候选的补救通路（计划补记，2026-07-19）**

Task 7/8 落地后暴露的运维缺口：`_migration_20` 给 `promotion_candidates.target_base_id` 的默认值是
空串且**不回填**。于是 Task 7 之前创建、仍处 `proposed`/`under_review` 的候选行，批准时会命中
「晋升候选缺少目标公共知识库」的守卫直接失败 —— 而 `target_base_id` **只在 propose 时可设**，
没有任何接口能给存量候选补目标。操作员目前只能「拒绝 + 重新提交」。

spec §7 的「不回填」只讨论过挂载边，没有讨论这个场景。补两件事：

1. **一个清点/补救的 CLI**（放在 `scripts/` 下，或作为既有诊断脚本的子命令）：列出
   `status IN ('proposed','under_review') AND target_base_id=''` 的候选行（含它们各自 notebook 的
   挂载情况），并支持给它们批量指定目标——只允许指定该候选所属 notebook **已挂载的公共知识库**，
   与 propose 侧同一条规则。按本仓库约定，新 CLI 要在 `README.md` 与 `README_zh.md` 都写用法。
2. **README 的升级说明**里写清这条：升级到 SCHEMA 20 后，存量待批候选需要先跑这个命令处理，
   否则批准会失败。

如果实际部署库里没有这类行（可以在报告里说明如何核查），CLI 仍然要交付——它是升级路径的一部分，
不能依赖「碰巧没有」。

- [ ] **Step 3: 跑文档守卫**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_architecture_documentation.py -q
```

Expected: PASS。若失败，读失败信息里给出的 pinned 短语，把 `test_architecture_documentation.py` 里对应的期望短语同步更新为新措辞（该守卫是按精确短语匹配的，因改文案而失配是**正确行为**，改期望值而不是改成模糊匹配）。

- [ ] **Step 4: 提交**

```bash
git add scripts/merge_dbs.py README.md README_zh.md AGENTS.md backend/tests/test_architecture_documentation.py
git commit -m "docs+tool: 多领域基准库的文档口径与 merge_dbs 边界报错"
```

---

## Task 10: 后端守卫重基线与全量回归

改动触及大量 SQL 站点与 facade 成员，`test_repository_surface_manifest.py` 的行号 pin 必然失配。这一步是流程 A 的收口。

**Files:**
- Modify: `backend/tests/test_repository_surface_manifest.py`（重基线产物）
- Modify: `backend/tests/test_architecture_hardening.py:96-108`

**Interfaces:**
- Consumes: Task 1-9
- Produces: 后端全绿

- [ ] **Step 1: 修 hardening 里的联邦大库守卫测试**

`test_architecture_hardening.py:96-108`，在 `repo.mark_notebook_base(base.id)` 之后补一行挂载：

```python
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(personal.id, [base.id], "user-local")
```

- [ ] **Step 2: 跑全量后端测试，收集失败清单**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q 2>&1 | tail -40
```

- [ ] **Step 3: 重基线 surface manifest**

按仓库既有流程重生成契约夹具（**只在需要时**、且只针对活契约）：

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/generate_repository_contract_fixtures.py
```

不要带 `--rebaseline`（那是冻结产物专用，会动 `baseline.db` / `facade_surface`，`SOURCE_COMMIT` 是 provenance 不该 bump）。

若 `test_repository_surface_manifest.py` 仍报行号失配，按它的失败信息更新 `EXPECTED_PATCH_DELTAS`；被改动的文件若本就属于行号不敏感集合，确认它在 `LINE_NUMBER_INSENSITIVE_FILES` 里。

- [ ] **Step 4: 跑到全绿**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
```

Expected: 全绿，0 failed。

- [ ] **Step 5: 提交**

```bash
git add backend/tests
git commit -m "test: 多领域基准库的守卫重基线与回归修正"
```

---

# 流程 B：前端

## Task 11: `notebook-bases.ts` 客户端与纯逻辑

**Files:**
- Create: `frontend/app/notebook-bases.ts`
- Create: `frontend/app/notebook-bases.test.mjs`

**Interfaces:**
- Consumes: Task 8 的三端点
- Produces:
  - `type NotebookRef = { id: string; name: string; tier: string }`
  - `type MountedBase = NotebookRef & { active: boolean; inactive_reason: string }`
  - `listBases(id)` / `setBases(id, ids)` / `listMountable(id)`
  - `groupMountable(list)` → `{ public: NotebookRef[]; mine: NotebookRef[] }`
  - `mountCostHint(count)` → `string`（>3 时非空）

- [ ] **Step 1: 写失败测试**

新建 `frontend/app/notebook-bases.test.mjs`：

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { groupMountable, mountCostHint, MOUNT_HINT_THRESHOLD } from "./notebook-bases.ts";

test("groupMountable 按 tier 分成公共/我的两组", () => {
  const got = groupMountable([
    { id: "1", name: "模拟", tier: "base" },
    { id: "2", name: "我的笔记", tier: "personal" },
    { id: "3", name: "物理设计", tier: "base" },
  ]);
  assert.deepEqual(got.public.map((n) => n.id), ["1", "3"]);
  assert.deepEqual(got.mine.map((n) => n.id), ["2"]);
});

test("挂载数不超过阈值时无成本提示", () => {
  assert.equal(mountCostHint(MOUNT_HINT_THRESHOLD), "");
});

test("超过阈值给出成本提示且提到检索", () => {
  const hint = mountCostHint(MOUNT_HINT_THRESHOLD + 1);
  assert.ok(hint.length > 0);
  assert.match(hint, /检索/);
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && node --test app/notebook-bases.test.mjs
```

Expected: FAIL —— 模块不存在。

- [ ] **Step 3: 实现**

新建 `frontend/app/notebook-bases.ts`：

```typescript
// 多领域基准库 —— 参考库挂载客户端(纯逻辑部分在 notebook-bases.test.mjs 里单测)。
// 自带 fetch 封装,与 notebook-tier.ts 同款,以便在 `node --test` 下免 React。

import { authHeaders } from "./auth.ts";

export type NotebookRef = { id: string; name: string; tier: string };
export type MountedBase = NotebookRef & { active: boolean; inactive_reason: string };

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
  return res.json() as Promise<T>;
}

export const listBases = (notebookId: string): Promise<MountedBase[]> =>
  apiFetch(`/notebooks/${notebookId}/bases`);

export const listMountable = (notebookId: string): Promise<NotebookRef[]> =>
  apiFetch(`/notebooks/${notebookId}/mountable`);

export const setBases = (
  notebookId: string,
  baseNotebookIds: string[]
): Promise<MountedBase[]> =>
  apiFetch(`/notebooks/${notebookId}/bases`, {
    method: "PUT",
    body: JSON.stringify({ base_notebook_ids: baseNotebookIds }),
  });

// 检索开销线性于挂载数(跨层桥是 |active nodes| × topk per participant)。不硬性
// 拦截,只在超过这个数时提示 —— 用户可能确有同时挂多个领域的正当需求。
export const MOUNT_HINT_THRESHOLD = 3;

export const mountCostHint = (count: number): string =>
  count > MOUNT_HINT_THRESHOLD
    ? `已挂 ${count} 个参考库，检索会逐个搜索它们，响应可能变慢。`
    : "";

export const groupMountable = (
  list: readonly NotebookRef[]
): { public: NotebookRef[]; mine: NotebookRef[] } => ({
  public: list.filter((n) => n.tier === "base"),
  mine: list.filter((n) => n.tier !== "base"),
});
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd frontend && node --test app/notebook-bases.test.mjs
```

Expected: 3 pass。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/notebook-bases.ts frontend/app/notebook-bases.test.mjs
git commit -m "feat(frontend): 参考库客户端与分组/成本提示纯逻辑"
```

---

## Task 12: 三态改二态 + 唯一性文案清理

**Files:**
- Modify: `frontend/app/notebook-tier.ts:30-54`
- Modify: `frontend/app/notebook-tier.test.mjs`（既有）
- Modify: `frontend/app/page.tsx:3033`、`:3043-3044`、`:3596-3603`

**Interfaces:**
- Consumes: Task 3（后端已去唯一性）
- Produces: `TierAction = "set" | "unset"`（**删掉 `replace`**）

- [ ] **Step 1: 改测试**

`frontend/app/notebook-tier.test.mjs`，删掉所有断言 `replace` 的用例，加：

```javascript
test("公共知识库不再唯一：别处已有 base 时仍是 set 而非 replace", () => {
  const got = tierActionState({ id: "a", name: "A", tier: "personal" });
  assert.equal(got.action, "set");
  assert.equal(got.label, "设为公共知识库");
});

test("当前已是公共知识库 → unset", () => {
  const got = tierActionState({ id: "a", name: "A", tier: "base" });
  assert.equal(got.action, "unset");
  assert.equal(got.label, "取消公共知识库");
});
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd frontend && node --test app/notebook-tier.test.mjs
```

Expected: FAIL —— 得到 `replace` / 「替换为基准库」。

- [ ] **Step 3: 实现二态**

`notebook-tier.ts:30-54` 整段替换为：

```typescript
export type TierAction = "set" | "unset";

export type TierActionState = {
  action: TierAction;
  label: string;
};

// 二态发布按钮 —— 公共知识库不再全局唯一(多领域各有自己的),故没有「替换」这一态:
// - 当前 notebook 已是 base → unset(撤回发布)
// - 否则 → set(发布)
export const tierActionState = (
  current: NotebookSummaryLike | undefined
): TierActionState =>
  current?.tier === "base"
    ? { action: "unset", label: "取消公共知识库" }
    : { action: "set", label: "设为公共知识库" };
```

第二个参数（原来的全库列表）**删掉** —— 判定不再需要它。唯一的 caller 在 `page.tsx:3593`
（`tierActionState(currentNotebook, notebooks)`），同步改成 `tierActionState(currentNotebook)`。
测试里的调用同样去掉第二个实参。

- [ ] **Step 4: 清 page.tsx 的唯一性文案**

- `page.tsx:3033` —— 删掉整个 `replace` 分支的 `window.confirm`（「基准库全局唯一 —— 替换为…？」）。`handleTierAction` 里只保留 set / unset 两条路径。
- `page.tsx:3043-3044` —— toast 改为：
  ```
  "已发布为公共知识库 — 其他笔记本可在设置里把它挂为参考库"
  "已撤回公共知识库发布"
  ```
- `page.tsx:3594-3600` —— 「当前基准库」只读段落改为读 `currentNotebook?.base_notebooks`，标题改「本笔记本的参考库」，无挂载时不显示该段。
- `page.tsx:3603` —— desc 改为「把当前知识库发布为公共知识库，供其他笔记本挂载为参考库（管理员）」。

- [ ] **Step 5: 跑测试并检查弯引号**

```bash
cd frontend && node --test app/notebook-tier.test.mjs
git diff | grep -c '^-.*[“”]'
```

Expected: 测试全绿；`grep -c` 输出 `0`。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/notebook-tier.ts frontend/app/notebook-tier.test.mjs frontend/app/page.tsx
git commit -m "feat(frontend): 发布按钮三态改二态，清理全局唯一文案"
```

---

## Task 13: 编辑表单参考库多选

**Files:**
- Modify: `frontend/app/page.tsx:2222-2230`（`handleEditNotebook`）、`:4512-4530`（编辑表单）
- Modify: `frontend/app/globals.css`（选择器样式）

**Interfaces:**
- Consumes: Task 11 的 `listMountable` / `listBases` / `setBases` / `groupMountable` / `mountCostHint`
- Produces: 编辑弹窗里的「参考库」多选

- [ ] **Step 1: 加状态与加载**

`page.tsx`，在 `editingNotebook` state（`:831`）旁加：

```typescript
  const [mountable, setMountable] = useState<NotebookRef[]>([]);
  const [mountedIds, setMountedIds] = useState<string[]>([]);
  const [mountEdges, setMountEdges] = useState<MountedBase[]>([]);
```

打开编辑弹窗时（`setEditingNotebook(currentNotebook)` 之处，`:3645`）改为一个异步函数，先拉两个列表再开弹窗：

```typescript
  const openNotebookEditor = async (nb: NotebookSummary) => {
    const [cands, edges] = await Promise.all([listMountable(nb.id), listBases(nb.id)]);
    setMountable(cands);
    setMountEdges(edges);
    setMountedIds(edges.map((e) => e.id));
    setEditingNotebook(nb);
  };
```

`:3645` 的 action 改为 `() => openNotebookEditor(currentNotebook).catch(reportError)`。

- [ ] **Step 2: 渲染多选**

`page.tsx:4525`（「领域」那一行）之后插入。同时把该行 label 从「领域」改为「领域关键词」消歧（它只是 prompt 提示词，与参考库无关）：

```tsx
              <label>领域关键词<input name="primary_domain" defaultValue={editingNotebook.primary_domain} maxLength={80} /></label>
              <div className="base-picker">
                <span className="base-picker-title">参考库</span>
                <p className="base-picker-desc">检索时会一并搜索这些知识库。不选则只搜本笔记本。</p>
                {(() => {
                  const groups = groupMountable(mountable);
                  const render = (title: string, list: NotebookRef[]) =>
                    list.length === 0 ? null : (
                      <div className="base-picker-group" key={title}>
                        <span className="base-picker-group-title">{title}</span>
                        {list.map((n) => {
                          const edge = mountEdges.find((e) => e.id === n.id);
                          const dead = edge ? !edge.active : false;
                          return (
                            <label className={`base-picker-row${dead ? " base-picker-row-dead" : ""}`} key={n.id}>
                              <input
                                type="checkbox"
                                checked={mountedIds.includes(n.id)}
                                onChange={(e) =>
                                  setMountedIds((prev) =>
                                    e.target.checked
                                      ? [...prev, n.id]
                                      : prev.filter((id) => id !== n.id)
                                  )
                                }
                              />
                              <span className="base-picker-name" title={n.name}>{n.name}</span>
                              {dead && <span className="base-picker-dead-note">{edge?.inactive_reason}</span>}
                            </label>
                          );
                        })}
                      </div>
                    );
                  return (
                    <>
                      {render("公共知识库", groups.public)}
                      {render("我的笔记本", groups.mine)}
                      {groups.public.length === 0 && groups.mine.length === 0 && (
                        <p className="base-picker-empty">暂无可挂载的知识库。</p>
                      )}
                    </>
                  );
                })()}
                {mountCostHint(mountedIds.length) && (
                  <p className="base-picker-hint">{mountCostHint(mountedIds.length)}</p>
                )}
              </div>
```

- [ ] **Step 3: 提交时一并保存**

`handleEditNotebook`（`:2222-2230`），在 `api<NotebookSummary>(...)` 之后加：

```typescript
    await setBases(editingNotebook.id, mountedIds);
```

并把刷新后的 notebook 重新拉一次，让 `base_notebooks` / `base_kg_available` 立即生效。

- [ ] **Step 3b: 晋升目标选择器（计划补记，2026-07-19）**

承 Task 8 Step 4b 的后端通路。晋升队列（`page.tsx:5312` 一带）与知识条目的「↑ 提交晋升」按钮
（`page.tsx:6072-6079`）需要能指定目标公共知识库：

- 提交晋升时，若本笔记本挂载了 **>1 个**公共知识库 → 弹出选择器要求选一个，把选中的 id 作为
  `target_base_id` 传给端点；挂 **1 个** → 直接用它，不打扰用户；挂 **0 个** → 按钮禁用并提示
  「需先挂载一个公共知识库」。
- 晋升队列的每个候选行显示它的目标库名（后端 `promotion_row_to_dict` 已暴露 `target_base_id`）。
  多领域下策展人必须能看出「这条要进哪个库」才能审。

- [ ] **Step 4: 加样式**

`globals.css` 末尾追加（与 `.tier-badge` 群落同风格；列对齐 + 省略号截断，承 UI 精致度约束）：

```css
.base-picker { display: flex; flex-direction: column; gap: 6px; }
.base-picker-title { font-weight: 600; }
.base-picker-desc,
.base-picker-empty,
.base-picker-hint { font-size: 12px; opacity: .7; margin: 0; }
.base-picker-hint { color: var(--warning, #b8860b); }
.base-picker-group { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }
.base-picker-group-title { font-size: 12px; opacity: .6; }
.base-picker-row { display: grid; grid-template-columns: 16px 1fr auto; align-items: center; gap: 8px; }
.base-picker-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.base-picker-row-dead { opacity: .45; }
.base-picker-dead-note { font-size: 11px; opacity: .8; }
```

- [ ] **Step 5: 构建验证**

```bash
cd frontend && rm -rf .next && npm run build
```

Expected: 构建成功。（`rm -rf .next` 是必要的——本仓库的「前端功能凭空消失」多次被证实是 stale 缓存。）

- [ ] **Step 6: 检查弯引号并提交**

```bash
git diff | grep -c '^-.*[“”]'
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(frontend): 编辑表单里的参考库多选"
```

---

## Task 14: 徽章带库名 + §7 引导文案

**Files:**
- Modify: `frontend/app/answer-panel.tsx:138-145`、`:390-392`
- Modify: `frontend/app/report-view.tsx:885-887`
- Modify: `frontend/app/page.tsx:1780`、`:2433-2435`、`:3749-3759`、`:4156-4157`

**Interfaces:**
- Consumes: Task 5 的 `base_notebooks`
- Produces: 引用徽章带来源库名；深入分析拦截文案指向参考库设置

- [ ] **Step 1: 徽章带库名**

`answer-panel.tsx:138-145`。引用已带 `notebook_id`，需要一个 id→name 映射（从 `notebooks` 列表 + `base_notebooks` 合并得到，作为 prop 传入）：

```tsx
        {tier && (
          <span
            className={`tier-badge tier-${tier}`}
            title={
              sourceName
                ? `来自「${sourceName}」（${tier === "base" ? "公共知识库" : "个人知识库"}）`
                : (tier === "base" ? "来自公共知识库" : "来自个人知识库")
            }
          >
            {label(TIER, tier, "未知来源")}
          </span>
        )}
```

`sourceName` 由 `reference.citation?.notebook_id` / `reference.anchor?.notebook_id` 查映射得到，查不到就退回原文案（不要显示 id）。

- [ ] **Step 2: 分布徽章文案**

`answer-panel.tsx:390-392` 与 `report-view.tsx:885-887`，把「来源 · 个人 N · 基准库 M」改为「来源 · 个人 N · 公共 M」，`title` 改为「本次引用的来源分布（个人知识库 / 公共知识库）」。**维持两档聚合，不逐库拆分**——挂 3 个库时拆成 4 段会把徽章撑爆，逐库粒度由单条引用的徽章承载。

- [ ] **Step 3: 深入分析门与引导文案**

`page.tsx:1780` 保持逻辑不变（`kg_ready || base_kg_available`），但 `base_kg_available` 的语义已变成「本库挂载的参考库里有图」。

`page.tsx:2433-2435` 拦截 toast 改为：

```typescript
      setStatusText(`${strictLabel}需要知识图谱 — 可在「设置 → 编辑当前 notebook」里挂一个参考库，或为本库构建图谱`);
```

`page.tsx:4156-4157` 输入框旁提示改为按实际挂载渲染：

```tsx
        {!currentNotebook?.kg_ready && (currentNotebook?.base_notebooks?.length ?? 0) > 0 && (
          <span className="chat-hint">本笔记本无图，将使用参考库「{currentNotebook.base_notebooks.map((b) => b.name).join("、")}」推理</span>
        )}
```

`page.tsx:3749-3759` 构建按钮的 title / hint 里「借用底层库（base）」改为「借用已挂载的参考库」；未挂载时改为「本库尚未建图，也未挂参考库」。

- [ ] **Step 4: 构建验证**

```bash
cd frontend && rm -rf .next && npm run build
cd frontend && node --test app/*.test.mjs
```

Expected: 构建成功；前端单测全绿。

> 注意：`node --test app/*.test.mjs` **只匹配顶层**，嵌套目录里的 `.test.mjs` 不会跑。若有嵌套测试，另行指定路径。

- [ ] **Step 5: 检查弯引号并提交**

```bash
git diff | grep -c '^-.*[“”]'
git add frontend/app
git commit -m "feat(frontend): 引用徽章带来源库名 + 参考库引导文案"
```

---

## Task 15: 端到端验证与收口

**Files:** 无（验证任务）

- [ ] **Step 1: 后端全量**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
```

Expected: 全绿，0 failed。

- [ ] **Step 2: 前端全量**

```bash
cd frontend && node --test app/*.test.mjs && rm -rf .next && npm run build
```

Expected: 全绿 + 构建成功。

- [ ] **Step 3: 真机手工验证清单**

后端从 `backend/` 目录起（要加载 `../.env` 与真实库）。**不代劳启停用户服务** —— 若服务未跑，告知用户需要重启（本次 `SCHEMA_VERSION` 19→20，已部署库必须重启才会跑迁移）。

逐条走：

1. admin 把两个笔记本都发布为公共知识库 → 两个都显示为 base，互不降级
2. 普通笔记本编辑弹窗里能看到这两个公共库（即便它们在主列表隐藏）
3. 挂 1 个 → 提问，引用卡片出现该库的命中，徽章带库名
4. 挂自己的另一个笔记本 → 命中标「个人知识库」且徽章带库名
5. 全部取消挂载 → 提问只剩本库命中；深入分析按钮按 §7 文案引导
6. 挂 4 个 → 出现成本提示，但不拦截
7. admin 撤回其中一个公共库的发布 → 挂了它的笔记本里该行置灰并给出原因；重新发布 → 自动恢复

- [ ] **Step 4: 提 PR**

先 rebase 到 master 保持线性，再 push、建 PR（base=master）：

```bash
git fetch origin && git rebase origin/master
git push -u origin HEAD
gh pr create --base master --title "feat: 多领域基准库" --body "..."
```

PR 描述里必须写明：**`SCHEMA_VERSION` 19→20，已部署实例需重启后端才会执行迁移**；以及**不回填**——上线后所有笔记本都是未挂载状态，需各自选择参考库。

---

## Self-Review 记录

对照 spec 逐节核查的结果：

| Spec 章节 | 覆盖它的 Task |
| --- | --- |
| §1 数据模型（新表 / target_base_id / 去唯一性 / primary_domain label） | Task 1、Task 3、Task 13 Step 2 |
| §2 解析层收口 | Task 2 |
| §3 改动站点清单（9 行） | Task 2（3 行）、Task 4（2 行）、Task 5（2 行）、Task 6（1 行）、Task 7（1 行） |
| §4 tier 身份语义 + 徽章带库名 + 分布徽章 | Task 2（tier 保真）、Task 14 |
| §5 API 四端点 / 安全边界 / 失效边 active / 响应体变更 / 前端落点 / 文案 | Task 8、Task 2（失效边）、Task 5（响应体）、Task 12、Task 13 |
| §6 发布权限 / 晋升目标 / 成本 / eligible 洞 / 删除提示 | Task 8 Step 5、Task 7、Task 11（成本提示）、Task 6 |
| §7 不回填 + 三处引导文案 + merge_dbs | Task 1（迁移只建表不写边）、Task 14 Step 3、Task 9 |
| §8 明确不做 | 无 task —— 正确 |
| 测试策略全部条目 | Task 1/2/4/5/6/7/8 的测试步骤 |
| 实现约束（迁移双写 / 架构守卫 / 文档守卫 / 弯引号 / 前后端同 PR） | Global Constraints + Task 9 + Task 10 |

**已知的一处 spec 要求未独立成 task**：§6 的「删除被挂载的库时提示 N 个笔记本正在引用」。它落在既有的删除确认弹窗里，作为 Task 13 的附带项实现——若实现时发现删除弹窗结构复杂，拆成独立 task。

**类型一致性核查**：`NotebookRef` / `MountedBase` 在后端（Task 5 `schemas.py`）与前端（Task 11 `notebook-bases.ts`）字段完全对应（`id`/`name`/`tier` + `active`/`inactive_reason`）。`resolve_participants` 返回 `list[tuple[str, str]]`，被 `participant_ids`/`participant_tiers` 消费的形状在 Task 2 内自洽。`_any_base_notebook_has_kg` 的签名变更（新增必填 `notebook_id`）在 Task 4 Step 4 明确要求同步全部 caller。

**留给实现者的现场确认点**（plan 里已就地标注，不是占位符）：`create_knowledge_object` / `propose_promotion` / `create_app` / `can_write_notebook` / scale eligible 函数的确切签名与访问路径 —— 这些是既有 API，plan 给了定位命令，实现时按仓库现状对齐即可。
