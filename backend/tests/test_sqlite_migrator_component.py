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


def test_schema_version_constant_is_v17():
    # master v11/v12 hot-path indexes、Memory / Agent v13 migration、Task 1 的
    # sources.memory_id v14 migration、Task 5 的 parse_status/source_type 覆盖
    # 索引 v15 migration、knowhow-tables PR-1 Task 1 的五张新表 v16 migration
    # 与 paper-metadata Task 1 的 source_paper_meta/source_authors 两表 v17
    # migration 均保留；与 facade 模块级 SCHEMA_VERSION、test_follow_chain 守卫
    # 同步。
    assert SCHEMA_VERSION == 17


def test_add_column_guard_on_missing_table(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "x.db")
    SqliteMigrator.add_column_if_missing(db, "missing", "x", "TEXT")
    db.execute("create table t (id integer)")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    assert [r[1] for r in db.execute("pragma table_info(t)")] == ["id", "x"]
