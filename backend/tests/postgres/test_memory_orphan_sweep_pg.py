"""PostgreSQL twin of ``tests/test_memory_orphan_sweep.py`` (plan 2026-09-29 E5-3, N-5).

Same fixture shapes, same acceptance sentences, on the primary backend: the store
read and its keyset paging, the cascade through the real ``remove_memory_sources`` path
(every derived table, including the ones a foreign key does not reach), idempotency,
page-of-one equivalence, failure isolation and the per-notebook count behind
checkup's read-only H12. PostgreSQL has no FTS5 shadow tables, so nothing is exempt
from the "every derived row is gone" assertion here.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.services.memory_orphan_sweep import (
    MAX_CONSECUTIVE_FAILURES,
    ORPHAN_SWEEP_PAGE_SIZE,
    MemoryOrphanSweep,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_orphan_sweep"),
]

NOW = "2026-09-30T00:00:00+00:00"
OWNER = "u-e53-owner"
RECIPIENT = "u-e53-recipient"


@pytest.fixture
def postgres_repository(postgres_scope, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(
        Settings(
            database_url=postgres_scope.url,
            storage_dir=str(tmp_path),
            postgres_pool_min_size=1,
            postgres_pool_max_size=4,
            postgres_pool_acquire_timeout_seconds=2,
            postgres_statement_timeout_seconds=10,
            postgres_lock_timeout_seconds=2,
        )
    )
    try:
        yield repository
    finally:
        repository.close()


def _write(repo, sql, params=()):
    with repo._runtime.database.write() as db:
        db.execute(sql, params)


def _user(repo, user_id):
    _write(
        repo,
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, f"{user_id}@x", user_id, "user", "active", user_id, NOW, NOW),
    )


def _notebook(repo, notebook_id, owner=OWNER):
    _write(
        repo,
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (notebook_id, notebook_id, owner, "ready", NOW, NOW),
    )


def _memory_item(repo, memory_id, notebook_id, status="confirmed"):
    _write(
        repo,
        "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,title,"
        "content_md,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (memory_id, notebook_id, OWNER, "ask_answer", status, memory_id, "x", NOW, NOW),
    )


def _source(repo, notebook_id, source_id, source_type, memory_id=None):
    _write(
        repo,
        "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
        "memory_id,created_at,updated_at) VALUES (%s,%s,%s,%s,'extracted','extracted',"
        "%s,%s,%s)",
        (source_id, notebook_id, f"T {source_id}", source_type, memory_id, NOW, NOW),
    )


def _derive(repo, notebook_id, source_id):
    t = source_id
    vec = b"\x00\x01\x02\x03"
    with repo._runtime.database.write() as db:
        for n in (1, 2):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,created_at) VALUES (%s,%s,'paragraph',%s,%s,%s)",
                (f"el-{t}-{n}", source_id, f"p{n}", f"text {n}", NOW),
            )
        db.execute(
            "INSERT INTO element_embeddings (element_id,source_id,notebook_id,vector,"
            "created_at) VALUES (%s,%s,%s,%s,%s)",
            (f"el-{t}-1", source_id, notebook_id, vec, NOW),
        )
        for n in (1, 2):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,payload,evidence,"
                "source_id,created_at,updated_at) VALUES (%s,%s,'concept',%s::jsonb,%s::jsonb,"
                "%s,%s,%s)",
                (
                    f"ko-{t}-{n}", notebook_id,
                    json.dumps({"name": f"concept {t} {n}"}),
                    json.dumps([{"source_id": source_id, "element_id": f"el-{t}-1"}]),
                    source_id, NOW, NOW,
                ),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
                "VALUES (%s,%s,%s)",
                (f"ko-{t}-{n}", source_id, notebook_id),
            )
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s)",
            (f"ko-{t}-1", notebook_id, vec, NOW),
        )
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,created_at) VALUES (%s,%s,%s,%s,%s,'relates',%s)",
            (f"kr-{t}", notebook_id, source_id, f"ko-{t}-1", f"ko-{t}-2", NOW),
        )
        db.execute(
            "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s)",
            (f"kr-{t}", notebook_id, vec, NOW),
        )
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (f"cc-{t}", notebook_id, f"can-{t}", f"ko-{t}-1", f"concept {t} 1", NOW),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,created_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (f"ch-{t}", notebook_id, source_id, f"chunk text {t}", NOW),
        )
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
            "VALUES (%s,%s,%s,%s)",
            (f"ch-{t}", notebook_id, vec, NOW),
        )
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,"
            "created_at,updated_at) VALUES (%s,%s,%s,'kg','completed',%s,%s)",
            (f"er-{t}", notebook_id, source_id, NOW, NOW),
        )


_DERIVED = (
    ("source_elements", "id", "SELECT id FROM source_elements WHERE source_id=%s"),
    ("element_embeddings", "element_id", "SELECT element_id FROM element_embeddings WHERE source_id=%s"),
    ("knowledge_objects", "id", "SELECT id FROM knowledge_objects WHERE source_id=%s"),
    ("knowledge_object_sources", "object_id", "SELECT object_id FROM knowledge_object_sources WHERE source_id=%s"),
    ("knowledge_embeddings", "object_id", "SELECT object_id FROM knowledge_embeddings WHERE object_id IN "
                                          "(SELECT id FROM knowledge_objects WHERE source_id=%s)"),
    ("knowledge_relations", "id", "SELECT id FROM knowledge_relations WHERE source_id=%s"),
    ("relation_embeddings", "relation_id", "SELECT relation_id FROM relation_embeddings WHERE relation_id IN "
                                           "(SELECT id FROM knowledge_relations WHERE source_id=%s)"),
    ("concept_clusters", "member_object_id", "SELECT member_object_id FROM concept_clusters WHERE member_object_id IN "
                                              "(SELECT id FROM knowledge_objects WHERE source_id=%s)"),
    ("extraction_runs", "id", "SELECT id FROM extraction_runs WHERE source_id=%s"),
    ("chunks", "id", "SELECT id FROM chunks WHERE source_id=%s"),
    ("chunk_embeddings", "chunk_id", "SELECT chunk_id FROM chunk_embeddings WHERE chunk_id IN "
                                     "(SELECT id FROM chunks WHERE source_id=%s)"),
)


def _first(row):
    """PostgreSQL rows are dicts: the value of their (only) column."""
    return next(iter(row.values()))


def _capture(repo, source_id):
    out = {}
    with repo._runtime.database.connect() as db:
        for table, _key, find in _DERIVED:
            out[table] = [_first(r) for r in db.execute(find, (source_id,)).fetchall()]
    return out


def _alive(repo, captured):
    out = {}
    with repo._runtime.database.connect() as db:
        for table, key, _find in _DERIVED:
            out[table] = sum(
                _first(db.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=%s", (i,)).fetchone())
                for i in captured[table]
            )
    return out


def _source_ids(repo):
    with repo._runtime.database.connect() as db:
        return sorted(_first(r) for r in db.execute("SELECT id FROM sources").fetchall())


class World:
    def __init__(self, repo):
        self.repo = repo
        _user(repo, OWNER)
        _user(repo, RECIPIENT)
        _notebook(repo, "nb-a")
        _memory_item(repo, "mem-keep", "nb-a")
        _source(repo, "nb-a", "src-keep", "memory", "mem-keep")
        _source(repo, "nb-a", "src-doc", "markdown", "")
        _source(repo, "nb-a", "src-doc-null", "markdown", None)  # NULL memory_id, not a Memory source
        _source(repo, "nb-a", "src-knowhow", "knowhow", "")
        _memory_item(repo, "mem-hard", "nb-a")
        _source(repo, "nb-a", "src-hard", "memory", "mem-hard")
        repo._runtime.memory_store.delete_memory("mem-hard", OWNER)  # hard-delete residue
        _memory_item(repo, "mem-dep", "nb-a", status="deprecated")
        _source(repo, "nb-a", "src-dep", "memory", "mem-dep")
        _source(repo, "nb-a", "src-dangling", "memory", "mem-never")
        for source_id in ("src-keep", "src-doc", "src-hard", "src-dep", "src-dangling"):
            _derive(repo, "nb-a", source_id)
        # N-5: what an EARLIER deep copy left behind. Since E5-1 a copy carries no Memory,
        # so the legacy transform is written field by field (``notebook_sharing.py``:
        # ``data["memory_id"] = ""``; ``source_type`` stays 'memory'; ids remapped into
        # the new notebook; derived rows copied along).
        self.copy_id = "nb-legacy-copy"
        _notebook(repo, self.copy_id, owner=RECIPIENT)
        self.n5_ids = ["src-n5-a", "src-n5-b"]
        for legacy_id in self.n5_ids:
            _source(repo, self.copy_id, legacy_id, "memory", "")
            _derive(repo, self.copy_id, legacy_id)
        _source(repo, self.copy_id, "src-n5-doc", "markdown", "")
        _source(repo, "nb-a", "src-null", "memory", None)
        _derive(repo, "nb-a", "src-null")
        _derive(repo, "nb-a", "src-knowhow")
        self.orphan_ids = sorted(
            ["src-hard", "src-dep", "src-dangling", "src-null", *self.n5_ids]
        )
        self.control_ids = ["src-keep", "src-doc", "src-knowhow"]
        self.captured = {s: _capture(repo, s) for s in self.orphan_ids + self.control_ids}
        for sid in self.n5_ids:
            assert all(self.captured[sid][t] for t, _k, _f in _DERIVED), sid


def _assert_swept(world):
    ids = _source_ids(world.repo)
    for sid in world.orphan_ids:
        assert sid not in ids, sid
        alive = _alive(world.repo, world.captured[sid])
        assert not any(alive.values()), (sid, alive)
    for sid in world.control_ids:
        assert sid in ids, sid
        alive = _alive(world.repo, world.captured[sid])
        assert alive == {t: len(world.captured[sid][t]) for t in alive}, (sid, alive)
        assert any(alive.values())
    # non-Memory sources with a NULL / empty memory_id are never orphans
    assert "src-doc-null" in ids and "src-doc" in ids


def test_the_store_read_finds_exactly_the_orphan_shapes(postgres_repository):
    world = World(postgres_repository)
    store = postgres_repository._runtime.memory_store
    assert store.has_orphan_memory_sources() is True
    assert store.orphan_memory_source_ids(1000) == world.orphan_ids
    assert "src-keep" not in world.orphan_ids
    for sid in ("src-hard", "src-dep", "src-dangling", "src-null"):
        assert all(world.captured[sid][t] for t, _k, _f in _DERIVED), sid


def test_the_store_read_pages_by_key_and_bounds_its_result(postgres_repository):
    world = World(postgres_repository)
    store = postgres_repository._runtime.memory_store
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
    assert store.orphan_memory_source_ids(0) == everything[:1]


def test_a_pass_removes_every_orphan_and_everything_derived_from_it(postgres_repository):
    world = World(postgres_repository)
    sweep = MemoryOrphanSweep.for_repository(postgres_repository)

    assert sweep.run_pass() == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 0}
    _assert_swept(world)
    # idempotent: the next start finds nothing and schedules nothing
    again = MemoryOrphanSweep.for_repository(postgres_repository)
    assert again.has_orphans() is False and again.schedule() is None
    assert again.run_pass() == {"deleted": 0, "gone": 0, "failed": 0}
    assert postgres_repository._runtime.memory_store.orphan_memory_source_ids(1000) == []


def test_a_page_of_one_gives_the_same_end_state_as_the_default_page(
    postgres_repository,
):
    world = World(postgres_repository)
    store = postgres_repository._runtime.memory_store
    reads = []
    original = store.orphan_memory_source_refs

    def counting(limit, after_id=""):
        page = original(limit, after_id)
        reads.append((limit, len(page)))
        return page

    store.orphan_memory_source_refs = counting
    try:
        tally = MemoryOrphanSweep.for_repository(postgres_repository, page_size=1).run_pass()
    finally:
        del store.orphan_memory_source_refs
    assert tally == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 0}
    assert all(limit == 1 for limit, _n in reads)
    assert len(reads) == len(world.orphan_ids) + 1
    _assert_swept(world)
    assert ORPHAN_SWEEP_PAGE_SIZE > 1


def test_a_removal_call_holds_the_sources_of_one_notebook(postgres_repository):
    """Assembly review P3-6: the read carries each orphan's notebook, and the
    page (which interleaves two notebooks in id order) is removed in calls of
    one notebook each."""
    world = World(postgres_repository)
    repo = postgres_repository
    store = repo._runtime.memory_store
    notebook_of = dict(store.orphan_memory_source_refs(1000))
    assert notebook_of == {
        sid: (world.copy_id if sid in world.n5_ids else "nb-a") for sid in world.orphan_ids
    }
    real = repo._runtime.source_ingestion.remove_memory_sources
    calls = []

    def counting(source_ids):
        calls.append(list(source_ids))
        return real(source_ids)

    tally = MemoryOrphanSweep(
        store=store, remove_memory_sources=counting, event_log=repo._runtime.event_log
    ).run_pass()
    assert tally == {"deleted": len(world.orphan_ids), "gone": 0, "failed": 0}
    assert [sorted({notebook_of[sid] for sid in call}) for call in calls] == [
        ["nb-a"], [world.copy_id],
    ]
    _assert_swept(world)


def test_a_failing_source_is_isolated_and_left_for_the_next_start(
    postgres_repository,
):
    world = World(postgres_repository)
    repo = postgres_repository
    victim = world.orphan_ids[2]

    real = repo._runtime.source_ingestion.remove_memory_sources

    def flaky(source_ids):
        if victim in source_ids:
            raise RuntimeError("secret title")  # the whole call fails, as a transaction does
        return real(source_ids)

    log = repo._runtime.event_log
    seen = []
    original_emit = log.emit
    log.emit = lambda event, *a, **k: (seen.append(dict(event)), original_emit(event, *a, **k))[1]
    try:
        tally = MemoryOrphanSweep(
            store=repo._runtime.memory_store, remove_memory_sources=flaky, event_log=log
        ).run_pass()
    finally:
        del log.emit
    assert tally == {"deleted": len(world.orphan_ids) - 1, "gone": 0, "failed": 1}
    assert [e for e in seen if e["kind"] == "memory_orphan_sweep_failed"] == [{
        "kind": "memory_orphan_sweep_failed", "source_id": victim, "error_class": "RuntimeError",
    }]
    assert "secret title" not in json.dumps(seen)
    assert victim in _source_ids(repo)

    calls = []

    def broken(source_ids):
        calls.append(list(source_ids))
        raise ConnectionError("away")

    stopped = MemoryOrphanSweep(
        store=repo._runtime.memory_store, remove_memory_sources=broken, event_log=log
    ).run_pass()
    assert stopped == {"deleted": 0, "gone": 0, "failed": 1}  # only the victim was left
    assert calls == [[victim], [victim]]  # the call, then the id on its own
    assert MAX_CONSECUTIVE_FAILURES >= 2

    assert MemoryOrphanSweep.for_repository(repo).run_pass()["deleted"] == 1  # next start
    _assert_swept(world)


def test_checkup_h12_counts_the_notebooks_remaining_orphans(postgres_repository):
    world = World(postgres_repository)
    repo = postgres_repository

    def h12(notebook_id):
        return next(c for c in repo.checkup.run(notebook_id).checks if c.code == "H12")

    in_a = [s for s in world.orphan_ids if s not in world.n5_ids]
    first = h12("nb-a")
    assert (first.count, first.fix, first.sample) == (len(in_a), "none", [])
    assert h12(world.copy_id).count == len(world.n5_ids)
    MemoryOrphanSweep.for_repository(repo).run_pass()
    assert h12("nb-a").count == 0 and h12(world.copy_id).count == 0


def test_a_deep_copy_today_carries_no_memory_source(postgres_repository):
    """The sweep is for LEGACY data only (E5-1: copies carry no Memory)."""
    repo = postgres_repository
    _user(repo, OWNER)
    _user(repo, RECIPIENT)
    _notebook(repo, "nb-src")
    _memory_item(repo, "mem-c", "nb-src")
    _source(repo, "nb-src", "src-c", "memory", "mem-c")
    _derive(repo, "nb-src", "src-c")
    copy = repo.copy_notebook("nb-src", new_owner_id=RECIPIENT)
    with repo._runtime.database.connect() as db:
        carried = _first(db.execute(
            "SELECT COUNT(*) FROM sources WHERE notebook_id=%s AND source_type='memory'",
            (copy.id,),
        ).fetchone())
    assert carried == 0
    assert repo._runtime.memory_store.orphan_memory_source_ids(10) == []


def test_an_exception_is_a_failure_and_a_non_memory_id_is_refused(postgres_repository):
    """Any exception of the removal is a recorded failure of that id (a KeyError
    included); a page carrying a non-Memory source makes the removal refuse it
    (ValueError), recorded, while the Memory orphans around it are removed."""
    world = World(postgres_repository)
    repo = postgres_repository
    real_store = repo._runtime.memory_store
    victim = world.orphan_ids[0]
    real = repo._runtime.source_ingestion.remove_memory_sources

    def raising(source_ids):
        if victim in source_ids:
            raise KeyError("some-other-key")
        return real(source_ids)

    tally = MemoryOrphanSweep(
        store=real_store, remove_memory_sources=raising,
        event_log=repo._runtime.event_log,
    ).run_pass()
    assert tally == {"deleted": len(world.orphan_ids) - 1, "gone": 0, "failed": 1}

    class WithADocument:
        @staticmethod
        def orphan_memory_source_refs(limit, after_id=""):
            page = real_store.orphan_memory_source_refs(limit, after_id)
            return sorted([*page, ("src-doc", "nb-a")]) if page and not after_id else page

    second = MemoryOrphanSweep(
        store=WithADocument, remove_memory_sources=real,
        event_log=repo._runtime.event_log,
    ).run_pass()
    assert second == {"deleted": 1, "gone": 0, "failed": 1}  # the victim; src-doc refused
    assert "src-doc" in _source_ids(repo)
    _assert_swept(world)


def test_a_cleared_link_is_an_orphan_even_if_a_memory_row_has_an_empty_id(postgres_repository):
    """NULL falls out of the NOT EXISTS by itself (``m.id = NULL`` is never true); the
    empty string is guarded by ``m.id <> ''`` in the subquery, so a confirmed Memory row
    with an empty id cannot rescue a source whose link was cleared."""
    repo = postgres_repository
    _user(repo, OWNER)
    _notebook(repo, "nb-a")
    _memory_item(repo, "", "nb-a")
    _source(repo, "nb-a", "src-cleared", "memory", "")
    _source(repo, "nb-a", "src-null", "memory", None)
    store = repo._runtime.memory_store
    assert store.orphan_memory_source_ids(10) == ["src-cleared", "src-null"]
    assert store.has_orphan_memory_sources() is True
