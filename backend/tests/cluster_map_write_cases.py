"""Shared cases for the paged cluster-map write (the rebuild's write-generation
step, ``UnifiedKgStore.write_cluster_map_generation`` + its service loop in
``KnowledgeLifecycleService._write_cluster_map_streamed``).

``tests/test_cluster_map_write_paging.py`` runs every case on SQLite and
``tests/postgres/test_cluster_map_write_paging_pg.py`` on PostgreSQL. Both
backends are held to the SAME expectations: a paged write produces, row for
row and in the same insertion order, what one unpaged write produces; page
boundaries fall exactly every ``page_rows`` scratch rows; a claim lost between
two pages stops the write loudly and the next rebuild reaps the pages already
committed. Every store call goes through ``repo._runtime.unified_kg``.

Insertion order is read back per backend: SQLite's ``concept_clusters.rowid``;
on PostgreSQL a test-local AFTER INSERT trigger logs each row with a serial
(heap position is no insertion order there -- free-space reuse places rows
anywhere).
"""
from __future__ import annotations

import json

import pytest

from app.models.schemas import NotebookCreate
from app.repositories.ports import KgDerivedGenerationPreempted

RUN = "run-cmw"
OTHER_RUN = "run-cmw-other"
EMPTY_RUN = "run-cmw-empty"
CREATED = "2026-10-21T08:00:00+00:00"
NAMES = [f"Device {chr(ord('A') + i)}" for i in range(10)]
# One seed with no canonical: its scratch row is skipped by the join, and it
# sits inside the first page so page 1 writes fewer rows than it scans.
UNMAPPED = "device b"


def _p(postgres: bool) -> str:
    return "%s" if postgres else "?"


def install_insert_log(repo, *, postgres: bool) -> None:
    if not postgres:
        return
    with repo._runtime.database.write() as db:
        db.execute(
            "CREATE TABLE cmw_insert_log (seq bigserial PRIMARY KEY, "
            "notebook_id text, member text, canonical_id text, canonical_name text, "
            "canonical_description text, canonical_desc_sig text, object_type text, "
            "created_at text, generation bigint)"
        )
        db.execute(
            "CREATE FUNCTION cmw_log() RETURNS trigger LANGUAGE plpgsql AS $f$ "
            "BEGIN INSERT INTO cmw_insert_log (notebook_id, member, canonical_id, "
            "canonical_name, canonical_description, canonical_desc_sig, object_type, "
            "created_at, generation) VALUES (NEW.notebook_id, NEW.member_object_id, "
            "NEW.canonical_id, NEW.canonical_name, NEW.canonical_description, "
            "NEW.canonical_desc_sig, NEW.object_type, NEW.created_at::text, "
            "NEW.generation); RETURN NULL; END $f$"
        )
        db.execute(
            "CREATE TRIGGER cmw_log AFTER INSERT ON concept_clusters "
            "FOR EACH ROW EXECUTE FUNCTION cmw_log()"
        )


def written(repo, notebook_id: str, generation: int, *, postgres: bool) -> list:
    """The generation's rows, in insertion order, without the random id."""
    p = _p(postgres)
    if postgres:
        sql = (
            "SELECT member, canonical_id, canonical_name, canonical_description, "
            "canonical_desc_sig, object_type, created_at FROM cmw_insert_log "
            f"WHERE notebook_id={p} AND generation={p} ORDER BY seq"
        )
    else:
        sql = (
            "SELECT member_object_id AS member, canonical_id, canonical_name, "
            "canonical_description, canonical_desc_sig, object_type, created_at "
            f"FROM concept_clusters WHERE notebook_id={p} AND generation={p} "
            "ORDER BY rowid"
        )
    with repo._connect() as db:
        return [tuple(row[k] for k in ("member", "canonical_id", "canonical_name",
                                       "canonical_description", "canonical_desc_sig",
                                       "object_type", "created_at"))
                for row in db.execute(sql, (notebook_id, generation)).fetchall()]


def _seed_world(repo) -> tuple[str, list[tuple[str, str]]]:
    nb = repo.create_notebook(NotebookCreate(name="cmw")).id
    other = repo.create_notebook(NotebookCreate(name="cmw-other")).id
    for notebook in (nb, other):
        repo.store_kg(notebook, None, [
            {"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": name, "section_path": ""}, "evidence": []}
            for i, name in enumerate(NAMES)
        ], [])
    store = repo._runtime.unified_kg
    with repo._connect() as db:
        stream = [(r["id"], json.loads(r["payload"])["name"])
                  for r in store.stream_seed_rows(db, nb, "concept")]
        other_stream = [(r["id"], json.loads(r["payload"])["name"])
                        for r in store.stream_seed_rows(db, other, "concept")]
    assert len(stream) == len(NAMES)
    # Scratch is filled like Pass A2 (stream order, batch by batch), with a
    # concurrent run of the same notebook and another notebook's run writing
    # between the batches: the run's keys interleave with theirs.
    batches = [stream[0:4], stream[4:7], stream[7:10]]
    for index, batch in enumerate(batches):
        with repo._write() as db:
            store.insert_scratch_rows(db, [
                (nb, RUN, oid, name.lower()) for oid, name in batch])
        with repo._write() as db:
            store.insert_scratch_rows(db, [
                (nb, OTHER_RUN, oid, name.lower()) for oid, name in batch])
            store.insert_scratch_rows(db, [
                (other, RUN, oid, name.lower())
                for oid, name in other_stream[index:index + 2]])
    canonical = []
    for name in NAMES:
        seed = name.lower()
        if seed == UNMAPPED:
            continue
        # two seeds share a canonical; the rest are singletons
        cid = "K-device a" if seed == "device c" else f"K-{seed}"
        canonical.append((nb, RUN, seed, cid, f"name of {cid}",
                          f"about {cid}", f"sig-{cid}"))
    with repo._write() as db:
        store.insert_canonical_scratch_rows(db, canonical)
        # the other run maps the same seeds elsewhere: any leak shows up
        store.insert_canonical_scratch_rows(db, [
            (nb, OTHER_RUN, seed, f"K-other-{seed}", "x", "", "")
            for _nb, _run, seed, *_rest in canonical])
    return nb, stream


def _write_pages(repo, nb: str, run: str, generation: int, page_rows: int,
                 *, postgres: bool) -> tuple[list, list]:
    """Loop the store like the service does; return the cursors it handed back
    and the rows written after each page."""
    store = repo._runtime.unified_kg
    cursors, after_page = [], []
    start = None
    while True:
        with repo._write() as db:
            start = store.write_cluster_map_generation(
                db, nb, "concept", run, CREATED, generation,
                start=start, page_rows=page_rows)
        cursors.append(start)
        after_page.append(len(written(repo, nb, generation, postgres=postgres)))
        if start is None:
            return cursors, after_page
        assert len(cursors) < 50, "the page loop does not terminate"


def check_paged_write_matches_one_shot(repo, *, postgres: bool) -> None:
    install_insert_log(repo, postgres=postgres)
    nb, stream = _seed_world(repo)
    expected_members = [oid for oid, name in stream if name.lower() != UNMAPPED]
    # precondition: insertion order is not the id order, so the order check
    # below can tell "scratch insertion order" from "sorted by member id"
    assert expected_members != sorted(expected_members)

    one_cursors, one_counts = _write_pages(repo, nb, RUN, 8, 1000, postgres=postgres)
    paged_cursors, paged_counts = _write_pages(repo, nb, RUN, 7, 3, postgres=postgres)
    one = written(repo, nb, 8, postgres=postgres)
    paged = written(repo, nb, 7, postgres=postgres)

    # one page, one call
    assert one_cursors == [None] and one_counts == [9]
    # row for row, in the same insertion order: the scratch rows' order
    assert paged == one
    assert [row[0] for row in paged] == expected_members
    by_member = {row[0]: row for row in paged}
    for oid, name in stream:
        seed = name.lower()
        if seed == UNMAPPED:
            continue
        cid = "K-device a" if seed == "device c" else f"K-{seed}"
        assert by_member[oid][1:6] == (cid, f"name of {cid}", f"about {cid}",
                                       f"sig-{cid}", "concept"), oid
    # 10 scratch rows, 3 per page: pages end after scratch rows 3, 6, 9, 10.
    # Page 1 scans the unmapped seed's row and writes 2.
    assert paged_counts == [2, 5, 8, 9]
    assert [c is None for c in paged_cursors] == [False, False, False, True]
    assert paged_cursors[0] < paged_cursors[1] < paged_cursors[2]

    # exact multiple: 10 rows, 5 per page -> two full pages, then an empty
    # final call that finds no bound and writes nothing
    multiple_cursors, multiple_counts = _write_pages(
        repo, nb, RUN, 9, 5, postgres=postgres)
    assert multiple_counts == [4, 9, 9]
    assert [c is None for c in multiple_cursors] == [False, False, True]
    assert written(repo, nb, 9, postgres=postgres) == one

    # a run with no scratch rows: one call, nothing written
    empty_cursors, empty_counts = _write_pages(
        repo, nb, EMPTY_RUN, 10, 3, postgres=postgres)
    assert (empty_cursors, empty_counts) == ([None], [0])

    with repo._write() as db:
        with pytest.raises(ValueError):
            repo._runtime.unified_kg.write_cluster_map_generation(
                db, nb, "concept", RUN, CREATED, 11, start=None, page_rows=0)


def check_pass_b_reads_vectors_in_scratch_insertion_order(
    repo, *, postgres: bool,
) -> tuple[str, list]:
    """Pass B's vector read returns this run's rows in scratch insertion
    order -- the stream order Pass A2 wrote them in -- on a scratch whose keys
    interleave with another run's and another notebook's. Float32 mean
    accumulation is order-sensitive, so this order is what keeps the seed
    representatives bit-for-bit stable. Returns the notebook and the rows for
    a backend-specific comparison."""
    nb, stream = _seed_world(repo)
    p = _p(postgres)
    store = repo._runtime.unified_kg
    with repo._connect() as db:
        embedded = {r["object_id"] for r in db.execute(
            f"SELECT object_id FROM knowledge_embeddings WHERE notebook_id={p}",
            (nb,)).fetchall()}
        rows = [(r["seed"], bytes(r["vector"]))
                for r in store.scratch_vector_rows(db, nb, RUN)]
    assert len(embedded) == len(NAMES), "precondition: every object has a vector"
    assert [seed for seed, _vector in rows] == [
        name.lower() for oid, name in stream if oid in embedded]
    return nb, rows


def _published(repo, nb: str, *, postgres: bool) -> set:
    p = _p(postgres)
    with repo._connect() as db:
        return {
            (r["member_object_id"], r["canonical_id"], r["canonical_name"],
             r["object_type"])
            for r in db.execute(
                "SELECT member_object_id, canonical_id, canonical_name, object_type "
                "FROM concept_clusters WHERE notebook_id=" + p + " AND generation="
                "(SELECT cluster_generation FROM unified_kg_state WHERE notebook_id="
                + p + ")", (nb, nb)).fetchall()
        }


def _rows_in_generation(repo, nb: str, generation: int, *, postgres: bool) -> list:
    p = _p(postgres)
    with repo._connect() as db:
        return [(r["object_type"],) for r in db.execute(
            f"SELECT object_type FROM concept_clusters WHERE notebook_id={p} "
            f"AND generation={p}", (nb, generation)).fetchall()]


def check_rebuild_pages_and_a_lost_claim_stops_between_pages(
    repo, *, postgres: bool, monkeypatch,
) -> None:
    nb = repo.create_notebook(NotebookCreate(name="cmw-rebuild")).id
    repo.store_kg(nb, None, [
        {"local_id": f"o{i}", "object_type": "concept",
         "payload": {"name": f"Widget {i}", "section_path": ""}, "evidence": []}
        for i in range(7)
    ], [])
    service = repo._runtime.knowledge_lifecycle
    store = repo._runtime.unified_kg

    # Reference: the default page size writes each type in one page.
    repo.rebuild_unified_kg(nb, force=True)
    reference = _published(repo, nb, postgres=postgres)
    assert len({m for m, _c, _n, t in reference if t == "concept"}) == 7

    calls: list = []
    original_write = store.write_cluster_map_generation

    def recording_write(db, notebook_id, object_type, run_id, created_at,
                        generation, **kwargs):
        result = original_write(db, notebook_id, object_type, run_id,
                                created_at, generation, **kwargs)
        calls.append((object_type, generation, kwargs["start"], result))
        return result

    monkeypatch.setattr(store, "write_cluster_map_generation", recording_write)
    monkeypatch.setattr(service, "_CLUSTER_MAP_PAGE_ROWS", 2)

    # 1. Paged rebuild: 7 concept members in 2-row pages = 4 page calls, each
    #    cursor handed to the next call; the published result is the same.
    repo.rebuild_unified_kg(nb, force=True)
    concept = [c for c in calls if c[0] == "concept"]
    assert len(concept) == 4, concept
    assert concept[0][2] is None and concept[-1][3] is None
    for previous, following in zip(concept, concept[1:]):
        assert following[2] == previous[3] is not None
    assert _published(repo, nb, postgres=postgres) == reference

    # 2. The claim is lost between two pages: the second concept page's
    #    recheck sees another generation and the rebuild stops there, loudly.
    calls.clear()
    events: list = []
    original_emit = repo._runtime.event_log.emit
    monkeypatch.setattr(repo._runtime.event_log, "emit",
                        lambda e: (events.append(e), original_emit(e))[1])
    rechecks: list = []
    original_held = store.derived_claim_still_held
    p = _p(postgres)

    def usurped_on_the_second_page(db, notebook_id, generation):
        if [c for c in calls if c[0] == "concept"]:
            rechecks.append(generation)
            # the usurper's claim, seen by this page's recheck
            db.execute(
                "UPDATE unified_kg_state SET derived_building_generation="
                f"derived_building_generation+7 WHERE notebook_id={p}",
                (notebook_id,))
        return original_held(db, notebook_id, generation)

    monkeypatch.setattr(store, "derived_claim_still_held", usurped_on_the_second_page)
    with pytest.raises(KgDerivedGenerationPreempted):
        repo.rebuild_unified_kg(nb, force=True)
    preempted = [e for e in events if e.get("kind") == "kg_generation_preempted"]
    assert [e["stage"] for e in preempted] == ["write:concept"], preempted
    concept = [c for c in calls if c[0] == "concept"]
    assert len(concept) == 1 and len(rechecks) == 1, (concept, rechecks)
    lost = concept[0][1]
    # exactly the first page stays behind, invisible to readers
    assert _rows_in_generation(repo, nb, lost, postgres=postgres) == [("concept",)] * 2
    assert _published(repo, nb, postgres=postgres) == reference

    # 3. The next rebuild reaps the stranded page and publishes in full.
    monkeypatch.setattr(store, "derived_claim_still_held", original_held)
    repo.rebuild_unified_kg(nb, force=True)
    assert _rows_in_generation(repo, nb, lost, postgres=postgres) == []
    assert _published(repo, nb, postgres=postgres) == reference
