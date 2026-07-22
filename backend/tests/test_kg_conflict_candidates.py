"""
Tests for kg_conflict_candidates storage layer (Task 1 — CRUD only).
Mirrors the test_cluster_and_candidate_crud / test_set_merge_decision_rejects_bad_status
pattern from test_unified_kg_repository.py.
"""
import json
import pytest
from datetime import datetime
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))               # inject; no real model loads (lazy)
    return r


# ---------------------------------------------------------------------------
# write_conflict_candidate + pending_conflicts
# ---------------------------------------------------------------------------

def test_write_and_pending_returns_candidate(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(
        notebook_id=nb.id,
        kind="node",
        left_ref="obj-aaa",
        right_ref="obj-bbb",
        conflict_type="mutual",
    )
    pending = repo.pending_conflicts(nb.id)
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == cid
    assert row["notebook_id"] == nb.id
    assert row["kind"] == "node"
    assert row["left_ref"] == "obj-aaa"
    assert row["right_ref"] == "obj-bbb"
    assert row["conflict_type"] == "mutual"
    assert row["status"] == "pending"


def test_pending_conflicts_only_returns_pending(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid1 = repo.write_conflict_candidate(nb.id, "node", "A", "B", conflict_type="temporal")
    cid2 = repo.write_conflict_candidate(nb.id, "edge", "r1", "r2", conflict_type="granularity")
    # apply one
    repo.set_conflict_status(nb.id, cid1, "applied")
    pending = repo.pending_conflicts(nb.id)
    assert len(pending) == 1
    assert pending[0]["id"] == cid2


def test_pending_conflicts_empty_when_none(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.pending_conflicts(nb.id) == []


# ---------------------------------------------------------------------------
# set_conflict_status
# ---------------------------------------------------------------------------

def test_set_conflict_status_applied(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(nb.id, "node", "X", "Y")
    repo.set_conflict_status(nb.id, cid, "applied")
    assert repo.pending_conflicts(nb.id) == []
    row = repo.get_conflict_candidate(nb.id, cid)
    assert row["status"] == "applied"


def test_set_conflict_status_rejected(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(nb.id, "node", "X", "Y")
    repo.set_conflict_status(nb.id, cid, "rejected")
    assert repo.pending_conflicts(nb.id) == []
    row = repo.get_conflict_candidate(nb.id, cid)
    assert row["status"] == "rejected"


def test_set_conflict_status_rejects_bad_value(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(nb.id, "node", "X", "Y")
    with pytest.raises(ValueError):
        repo.set_conflict_status(nb.id, cid, "maybe")


def test_set_conflict_status_rejects_pending(repo):
    """'pending' is the initial state; it is not a valid transition target."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(nb.id, "node", "X", "Y")
    with pytest.raises(ValueError):
        repo.set_conflict_status(nb.id, cid, "pending")


# ---------------------------------------------------------------------------
# get_conflict_candidate — field round-trip incl. nullable JSON
# ---------------------------------------------------------------------------

def test_get_conflict_candidate_full_fields(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    payload = {"merged_relation": "causes", "note": "keep left"}
    cid = repo.write_conflict_candidate(
        notebook_id=nb.id,
        kind="edge",
        left_ref="rel-001",
        right_ref="rel-002",
        conflict_type="granularity",
        resolution="modify",
        winner_ref=None,
        resolved_payload=json.dumps(payload),
        confidence=0.87,
        rationale="left is more specific",
    )
    row = repo.get_conflict_candidate(nb.id, cid)
    assert row["kind"] == "edge"
    assert row["left_ref"] == "rel-001"
    assert row["right_ref"] == "rel-002"
    assert row["conflict_type"] == "granularity"
    assert row["resolution"] == "modify"
    assert row["winner_ref"] is None
    assert json.loads(row["resolved_payload"]) == payload
    assert abs(row["confidence"] - 0.87) < 1e-6
    assert row["rationale"] == "left is more specific"
    assert row["status"] == "pending"
    datetime.fromisoformat(row["created_at"])   # validates ISO format produced by _now()
    datetime.fromisoformat(row["updated_at"])


def test_get_conflict_candidate_nullable_fields_default_none(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cid = repo.write_conflict_candidate(nb.id, "node", "A", "B")
    row = repo.get_conflict_candidate(nb.id, cid)
    assert row["conflict_type"] is None
    assert row["resolution"] is None
    assert row["winner_ref"] is None
    assert row["resolved_payload"] is None
    assert row["confidence"] is None
    assert row["rationale"] is None


def test_get_conflict_candidate_returns_none_for_unknown_id(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.get_conflict_candidate(nb.id, "does-not-exist") is None


# ---------------------------------------------------------------------------
# index / isolation: pending_conflicts is per-notebook
# ---------------------------------------------------------------------------

def test_pending_conflicts_isolated_per_notebook(repo):
    nb1 = repo.create_notebook(NotebookCreate(name="nb1"))
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    repo.write_conflict_candidate(nb1.id, "node", "A", "B")
    repo.write_conflict_candidate(nb2.id, "node", "C", "D")
    assert len(repo.pending_conflicts(nb1.id)) == 1
    assert len(repo.pending_conflicts(nb2.id)) == 1
    assert repo.pending_conflicts(nb1.id)[0]["left_ref"] == "A"
    assert repo.pending_conflicts(nb2.id)[0]["left_ref"] == "C"


# ---------------------------------------------------------------------------
# not-found guards (review issues #1 and #3)
# ---------------------------------------------------------------------------

def test_set_conflict_status_raises_keyerror_for_missing_id(repo):
    """set_conflict_status must raise KeyError (not silently no-op) on unknown id."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(KeyError):
        repo.set_conflict_status(nb.id, "nonexistent", "applied")


def test_pending_conflicts_raises_for_nonexistent_notebook(repo):
    """pending_conflicts must raise KeyError when the notebook does not exist."""
    with pytest.raises(KeyError):
        repo.pending_conflicts("nonexistent-notebook-id")
