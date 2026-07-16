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
