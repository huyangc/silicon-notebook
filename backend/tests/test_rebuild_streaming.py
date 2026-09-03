import time
import pytest
from unittest.mock import patch
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))               # inject; no real model loads (lazy)
    return r


def test_rebuild_streaming_clusters_same_name(repo):
    """Two concepts MOSFET/mosfet across two store_kg calls collapse to one
    canonical via the streamed (scratch-table) rebuild path."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
                                 "payload": {"name": "MOSFET", "section_path": ""},
                                 "evidence": []}], [])
    repo.store_kg(nb.id, None, [{"local_id": "b", "object_type": "concept",
                                 "payload": {"name": "mosfet", "section_path": ""},
                                 "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2


def test_concurrent_rebuild_scratch_isolated(repo):
    """run_id isolation: a stray scratch row left by a different run is untouched
    by a subsequent rebuild, and both rebuilds produce correct clusters + leave
    zero own-run scratch rows behind.

    Simulates interleaving by inserting a row with a fake 'other_run' run_id
    BEFORE the second rebuild. Asserts:
      1. After rebuild 1: no scratch rows for nb (own run fully cleaned).
      2. After inserting the stray row: it is visible in the table.
      3. After rebuild 2: stray row is STILL there (different run_id untouched),
         AND the rebuild's own rows are fully cleaned.
      4. Both rebuilds produce the same stable cluster result.
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "NMOS", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "nmos", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "claim",
         "payload": {"name": "gain is high", "section_path": ""}, "evidence": []},
    ], [])

    # --- Rebuild 1 ---
    count1 = repo.rebuild_unified_kg(nb.id)
    cmap1 = repo.cluster_map(nb.id)
    with repo._connect() as db:
        after1 = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert after1 == 0, "rebuild 1 must leave zero scratch rows"

    # Insert a stray row as if a different run_id's rebuild were still in flight.
    stray_run_id = "other_run_000"
    with repo._write() as db:
        db.execute(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
            (nb.id, stray_run_id, "fake-obj-id", "fake-seed"))
    with repo._connect() as db:
        stray_count = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, stray_run_id)).fetchone()["c"]
    assert stray_count == 1, "stray row must be present before rebuild 2"

    # --- Rebuild 2 ---
    count2 = repo.rebuild_unified_kg(nb.id)
    cmap2 = repo.cluster_map(nb.id)

    # Stray row from the other run must be untouched.
    with repo._connect() as db:
        stray_after = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, stray_run_id)).fetchone()["c"]
        total_after = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]

    assert stray_after == 1, "stray row from other run_id must survive rebuild 2"
    assert total_after == 1, "rebuild 2's own rows must all be cleaned (only stray remains)"

    # Both rebuilds should produce the same stable cluster result.
    assert count1 == count2, "cluster count must be stable across rebuilds"
    # NMOS/nmos (concepts) → one canonical; two member objects.
    # cluster_map returns all types (concepts + claims), so filter to concept entries.
    concept_canonicals1 = {cid for cid in cmap1.values() if cid.startswith("K-")}
    assert len(concept_canonicals1) == 1, "NMOS/nmos must collapse to one concept canonical"
    concept_members1 = [oid for oid, cid in cmap1.items() if cid.startswith("K-")]
    assert len(concept_members1) == 2, "both NMOS objects must be in the concept cluster"
    assert cmap1 == cmap2, "cluster map must be identical across sequential rebuilds"


def test_interrupted_rebuild_clears_only_its_own_run_scratch_rows(repo, monkeypatch):
    """Pass A2 now commits each scratch batch independently (off the write
    lock) instead of one all-or-nothing transaction for the whole rebuild.
    That means a crash/exception ANYWHERE later in rebuild_unified_kg — not
    just during Pass A2 itself — can no longer rely on a rollback to erase
    scratch rows that were already durably committed. rebuild_unified_kg must
    clear its OWN run_id's rows in a `finally` regardless of where it failed,
    while leaving a different (concurrent) run_id's rows untouched — same
    isolation contract as test_concurrent_rebuild_scratch_isolated above, just
    exercised on the crash path instead of the success path.

    Forces the crash in cluster_seeds — AFTER _stream_seed_reps has streamed
    and committed every "concept" scratch batch (proving the batches are
    already durable, not rolled back, by the time the failure hits) but
    before the run's final cleanup line would otherwise run.
    """
    from app.services import kg_merge

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": f"o{i}", "object_type": "concept",
         "payload": {"name": f"c-{i}", "section_path": ""}, "evidence": []}
        for i in range(2_500)
    ], [])

    # Stray row under a different run_id, simulating a concurrent rebuild of
    # the same notebook still in flight.
    stray_run_id = "other_run_concurrent"
    with repo._write() as db:
        db.execute(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
            (nb.id, stray_run_id, "fake-obj-id", "fake-seed"))

    def _boom(*args, **kwargs):
        raise RuntimeError("injected failure after Pass A2 scratch writes")

    monkeypatch.setattr(kg_merge, "cluster_seeds", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        repo.rebuild_unified_kg(nb.id)

    with repo._connect() as db:
        total = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
        stray = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, stray_run_id)).fetchone()["c"]

    assert stray == 1, "stray row from a concurrent/other run_id must survive the crash"
    assert total == 1, (
        "crashed rebuild must leave zero of its OWN run_id's scratch rows "
        "(only the pre-existing stray row should remain)", total)


@pytest.mark.slow
def test_rebuild_streaming_scales(repo):
    """Gated scale test: rebuild_unified_kg completes with bounded memory for a
    large synthetic notebook (~20k mostly-unique concepts).  The test records
    wall-clock time and verifies that:
      - every concept is written to concept_clusters (rows == N)
      - kg_cluster_scratch is fully cleaned up after the rebuild
      - the cluster count is ≥ 90 % of N (mostly-unique names → mostly separate clusters)
    """
    nb = repo.create_notebook(NotebookCreate(name="big"))
    N = 20_000
    BATCH = 2_000
    for start in range(0, N, BATCH):
        objs = [
            {
                "local_id": f"c{i}",
                "object_type": "concept",
                "payload": {"name": f"concept number {i}", "section_path": ""},
                "evidence": [],
            }
            for i in range(start, start + BATCH)
        ]
        repo.store_kg(nb.id, None, objs, [])

    t = time.perf_counter()
    n_clusters = repo.rebuild_unified_kg(nb.id)
    dt = time.perf_counter() - t

    with repo._connect() as db:
        rows = db.execute(
            "SELECT COUNT(*) AS c FROM concept_clusters "
            "WHERE notebook_id=? AND object_type='concept'",
            (nb.id,),
        ).fetchone()["c"]
        scratch = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["c"]

    assert rows == N, f"expected {N} concept_clusters rows, got {rows}"
    assert scratch == 0, "kg_cluster_scratch must be empty after rebuild"
    assert n_clusters >= N * 0.9, (
        f"expected ≥{N * 0.9:.0f} clusters (mostly-unique names), got {n_clusters}"
    )
    print(
        f"\n[scale] rebuild_unified_kg {N} concepts: {dt:.2f}s, clusters={n_clusters}"
    )


def test_scratch_seed_and_cluster_swap_keep_store_seams(repo, monkeypatch):
    """Pins the store-seam sequence + connection-type contract of
    rebuild_unified_kg's two scratch tables.

    Updated for write-lock slimming improvement point 2 (design doc §5.5):
    _write_cluster_map_streamed no longer streams kg_cluster_scratch rows
    through a cross-connection cursor into a Python-built INSERT
    (stream_scratch_rows / replace_cluster_rows_streamed, both removed —
    no callers left once this test stopped exercising them). It now stages
    the seed->canonical mapping into kg_canonical_scratch (a batched
    preparation segment, mirroring insert_scratch_rows below) and then
    writes the in-flight generation's concept_clusters rows in one pure-SQL
    INSERT...SELECT (write_cluster_map_generation — 批 3·W2:无 DELETE、无
    advisory lock,四列唯一按代隔离) that joins the two scratch tables —
    this test's job is to keep pinning THAT sequence instead.
    """
    import sqlite3
    from contextlib import contextmanager
    runtime = object.__getattribute__(repo, "_runtime")
    nb = getattr(repo, "create_notebook")(NotebookCreate(name="stream delegation"))
    getattr(repo, "store_kg")(nb.id, None, [{
        "local_id": "a", "object_type": "concept",
        "payload": {"name": "MOSFET", "section_path": ""}, "evidence": [],
    }], [])
    events = []
    opened_reads, opened_writes = set(), set()
    original_connect = runtime.database.connect
    original_write = runtime.database.write

    def traced_connect():
        db = original_connect()
        opened_reads.add(id(db))
        return db

    @contextmanager
    def traced_write():
        with original_write() as db:
            opened_writes.add(id(db))
            events.append(("write.begin", id(db)))
            yield db
        events.append(("write.commit", id(db)))

    monkeypatch.setattr(runtime.database, "connect", traced_connect)
    monkeypatch.setattr(runtime.database, "write", traced_write)
    store = runtime.unified_kg

    def spy(name, *, cursor=False):
        original = getattr(store, name)

        def wrapped(db, *args, **kwargs):
            assert isinstance(db, sqlite3.Connection)
            result = original(db, *args, **kwargs)
            if cursor:
                assert isinstance(result, sqlite3.Cursor)
            events.append((name, id(db)))
            return result

        monkeypatch.setattr(store, name, wrapped)

    for name, cursor in (
        ("clear_scratch_run", False), ("seed_payload_rows", True),
        ("stream_seed_rows", True), ("insert_scratch_rows", False),
        ("scratch_vector_rows", True),
        ("clear_canonical_scratch_run", False),
        ("insert_canonical_scratch_rows", False),
        ("write_cluster_map_generation", False),
    ):
        spy(name, cursor=cursor)
    progress = []

    getattr(repo, "rebuild_unified_kg")(
        nb.id, force=True,
        progress=lambda stage, current, total: progress.append((stage, current, total)),
    )

    names = [event[0] for event in events]
    assert names.index("clear_scratch_run") < names.index("seed_payload_rows")
    assert names.index("seed_payload_rows") < names.index("stream_seed_rows")
    assert names.index("stream_seed_rows") < names.index("insert_scratch_rows")
    assert names.index("insert_scratch_rows") < names.index("scratch_vector_rows")
    assert names.index("scratch_vector_rows") < names.index("clear_canonical_scratch_run")
    assert names.index("clear_canonical_scratch_run") < names.index("insert_canonical_scratch_rows")
    assert names.index("insert_canonical_scratch_rows") < names.index("write_cluster_map_generation")
    for name, db_id, *_ in events:
        if name in {
            "clear_scratch_run", "insert_scratch_rows",
            "clear_canonical_scratch_run", "insert_canonical_scratch_rows",
            "write_cluster_map_generation",
        }:
            assert db_id in opened_writes
        elif name not in {"write.begin", "write.commit"}:
            assert db_id in opened_reads
    assert progress
