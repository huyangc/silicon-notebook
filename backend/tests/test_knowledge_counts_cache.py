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
        "CREATE TABLE knowledge_objects(id TEXT, notebook_id TEXT, object_type TEXT, status TEXT, source_id TEXT DEFAULT '');"
        "CREATE TABLE unified_kg_state(notebook_id TEXT PRIMARY KEY, kg_mutation_seq INTEGER);"
        "CREATE TABLE chunks(id TEXT, notebook_id TEXT, source_id TEXT);"
        "CREATE TABLE sources(id TEXT, notebook_id TEXT, source_type TEXT);"
        "CREATE TABLE source_elements(id TEXT, source_id TEXT);"
        "CREATE TABLE extraction_runs("
        "source_id TEXT, run_type TEXT, status TEXT, created_at TEXT);"
    )
    return db


def _add(db, nb, ot, st, n, start=0):
    db.executemany(
        "INSERT INTO knowledge_objects(id,notebook_id,object_type,status) VALUES(?,?,?,?)",
        [(f"{nb}-{ot}-{st}-{start + i}", nb, ot, st) for i in range(n)],
    )


def _add_chunks(db, nb, n, start=0):
    db.executemany(
        "INSERT INTO chunks(id,notebook_id,source_id) VALUES(?,?,?)",
        [(f"{nb}-ch-{start + i}", nb, f"{nb}-s1") for i in range(n)],
    )


def _add_source(
    db, nb, sid, *, source_type="document", parsed=True, has_kg=False
):
    db.execute(
        "INSERT INTO sources(id,notebook_id,source_type) VALUES(?,?,?)",
        (sid, nb, source_type),
    )
    if parsed:
        db.execute("INSERT INTO source_elements(id,source_id) VALUES(?,?)",
                   (f"el-{sid}", sid))
    if has_kg:
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id) "
            "VALUES(?,?,?,?,?)", (f"ko-{sid}", nb, "concept", "approved", sid))


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


def test_chunk_count_memo_and_seq_invalidation():
    db = _db()
    nb = "nbc"
    _add_chunks(db, nb, 4)
    _set_seq(db, nb, 1)
    db.commit()
    assert kcc.chunk_count(db, nb) == 4

    # add chunks WITHOUT bumping the seq -> served from memo (stale by design)
    _add_chunks(db, nb, 6, start=100)
    db.commit()
    assert kcc.chunk_count(db, nb) == 4

    # bump seq (what build_chunks_for_source / delete_source do) -> recompute
    _set_seq(db, nb, 2)
    db.commit()
    assert kcc.chunk_count(db, nb) == 10


def test_pending_source_count_predicate_and_seq_gate():
    db = _db()
    nb = "nbp"
    # parsed + no KG -> pending; parsed + KG -> not pending; unparsed -> not pending
    _add_source(db, nb, "s-pending", parsed=True, has_kg=False)
    _add_source(db, nb, "s-haskg", parsed=True, has_kg=True)
    _add_source(db, nb, "s-unparsed", parsed=False, has_kg=False)
    _set_seq(db, nb, 1)
    db.commit()
    assert kcc.pending_source_count(db, nb) == 1

    # give the pending source a KG object WITHOUT bumping -> memo hit, still 1
    db.execute(
        "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id) "
        "VALUES('ko-late',?, 'concept','approved','s-pending')", (nb,))
    db.commit()
    assert kcc.pending_source_count(db, nb) == 1
    # bump -> recompute -> now 0 pending
    _set_seq(db, nb, 2)
    db.commit()
    assert kcc.pending_source_count(db, nb) == 0


def test_pending_source_count_empty_source_id_does_not_satisfy():
    db = _db()
    nb = "nbp2"
    _add_source(db, nb, "s1", parsed=True, has_kg=False)
    # a KO with empty source_id must NOT clear the source's pending status
    db.execute(
        "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id) "
        "VALUES('ko-empty',?, 'concept','approved','')", (nb,))
    _set_seq(db, nb, 1)
    db.commit()
    assert kcc.pending_source_count(db, nb) == 1


def test_physical_and_visible_pending_source_caches_diverge_and_invalidate():
    db = _db()
    nb = "nb-visible-pending"
    _add_source(db, nb, "s-uploaded", source_type="document")
    _add_source(db, nb, "s-memory", source_type="memory")
    _add_source(db, nb, "s-knowhow", source_type="knowhow")
    _set_seq(db, nb, 1)
    db.commit()

    assert kcc.pending_source_count(db, nb) == 3
    assert kcc.visible_pending_source_count(db, nb) == 1
    kcc.invalidate(nb)
    assert kcc.visible_pending_source_count(db, nb) == 1
    assert kcc.pending_source_count(db, nb) == 3

    # Both sibling memos must be cleared by the same explicit invalidation.
    db.execute(
        "INSERT INTO knowledge_objects"
        "(id,notebook_id,object_type,status,source_id) VALUES(?,?,?,?,?)",
        ("ko-uploaded", nb, "concept", "approved", "s-uploaded"),
    )
    db.commit()
    assert kcc.pending_source_count(db, nb) == 3
    assert kcc.visible_pending_source_count(db, nb) == 1
    kcc.invalidate(nb)
    assert kcc.pending_source_count(db, nb) == 2
    assert kcc.visible_pending_source_count(db, nb) == 0


def test_object_type_total_all_vs_status_slice():
    db = _db()
    nb = "nbo"
    _add(db, nb, "claim", "approved", 5)
    _add(db, nb, "claim", "deprecated", 2)
    _add(db, nb, "concept", "approved", 3)
    _set_seq(db, nb, 1)
    db.commit()
    # falsy status = ALL statuses (incl deprecated), matching the bare COUNT
    assert kcc.object_type_total(db, nb, "claim") == 7
    assert kcc.object_type_total(db, nb, "claim", "") == 7
    # truthy status = single bucket
    assert kcc.object_type_total(db, nb, "claim", "approved") == 5
    assert kcc.object_type_total(db, nb, "claim", "deprecated") == 2
    # unknown type/status -> 0
    assert kcc.object_type_total(db, nb, "missing") == 0
    assert kcc.object_type_total(db, nb, "claim", "draft") == 0

    # status flip that bumps the seq -> value refreshes (invalidation contract)
    db.execute("UPDATE knowledge_objects SET status='deprecated' "
               "WHERE notebook_id=? AND object_type='concept'", (nb,))
    _set_seq(db, nb, 2)
    db.commit()
    assert kcc.object_type_total(db, nb, "concept", "approved") == 0
    assert kcc.object_type_total(db, nb, "concept") == 3  # all-status still 3
