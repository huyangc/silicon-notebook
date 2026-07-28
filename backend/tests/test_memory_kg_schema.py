"""Task 1: sources.memory_id link column + migration 14 + schema golden.

A Memory record confirmed by the user gets at most one derived synthetic
source (source_type='memory'), linked back via sources.memory_id. This
column plus its partial unique index land here; Task 2 wires
insert_source(memory_id=...) / source_id_for_memory into the memory-derived
source pipeline (see docs/superpowers/plans/2026-07-14-memory-kg-extraction.md).
"""
from __future__ import annotations


import pytest

from app.core.config import Settings
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
