"""PR-A·A5 object rule edge cases and cost bounds (rulings Q4 / M1).

The object rule (``KgViewerScope.row_hidden``): an object is hidden from a
viewer when its OWN ``source_id`` is a hidden source the viewer may not read
(whatever its evidence says — merged and promoted objects carry the union),
or when its evidence cites such a source and nothing the viewer may read.
Scenario helpers come from tests/test_kg_viewer_scope.py.
"""
from __future__ import annotations

import json

import pytest

from tests.test_kg_viewer_scope import (  # noqa: F401  (``repo`` is a fixture)
    _ev,
    _memory,
    _now,
    _object_id,
    _source,
    as_user,
    build_scenario,
    reader_of,
    repo,
)


def _cluster_of(repo, nb, object_id):
    return repo.cluster_map(nb)[object_id]


# ------------------------------------------------------ owner column (5a, 5d)
def _add_owned_objects(repo, s):
    """Objects OWNED by A's Memory whose evidence is visible or empty."""
    repo.store_kg(s.nb, "src-ma", [
        # Merged into the Engram cluster, occurrence only in the visible source.
        {"local_id": "m", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "A-PRIVATE merged"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        # No evidence at all.
        {"local_id": "n", "object_type": "claim",
         "payload": {"name": "A-PRIVATE bare claim", "section_path": "1"},
         "evidence": []},
    ], [{"source_local_id": "n", "target_local_id": "m", "edge_type": "about",
         "evidence": []}])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        merged = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id='src-ma' "
            "AND json_extract(payload,'$.section_path')='A-PRIVATE merged'", (s.nb,),
        ).fetchone()["id"]
        bare = _object_id(db, s.nb, "A-PRIVATE bare claim", "src-ma")
    return merged, bare


def test_owner_column_hides_objects_whatever_their_evidence(repo):
    s = build_scenario(repo, b_memory=False)
    merged, bare = _add_owned_objects(repo, s)
    canonical = _cluster_of(repo, s.nb, s.ids.engram_s)
    assert _cluster_of(repo, s.nb, merged) == canonical
    for oid in (merged, bare):
        with pytest.raises(KeyError):
            as_user(s.b, repo.node_context, s.nb, oid)
        assert as_user(s.a, repo.node_context, s.nb, oid)["id"] == oid
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert [m["id"] for m in detail["members"]] == [s.ids.engram_s]
    assert detail["member_total"] == 1
    assert "A-PRIVATE" not in repr(detail)
    owner = as_user(s.a, repo.concept_detail, s.nb, canonical)
    assert merged in {m["id"] for m in owner["members"]}
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert bare not in {n["id"] for n in view["nodes"]}
    assert "A-PRIVATE" not in repr(view)
    assert bare in {n["id"] for n in as_user(s.a, repo.kg_neighbors, s.nb, merged)["nodes"]}


# ------------------------------------------------ mixed member evidence (4a)
def test_visible_member_never_returns_its_foreign_evidence_items(repo):
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [{
        "local_id": "c", "object_type": "concept",
        "payload": {"name": "Engram", "section_path": "1"},
        "evidence": [
            _ev("src-s", "el-s-def"),
            _ev("src-ma", "el-ma-occ"),
            # Labelled visible, but the element lives in A's Memory: only
            # enrichment (which resolves source_id from the element row) can
            # tell, so the post-enrich filter is what drops it.
            {**_ev("src-s", "el-ma-def"), "quoted_span": "mislabelled"},
        ]}], [])
    repo.rebuild_unified_kg(s.nb)
    canonical = _cluster_of(repo, s.nb, s.ids.engram_s)
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    member_blob = repr(detail["members"])
    assert "el-ma-occ" not in member_blob and "src-ma" not in member_blob
    evidence_blob = repr(detail["evidence"])
    assert "A-PRIVATE" not in evidence_blob and "src-ma" not in evidence_blob
    assert any(e.get("element_text") == "VISIBLE definition of Engram"
               for e in detail["evidence"])


# ---------------------------------------------------------- fail closed (4c)
@pytest.mark.parametrize("endpoint", ["context", "concept", "neighbors"])
def test_a_scope_that_cannot_be_built_fails_the_request(repo, monkeypatch, endpoint):
    s = build_scenario(repo, b_memory=False)
    reader = reader_of(repo)

    def boom(*_a, **_k):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(reader.sources, "memory_source_ids", boom)
    call = {
        "context": (repo.node_context, (s.nb, s.ids.engram_ma)),
        "concept": (repo.concept_detail, (s.nb, s.ids.secret_canonical)),
        "neighbors": (repo.kg_neighbors, (s.nb, s.ids.engram_s)),
    }[endpoint]
    with pytest.raises(RuntimeError):
        as_user(s.b, call[0], *call[1])


# ------------------------------------ evidence-only hidden, reverse index (3)
def _add_dangling(repo, s):
    """Owned by the VISIBLE source, evidence only A's Memory + a source id
    that no longer exists: hidden by the evidence half, NOT in the owned set."""
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "d", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "DANGLING"},
         "evidence": [_ev("src-ma", "el-ma-def"), _ev("src-gone", "el-gone")]},
        {"local_id": "k", "object_type": "claim",
         "payload": {"name": "DANGLING claim", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-def"), _ev("src-gone", "el-gone")]},
    ], [{"source_local_id": "k", "target_local_id": "d", "edge_type": "about",
         "evidence": []}])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        dangling = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id='src-s' "
            "AND json_extract(payload,'$.section_path')='DANGLING'", (s.nb,),
        ).fetchone()["id"]
        claim = _object_id(db, s.nb, "DANGLING claim", "src-s")
    return dangling, claim


@pytest.mark.parametrize("certified", [False, True])
def test_evidence_only_hidden_objects_with_and_without_a_certified_reverse_index(
    repo, certified,
):
    """The reverse index is never read on these paths, so certifying it must
    not change a single answer; the evidence half is judged on each row."""
    s = build_scenario(repo, b_memory=False)
    dangling, claim = _add_dangling(repo, s)
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=? WHERE notebook_id=?",
            (1 if certified else 0, s.nb),
        )
    for oid in (dangling, claim):
        with pytest.raises(KeyError):
            as_user(s.b, repo.node_context, s.nb, oid)
    canonical = _cluster_of(repo, s.nb, s.ids.engram_s)
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert [m["id"] for m in detail["members"]] == [s.ids.engram_s]
    assert detail["member_total"] == 1
    assert "DANGLING" not in repr(detail)
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert claim not in {n["id"] for n in view["nodes"]}


# ------------------------------------------------ paging property (3, 4d)
def _fixed_hub(repo, s, pattern):
    """A cluster whose members have FIXED ids ``ko-hub-NN`` in pattern order:
    V visible, H owned by A's Memory, D evidence-only hidden (dangling)."""
    rows = []
    for index, kind in enumerate(pattern):
        source_id = "src-ma" if kind == "H" else "src-s"
        evidence = {
            "V": [_ev("src-s", "el-s-occ")],
            "H": [_ev("src-ma", "el-ma-occ")],
            "D": [_ev("src-ma", "el-ma-def"), _ev("src-gone", "el-gone")],
        }[kind]
        rows.append((
            f"ko-hub-{index:02d}", s.nb, "concept", "approved",
            json.dumps({"name": "Hubword", "section_path": kind}),
            json.dumps(evidence), source_id, _now(index), _now(index),
        ))
    with repo._write() as db:
        repo._runtime.knowledge.insert_object_chunk(db, rows)
    repo.rebuild_unified_kg(s.nb)
    return _cluster_of(repo, s.nb, "ko-hub-00")


@pytest.mark.parametrize("pattern,limit", [
    ("VHHHHH", 1),        # hidden tail after the only visible member
    ("HHVHHV", 1),        # hidden head, visible on every page boundary
    ("VVDHVV", 2),        # evidence-only hidden right after a full page
    ("VDDDDV", 1),        # a run of evidence-only hidden longer than any over-fetch
    ("DHVHDVHVD", 2),
    ("VVVVVVV", 3),
    ("HDVVHDVV", 200),
    ("HHHH", 2),          # nothing visible: a 404 on the first page
])
def test_concept_pages_are_exact_around_hidden_members(repo, pattern, limit):
    s = build_scenario(repo, b_memory=False)
    canonical = _fixed_hub(repo, s, pattern)
    expected = [f"ko-hub-{i:02d}" for i, kind in enumerate(pattern) if kind == "V"]
    if not expected:
        with pytest.raises(KeyError):
            as_user(s.b, repo.concept_detail, s.nb, canonical, limit=limit)
        return
    seen, after, total, pages = [], "", None, 0
    while True:
        page = as_user(s.b, repo.concept_detail, s.nb, canonical, limit=limit, after=after)
        pages += 1
        if total is None:
            total = page["member_total"]
        else:
            assert page["member_total"] is None
        seen.extend(m["id"] for m in page["members"])
        if not page["next_cursor"]:
            break
        assert len(page["members"]) == limit, (pattern, limit, page["members"])
        after = page["next_cursor"]
        assert pages < 50
    assert seen == expected
    assert total == len(expected)
    # A reads its own Memory, so every member (D cites it) is A's to see.
    owner = as_user(s.a, repo.concept_detail, s.nb, canonical, limit=200)
    assert owner["member_total"] == len(pattern)


# ------------------------------------------------- cap counts visible (5b)
@pytest.mark.parametrize("path", ["viz", "db"])
def test_neighbour_cap_counts_visible_neighbours(repo, monkeypatch, path):
    s = build_scenario(repo, b_memory=False)
    hidden = [{"local_id": f"h{i}", "object_type": "claim",
               "payload": {"name": f"A-PRIVATE claim {i}", "section_path": "1"},
               "evidence": [_ev("src-ma", "el-ma-occ")]} for i in range(6)]
    repo.store_kg(s.nb, "src-ma", hidden + [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Capword", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-occ")]}],
        [{"source_local_id": f"h{i}", "target_local_id": "hub", "edge_type": "about",
          "evidence": []} for i in range(6)])
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Capword", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]}] + [
        {"local_id": f"v{i}", "object_type": "claim",
         "payload": {"name": f"visible claim {i}", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-def")]} for i in range(3)],
        [{"source_local_id": f"v{i}", "target_local_id": "hub", "edge_type": "about",
          "evidence": []} for i in range(3)])
    repo.rebuild_unified_kg(s.nb)
    if path == "db":
        monkeypatch.setattr(repo._runtime.scale_artifacts, "viz_index",
                            lambda *_a, **_k: None)
    with repo._write() as db:
        hub = _object_id(db, s.nb, "Capword", "src-s")
    view = as_user(s.b, repo.kg_neighbors, s.nb, hub, cap=3)
    focus = view["focus_id"]
    neighbours = [n for n in view["nodes"] if n["id"] != focus]
    assert len(neighbours) == 3, view
    assert all(n["payload"]["name"].startswith("visible claim") for n in neighbours)
    capped = as_user(s.b, repo.kg_neighbors, s.nb, hub, cap=2)
    assert len([n for n in capped["nodes"] if n["id"] != capped["focus_id"]]) == 2


# ------------------------------------------------------- statement counts (3)
def _count_statements(repo, monkeypatch):
    database = repo._runtime.database
    original = database.connect
    statements: list = []

    class Counting:
        def __init__(self, cm):
            self.cm = cm

        def __enter__(self):
            self.db = self.cm.__enter__()
            self.db.set_trace_callback(statements.append)
            return self.db

        def __exit__(self, *exc):
            self.db.set_trace_callback(None)
            return self.cm.__exit__(*exc)

    monkeypatch.setattr(database, "connect", lambda *a, **k: Counting(original(*a, **k)))
    return statements


def _foreign_memories(repo, s, count, *, offset=0):
    """``count`` extra Memory sources of A, each owning one concept of its
    own (outside the probed clusters)."""
    names = [f"{j:03d}" for j in range(offset, offset + count)]
    with repo._write() as db:
        for j in names:
            _memory(db, s.nb, f"mem-x{j}", s.a.id)
            _source(db, s.nb, f"src-x{j}", memory_id=f"mem-x{j}",
                    elements=[(f"el-x{j}", f"X{j}")])
    for j in names:
        repo.store_kg(s.nb, f"src-x{j}", [{
            "local_id": "c", "object_type": "concept",
            "payload": {"name": f"Private topic {j}", "section_path": "1"},
            "evidence": [_ev(f"src-x{j}", f"el-x{j}")]}], [])
    repo.rebuild_unified_kg(s.nb)


def _endpoint_counts(repo, monkeypatch, s):
    statements = _count_statements(repo, monkeypatch)
    counts = {}
    for name, fn, args in (
        ("context", repo.node_context, (s.nb, s.ids.engram_s)),
        ("concept", repo.concept_detail, (s.nb, s.ids.engram_canonical)),
        ("neighbors", repo.kg_neighbors, (s.nb, s.ids.engram_s)),
    ):
        statements.clear()
        as_user(s.b, fn, *args)
        counts[name] = len(statements)
    monkeypatch.undo()
    return counts


def test_statement_count_is_constant_in_the_number_of_unreadable_sources(
    repo, monkeypatch,
):
    """Each endpoint issues the same number of statements with 31 and with 301
    unreadable Memory sources: the owned-object set is ONE statement
    (``relink_object_rows_for_source(source_ids=...)``), partly hidden
    clusters of a neighbourhood are read in ONE batched statement, and nothing
    reads ``source_index_backfilled``, the reverse index or a per-cluster
    COUNT. (The owned objects here stay under one 900-id fold batch.)"""
    s = build_scenario(repo, b_memory=False)
    _foreign_memories(repo, s, 30)
    few = _endpoint_counts(repo, monkeypatch, s)
    _foreign_memories(repo, s, 270, offset=30)
    many = _endpoint_counts(repo, monkeypatch, s)
    assert few == many, (few, many)
    assert few["context"] <= 30 and few["concept"] <= 30 and few["neighbors"] <= 40, few


def test_owned_object_read_seeks_the_source_index(repo):
    """The set-valued owner read is driven by the id list: SQLite seeks the
    source index once per listed id instead of scanning the notebook. The
    statement pinned is the one the store actually issues."""
    s = build_scenario(repo, b_memory=False)
    statements = []
    with repo._runtime.database.connect() as db:
        db.set_trace_callback(statements.append)
        repo._runtime.knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=["src-ma", "src-mb"])
        db.set_trace_callback(None)
        assert len(statements) == 1, statements
        plan = " | ".join(
            str(tuple(row)) for row in
            db.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()
        )
    assert "idx_knowledge_objects_source (source_id=?)" in plan, plan
    assert "SCAN knowledge_objects" not in plan, plan
    assert "idx_knowledge_objects_nb_updated" not in plan, plan


def test_set_valued_store_reads(repo):
    """``source_ids=`` / ``canonical_ids=``: owned live objects only, an empty
    list issues no statement, and each cluster is cut to ``limit`` rows."""
    s = build_scenario(repo, b_memory=False)
    knowledge = repo._runtime.knowledge
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?",
                   (s.ids.definer_ma,))
    with repo._runtime.database.connect() as db:
        owned = {r["id"] for r in knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=["src-ma", "src-none"])}
        statements = []
        db.set_trace_callback(statements.append)
        assert knowledge.relink_object_rows_for_source(db, s.nb, source_ids=[]) == []
        assert knowledge.concept_cluster_detail_rows(
            db, s.nb, "", limit=1, canonical_ids=[]) == ([], "")
        db.set_trace_callback(None)
        assert statements == []
        rows, name = knowledge.concept_cluster_detail_rows(
            db, s.nb, "", limit=1,
            canonical_ids=[s.ids.engram_canonical, s.ids.secret_canonical])
    assert owned == {s.ids.engram_ma, s.ids.secret}
    assert name == ""
    by_cluster = {}
    for row in rows:
        by_cluster.setdefault(row["canonical_id"], []).append(row["member_object_id"])
    assert by_cluster == {
        s.ids.engram_canonical: [min(s.ids.engram_s, s.ids.engram_ma)],
        s.ids.secret_canonical: [s.ids.secret],
    }


def _mixed_neighbour_clusters(repo, s, names):
    """Concepts linked to the visible Engram, each clustering a visible member
    with a member owned by A's Memory (partly hidden clusters)."""
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]}] + [
        {"local_id": n, "object_type": "concept",
         "payload": {"name": n, "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-def")]} for n in names],
        [{"source_local_id": n, "target_local_id": "hub", "edge_type": "about",
          "evidence": []} for n in names])
    repo.store_kg(s.nb, "src-ma", [
        {"local_id": n, "object_type": "concept",
         "payload": {"name": n, "section_path": "A-PRIVATE"},
         "evidence": [_ev("src-ma", "el-ma-occ")]} for n in names], [])
    repo.rebuild_unified_kg(s.nb)


def test_partly_hidden_clusters_are_read_in_one_batch(repo, monkeypatch):
    """Neighbour hydration probes every partly hidden cluster of the
    response with ONE batched read: the statement count does not grow with
    the number of such clusters."""
    s = build_scenario(repo, b_memory=False)
    _mixed_neighbour_clusters(repo, s, [f"Mixed{i}" for i in range(2)])
    few = _endpoint_counts(repo, monkeypatch, s)["neighbors"]
    _mixed_neighbour_clusters(repo, s, [f"Mixed{i}" for i in range(2, 8)])
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    labels = sorted(n["payload"]["name"] for n in view["nodes"]
                    if n["payload"]["name"].startswith("Mixed"))
    assert labels == [f"Mixed{i}" for i in range(8)], view
    many = _endpoint_counts(repo, monkeypatch, s)["neighbors"]
    assert few == many, (few, many)
