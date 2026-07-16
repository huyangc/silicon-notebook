# 合并两个共享 base 库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付离线、非破坏性的 `scripts/merge_dbs.py`，把两个共享同一 base 库的 SQLite 库合并成一个 v17 库（base 保留指定一侧、两边 personal notebook 全并入），含磁盘 storage 合并与中英 README。

**Architecture:** 先把两个输入库各拷一份、用 app 底层 `SqliteMigrator.migrate()`（**不** seed）迁到 SCHEMA_VERSION=17；preflight 强校验（版本一致 / base 唯一同 id / 非 base id 不重叠 / users 冲突）；然后 primary 副本整份复制为输出、`ATTACH` secondary 后按**数据驱动的表分类清单**批量 `INSERT` 导入 secondary 的非 base notebook 行；外部内容 FTS `rebuild`、清 KG 构建状态、合并 `storage/notebooks/`。一个**运行时完整性守卫**确保每张业务表都被显式归类，杜绝静默丢数据。

**Tech Stack:** Python 3.13 stdlib（`argparse`/`sqlite3`/`shutil`/`tempfile`/`pathlib`）+ 复用 `app.core.config.Settings`、`app.repositories.sqlite.database.SqliteDatabase`、`app.repositories.sqlite.migrations.SqliteMigrator`。测试 pytest，按文件路径 `importlib` 加载脚本。

## Global Constraints

- **SCHEMA_VERSION = 17**（`backend/app/repositories/sqlite/migrations.py:14`）；输出库必须是 17。
- **迁移只调 `SqliteMigrator(db, settings).migrate()`，绝不调 `.initialize()`/`.seed()`**——`_seed()` 会插 `user-local`、按 `settings.admin_password` **重置 admin 密码**、种 whitelist/object_schemas，会篡改源库数据。
- **非破坏性**：两个源库文件全程只读，只操作其临时副本；产出独立 `--out` + `--out-storage`。
- **fail-loud，绝不静默降级**（[[cli-no-silent-degradation]]）：任一 preflight 校验失败即打印原因 + 非零退出、不产出。
- **分类清单是超集，两向不对称**：迁移上来的老库常残留已废弃功能的遗留表（如 `articles`/`article_claims`/`derived_rule_candidates`/`extraction_candidates`，PR#110 删功能不 DROP 表；全新 v17 库没有）。守卫对"已分类但本库缺失"只提示、merge 时按表存在性跳过；对"本库有却未分类"致命中止（防静默漏拷）。**不要为了匹配全新库而从清单删表。** 守卫对两个输入库都跑。
- **所有跨库 INSERT 用显式列清单**（`PRAGMA table_info` 生成，排除隐式 rowid），让 SQLite 重分配 rowid。
- **FTS**：`chunks_fts`/`kg_objects_fts` 是独立内容 FTS（带 `notebook_id UNINDEXED`、无触发器）→ 按 `notebook_id` 列清单拷行；`memory_items_fts` 是外部内容 FTS（`content='memory_items'`+触发器）→ 导入后 `rebuild`；`*_fts_{config,content,data,docsize,idx}` 影子表绝不直接碰。
- **CLI 运行约定**：`PYTHONPATH=backend python scripts/merge_dbs.py ...`（同 `scripts/batch_ingest.py`）。
- **worktree 纪律**：文件写在 worktree（[[multi-agent-shared-checkout]]），收尾 rebase 到 master 提 PR（[[pr-merge-is-rebase]]）。
- **README 双写**：`README.md` + `README_zh.md` 同 PR（[[document-cli-in-readme]]），通用口径不写机器特定路径（[[committed-docs-stay-generic]]）。

---

### Task 1: 脚本骨架 + 表分类清单 + 完整性守卫

建立单文件脚本骨架与**数据驱动的表分类**，并用一个运行时守卫保证 v17 库的每张业务表都被恰好归入一类——未归类即 `raise`（未来 schema 加表时逼出人工分类，杜绝静默丢数据）。

**Files:**
- Create: `scripts/merge_dbs.py`
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Produces:
  - 模块级常量 `NOTEBOOKS_TABLE='notebooks'`、`NOTEBOOK_SCOPED_TABLES: list[str]`（40）、`FTS_NOTEBOOK_TABLES=['chunks_fts','kg_objects_fts']`、`CHILD_TABLES: list[tuple[str,str,str,str]]`（子表, 父表, 子表FK列, 父表键列）、`GLOBAL_UNION_TABLES: list[str]`、`EXTERNAL_FTS_TABLES=['memory_items_fts']`、`SKIP_SECONDARY_TABLES=['auth_sessions']`、`KG_STATE_TABLES=['kg_rebuild_checkpoint','unified_kg_state','kg_cluster_scratch']`
  - `discover_tables(conn) -> tuple[set[str], set[str]]` 返回 `(business_tables, virtual_fts_tables)`
  - `assert_taxonomy_complete(conn) -> None` 守卫；不完整则 `raise SystemExit`
  - `table_columns(conn, table) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_merge_dbs.py
from __future__ import annotations
import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "merge_dbs.py"
_spec = importlib.util.spec_from_file_location("merge_dbs", _SCRIPT)
md = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["merge_dbs"] = md
_spec.loader.exec_module(md)


def _fresh_db(path):
    """Fresh v17 schema+seed via the app repository (created at SCHEMA_VERSION)."""
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    SQLiteRepository(Settings(database_url=f"sqlite:///{path}"))
    return sqlite3.connect(path)


def test_taxonomy_covers_every_business_table(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    # Must not raise: every business/virtual table is classified.
    md.assert_taxonomy_complete(conn)


def test_taxonomy_guard_fails_on_unclassified_table(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    conn.execute("CREATE TABLE surprise_new_table (id TEXT PRIMARY KEY, notebook_id TEXT)")
    conn.commit()
    with pytest.raises(SystemExit):
        md.assert_taxonomy_complete(conn)


def test_taxonomy_tolerates_classified_table_absent(tmp_path):
    """已分类但本库缺失的表(如全新库没有的废弃表)只提示、不致命。"""
    conn = _fresh_db(tmp_path / "a.db")
    # 删掉一张确定存在且已分类的表, 模拟"清单里有、本库没有"
    conn.execute("DROP TABLE IF EXISTS notebook_assets")
    conn.commit()
    md.assert_taxonomy_complete(conn)  # 不应 raise
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -q`
Expected: FAIL —— `ModuleNotFoundError`/`AttributeError`（`scripts/merge_dbs.py` 尚不存在或无 `assert_taxonomy_complete`）

- [ ] **Step 3: 写脚本骨架 + 分类 + 守卫**

```python
#!/usr/bin/env python3
"""离线合并两个共享同一 base 库的 silicon_notebook SQLite 库。

用法:
  PYTHONPATH=backend python scripts/merge_dbs.py \
    --db-a A.db --storage-a A/storage \
    --db-b B.db --storage-b B/storage \
    --keep-base a --out merged.db --out-storage merged_storage \
    [--assume-same-users] [--dry-run] [--force]

非破坏性: 两个源库只读拷贝, 产出独立 --out / --out-storage。设计见
docs/superpowers/specs/2026-07-16-merge-duplicate-base-dbs-design.md。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# --- 表分类(SCHEMA_VERSION=17) --------------------------------------------
NOTEBOOKS_TABLE = "notebooks"  # 按 id 筛(自身即 notebook 行)

NOTEBOOK_SCOPED_TABLES = [
    "sources", "source_authors", "source_paper_meta", "chunks", "chunk_embeddings",
    "element_embeddings", "knowledge_objects", "knowledge_embeddings",
    "knowledge_relations", "knowledge_object_sources", "object_schemas",
    "concept_clusters", "concept_comentions", "concept_merge_candidates",
    "canonical_relations", "communities", "community_members", "mention_edges",
    "relation_embeddings", "unified_kg_state", "kg_rebuild_checkpoint",
    "kg_cluster_scratch", "kg_conflict_candidates", "merge_review_jobs",
    "promotion_candidates", "derived_rule_candidates", "extraction_runs",
    "extraction_candidates", "articles", "article_claims", "conversations",
    "answers", "feedback", "ask_jobs", "reports", "memory_items",
    "knowhow_tables", "notebook_assets", "notebook_members", "agent_token_notebooks",
]

# 独立内容 FTS(带 notebook_id 列, 无触发器) —— 按 notebook_id 列清单拷行
FTS_NOTEBOOK_TABLES = ["chunks_fts", "kg_objects_fts"]

# 子表: (子表, 父表, 子表FK列, 父表键列) —— 按父行集合筛
CHILD_TABLES = [
    ("source_elements", "sources", "source_id", "id"),
    ("knowhow_columns", "knowhow_tables", "table_id", "id"),
    ("knowhow_rows", "knowhow_tables", "table_id", "id"),
    ("knowhow_cells", "knowhow_rows", "row_id", "id"),
    ("memory_provenance", "memory_items", "memory_id", "id"),
    ("memory_revisions", "memory_items", "memory_id", "id"),
    ("memory_embeddings", "memory_items", "memory_id", "id"),
    ("ask_trace_steps", "ask_jobs", "job_id", "id"),
]

# 全局表: 主库优先取并集
GLOBAL_UNION_TABLES = [
    "users", "user_profiles", "agent_profiles", "agent_access_tokens",
    "concept_whitelist",
]

# 外部内容 FTS —— 导入后 rebuild
EXTERNAL_FTS_TABLES = ["memory_items_fts"]

# 副库不导入(临时登录会话, 用户重登即可; primary 的随整库复制保留)
SKIP_SECONDARY_TABLES = ["auth_sessions"]

# 导入后清空(引用可再生的 kg_index 产物, 逼部署侧干净重建)
KG_STATE_TABLES = ["kg_rebuild_checkpoint", "unified_kg_state", "kg_cluster_scratch"]

FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_docsize", "_config", "_content")


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def discover_tables(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """返回 (业务表, FTS 虚表)。排除 sqlite_* 与 FTS 影子表。"""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    virtual = {n for (n, s) in rows if s and "VIRTUAL TABLE" in s.upper()}
    shadow = {v + suf for v in virtual for suf in FTS_SHADOW_SUFFIXES}
    business = {n for (n, _) in rows if n not in shadow and n not in virtual}
    return business, virtual


def assert_taxonomy_complete(conn: sqlite3.Connection) -> None:
    """守卫: DB 中每张业务表/FTS 虚表都必须被显式归类, 否则 fail-loud(防静默丢数据)。

    分类清单是**超集**: 允许清单里的表在某个库里不存在——schema 随版本演进, 迁移上来
    的老库常残留已废弃功能的遗留表(如 articles/article_claims/derived_rule_candidates/
    extraction_candidates, PR#110 删了功能但不 DROP 表), 全新库则没有。这类"已分类但
    本库缺失"只提示、不致命(merge 时按表存在性跳过)。真正致命的是"本库有、但未分类"
    的表: 那会在合并时静默漏拷该表数据。"""
    business, virtual = discover_tables(conn)
    classified_business = (
        {NOTEBOOKS_TABLE}
        | set(NOTEBOOK_SCOPED_TABLES)
        | {t for (t, *_rest) in CHILD_TABLES}
        | set(GLOBAL_UNION_TABLES)
        | set(SKIP_SECONDARY_TABLES)
    )
    classified_virtual = set(FTS_NOTEBOOK_TABLES) | set(EXTERNAL_FTS_TABLES)
    unclassified_b = business - classified_business
    unclassified_v = virtual - classified_virtual
    absent = classified_business - business  # 已分类但本库没有 —— 容忍
    if absent:
        print(f"[提示] 分类清单含本库不存在的表(容忍, merge 时跳过): {sorted(absent)}",
              file=sys.stderr)
    if unclassified_b or unclassified_v:
        raise SystemExit(
            "发现未分类的表, 拒绝合并(防静默丢数据):\n"
            f"  未分类业务表: {sorted(unclassified_b)}\n"
            f"  未分类 FTS 虚表: {sorted(unclassified_v)}\n"
            "请把它们加进 scripts/merge_dbs.py 的对应分类清单后重跑。"
        )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - filled in Task 6
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -q`
Expected: PASS（3 passed）。若 `test_taxonomy_covers_every_business_table` 报 `未分类业务表` → 按报告把缺的表补进对应清单，再跑至通过（守卫的预期用途）。注意：清单里的表在全新 v17 库中不存在是**正常**的（如 articles 等 PR#110 废弃表，只在迁移上来的老库里残留），守卫容忍这种"已分类但本库缺失"、只打印提示，不要因此删清单里的表。

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): script skeleton + table taxonomy + completeness guard"
```

---

### Task 2: `migrate_to_current` —— 只迁 schema、不 seed

对一个库副本用底层 migrator 迁到 SCHEMA_VERSION=17，**不触发 seed**（不塞 user-local、不改密码）。

**Files:**
- Modify: `scripts/merge_dbs.py`（加 `migrate_to_current`）
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Consumes: `app.core.config.Settings`、`SqliteDatabase`、`SqliteMigrator`
- Produces: `migrate_to_current(db_path: Path) -> list[int]`（返回 applied 版本号列表；副作用=就地把 `db_path` 迁到 17）

- [ ] **Step 1: 写失败测试**

```python
def test_migrate_brings_v15_copy_to_17_and_recreates_tables(tmp_path):
    p = tmp_path / "old.db"
    _fresh_db(p).close()  # v17 schema
    # 模拟 v15: 降版本戳 + 丢掉 v16/v17 才建的表
    conn = sqlite3.connect(p)
    for t in ("knowhow_cells", "knowhow_rows", "knowhow_columns", "knowhow_tables",
              "notebook_assets", "source_paper_meta", "source_authors"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()

    applied = md.migrate_to_current(p)

    conn = sqlite3.connect(p)
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 17
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"knowhow_tables", "notebook_assets", "source_paper_meta", "source_authors"} <= names
    assert 16 in applied and 17 in applied
    conn.close()


def test_migrate_does_not_seed_user_local(tmp_path):
    """迁移绝不能塞 seed 的 user-local(那是 initialize/seed 的职责)。"""
    p = tmp_path / "old.db"
    _fresh_db(p).close()
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM users")  # 清空后模拟"无内建用户"的老库
    conn.execute("PRAGMA user_version = 16")
    conn.commit()
    conn.close()

    md.migrate_to_current(p)

    conn = sqlite3.connect(p)
    n = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    conn.close()
    assert n == 0, "migrate() 不应 seed 用户"


def test_migrate_checkpoints_wal_so_file_copy_is_complete(tmp_path):
    """migrate_to_current 必须 checkpoint WAL: 迁移后只拷 .db 文件(不含 -wal),
    副本里必须已含迁移写入(否则 WAL 未落盘, main() 的 copy/ATTACH 会静默丢数据)。
    没有 checkpoint 时: 副本 user_version 仍是 15、新表缺失 -> 断言失败。"""
    import shutil
    p = tmp_path / "old.db"
    _fresh_db(p).close()
    conn = sqlite3.connect(p)
    for t in ("knowhow_cells", "knowhow_rows", "knowhow_columns", "knowhow_tables",
              "notebook_assets", "source_paper_meta", "source_authors"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()

    md.migrate_to_current(p)

    p2 = tmp_path / "copied.db"           # 只拷 .db, 模拟 main() 的 shutil.copy2
    shutil.copy2(p, p2)
    conn = sqlite3.connect(p2)
    ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert ver == 17
    assert {"knowhow_tables", "source_paper_meta", "source_authors"} <= names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k migrate -q`
Expected: FAIL —— `AttributeError: module 'merge_dbs' has no attribute 'migrate_to_current'`

- [ ] **Step 3: 实现 `migrate_to_current`**

在 `discover_tables` 前后任意处加：
```python
def migrate_to_current(db_path: Path) -> list[int]:
    """把 db_path 就地迁到 SCHEMA_VERSION。只 migrate(), 不 seed。

    SqliteDatabase 用 WAL 模式; 迁移写入进 -wal, 若不 checkpoint 就返回, 主 .db 文件
    仍是旧内容 —— 后续 main() 里对该文件做 shutil.copy2 / merge_core 的 ATTACH 只读 .db,
    会静默丢掉刚迁移的数据。所以返回前必须 checkpoint(TRUNCATE) 并关闭连接, 保证磁盘
    上的 .db 文件是完整、自洽的。"""
    # 延迟 import: 让 Task 1 的纯 sqlite 测试无需 app 依赖即可跑。
    from app.core.config import Settings
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=db_path.parent)
    applied = SqliteMigrator(database, settings).migrate()
    try:  # WAL 落盘: 把 -wal 合并回 .db 并截断, 保证后续 copy/ATTACH 看到完整数据
        database.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        database.close_local()  # 关闭本线程 WAL 连接(SqliteDatabase 直连, 无 repo 闭包环)
    return applied
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k migrate -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): migrate a db copy to current schema without seeding"
```

---

### Task 3: `preflight` —— 版本 / base / 非 base 不重叠 / users 校验 + base 统计

对两个**已迁到 17**的库做前置校验，任一不过即 `raise SystemExit`；并打印两库 base 统计供人工核对 keep-base。

**Files:**
- Modify: `scripts/merge_dbs.py`
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Consumes: `table_columns`
- Produces:
  - `notebook_ids(conn) -> dict[str, str]`（id→tier）
  - `base_id(conn) -> str`（恰好一个 tier='base'，否则 raise）
  - `base_stats(conn, nb_id) -> dict[str,int]`（sources/chunks/knowledge_objects 行数）
  - `preflight(conn_a, conn_b, assume_same_users: bool) -> str`（返回共享 base id；不通过则 `raise SystemExit`）

- [ ] **Step 1: 写失败测试**

先加建库辅助（放测试文件顶部，`md` 加载之后）：
```python
NOW = "2026-01-01T00:00:00"

def _add_notebook(conn, nb_id, tier, name="nb"):
    conn.execute(
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) VALUES(?,?,'','','active','user-local',?,?,?)",
        (nb_id, name, NOW, NOW, tier),
    )

def _add_source(conn, nb_id, src_id):
    conn.execute(
        "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)", (src_id, nb_id, "t", "pdf", NOW, NOW))

def _add_chunk(conn, nb_id, src_id, chunk_id, text="hello world"):
    conn.execute(
        "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) VALUES(?,?,?,?,?)",
        (chunk_id, nb_id, src_id, text, NOW))
    conn.execute(
        "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES(?,?,?)",
        (chunk_id, nb_id, text))

def _add_kg_object(conn, nb_id, obj_id, name="Widget"):
    conn.execute(
        "INSERT INTO knowledge_objects(id,notebook_id,object_type,created_at,updated_at) "
        "VALUES(?,?,?,?,?)", (obj_id, nb_id, "concept", NOW, NOW))
    conn.execute(
        "INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES(?,?,?)",
        (obj_id, nb_id, name))

def _add_memory(conn, nb_id, mem_id, title="note"):
    conn.execute(
        "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (mem_id, nb_id, "user-local", "manual", "confirmed", title, "body text", NOW, NOW))

def _add_source_element(conn, src_id, el_id):
    conn.execute(
        "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) "
        "VALUES(?,?,?,?,?,?)", (el_id, src_id, "para", "p1", "element text", NOW))

BASE = "nb-base00000"

def _seed_pair(tmp_path):
    """primary=A(base+p_a1), secondary=B(base+p_b1); 各含跨类行。"""
    pa, pb = tmp_path / "a.db", tmp_path / "b.db"
    ca, cb = _fresh_db(pa), _fresh_db(pb)
    for c in (ca, cb):
        _add_notebook(c, BASE, "base", "Shared Base")
    # A 的 base 更全(2 源) —— keep-base=a
    _add_source(ca, BASE, "src-a-base"); _add_source(ca, BASE, "src-a-base2")
    _add_source(cb, BASE, "src-b-base")
    # A 的 personal
    _add_notebook(ca, "nb-a11111111", "personal", "A-personal")
    _add_source(ca, "nb-a11111111", "src-a1")
    _add_chunk(ca, "nb-a11111111", "src-a1", "ck-a1")
    _add_source_element(ca, "src-a1", "el-a1")
    # B 的 personal(id 与 A 不重叠)
    _add_notebook(cb, "nb-b22222222", "personal", "B-personal")
    _add_source(cb, "nb-b22222222", "src-b1")
    _add_chunk(cb, "nb-b22222222", "src-b1", "ck-b1", text="quantum flux")
    _add_kg_object(cb, "nb-b22222222", "obj-b1", name="Flux Capacitor")
    _add_memory(cb, "nb-b22222222", "mem-b1", title="flux note")
    _add_source_element(cb, "src-b1", "el-b1")
    ca.commit(); cb.commit()
    return pa, pb, ca, cb
```

测试：
```python
def test_preflight_ok_returns_shared_base(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    assert md.preflight(ca, cb, assume_same_users=True) == BASE

def test_preflight_rejects_version_mismatch(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    cb.execute("PRAGMA user_version = 16"); cb.commit()
    with pytest.raises(SystemExit):
        md.preflight(ca, cb, assume_same_users=True)

def test_preflight_rejects_different_base_id(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    cb.execute("UPDATE notebooks SET id='nb-otherbase' WHERE tier='base'"); cb.commit()
    with pytest.raises(SystemExit):
        md.preflight(ca, cb, assume_same_users=True)

def test_preflight_rejects_nonbase_id_collision(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    cb.execute("UPDATE notebooks SET id='nb-a11111111' WHERE id='nb-b22222222'"); cb.commit()
    with pytest.raises(SystemExit):
        md.preflight(ca, cb, assume_same_users=True)

def test_preflight_rejects_user_overlap_without_flag(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)  # 两库都有 seed 的 user-local → 天然重叠
    with pytest.raises(SystemExit):
        md.preflight(ca, cb, assume_same_users=False)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k preflight -q`
Expected: FAIL —— `AttributeError: ... 'preflight'`

- [ ] **Step 3: 实现 preflight 相关函数**

```python
def notebook_ids(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, tier FROM notebooks").fetchall()}


def base_id(conn: sqlite3.Connection) -> str:
    ids = [r[0] for r in conn.execute("SELECT id FROM notebooks WHERE tier='base'").fetchall()]
    if len(ids) != 1:
        raise SystemExit(f"期望恰好 1 个 base 库, 实得 {len(ids)} 个: {ids}")
    return ids[0]


def base_stats(conn: sqlite3.Connection, nb_id: str) -> dict[str, int]:
    out = {}
    for t in ("sources", "chunks", "knowledge_objects"):
        out[t] = conn.execute(
            f"SELECT count(*) FROM {t} WHERE notebook_id=?", (nb_id,)
        ).fetchone()[0]
    return out


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def preflight(conn_a: sqlite3.Connection, conn_b: sqlite3.Connection,
              assume_same_users: bool) -> str:
    # 0) 两库都跑分类守卫: 被导入的副库若有未分类表, 会在 merge 时静默漏拷其数据,
    #    所以两侧都要检查(不只 primary)。
    assert_taxonomy_complete(conn_a)
    assert_taxonomy_complete(conn_b)
    # 1) 版本一致且为 17
    va, vb = _user_version(conn_a), _user_version(conn_b)
    if not (va == vb == 17):
        raise SystemExit(f"schema 版本必须都为 17, 实得 A={va} B={vb}")
    # 2) 各恰好一个 base 且 id 相同
    ba, bb = base_id(conn_a), base_id(conn_b)
    if ba != bb:
        raise SystemExit(f"两库 base id 不同: A={ba} B={bb}; 无法认定为同一 base")
    # 3) notebook id 交集恰好只有 base
    ids_a, ids_b = set(notebook_ids(conn_a)), set(notebook_ids(conn_b))
    overlap = (ids_a & ids_b) - {ba}
    if overlap:
        raise SystemExit(f"除 base 外 notebook id 撞车, 无法安全移植: {sorted(overlap)}")
    # 4) users 交集
    ua = {r[0] for r in conn_a.execute("SELECT id FROM users")}
    ub = {r[0] for r in conn_b.execute("SELECT id FROM users")}
    u_overlap = ua & ub
    if u_overlap and not assume_same_users:
        raise SystemExit(
            f"两库有相同 user id: {sorted(u_overlap)}。若确为同一人, 加 --assume-same-users; "
            "否则请先在源库侧改 id 避免归属错乱。")
    # 5) 打印 base 统计供核对
    print(f"[base 统计] A({ba}): {base_stats(conn_a, ba)}", file=sys.stderr)
    print(f"[base 统计] B({bb}): {base_stats(conn_b, bb)}", file=sys.stderr)
    return ba
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k preflight -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): preflight validation + base stats"
```

---

### Task 4: `merge_core` —— ATTACH 导入所有类 + FK 校验 + FTS rebuild + 清 KG 状态

把 primary 库文件复制为输出，`ATTACH` secondary，按分类清单导入 secondary 的非 base notebook 行。

**Files:**
- Modify: `scripts/merge_dbs.py`
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Consumes: `table_columns`、`assert_taxonomy_complete`、分类清单常量
- Produces: `merge_core(out_db: Path, primary_db: Path, secondary_db: Path, shared_base: str) -> dict`（就地把 `primary_db` 复制到 `out_db` 再导入 secondary；返回 `{"imported_notebooks": [...], "row_counts": {...}}`）

- [ ] **Step 1: 写失败测试**

```python
def test_merge_core_conserves_rows_and_keeps_primary_base(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    ca.close(); cb.close()
    out = tmp_path / "merged.db"

    md.merge_core(out, pa, pb, shared_base=BASE)

    conn = sqlite3.connect(out)
    nb = {r[0]: r[1] for r in conn.execute("SELECT id, tier FROM notebooks")}
    # base 保留 primary 那份 + 两边 personal 都在
    assert nb == {BASE: "base", "nb-a11111111": "personal", "nb-b22222222": "personal"}
    # base 的源计数 = primary(A) 的 2, 不是 B 的 1
    assert conn.execute("SELECT count(*) FROM sources WHERE notebook_id=?", (BASE,)).fetchone()[0] == 2
    # B 的 personal 数据都进来了(跨 A/B/C 类)
    assert conn.execute("SELECT count(*) FROM chunks WHERE notebook_id='nb-b22222222'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM knowledge_objects WHERE notebook_id='nb-b22222222'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM memory_items WHERE notebook_id='nb-b22222222'").fetchone()[0] == 1
    # 子表(B 类)随父源进来
    assert conn.execute(
        "SELECT count(*) FROM source_elements WHERE source_id='src-b1'").fetchone()[0] == 1
    # FK 无悬挂
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_merge_core_fts_queryable_for_imported_notebook(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    ca.close(); cb.close()
    out = tmp_path / "merged.db"
    md.merge_core(out, pa, pb, shared_base=BASE)
    conn = sqlite3.connect(out)
    # 独立内容 FTS: 拷入的 chunk 命中
    assert conn.execute(
        "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'quantum'").fetchone()[0] == "ck-b1"
    assert conn.execute(
        "SELECT object_id FROM kg_objects_fts WHERE kg_objects_fts MATCH 'Flux'").fetchone()[0] == "obj-b1"
    # 外部内容 FTS: rebuild 后 memory 命中
    assert conn.execute(
        "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'flux'").fetchone() is not None
    conn.close()


def test_merge_core_clears_kg_state_for_imported(tmp_path):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    # 给 B 的 personal 塞一条 kg 构建状态, 应在导入后被清
    cb.execute("INSERT INTO unified_kg_state(notebook_id) VALUES('nb-b22222222')")
    cb.commit(); ca.close(); cb.close()
    out = tmp_path / "merged.db"
    md.merge_core(out, pa, pb, shared_base=BASE)
    conn = sqlite3.connect(out)
    assert conn.execute(
        "SELECT count(*) FROM unified_kg_state WHERE notebook_id='nb-b22222222'").fetchone()[0] == 0
    conn.close()


def _add_knowhow(conn, nb_id, tbl_id, cell_text):
    """一张 knowhow 表: 1 列 1 行 1 格(覆盖二级子表 cells->rows->tables)。"""
    conn.execute("INSERT INTO knowhow_tables(id,notebook_id,title,created_at,updated_at) "
                 "VALUES(?,?,?,?,?)", (tbl_id, nb_id, "T", NOW, NOW))
    conn.execute("INSERT INTO knowhow_columns(id,table_id,name,position) VALUES(?,?,?,0)",
                 (tbl_id + "-c", tbl_id, "col"))
    conn.execute("INSERT INTO knowhow_rows(id,table_id,position,created_at,updated_at) "
                 "VALUES(?,?,0,?,?)", (tbl_id + "-r", tbl_id, NOW, NOW))
    conn.execute("INSERT INTO knowhow_cells(id,row_id,column_id,content_md,updated_at) "
                 "VALUES(?,?,?,?,?)", (tbl_id + "-cell", tbl_id + "-r", tbl_id + "-c",
                                       cell_text, NOW))


def test_merge_core_grandchild_excludes_secondary_base_knowhow(tmp_path):
    """knowhow_cells 是二级子表: secondary base 的 cells 不得被带入(否则 row_id 悬挂)。"""
    pa, pb, ca, cb = _seed_pair(tmp_path)
    _add_knowhow(cb, BASE, "kt-b-base", "BASE-CELL")          # secondary base 的 knowhow
    _add_knowhow(cb, "nb-b22222222", "kt-b-personal", "P-CELL")  # secondary personal 的 knowhow
    cb.commit(); ca.close(); cb.close()
    out = tmp_path / "merged.db"
    md.merge_core(out, pa, pb, shared_base=BASE)
    conn = sqlite3.connect(out)
    cells = {r[0] for r in conn.execute("SELECT content_md FROM knowhow_cells")}
    assert "P-CELL" in cells and "BASE-CELL" not in cells
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []  # 无悬挂
    conn.close()
```
> 注：`unified_kg_state` 若有额外 NOT NULL 列，测试 INSERT 需补列——运行时按报错补齐。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k merge_core -q`
Expected: FAIL —— `AttributeError: ... 'merge_core'`

- [ ] **Step 3: 实现 `merge_core`**

```python
import shutil


def _col_list(conn: sqlite3.Connection, table: str) -> str:
    return ", ".join(table_columns(conn, table))


def _table_exists(conn: sqlite3.Connection, table: str, schema: str = "main") -> bool:
    return conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def merge_core(out_db: Path, primary_db: Path, secondary_db: Path,
               shared_base: str) -> dict:
    if out_db.exists():
        raise SystemExit(f"输出已存在: {out_db}(用 --force 覆盖或换路径)")
    shutil.copy2(primary_db, out_db)

    conn = sqlite3.connect(out_db)
    try:
        assert_taxonomy_complete(conn)
        conn.execute("PRAGMA foreign_keys = OFF")  # 导入期不校验; 结束后统一 foreign_key_check
        conn.execute("ATTACH DATABASE ? AS sec", (str(secondary_db),))

        sec_nb = [r[0] for r in conn.execute(
            "SELECT id FROM sec.notebooks WHERE id != ?", (shared_base,)).fetchall()]
        ph = ",".join("?" for _ in sec_nb) or "NULL"  # sec_nb 为空时 IN (NULL) 匹配 0 行

        # 子表 -> 限定 FK 落在"以 sec_nb 为界的已导入父行"内(每个子句恰含一个 IN ({ph}))。
        # knowhow_cells 是二级子表(cells->rows->tables.notebook_id), 必须两层下钻,
        # 否则会带入 secondary base 的 cells -> row_id 悬挂 -> FK 失败。
        child_scopes = {
            "source_elements": f"source_id IN (SELECT id FROM sec.sources WHERE notebook_id IN ({ph}))",
            "knowhow_columns": f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_rows":    f"table_id IN (SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph}))",
            "knowhow_cells":   (f"row_id IN (SELECT id FROM sec.knowhow_rows WHERE table_id IN "
                                f"(SELECT id FROM sec.knowhow_tables WHERE notebook_id IN ({ph})))"),
            "memory_provenance": f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "memory_revisions":  f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "memory_embeddings": f"memory_id IN (SELECT id FROM sec.memory_items WHERE notebook_id IN ({ph}))",
            "ask_trace_steps":   f"job_id IN (SELECT id FROM sec.ask_jobs WHERE notebook_id IN ({ph}))",
        }
        missing = {c for (c, *_r) in CHILD_TABLES} - set(child_scopes)
        if missing:  # 新增子表却没定义导入范围 -> fail-loud
            raise SystemExit(f"子表缺少导入范围定义: {sorted(missing)}")

        row_counts: dict[str, int] = {}

        def _run(table: str, where: str) -> None:
            # 两库都得有该表才能跨库 INSERT; 版本演进导致某库缺表则跳过(见守卫容忍逻辑)。
            if not (_table_exists(conn, table, "main") and _table_exists(conn, table, "sec")):
                return
            cols = _col_list(conn, table)
            cur = conn.execute(
                f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM sec.{table} WHERE {where}",
                tuple(sec_nb))  # where 恰含一个 IN ({ph}) -> 一份 sec_nb 参数
            row_counts[table] = row_counts.get(table, 0) + cur.rowcount

        with conn:  # 单事务(FK off 期间, 顺序无关)
            _run(NOTEBOOKS_TABLE, f"id IN ({ph})")                       # notebooks 自身按 id
            for t in NOTEBOOK_SCOPED_TABLES + FTS_NOTEBOOK_TABLES:        # A 类 + 独立 FTS
                _run(t, f"notebook_id IN ({ph})")
            for child, *_rest in CHILD_TABLES:                            # B 类: 显式范围
                _run(child, child_scopes[child])
            for t in GLOBAL_UNION_TABLES:                                # C 类: 主库优先并集
                if not (_table_exists(conn, t, "main") and _table_exists(conn, t, "sec")):
                    continue
                cols = _col_list(conn, t)
                conn.execute(
                    f"INSERT OR IGNORE INTO main.{t} ({cols}) SELECT {cols} FROM sec.{t}")
            for t in KG_STATE_TABLES:                                    # 清导入 notebook 的 KG 状态
                if _table_exists(conn, t, "main"):
                    conn.execute(
                        f"DELETE FROM main.{t} WHERE notebook_id IN ({ph})", tuple(sec_nb))

        # 外部内容 FTS rebuild(在自己的事务里), 提交后再 DETACH(DETACH 不能在事务中)。
        for t in EXTERNAL_FTS_TABLES:
            if _table_exists(conn, t, "main"):
                conn.execute(f"INSERT INTO main.{t}({t}) VALUES('rebuild')")
        conn.commit()

        dangling = conn.execute("PRAGMA foreign_key_check").fetchall()
        if dangling:
            raise SystemExit(f"合并后存在悬挂外键, 已中止: {dangling[:20]}")

        conn.execute("DETACH DATABASE sec")
        conn.commit()
        return {"imported_notebooks": sec_nb, "row_counts": row_counts}
    finally:
        conn.close()
```
> 说明：`child_scopes` 对每张子表显式给出"限定在 sec_nb 内的父行"WHERE 子句，`knowhow_cells` 二级下钻到 `knowhow_tables.notebook_id`，避免带入 secondary base 的 cells。`_table_exists` 让所有循环对"清单里有、本库没有"的表（如全新库缺的 articles 等废弃表）安全跳过，与 Task 1 守卫的超集容忍一致。`foreign_key_check` 是最终正确性保证（导入期 FK off 只为免除插入顺序约束）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k merge_core -q`
Expected: PASS（3 passed）。若某表因额外 NOT NULL 列插入失败，补 fixture 列后重跑。

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): core ATTACH import across all table classes"
```

---

### Task 5: `merge_storage` —— 合并 storage/notebooks，跳过可再生产物

**Files:**
- Modify: `scripts/merge_dbs.py`
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Produces: `merge_storage(out_storage: Path, primary_storage: Path, secondary_storage: Path, imported_notebooks: list[str]) -> None`

- [ ] **Step 1: 写失败测试**

```python
def test_merge_storage_copies_primary_whole_and_secondary_imported(tmp_path):
    ps = tmp_path / "pstore"; ss = tmp_path / "sstore"; out = tmp_path / "outstore"
    # primary storage: base + a1 目录, 外加可再生 kg_index
    (ps / "notebooks" / BASE).mkdir(parents=True)
    (ps / "notebooks" / BASE / "f.pdf").write_text("base-primary")
    (ps / "notebooks" / "nb-a11111111").mkdir(parents=True)
    (ps / "notebooks" / "nb-a11111111" / "a.pdf").write_text("a-src")
    (ps / "kg_index").mkdir(); (ps / "kg_index" / "ann.bin").write_text("regenerable")
    # secondary storage: base(应忽略) + b1(应拷)
    (ss / "notebooks" / BASE).mkdir(parents=True)
    (ss / "notebooks" / BASE / "f.pdf").write_text("base-secondary-SHOULD-NOT-WIN")
    (ss / "notebooks" / "nb-b22222222").mkdir(parents=True)
    (ss / "notebooks" / "nb-b22222222" / "b.pdf").write_text("b-src")

    md.merge_storage(out, ps, ss, imported_notebooks=["nb-b22222222"])

    assert (out / "notebooks" / BASE / "f.pdf").read_text() == "base-primary"
    assert (out / "notebooks" / "nb-a11111111" / "a.pdf").read_text() == "a-src"
    assert (out / "notebooks" / "nb-b22222222" / "b.pdf").read_text() == "b-src"
    assert not (out / "kg_index").exists()  # 可再生, 不搬
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k storage -q`
Expected: FAIL —— `AttributeError: ... 'merge_storage'`

- [ ] **Step 3: 实现**

```python
def merge_storage(out_storage: Path, primary_storage: Path,
                  secondary_storage: Path, imported_notebooks: list[str]) -> None:
    out_nb = out_storage / "notebooks"
    out_nb.mkdir(parents=True, exist_ok=True)
    # primary 的 notebooks/ 整份(不含 kg_index / kg_viz)
    prim_nb = primary_storage / "notebooks"
    if prim_nb.is_dir():
        shutil.copytree(prim_nb, out_nb, dirs_exist_ok=True)
    # secondary 的每个导入 notebook 目录
    for nb_id in imported_notebooks:
        src = secondary_storage / "notebooks" / nb_id
        if not src.is_dir():
            continue
        dst = out_nb / nb_id
        if dst.exists():
            raise SystemExit(f"storage 目录撞车(不应发生): {dst}")
        shutil.copytree(src, dst)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k storage -q`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): merge storage/notebooks, skip regenerable artifacts"
```

---

### Task 6: `main` —— CLI 编排 + dry-run + --force + 端到端测试

把 Task 1–5 串起来：拷副本→迁移→preflight→(dry-run 停)→merge_core→merge_storage→打印总结。

**Files:**
- Modify: `scripts/merge_dbs.py`（实现 `main`）
- Test: `backend/tests/test_merge_dbs.py`

**Interfaces:**
- Consumes: `migrate_to_current`、`preflight`、`merge_core`、`merge_storage`、`base_stats`
- Produces: `main(argv) -> int`；`--keep-base a|b` 决定 primary/secondary

- [ ] **Step 1: 写失败测试（端到端，走 argv）**

```python
def _run_cli(tmp_path, extra=()):
    pa, pb, ca, cb = _seed_pair(tmp_path)
    ca.close(); cb.close()
    (tmp_path / "sa" / "notebooks" / "nb-a11111111").mkdir(parents=True)
    (tmp_path / "sb" / "notebooks" / "nb-b22222222").mkdir(parents=True)
    (tmp_path / "sb" / "notebooks" / "nb-b22222222" / "b.pdf").write_text("b")
    argv = [
        "--db-a", str(pa), "--storage-a", str(tmp_path / "sa"),
        "--db-b", str(pb), "--storage-b", str(tmp_path / "sb"),
        "--keep-base", "a",
        "--out", str(tmp_path / "merged.db"),
        "--out-storage", str(tmp_path / "mstore"),
        "--assume-same-users", *extra,
    ]
    return md.main(argv), tmp_path

def test_cli_end_to_end_produces_merged_db_and_storage(tmp_path):
    rc, tp = _run_cli(tmp_path)
    assert rc == 0
    conn = sqlite3.connect(tp / "merged.db")
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 17
    nb = {r[0] for r in conn.execute("SELECT id FROM notebooks")}
    assert nb == {BASE, "nb-a11111111", "nb-b22222222"}
    conn.close()
    assert (tp / "mstore" / "notebooks" / "nb-b22222222" / "b.pdf").read_text() == "b"


def test_cli_end_to_end_migrates_v15_and_v16_inputs(tmp_path):
    """最贴近真实场景: 输入是 v15 + v16 库, main() 应先各自迁到 17 再合并。
    降级会丢掉 v16/v17 才建的(空)表; chunks/source_elements 等数据表保留。
    这条端到端跑通 migrate(含 WAL 落盘)->preflight->merge, 是用户实际情形的守卫。"""
    pa, pb, ca, cb = _seed_pair(tmp_path)
    for t in ("knowhow_cells", "knowhow_rows", "knowhow_columns", "knowhow_tables",
              "notebook_assets", "source_paper_meta", "source_authors"):
        ca.execute(f"DROP TABLE IF EXISTS {t}")     # A -> v15
    ca.execute("PRAGMA user_version = 15")
    for t in ("source_paper_meta", "source_authors"):
        cb.execute(f"DROP TABLE IF EXISTS {t}")      # B -> v16
    cb.execute("PRAGMA user_version = 16")
    ca.commit(); cb.commit(); ca.close(); cb.close()
    (tmp_path / "sa" / "notebooks").mkdir(parents=True)
    (tmp_path / "sb" / "notebooks" / "nb-b22222222").mkdir(parents=True)
    (tmp_path / "sb" / "notebooks" / "nb-b22222222" / "b.pdf").write_text("b")
    rc = md.main([
        "--db-a", str(pa), "--storage-a", str(tmp_path / "sa"),
        "--db-b", str(pb), "--storage-b", str(tmp_path / "sb"),
        "--keep-base", "a", "--out", str(tmp_path / "merged.db"),
        "--out-storage", str(tmp_path / "mstore"), "--assume-same-users",
    ])
    assert rc == 0
    conn = sqlite3.connect(tmp_path / "merged.db")
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 17
    nb = {r[0] for r in conn.execute("SELECT id FROM notebooks")}
    assert nb == {BASE, "nb-a11111111", "nb-b22222222"}
    # 迁移写入的数据表随合并保留(chunks 从 B 的 personal 带过来)
    assert conn.execute(
        "SELECT count(*) FROM chunks WHERE notebook_id='nb-b22222222'").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_cli_dry_run_writes_nothing(tmp_path):
    rc, tp = _run_cli(tmp_path, extra=("--dry-run",))
    assert rc == 0
    assert not (tp / "merged.db").exists()
    assert not (tp / "mstore").exists()

def test_cli_refuses_existing_out_without_force(tmp_path):
    rc, tp = _run_cli(tmp_path)
    assert rc == 0
    # 二次运行同 out 无 --force → 非零
    pa2 = tp / "merged.db"
    with pytest.raises(SystemExit):
        md.main([
            "--db-a", str(tp / "a.db"), "--storage-a", str(tp / "sa"),
            "--db-b", str(tp / "b.db"), "--storage-b", str(tp / "sb"),
            "--keep-base", "a", "--out", str(pa2),
            "--out-storage", str(tp / "mstore2"), "--assume-same-users",
        ])


def _cli_argv(tp, out="merged.db", mstore="mstore", extra=()):
    return [
        "--db-a", str(tp / "a.db"), "--storage-a", str(tp / "sa"),
        "--db-b", str(tp / "b.db"), "--storage-b", str(tp / "sb"),
        "--keep-base", "a", "--out", str(tp / out),
        "--out-storage", str(tp / mstore), "--assume-same-users", *extra,
    ]


def test_cli_dry_run_not_blocked_by_existing_out(tmp_path):
    """dry-run 即使 --out 已存在也应放行(它不写 out), 且不改动那个已存在的文件。"""
    rc, tp = _run_cli(tmp_path)  # 先产出一个 merged.db
    assert rc == 0
    before = (tp / "merged.db").stat().st_mtime_ns
    rc2 = md.main(_cli_argv(tp, extra=("--dry-run",)))  # 无 --force
    assert rc2 == 0
    assert (tp / "merged.db").stat().st_mtime_ns == before  # 原文件未被动过

def test_cli_force_overwrites_existing_out(tmp_path):
    """--force 应覆盖已存在的 --out 并成功产出。"""
    rc, tp = _run_cli(tmp_path)
    assert rc == 0
    rc2 = md.main(_cli_argv(tp, mstore="mstore2", extra=("--force",)))
    assert rc2 == 0
    conn = sqlite3.connect(tp / "merged.db")
    nb = {r[0] for r in conn.execute("SELECT id FROM notebooks")}
    conn.close()
    assert nb == {BASE, "nb-a11111111", "nb-b22222222"}
```
> 注：v15/v16 库经 `--db-a/--db-b` 传入时 `main` 会先 `migrate_to_current` 迁到 17；本测试用 `_seed_pair`（已是 17）直接验证编排；真实 v15+v16 迁移端到端见 `test_cli_end_to_end_migrates_v15_and_v16_inputs`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -k cli -q`
Expected: FAIL —— `NotImplementedError`

- [ ] **Step 3: 实现 `main`**

```python
import tempfile


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="离线合并两个共享 base 的 silicon_notebook 库")
    ap.add_argument("--db-a", required=True); ap.add_argument("--storage-a", required=True)
    ap.add_argument("--db-b", required=True); ap.add_argument("--storage-b", required=True)
    ap.add_argument("--keep-base", required=True, choices=["a", "b"],
                    help="保留哪侧的 base(= 该侧为容器/primary)")
    ap.add_argument("--out", required=True); ap.add_argument("--out-storage", required=True)
    ap.add_argument("--assume-same-users", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    out = Path(args.out)
    # dry-run 不写 out, 不该被"输出已存在"挡住(它就是拿来预览计划的); 只在真跑时早退。
    if out.exists() and not args.force and not args.dry_run:
        raise SystemExit(f"输出已存在: {out}(加 --force 覆盖或换路径)")

    # primary = keep-base 那侧
    if args.keep_base == "a":
        prim_db, prim_store = Path(args.db_a), Path(args.storage_a)
        sec_db, sec_store = Path(args.db_b), Path(args.storage_b)
    else:
        prim_db, prim_store = Path(args.db_b), Path(args.storage_b)
        sec_db, sec_store = Path(args.db_a), Path(args.storage_a)

    with tempfile.TemporaryDirectory(prefix="merge_dbs_") as tmp:
        tmp = Path(tmp)
        prim_copy, sec_copy = tmp / "primary.db", tmp / "secondary.db"
        shutil.copy2(prim_db, prim_copy); shutil.copy2(sec_db, sec_copy)

        ap_applied = migrate_to_current(prim_copy)
        bp_applied = migrate_to_current(sec_copy)
        print(f"[迁移] primary applied={ap_applied} secondary applied={bp_applied}", file=sys.stderr)

        conn_p, conn_s = sqlite3.connect(prim_copy), sqlite3.connect(sec_copy)
        try:
            shared_base = preflight(conn_p, conn_s, args.assume_same_users)
            sec_nb = [r[0] for r in conn_s.execute(
                "SELECT id FROM notebooks WHERE id != ?", (shared_base,)).fetchall()]
        finally:
            conn_p.close(); conn_s.close()

        print(f"[计划] 将导入 {len(sec_nb)} 个 notebook: {sec_nb}", file=sys.stderr)
        if args.dry_run:
            print("[dry-run] 未产出任何文件。", file=sys.stderr)
            return 0

        if out.exists() and args.force:
            out.unlink()
        try:
            result = merge_core(out, prim_copy, sec_copy, shared_base)
            merge_storage(Path(args.out_storage), prim_store, sec_store,
                          result["imported_notebooks"])
        except BaseException:
            # merge_core 在 FK 校验失败时会留下已提交的部分 out_db; 失败即删, 不留半成品。
            if out.exists():
                out.unlink()
            raise

    print(f"[完成] 输出库={out}  导入 notebook={result['imported_notebooks']}", file=sys.stderr)
    print(f"[完成] 行数={result['row_counts']}", file=sys.stderr)
    print("[提醒] 部署后在 app 内点一次「重建索引/刷新图谱」以重生成 kg_index/kg_viz/ANN。",
          file=sys.stderr)
    return 0
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -q`
Expected: PASS（全部通过）

- [ ] **Step 5: 提交**

```bash
git add scripts/merge_dbs.py backend/tests/test_merge_dbs.py
git commit -m "feat(merge): CLI orchestration with dry-run and force"
```

---

### Task 7: README 中英文档 + 收尾

**Files:**
- Modify: `README.md`、`README_zh.md`

**Interfaces:** 无代码接口；纯文档。

- [ ] **Step 1: 找到 README 中脚本/CLI 列表章节**

Run: `grep -n "batch_ingest\|scripts/" README.md README_zh.md | head`
在 `scripts/batch_ingest.py` 等条目附近插入 `merge_dbs.py`。

- [ ] **Step 2: 写 README_zh.md 条目**

在对应「脚本/工具」小节加：
```markdown
#### `scripts/merge_dbs.py` —— 合并两个共享 base 的库

把两个各自部署、**共享同一个 base 库**（同 notebook id）的 SQLite 库离线合并成一个，
base 保留指定一侧、两边其余 notebook 全部并入。非破坏性（源库只读，产出新文件）。
两库 schema 版本低于当前时会先各自迁到最新。

```bash
PYTHONPATH=backend python scripts/merge_dbs.py \
  --db-a A/silicon_notebook.db --storage-a A/storage \
  --db-b B/silicon_notebook.db --storage-b B/storage \
  --keep-base a \
  --out merged/silicon_notebook.db --out-storage merged/storage \
  --assume-same-users
```

- `--keep-base a|b`：保留哪侧的 base（更全的那侧）；运行时会打印两侧 base 统计供核对。
- `--assume-same-users`：两库有相同 user id 时确认是同一人（否则中止）。
- `--dry-run`：只迁移+校验+打印将导入的 notebook，不产出。
- 前提：除 base 外两库 notebook id 不重叠（撞车会中止报告）。
- 合并后把 `merged/` 部署到保留的那台，首次启动后在 app 内点「重建索引/刷新图谱」重生成 ANN/图产物。
```
```

- [ ] **Step 3: 写 README.md 对应英文条目**

同结构英文版（`#### scripts/merge_dbs.py — Merge two shared-base DBs`，正文 English）。

- [ ] **Step 4: 跑全量脚本测试确认未回归**

Run: `cd backend && python -m pytest tests/test_merge_dbs.py -q`
Expected: PASS

- [ ] **Step 5: 提交 + rebase + PR**

```bash
git add README.md README_zh.md
git commit -m "docs: document scripts/merge_dbs.py in README (zh/en)"
git fetch origin && git rebase origin/master
git push -u origin claude/merge-duplicate-dbs-d5e135
gh pr create --base master --title "feat: offline merge of two shared-base DBs" \
  --body "见 docs/superpowers/specs/2026-07-16-merge-duplicate-base-dbs-design.md。

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Self-Review

**1. Spec coverage：**
- 迁移到 17（只 migrate 不 seed）→ Task 2 ✓
- Preflight 四项校验 + base 统计 → Task 3 ✓
- A/B/C 三类 + notebooks + 两种 FTS + KG 状态清理 → Task 4 ✓
- storage 合并、跳过 kg_index/kg_viz → Task 5 ✓
- 非破坏性 / dry-run / force → Task 6 ✓
- 完整性守卫（含 auth_sessions 显式跳过、防静默丢数据）→ Task 1 ✓
- README 中英 → Task 7 ✓
- 非目标（不做 base KG 去重、不 id 重映射、不搬可再生产物）→ 均未实现，符合 ✓

**2. Placeholder scan：** 每个 code step 含完整可运行代码；两处「运行时按 NOT NULL 报错补 fixture 列」是 TDD 正常迭代提示、非占位。无 TODO/TBD。

**3. Type consistency：** `migrate_to_current(Path)->list[int]`、`preflight(conn,conn,bool)->str`、`merge_core(Path,Path,Path,str)->dict`(键 `imported_notebooks`/`row_counts`)、`merge_storage(Path,Path,Path,list)->None` 在 Task 6 `main` 中调用签名一致 ✓。`assert_taxonomy_complete` 在 Task 1 定义、Task 4 `merge_core` 调用 ✓。分类常量在 Task 1 定义、Task 4 消费 ✓。
