"""knowledge_counts_cache: seq-gated per-notebook object count memo.

Verifies (1) the raw {(type,status):count} + the two derived views (non-deprecated
/ status-whitelist / active total), (2) the memo elides recompute at a stable
kg_mutation_seq, and (3) a seq bump forces a fresh count — the invalidation
contract the 7 rewired call sites rely on.
"""
import sqlite3

import pytest

from app.repositories.sqlite import knowledge_counts_cache as kcc

USABLE = ("approved", "reviewed", "project_specific", "conflict")


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE knowledge_objects(id TEXT, notebook_id TEXT, object_type TEXT, status TEXT);"
        "CREATE TABLE unified_kg_state(notebook_id TEXT PRIMARY KEY, kg_mutation_seq INTEGER);"
    )
    return db


def _add(db, nb, ot, st, n, start=0):
    db.executemany(
        "INSERT INTO knowledge_objects VALUES(?,?,?,?)",
        [(f"{nb}-{ot}-{st}-{start + i}", nb, ot, st) for i in range(n)],
    )


def _set_seq(db, nb, seq):
    db.execute(
        "INSERT INTO unified_kg_state(notebook_id,kg_mutation_seq) VALUES(?,?) "
        "ON CONFLICT(notebook_id) DO UPDATE SET kg_mutation_seq=excluded.kg_mutation_seq",
        (nb, seq),
    )


@pytest.fixture(autouse=True)
def _clear_memo():
    kcc.invalidate()
    yield
    kcc.invalidate()


def test_raw_counts_and_derived_filters():
    db = _db()
    nb = "nb1"
    _add(db, nb, "claim", "approved", 5)
    _add(db, nb, "claim", "deprecated", 2)
    _add(db, nb, "concept", "reviewed", 3)
    _add(db, nb, "formula", "draft", 4)
    _set_seq(db, nb, 1)
    db.commit()

    raw = kcc.type_status_counts(db, nb)
    assert raw[("claim", "approved")] == 5
    assert raw[("claim", "deprecated")] == 2

    # non-deprecated (statuses=None)
    assert kcc.type_counts(db, nb) == {"claim": 5, "concept": 3, "formula": 4}
    # USABLE whitelist excludes 'draft' and 'deprecated'
    assert kcc.type_counts(db, nb, USABLE) == {"claim": 5, "concept": 3}
    # active total = all non-deprecated
    assert kcc.active_object_count(db, nb) == 12


def test_absent_notebook_is_zero():
    db = _db()
    assert kcc.type_status_counts(db, "missing") == {}
    assert kcc.type_counts(db, "missing") == {}
    assert kcc.active_object_count(db, "missing") == 0


def test_memo_hit_then_seq_invalidation():
    db = _db()
    nb = "nb2"
    _add(db, nb, "claim", "approved", 3)
    _set_seq(db, nb, 1)
    db.commit()
    assert kcc.active_object_count(db, nb) == 3

    # mutate rows WITHOUT bumping the seq -> served from memo (stale by design;
    # proves the memo actually elides the GROUP BY at a stable seq).
    _add(db, nb, "claim", "approved", 7, start=100)
    db.commit()
    assert kcc.active_object_count(db, nb) == 3

    # bump kg_mutation_seq (what every real KG write does) -> recompute.
    _set_seq(db, nb, 2)
    db.commit()
    assert kcc.active_object_count(db, nb) == 10
    assert kcc.type_counts(db, nb) == {"claim": 10}


def test_seq_zero_aliasing_needs_explicit_invalidate():
    """A cached entry at seq 0 aliases with the 'no state row' sentinel (also 0).
    delete_notebook_kg drops the unified_kg_state row (seq reads 0 afterward), so a
    notebook whose counts were cached at seq 0 (e.g. a freshly copy_notebook'd nb)
    would be served stale post-delete UNLESS the delete path calls invalidate() —
    which delete_notebook_kg / run_extraction now do. This pins that contract."""
    db = _db()
    nb = "nb-alias"
    _add(db, nb, "claim", "approved", 4)
    _set_seq(db, nb, 0)  # objects present, seq 0 (copy_notebook doesn't bump)
    db.commit()
    assert kcc.active_object_count(db, nb) == 4  # cached at seq 0

    # simulate delete_notebook_graph_rows: objects gone, state row gone (seq still 0)
    db.execute("DELETE FROM knowledge_objects WHERE notebook_id=?", (nb,))
    db.execute("DELETE FROM unified_kg_state WHERE notebook_id=?", (nb,))
    db.commit()
    assert kcc.active_object_count(db, nb) == 4  # aliasing: seq still 0 -> stale hit
    kcc.invalidate(nb)  # what the delete path now does
    assert kcc.active_object_count(db, nb) == 0


def test_invalidate_forces_recompute_without_seq_change():
    db = _db()
    nb = "nb3"
    _add(db, nb, "risk", "approved", 2)
    _set_seq(db, nb, 5)
    db.commit()
    assert kcc.active_object_count(db, nb) == 2
    _add(db, nb, "risk", "approved", 1, start=50)
    db.commit()
    assert kcc.active_object_count(db, nb) == 2  # cached
    kcc.invalidate(nb)
    assert kcc.active_object_count(db, nb) == 3  # recomputed after explicit drop
