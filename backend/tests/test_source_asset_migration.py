"""Task 2: schema migration 19 — notebook_assets.source_id link column.

MinerU-extracted embedded images (pdf/docx/pptx) need to be linked back to
their originating source so the source view can render them and source
delete/reparse can cascade-clean them; knowhow paste-in images leave
source_id NULL (no originating source — see
docs/superpowers/plans task-2-brief.md). This column + its index land here,
mirroring _migration_16/_migration_17/_migration_18's already-deployed-DB
backfill pattern (test_knowhow_schema.py / test_memory_kg_schema.py).
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SCHEMA_VERSION, SQLiteRepository


def _repo(tmp_path, name="assets.db"):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/{name}",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA index_list({table})").fetchall()}


def test_fresh_db_has_notebook_assets_source_id(tmp_path):
    """全新库:SCHEMA_VERSION 已到 26（notebook_assets.source_id 列在 v19 落地，
    此后 v20 加了 notebook_bases + promotion_candidates.target_base_id、v21 加了
    normalized-anchor expression index、v22 加了 kg_build_jobs、v23 加了模型服务
    最新状态表、v24 加了写锁瘦身改造点 2 的 kg_canonical_scratch 表、v25 清除用户
    模型凭据并改为系统服务状态、v26 加了 knowhow_changes/knowhow_milestones 两表，
    均不影响本列）；notebook_assets 带可空 source_id 列 + idx_notebook_assets_source
    索引；user_version 已盖到
    SCHEMA_VERSION。"""
    repo = _repo(tmp_path)
    assert SCHEMA_VERSION == 25
    assert "source_id" in _cols(repo, "notebook_assets")
    assert "idx_notebook_assets_source" in _indexes(repo, "notebook_assets")
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_source_id_is_nullable_and_links_to_a_source(tmp_path):
    """knowhow 粘贴图片 source_id 留 NULL；来源内嵌图片存 source_id，按其过滤
    可查到该图。"""
    repo = _repo(tmp_path)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb1', 'n', 't', 't')"
        )
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, "
            "parse_status, created_at, updated_at) "
            "VALUES ('src1', 'nb1', 'S', 'pdf', 'ready', 'ready', 't', 't')"
        )
        db.execute(
            "INSERT INTO notebook_assets "
            "(id, notebook_id, filename, mime, size, created_at, source_id) "
            "VALUES ('na-pasted', 'nb1', 'p.png', 'image/png', 1, 't', NULL)"
        )
        db.execute(
            "INSERT INTO notebook_assets "
            "(id, notebook_id, filename, mime, size, created_at, source_id) "
            "VALUES ('na-embedded', 'nb1', 'e.png', 'image/png', 2, 't', 'src1')"
        )
    with repo._connect() as db:
        pasted = db.execute(
            "SELECT source_id FROM notebook_assets WHERE id='na-pasted'"
        ).fetchone()["source_id"]
        linked = db.execute(
            "SELECT id FROM notebook_assets WHERE source_id='src1'"
        ).fetchone()["id"]
    assert pasted is None
    assert linked == "na-embedded"


def test_deployed_v18_db_backfills_source_id_column(tmp_path):
    """已部署库(user_version=18,即本迁移之前,notebook_assets 尚无 source_id)
    重启后 _migration_19 必须补齐该列 + idx_notebook_assets_source 索引——回退
    状态 + 版本闸 + 重开新 SQLiteRepository 的写法镜像
    test_rebuild_checkpoint.py::test_deployed_v9_db_gets_checkpoint_table_backfilled
    (SQLite>=3.35 支持 ALTER TABLE ... DROP COLUMN)。"""
    db_name = "deployed.db"
    repo = _repo(tmp_path, db_name)  # 先建到当前 SCHEMA_VERSION
    assert "source_id" in _cols(repo, "notebook_assets")

    with repo._connect() as db:
        db.execute("DROP INDEX idx_notebook_assets_source")
        db.execute("ALTER TABLE notebook_assets DROP COLUMN source_id")
        db.execute("PRAGMA user_version = 18")
    assert "source_id" not in _cols(repo, "notebook_assets")

    # 同库路径上构造一个全新 SQLiteRepository ——镜像"重启"：构造函数里的
    # migrator.initialize() 自动重跑迁移，不依赖复用同一个 repo 对象。
    repo2 = _repo(tmp_path, db_name)
    assert "source_id" in _cols(repo2, "notebook_assets")
    assert "idx_notebook_assets_source" in _indexes(repo2, "notebook_assets")
    with repo2._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION


def test_migration_19_is_reentrant_when_column_already_present(tmp_path):
    """守卫可重入场景二：列已经存在(非"删列回退"而是版本戳单独滞后)时再次
    触发 _migration_19 不得抛 'duplicate column name'——PRAGMA table_info 存在
    性守卫必须先查后加。"""
    db_name = "reentrant.db"
    repo = _repo(tmp_path, db_name)  # 全新库，source_id 列已随 v19 到位
    assert "source_id" in _cols(repo, "notebook_assets")

    with repo._connect() as db:
        # 只回退版本戳，不删列——模拟"迁移已实际生效但 user_version 记录滞后"
        # 的边缘场景（例如迁移中途更新 PRAGMA 前崩溃后手工回退）。
        db.execute("PRAGMA user_version = 18")

    applied = repo._migrate()  # 关键：不得抛 sqlite3.OperationalError(duplicate column name)

    assert 19 in applied
    assert "source_id" in _cols(repo, "notebook_assets")
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
