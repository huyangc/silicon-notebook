"""
Tests for resolve_notebook_conflicts (Task 5 — orchestration).

Uses a real temp-DB SQLiteRepository with FakeEmbedder + an injected
FakeLLM to avoid any real model calls.

Seeds a notebook with two Claim objects and two contradicting relations,
then exercises:
  - candidates detected and queue rows written
  - high-confidence discard verdict → DB mutated AND candidate status='applied'
  - low-confidence verdict → DB unchanged AND candidate status='pending'
  - llm not configured → skipped summary, no crash
  - build_notebook_kg only calls resolve when kg_conflict_resolution_enabled=True
"""
from __future__ import annotations

import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


# ---------------------------------------------------------------------------
# Fake LLM helpers
# ---------------------------------------------------------------------------

class FakeLLM:
    """Injects canned JSON verdicts. `configured` defaults to True."""

    def __init__(self, verdicts: list[dict], configured: bool = True):
        self._verdicts = list(verdicts)
        self._cursor = 0
        self.configured = configured
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, schema_hint=None) -> str:
        self.calls.append(messages)
        if self._cursor >= len(self._verdicts):
            # Default: no conflict (safe fallback)
            return json.dumps({
                "conflict_type": "none",
                "resolution": "keep",
                "winner_ref": None,
                "resolved_payload": None,
                "confidence": 0.0,
                "rationale": "no conflict",
            })
        v = self._verdicts[self._cursor]
        self._cursor += 1
        return json.dumps(v)


class UnconfiguredLLM:
    configured = False

    def chat_json(self, messages, schema_hint=None) -> str:
        raise AssertionError("should not be called when unconfigured")


# ---------------------------------------------------------------------------
# Repository fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------

def _seed_notebook_with_contradicting_claims(repo: SQLiteRepository):
    """Create a notebook with two Claims connected by contradictory relations.

    The shared_pair_diff_edge strategy will fire because the same (A, B) pair
    is connected by two different edge_types (supports and contradicts).
    Returns (notebook_id, oid_a, oid_b, rel_a_id, rel_b_id).
    """
    nb = repo.create_notebook(NotebookCreate(name="conflict-test"))
    repo.store_kg(nb.id, None, [
        {
            "local_id": "A",
            "object_type": "claim",
            "payload": {"name": "Claim A: NMOS has high threshold"},
            "evidence": [{"quoted_span": "NMOS threshold is high.", "element_id": ""}],
        },
        {
            "local_id": "B",
            "object_type": "claim",
            "payload": {"name": "Claim B: PMOS has low threshold"},
            "evidence": [{"quoted_span": "PMOS threshold is low.", "element_id": ""}],
        },
    ], [
        {
            "source_local_id": "A",
            "target_local_id": "B",
            "edge_type": "supports",
            "evidence": [{"quoted_span": "NMOS supports PMOS design.", "element_id": ""}],
        },
        {
            "source_local_id": "A",
            "target_local_id": "B",
            "edge_type": "contradicts",
            "evidence": [{"quoted_span": "NMOS contradicts PMOS claim.", "element_id": ""}],
        },
    ])
    # Fetch back real DB ids
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY created_at, id",
            (nb.id,),
        ).fetchall()
        rels = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=? ORDER BY rowid",
            (nb.id,),
        ).fetchall()
    oid_a, oid_b = objs[0]["id"], objs[1]["id"]
    rel_a, rel_b = rels[0]["id"], rels[1]["id"]
    return nb.id, oid_a, oid_b, rel_a, rel_b


# ---------------------------------------------------------------------------
# Test: llm not configured → skipped, no crash
# ---------------------------------------------------------------------------

def test_llm_not_configured_returns_skipped_summary(repo):
    repo.llm_client = UnconfiguredLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb-no-llm"))

    result = repo.resolve_notebook_conflicts(nb.id)

    assert result["skipped_llm"] is True
    assert result["detected"] == 0
    assert result["auto_applied"] == 0
    assert result["queued"] == 0


# ---------------------------------------------------------------------------
# Test: candidates detected and queue rows written
# ---------------------------------------------------------------------------

def test_candidates_detected_and_queued(repo):
    """At least one candidate is detected for the contradicting-edge seed."""
    nb_id, oid_a, oid_b, rel_a, rel_b = _seed_notebook_with_contradicting_claims(repo)

    # FakeLLM with no-conflict verdict so nothing is auto-applied
    repo.llm_client = FakeLLM([])  # defaults to "none/keep/0.0" for every call

    result = repo.resolve_notebook_conflicts(nb_id)

    assert result["skipped_llm"] is False
    assert result["detected"] >= 1
    # All should land as pending (no-conflict verdict)
    assert result["queued"] >= 1
    assert result["auto_applied"] == 0

    pending = repo.pending_conflicts(nb_id)
    assert len(pending) >= 1


# ---------------------------------------------------------------------------
# Test: high-confidence discard verdict → DB mutated AND status='applied'
# ---------------------------------------------------------------------------

def test_high_confidence_discard_applies_and_marks_applied(repo):
    nb_id, oid_a, oid_b, rel_a, rel_b = _seed_notebook_with_contradicting_claims(repo)

    relations = repo.relations_for_notebook(nb_id)
    # Find the expected pair — shared_pair_diff_edge between rel_a and rel_b
    # Canonical ordering: smaller id goes left
    left_ref, right_ref = (rel_a, rel_b) if rel_a < rel_b else (rel_b, rel_a)
    winner_ref = left_ref  # keep the left one, discard right

    high_conf_verdict = {
        "conflict_type": "mutual",
        "resolution": "discard",
        "winner_ref": winner_ref,
        "resolved_payload": None,
        "confidence": 0.98,
        "rationale": "These edges genuinely contradict each other.",
    }

    repo.llm_client = FakeLLM([high_conf_verdict])
    # Set threshold below 0.98 so it auto-applies
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    result = repo.resolve_notebook_conflicts(nb_id)

    assert result["auto_applied"] >= 1

    # The loser edge should be 'rejected'
    loser_ref = right_ref  # the one not kept
    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?",
            (loser_ref,),
        ).fetchone()
    assert row is not None
    assert row["review_status"] == "rejected", (
        f"Expected loser edge {loser_ref!r} to be rejected, got {row['review_status']!r}"
    )

    # The candidate row should be 'applied'
    pending = repo.pending_conflicts(nb_id)
    # There may be other candidates; check that at least one is applied
    all_cands = repo.pending_conflicts(nb_id)
    # Candidates for this pair should not be pending (they got applied)
    # Verify by checking the candidates table directly
    with repo._connect() as db:
        rows = db.execute(
            "SELECT status FROM kg_conflict_candidates WHERE notebook_id=? AND left_ref=? AND right_ref=?",
            (nb_id, left_ref, right_ref),
        ).fetchall()
    assert any(r["status"] == "applied" for r in rows), (
        f"Expected at least one candidate row to be 'applied', got: {[r['status'] for r in rows]}"
    )


# ---------------------------------------------------------------------------
# Test: low-confidence verdict → DB unchanged AND status='pending'
# ---------------------------------------------------------------------------

def test_low_confidence_leaves_db_unchanged_and_pending(repo):
    nb_id, oid_a, oid_b, rel_a, rel_b = _seed_notebook_with_contradicting_claims(repo)

    left_ref, right_ref = (rel_a, rel_b) if rel_a < rel_b else (rel_b, rel_a)
    winner_ref = left_ref

    low_conf_verdict = {
        "conflict_type": "mutual",
        "resolution": "discard",
        "winner_ref": winner_ref,
        "resolved_payload": None,
        "confidence": 0.60,  # below the 0.95 threshold
        "rationale": "Possibly conflicting but uncertain.",
    }

    repo.llm_client = FakeLLM([low_conf_verdict])
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    # Snapshot edge statuses before
    with repo._connect() as db:
        before = {
            r["id"]: r["review_status"]
            for r in db.execute(
                "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                (nb_id,),
            ).fetchall()
        }

    result = repo.resolve_notebook_conflicts(nb_id)

    # Should NOT auto-apply
    assert result["auto_applied"] == 0

    # Edge statuses should be unchanged
    with repo._connect() as db:
        after = {
            r["id"]: r["review_status"]
            for r in db.execute(
                "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                (nb_id,),
            ).fetchall()
        }
    assert before == after, f"Edge statuses changed unexpectedly: {before} → {after}"

    # The candidate row should remain 'pending'
    with repo._connect() as db:
        rows = db.execute(
            "SELECT status FROM kg_conflict_candidates WHERE notebook_id=? AND left_ref=? AND right_ref=?",
            (nb_id, left_ref, right_ref),
        ).fetchall()
    assert all(r["status"] == "pending" for r in rows), (
        f"Expected all candidate rows to be 'pending', got: {[r['status'] for r in rows]}"
    )


# ---------------------------------------------------------------------------
# Test: build_notebook_kg calls resolve when enabled, skips when disabled
# ---------------------------------------------------------------------------

def test_build_notebook_kg_calls_resolve_when_enabled(repo, monkeypatch):
    """resolve_notebook_conflicts is called when kg_conflict_resolution_enabled=True."""
    nb_id, *_ = _seed_notebook_with_contradicting_claims(repo)
    calls: list[str] = []

    original = repo.resolve_notebook_conflicts

    def spy(notebook_id):
        calls.append(notebook_id)
        return original(notebook_id)

    monkeypatch.setattr(repo, "resolve_notebook_conflicts", spy)
    repo.llm_client = FakeLLM([])  # no-op verdicts
    repo.settings.kg_conflict_resolution_enabled = True
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    # build_notebook_kg requires a real LLM, so we monkeypatch _run_extraction too
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: None)
    # Also ensure llm_client.configured is True (needed by build_notebook_kg guard)
    repo.llm_client.configured = True

    repo.build_notebook_kg(nb_id)

    assert nb_id in calls, "resolve_notebook_conflicts was not called"


def test_build_notebook_kg_skips_resolve_when_disabled(repo, monkeypatch):
    """resolve_notebook_conflicts is NOT called when kg_conflict_resolution_enabled=False."""
    nb_id, *_ = _seed_notebook_with_contradicting_claims(repo)
    calls: list[str] = []

    def spy(notebook_id):
        calls.append(notebook_id)

    monkeypatch.setattr(repo, "resolve_notebook_conflicts", spy)
    repo.llm_client = FakeLLM([])
    repo.llm_client.configured = True
    repo.settings.kg_conflict_resolution_enabled = False

    monkeypatch.setattr(repo, "_run_extraction", lambda sid: None)

    repo.build_notebook_kg(nb_id)

    assert calls == [], "resolve_notebook_conflicts should not have been called"


# ---------------------------------------------------------------------------
# Test: resolution=keep (conflict_type=none) → not auto-applied, remains pending
# ---------------------------------------------------------------------------

def test_conflict_type_none_stays_pending(repo):
    nb_id, oid_a, oid_b, rel_a, rel_b = _seed_notebook_with_contradicting_claims(repo)

    none_verdict = {
        "conflict_type": "none",
        "resolution": "keep",
        "winner_ref": None,
        "resolved_payload": None,
        "confidence": 0.99,  # high confidence but conflict_type=none → not auto-applied
        "rationale": "These are corroborating, not conflicting.",
    }

    repo.llm_client = FakeLLM([none_verdict])
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    result = repo.resolve_notebook_conflicts(nb_id)

    # conflict_type="none" means no auto-apply regardless of confidence
    assert result["auto_applied"] == 0


# ---------------------------------------------------------------------------
# Test: summary dict has all expected keys
# ---------------------------------------------------------------------------

def test_summary_dict_has_expected_keys(repo):
    nb = repo.create_notebook(NotebookCreate(name="summary-keys"))
    repo.llm_client = FakeLLM([])

    result = repo.resolve_notebook_conflicts(nb.id)

    assert "detected" in result
    assert "auto_applied" in result
    assert "queued" in result
    assert "skipped_llm" in result
