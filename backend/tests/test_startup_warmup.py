"""Startup-readiness warm-up: ``knowledge_counts_cache.warm_all`` (store layer)
and the ``warm_open_path_caches`` facade delegate the startup daemon calls.

``warm_all`` primes all three per-notebook open-path memos (the per-type GROUP
BY, the pending-source correlated count and the chunk count) for every live
notebook — skipping ``status='copying'`` half-copies — and reports progress per
notebook, so the first open / board / status-poll after a fresh process start is
served warm instead of paying the cold recompute. The facade method is the
SQL-free seam ``app/services/startup_warmup.py`` calls behind the readiness gate.

The store-layer tests mirror ``tests/test_knowledge_counts_cache.py`` (in-memory
db); the facade test drives the real repository like the count tests do.
"""
import sqlite3

import pytest

from app.api.deps import repository
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowledge_counts_cache as kcc


@pytest.fixture(autouse=True)
def _clear_memo():
    kcc.invalidate()
    yield
    kcc.invalidate()


# --------------------------------------------------------------------------
# store layer: warm_all over a hermetic in-memory schema
# --------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE notebooks(id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'draft');"
        "CREATE TABLE knowledge_objects(id TEXT, notebook_id TEXT, object_type TEXT, status TEXT, source_id TEXT DEFAULT '');"
        "CREATE TABLE unified_kg_state(notebook_id TEXT PRIMARY KEY, kg_mutation_seq INTEGER);"
        "CREATE TABLE chunks(id TEXT, notebook_id TEXT, source_id TEXT);"
        "CREATE TABLE sources(id TEXT, notebook_id TEXT);"
        "CREATE TABLE source_elements(id TEXT, source_id TEXT);"
        "CREATE TABLE extraction_runs("
        "source_id TEXT, run_type TEXT, status TEXT, created_at TEXT);"
    )
    return db


def _add_notebook(db, nb, status="draft"):
    db.execute("INSERT INTO notebooks(id,status) VALUES(?,?)", (nb, status))


def _add_object(db, nb, ot, st):
    db.execute(
        "INSERT INTO knowledge_objects(id,notebook_id,object_type,status) VALUES(?,?,?,?)",
        (f"{nb}-{ot}-{st}", nb, ot, st),
    )


def test_warm_all_primes_all_three_memos_and_returns_count():
    db = _db()
    _add_notebook(db, "nb1")
    _add_notebook(db, "nb2")
    _add_object(db, "nb1", "claim", "approved")
    _add_object(db, "nb2", "concept", "reviewed")
    db.commit()

    total = kcc.warm_all(db)

    assert total == 2
    for nb in ("nb1", "nb2"):
        assert nb in kcc._MEMO
        assert nb in kcc._PENDING
        assert nb in kcc._CHUNKS
    # the memoized value is the real breakdown, not a placeholder
    assert kcc._MEMO["nb1"][1] == {("claim", "approved"): 1}


def test_warm_all_reports_progress_once_per_notebook():
    db = _db()
    for i in range(3):
        _add_notebook(db, f"nb{i}")
    db.commit()

    calls = []
    total = kcc.warm_all(db, progress=lambda done, tot: calls.append((done, tot)))

    assert total == 3
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_warm_all_skips_copying_notebooks():
    db = _db()
    _add_notebook(db, "live", status="draft")
    _add_notebook(db, "half", status="copying")
    db.commit()

    calls = []
    total = kcc.warm_all(db, progress=lambda d, t: calls.append((d, t)))

    assert total == 1  # the half-copied notebook is excluded
    assert "live" in kcc._MEMO
    assert "half" not in kcc._MEMO
    assert calls == [(1, 1)]


def test_warm_all_is_best_effort_on_sqlite_error():
    """A per-notebook sqlite error must not abort the whole warm: the notebook
    is still counted, progress still fires (finally), and the run continues."""
    db = _db()
    _add_notebook(db, "nb1")
    _add_notebook(db, "nb2")
    db.commit()
    db.execute("DROP TABLE chunks")  # chunk_count now raises sqlite3.OperationalError
    db.commit()

    calls = []
    total = kcc.warm_all(db, progress=lambda d, t: calls.append((d, t)))

    assert total == 2
    assert calls == [(1, 2), (2, 2)]
    # the memos primed before the failing call are still populated
    assert "nb1" in kcc._MEMO and "nb1" in kcc._PENDING


def test_warm_all_without_progress_and_when_empty():
    db = _db()
    assert kcc.warm_all(db) == 0  # no notebooks
    _add_notebook(db, "nb1")
    db.commit()
    assert kcc.warm_all(db) == 1  # progress=None must not raise


# --------------------------------------------------------------------------
# facade: warm_open_path_caches over the real repository (isolated tmp DB via
# the autouse conftest fixture) — exactly what the startup daemon calls.
# --------------------------------------------------------------------------
def test_warm_open_path_caches_primes_memos_and_reports_progress():
    repo = repository()
    nb1 = repo.create_notebook(NotebookCreate(name="kb-a"))
    repo._test_insert_object(nb1.id, "claim", {"name": "c1"})
    nb2 = repo.create_notebook(NotebookCreate(name="kb-b"))
    repo._test_insert_object(nb2.id, "concept", {"name": "c2"})

    kcc.invalidate()
    assert not kcc._MEMO  # cold after the clear — warm is what repopulates

    calls = []
    total = repo.warm_open_path_caches(
        progress=lambda done, tot: calls.append((done, tot))
    )

    # (a) returns the notebook count warmed
    assert total == 2
    # (b) progress fired once per notebook, 1-based, monotonic
    assert len(calls) == 2
    assert [done for done, _ in calls] == [1, 2]
    assert all(tot == 2 for _, tot in calls)
    # (c) all three per-notebook memos are now populated
    for nb in (nb1.id, nb2.id):
        assert nb in kcc._MEMO
        assert nb in kcc._PENDING
        assert nb in kcc._CHUNKS
