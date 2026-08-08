"""PR-B: the per-source relink must produce the SAME edges as the whole-graph one.

The production path no longer loads the notebook's objects, evidence and relations
in one go; it walks one source at a time. That is only a memory change if the edge
set is identical, so these tests pin the equivalence directly: an oracle built from
the historical ``relink_rows`` shape (still on the store, reference-only) computes
what the old code would have written, and the paged run has to match it edge for
edge — plus the same isolated_before / edges_added / isolated_after counters.

The fixture is built to break a naive decomposition:
  · cross-source edges (must not be re-proposed, and must still be visible);
  · a node whose ONLY edge is cross-source — invisible to a per-source relation
    read that filtered by source, and therefore wrongly re-linked as isolated;
  · a source with no objects at all;
  · objects carrying an empty source_id and objects pointing at a deleted source —
    partitions that a loop driven off the `sources` table would silently drop.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.kg.relink import complete_isolated_edges
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _insert_source(repo, notebook_id, source_id):
    now = _now()
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, 'T', 'markdown', 'extracted', 'parsed',
                       'f.md', '', 0, '', '', 'academic_paper', ?, ?)""",
            (source_id, notebook_id, now, now),
        )


def _insert_object(repo, notebook_id, source_id, object_type, name, element_ids,
                   status="approved", oid=None):
    oid = oid or f"ko-{uuid4().hex[:10]}"
    now = _now()
    evidence = [{"source_id": source_id, "element_id": eid,
                 "quoted_span": name, "confidence": 1.0} for eid in element_ids]
    with repo._write() as db:
        db.execute(
            """INSERT INTO knowledge_objects
               (id, notebook_id, object_type, status, owner, payload, evidence,
                source_candidate_id, source_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, '', ?, ?, NULL, ?, ?, ?)""",
            (oid, notebook_id, object_type, status,
             json.dumps({"name": name}, ensure_ascii=False),
             json.dumps(evidence, ensure_ascii=False), source_id, now, now),
        )
    return oid


def _insert_relation(repo, notebook_id, source_id, src, tgt, edge_type="about"):
    with repo._write() as db:
        db.execute(
            """INSERT INTO knowledge_relations
               (id, notebook_id, source_id, source_object_id, target_object_id,
                edge_type, evidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, '[]', ?)""",
            (f"rel-{uuid4().hex[:10]}", notebook_id, source_id, src, tgt,
             edge_type, _now()),
        )


def _oracle(repo, notebook_id):
    """What the historical whole-notebook implementation would have produced.

    Deliberately re-derived from ``relink_rows`` (the frozen reference reader) and
    the untouched pure core, not from the production method under test.
    """
    with repo._connect() as db:
        obj_rows, rel_rows, _valid = repo._runtime.knowledge.relink_rows(
            db, notebook_id
        )
    nodes = []
    for r in obj_rows:
        payload = json.loads(r["payload"] or "{}")
        evidence = json.loads(r["evidence"] or "[]")
        nodes.append({
            "id": r["id"],
            "object_type": r["object_type"],
            "name": payload.get("name", ""),
            "source_id": r["source_id"] or "",
            "element_ids": {
                ev.get("element_id") for ev in evidence
                if isinstance(ev, dict) and ev.get("element_id")
            },
        })
    edges = [(r["source_object_id"], r["target_object_id"]) for r in rel_rows]
    connected = {oid for pair in edges for oid in pair}
    isolated_before = sum(1 for n in nodes if n["id"] not in connected)
    existing = {
        (r["source_object_id"], r["target_object_id"], r["edge_type"])
        for r in rel_rows
    }
    proposed = []
    for e in complete_isolated_edges(nodes, edges):
        triple = (e["source_object_id"], e["target_object_id"], e["edge_type"])
        if triple in existing:
            continue
        existing.add(triple)
        proposed.append(triple)
    now_connected = connected | {oid for t in proposed for oid in (t[0], t[1])}
    return {
        "edges": set(proposed),
        "stats": {
            "isolated_before": isolated_before,
            "edges_added": len(proposed),
            "isolated_after": sum(
                1 for n in nodes if n["id"] not in now_connected
            ),
        },
    }


def _written_edges(repo, notebook_id):
    return {
        (r["source_object_id"], r["target_object_id"], r["edge_type"])
        for r in repo.relations_for_notebook(notebook_id)
        if any(
            str(item.get("basis", "")).startswith("relink:")
            for item in (r.get("evidence") or [])
            if isinstance(item, dict)
        )
    }


def _build_multi_source_notebook(repo):
    """Sources A/B/C/D plus the two source-less partitions."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    for sid in ("src-a", "src-b", "src-c", "src-d"):
        _insert_source(repo, nb, sid)

    ids = {}
    # --- src-a: a plain isolated claim+concept pair sharing an element.
    ids["a_claim"] = _insert_object(
        repo, nb, "src-a", "claim", "Engram improves perplexity", ["ea1"])
    ids["a_concept"] = _insert_object(
        repo, nb, "src-a", "concept", "Engram", ["ea1"])
    # A concept in src-a that also shares the element — competing candidate, so
    # the per-node cap and the candidate ordering are actually exercised.
    ids["a_concept2"] = _insert_object(
        repo, nb, "src-a", "concept", "Engram memory layer", ["ea1"])

    # --- src-b: the trap. `b_bridge` has NO intra-source edge; its only edge
    # crosses into src-c. A per-source view that could not see cross-source edges
    # would call it isolated and link it to `b_concept`.
    ids["b_bridge"] = _insert_object(
        repo, nb, "src-b", "claim", "Bridge claim about Engram", ["eb1"])
    ids["b_concept"] = _insert_object(
        repo, nb, "src-b", "concept", "Engram", ["eb1"])
    ids["c_concept"] = _insert_object(
        repo, nb, "src-c", "concept", "Engram", ["ec1"])
    _insert_relation(repo, nb, "src-b", ids["b_bridge"], ids["c_concept"])

    # --- src-c: a claim already linked intra-source (idempotency / existing pair).
    ids["c_claim"] = _insert_object(
        repo, nb, "src-c", "claim", "Retrieval augments Engram", ["ec1"])
    _insert_relation(repo, nb, "src-c", ids["c_claim"], ids["c_concept"])

    # --- src-d: exists as a source row but owns no objects at all.

    # --- '' partition: objects with no source id are siblings of each other in
    # the historical implementation; keep that exactly.
    ids["blank_claim"] = _insert_object(
        repo, nb, "", "claim", "Unattributed claim about Engram", ["ez1"])
    ids["blank_concept"] = _insert_object(
        repo, nb, "", "concept", "Engram", ["ez1"])

    # --- orphan partition: source row deleted, objects survive (no FK).
    ids["gone_claim"] = _insert_object(
        repo, nb, "src-gone", "claim", "Orphaned claim about Engram", ["eg1"])
    ids["gone_concept"] = _insert_object(
        repo, nb, "src-gone", "concept", "Engram", ["eg1"])

    # --- a deprecated object must stay out of both the node set and the counts.
    _insert_object(repo, nb, "src-a", "concept", "Deprecated Engram", ["ea1"],
                   status="deprecated")
    return nb, ids


def test_paged_relink_matches_the_whole_graph_oracle(repo):
    nb, ids = _build_multi_source_notebook(repo)

    expected = _oracle(repo, nb)
    stats = repo.relink_notebook_kg(nb)

    assert stats == expected["stats"]
    assert _written_edges(repo, nb) == expected["edges"]
    # Guard the guard: a fixture where nothing is proposed would pass vacuously.
    assert expected["stats"]["edges_added"] > 0


def test_cross_source_connected_node_is_never_relinked(repo):
    """The one divergence the per-source relation read exists to prevent.

    ``claim_x``'s only edge leaves the source. Every node in src-x is therefore
    connected and relink must write nothing. Hide cross-source edges from the
    per-source read and ``claim_x`` reads as isolated, shares element ``ex1`` with
    ``concept_x``, and gets an about edge it must never have — so this asserts a
    silence that only the correct decomposition produces.
    """
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _insert_source(repo, nb, "src-x")
    _insert_source(repo, nb, "src-y")
    claim_x = _insert_object(
        repo, nb, "src-x", "claim", "Engram claim that bridges out", ["ex1"])
    concept_x = _insert_object(repo, nb, "src-x", "concept", "Engram", ["ex1"])
    claim_x2 = _insert_object(
        repo, nb, "src-x", "claim", "Second Engram claim", ["ex2"])
    concept_y = _insert_object(repo, nb, "src-y", "concept", "Engram", ["ey1"])
    # The only edge out of claim_x crosses into src-y.
    _insert_relation(repo, nb, "src-x", claim_x, concept_y)
    # ...and an ordinary intra-source edge keeps concept_x/claim_x2 connected too.
    _insert_relation(repo, nb, "src-x", claim_x2, concept_x)

    expected = _oracle(repo, nb)
    stats = repo.relink_notebook_kg(nb)

    assert expected["stats"]["edges_added"] == 0
    assert stats == expected["stats"]
    assert stats["isolated_before"] == 0
    assert _written_edges(repo, nb) == set()
    assert claim_x and concept_x  # ids kept readable in failure output


def test_source_less_and_orphan_partitions_are_not_dropped(repo):
    """A loop driven off `sources` would silently skip both of these."""
    nb, ids = _build_multi_source_notebook(repo)
    repo.relink_notebook_kg(nb)

    written = _written_edges(repo, nb)
    assert (ids["blank_claim"], ids["blank_concept"], "about") in written
    assert (ids["gone_claim"], ids["gone_concept"], "about") in written

    # FK safety: the relation row of a partition whose source row is gone (or
    # blank) must store NULL, exactly like the whole-notebook version did.
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_object_id, source_id FROM knowledge_relations "
            "WHERE notebook_id=? AND source_object_id IN (?, ?)",
            (nb, ids["blank_claim"], ids["gone_claim"]),
        ).fetchall()
    assert {r["source_id"] for r in rows} == {None}
    assert len(rows) == 2


def test_paged_relink_is_idempotent_and_stays_equivalent(repo):
    nb, _ids = _build_multi_source_notebook(repo)
    first = repo.relink_notebook_kg(nb)
    after_first = _written_edges(repo, nb)

    second = repo.relink_notebook_kg(nb)

    assert second["edges_added"] == 0
    assert _written_edges(repo, nb) == after_first
    assert second["isolated_before"] == first["isolated_after"]
    # And a fresh oracle over the now-linked graph agrees there is nothing left.
    assert _oracle(repo, nb)["stats"]["edges_added"] == 0


def test_paged_relink_matches_oracle_across_a_page_boundary(repo, monkeypatch):
    """Force many keyset pages so the resume cursor itself is exercised."""
    import app.services.knowledge_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "_RELINK_SOURCE_PAGE_SIZE", 1)

    nb, _ids = _build_multi_source_notebook(repo)
    expected = _oracle(repo, nb)
    stats = repo.relink_notebook_kg(nb)

    assert stats == expected["stats"]
    assert _written_edges(repo, nb) == expected["edges"]


def test_relink_never_reads_the_whole_notebook(repo, monkeypatch):
    """The bounded-read guard: no production path may call `relink_rows`.

    Mutation check for T6 — restoring the old whole-notebook read makes this fail.
    """
    from app.repositories.sqlite.knowledge_store import KnowledgeStore

    def _boom(*_args, **_kwargs):
        raise AssertionError("relink_rows is reference-only, not a live read")

    nb, _ids = _build_multi_source_notebook(repo)
    monkeypatch.setattr(KnowledgeStore, "relink_rows", staticmethod(_boom))
    stats = repo.relink_notebook_kg(nb)
    assert stats["edges_added"] > 0


def test_object_id_batching_does_not_change_the_answer(repo, monkeypatch):
    """Relation reads batch object ids; a batch boundary must be invisible."""
    import app.services.knowledge_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "_RELINK_ID_BATCH_SIZE", 1)

    nb, _ids = _build_multi_source_notebook(repo)
    expected = _oracle(repo, nb)
    stats = repo.relink_notebook_kg(nb)

    assert stats == expected["stats"]
    assert _written_edges(repo, nb) == expected["edges"]
