from types import SimpleNamespace

from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


def test_initialize_orders_migrate_recover_seed():
    m = SqliteMigrator(SimpleNamespace(), SimpleNamespace())
    calls = []
    m.migrate = lambda: calls.append("migrate") or []
    m.recover_interrupted_jobs = lambda: calls.append("recover")
    m.seed = lambda: calls.append("seed")
    assert m.initialize() == []
    assert calls == ["migrate", "recover", "seed"]


def test_schema_version_constant_is_v23():
    # master v11/v12 hot-path indexes、Memory / Agent v13 migration、Task 1 的
    # sources.memory_id v14 migration、Task 5 的 parse_status/source_type 覆盖
    # 索引 v15 migration、knowhow-tables PR-1 Task 1 的五张新表 v16 migration、
    # paper-metadata Task 1 的 source_paper_meta/source_authors 两表 v17
    # migration、PR-2+3 Task 1 的 cell_code 表 + role 词表重映射 v18 migration、
    # source-asset-linking Task 2 的 notebook_assets.source_id 列 v19 migration
    # 与多领域基准库 Task 1 的 notebook_bases 挂载表 + promotion_candidates.
    # target_base_id 列 v20 migration、JS-trim anchor expression index v21 与
    # KG 构建任务状态表 v22、每用户模型服务最新状态表 v23 均保留；与 facade
    # 模块级 SCHEMA_VERSION 同步。
    assert SCHEMA_VERSION == 23


def test_add_column_guard_on_missing_table(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "x.db")
    SqliteMigrator.add_column_if_missing(db, "missing", "x", "TEXT")
    db.execute("create table t (id integer)")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    assert [r[1] for r in db.execute("pragma table_info(t)")] == ["id", "x"]
