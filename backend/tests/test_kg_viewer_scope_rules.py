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


def _foreign_memories(repo, s, count):
    """``count`` extra Memory sources of A, each owning one concept of its
    own (outside the probed clusters)."""
    with repo._write() as db:
        for j in range(count):
            _memory(db, s.nb, f"mem-x{j:03d}", s.a.id)
            _source(db, s.nb, f"src-x{j:03d}", memory_id=f"mem-x{j:03d}",
                    elements=[(f"el-x{j:03d}", f"X{j}")])
    for j in range(count):
        repo.store_kg(s.nb, f"src-x{j:03d}", [{
            "local_id": "c", "object_type": "concept",
            "payload": {"name": f"Private topic {j}", "section_path": "1"},
            "evidence": [_ev(f"src-x{j:03d}", f"el-x{j:03d}")]}], [])
    repo.rebuild_unified_kg(s.nb)


def _endpoint_counts(repo, monkeypatch, foreign_count):
    s = build_scenario(repo, b_memory=False)
    _foreign_memories(repo, s, foreign_count)
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
    return counts


@pytest.mark.parametrize("foreign_count", [30, 300])
def test_statement_count_scales_only_with_the_owned_object_read(
    repo, monkeypatch, foreign_count,
):
    """Object context is flat in the number of foreign Memory sources. Concept
    detail and neighbours are flat apart from ONE term: the per-source
    ``relink_object_rows_for_source`` read that builds the owned-object set
    (one statement per unreadable hidden source, index-seeked), which a
    set-valued store read would collapse to one. Everything else — no
    ``source_index_backfilled``, no reverse index, no per-cluster COUNT — is a
    constant (1 foreign source of the base scenario + ``foreign_count``)."""
    counts = _endpoint_counts(repo, monkeypatch, foreign_count)
    unreadable = 1 + foreign_count
    assert counts["context"] <= 30, counts
    assert counts["concept"] - unreadable <= 30, counts
    assert counts["neighbors"] - unreadable <= 40, counts
