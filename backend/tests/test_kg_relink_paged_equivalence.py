"""PR-B: the per-source relink must agree with the whole-graph one.

The production path no longer loads the notebook's objects, evidence and relations
in one go; it walks one source at a time. That is only a memory change if the answer
survives, so these tests pin the equivalence directly: an oracle built from the
historical ``relink_rows`` shape (still on the store, reference-only) computes what
the old code would have written, and the paged run has to match it — edge for edge
plus the same isolated_before / edges_added / isolated_after counters.

"Edge for edge" holds with ONE registered exception, pinned by its own case at the
bottom of this file: the per-source read pins ``ORDER BY rowid`` while the historical
query had no ``ORDER BY`` and inherited the planner's ``updated_at`` index order. On
a notebook where those two orders disagree AND the per-node cap binds, the two passes
can bind an isolated node to different — equally valid — same-source partners. The
COUNTS still have to match exactly.

The fixture is built to break a naive decomposition:
  · cross-source edges (must not be re-proposed, and must still be visible);
  · a node whose ONLY edge is cross-source — invisible to a per-source relation
    read that filtered by source, and therefore wrongly re-linked as isolated;
  · a source with no objects at all;
  · objects carrying an empty source_id and objects pointing at a deleted source —
    partitions that a loop driven off the `sources` table ALONE would silently drop
    (the production driver walks `sources` and unions in a one-shot orphan query
    precisely to cover them).
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
                   status="approved", oid=None, updated_at=None):
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
             json.dumps(evidence, ensure_ascii=False), source_id, now,
             updated_at or now),
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
    """The production driver walks `sources`; these two partitions have no row
    there and only exist because of the one-shot orphan query. Drop that query and
    both of these edges disappear."""
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


def _kg_mutation_seq(repo, notebook_id: int | str) -> int:
    with repo._connect() as db:
        row = db.execute(
            "SELECT COALESCE(kg_mutation_seq, 0) AS seq FROM unified_kg_state "
            "WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
    return int(row["seq"]) if row else 0


def test_a_partial_run_still_publishes_the_change_signal(repo, monkeypatch):
    """Writes are committed per source, so a mid-notebook failure leaves REAL edges
    in the database. Those edges must not escape ``kg_mutation_seq``.

    Publishing only after the loop completes (the shape this replaced) is exactly
    the bug: ``mark_unified_kg_dirty`` is documented as the single choke point every
    mutation funnels through, and the ``_cluster_input_version`` fallback COUNT does
    not count relations — so a missed bump makes 「刷新图谱」 short-circuit for that
    notebook permanently, with no way for a later run to notice.
    """
    nb, _ids = _build_multi_source_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._relink_one_source
    calls = {"n": 0}

    def _boom_after_the_first_source(notebook_id, source_id, written):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(notebook_id, source_id, written)
        raise RuntimeError("this source's read blew up mid-notebook")

    monkeypatch.setattr(
        lifecycle, "_relink_one_source", _boom_after_the_first_source
    )
    invalidated: list[str] = []
    monkeypatch.setattr(
        lifecycle, "_invalidate_unified_cache", invalidated.append
    )
    seq_before = _kg_mutation_seq(repo, nb)

    with pytest.raises(RuntimeError):
        repo.relink_notebook_kg(nb)

    # Guard the guard: the aborted run must really have committed something,
    # otherwise "the signal was published" would be vacuously satisfiable.
    assert calls["n"] == 2
    assert _written_edges(repo, nb), "first partition wrote no edge — fixture broken"
    assert _kg_mutation_seq(repo, nb) > seq_before
    assert invalidated == [nb]


def test_a_failed_write_publishes_no_signal(repo, monkeypatch):
    """The realistic partial-failure mode, and the one that pins WHERE the ledger
    is written: right after the transaction commits, never before it.

    Move the accounting one line up (before ``insert_relation_chunk``) and a run
    whose write blew up starts claiming edges it never wrote — publishing a KG
    change signal for a graph that did not change.
    """
    nb, _ids = _build_multi_source_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    invalidated: list[str] = []
    monkeypatch.setattr(
        lifecycle, "_invalidate_unified_cache", invalidated.append
    )

    def _write_blows_up(_db, _rows):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(
        lifecycle.knowledge, "insert_relation_chunk", _write_blows_up
    )
    seq_before = _kg_mutation_seq(repo, nb)

    with pytest.raises(RuntimeError):
        repo.relink_notebook_kg(nb)

    assert _written_edges(repo, nb) == set()
    assert _kg_mutation_seq(repo, nb) == seq_before
    assert invalidated == []


def test_a_run_that_writes_nothing_publishes_no_signal(repo, monkeypatch):
    """The other half of the same predicate: 'any edge committed', not 'the loop
    raised'. A failing run with zero writes must stay silent."""
    nb, _ids = _build_multi_source_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle

    def _boom(_notebook_id, _source_id, _written):
        raise RuntimeError("failed before writing anything")

    monkeypatch.setattr(lifecycle, "_relink_one_source", _boom)
    invalidated: list[str] = []
    monkeypatch.setattr(
        lifecycle, "_invalidate_unified_cache", invalidated.append
    )
    seq_before = _kg_mutation_seq(repo, nb)

    with pytest.raises(RuntimeError):
        repo.relink_notebook_kg(nb)

    assert _written_edges(repo, nb) == set()
    assert _kg_mutation_seq(repo, nb) == seq_before
    assert invalidated == []


def test_relink_one_source_advances_the_signal_without_relink_notebook_kg_ever_running(
    repo,
):
    """P1 fix: kg_mutation_seq must be durable per source, not deferred to
    ``relink_notebook_kg``'s ``finally`` — a real kill -9 lands between a
    source's own commit and that finally ever executing, and nothing in a
    Python test can survive a hard process kill to assert on afterwards. The
    property that survives it is testable directly, though: call
    ``_relink_one_source`` on its own and NEVER call ``relink_notebook_kg`` at
    all, so its ``finally`` block does not run — not even via a caught
    exception. If the signal is still durable, it can only have been written
    by ``_relink_one_source`` itself, atomically with its own edge insert.
    """
    nb, _ids = _build_multi_source_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    seq_before = _kg_mutation_seq(repo, nb)
    written = {"edges": 0}

    lifecycle._relink_one_source(nb, "src-a", written)

    assert written["edges"] > 0, "fixture wrote no edges for src-a — test is vacuous"
    assert _kg_mutation_seq(repo, nb) > seq_before


def test_partial_failure_leaves_the_first_sources_signal_durable_before_the_run_finishes(
    repo, monkeypatch,
):
    """Stronger than ``test_a_partial_run_still_publishes_the_change_signal``:
    that test only observes the FINAL state, after the RuntimeError has
    propagated all the way out through ``relink_notebook_kg``'s ``finally`` —
    which cannot distinguish "the first source's own transaction already
    published the signal" from "the finally published it on the way out,
    because Python's exception handling ran it". This test captures the state
    the instant the SECOND source is entered — strictly inside the `for` loop,
    before the run has failed, before its `finally` has had any chance to run
    — and pins that the first source's contribution is already durable by
    then, with the second (about-to-fail) source having contributed nothing
    yet.
    """
    nb, _ids = _build_multi_source_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._relink_one_source
    calls = {"n": 0}
    observed_at_second_call: dict = {}

    def _boom_after_the_first_source(notebook_id, source_id, written):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(notebook_id, source_id, written)
        # About to process the SECOND (failing) partition. Snapshot right
        # here, before doing anything else — this is what the first source's
        # own transaction left behind, observed from strictly inside the
        # still-running loop, nowhere near relink_notebook_kg's finally.
        observed_at_second_call["seq"] = _kg_mutation_seq(repo, notebook_id)
        observed_at_second_call["written_so_far"] = dict(written)
        raise RuntimeError("this source's read blew up mid-notebook")

    monkeypatch.setattr(
        lifecycle, "_relink_one_source", _boom_after_the_first_source
    )
    seq_before = _kg_mutation_seq(repo, nb)

    with pytest.raises(RuntimeError):
        repo.relink_notebook_kg(nb)

    assert calls["n"] == 2
    assert observed_at_second_call["written_so_far"]["edges"] > 0, (
        "first partition wrote no edge — fixture broken"
    )
    assert observed_at_second_call["seq"] > seq_before


# ---------------------------------------------------------------------------
# The one registered divergence: intra-source candidate order under the cap
# ---------------------------------------------------------------------------

def _build_capped_source(repo):
    """One source where the per-node cap binds and input ORDER decides the loser.

    Four claims and two concepts all share element ``ecap``. Each claim can take
    both concepts, but a concept accepts at most ``max_per_node``(=3) relink edges,
    so the first three claims in processing order consume the concepts' budget and
    the fourth gets nothing. Reverse the order and a different claim loses — same
    edge count, different edges.

    ``updated_at`` is written in reverse of insertion order, which is what the
    historical unordered query sorted by in practice.
    """
    nb = repo.create_notebook(NotebookCreate(name="cap")).id
    _insert_source(repo, nb, "src-cap")
    claims = []
    total = 4
    for index in range(total):
        claims.append(_insert_object(
            repo, nb, "src-cap", "claim", f"Claim number {index}", ["ecap"],
            oid=f"ko-cap-claim-{index}",
            # 2026-01-0{4,3,2,1}: descending, i.e. the exact reverse of rowid order.
            updated_at=f"2026-01-0{total - index}T00:00:00+00:00",
        ))
    concepts = [
        _insert_object(repo, nb, "src-cap", "concept", "Alpha resonance", ["ecap"],
                       oid="ko-cap-concept-a",
                       updated_at="2026-01-09T00:00:00+00:00"),
        _insert_object(repo, nb, "src-cap", "concept", "Beta resonance", ["ecap"],
                       oid="ko-cap-concept-b",
                       updated_at="2026-01-08T00:00:00+00:00"),
    ]
    return nb, claims, concepts


def _core_triples(repo, notebook_id, ordered_ids):
    """Run the untouched pure core over this exact node order."""
    with repo._connect() as db:
        rows = {
            r["id"]: r for r in db.execute(
                "SELECT id, object_type, source_id, payload, evidence "
                "FROM knowledge_objects WHERE notebook_id=?", (notebook_id,),
            ).fetchall()
        }
    nodes = []
    for oid in ordered_ids:
        r = rows[oid]
        nodes.append({
            "id": r["id"],
            "object_type": r["object_type"],
            "name": json.loads(r["payload"] or "{}").get("name", ""),
            "source_id": r["source_id"] or "",
            "element_ids": {
                ev.get("element_id") for ev in json.loads(r["evidence"] or "[]")
                if isinstance(ev, dict) and ev.get("element_id")
            },
        })
    return {
        (e["source_object_id"], e["target_object_id"], e["edge_type"])
        for e in complete_isolated_edges(nodes, [])
    }


def test_intra_source_order_may_pick_a_different_partner_but_never_a_different_count(
    repo,
):
    """The registered, accepted divergence from "edge for edge".

    ``relink_object_rows_for_source`` pins ``ORDER BY rowid``; the historical
    whole-notebook query had no ``ORDER BY`` and was served from
    ``idx_knowledge_objects_nb_updated``, i.e. ``updated_at`` order. Under the
    per-node cap that difference is observable — so this test states exactly what
    is and is not promised: counts identical, partner identity not.

    The first half proves the divergence is real without depending on any planner:
    the untouched pure core is handed the two orders directly.
    """
    nb, claims, _concepts = _build_capped_source(repo)

    by_rowid = _core_triples(repo, nb, claims + _concepts_of(repo, nb))
    by_updated = _core_triples(
        repo, nb, list(reversed(claims)) + _concepts_of(repo, nb)
    )
    assert by_rowid != by_updated, (
        "fixture no longer exercises the per-node cap — without an order-sensitive "
        "case this test would pass vacuously"
    )
    assert len(by_rowid) == len(by_updated)

    expected = _oracle(repo, nb)
    stats = repo.relink_notebook_kg(nb)
    written = _written_edges(repo, nb)

    # Counts are the contract, and they hold exactly.
    assert stats == expected["stats"]
    assert len(written) == expected["stats"]["edges_added"]
    # The paged pass writes the rowid-order answer, by construction.
    assert written == by_rowid
    # ...and the divergence really happens END TO END on this fixture, not just in
    # the pure core above: the historical reader is served from
    # idx_knowledge_objects_nb_updated, so the oracle picks the other partner. If
    # this ever stops holding (a planner change, an ANALYZE, an index edit), the
    # registered exception no longer reproduces and the docs claiming it must be
    # re-checked rather than this assertion relaxed.
    assert written != expected["edges"], (
        "fixture no longer reproduces the registered updated_at-vs-rowid divergence"
    )
    # Every written edge stays inside the source — partitioning never invents a
    # cross-source link, whichever partner won.
    source_ids = set(claims) | set(_concepts_of(repo, nb))
    assert all(src in source_ids and tgt in source_ids for src, tgt, _ in written)


def _concepts_of(repo, notebook_id):
    with repo._connect() as db:
        return [
            r["id"] for r in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? "
                "AND object_type='concept' ORDER BY rowid", (notebook_id,),
            ).fetchall()
        ]
