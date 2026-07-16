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
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{path}"))
    # 显式释放 repo 自己线程本地的 WAL 连接: 否则该连接只能靠 gc 回收(sqlite_repository
    # 内部闭包成环, 不会被立即引用计数释放), merge_core 测试里 ca/cb.close() 后紧跟着
    # shutil.copy2 原始文件会看到未 checkpoint 的 -wal, 漏掉刚写入的行。
    repo.close_local()
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


def test_migrate_checkpoints_wal_so_file_copy_is_complete(tmp_path):
    """migrate_to_current 必须 checkpoint WAL: 迁移后只拷 .db 文件(不含 -wal),
    副本里必须已含迁移写入(否则 WAL 未落盘, main() 的 copy/ATTACH 会静默丢数据)。"""
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
    cb.execute("INSERT INTO unified_kg_state(notebook_id,updated_at) VALUES('nb-b22222222',?)", (NOW,))
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
