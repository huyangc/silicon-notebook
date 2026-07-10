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


def test_schema_version_constant_is_v10():
    # rebase 到 master 后随 kg_rebuild_checkpoint(迁移10)升到 v10;
    # 与 facade 模块级 SCHEMA_VERSION、test_follow_chain 守卫同步。
    assert SCHEMA_VERSION == 10


def test_add_column_guard_on_missing_table(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "x.db")
    SqliteMigrator.add_column_if_missing(db, "missing", "x", "TEXT")
    db.execute("create table t (id integer)")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    assert [r[1] for r in db.execute("pragma table_info(t)")] == ["id", "x"]
