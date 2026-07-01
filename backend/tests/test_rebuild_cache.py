"""Tests for the skip-when-unchanged gate on rebuild_unified_kg (perf fix).

rebuild_unified_kg re-clusters the whole KG on every call. This gate computes an
O(1) content-version of the clustering INPUTS (objects, decided merge pairs,
embeddings, embed_dim) and, when unchanged since the last rebuild, skips the
entire recompute and returns the cached cluster count. The cardinal rule is
correctness: a stale clustering result must NEVER be served, so the version must
change whenever any clustering input changes.

The tests spy on repo._stream_seed_reps (the first heavy step of the recompute)
to assert whether a real rebuild ran or the gate short-circuited.
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")  # embedder_configured=True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)  # inject; no real model loads (lazy)
    return r


def _concept(local_id, name, span="span"):
    return {
        "local_id": local_id,
        "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": "D",
            "element_id": "e", "element_type": "p", "location_label": "1",
            "quoted_span": span, "confidence": 1.0,
        }],
    }


def _make_kg(repo, names=("MOSFET", "cascode")):
    """A notebook with a few concepts across two sources (some share a
    normalized name so a canonical is multi-member)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [_concept(f"a{i}", n, f"{n}-a") for i, n in enumerate(names)], [])
    repo.store_kg(nb.id, "s2", [_concept(f"b{i}", n.lower(), f"{n}-b") for i, n in enumerate(names)], [])
    return nb


def _spy_stream(repo, monkeypatch):
    """Wrap _stream_seed_reps so we can assert whether the recompute ran."""
    calls = {"n": 0}
    orig = repo._stream_seed_reps

    def _wrapped(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(repo, "_stream_seed_reps", _wrapped)
    return calls


def _cluster_rows(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT object_type, member_object_id, canonical_id, canonical_name "
            "FROM concept_clusters WHERE notebook_id=? "
            "ORDER BY object_type, member_object_id, canonical_id",
            (nb_id,)).fetchall()


# --- 1. version sensitivity -------------------------------------------------

def test_version_changes_when_concept_added(repo):
    nb = _make_kg(repo)
    v0 = repo._cluster_input_version(nb.id)
    repo.store_kg(nb.id, "s3", [_concept("c0", "bandgap", "bandgap-c")], [])
    v1 = repo._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_changes_when_embedding_replaced(repo):
    nb = _make_kg(repo)
    v0 = repo._cluster_input_version(nb.id)
    # Add a brand-new embedding row (count changes) for a synthetic object id.
    with repo._write() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
            "VALUES (?,?,?,?)",
            ("ko-synthetic", nb.id, json.dumps([0.0] * 16), _now()),
        )
    v1 = repo._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_changes_when_decided_pair_recorded(repo):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)  # populates pending candidates
    v0 = repo._cluster_input_version(nb.id)
    # Record a confirmed decided pair (mirrors decided_pairs' WHERE).
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'confirmed', ?, ?)",
            ("mc-decided", nb.id, "K-foo", "K-bar", 0.9, now, now),
        )
    v1 = repo._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_stable_across_rebuild(repo):
    """rebuild writes only concept_clusters + pending candidates, neither of which
    is in the signature, so the version is identical before and after a rebuild."""
    nb = _make_kg(repo)
    before = repo._cluster_input_version(nb.id)
    repo.rebuild_unified_kg(nb.id)
    after = repo._cluster_input_version(nb.id)
    assert before == after


def test_version_excludes_pending_candidates(repo):
    """Pending merge candidates (written by rebuild) must NOT move the version;
    only confirmed/rejected decisions do."""
    nb = _make_kg(repo)
    v0 = repo._cluster_input_version(nb.id)
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'pending', ?, ?)",
            ("mc-pending", nb.id, "K-foo", "K-bar", 0.9, now, now),
        )
    v1 = repo._cluster_input_version(nb.id)
    assert v0 == v1


# --- 2. skip when unchanged -------------------------------------------------

def test_second_rebuild_skips_when_unchanged(repo, monkeypatch):
    nb = _make_kg(repo)
    first = repo.rebuild_unified_kg(nb.id)
    rows_before = [tuple(r) for r in _cluster_rows(repo, nb.id)]

    calls = _spy_stream(repo, monkeypatch)
    second = repo.rebuild_unified_kg(nb.id)

    assert calls["n"] == 0                       # recompute short-circuited
    assert second == first                       # same cluster count returned
    rows_after = [tuple(r) for r in _cluster_rows(repo, nb.id)]
    assert rows_after == rows_before             # concept_clusters untouched
    assert rows_after                            # (sanity) clusters exist


# --- 3. invalidation --------------------------------------------------------

def test_rebuild_recomputes_after_input_change(repo, monkeypatch):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)

    # Add a new concept -> version changes -> next rebuild must recompute.
    repo.store_kg(nb.id, "s3", [_concept("c0", "bandgap", "bandgap-c")], [])

    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id)
    assert calls["n"] >= 1                        # real recompute ran


# --- 4. force bypass --------------------------------------------------------

def test_force_bypasses_gate(repo, monkeypatch):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)

    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id, force=True)    # unchanged inputs, but forced
    assert calls["n"] >= 1                        # recompute ran despite no change


# --- 5. migration -----------------------------------------------------------

def test_migration_adds_cluster_input_version_column(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute(
            "PRAGMA table_info(unified_kg_state)").fetchall()}
    assert "cluster_input_version" in cols


# --- 6. first-ever rebuild never skips --------------------------------------

def test_first_rebuild_never_skips(repo, monkeypatch):
    nb = _make_kg(repo)
    # No stored version yet (no prior rebuild) -> must recompute.
    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id)
    assert calls["n"] >= 1
