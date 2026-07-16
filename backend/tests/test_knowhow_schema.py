"""Task 1 (knowhow-tables PR-1): schema migration 16 — five new tables for the
knowhow-table feature (editable grid truth source) + notebook image assets.

knowhow_tables/knowhow_columns/knowhow_rows/knowhow_cells hold the
user-edited grid that Task 2's repository module and Task 6's import API
read/write; column role + position drive the row-detail drawer's rendering
order (Task 8); projection_status is the per-row state machine Task 5's
deterministic projector advances (pending -> synced/failed). notebook_assets
stores upload metadata for images embedded inline in cell markdown (Task 4's
authed asset routes). See
docs/superpowers/plans/2026-07-15-knowhow-tables-pr1.md Task 1.
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
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


KNOWHOW_TABLE_COLUMNS = {
    "knowhow_tables": {
        "id", "notebook_id", "title", "description", "mutation_seq",
        "hidden_source_id", "created_by", "created_at", "updated_at",
    },
    "knowhow_columns": {"id", "table_id", "name", "role", "position"},
    "knowhow_rows": {
        "id", "table_id", "position", "projection_status", "created_at", "updated_at",
    },
    "knowhow_cells": {"id", "row_id", "column_id", "content_md", "updated_at"},
    "notebook_assets": {
        "id", "notebook_id", "filename", "mime", "size", "created_by", "created_at",
    },
}


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA index_list({table})").fetchall()}


def _mk_notebook(repo, nb_id="nb1"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES (?, 'n', 't', 't')",
            (nb_id,),
        )


def test_fresh_db_has_knowhow_tables(repo):
    """全新库:五表 + 其 idx_knowhow_*/idx_notebook_assets_nb 索引存在；关键列
    (role/projection_status/mutation_seq 默认值)可插查；UNIQUE(row_id,
    column_id) 生效。"""
    for table, expected_cols in KNOWHOW_TABLE_COLUMNS.items():
        assert expected_cols <= _cols(repo, table), f"{table} missing expected columns"

    assert "idx_knowhow_tables_nb" in _indexes(repo, "knowhow_tables")
    assert "idx_knowhow_columns_table" in _indexes(repo, "knowhow_columns")
    assert "idx_knowhow_rows_table" in _indexes(repo, "knowhow_rows")
    assert "idx_knowhow_cells_row" in _indexes(repo, "knowhow_cells")
    assert "idx_notebook_assets_nb" in _indexes(repo, "notebook_assets")

    _mk_notebook(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, created_at, updated_at) "
            "VALUES ('kt1', 'nb1', 'T', 't', 't')"
        )
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, position) "
            "VALUES ('kc1', 'kt1', '概念', 0)"
        )
        db.execute(
            "INSERT INTO knowhow_rows (id, table_id, position, created_at, updated_at) "
            "VALUES ('kr1', 'kt1', 0, 't', 't')"
        )
        db.execute(
            "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
            "VALUES ('kx1', 'kr1', 'kc1', 'hello', 't')"
        )
        db.execute(
            "INSERT INTO notebook_assets (id, notebook_id, filename, mime, size, created_at) "
            "VALUES ('na1', 'nb1', 'a.png', 'image/png', 10, 't')"
        )

    with repo._connect() as db:
        kt = dict(db.execute(
            "SELECT mutation_seq, hidden_source_id, created_by FROM knowhow_tables WHERE id='kt1'"
        ).fetchone())
        role = db.execute(
            "SELECT role FROM knowhow_columns WHERE id='kc1'"
        ).fetchone()["role"]
        projection_status = db.execute(
            "SELECT projection_status FROM knowhow_rows WHERE id='kr1'"
        ).fetchone()["projection_status"]
        content_md = db.execute(
            "SELECT content_md FROM knowhow_cells WHERE row_id='kr1' AND column_id='kc1'"
        ).fetchone()["content_md"]
        filename = db.execute(
            "SELECT filename FROM notebook_assets WHERE id='na1'"
        ).fetchone()["filename"]

    # 默认值:role='plain'、projection_status='pending'、mutation_seq=0。
    assert kt == {"mutation_seq": 0, "hidden_source_id": None, "created_by": ""}
    assert role == "plain"
    assert projection_status == "pending"
    assert content_md == "hello"
    assert filename == "a.png"

    with pytest.raises(sqlite3.IntegrityError):
        with repo._write() as db:
            db.execute(
                "INSERT INTO knowhow_cells (id, row_id, column_id, content_md, updated_at) "
                "VALUES ('kx2', 'kr1', 'kc1', 'dup', 't')"
            )


def test_startup_recovery_marks_orphaned_knowhow_rows_failed(tmp_path):
    """Post-restart, knowhow rows still 'syncing'/'pending' are orphaned in the
    single-process model (background_jobs don't survive a restart, and no other
    code path ever revisits them) — startup recovery (_recover_interrupted_jobs,
    re-run on every construction) must flip BOTH to 'failed' so the retry
    affordance surfaces, while leaving already-settled rows untouched."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/recover.db",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    _mk_notebook(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, created_at, updated_at) "
            "VALUES ('kt1', 'nb1', 'T', 't', 't')"
        )
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, position) "
            "VALUES ('kc1', 'kt1', '概念', 0)"
        )
        for rid, status in (
            ("r-sync", "syncing"), ("r-pend", "pending"),
            ("r-done", "synced"), ("r-fail", "failed"),
        ):
            db.execute(
                "INSERT INTO knowhow_rows (id, table_id, position, projection_status, created_at, updated_at) "
                "VALUES (?, 'kt1', 0, ?, 't', 't')",
                (rid, status),
            )

    # Reopen on the same DB — construction re-runs _recover_interrupted_jobs.
    repo2 = SQLiteRepository(settings)
    with repo2._connect() as db:
        statuses = {
            r["id"]: r["projection_status"]
            for r in db.execute("SELECT id, projection_status FROM knowhow_rows").fetchall()
        }
    assert statuses["r-sync"] == "failed"   # orphaned syncing -> failed
    assert statuses["r-pend"] == "failed"   # orphaned pending -> failed
    assert statuses["r-done"] == "synced"   # already settled -> untouched
    assert statuses["r-fail"] == "failed"   # already failed -> stays failed


def test_v15_db_upgraded_gets_knowhow_tables(repo):
    """已部署库(user_version=15,即本任务之前)重启后必须由 _migration_16 补齐
    五张 knowhow 表 + 其索引(schema-migration-convention 教训用例:回退状态 +
    版本闸 + 重跑 _migrate,参照 test_memory_kg_schema.py::
    test_deployed_v13_db_upgrades_via_migration_14 的构造方式)。"""
    # Drop children before parents: with PRAGMA foreign_keys=ON, SQLite's
    # DROP TABLE performs an implicit DELETE first, and knowhow_cells carries
    # two FK columns (row_id -> knowhow_rows, column_id -> knowhow_columns).
    # Dropping a parent while a child's FK to it is still live is fine, but
    # dropping the OTHER parent afterwards fails schema validation on the
    # child's now-dangling FK to the already-dropped table ("no such table")
    # unless the child is gone first.
    with repo._connect() as db:
        for table in (
            # knowhow_cell_code (migration 17) also FKs onto knowhow_rows AND
            # knowhow_columns, so it must drop before EITHER of those parents
            # for the same reason knowhow_cells must — see the docstring above.
            "knowhow_cell_code", "knowhow_cells", "knowhow_rows", "knowhow_columns",
            "knowhow_tables", "notebook_assets",
        ):
            db.execute(f"DROP TABLE IF EXISTS {table}")
        db.execute("PRAGMA user_version = 15")

    for table in KNOWHOW_TABLE_COLUMNS:
        assert not _cols(repo, table), f"{table} should be absent before migrating"

    applied = repo._migrate()

    assert 16 in applied
    # 版本闸落到当前 SCHEMA_VERSION(非硬编码字面量,防后续新迁移使断言假红)。
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == sr.SCHEMA_VERSION
    for table, expected_cols in KNOWHOW_TABLE_COLUMNS.items():
        assert expected_cols <= _cols(repo, table)
    assert "idx_knowhow_tables_nb" in _indexes(repo, "knowhow_tables")
    assert "idx_knowhow_columns_table" in _indexes(repo, "knowhow_columns")
    assert "idx_knowhow_rows_table" in _indexes(repo, "knowhow_rows")
    assert "idx_knowhow_cells_row" in _indexes(repo, "knowhow_cells")
    assert "idx_notebook_assets_nb" in _indexes(repo, "notebook_assets")


# ---------------------------------------------------------------------------
# Task 1 (knowhow-tables PR-2+3): migration 17 — knowhow_cell_code (格子级代码
# 附件) + knowhow_columns.role 存量词表重映射(五角色->四行为类型)。
# ---------------------------------------------------------------------------


KNOWHOW_CELL_CODE_COLUMNS = {
    "id", "row_id", "column_id", "code_text", "language", "updated_by",
    "cell_content_hash", "created_at", "updated_at",
}


def test_fresh_db_has_knowhow_cell_code_table(repo):
    """全新库:knowhow_cell_code 表 + idx_knowhow_cell_code_row 索引存在；
    language/updated_by 默认值可插查；UNIQUE(row_id, column_id) 生效。"""
    assert KNOWHOW_CELL_CODE_COLUMNS <= _cols(repo, "knowhow_cell_code")
    assert "idx_knowhow_cell_code_row" in _indexes(repo, "knowhow_cell_code")

    _mk_notebook(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, created_at, updated_at) "
            "VALUES ('kt1', 'nb1', 'T', 't', 't')"
        )
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, position) "
            "VALUES ('kc1', 'kt1', '方法', 0)"
        )
        db.execute(
            "INSERT INTO knowhow_rows (id, table_id, position, created_at, updated_at) "
            "VALUES ('kr1', 'kt1', 0, 't', 't')"
        )
        db.execute(
            "INSERT INTO knowhow_cell_code "
            "(id, row_id, column_id, code_text, cell_content_hash, created_at, updated_at) "
            "VALUES ('code1', 'kr1', 'kc1', 'print(1)', 'abc123', 't', 't')"
        )

    with repo._connect() as db:
        row = dict(db.execute(
            "SELECT language, updated_by FROM knowhow_cell_code WHERE id='code1'"
        ).fetchone())
    assert row == {"language": "", "updated_by": ""}

    with pytest.raises(sqlite3.IntegrityError):
        with repo._write() as db:
            db.execute(
                "INSERT INTO knowhow_cell_code "
                "(id, row_id, column_id, code_text, cell_content_hash, created_at, updated_at) "
                "VALUES ('code2', 'kr1', 'kc1', 'print(2)', 'def456', 't', 't')"
            )


def test_v16_db_upgraded_gets_cell_code_table_and_remaps_legacy_roles(repo):
    """已部署库(user_version=16,即本任务之前)重启后 _migration_17 必须：(a) 补建
    knowhow_cell_code 表 + 其索引；(b) 就地重映射 knowhow_columns.role 存量值——
    concept->anchor、identify/root_cause/fix->procedure(三个"步骤类"角色收敛
    为一个,原始子类型信息在这次重映射后不可逆地丢失,是设计决定的代价)、
    tool->entity、plain(ELSE 分支兜底)->attribute。"""
    _mk_notebook(repo)
    legacy_roles = {
        "c-concept": "concept", "c-identify": "identify",
        "c-root_cause": "root_cause", "c-fix": "fix",
        "c-tool": "tool", "c-plain": "plain",
    }
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, created_at, updated_at) "
            "VALUES ('kt1', 'nb1', 'T', 't', 't')"
        )
        for i, (col_id, role) in enumerate(legacy_roles.items()):
            db.execute(
                "INSERT INTO knowhow_columns (id, table_id, name, role, position) "
                "VALUES (?, 'kt1', ?, ?, ?)",
                (col_id, f"列{i}", role, i),
            )
        # Faithful "already deployed at v16" reconstruction (mirrors
        # test_v15_db_upgraded_gets_knowhow_tables above): the migration-17-only
        # table is dropped and the version pin rolled back before re-migrating.
        db.execute("DROP TABLE IF EXISTS knowhow_cell_code")
        db.execute("PRAGMA user_version = 16")

    assert not _cols(repo, "knowhow_cell_code")

    applied = repo._migrate()

    assert 17 in applied
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == sr.SCHEMA_VERSION
    assert KNOWHOW_CELL_CODE_COLUMNS <= _cols(repo, "knowhow_cell_code")
    assert "idx_knowhow_cell_code_row" in _indexes(repo, "knowhow_cell_code")

    with repo._connect() as db:
        roles = {
            r["id"]: r["role"]
            for r in db.execute(
                "SELECT id, role FROM knowhow_columns WHERE table_id='kt1'"
            ).fetchall()
        }
    assert roles == {
        "c-concept": "anchor",
        "c-identify": "procedure",
        "c-root_cause": "procedure",
        "c-fix": "procedure",
        "c-tool": "entity",
        "c-plain": "attribute",
    }
