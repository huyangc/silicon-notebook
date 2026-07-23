"""Task 1: sources.memory_id link column + migration 14 + schema golden.

A Memory record confirmed by the user gets at most one derived synthetic
source (source_type='memory'), linked back via sources.memory_id. This
column plus its partial unique index land here; Task 2 wires
insert_source(memory_id=...) / source_id_for_memory into the memory-derived
source pipeline (see docs/superpowers/plans/2026-07-14-memory-kg-extraction.md).
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
            database_url=f"sqlite:///{tmp_path}/memory_kg.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def _cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _mk_notebook(repo, nb_id="nb1"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES (?, 'n', 't', 't')",
            (nb_id,),
        )


def test_fresh_db_has_sources_memory_id(repo):
    assert "memory_id" in _cols(repo, "sources")
    with repo._connect() as db:
        idx = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_sources_memory_id'"
        ).fetchone()
    assert idx is not None and "WHERE memory_id" in idx["sql"]


def test_schema_version_is_27():
    # paper-metadata Task 1's _migration_17 (source_paper_meta/source_authors
    # tables) bumped v16 → v17; knowhow-tables PR-2+3 Task 1's _migration_18
    # (knowhow_cell_code table + role vocabulary remap) bumped v17 → v18;
    # source-asset-linking Task 2's _migration_19 (notebook_assets.source_id
    # column + its index) bumped v18 → v19; multi-domain-base Task 1's
    # _migration_20 (notebook_bases table + promotion_candidates.
    # target_base_id column) bumped v19 → v20; the normalized-anchor expression
    # index bumped v20 → v21; durable KG build jobs bumped v21 → v22; model
    # service status persistence bumped v22 → v23; write-lock slimming
    # improvement point 2's kg_canonical_scratch table bumped v23 → v24;
    # system-owned model services and the irreversible credential/status scrub
    # bumped v24 → v25 (#328); knowhow table version control's _migration_26
    # (knowhow_changes/knowhow_milestones tables) bumped v25 → v26 (#327);
    # source-completion-marker P1.5's _migration_27 (sources.chunked_at column)
    # bumped v26 → v27; the per-notebook document limit's _migration_28
    # (app_settings table + user_profiles.upload_document_limit column) bumped
    # v27 → v28.
    assert sr.SCHEMA_VERSION == 28


def test_deployed_v13_db_upgrades_via_migration_14(repo):
    """已部署库(user_version 已到 13,即 Task 1 之前)重启后必须由 _migration_14
    补齐 sources.memory_id 列 + idx_sources_memory_id 分区唯一索引
    (schema-migration-convention 教训用例:回退状态 + 版本闸 + 重跑 _migrate)。"""
    with repo._connect() as db:
        db.execute("DROP INDEX idx_sources_memory_id")
        db.execute("ALTER TABLE sources DROP COLUMN memory_id")
        db.execute("PRAGMA user_version = 13")
    assert "memory_id" not in _cols(repo, "sources")

    applied = repo._migrate()

    assert 14 in applied
    # 版本闸落到当前 SCHEMA_VERSION(非硬编码字面量,防后续新迁移使断言假红)。
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == sr.SCHEMA_VERSION
        idx = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_sources_memory_id'"
        ).fetchone()
    assert "memory_id" in _cols(repo, "sources")
    assert idx is not None and "WHERE memory_id" in idx["sql"]


def _insert(repo, **overrides):
    store = repo._runtime.source_store
    fields = dict(
        source_id="src-1",
        notebook_id="nb1",
        title="T",
        source_type="pdf",
        status="ready",
        parse_status="ready",
        file_name="",
        file_path="",
        file_size=0,
        file_hash="h1",
        summary="",
        doc_type="paper",
    )
    fields.update(overrides)
    store.insert_source(**fields)
    return store


def test_insert_source_with_memory_id_is_findable_via_source_id_for_memory(repo):
    _mk_notebook(repo)
    store = _insert(
        repo,
        source_id="src-mem",
        source_type="memory",
        doc_type="memory",
        memory_id="mem-1",
    )
    assert store.source_id_for_memory("mem-1") == "src-mem"


def test_source_id_for_memory_returns_none_when_absent(repo):
    _mk_notebook(repo)
    store = _insert(repo)  # memory_id defaults to ""; ordinary (non-memory) source
    assert store.source_id_for_memory("no-such-memory") is None
    # Ordinary sources leave memory_id at its "" default; that default itself
    # must never resolve to a row (would corrupt every plain source lookup).
    assert store.source_id_for_memory("") is None


def test_partial_unique_index_caps_one_derived_source_per_memory(repo):
    _mk_notebook(repo)
    store = _insert(
        repo, source_id="src-1", source_type="memory", doc_type="memory", memory_id="mem-1"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_source(
            source_id="src-2",
            notebook_id="nb1",
            title="T2",
            source_type="memory",
            status="ready",
            parse_status="ready",
            file_name="",
            file_path="",
            file_size=0,
            file_hash="h2",
            summary="",
            doc_type="memory",
            memory_id="mem-1",
        )


def test_multiple_sources_without_memory_id_do_not_collide(repo):
    """Non-memory sources all default memory_id to "" — the partial index's
    ``!= ''`` clause must exclude them from the uniqueness constraint, or
    every second plain source insert would start raising IntegrityError."""
    _mk_notebook(repo)
    _insert(repo, source_id="src-a", file_hash="ha")
    store = _insert(repo, source_id="src-b", file_hash="hb")
    with store.database.connect() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM sources WHERE memory_id = ''"
        ).fetchone()["c"]
    assert count == 2
