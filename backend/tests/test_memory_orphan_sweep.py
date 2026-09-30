"""Post-readiness sweep of ownerless Memory sources (plan 2026-09-29 E5-3, audit N-5), SQLite.

Real repository, real ``delete_source`` path, real notebook copy -- nothing in the
delete cascade is replaced. The N-5 orphan is produced by the real deep copy
(which clears ``sources.memory_id`` and keeps ``source_type = 'memory'``); the
hard-delete residue by the real ``MemoryStore.delete_memory`` (which never tears
the derived source down); a non-confirmed Memory and a Memory row that never
existed are seeded directly.

Pinned: after a pass every orphan source AND every row derived from it is gone
while a healthy Memory's source, a normal document and a Knowhow source keep all
theirs; the sweep is idempotent (second pass: no job, no event, zero counts); a
page of 1 and the default page give the same end state; a failure on one source is
isolated and content-free, a systemic failure ends the pass and leaves the rest
for the next start; nothing runs before readiness; the startup hook sits after
``mark_ready`` and never raises; checkup's read-only H12 counts the notebook's
remaining orphans; the SQLite plans of the two statements.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap
import time

import pytest

from app.core import readiness
from app.core.config import Settings
from app.repositories.sqlite import memory_sql
from app.services import background_jobs, startup_warmup
from app.services import memory_orphan_sweep as sweep_module
from app.services.memory_orphan_sweep import (
    MAX_CONSECUTIVE_FAILURES,
    ORPHAN_SWEEP_PAGE_SIZE,
    MemoryOrphanSweep,
)
from app.services.sqlite_repository import SQLiteRepository

NOW = "2026-09-30T00:00:00+00:00"
OWNER = "user-e53-owner"
RECIPIENT = "user-e53-recipient"


def _repo(tmp_path, monkeypatch, name="e53"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / (name + '.db')}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / (name + "-storage")))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _repo(tmp_path, monkeypatch)


@pytest.fixture
def events(repo, monkeypatch):
    """Record what the service emits while still emitting it (observation,
    not replacement)."""
    seen: list[dict] = []
    log = repo._runtime.event_log
    original = log.emit

    def recording(event, *args, **kwargs):
        seen.append(dict(event))
        return original(event, *args, **kwargs)

    monkeypatch.setattr(log, "emit", recording)
    return seen


def _sweep_events(events):
    return [e for e in events if str(e.get("kind", "")).startswith("memory_orphan_sweep_")]


# ----------------------------------------------------------------- fixtures
def _write(repo, sql, params=()):
    with repo._runtime.database.write() as db:
        db.execute(sql, params)


def _user(repo, user_id):
    _write(
        repo,
        "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, f"{user_id}@e.test", user_id, "user", NOW, NOW),
    )


def _notebook(repo, notebook_id, owner=OWNER):
    _write(
        repo,
        "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (notebook_id, notebook_id, "", "Semiconductor", "draft", owner, NOW, NOW),
    )


def _memory_item(repo, memory_id, notebook_id, status="confirmed"):
    _write(
        repo,
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (memory_id, notebook_id, OWNER, "ask_answer", status, memory_id, "x", NOW, NOW),
    )


def _source(repo, notebook_id, source_id, source_type, memory_id=None):
    repo._runtime.source_store.insert_source(
        source_id=source_id,
        notebook_id=notebook_id,
        title=f"T {source_id}",
        source_type=source_type,
        status="extracted",
        parse_status="extracted",
        file_name="",
        file_path="",
        file_size=0,
        file_hash="",
        summary="",
        doc_type=source_type if source_type in ("memory", "knowhow") else "",
        memory_id=memory_id,
    )
    if memory_id is None:
        # insert_source normalises "no link" to ''; the N-5 / NULL shape is a raw NULL.
        _write(repo, "UPDATE sources SET memory_id = NULL WHERE id = ?", (source_id,))


def _derive(repo, notebook_id, source_id):
    """Every kind of row a Memory source's extraction leaves behind."""
    t = source_id
    with repo._runtime.database.write() as db:
        for n in (1, 2):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,created_at) VALUES (?,?,?,?,?,?)",
                (f"el-{t}-{n}", source_id, "paragraph", f"p{n}", f"text {n}", NOW),
            )
        db.execute(
            "INSERT INTO element_embeddings (element_id,source_id,notebook_id,vector,"
            "created_at) VALUES (?,?,?,?,?)",
            (f"el-{t}-1", source_id, notebook_id, "[0.1,0.2]", NOW),
        )
        for n in (1, 2):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,evidence,"
                "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"ko-{t}-{n}", notebook_id, "concept",
                    json.dumps({"name": f"concept {t} {n}"}),
                    json.dumps([{"source_id": source_id, "element_id": f"el-{t}-1"}]),
                    source_id, NOW, NOW,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (?,?,?)",
                (f"ko-{t}-{n}", source_id, notebook_id),
            )
            db.execute(
                "INSERT INTO kg_objects_fts (object_id,notebook_id,name) VALUES (?,?,?)",
                (f"ko-{t}-{n}", notebook_id, f"concept {t} {n}"),
            )
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (f"ko-{t}-1", notebook_id, "[0.1,0.2]", NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) VALUES (?,?,?,?,?,?,?)",
            (f"kr-{t}", notebook_id, source_id, f"ko-{t}-1", f"ko-{t}-2", "relates", NOW),
        )
        db.execute(
            "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (f"kr-{t}", notebook_id, "[0.1,0.2]", NOW),
        )
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,created_at) VALUES (?,?,?,?,?,?)",
            (f"cc-{t}", notebook_id, f"can-{t}", f"ko-{t}-1", f"concept {t} 1", NOW),
        )
        # a Memory source is never chunked in real life; the cascade must still be
        # complete if one ever is (chunk rows hang off the source by foreign key)
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
            "VALUES (?,?,?,?,?)",
            (f"ch-{t}", notebook_id, source_id, f"chunk text {t}", NOW),
        )
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            (f"ch-{t}", notebook_id, "[0.1,0.2]", NOW),
        )
        db.execute(
            "INSERT INTO chunk_elements (notebook_id,element_id,chunk_id) VALUES (?,?,?)",
            (notebook_id, f"el-{t}-1", f"ch-{t}"),
        )
        db.execute(
            "INSERT INTO chunks_fts (chunk_id,notebook_id,text) VALUES (?,?,?)",
            (f"ch-{t}", notebook_id, f"chunk text {t}"),
        )
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (f"er-{t}", notebook_id, source_id, "kg", "completed", NOW, NOW),
        )


# (table, key column, how to find this source's rows BEFORE any deletion)
_DERIVED = (
    ("source_elements", "id", "SELECT id FROM source_elements WHERE source_id=?"),
    ("element_embeddings", "element_id", "SELECT element_id FROM element_embeddings WHERE source_id=?"),
    ("knowledge_objects", "id", "SELECT id FROM knowledge_objects WHERE source_id=?"),
    ("knowledge_object_sources", "object_id", "SELECT object_id FROM knowledge_object_sources WHERE source_id=?"),
    ("kg_objects_fts", "object_id", "SELECT object_id FROM kg_objects_fts WHERE object_id IN "
                                    "(SELECT id FROM knowledge_objects WHERE source_id=?)"),
    ("knowledge_embeddings", "object_id", "SELECT object_id FROM knowledge_embeddings WHERE object_id IN "
                                          "(SELECT id FROM knowledge_objects WHERE source_id=?)"),
    ("knowledge_relations", "id", "SELECT id FROM knowledge_relations WHERE source_id=?"),
    ("relation_embeddings", "relation_id", "SELECT relation_id FROM relation_embeddings WHERE relation_id IN "
                                           "(SELECT id FROM knowledge_relations WHERE source_id=?)"),
    ("concept_clusters", "member_object_id", "SELECT member_object_id FROM concept_clusters WHERE member_object_id IN "
                                              "(SELECT id FROM knowledge_objects WHERE source_id=?)"),
    ("extraction_runs", "id", "SELECT id FROM extraction_runs WHERE source_id=?"),
    ("chunks", "id", "SELECT id FROM chunks WHERE source_id=?"),
    ("chunk_embeddings", "chunk_id", "SELECT chunk_id FROM chunk_embeddings WHERE chunk_id IN "
                                     "(SELECT id FROM chunks WHERE source_id=?)"),
    ("chunk_elements", "chunk_id", "SELECT chunk_id FROM chunk_elements WHERE chunk_id IN "
                                   "(SELECT id FROM chunks WHERE source_id=?)"),
    ("chunks_fts", "chunk_id", "SELECT chunk_id FROM chunks_fts WHERE chunk_id IN "
                               "(SELECT id FROM chunks WHERE source_id=?)"),
)


def _capture(repo, source_id):
    """The keys of every derived row of ``source_id`` as they are NOW."""
    out = {}
    with repo._runtime.database.connect() as db:
        for table, _key, find in _DERIVED:
            out[table] = [r[0] for r in db.execute(find, (source_id,)).fetchall()]
    return out


def _alive(repo, captured):
    """How many of the captured derived rows still exist, per table."""
    out = {}
    with repo._runtime.database.connect() as db:
        for table, key, _find in _DERIVED:
            ids = captured[table]
            out[table] = sum(
                db.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=?", (i,)).fetchone()[0]
                for i in ids
            )
    return out


def _source_ids(repo):
    with repo._runtime.database.connect() as db:
        return sorted(r[0] for r in db.execute("SELECT id FROM sources").fetchall())


class World:
    """One deployment: a healthy Memory, every orphan shape, and the controls."""

    def __init__(self, repo):
        self.repo = repo
        _user(repo, OWNER)
        _user(repo, RECIPIENT)
        _notebook(repo, "nb-a")
        # ---- controls: none of these may be touched
        _memory_item(repo, "mem-keep", "nb-a")
        _source(repo, "nb-a", "src-keep", "memory", "mem-keep")
        _source(repo, "nb-a", "src-doc", "markdown")
        _source(repo, "nb-a", "src-knowhow", "knowhow")
        # ---- orphans
        # hard-delete residue: the real store delete leaves the derived source behind
        _memory_item(repo, "mem-hard", "nb-a")
        _source(repo, "nb-a", "src-hard", "memory", "mem-hard")
        repo._runtime.memory_store.delete_memory("mem-hard", OWNER)
        # a Memory row that is not confirmed any more
        _memory_item(repo, "mem-dep", "nb-a", status="deprecated")
        _source(repo, "nb-a", "src-dep", "memory", "mem-dep")
        # a link to a Memory row that never existed
        _source(repo, "nb-a", "src-dangling", "memory", "mem-never")
        for source_id in ("src-keep", "src-doc", "src-hard", "src-dep", "src-dangling"):
            _derive(repo, "nb-a", source_id)
        # ---- N-5: the real deep copy clears memory_id and keeps source_type = 'memory'
        copy = repo.copy_notebook("nb-a", new_owner_id=RECIPIENT)
        self.copy_id = copy.id
        with repo._runtime.database.connect() as db:
            rows = db.execute(
                "SELECT id, source_type, memory_id FROM sources WHERE notebook_id=? "
                "AND source_type='memory' ORDER BY id", (self.copy_id,),
            ).fetchall()
        self.n5_ids = [r["id"] for r in rows]
        assert self.n5_ids and all(not r["memory_id"] for r in rows), rows
        # a raw NULL memory_id (the other "no link" spelling)
        _source(repo, "nb-a", "src-null", "memory", None)
        _derive(repo, "nb-a", "src-null")
        # (derived after the copy: the copy skips a Knowhow source's objects but not
        # their vectors, which is not what this file is about)
        _derive(repo, "nb-a", "src-knowhow")
        self.orphan_ids = sorted(
            ["src-hard", "src-dep", "src-dangling", "src-null", *self.n5_ids]
        )
        self.control_ids = ["src-keep", "src-doc", "src-knowhow"]
        self.captured = {sid: _capture(repo, sid) for sid in self.orphan_ids + self.control_ids}
        # the copied Memory sources carry copied derived rows too
        for sid in self.n5_ids:
            assert self.captured[sid]["source_elements"], sid
            assert self.captured[sid]["knowledge_objects"], sid

    def orphans_alive(self):
        return {sid: _alive(self.repo, self.captured[sid]) for sid in self.orphan_ids}


@pytest.fixture
def world(repo):
    return World(repo)


# SQLite's two FTS5 shadow tables are the derived tables no online delete path cleans:
# ``knowledge_store._delete_object_id_batch`` excludes ``kg_objects_fts`` on purpose
# and ``chunks_fts`` has no foreign key to hang a cascade on; the next notebook FTS
# rebuild / graph delete sweeps them. PostgreSQL has no such shadows. What they still
# hold points at a parent row that no longer exists.
_SQLITE_FTS_SHADOWS = {
    "kg_objects_fts": "SELECT COUNT(*) FROM knowledge_objects WHERE id=?",
    "chunks_fts": "SELECT COUNT(*) FROM chunks WHERE id=?",
}


def _assert_swept(world):
    ids = _source_ids(world.repo)
    for sid in world.orphan_ids:
        assert sid not in ids, sid
    for sid, alive in world.orphans_alive().items():
        assert not any(n for table, n in alive.items() if table not in _SQLITE_FTS_SHADOWS), (sid, alive)
        with world.repo._runtime.database.connect() as db:
            for table, parent_count in _SQLITE_FTS_SHADOWS.items():
                for key in world.captured[sid][table]:
                    assert db.execute(parent_count, (key,)).fetchone()[0] == 0, (sid, table, key)
    # the controls kept the source AND every derived row
    for sid in world.control_ids:
        assert sid in ids, sid
        alive = _alive(world.repo, world.captured[sid])
        assert alive == {t: len(world.captured[sid][t]) for t in alive}, (sid, alive)
        assert any(alive.values())


# --------------------------------------------------------------------- tests
def test_the_fixture_really_holds_every_orphan_shape(world):
    """Guards the guard: each shape is an orphan by the store's own predicate, the
    controls are not, and every orphan has derived rows in every table."""
    store = world.repo._runtime.memory_store
    assert store.orphan_memory_source_ids(1000) == world.orphan_ids
    for sid in ["src-hard", "src-dep", "src-dangling", "src-null"]:
        assert all(world.captured[sid][t] for t, _k, _f in _DERIVED), sid


def test_a_pass_removes_every_orphan_and_everything_derived_from_it(world, events):
    tally = MemoryOrphanSweep.for_repository(world.repo).run_pass()

    assert tally == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 0}
    _assert_swept(world)
    assert world.repo._runtime.memory_store.orphan_memory_source_ids(1000) == []
    kinds = [e["kind"] for e in _sweep_events(events)]
    assert kinds == ["memory_orphan_sweep_started", "memory_orphan_sweep_completed"]
    assert _sweep_events(events)[-1] == {
        "kind": "memory_orphan_sweep_completed",
        "deleted": len(world.orphan_ids), "gone": 0, "failed": 0,
    }


def test_the_sweep_is_idempotent_and_a_clean_start_does_nothing(world, events):
    MemoryOrphanSweep.for_repository(world.repo).run_pass()
    events.clear()
    before = _source_ids(world.repo)

    again = MemoryOrphanSweep.for_repository(world.repo)  # "next start"
    assert again.has_orphans() is False
    assert again.schedule() is None  # no job for nothing to do
    assert again.run_pass() == {"deleted": 0, "gone": 0, "failed": 0}
    assert _source_ids(world.repo) == before
    assert _sweep_events(events) == []


def _row_counts(repo):
    """Row counts of ``sources`` and of every derived table: ids differ between two
    databases (the copy's ids are random), counts do not."""
    tables = ["sources", *(table for table, _k, _f in _DERIVED)]
    with repo._runtime.database.connect() as db:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def test_a_page_of_one_gives_the_same_end_state_as_the_default_page(
    tmp_path, monkeypatch
):
    def run(name, page_size):
        repo = _repo(tmp_path, monkeypatch, name)
        world = World(repo)
        reads = []
        store = repo._runtime.memory_store
        original = store.orphan_memory_source_ids

        def counting(limit, after_id=""):
            page = original(limit, after_id)
            reads.append((limit, len(page)))
            return page

        monkeypatch.setattr(store, "orphan_memory_source_ids", counting)
        kwargs = {} if page_size is None else {"page_size": page_size}
        tally = MemoryOrphanSweep.for_repository(repo, **kwargs).run_pass()
        _assert_swept(world)
        return tally, _row_counts(repo), reads, len(world.orphan_ids)

    small_tally, small_counts, small_reads, orphans = run("small", 1)
    big_tally, big_counts, big_reads, _same = run("big", None)

    assert small_tally == big_tally == {"deleted": orphans, "gone": 0, "failed": 0}
    assert small_counts == big_counts
    # the page size really did change the number of reads, never the result
    assert all(limit == 1 for limit, _n in small_reads)
    assert len(small_reads) == orphans + 1  # one read per orphan, then the empty read
    assert all(limit == ORPHAN_SWEEP_PAGE_SIZE for limit, _n in big_reads)
    assert len(big_reads) == 2  # one full page, then the empty read that ends it


def test_the_page_size_is_a_named_protocol_constant():
    assert isinstance(ORPHAN_SWEEP_PAGE_SIZE, int) and ORPHAN_SWEEP_PAGE_SIZE > 1
    default = inspect.signature(MemoryOrphanSweep.__init__).parameters["page_size"].default
    assert default == ORPHAN_SWEEP_PAGE_SIZE
    assert MAX_CONSECUTIVE_FAILURES >= 2


def test_a_source_that_fails_is_isolated_and_the_next_start_finishes_it(world, events):
    repo = world.repo
    victim = world.orphan_ids[2]

    def flaky(source_id):
        if source_id == victim:
            raise RuntimeError("disk on fire: secret title")
        return repo.delete_source(source_id)

    tally = MemoryOrphanSweep(
        store=repo._runtime.memory_store, delete_source=flaky,
        event_log=repo._runtime.event_log,
    ).run_pass()

    assert tally == {"deleted": len(world.orphan_ids) - 1, "gone": 0, "failed": 1}
    assert _source_ids(repo).count(victim) == 1
    assert any(
        n for table, n in _alive(repo, world.captured[victim]).items() if table not in _SQLITE_FTS_SHADOWS
    )  # the failed source kept its derived rows: nothing was half-deleted
    failed = [e for e in _sweep_events(events) if e["kind"] == "memory_orphan_sweep_failed"]
    assert failed == [{
        "kind": "memory_orphan_sweep_failed",
        "source_id": victim, "error_class": "RuntimeError",
    }]
    assert "disk on fire" not in json.dumps(_sweep_events(events))

    events.clear()
    retry = MemoryOrphanSweep.for_repository(repo).run_pass()  # the next start
    assert retry == {"deleted": 1, "gone": 0, "failed": 0}
    _assert_swept(world)


def test_a_systemic_failure_ends_the_pass_and_leaves_the_rest_for_the_next_start(
    world, events
):
    repo = world.repo
    calls = []

    def broken(source_id):
        calls.append(source_id)
        raise ConnectionError("database away")

    tally = MemoryOrphanSweep(
        store=repo._runtime.memory_store, delete_source=broken,
        event_log=repo._runtime.event_log,
    ).run_pass()

    assert len(calls) == MAX_CONSECUTIVE_FAILURES  # stopped, did not fail one by one
    assert tally == {"deleted": 0, "gone": 0, "failed": MAX_CONSECUTIVE_FAILURES}
    assert len(world.orphan_ids) > MAX_CONSECUTIVE_FAILURES
    assert set(world.orphan_ids) <= set(_source_ids(repo))  # everything left in place

    retry = MemoryOrphanSweep.for_repository(repo).run_pass()
    assert retry["deleted"] == len(world.orphan_ids)
    _assert_swept(world)


def test_a_success_resets_the_consecutive_failure_count(world):
    """Only a run of failures is systemic: failures separated by successes never end
    the pass."""
    repo = world.repo
    order = []

    def alternating(source_id):
        order.append(source_id)
        if len(order) % 2:
            raise RuntimeError("one bad source")
        return repo.delete_source(source_id)

    tally = MemoryOrphanSweep(
        store=repo._runtime.memory_store, delete_source=alternating,
        event_log=repo._runtime.event_log,
    ).run_pass()

    assert len(order) == len(world.orphan_ids)  # every orphan was attempted once
    assert tally["failed"] == (len(order) + 1) // 2
    assert tally["deleted"] == len(order) // 2


def test_a_source_removed_by_another_writer_counts_as_gone(world):
    repo = world.repo
    first = world.orphan_ids[0]
    real = repo._runtime.memory_store
    served = []

    class StaleOnce:
        """The first read still lists ``first``; another writer deletes it before
        the sweep gets there."""

        @staticmethod
        def orphan_memory_source_ids(limit, after_id=""):
            page = real.orphan_memory_source_ids(limit, after_id)
            if not served:
                served.append(True)
                repo.delete_source(first)
            return page

    tally = MemoryOrphanSweep(
        store=StaleOnce, delete_source=repo.delete_source,
        event_log=repo._runtime.event_log,
    ).run_pass()

    assert tally["gone"] == 1 and tally["failed"] == 0
    assert tally["deleted"] == len(world.orphan_ids) - 1
    _assert_swept(world)


def test_nothing_runs_before_readiness(world, events):
    readiness.reset()
    try:
        service = MemoryOrphanSweep.for_repository(world.repo)
        assert service.schedule() is None
        assert service.run_pass() == {"deleted": 0, "gone": 0, "failed": 0}
        assert set(world.orphan_ids) <= set(_source_ids(world.repo))
        assert _sweep_events(events) == []
    finally:
        readiness.mark_ready()


def test_startup_hook_runs_the_sweep_in_the_background(world, monkeypatch):
    handles = []
    real_submit = background_jobs.submit

    def observing_submit(*args, **kwargs):
        handle = real_submit(*args, **kwargs)
        handles.append((kwargs.get("name"), handle))
        return handle

    monkeypatch.setattr(background_jobs, "submit", observing_submit)

    startup_warmup._sweep_orphan_memory_sources(world.repo)

    assert [name for name, _h in handles] == [sweep_module.JOB_NAME]
    handles[0][1].join(timeout=60)
    assert not handles[0][1].is_alive()
    _assert_swept(world)


def test_startup_reaches_the_sweep_only_after_mark_ready():
    tree = ast.parse(textwrap.dedent(inspect.getsource(startup_warmup.run_startup)))
    lines: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            lines.setdefault(node.func.id, []).append(node.lineno)
    assert "_sweep_orphan_memory_sources" in lines
    # EVERY call of the sweep sits after the readiness flip
    assert max(lines["_mark_lifecycle_ready"]) < min(lines["_sweep_orphan_memory_sources"])


def test_startup_hook_never_raises(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("scheduling exploded")

    monkeypatch.setattr(MemoryOrphanSweep, "schedule", boom)
    startup_warmup._sweep_orphan_memory_sources(repo)  # must not raise


def test_checkup_h12_counts_the_notebooks_remaining_orphans(world):
    def h12(notebook_id):
        return next(
            c for c in world.repo.checkup.run(notebook_id).checks if c.code == "H12"
        )

    in_a = [s for s in world.orphan_ids if s not in world.n5_ids]
    before_a, before_copy = h12("nb-a"), h12(world.copy_id)
    assert (before_a.count, before_a.fix, before_a.sample) == (len(in_a), "none", [])
    assert before_copy.count == len(world.n5_ids)

    MemoryOrphanSweep.for_repository(world.repo).run_pass()

    assert h12("nb-a").count == 0
    assert h12(world.copy_id).count == 0


def test_checkup_h12_follows_a_failed_pass_until_the_next_start(world):
    repo = world.repo

    def broken(_source_id):
        raise ConnectionError("away")

    MemoryOrphanSweep(
        store=repo._runtime.memory_store, delete_source=broken,
        event_log=repo._runtime.event_log,
    ).run_pass()
    total = sum(
        next(c for c in repo.checkup.run(nb).checks if c.code == "H12").count
        for nb in ("nb-a", world.copy_id)
    )
    assert total == len(world.orphan_ids)  # nothing was deleted, all still counted
    MemoryOrphanSweep.for_repository(repo).run_pass()
    assert all(
        next(c for c in repo.checkup.run(nb).checks if c.code == "H12").count == 0
        for nb in ("nb-a", world.copy_id)
    )


def test_the_store_read_pages_by_key_and_bounds_its_result(world):
    store = world.repo._runtime.memory_store
    everything = store.orphan_memory_source_ids(1000)
    assert everything == sorted(everything) == world.orphan_ids
    walked, after = [], ""
    while True:
        page = store.orphan_memory_source_ids(2, after)
        assert len(page) <= 2
        if not page:
            break
        walked += page
        after = page[-1]
    assert walked == everything
    assert store.orphan_memory_source_ids(0) == everything[:1]  # limit is floored at 1


def test_a_cleared_link_is_an_orphan_even_if_a_memory_row_has_an_empty_id(repo):
    """The explicit ``memory_id = ''`` clause is not decoration: it keeps the N-5 shape
    (a link the copy cleared) an orphan on its own, whatever ``memory_items`` holds.
    (``memory_id IS NULL`` is covered by ``NOT EXISTS`` -- ``m.id = NULL`` is never
    true -- and stays as the null-safe spelling; nothing observable separates them.)"""
    _user(repo, OWNER)
    _notebook(repo, "nb-a")
    _memory_item(repo, "", "nb-a")  # a confirmed Memory row whose id is the empty string
    _source(repo, "nb-a", "src-cleared", "memory", "")
    assert repo._runtime.memory_store.orphan_memory_source_ids(10) == ["src-cleared"]


def test_orphan_predicate_uses_the_shared_memory_source_type_fragment():
    from app.repositories.sqlite import memory_store

    assert memory_sql.memory_source_type_predicate("s.source_type") in \
        memory_store._ORPHAN_MEMORY_SOURCE_WHERE
    assert "'memory'" not in memory_store._ORPHAN_MEMORY_SOURCE_WHERE.replace(
        memory_sql.memory_source_type_predicate("s.source_type"), ""
    )


def test_sqlite_plans_of_the_two_statements(world):
    """No ANALYZE: the plan an untuned database picks. The global read is one
    ordered scan of ``sources`` whose per-Memory-source probe of ``memory_items`` is
    a primary-key SEARCH; the checkup count is seeked on the notebook prefix of
    ``(notebook_id, source_type)``."""
    from app.repositories.sqlite import memory_store

    where = memory_store._ORPHAN_MEMORY_SOURCE_WHERE
    with world.repo._runtime.database.connect() as db:
        def plan(sql, params):
            return " | ".join(
                str(r["detail"]) for r in db.execute("EXPLAIN QUERY PLAN " + sql, params)
            )

        ids = plan(
            "SELECT s.id FROM sources s WHERE " + where + " AND s.id > ? ORDER BY s.id LIMIT ?",
            ("", 50),
        )
        count = plan(
            "SELECT count(*) AS n FROM sources s WHERE s.notebook_id = ? AND " + where,
            ("nb-a",),
        )
    # the ordered read walks the primary-key range (no sort), probes memory_items by key
    assert "TEMP B-TREE" not in ids.upper(), ids
    assert "SEARCH s USING INDEX sqlite_autoindex_sources_1 (id>?)" in ids, ids
    assert "CORRELATED SCALAR SUBQUERY" in ids.upper(), ids
    assert "SEARCH m USING INDEX sqlite_autoindex_memory_items_1 (id=?)" in ids, ids
    assert "SCAN m" not in ids, ids
    # the count never scans the whole table
    assert "SEARCH s USING" in count and "notebook_id=?" in count, count
    assert "SCAN s" not in count, count
