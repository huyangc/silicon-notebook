"""单元测试:`scripts/merge_dbs.py` 的表分类守卫(部署侧运维脚本,不属于 app 包)。

按文件路径直接 import(同 test_mineru_probe.py)。只测 `assert_taxonomy_complete`:
对当前 SCHEMA_VERSION 的全新迁移库跑一遍,钉住"库里每张业务表/FTS 虚表都必须被
显式归类"这条不变量——下次再加新表却忘了登记分类清单,这条测试就会红,而不是
让 merge_dbs.py 在生产合并时才发现漏拷数据。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "merge_dbs.py"
)
_spec = importlib.util.spec_from_file_location("merge_dbs", _SCRIPT_PATH)
merge_dbs = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["merge_dbs"] = merge_dbs
_spec.loader.exec_module(merge_dbs)


@pytest.fixture
def fresh_db(tmp_path):
    """迁到当前 SCHEMA_VERSION 的全新 SQLite 库(只 migrate, 不 seed)。"""
    db_path = tmp_path / "fresh.db"
    merge_dbs.migrate_to_current(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def test_assert_taxonomy_complete_passes_for_fresh_schema(fresh_db):
    """全新迁移库的每张表都已被某个分类清单收纳 —— 不应该抛。"""
    merge_dbs.assert_taxonomy_complete(fresh_db)  # 不抛即通过


def test_assert_taxonomy_complete_flags_unclassified_command_catalog_tables(
    fresh_db, monkeypatch
):
    """变异验证:把 catalog_jobs / catalog_candidates 从分类清单里删掉,
    守卫必须 fail-loud(SystemExit),而不是静默放行未归类的表。"""
    monkeypatch.setattr(
        merge_dbs,
        "SKIP_SECONDARY_TABLES",
        [
            t
            for t in merge_dbs.SKIP_SECONDARY_TABLES
            if t not in ("catalog_jobs", "catalog_candidates")
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        merge_dbs.assert_taxonomy_complete(fresh_db)
    message = str(exc_info.value)
    assert "catalog_jobs" in message
    assert "catalog_candidates" in message


def _schema_db(definition: tuple[str, ...]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE object_schemas ("
        "object_type TEXT PRIMARY KEY, notebook_id TEXT NOT NULL DEFAULT '', "
        "plural TEXT, fields TEXT, primary_field TEXT, description TEXT, "
        "label TEXT, list_fields TEXT, source TEXT, status TEXT, rationale TEXT)"
    )
    db.execute(
        "INSERT INTO object_schemas VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        definition,
    )
    return db


def test_global_schema_union_rejects_same_name_with_different_semantics():
    base = (
        "claim", "", "claims", '["statement"]', "statement", "desc",
        "Claim", "[]", "builtin", "active", "",
    )
    left = _schema_db(base)
    right = _schema_db((*base[:6], "Different", *base[7:]))
    try:
        with pytest.raises(SystemExit, match="claim"):
            merge_dbs._assert_global_schema_compatibility(left, right)
    finally:
        left.close()
        right.close()


def test_global_schema_union_accepts_semantically_identical_rows():
    definition = (
        "claim", "", "claims", '["statement"]', "statement", "desc",
        "Claim", "[]", "builtin", "active", "",
    )
    left = _schema_db(definition)
    right = _schema_db(definition)
    try:
        merge_dbs._assert_global_schema_compatibility(left, right)
    finally:
        left.close()
        right.close()
