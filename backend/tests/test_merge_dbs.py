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
