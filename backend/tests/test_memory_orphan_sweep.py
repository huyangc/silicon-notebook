"""Post-readiness sweep of ownerless Memory sources (plan 2026-09-29 E5-3, audit N-5), SQLite.

Real repository, real ``remove_memory_sources`` path (the Memory-source removal
every other path uses, with its composition-wired hooks), real notebook copy --
nothing in the delete cascade is replaced. The N-5 orphan is produced by the real deep copy
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
    REMOVE_BATCH,
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
        # ---- N-5: what an EARLIER deep copy left behind. Since E5-1 a copy carries no
        # Memory, so the legacy transform is written field by field (see
        # ``notebook_sharing.py``: ``data["memory_id"] = ""`` -- "memory_id is NOT an id
        # that gets remapped ... Force it empty"; ``source_type`` stays 'memory', ids are
        # remapped into the new notebook, derived rows are copied along).
        self.copy_id = "nb-legacy-copy"
        _notebook(repo, self.copy_id, owner=RECIPIENT)
        self.n5_ids = ["src-n5-a", "src-n5-b"]
        for legacy_id in self.n5_ids:
            _source(repo, self.copy_id, legacy_id, "memory", "")
            _derive(repo, self.copy_id, legacy_id)
        _source(repo, self.copy_id, "src-n5-doc", "markdown", "")  # the copy's ordinary source
        # a raw NULL memory_id (the other "no link" spelling)
        _source(repo, "nb-a", "src-null", "memory", None)
        _derive(repo, "nb-a", "src-null")
        _derive(repo, "nb-a", "src-knowhow")
        self.orphan_ids = sorted(
            ["src-hard", "src-dep", "src-dangling", "src-null", *self.n5_ids]
        )
        self.control_ids = ["src-keep", "src-doc", "src-knowhow"]
        self.captured = {sid: _capture(repo, sid) for sid in self.orphan_ids + self.control_ids}
        # the copied Memory sources carry copied derived rows too
        for sid in self.n5_ids:
            assert all(self.captured[sid][t] for t, _k, _f in _DERIVED), sid

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


def _remover(repo, fails=lambda source_id: None):
    """A stand-in for ``remove_memory_sources`` that raises ``fails(id)`` for the
    ids it names -- for a whole call when one of them is in it, as a failed
    transaction does -- and otherwise removes through the real path."""
    real = repo._runtime.source_ingestion.remove_memory_sources
    calls: list[list[str]] = []

    def remove(source_ids):
        ids = list(source_ids)
        calls.append(ids)
        for source_id in ids:
            error = fails(source_id)
            if error is not None:
                raise error
        return real(ids)

    remove.calls = calls
    return remove


def _sweep(repo, remove, **kwargs):
    return MemoryOrphanSweep(
        store=kwargs.pop("store", repo._runtime.memory_store),
        remove_memory_sources=remove,
        event_log=repo._runtime.event_log,
        **kwargs,
    )


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
        original = store.orphan_memory_source_refs

        def counting(limit, after_id=""):
            page = original(limit, after_id)
            reads.append((limit, len(page)))
            return page

        monkeypatch.setattr(store, "orphan_memory_source_refs", counting)
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

    flaky = _remover(repo, lambda sid: RuntimeError("disk on fire: secret title")
                     if sid == victim else None)

    tally = _sweep(repo, flaky).run_pass()

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
    broken = _remover(repo, lambda sid: ConnectionError("database away"))

    tally = _sweep(repo, broken).run_pass()

    # one call for the batch, then one id at a time until the cap: stopped, did
    # not fail one by one
    singles = [ids for ids in broken.calls if len(ids) == 1]
    assert len(singles) == MAX_CONSECUTIVE_FAILURES
    assert tally == {"deleted": 0, "gone": 0, "failed": MAX_CONSECUTIVE_FAILURES}
    assert len(world.orphan_ids) > MAX_CONSECUTIVE_FAILURES
    assert set(world.orphan_ids) <= set(_source_ids(repo))  # everything left in place

    retry = MemoryOrphanSweep.for_repository(repo).run_pass()
    assert retry["deleted"] == len(world.orphan_ids)
    _assert_swept(world)


def test_a_poison_row_at_the_end_of_a_page_does_not_stop_the_rest(world, events):
    """With a page of one every row is the last of its page: the failed row must still
    advance the cursor, otherwise the next read returns it again, the consecutive-failure
    counter is used up by that one row and the orphans after it are never reached."""
    repo = world.repo
    victim = world.orphan_ids[0]

    flaky = _remover(repo, lambda sid: RuntimeError("poison") if sid == victim else None)

    tally = _sweep(repo, flaky, page_size=1).run_pass()

    assert tally == {"deleted": len(world.orphan_ids) - 1, "gone": 0, "failed": 1}
    assert _source_ids(repo).count(victim) == 1
    assert [s for s in world.orphan_ids if s != victim and s in _source_ids(repo)] == []


def test_a_first_page_read_that_fails_is_reported_and_starts_nothing(world, events):
    """The startup probe succeeded, then the FIRST page read times out (the coldest, most
    timeout-prone scan): exactly one content-free ``_failed`` -- no ``_started`` and no
    ``_completed`` -- and nothing is deleted."""
    repo = world.repo

    class Store:
        @staticmethod
        def has_orphan_memory_sources():
            return True

        @staticmethod
        def orphan_memory_source_refs(limit, after_id=""):
            raise TimeoutError("canceling statement due to statement timeout: secret")

    tally = _sweep(repo, _remover(repo), store=Store).run_pass()

    assert tally == {"deleted": 0, "gone": 0, "failed": 0}
    assert _sweep_events(events) == [
        {"kind": "memory_orphan_sweep_failed", "error_class": "TimeoutError"},
    ]
    assert set(world.orphan_ids) <= set(_source_ids(repo))


def test_a_failed_read_is_reported_content_free_and_ends_the_pass(world, events):
    repo = world.repo
    real = repo._runtime.memory_store
    reads = []

    class Store:
        @staticmethod
        def has_orphan_memory_sources():
            return real.has_orphan_memory_sources()

        @staticmethod
        def orphan_memory_source_refs(limit, after_id=""):
            reads.append(after_id)
            if len(reads) == 2:  # the second page: statement timeout
                raise TimeoutError("canceling statement due to statement timeout: secret")
            return real.orphan_memory_source_refs(limit, after_id)

    tally = _sweep(repo, _remover(repo), store=Store, page_size=2).run_pass()

    assert tally["deleted"] == 2 and tally["failed"] == 0
    kinds = [e["kind"] for e in _sweep_events(events)]
    assert kinds == [
        "memory_orphan_sweep_started", "memory_orphan_sweep_failed",
        "memory_orphan_sweep_completed",
    ]
    assert _sweep_events(events)[1] == {
        "kind": "memory_orphan_sweep_failed", "error_class": "TimeoutError",
    }
    assert "secret" not in json.dumps(_sweep_events(events))
    # the rest waits for the next start, which finishes it
    assert MemoryOrphanSweep.for_repository(repo).run_pass()["deleted"] == len(world.orphan_ids) - 2
    _assert_swept(world)


def test_a_success_resets_the_consecutive_failure_count(world):
    """Only a run of failures is systemic: failures separated by successes never end
    the pass."""
    repo = world.repo
    order = []
    real = repo._runtime.source_ingestion.remove_memory_sources

    def alternating(source_ids):
        ids = list(source_ids)
        if len(ids) > 1:
            raise RuntimeError("the batch fails")  # every id is then retried alone
        order.append(ids[0])
        if len(order) % 2:
            raise RuntimeError("one bad source")
        return real(ids)

    tally = _sweep(repo, alternating).run_pass()

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
        def orphan_memory_source_refs(limit, after_id=""):
            page = real.orphan_memory_source_refs(limit, after_id)
            if not served:
                served.append(True)
                repo.delete_source(first)
            return page

    tally = _sweep(repo, _remover(repo), store=StaleOnce).run_pass()

    # the call removed the rows it still found; the missing one is ``gone``
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


def _block_of(tree, callee):
    """The statement list (a ``body``) that directly holds ``callee(...)`` as a statement."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, stmt in enumerate(block):
                call = stmt.value if isinstance(stmt, ast.Expr) else None
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                        and call.func.id == callee:
                    return block, index
    raise AssertionError(f"{callee}(...) is not a plain statement of run_startup")


def test_startup_reaches_the_sweep_on_the_success_path_after_mark_ready():
    """NOTE for whoever refactors ``run_startup``: this guard reads its AST. Moving the
    post-readiness catch-ups into a helper or another block makes it fail closed
    ("not a plain statement of run_startup"); update the guard in the same change.

    The sweep is a statement of the SAME block as the other post-readiness catch-up
    (``_reproject_legacy_knowhow_tables``), after it and before ``return repo`` -- not
    merely textually after ``mark_ready`` (a call moved into the not-ready branch would
    pass a text-order check and never run)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(startup_warmup.run_startup)))
    block, at = _block_of(tree, "_sweep_orphan_memory_sources")
    other, other_at = _block_of(tree, "_reproject_legacy_knowhow_tables")
    assert block is other and other_at < at
    assert any(isinstance(stmt, ast.Return) for stmt in block[at + 1:])
    # ... and the readiness flip is an earlier ``if`` of the enclosing try, whose
    # branch returns (a not-ready start never reaches the sweep)
    body = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(startup_warmup.run_startup))))
    assert body.index("_sweep_orphan_memory_sources(") > body.rindex("_mark_lifecycle_ready(")


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

    _sweep(repo, _remover(repo, lambda sid: ConnectionError("away"))).run_pass()
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


def test_a_deep_copy_today_carries_no_memory_source(repo):
    """The sweep is for LEGACY data only: copies no longer carry Memory (E5-1), so a
    fresh ``copy_notebook`` produces no Memory source at all and no new N-5 orphan
    appears."""
    _user(repo, OWNER)
    _user(repo, RECIPIENT)
    _notebook(repo, "nb-src")
    _memory_item(repo, "mem-c", "nb-src")
    _source(repo, "nb-src", "src-c", "memory", "mem-c")
    _derive(repo, "nb-src", "src-c")
    copy = repo.copy_notebook("nb-src", new_owner_id=RECIPIENT)
    with repo._runtime.database.connect() as db:
        carried = db.execute(
            "SELECT COUNT(*) FROM sources WHERE notebook_id=? AND source_type='memory'",
            (copy.id,),
        ).fetchone()[0]
    assert carried == 0
    assert repo._runtime.memory_store.orphan_memory_source_ids(10) == []


def test_an_exception_is_a_failure_and_only_an_absent_row_is_gone(world, events):
    """``gone`` is a row the removal no longer found (another writer removed it:
    the call's ids minus what it removed); any exception -- a KeyError included
    -- is a recorded failure of that id, content-free."""
    repo = world.repo
    victim = world.orphan_ids[0]
    raising = _remover(repo, lambda sid: KeyError("some-other-key") if sid == victim else None)

    tally = _sweep(repo, raising).run_pass()

    assert tally == {"deleted": len(world.orphan_ids) - 1, "gone": 0, "failed": 1}
    failed = [e for e in _sweep_events(events) if e["kind"] == "memory_orphan_sweep_failed"]
    assert failed == [{
        "kind": "memory_orphan_sweep_failed", "source_id": victim, "error_class": "KeyError",
    }]
    assert "some-other-key" not in json.dumps(_sweep_events(events))


def test_a_non_memory_source_in_a_page_is_a_recorded_failure_never_swallowed(world, events):
    """The removal accepts Memory sources only and refuses a whole call holding
    anything else (``ValueError``). The orphan predicate never yields such an id;
    if a page ever carried one, that id is a recorded failure, the Memory orphans
    around it are still removed, and the document source keeps everything."""
    repo = world.repo
    real = repo._runtime.memory_store

    class WithADocument:
        @staticmethod
        def orphan_memory_source_refs(limit, after_id=""):
            page = real.orphan_memory_source_refs(limit, after_id)
            return sorted([*page, ("src-doc", "nb-a")]) if page and not after_id else page

    tally = _sweep(repo, repo._runtime.source_ingestion.remove_memory_sources,
                   store=WithADocument).run_pass()

    assert tally == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 1}
    failed = [e for e in _sweep_events(events) if e["kind"] == "memory_orphan_sweep_failed"]
    assert failed == [{
        "kind": "memory_orphan_sweep_failed", "source_id": "src-doc", "error_class": "ValueError",
    }]
    _assert_swept(world)


def test_an_orphan_merged_into_a_shared_object_leaves_it_whole_and_no_memory_name(world):
    """B2 for orphans: the sweep goes through the Memory removal, so an orphan's
    object manually merged into a shared object loses only its evidence (the
    shared object stays), and a cluster the orphan's object belongs to -- a
    real-name canonical id whose name also sits on a shared member's row --
    goes whole (the member arm; the minted-id seed arm is covered by
    ``memory_purge_cases``' B2 fixture on both backends)."""
    repo = world.repo
    orphan, orphan_object, shared = "src-dep", "ko-src-dep-1", "ko-src-doc-1"
    with repo._runtime.database.write() as db:
        evidence = json.loads(db.execute(
            "SELECT evidence FROM knowledge_objects WHERE id=?", (shared,)
        ).fetchone()[0])
        evidence.append({"source_id": orphan, "element_id": f"el-{orphan}-1"})
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                   (json.dumps(evidence), shared))
        db.execute("INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                   "VALUES (?,?,?)", (shared, orphan, "nb-a"))
        for member in (orphan_object, shared):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,created_at,generation) VALUES (?,?,?,?,?,?,1)",
                (f"cc-b2-{member}", "nb-a", "K-orphan private", member,
                 "ORPHAN-PRIVATE name", NOW),
            )

    MemoryOrphanSweep.for_repository(repo).run_pass()

    with repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_objects WHERE id=?",
                          (shared,)).fetchone()[0] == 1
        kept = json.loads(db.execute("SELECT evidence FROM knowledge_objects WHERE id=?",
                                     (shared,)).fetchone()[0])
        assert orphan not in {item.get("source_id") for item in kept}
        assert db.execute(
            "SELECT COUNT(*) FROM concept_clusters WHERE canonical_name LIKE 'ORPHAN-PRIVATE%' "
            "OR canonical_id='K-orphan private'"
        ).fetchone()[0] == 0


def test_a_page_is_removed_in_calls_of_at_most_the_purge_page(world):
    """The removal's statements are registered as one purge page (at most 200
    ids); a read page larger than that goes in several calls."""
    assert REMOVE_BATCH <= 200 < ORPHAN_SWEEP_PAGE_SIZE
    repo = world.repo
    counting = _remover(repo)
    import app.services.memory_orphan_sweep as module

    original = module.REMOVE_BATCH
    module.REMOVE_BATCH = 2
    try:
        tally = _sweep(repo, counting).run_pass()
    finally:
        module.REMOVE_BATCH = original
    assert tally["deleted"] == len(world.orphan_ids)
    assert max(len(ids) for ids in counting.calls) == 2
    assert len(counting.calls) == -(-len(world.orphan_ids) // 2)


def _orphan_notebooks(world):
    return {
        sid: (world.copy_id if sid in world.n5_ids else "nb-a") for sid in world.orphan_ids
    }


def test_a_removal_call_holds_the_sources_of_one_notebook(world):
    """Assembly review P3-6: the read page interleaves two notebooks' orphans
    (id order puts ``src-null`` after the copy's ``src-n5-*``); every removal
    call holds ONE notebook's sources, so one transaction locks and marks
    dirty one notebook, not every notebook of the page."""
    repo = world.repo
    notebook_of = _orphan_notebooks(world)
    page_notebooks = [notebook_of[sid] for sid in world.orphan_ids]
    assert page_notebooks != sorted(page_notebooks)  # the page really interleaves them
    counting = _remover(repo)

    tally = _sweep(repo, counting).run_pass()

    assert tally == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 0}
    assert [sorted({notebook_of[sid] for sid in call}) for call in counting.calls] == [
        ["nb-a"], [world.copy_id],
    ]
    _assert_swept(world)


def test_a_row_gone_during_the_one_by_one_retry_counts_as_gone(world):
    """Assembly review P3-3 (S1): a batch fails on a poison row; while it
    failed, another writer removed a second row of that batch. Retried alone,
    that row is found no more: ``gone``, not ``deleted``."""
    repo = world.repo
    poison, vanished = "src-dep", "src-hard"
    real = repo._runtime.source_ingestion.remove_memory_sources

    def remove(source_ids):
        ids = list(source_ids)
        if poison in ids and len(ids) > 1:
            repo.delete_source(vanished)  # another writer, while the batch fails
            raise RuntimeError("the batch fails")
        if ids == [poison]:
            raise RuntimeError("poison")
        return real(ids)

    tally = _sweep(repo, remove).run_pass()

    assert tally == {"deleted": len(world.orphan_ids) - 2, "gone": 1, "failed": 1}
    assert vanished not in _source_ids(repo)
    assert poison in _source_ids(repo)


def test_a_successful_batch_resets_the_consecutive_failures(world, monkeypatch):
    """Assembly review P3-3 (S2), batches of two: batch 1 ends with two
    failures in a row, batch 2 succeeds whole, batch 3's first id fails. Only
    one failure is consecutive at that point, so batch 3's second id is still
    removed; without the reset the pass would stop at three."""
    monkeypatch.setattr(sweep_module, "REMOVE_BATCH", 2)
    repo = world.repo
    failing = {"src-dangling", "src-dep", "src-n5-a"}
    flaky = _remover(repo, lambda sid: RuntimeError("bad") if sid in failing else None)

    tally = _sweep(repo, flaky).run_pass()

    assert [len(call) for call in flaky.calls] == [2, 1, 1, 2, 2, 1, 1]
    assert tally == {"deleted": 3, "gone": 0, "failed": 3}
    assert "src-n5-b" not in _source_ids(repo)


def test_the_failure_cap_ends_the_pass_between_batches(world, monkeypatch):
    """Assembly review P3-3 (S3), batches of two, everything failing: the cap
    is reached inside batch 2's one-by-one retry, and the pass ends there --
    batch 3 is never tried, neither as a whole nor one by one."""
    monkeypatch.setattr(sweep_module, "REMOVE_BATCH", 2)
    repo = world.repo
    broken = _remover(repo, lambda sid: ConnectionError("database away"))

    tally = _sweep(repo, broken).run_pass()

    assert tally == {"deleted": 0, "gone": 0, "failed": MAX_CONSECUTIVE_FAILURES}
    assert broken.calls == [
        ["src-dangling", "src-dep"], ["src-dangling"], ["src-dep"],
        ["src-hard", "src-null"], ["src-hard"],
    ]
    assert set(world.orphan_ids) <= set(_source_ids(repo))


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
            "SELECT s.id, s.notebook_id FROM sources s WHERE " + where
            + " AND s.id > ? ORDER BY s.id LIMIT ?",
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
