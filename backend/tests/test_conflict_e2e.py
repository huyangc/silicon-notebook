"""
End-to-end tests for KG conflict resolution wired through build_notebook_kg (Task T8).

Strategy
--------
To drive the real build_notebook_kg path while skipping actual extraction:
  1. Insert a source row via the same raw-SQL helper used in test_p4_kg_shrink.py.
  2. Seed KG with that *same* source_id (non-empty) via store_kg — so the source
     lands in kgful and build_notebook_kg marks it "skipped" (no _run_extraction call).
  3. Seed a clear edge conflict on top of that KG.
  4. Call build_notebook_kg with kg_conflict_resolution_enabled=True / False.

This lets us exercise the real pipeline tail — the conflict hook that fires after the
extraction loop — without any LLM calls for parsing or embedding.
"""
from __future__ import annotations

import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_p4_kg_shrink.py)
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


def _make_source(repo: SQLiteRepository, nb_id: str, sid: str) -> str:
    """Insert a bare source row (already-extracted, so build skips it)."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,'markdown','extracted','parsed','a.md','',0,'','','academic_paper',?,?)",
            (sid, nb_id, "Doc", _now(), _now()),
        )
    return sid


class RefAwareFakeLLM:
    """FakeLLM that parses the prompt for 'Left ref:' / 'Right ref:' lines and
    returns a high-confidence discard verdict, always keeping the left ref.

    Replicates the RefAwareFakeLLM from test_resolve_notebook_conflicts.py so
    this file has no cross-file import dependency.
    """

    configured = True

    def __init__(self, confidence: float = 0.98):
        self._confidence = confidence
        self.calls: list[list[dict]] = []

    def chat_json(self, messages, schema_hint=None) -> str:
        import re
        self.calls.append(messages)
        prompt = messages[0]["content"] if messages else ""
        left_ref = right_ref = None
        for line in prompt.splitlines():
            m = re.match(r"Left ref:\s+(\S+)", line)
            if m:
                left_ref = m.group(1)
            m = re.match(r"Right ref:\s+(\S+)", line)
            if m:
                right_ref = m.group(1)
        return json.dumps({
            "conflict_type": "mutual",
            "resolution": "discard",
            "winner_ref": left_ref,
            "resolved_payload": None,
            "confidence": self._confidence,
            "rationale": "e2e test verdict — keep left, discard right",
        })


def _seed_conflict(repo: SQLiteRepository, nb_id: str, source_id: str):
    """Seed two claims + a contradicting edge pair under the given source_id.

    Using a real source_id (non-empty) means build_notebook_kg will recognise
    this source as 'already has KG' and skip extraction — letting the conflict
    hook fire without any LLM parse/extract calls.

    Returns (oid_a, oid_b, rel_a_id, rel_b_id).
    """
    repo.store_kg(nb_id, source_id, [
        {
            "local_id": "A",
            "object_type": "claim",
            "payload": {"name": "Claim A: voltage rises"},
            "evidence": [{"quoted_span": "Voltage rises.", "element_id": ""}],
        },
        {
            "local_id": "B",
            "object_type": "claim",
            "payload": {"name": "Claim B: current stays constant"},
            "evidence": [{"quoted_span": "Current stays constant.", "element_id": ""}],
        },
    ], [
        {
            "source_local_id": "A",
            "target_local_id": "B",
            "edge_type": "supports",
            "evidence": [{"quote": "Voltage supports current stability."}],
        },
        {
            "source_local_id": "A",
            "target_local_id": "B",
            "edge_type": "contradicts",
            "evidence": [{"quote": "Voltage contradicts current stability."}],
        },
    ])
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? ORDER BY created_at, id",
            (nb_id,),
        ).fetchall()
        rels = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=? ORDER BY rowid",
            (nb_id,),
        ).fetchall()
    oid_a, oid_b = objs[0]["id"], objs[1]["id"]
    rel_a, rel_b = rels[0]["id"], rels[1]["id"]
    return oid_a, oid_b, rel_a, rel_b


# ---------------------------------------------------------------------------
# E2E positive test: conflict resolved THROUGH build_notebook_kg
# ---------------------------------------------------------------------------

def test_e2e_conflict_resolved_via_build_hook(repo):
    """Full pipeline: build_notebook_kg → conflict hook → auto-apply → graph mutated.

    The source already has KG, so build skips extraction and goes straight to the
    conflict resolution pass at the end.  A high-confidence discard verdict marks
    the loser edge 'rejected' and the candidate row 'applied'.
    """
    nb = repo.create_notebook(NotebookCreate(name="e2e-conflict"))
    sid = _make_source(repo, nb.id, "src-e2e-01")

    oid_a, oid_b, rel_a, rel_b = _seed_conflict(repo, nb.id, sid)

    # Wire up a RefAware fake LLM that always issues a high-confidence discard
    fake_llm = RefAwareFakeLLM(confidence=0.98)
    repo.llm_client = fake_llm
    repo.settings.kg_conflict_resolution_enabled = True
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    result = repo.build_notebook_kg(nb.id)

    # Source was already kgful → skipped (no extraction)
    assert sid in result["skipped"], (
        f"Expected source {sid!r} to be skipped (already has KG); got {result}"
    )
    assert result["built"] == [], f"Expected no new builds; got {result}"

    # LLM should have been called (at least one conflict adjudicated)
    assert len(fake_llm.calls) >= 1, "RefAwareFakeLLM was never called — conflict hook did not fire"

    # Canonical ordering: smaller id goes left; FakeLLM keeps left, discards right
    left_ref, right_ref = (rel_a, rel_b) if rel_a < rel_b else (rel_b, rel_a)
    loser_ref = right_ref

    # Loser edge must be 'rejected'
    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?",
            (loser_ref,),
        ).fetchone()
    assert row is not None, f"Loser edge {loser_ref!r} not found in DB"
    assert row["review_status"] == "rejected", (
        f"Expected loser edge {loser_ref!r} review_status='rejected', got {row['review_status']!r}"
    )

    # Candidate row must be 'applied'
    with repo._connect() as db:
        rows = db.execute(
            "SELECT status FROM kg_conflict_candidates "
            "WHERE notebook_id=? AND left_ref=? AND right_ref=?",
            (nb.id, left_ref, right_ref),
        ).fetchall()
    assert any(r["status"] == "applied" for r in rows), (
        f"Expected at least one candidate row to be 'applied', got {[r['status'] for r in rows]}"
    )


# ---------------------------------------------------------------------------
# E2E negative test: flag off → graph unmutated, no applied candidates
# ---------------------------------------------------------------------------

def test_e2e_conflict_skipped_when_flag_disabled(repo):
    """When kg_conflict_resolution_enabled=False, build leaves the graph untouched."""
    nb = repo.create_notebook(NotebookCreate(name="e2e-no-conflict"))
    sid = _make_source(repo, nb.id, "src-e2e-02")

    oid_a, oid_b, rel_a, rel_b = _seed_conflict(repo, nb.id, sid)

    fake_llm = RefAwareFakeLLM(confidence=0.98)
    repo.llm_client = fake_llm
    repo.settings.kg_conflict_resolution_enabled = False
    repo.settings.kg_conflict_auto_apply_threshold = 0.95

    # Snapshot edge statuses before build
    with repo._connect() as db:
        before = {
            r["id"]: r["review_status"]
            for r in db.execute(
                "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                (nb.id,),
            ).fetchall()
        }

    result = repo.build_notebook_kg(nb.id)

    # LLM must NOT have been called (conflict hook never ran)
    assert fake_llm.calls == [], (
        "RefAwareFakeLLM was called even though kg_conflict_resolution_enabled=False"
    )

    # Edge statuses must be unchanged
    with repo._connect() as db:
        after = {
            r["id"]: r["review_status"]
            for r in db.execute(
                "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
                (nb.id,),
            ).fetchall()
        }
    assert before == after, f"Edge statuses changed unexpectedly: {before} → {after}"

    # No applied candidates
    with repo._connect() as db:
        applied = db.execute(
            "SELECT COUNT(*) AS n FROM kg_conflict_candidates WHERE notebook_id=? AND status='applied'",
            (nb.id,),
        ).fetchone()
    assert applied["n"] == 0, f"Found applied candidates even with flag disabled: {applied['n']}"
