# backend/tests/test_sharing_store_component.py
"""Task 9: SharingStore component — share/membership rows and the
connection-taking deep-copy primitives extracted off the facade mixin.

Row-level semantics under test (must stay identical to the former
SQLiteNotebookSharingMixin):
- set_share_token reuses an existing token atomically (candidate token only
  applies when the notebook is not already shared);
- clear_share clears token AND kicks members in ONE write transaction;
- sweep_stale_copies removes only expired 'copying' sentinels;
- snapshot/insert/compensate copy primitives keep the exact table set.
"""
import uuid

import pytest

from app.core.config import Settings
from app.repositories.sqlite.sharing_store import SharingStore
from app.services import sqlite_repository


NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return sqlite_repository.SQLiteRepository(Settings())


@pytest.fixture
def store(repo):
    return repo._runtime.sharing_store


def _seed_notebook(store, owner="user-local", name="NB"):
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with store.database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb, name, "", "Semiconductor", "draft", owner, NOW, NOW),
        )
    return nb


def _seed_user(store, uid, username=None):
    with store.database.write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id,email,display_name,username,password_hash,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, f"{uid}@t", uid, username or uid, "x", "user", NOW, NOW),
        )


def test_store_is_wired_on_runtime(repo):
    store = repo._runtime.sharing_store
    assert isinstance(store, SharingStore)
    assert store.database is repo._runtime.database
    assert store.settings is repo._runtime.settings


def test_set_share_token_reuses_existing_token(store):
    nb = _seed_notebook(store)
    assert store.set_share_token(nb, "shr-first") == "shr-first"
    assert store.find_by_token("shr-first") == nb
    # already shared -> the candidate token must NOT replace the existing one
    assert store.set_share_token(nb, "shr-second") == "shr-first"
    assert store.find_by_token("shr-second") is None
    assert store.find_by_token("shr-first") == nb


def test_clear_share_clears_token_and_kicks_members(store):
    nb = _seed_notebook(store)
    _seed_user(store, "user-bob", "b00000001")
    store.set_share_token(nb, "shr-x")
    store.add_member(nb, "user-bob")
    store.clear_share(nb)
    assert store.find_by_token("shr-x") is None
    assert store.list_members(nb) == []


def test_membership_rows_are_idempotent_and_listable(store):
    nb = _seed_notebook(store, owner="user-local")
    _seed_user(store, "user-bob", "b00000002")
    assert store.user_can_access_notebook(nb, "user-local") is True
    assert store.user_can_access_notebook(nb, "user-bob") is False
    assert store.is_member(nb, "user-bob") is False
    store.add_member(nb, "user-bob")
    assert store.is_member(nb, "user-bob") is True
    assert [m["username"] for m in store.list_members(nb)] == ["b00000002"]
    store.add_member(nb, "user-bob")  # idempotent
    assert len(store.list_members(nb)) == 1
    store.remove_member(nb, "user-bob")
    assert store.is_member(nb, "user-bob") is False
    store.add_member(nb, "user-bob")
    store.kick_all_members(nb)
    assert store.list_members(nb) == []


def test_owner_lookups_and_shared_by_owner_rows(store):
    nb = _seed_notebook(store, owner="user-local")
    with store.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("src-own", nb, "S", "document", "s.md", "", 0, NOW, NOW),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("cv-own", nb, "chat", "user-local", NOW, NOW),
        )
        db.execute(
            "INSERT INTO answers (id,notebook_id,question,payload,created_at) VALUES (?,?,?,?,?)",
            ("ans-own", nb, "q", "{}", NOW),
        )
    assert store.source_owner("src-own") == "user-local"
    assert store.conversation_owner("cv-own") == "user-local"
    assert store.answer_owner("ans-own") == "user-local"
    assert store.source_owner("missing") is None
    assert store.conversation_owner("missing") is None
    assert store.answer_owner("missing") is None
    assert store.list_shared_by_owner("user-local") == []  # not shared yet
    store.set_share_token(nb, "shr-own")
    assert [r["id"] for r in store.list_shared_by_owner("user-local")] == [nb]


def test_find_by_token_requires_shared_flag(store):
    nb = _seed_notebook(store)
    with store.database.write() as db:
        db.execute(
            "UPDATE notebooks SET share_token='shr-latent', is_shared=0 WHERE id=?",
            (nb,),
        )
    assert store.find_by_token("shr-latent") is None


def test_sweep_stale_copies_only_removes_expired(store):
    from datetime import datetime

    current = _seed_notebook(store)
    expired = _seed_notebook(store)
    fresh = datetime.now().replace(microsecond=0).isoformat()
    with store.database.write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying', created_at=? WHERE id=?",
            (fresh, current),
        )
        db.execute(
            "UPDATE notebooks SET status='copying', created_at='2000-01-01T00:00:00' WHERE id=?",
            (expired,),
        )
    assert store.sweep_stale_copies(created_by="user-local") == 1
    with store.database.connect() as db:
        assert db.execute("SELECT 1 FROM notebooks WHERE id=?", (current,)).fetchone()
        assert db.execute("SELECT 1 FROM notebooks WHERE id=?", (expired,)).fetchone() is None


def test_snapshot_insert_and_compensate_primitives(store):
    src = _seed_notebook(store)
    with store.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("src-snap", src, "S", "document", "s.md", "", 1, NOW, NOW),
        )
    snapshot = store.snapshot_copy_rows(src)
    assert [row["id"] for row in snapshot["notebooks"]] == [src]
    assert [row["id"] for row in snapshot["sources"]] == ["src-snap"]
    assert set(snapshot) == {
        "notebooks", "sources", "source_paper_meta", "source_authors",
        "source_elements", "chunks",
        "knowledge_objects", "knowledge_relations", "chunk_embeddings",
        "element_embeddings", "knowledge_embeddings", "relation_embeddings",
        "concept_clusters",
        # PR-2+3 Task 13: knowhow business tables + notebook_assets now
        # travel with a copy (see sharing_store._COPY_SNAPSHOT_QUERIES).
        "knowhow_tables", "knowhow_columns", "knowhow_rows", "knowhow_cells",
        "knowhow_cell_code", "notebook_assets",
    }
    destination = dict(snapshot["notebooks"][0])
    destination.update(id="nb-copy-dest", status="copying")
    store.insert_copy_rows("notebooks", [destination], chunk_size=1000)
    with store.database.connect() as db:
        assert db.execute(
            "SELECT status FROM notebooks WHERE id='nb-copy-dest'"
        ).fetchone()[0] == "copying"
    store.compensate_copy("nb-copy-dest")
    with store.database.connect() as db:
        assert db.execute("SELECT 1 FROM notebooks WHERE id='nb-copy-dest'").fetchone() is None
        assert db.execute("SELECT 1 FROM notebooks WHERE id=?", (src,)).fetchone()
