"""Tests for the scale delta search policy:

Default behaviour = search only the INDEXED part (ANN core ∪ FTS lexical); the
watermark-delta brute-force is OFF unless SCALE_SEARCH_INCLUDE_DELTA=True. When
content is added to an already-indexed notebook and SCALE_AUTO_FOLD_ON_ADD=True
(default), an idle incremental fold is enqueued so the delta becomes indexed.
scale_index_status surfaces the pending-unindexed count.
"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, ScaleIndexStatus
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _insert_source_chunk(
    repo, nb_id, sid, cid, text, embed_text, day, source_type="md"
):
    """Insert a source + one chunk + that chunk's embedding (embedding derived
    from `embed_text` so the query vector can be steered independently of the
    chunk's lexical text)."""
    with repo._write() as db:
        now = f"2026-07-{day:02d}T00:00:00"
        db.execute(
            "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, nb_id, "t", source_type, "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)", (cid, nb_id, sid, text, "", "[]", now))
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_query(embed_text)
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (cid, nb_id, json.dumps(v), now))


def _build_indexed_nb_with_delta(repo):
    """Base notebook: source A ('alpha') is captured by the watermark at build
    time; source B ('bravo') is added AFTER → it is the delta. B's embedding text
    is chosen so that its vector is the BEST match for the query used in the
    tests, but its chunk text has NO lexical overlap with that query (so only the
    semantic path — ANN core ∪ delta brute-force — could surface it, not FTS)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    # A: watermark source. Its chunk text/embedding is 'alpha' (far from query).
    _insert_source_chunk(repo, nb.id, "sA", "cA", "alpha content here", "alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)  # watermark = {sA}; cA is in the chunk ANN
    # B: delta source added after the watermark. Its CHUNK TEXT is deliberately
    # lexically disjoint from the query 'zeta'; its EMBEDDING is 'zeta' so it is
    # the top semantic match — reachable only via the (opt-in) delta brute-force.
    _insert_source_chunk(repo, nb.id, "sB", "cB", "unrelated words xxyyzz", "zeta", 2)
    return nb


# ── Test 1: default = delta NOT brute-forced (semantic path skips it) ─────────

def test_default_delta_not_semantically_searched(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", False)
    nb = _build_indexed_nb_with_delta(repo)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and getattr(idx, "chunk_ann_labels", None)
    # Query 'zeta' has NO lexical overlap with cB's text; cB's vector is the best
    # semantic match. With delta OFF, cB must NOT appear (it's not in ANN core and
    # the delta brute-force is skipped). FTS can't surface it (no lexical hit).
    qv = repo._embed_query("zeta")
    out = repo._retrieve_chunks_ann(nb.id, "zeta", qv, idx, recall=10)
    assert out is not None
    scored = out[0]
    got = {c.chunk_id for c in scored}
    assert "cB" not in got, f"delta chunk cB must be absent when delta is OFF; got {got}"


# ── Test 2: opt-in = delta included (brute-force runs) ────────────────────────

def test_optin_delta_included(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _build_indexed_nb_with_delta(repo)
    idx = repo._scale_index(nb.id, allow_stale=True)
    assert idx is not None and getattr(idx, "chunk_ann_labels", None)
    qv = repo._embed_query("zeta")
    out = repo._retrieve_chunks_ann(nb.id, "zeta", qv, idx, recall=10)
    assert out is not None
    scored = out[0]
    got = {c.chunk_id for c in scored}
    assert "cB" in got, f"delta chunk cB must be present when delta is ON; got {got}"


# ── Test 3: auto-fold enqueues on add (only when already indexed) ─────────────

def test_auto_fold_enqueues_when_indexed(repo, monkeypatch):
    # never actually run the off-peak scheduler / spawn threads in tests
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_ensure_scheduler", lambda: None)
    monkeypatch.setattr(repo.settings, "scale_auto_fold_on_add", True)
    nb = _build_indexed_nb_with_delta(repo)  # already has a scale index
    assert repo._scale_index(nb.id, allow_stale=True) is not None
    assert nb.id not in repo._scale_idle_queue
    repo._maybe_enqueue_scale_fold(nb.id)
    assert nb.id in repo._scale_idle_queue
    assert repo._scale_idle_queue[nb.id][0] == "fold"


def test_auto_fold_no_index_does_not_enqueue(repo, monkeypatch):
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_ensure_scheduler", lambda: None)
    monkeypatch.setattr(repo.settings, "scale_auto_fold_on_add", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    _insert_source_chunk(repo, nb.id, "sA", "cA", "alpha", "alpha", 1)
    repo.rebuild_unified_kg(nb.id)
    # NO scale index built → must NOT auto-build / enqueue
    assert repo._scale_index(nb.id, allow_stale=True) is None
    repo._maybe_enqueue_scale_fold(nb.id)
    assert nb.id not in repo._scale_idle_queue


def test_auto_fold_disabled_does_not_enqueue(repo, monkeypatch):
    monkeypatch.setattr(repo._runtime.scale_artifacts, "_ensure_scheduler", lambda: None)
    monkeypatch.setattr(repo.settings, "scale_auto_fold_on_add", False)
    nb = _build_indexed_nb_with_delta(repo)  # has index + delta
    assert repo._scale_index(nb.id, allow_stale=True) is not None
    repo._maybe_enqueue_scale_fold(nb.id)
    assert nb.id not in repo._scale_idle_queue


# ── Test 4: status exposes pending-unindexed ──────────────────────────────────

def test_status_exposes_pending_unindexed(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", False)
    nb = _build_indexed_nb_with_delta(repo)  # watermark={sA}, delta source sB
    st = repo.scale_index_status(nb.id)
    assert st["unindexed_sources"] >= 1
    assert st["delta_chunks"] >= 1
    assert st["delta_searchable"] is False
    # keys must always be present (schema stability across all states)
    fresh = repo.create_notebook(NotebookCreate(name="fresh"))
    st2 = repo.scale_index_status(fresh.id)
    assert "unindexed_sources" in st2 and "delta_searchable" in st2


def test_status_exposes_only_visible_unindexed_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="mixed delta"))
    _insert_source_chunk(
        repo, nb.id, "s-uploaded", "c-uploaded", "uploaded", "uploaded", 1
    )
    _insert_source_chunk(
        repo,
        nb.id,
        "s-memory",
        "c-memory",
        "memory",
        "memory",
        2,
        source_type="memory",
    )
    _insert_source_chunk(
        repo,
        nb.id,
        "s-knowhow",
        "c-knowhow",
        "knowhow",
        "knowhow",
        3,
        source_type="knowhow",
    )

    scale_status = repo.scale_index_status(nb.id)
    assert scale_status["unindexed_sources"] == 1
    assert scale_status["has_unindexed_content"] is True


def test_derived_only_delta_remains_actionable_physical_content(repo):
    nb = repo.create_notebook(NotebookCreate(name="derived delta"))
    _insert_source_chunk(
        repo, nb.id, "s-uploaded", "c-uploaded", "uploaded", "uploaded", 1
    )
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    _insert_source_chunk(
        repo,
        nb.id,
        "s-memory",
        "c-memory",
        "memory",
        "memory",
        2,
        source_type="memory",
    )
    _insert_source_chunk(
        repo,
        nb.id,
        "s-knowhow",
        "c-knowhow",
        "knowhow",
        "knowhow",
        3,
        source_type="knowhow",
    )

    scale_status = repo.scale_index_status(nb.id)
    assert scale_status["unindexed_sources"] == 0
    assert scale_status["has_unindexed_content"] is True
    assert repo._index_delta(nb.id)["delta_sources"] == ["s-knowhow", "s-memory"]


def test_status_delta_searchable_reflects_setting(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = _build_indexed_nb_with_delta(repo)
    st = repo.scale_index_status(nb.id)
    assert st["delta_searchable"] is True


# ── Test 5: schema round-trips the new fields ─────────────────────────────────

def test_schema_round_trips_new_fields():
    m = ScaleIndexStatus(exists=True, stale=False, building=False, eligible=True,
                         unindexed_sources=3, has_unindexed_content=True,
                         delta_searchable=True)
    assert m.unindexed_sources == 3
    assert m.has_unindexed_content is True
    assert m.delta_searchable is True
    # defaults for older callers
    d = ScaleIndexStatus(exists=False, stale=False, building=False, eligible=False)
    assert d.unindexed_sources == 0
    assert d.has_unindexed_content is False
    assert d.delta_searchable is False


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
