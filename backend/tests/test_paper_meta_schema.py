"""source_paper_meta / source_authors 迁移测试(paper-metadata Task 1)。

镜像 test_knowhow_schema.py 的两层覆盖:全新库经 _migration_1..17 建齐;
已部署库(user_version=16)经版本闸补建 —— 防「新表塞进已封版迁移导致
已部署库漏建」(schema-migration-convention)。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.services import sqlite_repository as sr
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/paper_meta.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def _columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def _indexes(db):
    return {
        r["name"]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }


# 断言 1(全新库): 两表存在,列序如下
EXPECTED_PAPER_META_COLS = [
    "source_id", "notebook_id", "is_paper", "paper_title", "venue", "pub_year",
    "doi", "keywords", "raw_json", "model", "created_at", "updated_at",
]
EXPECTED_AUTHOR_COLS = [
    "id", "source_id", "notebook_id", "position", "name", "affiliation", "created_at",
]


def _mk_notebook(repo, nb_id="nb1"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES (?, 'n', 't', 't')",
            (nb_id,),
        )


def _mk_source(repo, nb_id, sid):
    """插一行最小 sources 满足 source_paper_meta/source_authors 的 FK
    (与 test_mention_bridge._mk_src 同款)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES (?, ?, 'T', 'pdf', 't', 't')",
            (sid, nb_id),
        )


def test_fresh_db_has_paper_meta_tables_and_indexes(repo):
    """断言 1 + 3:全新库经 _migration_1..17 建齐两表(列序与 brief 一致)+
    其 idx_source_paper_meta_nb/idx_source_authors_source/idx_source_authors_nb
    三索引。"""
    with repo._connect() as db:
        assert _columns(db, "source_paper_meta") == EXPECTED_PAPER_META_COLS
        assert _columns(db, "source_authors") == EXPECTED_AUTHOR_COLS
        idx = _indexes(db)
    assert "idx_source_paper_meta_nb" in idx
    assert "idx_source_authors_source" in idx
    assert "idx_source_authors_nb" in idx


def test_v16_db_upgraded_gets_paper_meta_tables(repo):
    """断言 2:已部署库(user_version=16,即本任务之前)重启后必须由 _migration_17
    补齐两表(schema-migration-convention 教训用例:回退状态 + 版本闸 + 重跑
    _migrate,镜像 test_knowhow_schema.py::test_v15_db_upgraded_gets_knowhow_tables
    的构造方式:`PRAGMA user_version = 15` -> 这里对应 16)。"""
    with repo._connect() as db:
        db.execute("DROP TABLE IF EXISTS source_authors")
        db.execute("DROP TABLE IF EXISTS source_paper_meta")
        db.execute("PRAGMA user_version = 16")

    with repo._connect() as db:
        assert not _columns(db, "source_paper_meta")
        assert not _columns(db, "source_authors")

    applied = repo._migrate()

    assert 17 in applied
    with repo._connect() as db:
        # 版本闸落到当前 SCHEMA_VERSION(非硬编码字面量,防后续新迁移使断言假红)。
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == sr.SCHEMA_VERSION
        assert _columns(db, "source_paper_meta") == EXPECTED_PAPER_META_COLS
        assert _columns(db, "source_authors") == EXPECTED_AUTHOR_COLS
        idx = _indexes(db)
    assert "idx_source_paper_meta_nb" in idx
    assert "idx_source_authors_source" in idx
    assert "idx_source_authors_nb" in idx


def test_deleting_source_cascades_to_paper_meta_and_authors(repo):
    """断言 4:级联 —— 插入 notebook/source/meta/author 行后 DELETE sources 行,
    两表对应行消失(PRAGMA foreign_keys 由 database.py 的 _new_connection 默认
    ON,与 test_knowhow_schema.py 同款,零显式设置)。"""
    _mk_notebook(repo)
    _mk_source(repo, "nb1", "src1")
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_paper_meta "
            "(source_id, notebook_id, is_paper, paper_title, created_at, updated_at) "
            "VALUES ('src1', 'nb1', 1, 'T', 't', 't')"
        )
        db.execute(
            "INSERT INTO source_authors "
            "(id, source_id, notebook_id, position, name, created_at) "
            "VALUES ('au1', 'src1', 'nb1', 0, 'Alice', 't')"
        )

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM source_paper_meta"
        ).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) c FROM source_authors"
        ).fetchone()["c"] == 1

    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id='src1'")

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM source_paper_meta"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM source_authors"
        ).fetchone()["c"] == 0
