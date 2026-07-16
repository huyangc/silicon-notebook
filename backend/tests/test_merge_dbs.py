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
        (mem_id, nb_id, "user-local", "external_agent", "confirmed", title, "body text", NOW, NOW))

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
