"""Task 2 — reconnect isolated KG nodes.

Two seams under test:
  - the PURE inline helper `_relink_extra_relations(objects, relations, source_id)`
    used inside `_run_extraction` BETWEEN build_records and store_kg, and
  - the `relink_notebook_kg(notebook_id)` backfill wired at the end of
    `build_notebook_kg`.

The deterministic core (`app.services.kg.relink.complete_isolated_edges`) is
covered by tests/kg/test_relink.py; here we only test the repository adapters.
"""
import json
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


# ---------------------------------------------------------------------------
# Part A — pure inline helper
# ---------------------------------------------------------------------------

def test_relink_extra_relations_links_isolated_claim_to_concept(repo):
    """A claim + a concept that share an evidence element_id, with NO relation
    between them, must yield exactly one relink relation dict shaped like the
    LLM relations build_records emits (so store_kg can remap + persist it)."""
    objects = [
        {"local_id": "K1", "object_type": "claim",
         "payload": {"name": "Engram improves perplexity"},
         "evidence": [{"element_id": "e1", "quote": "..."}]},
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram"},
         "evidence": [{"element_id": "e1", "quote": "..."}]},
    ]
    extra = repo._relink_extra_relations(objects, [], "s1")
    assert len(extra) == 1
    rel = extra[0]
    assert rel["source_local_id"] == "K1"      # claim is the about-source
    assert rel["target_local_id"] == "C1"
    assert rel["edge_type"] == "about"
    assert rel["evidence"][0]["basis"] == "relink:shared-element"
    assert rel["evidence"][0]["quote"] == ""


def test_relink_extra_relations_noop_when_already_connected(repo):
    """If the claim and concept are already linked, no relink edge is added."""
    objects = [
        {"local_id": "K1", "object_type": "claim",
         "payload": {"name": "Engram improves perplexity"},
         "evidence": [{"element_id": "e1"}]},
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram"},
         "evidence": [{"element_id": "e1"}]},
    ]
    relations = [{"source_local_id": "K1", "target_local_id": "C1",
                  "edge_type": "about", "evidence": []}]
    extra = repo._relink_extra_relations(objects, relations, "s1")
    assert extra == []


# ---------------------------------------------------------------------------
# Part B — notebook-level backfill
# ---------------------------------------------------------------------------

def _insert_source(repo, notebook_id, source_id):
    """Insert a minimal sources row (knowledge_relations.source_id has an FK to
    sources(id); production objects are always backed by a real source)."""
    now = _now()
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, 'T', 'markdown', 'extracted', 'parsed',
                       'f.md', '', 0, '', '', 'academic_paper', ?, ?)""",
            (source_id, notebook_id, now, now),
        )


def _insert_object(repo, notebook_id, source_id, object_type, name, element_ids,
                   status="approved"):
    """Insert a knowledge_objects row with real evidence (element_ids) so the
    backfill's $.element_id extraction has something to read."""
    oid = f"ko-{uuid4().hex[:10]}"
    now = _now()
    evidence = [{"source_id": source_id, "element_id": eid,
                 "quoted_span": name, "confidence": 1.0} for eid in element_ids]
    with repo._write() as db:
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_candidate_id, source_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)""",
            (oid, notebook_id, object_type, status,
             json.dumps({"name": name}, ensure_ascii=False),
             json.dumps(evidence, ensure_ascii=False), source_id, now, now),
        )
    return oid


def test_relink_notebook_kg_connects_shared_element(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:8]}"
    _insert_source(repo, nb.id, sid)
    claim = _insert_object(repo, nb.id, sid, "claim",
                           "Engram improves perplexity", ["e1"])
    concept = _insert_object(repo, nb.id, sid, "concept", "Engram", ["e1"])
    # No relation between them yet.
    assert repo.relations_for_notebook(nb.id) == []

    stats = repo.relink_notebook_kg(nb.id)
    assert stats["edges_added"] == 1
    assert stats["isolated_before"] >= 2
    assert stats["isolated_after"] < stats["isolated_before"]

    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1
    endpoints = {rels[0]["source_object_id"], rels[0]["target_object_id"]}
    assert endpoints == {claim, concept}
    assert rels[0]["edge_type"] == "about"
    # review_status defaults to the same value LLM edges get ('pending').
    with repo._connect() as db:
        rs = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE notebook_id=?",
            (nb.id,)).fetchone()["review_status"]
    assert rs == "pending"


def test_relink_notebook_kg_is_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:8]}"
    _insert_source(repo, nb.id, sid)
    _insert_object(repo, nb.id, sid, "claim", "Engram improves perplexity", ["e1"])
    _insert_object(repo, nb.id, sid, "concept", "Engram", ["e1"])

    first = repo.relink_notebook_kg(nb.id)
    assert first["edges_added"] == 1
    second = repo.relink_notebook_kg(nb.id)
    assert second["edges_added"] == 0                       # idempotent
    assert len(repo.relations_for_notebook(nb.id)) == 1


def test_relink_notebook_kg_intra_source_only(repo):
    """Two objects in DIFFERENT sources sharing an element_id must NOT be linked
    (the relink core's intra-source guard, fed real source_ids)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_source(repo, nb.id, "src-A")
    _insert_source(repo, nb.id, "src-B")
    _insert_object(repo, nb.id, "src-A", "claim", "Engram improves perplexity", ["e1"])
    _insert_object(repo, nb.id, "src-B", "concept", "Engram", ["e1"])
    stats = repo.relink_notebook_kg(nb.id)
    assert stats["edges_added"] == 0
    assert repo.relations_for_notebook(nb.id) == []


def test_build_notebook_kg_skips_relink_when_disabled(repo, monkeypatch):
    """With kg_relink_enabled=False the build_notebook_kg path must NOT relink.

    build_notebook_kg requires an LLM; we stub the LLM gate + extraction so the
    only thing exercised is the relink wiring at the tail. With two pre-seeded
    isolated objects sharing an element, relink (if it ran) would add an edge.
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:8]}"
    _insert_source(repo, nb.id, sid)
    _insert_object(repo, nb.id, sid, "claim", "Engram improves perplexity", ["e1"])
    _insert_object(repo, nb.id, sid, "concept", "Engram", ["e1"])

    # Make build_notebook_kg's body a no-op except for the relink tail: pretend
    # the LLM is configured, and skip per-source extraction (objects already seeded).
    class _Cfg:
        configured = True
    monkeypatch.setattr(repo, "llm_client", _Cfg())
    monkeypatch.setattr(repo, "_run_extraction", lambda *a, **k: None)

    repo.settings.kg_relink_enabled = False
    out = repo.build_notebook_kg(nb.id)
    assert "relink" not in out
    assert repo.relations_for_notebook(nb.id) == []        # relink skipped

    # Flip it on: now build_notebook_kg should relink and report stats.
    repo.settings.kg_relink_enabled = True
    out2 = repo.build_notebook_kg(nb.id)
    assert "relink" in out2
    assert out2["relink"]["edges_added"] == 1
    assert len(repo.relations_for_notebook(nb.id)) == 1
