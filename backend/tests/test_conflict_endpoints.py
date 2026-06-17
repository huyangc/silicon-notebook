"""Tests for KG conflict resolve/review API endpoints (Task 6).

Mirrors test_url_sources_api.py conventions:
- FastAPI TestClient with monkeypatched repository() → direct repo instance
- Real temp-DB SQLiteRepository with FakeEmbedder injected
- Tests: resolve (409/200/404), pending (list/404), confirm (apply/409/404), reject (reject/404)
"""
from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


# ---------------------------------------------------------------------------
# Fake LLM helpers
# ---------------------------------------------------------------------------

class _ConfiguredLLM:
    configured = True

    def chat_json(self, messages, schema_hint=None) -> str:
        return json.dumps({
            "conflict_type": "none",
            "resolution": "keep",
            "winner_ref": None,
            "resolved_payload": None,
            "confidence": 0.0,
            "rationale": "no conflict",
        })


class _UnconfiguredLLM:
    configured = False

    def chat_json(self, messages, schema_hint=None) -> str:
        raise AssertionError("should not be called")


# ---------------------------------------------------------------------------
# Shared setup helpers (mirrors test_url_sources_api.py)
# ---------------------------------------------------------------------------

def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")


def _make_repo(monkeypatch) -> SQLiteRepository:
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _client(repo: SQLiteRepository, monkeypatch) -> TestClient:
    import app.api.routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Seeding helper — produce a pending conflict candidate
# ---------------------------------------------------------------------------

def _seed_pending_conflict(repo: SQLiteRepository) -> tuple[str, str]:
    """Create a notebook + one pending conflict candidate (edge/discard).

    Returns (notebook_id, candidate_id).
    """
    nb = repo.create_notebook(NotebookCreate(name="conflict-nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim A"},
         "evidence": [{"quoted_span": "A.", "element_id": ""}]},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim B"},
         "evidence": [{"quoted_span": "B.", "element_id": ""}]},
    ], [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "supports",
         "evidence": [{"quoted_span": "A supports B.", "element_id": ""}]},
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "contradicts",
         "evidence": [{"quoted_span": "A contradicts B.", "element_id": ""}]},
    ])
    with repo._connect() as db:
        rels = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=? ORDER BY rowid",
            (nb.id,),
        ).fetchall()
    left_ref, right_ref = rels[0]["id"], rels[1]["id"]
    cid = repo.write_conflict_candidate(
        nb.id,
        kind="edge",
        left_ref=left_ref,
        right_ref=right_ref,
        conflict_type="mutual",
        resolution="discard",
        winner_ref=left_ref,
        resolved_payload=None,
        confidence=0.60,
        rationale="manually seeded",
    )
    return nb.id, cid


# ---------------------------------------------------------------------------
# POST /api/notebooks/{notebook_id}/kg/conflicts/resolve
# ---------------------------------------------------------------------------

class TestResolveEndpoint:
    def test_409_when_llm_unconfigured(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        repo.llm_client = _UnconfiguredLLM()
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="no-llm"))
        r = tc.post(f"/api/notebooks/{nb.id}/kg/conflicts/resolve")
        assert r.status_code == 409

    def test_404_unknown_notebook(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        repo.llm_client = _ConfiguredLLM()
        tc = _client(repo, monkeypatch)
        r = tc.post("/api/notebooks/nonexistent-nb/kg/conflicts/resolve")
        assert r.status_code == 404

    def test_200_returns_resolving_status(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        repo.llm_client = _ConfiguredLLM()
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="resolve-nb"))
        r = tc.post(f"/api/notebooks/{nb.id}/kg/conflicts/resolve")
        assert r.status_code == 200
        assert r.json().get("status") == "resolving"

    def test_returns_promptly_nonblocking(self, tmp_path, monkeypatch):
        """Background thread must not block the HTTP response (< 5 s)."""
        import time
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        repo.llm_client = _ConfiguredLLM()
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="timing-nb"))
        start = time.monotonic()
        r = tc.post(f"/api/notebooks/{nb.id}/kg/conflicts/resolve")
        elapsed = time.monotonic() - start
        assert r.status_code == 200
        assert elapsed < 5.0


# ---------------------------------------------------------------------------
# GET /api/notebooks/{notebook_id}/kg/conflicts/pending
# ---------------------------------------------------------------------------

class TestPendingEndpoint:
    def test_empty_when_no_conflicts(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="empty-nb"))
        r = tc.get(f"/api/notebooks/{nb.id}/kg/conflicts/pending")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_seeded_candidates(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        r = tc.get(f"/api/notebooks/{nb_id}/kg/conflicts/pending")
        assert r.status_code == 200
        items = r.json()
        assert len(items) >= 1
        assert cid in [item["id"] for item in items]

    def test_404_unknown_notebook(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        r = tc.get("/api/notebooks/bogus-nb/kg/conflicts/pending")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/confirm
# ---------------------------------------------------------------------------

class TestConfirmEndpoint:
    def test_applies_and_returns_summary(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        r = tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")
        assert r.status_code == 200
        body = r.json()
        # Summary should have at least one of: status, action, ok
        assert "status" in body or "action" in body or "ok" in body

    def test_marks_candidate_applied(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")
        with repo._connect() as db:
            row = db.execute(
                "SELECT status FROM kg_conflict_candidates WHERE id=?", (cid,)
            ).fetchone()
        assert row is not None
        assert row["status"] == "applied"

    def test_loser_edge_gets_rejected(self, tmp_path, monkeypatch):
        """edge/discard confirm → loser edge review_status='rejected'."""
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        cand = repo.get_conflict_candidate(cid)
        winner_ref = cand["winner_ref"]
        loser_ref = cand["right_ref"] if winner_ref == cand["left_ref"] else cand["left_ref"]

        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")

        with repo._connect() as db:
            row = db.execute(
                "SELECT review_status FROM knowledge_relations WHERE id=?", (loser_ref,)
            ).fetchone()
        assert row is not None
        assert row["review_status"] == "rejected"

    def test_409_when_already_applied(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")
        r = tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")
        assert r.status_code == 409

    def test_409_when_already_rejected(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        r = tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/confirm")
        assert r.status_code == 409

    def test_404_unknown_candidate(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="c-nb"))
        r = tc.post(f"/api/notebooks/{nb.id}/kg/conflicts/kcc-nonexistent/confirm")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/reject
# ---------------------------------------------------------------------------

class TestRejectEndpoint:
    def test_returns_rejected_status(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        r = tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        assert r.status_code == 200
        assert r.json().get("status") == "rejected"

    def test_marks_candidate_rejected(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        with repo._connect() as db:
            row = db.execute(
                "SELECT status FROM kg_conflict_candidates WHERE id=?", (cid,)
            ).fetchone()
        assert row is not None
        assert row["status"] == "rejected"

    def test_does_not_mutate_edges(self, tmp_path, monkeypatch):
        """reject must not modify any knowledge_relations rows."""
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        with repo._connect() as db:
            before = {
                r["id"]: r["review_status"]
                for r in db.execute(
                    "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                    (nb_id,),
                ).fetchall()
            }
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        with repo._connect() as db:
            after = {
                r["id"]: r["review_status"]
                for r in db.execute(
                    "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                    (nb_id,),
                ).fetchall()
            }
        assert before == after, f"Edges changed after reject: {before} → {after}"

    def test_409_when_already_rejected(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb_id, cid = _seed_pending_conflict(repo)
        tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        r = tc.post(f"/api/notebooks/{nb_id}/kg/conflicts/{cid}/reject")
        assert r.status_code == 409

    def test_404_unknown_candidate(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch)
        repo = _make_repo(monkeypatch)
        tc = _client(repo, monkeypatch)
        nb = repo.create_notebook(NotebookCreate(name="r-nb"))
        r = tc.post(f"/api/notebooks/{nb.id}/kg/conflicts/kcc-nonexistent/reject")
        assert r.status_code == 404
