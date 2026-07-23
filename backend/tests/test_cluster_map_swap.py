"""写锁瘦身改造点 2(design doc §5.5):_write_cluster_map_streamed 的预备段
(kg_canonical_scratch,分批提交)+ 切换段(纯 SQL DELETE+INSERT...SELECT,
swap_cluster_map_from_scratch)。

风格与 fixture 镜像 test_write_lock_scratch_batching.py(改造点 1 的姊妹测试
文件)——同样用 repo.rebuild_unified_kg 的公开路径覆盖端到端行为,用直接调用
_write_cluster_map_streamed + 手工造 scratch 行覆盖只有构造特定输入才测得出的
边界(seed 无 canonical、type 为空、run_id 隔离、插入顺序)。
"""
import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _lifecycle(repo):
    return repo._runtime.knowledge_lifecycle


def _insert_object_scratch(repo, notebook_id, run_id, rows):
    """rows: list of (object_id, seed) -> kg_cluster_scratch (the OTHER
    scratch table, driving side of the swap's join). Mirrors the raw-SQL
    seeding style already used by test_rebuild_streaming.py for this table."""
    with repo._write() as db:
        db.executemany(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) "
            "VALUES (?,?,?,?)",
            [(notebook_id, run_id, oid, seed) for oid, seed in rows],
        )


def _cluster_rows(repo, notebook_id, object_type):
    with repo._connect() as db:
        return db.execute(
            "SELECT id, canonical_id, member_object_id, canonical_name, "
            "canonical_description, canonical_desc_sig FROM concept_clusters "
            "WHERE notebook_id=? AND object_type=? ORDER BY rowid",
            (notebook_id, object_type),
        ).fetchall()


# --------------------------------------------------------------- invariants

def test_seed_with_no_canonical_entry_is_skipped(repo):
    """Today's code skips a member object whose seed has no canonical entry
    (a defensive `if cid is None: continue`). The swap must reproduce this
    with an INNER JOIN, not a LEFT JOIN: 'obj-orphan's seed has no entry in
    seed_to_canonical (and therefore none in kg_canonical_scratch either)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    run_id = "run-skip"
    _insert_object_scratch(repo, nb.id, run_id, [
        ("obj-has-canonical", "seed-a"),
        ("obj-orphan", "seed-orphan"),
    ])
    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept",
        seed_to_canonical={"seed-a": "K-a"},  # 'seed-orphan' deliberately absent
        canonical_names={"K-a": "Alpha"},
        run_id=run_id,
    )
    rows = _cluster_rows(repo, nb.id, "concept")
    assert [r["member_object_id"] for r in rows] == ["obj-has-canonical"]
    assert rows[0]["canonical_id"] == "K-a"
    assert rows[0]["canonical_name"] == "Alpha"


def test_empty_object_type_still_clears_existing_rows(repo):
    """An empty seed_to_canonical dict (this type clustered down to zero
    members) must still DELETE whatever concept_clusters rows already
    existed for (notebook_id, object_type) -- the DELETE is unconditional,
    the INSERT...SELECT simply matches zero rows."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters (id, notebook_id, canonical_id, "
            "member_object_id, canonical_name, object_type, created_at) "
            "VALUES ('cc-stale', ?, 'K-old', 'obj-old', 'Old', 'concept', 't0')",
            (nb.id,),
        )
    assert _cluster_rows(repo, nb.id, "concept"), "sanity: pre-existing row must be there"

    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept", seed_to_canonical={}, canonical_names={}, run_id="run-empty",
    )
    assert _cluster_rows(repo, nb.id, "concept") == []


def test_canonical_description_and_desc_sig_flow_through_the_swap(repo):
    """desc_by_cid / desc_sig_by_cid (populated by the concept-description
    LLM stage) must reach concept_clusters unchanged through the new
    prep+swap path -- these two columns are carried by kg_canonical_scratch,
    not kg_cluster_scratch, so they are the columns most likely to go
    missing in a refactor that didn't thread them through correctly."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    run_id = "run-desc"
    _insert_object_scratch(repo, nb.id, run_id, [("obj-1", "seed-1")])
    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept",
        seed_to_canonical={"seed-1": "K-1"},
        canonical_names={"K-1": "One"},
        desc_by_cid={"K-1": "a fused description"},
        desc_sig_by_cid={"K-1": "sig-abc"},
        run_id=run_id,
    )
    rows = _cluster_rows(repo, nb.id, "concept")
    assert len(rows) == 1
    assert rows[0]["canonical_description"] == "a fused description"
    assert rows[0]["canonical_desc_sig"] == "sig-abc"


def test_swap_preserves_scratch_insertion_order(repo):
    """ORDER BY s.rowid in the swap's INSERT...SELECT must reproduce
    kg_cluster_scratch's insertion order in concept_clusters -- PR#136: a
    plan/index change must never silently reorder cluster rows. Input is
    deliberately NOT alphabetical / NOT grouped by seed, so a plan that
    sorted by seed or by member_object_id would visibly reorder this."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    run_id = "run-order"
    _insert_object_scratch(repo, nb.id, run_id, [
        ("obj-z", "seed-1"), ("obj-a", "seed-2"),
        ("obj-m", "seed-1"), ("obj-b", "seed-2"),
    ])
    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept",
        seed_to_canonical={"seed-1": "K-1", "seed-2": "K-2"},
        canonical_names={"K-1": "One", "K-2": "Two"},
        run_id=run_id,
    )
    with repo._connect() as db:
        ordered = [r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters "
            "WHERE notebook_id=? AND object_type='concept' ORDER BY rowid",
            (nb.id,))]
    assert ordered == ["obj-z", "obj-a", "obj-m", "obj-b"]


def test_canonical_scratch_run_id_isolation(repo):
    """A stray kg_canonical_scratch row under a DIFFERENT run_id survives a
    _write_cluster_map_streamed call untouched -- concurrent rebuilds of the
    same notebook never cross via this table either (mirrors
    test_concurrent_rebuild_scratch_isolated's contract for the sibling
    kg_cluster_scratch table)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    other_run = "other-run"
    with repo._write() as db:
        db.execute(
            "INSERT INTO kg_canonical_scratch (notebook_id, run_id, seed, "
            "canonical_id, canonical_name, canonical_description, canonical_desc_sig) "
            "VALUES (?,?,?,?,?,?,?)",
            (nb.id, other_run, "seed-x", "K-x", "X", "", ""),
        )

    my_run = "my-run"
    _insert_object_scratch(repo, nb.id, my_run, [("obj-1", "seed-1")])
    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept",
        seed_to_canonical={"seed-1": "K-1"},
        canonical_names={"K-1": "One"},
        run_id=my_run,
    )

    with repo._connect() as db:
        other_left = db.execute(
            "SELECT COUNT(*) AS c FROM kg_canonical_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, other_run)).fetchone()["c"]
    assert other_left == 1, "a different run_id's row must survive untouched"


def test_second_type_call_does_not_see_previous_types_canonical_scratch_rows(repo):
    """Within ONE run_id, _write_cluster_map_streamed is called once per
    object_type (concept, then claim, formula, procedure). A second type's
    call must not let the FIRST type's seed->canonical rows leak into its
    own swap join, even when a seed string collides across types --
    _write_cluster_map_streamed clears its own run's kg_canonical_scratch
    rows at the START of every call for exactly this reason (mirrors
    _stream_seed_reps clearing kg_cluster_scratch at the start of every
    type's pass)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    run_id = "shared-run"
    lifecycle = _lifecycle(repo)

    # Pass 1: "concept" type, seed "shared-seed" -> K-concept.
    _insert_object_scratch(repo, nb.id, run_id, [("obj-concept", "shared-seed")])
    lifecycle._write_cluster_map_streamed(
        nb.id, "concept",
        seed_to_canonical={"shared-seed": "K-concept"},
        canonical_names={"K-concept": "ConceptName"},
        run_id=run_id,
    )
    assert _cluster_rows(repo, nb.id, "concept"), "sanity: pass 1 must have written a row"

    # Pass 2: "claim" type reuses the SAME seed string but must NOT pick up
    # pass 1's canonical mapping -- if a stale kg_canonical_scratch row from
    # pass 1 survived, claim's (empty) swap join would wrongly resurrect a
    # concept_clusters row via the shared seed string.
    with repo._write() as db:
        db.execute("DELETE FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
                   (nb.id, run_id))
    _insert_object_scratch(repo, nb.id, run_id, [("obj-claim", "shared-seed")])
    lifecycle._write_cluster_map_streamed(
        nb.id, "claim", seed_to_canonical={}, canonical_names={}, run_id=run_id,
    )

    claim_rows = _cluster_rows(repo, nb.id, "claim")
    assert claim_rows == [], (
        "claim's empty seed_to_canonical must produce zero rows -- a stale "
        "kg_canonical_scratch row from the concept pass would wrongly "
        f"resurrect one via the shared seed string: {claim_rows}")
    # Pass 1's own concept_clusters row must be untouched by pass 2 (disjoint
    # object_type scope).
    assert _cluster_rows(repo, nb.id, "concept")


def test_canonical_scratch_empty_after_successful_rebuild(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id, force=True)
    with repo._connect() as db:
        left = db.execute(
            "SELECT COUNT(*) AS c FROM kg_canonical_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert left == 0


def test_canonical_scratch_empty_after_rebuild_that_raises_partway(repo, monkeypatch):
    """Mirrors test_interrupted_rebuild_clears_only_its_own_run_scratch_rows
    in test_rebuild_streaming.py, for the NEW kg_canonical_scratch table: a
    crash AFTER the concept type's prep+swap has already committed rows
    (proving they're durable, not rolled back) but before the whole rebuild
    finishes must still leave zero of this run's kg_canonical_scratch rows,
    via rebuild_unified_kg's outer finally.

    The crash is injected in seed_claim, which only runs (inside
    _stream_seed_reps's Pass A2) if the notebook has at least one 'claim'
    object -- concept is clustered+written FIRST, so by the time seed_claim
    fires, concept's own prep+swap has already fully committed."""
    from app.services import kg_merge

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "BJT", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "claim",
         "payload": {"name": "gain is high", "section_path": ""}, "evidence": []},
    ], [])

    def _boom(*args, **kwargs):
        raise RuntimeError("injected failure after concept pass committed")

    monkeypatch.setattr(kg_merge, "seed_claim", _boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        repo.rebuild_unified_kg(nb.id, force=True)

    with repo._connect() as db:
        left = db.execute(
            "SELECT COUNT(*) AS c FROM kg_canonical_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert left == 0, "crashed rebuild must leave zero kg_canonical_scratch rows"
    assert _cluster_rows(repo, nb.id, "concept"), (
        "concept type's already-swapped concept_clusters rows must survive "
        "a LATER crash in a different type's pass")


# ------------------------------------------------- write-lock-slimming shape

def test_canonical_scratch_staging_uses_a_fresh_transaction_per_batch(repo, monkeypatch):
    """Mirrors test_scratch_staging_uses_a_fresh_transaction_per_batch (for
    kg_cluster_scratch) in test_write_lock_scratch_batching.py -- the
    SIBLING preparation loop for kg_canonical_scratch must ALSO commit each
    batch on its own write connection, not accumulate the whole
    seed_to_canonical dict into one transaction. Judged by write-connection
    identity, not timing."""
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    seen_conns = []
    original = UnifiedKgStore.insert_canonical_scratch_rows

    def _spy(db, rows):
        seen_conns.append(db)
        return original(db, rows)

    monkeypatch.setattr(UnifiedKgStore, "insert_canonical_scratch_rows",
                        staticmethod(_spy))

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    run_id = "run-many-seeds"
    # 2500 DISTINCT seeds -> 3 prep batches at the 1000-row boundary.
    n = 2_500
    _insert_object_scratch(repo, nb.id, run_id,
                           [(f"obj-{i}", f"seed-{i}") for i in range(n)])
    seed_to_canonical = {f"seed-{i}": f"K-{i}" for i in range(n)}
    canonical_names = {f"K-{i}": f"Name{i}" for i in range(n)}

    _lifecycle(repo)._write_cluster_map_streamed(
        nb.id, "concept", seed_to_canonical, canonical_names, run_id=run_id,
    )

    assert len(seen_conns) == 3, (
        "2500 seeds at batch size 1000 should produce 3 prep batches", len(seen_conns))
    assert len({id(c) for c in seen_conns}) == len(seen_conns), (
        "multiple prep batches shared one write connection = still one big transaction",
        len(seen_conns), len({id(c) for c in seen_conns}))


def test_canonical_scratch_row_construction_runs_outside_the_write_transaction(repo, monkeypatch):
    """Mirrors test_seed_computation_runs_outside_the_write_transaction in
    test_write_lock_scratch_batching.py, for the SIBLING preparation loop:
    _canonical_scratch_row (the per-seed row-construction hook for
    kg_canonical_scratch) must never run while a SqliteDatabase.write()
    block is open -- that is the entire point of improvement point 2 (the
    Python work that used to happen while the write lock was held must now
    happen between batches, off the lock).

    Verified this guard is effective the same way the sibling test's
    docstring documents: temporarily moved the _canonical_scratch_row(...)
    call back inside the `with self._write() as wdb:` swap block and
    confirmed this test goes red, then reverted (see mutation-verification
    in the task report)."""
    import threading
    from contextlib import contextmanager

    from app.repositories.sqlite.database import SqliteDatabase
    from app.services import knowledge_lifecycle

    flag = threading.local()
    original_write = SqliteDatabase.write

    @contextmanager
    def traced_write(self):
        with original_write(self) as conn:
            flag.inside_write = True
            try:
                yield conn
            finally:
                flag.inside_write = False

    monkeypatch.setattr(SqliteDatabase, "write", traced_write)

    original_row = knowledge_lifecycle._canonical_scratch_row
    calls = []

    def guarded_row(*args, **kwargs):
        calls.append(1)
        assert not getattr(flag, "inside_write", False), (
            "_canonical_scratch_row ran INSIDE a SqliteDatabase.write() block -- "
            "the preparation segment's per-seed construction must stay outside "
            "the write transaction")
        return original_row(*args, **kwargs)

    monkeypatch.setattr(knowledge_lifecycle, "_canonical_scratch_row", guarded_row)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": f"o{i}", "object_type": "concept",
         "payload": {"name": f"c-{i}", "section_path": ""}, "evidence": []}
        for i in range(50)
    ], [])
    repo.rebuild_unified_kg(nb.id, force=True)

    assert calls, "_canonical_scratch_row was never called -- this test would pass vacuously"


def test_swap_and_bump_seq_share_one_write_transaction(repo, monkeypatch):
    """The swap segment (DELETE+INSERT...SELECT, swap_cluster_map_from_scratch)
    and the cluster_mutation_seq bump that follows it must land in the SAME
    write() transaction -- judged by write-connection identity (mirrors
    test_scratch_staging_uses_a_fresh_transaction_per_batch's technique),
    not by timing. A regression that split them into two self._write()
    blocks would still "work" functionally but would break the
    all-or-nothing atomicity P0-A relies on (see the comment at
    _write_cluster_map_streamed's bump call: concept_clusters and
    cluster_mutation_seq must move in the same commit, or a crash between
    the two could leave the DELETE+INSERT visible without the version bump
    that is supposed to signal it).

    Verified effective: temporarily split the swap call and the bump call
    into two separate `with self._write()` blocks and confirmed this test
    goes red, then reverted (see mutation-verification in the task report)."""
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    swap_conns = []
    original_swap = UnifiedKgStore.swap_cluster_map_from_scratch

    def _spy_swap(db, *args, **kwargs):
        swap_conns.append(db)
        return original_swap(db, *args, **kwargs)

    monkeypatch.setattr(UnifiedKgStore, "swap_cluster_map_from_scratch",
                        staticmethod(_spy_swap))

    lifecycle = _lifecycle(repo)
    bump_conns = []
    original_bump = lifecycle._bump_cluster_mutation_seq

    def _spy_bump(db, notebook_id):
        bump_conns.append(db)
        return original_bump(db, notebook_id)

    monkeypatch.setattr(lifecycle, "_bump_cluster_mutation_seq", _spy_bump)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id, force=True)

    assert swap_conns, "swap_cluster_map_from_scratch was never called"
    # One call per object_type this rebuild processes: concept + the three
    # _TYPE_MERGE entries (claim, formula, procedure) = 4.
    assert len(swap_conns) == len(bump_conns) == 4, (swap_conns, bump_conns)
    for swap_db, bump_db in zip(swap_conns, bump_conns):
        assert id(swap_db) == id(bump_db), (
            "swap_cluster_map_from_scratch and its cluster_mutation_seq bump "
            "ran on DIFFERENT write connections -- they must share one "
            "transaction")
