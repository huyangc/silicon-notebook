"""P4-T6: every pending-merge row carries the display names of its two sides
(``name_a`` / ``name_b``), so the knowledge-graph view shows the concepts'
names instead of the ``K-<seed>`` ids (which S1 turns into ``rmsnorm`` /
``~v1:...`` strings).

* A name is the PUBLISHED cluster's stored ``canonical_name``, read by one
  bounded point lookup per distinct id of the response
  (``GovernanceStore.pending_merge_names``); an id no published cluster row
  carries gets ``""`` and the browser shows a placeholder, never the id.
* The names follow the KG detail reads' cluster label rule
  (``KgViewerScope.cluster_labels``): where the notebook holds a hidden source
  the viewer may not read, a cluster that may hold a hidden member is named by
  its first visible member. A row with a side that has no visible live member
  is left out for that viewer: the cluster does not exist for them (the KG
  detail reads' rule), and its id carries a hidden member's seed. Every other
  row is returned to every reader (pending merges stay a shared tool).

The ``assert_*`` functions are shared with the PostgreSQL twin
(tests/postgres/test_pending_merge_names_pg.py): both backends must answer
the same rows, byte for byte (the random candidate ids aside).
"""
from __future__ import annotations

import random

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from tests.test_kg_viewer_scope import as_user
from tests.test_kg_viewer_scope import build_scenario as build_viewer_scenario

NOW = "2026-10-19T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _is_postgres(repo) -> bool:
    return type(repo).__name__ == "PostgresRepository"


def _sql(repo, statement: str) -> str:
    return statement.replace("?", "%s") if _is_postgres(repo) else statement


def _concept(local_id, name, span):
    return {
        "local_id": local_id, "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": "D", "element_id": "e",
            "element_type": "p", "location_label": "1", "quoted_span": span,
            "confidence": 1.0,
        }],
    }


def _without_ids(rows):
    return [{k: v for k, v in row.items() if k != "id"} for row in rows]


def names_world(repo) -> str:
    """Two published clusters with names that differ from their seeds
    ("ZORBLAX" under ``K-zorblax``, "Quixel Cascade" under
    ``K-quixelcascade`` — seed space 2), a third ("Vantor Gap"), and two
    candidates: one
    between the first two, one naming an id no cluster row carries. A row of
    a NON-published generation for ``K-zorblax``, whose member id sorts before
    every real member and whose name is ``STALE NAME``, must never answer."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    with repo._runtime.database.write() as db:
        for source_id in ("s1", "s2"):
            db.execute(_sql(repo,
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                (source_id, nb, source_id, "md", "ready", NOW, NOW))
    repo.store_kg(nb, "s1", [_concept("a1", "ZORBLAX", "z1"),
                             _concept("b1", "Quixel Cascade", "q1")], [])
    repo.store_kg(nb, "s2", [_concept("a2", "ZORBLAX", "z2"),
                             _concept("c2", "Vantor Gap", "v2")], [])
    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(nb)
    governance = repo._runtime.knowledge_governance
    governance.write_merge_candidate(nb, "K-zorblax", "K-quixelcascade", 0.91)
    governance.write_merge_candidate(nb, "K-vantorgap", "K-not formed yet", 0.85)
    with repo._runtime.database.write() as db:
        db.execute(_sql(repo,
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,object_type,created_at,generation) "
            "VALUES (?,?,?,?,?,?,?,?)"),
            ("cc-stale", nb, "K-zorblax", "0-stale", "STALE NAME", "concept", NOW, 999))
    return nb


EXPECTED_NAMES = [
    {"canonical_a": "K-zorblax", "canonical_b": "K-quixelcascade",
     "score": 0.91, "status": "pending",
     "name_a": "ZORBLAX", "name_b": "Quixel Cascade", "kind": "similarity"},
    {"canonical_a": "K-vantorgap", "canonical_b": "K-not formed yet",
     "score": 0.85, "status": "pending",
     "name_a": "Vantor Gap", "name_b": "", "kind": "similarity"},
]


def assert_rows_carry_both_sides_names(repo) -> None:
    nb = names_world(repo)
    rows = repo._runtime.knowledge_governance.pending_merges(nb)
    assert _without_ids(rows) == EXPECTED_NAMES
    assert all(str(row["id"]).startswith("mc-") for row in rows)
    assert "STALE" not in repr(rows)


def assert_hidden_members_never_name_a_side(repo, build_scenario, as_user) -> None:
    """The pre-isolation shape (a cluster mixing a visible source and A's
    Memory) with a stored name minted from A's Memory member, plus a cluster
    only A's Memory forms. B (a member) reads the visible member's name; A
    reads both stored names.

    A side with no visible live member does not exist for B -- the KG detail
    reads' rule for such a cluster (absent from search and the graph, 404 on
    its detail) -- so B does not get the row at all: its ``canonical_b`` is
    ``K-<seed>``, and that seed is the Memory member's normalised name. The
    whole response is scanned for the private spellings. A, who can read
    every member, still gets the row, unchanged."""
    s = build_scenario(repo, b_memory=False)
    with repo._runtime.database.write() as db:
        db.execute(_sql(repo,
            "UPDATE concept_clusters SET canonical_name=? "
            "WHERE notebook_id=? AND canonical_id=?"),
            ("Engram A-PRIVATE spelling", s.nb, s.ids.engram_canonical))
    governance = repo._runtime.knowledge_governance
    governance.write_merge_candidate(
        s.nb, s.ids.engram_canonical, s.ids.secret_canonical, 0.88)
    # a row whose both sides B can see: kept, named by the first visible member
    governance.write_merge_candidate(
        s.nb, s.ids.engram_canonical, "K-not formed yet", 0.5)
    base = {"canonical_a": s.ids.engram_canonical,
            "canonical_b": s.ids.secret_canonical,
            "score": 0.88, "status": "pending", "kind": "similarity"}
    visible = {"canonical_a": s.ids.engram_canonical, "canonical_b": "K-not formed yet",
               "score": 0.5, "status": "pending", "kind": "similarity"}
    member = as_user(s.b, governance.pending_merges, s.nb)
    assert _without_ids(member) == [{**visible, "name_a": "Engram", "name_b": ""}]
    text = repr(member).lower()
    secret_seed = s.ids.secret_canonical[2:].lower()
    for private in ("a-private", "secretproject", secret_seed,
                    s.ids.secret_canonical.lower()):
        assert private not in text, private
    owner = as_user(s.a, governance.pending_merges, s.nb)
    assert sorted(_without_ids(owner), key=lambda row: -row["score"]) == [
        {**base, "name_a": "Engram A-PRIVATE spelling", "name_b": "SecretProject"},
        {**visible, "name_a": "Engram A-PRIVATE spelling", "name_b": ""},
    ]


def assert_names_cost_one_statement_per_batch(repo, monkeypatch) -> None:
    """No name read for an empty list; otherwise one call per 900 distinct
    ids of the response, sorted, never more."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    governance = repo._runtime.knowledge_governance
    store = governance.governance_store
    calls = []
    real = store.pending_merge_names

    def spy(db, notebook_id, canonical_ids):
        calls.append(list(canonical_ids))
        return real(db, notebook_id, canonical_ids)

    monkeypatch.setattr(store, "pending_merge_names", spy)
    assert governance.pending_merges(nb) == []
    assert calls == []
    for i in range(500):
        governance.write_merge_candidate(nb, f"K-a{i:04d}", f"K-b{i:04d}", 0.9)
    # a repeated side is read once
    governance.write_merge_candidate(nb, "K-a0000", "K-b0001", 0.9)
    rows = governance.pending_merges(nb)
    assert len(rows) == 501 and {row["name_a"] for row in rows} == {""}
    assert [len(batch) for batch in calls] == [900, 100]
    flat = [cid for batch in calls for cid in batch]
    assert flat == sorted(flat) and len(set(flat)) == 1000


def test_rows_carry_both_sides_names(repo):
    """Mutation anchors: drop the published-generation predicate -> the
    stale row answers ``K-zorblax`` (``STALE NAME``); stop attaching the names
    -> the dict comparison fails."""
    assert_rows_carry_both_sides_names(repo)


def test_hidden_members_never_name_a_side(repo):
    """Mutation anchors: drop the ``cluster_labels`` pass -> B reads the
    private spelling and ``SecretProject``; keep the rows whose side has no
    visible member -> B reads the Memory-minted ``K-<seed>``."""
    assert_hidden_members_never_name_a_side(repo, build_viewer_scenario, as_user)


def test_names_cost_one_statement_per_batch(repo, monkeypatch):
    assert_names_cost_one_statement_per_batch(repo, monkeypatch)


# ------------------------------------------------------------------ plan pin
#
# Production SQLite carries no planner statistics (the repository never runs
# ANALYZE), so the pin holds in both states. The statement is the one the
# store actually issues (captured through the connection's trace callback,
# parameters inlined), on a production-shaped corpus: hub clusters with
# thousands of members, a retired generation interleaved with the published
# one, an in-flight generation, and other notebooks' rows. Run on SQLite 3.45
# (CI) and 3.53 (local) -- see docs/development.md.

def _seed_plan_corpus(repo) -> tuple[str, list[str]]:
    rng = random.Random(7)
    target = repo.create_notebook(NotebookCreate(name="target")).id
    others = [repo.create_notebook(NotebookCreate(name=f"o{i}")).id for i in range(4)]
    rows = []

    def cluster(nb, cid, members, generations):
        for m in range(members):
            member = f"{nb}-{cid}-m{rng.randrange(10**9):09d}"
            for gen in generations:
                rows.append((f"cc-{len(rows)}", nb, cid, member, cid.upper(),
                             "concept", NOW, gen))

    # target: 2 hubs x 4 000 members, 3 000 small clusters, published gen 2,
    # retired gen 1 on every member, in-flight gen 3 on the hubs
    for hub in ("K-hub0", "K-hub1"):
        cluster(target, hub, 4000, (1, 2, 3))
    for i in range(3000):
        cluster(target, f"K-c{i:05d}", 1 + i % 4, (1, 2))
    for nb in others:
        for i in range(2000):
            cluster(nb, f"K-c{i:05d}", 3, (2,))
    with repo._runtime.database.write() as db:
        db.executemany(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) VALUES (?,?,?,?,?,?,?,?)",
            rows)
        for nb in [target, *others]:
            db.execute(
                "INSERT INTO unified_kg_state (notebook_id,cluster_generation,updated_at) "
                "VALUES (?,2,?) ON CONFLICT(notebook_id) DO UPDATE SET cluster_generation=2",
                (nb, NOW))
    ids = ["K-hub0", "K-hub1"] + [f"K-c{i:05d}" for i in range(0, 3000, 3)][:898]
    return target, sorted(ids)


def test_name_lookup_seeks_each_id_on_the_canonical_member_index(repo):
    target, ids = _seed_plan_corpus(repo)
    store = repo._runtime.knowledge_governance.governance_store
    for state in ("no statistics", "analyzed"):
        with repo._runtime.database.connect() as db:
            if state == "analyzed":
                db.execute("ANALYZE")
            statements = []
            db.set_trace_callback(statements.append)
            names = store.pending_merge_names(db, target, ids)
            db.set_trace_callback(None)
            assert len(statements) == 1, statements
            assert len(names) == 900
            # the hub answers its published row's name, one row per id
            assert names["K-hub0"] == "K-HUB0" and names["K-c00000"] == "K-C00000"
            plan = [str(row["detail"]) for row in
                    db.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall()]
        text = " | ".join(plan)
        assert any(
            "SEARCH cc USING INDEX idx_clusters_nb_canonical_member_gen "
            "(notebook_id=? AND canonical_id=?)" in line for line in plan), (state, text)
        assert not any(line.startswith("SCAN cc") for line in plan), (state, text)
        assert "TEMP B-TREE" not in text, (state, text)
        assert any(line.startswith("SCAN c VIRTUAL TABLE") for line in plan), (state, text)
