"""Oracle: the graph-side keyset paging ≡ the whole-table reads it replaced.

Batch-3 W4 T-W4-3.1 turned six whole-notebook ``.fetchall()`` reads on the
offline build path into keyset-paginated scans. Paging is only admissible if
the rows — and, for every leg whose ORDER BY is load-bearing, their ORDER —
are identical, so the reference implementations below are the PRE-CHANGE SQL
copied verbatim and run against the same database inside the same test.

Every case runs at a page size of 3 as well as the production default: a bug
in a cursor, a boundary, or a per-page predicate is invisible when the whole
notebook fits in one page and obvious when it does not.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.postgres import chunk_store as chunk_store_module
from app.repositories.postgres import index_projection_store as projection_module
from app.repositories.postgres import knowledge_store as knowledge_store_module
from app.repositories.postgres.repository import PostgresRepository
from app.repositories.postgres.search import PAYLOAD_NAME_EXPRESSION
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_offline_maintenance"),
]

NOW = "2026-09-04T00:00:00"


@pytest.fixture
def repo(postgres_settings: Settings):
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def small_page(monkeypatch, request):
    """Force every paged read onto a tiny page so the cursors are exercised."""
    size = request.param
    monkeypatch.setattr(projection_module, "_GRAPH_FETCH_BATCH", size)
    monkeypatch.setattr(knowledge_store_module, "GRAPH_FETCH_BATCH", size)
    monkeypatch.setattr(chunk_store_module, "GRAPH_FETCH_BATCH", size)
    return size


def _seed(repo, *, objects: int = 11) -> str:
    notebook = repo.create_notebook(NotebookCreate(name="paging oracle"))
    nodes = []
    edges = []
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            ("src-1", notebook.id, "paging corpus", "md", "ready", NOW, NOW),
        )
        for index in range(objects):
            element_id = f"el-{index}"
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"chunk-{index}", notebook.id, "src-1",
                    f"Mixture of Experts variant {index}.", "S",
                    json.dumps([element_id]), NOW,
                ),
            )
            nodes.append({
                "local_id": f"n{index}",
                "object_type": "concept",
                "payload": {"name": f"expert router v{index}", "section_path": ""},
                "evidence": [{"element_id": element_id}],
            })
            if index:
                edges.append({
                    "source_local_id": f"n{index}",
                    "target_local_id": f"n{index - 1}",
                    "edge_type": "depends_on",
                    "evidence": [],
                })
    repo.store_kg(notebook.id, "src-1", nodes, edges)
    repo.rebuild_unified_kg(notebook.id)
    return notebook.id


# ────────────────────────────── the pre-change reads, copied verbatim ──

def _reference_graph_reads(db, notebook_id: str) -> dict:
    """``graph_rows``' four gathers exactly as they were before paging."""
    ph = ",".join("%s" for _ in USABLE_STATUSES)
    objects = db.execute(
        f"SELECT id, object_type, payload FROM knowledge_objects "
        f"WHERE notebook_id=%s AND status IN ({ph}) "
        f"ORDER BY ordinal, id COLLATE \"C\"",
        (notebook_id, *USABLE_STATUSES)).fetchall()
    relations = db.execute(
        "SELECT source_object_id, target_object_id, edge_type "
        "FROM knowledge_relations "
        "WHERE notebook_id=%s AND review_status!='rejected' "
        "ORDER BY id COLLATE \"C\"",
        (notebook_id,)).fetchall()
    chunks = db.execute(
        "SELECT id FROM chunks WHERE notebook_id=%s "
        "ORDER BY ordinal, id COLLATE \"C\"",
        (notebook_id,)).fetchall()
    clusters = db.execute(
        "SELECT canonical_id, member_object_id FROM concept_clusters "
        "WHERE notebook_id=%s "
        "AND generation = COALESCE((SELECT cluster_generation "
        "FROM unified_kg_state WHERE notebook_id = %s), 0) "
        "ORDER BY canonical_id COLLATE \"C\", member_object_id COLLATE \"C\"",
        (notebook_id, notebook_id)).fetchall()
    active = db.execute(
        f"SELECT id,object_type,{PAYLOAD_NAME_EXPRESSION} AS name "
        "FROM knowledge_objects "
        "WHERE notebook_id=%s AND status!='deprecated' ORDER BY ordinal",
        (notebook_id,)).fetchall()
    evidence = db.execute(
        "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=%s",
        (notebook_id,)).fetchall()
    elements = db.execute(
        "SELECT id,element_ids FROM chunks WHERE notebook_id=%s ORDER BY ordinal",
        (notebook_id,)).fetchall()
    return {
        "objects": [(r["id"], r["object_type"]) for r in objects],
        "relations": [
            (r["source_object_id"], r["target_object_id"], r["edge_type"])
            for r in relations
        ],
        "chunks": [r["id"] for r in chunks],
        "clusters": [(r["canonical_id"], r["member_object_id"]) for r in clusters],
        "active": [(r["id"], r["object_type"], r["name"]) for r in active],
        "evidence": sorted(r["id"] for r in evidence),
        "elements": [r["id"] for r in elements],
    }


def _paged_graph_reads(repo, db, notebook_id: str) -> dict:
    projections = repo._runtime.index_projections
    return {
        "active": [
            (r["id"], r["object_type"], r["name"])
            for r in projections.active_object_graph_rows(db, notebook_id)
        ],
        # The evidence read is DUAL-MODE: the online entry point keeps the
        # pre-paging unordered whole-table statement, and only the offline
        # build's `_paged` one is keyset-paged. Both must return the same rows.
        "evidence": sorted(
            r["id"] for r in
            repo._runtime.knowledge.notebook_object_evidence_rows_paged(
                db, notebook_id
            )
        ),
        "evidence_online": sorted(
            r["id"] for r in
            repo._runtime.knowledge.notebook_object_evidence_rows(db, notebook_id)
        ),
        "elements": [
            r["id"] for r in
            repo._runtime.chunk_store.id_element_rows(db, notebook_id)
        ],
    }


# ──────────────────────────────────────────────────────────────── oracles ──

@pytest.mark.parametrize("small_page", [3, 10_000], indirect=True)
def test_paged_reads_return_the_same_rows_in_the_same_order(repo, small_page):
    notebook_id = _seed(repo)
    with repo._connect() as db:
        want = _reference_graph_reads(db, notebook_id)
        got = _paged_graph_reads(repo, db, notebook_id)

    assert len(want["active"]) == 11
    assert got["active"] == want["active"]
    assert got["evidence"] == want["evidence"]
    assert got["evidence_online"] == want["evidence"]
    assert got["elements"] == want["elements"]


class _StatementSpy:
    """Transparent connection proxy that records the SQL text of every
    statement, so a test can pin HOW MANY reads a call issues and what shape
    they have — the only observable difference between the evidence read's two
    modes."""

    def __init__(self, db, on_execute=None):
        self._db = db
        self._on_execute = on_execute
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append(text)
        result = (
            self._db.execute(statement)
            if params is None
            else self._db.execute(statement, params)
        )
        if self._on_execute is not None:
            self._on_execute(self._db, text, len(self.statements))
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.mark.parametrize("small_page", [3], indirect=True)
def test_the_online_evidence_read_stays_one_unordered_statement(repo, small_page):
    """Double-review fix A. ``_ent_chunk_map`` runs this read on the first
    graph-mode PPR question after every KG version bump; the ``ORDER BY id``
    keyset paging needs costs +31% there (and spills an external merge sort on
    a notebook that fits in one page), so the online entry point must keep the
    pre-paging statement and only the build's ``_paged`` one may page.

    Mutation anchors, both behavioral: route the online entry point through
    the paged one and the single-statement/no-ORDER-BY assertions go red;
    make the paged entry point issue one whole-table read and the page-count
    assertion goes red. The two must also return the same rows, which is what
    lets them share one version-cache entry."""
    notebook_id = _seed(repo)
    knowledge = repo._runtime.knowledge

    with repo._connect() as db:
        online = _StatementSpy(db)
        online_rows = knowledge.notebook_object_evidence_rows(online, notebook_id)
        paged = _StatementSpy(db)
        paged_rows = list(
            knowledge.notebook_object_evidence_rows_paged(paged, notebook_id)
        )

    assert sorted(r["id"] for r in online_rows) == sorted(
        r["id"] for r in paged_rows
    )
    assert len(online_rows) == 11

    assert len(online.statements) == 1
    assert "ORDER BY" not in online.statements[0].upper()
    assert "LIMIT" not in online.statements[0].upper()

    # 11 rows at a page size of 3 → 3 + 3 + 3 + 2, the short page ending it.
    assert len(paged.statements) == 4
    assert all("ORDER BY id" in text for text in paged.statements)
    assert all("LIMIT" in text.upper() for text in paged.statements)


@pytest.mark.parametrize("small_page", [3], indirect=True)
def test_a_generation_flip_between_cluster_pages_cannot_tear_the_graph(
    repo, small_page, monkeypatch
):
    """Double-review fix B. The published generation is resolved ONCE and
    bound into every page. With the old inline
    ``COALESCE((SELECT cluster_generation ...), 0)`` scalar subquery the
    predicate was re-evaluated per page, so a generation published between two
    pages (READ COMMITTED sees it immediately) split one scan across two
    generations — the same member arriving under two canonical ids, a state
    ``uq_clusters_nb_type_member_generation`` makes impossible within one
    generation and which nothing downstream defends against.

    This test commits that flip from inside the scan, right after the first
    cluster page. Mutation anchor: put the scalar subquery back in place of
    the bound parameter and the next generation's hub appears in node_ids."""
    notebook_id = _seed(repo)
    with repo._connect() as db:
        published = int(db.execute(
            "SELECT cluster_generation FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["cluster_generation"])
        members = [
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=%s "
                "ORDER BY ordinal", (notebook_id,)
            ).fetchall()
        ]
    assert len(members) > small_page, "the flip must land between two pages"

    state = {"cluster_pages": 0}

    def flip_after_first_cluster_page(db, text, _index):
        if "concept_clusters" not in text or not text.lstrip().upper().startswith(
            "SELECT"
        ):
            return
        state["cluster_pages"] += 1
        if state["cluster_pages"] != 1:
            return
        # A newer generation becomes the published one, mid-scan.
        db.execute(
            "UPDATE unified_kg_state SET cluster_generation=%s WHERE notebook_id=%s",
            (published + 1, notebook_id),
        )
        for index, member in enumerate(members):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,"
                "canonical_description,created_at,generation) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"cc-next-{index}", notebook_id, "next-generation-canonical",
                    member, "next generation", "concept", "", NOW,
                    published + 1,
                ),
            )

    projections = repo._runtime.index_projections
    real_connect = projections.connect

    @contextmanager
    def flipping_connect():
        with real_connect() as db:
            yield _StatementSpy(db, on_execute=flip_after_first_cluster_page)

    monkeypatch.setattr(projections, "connect", flipping_connect)
    rows = projections.graph_rows(notebook_id, None, synonym_edges=[])

    assert state["cluster_pages"] > 1, "the cluster leg must span several pages"
    assert "cluster:next-generation-canonical" not in rows.node_ids
    assert not any(
        "next-generation-canonical" in str(node) for node in rows.node_ids
    )


def _graph_rows_at(monkeypatch, repo, notebook_id: str, page: int):
    with monkeypatch.context() as patch:
        patch.setattr(projection_module, "_GRAPH_FETCH_BATCH", page)
        projections = repo._runtime.index_projections
        rows = projections.graph_rows(notebook_id, None, synonym_edges=[])
        arrays = projections.graph_rows(
            notebook_id, None, synonym_edges=[], as_arrays=True
        )
    source, target, weight = arrays.edges
    return {
        "node_ids": list(rows.node_ids),
        "edges": [(a, b, float(w)) for a, b, w in rows.edges],
        "chunk_ids": list(rows.chunk_ids),
        "kg_node_ids": list(rows.kg_node_ids),
        "membership_counts": dict(rows.membership_counts),
        "array_node_ids": list(arrays.node_ids),
        "array_edges": [
            (int(s), int(t), float(w)) for s, t, w in zip(source, target, weight)
        ],
    }


def test_the_gathered_graph_is_bit_identical_at_every_page_size(repo, monkeypatch):
    """The whole ScaleGraphRows — node order, edge order, weights, the int
    array encoding, the IDF membership counts — must not move with the page
    size. A page size above the row count reproduces the pre-paging shape (one
    statement, no cursor), so this compares the new implementation against
    itself-as-the-old one across the boundaries a page size of 1 forces."""
    notebook_id = _seed(repo)

    whole = _graph_rows_at(monkeypatch, repo, notebook_id, 10_000)
    for page in (1, 2, 3, 7):
        assert _graph_rows_at(monkeypatch, repo, notebook_id, page) == whole, page

    # ...and the unpaged shape itself still matches the verbatim pre-change SQL.
    with repo._connect() as db:
        want = _reference_graph_reads(db, notebook_id)
    assert whole["kg_node_ids"] == [oid for oid, _type in want["objects"]]
    assert whole["chunk_ids"] == want["chunks"]
    assert whole["node_ids"] == whole["array_node_ids"]
    assert whole["edges"], "the seeded corpus must produce edges at all"
    # Every surviving relation appears in both directions at weight 1.0.
    pairs = {(a, b) for a, b, _w in whole["edges"]}
    for source_id, target_id, _edge_type in want["relations"]:
        assert (source_id, target_id) in pairs
        assert (target_id, source_id) in pairs
    # The array encoding decodes back to exactly the string edge set.
    index = {node: position for position, node in enumerate(whole["node_ids"])}
    assert {
        (whole["node_ids"][s], whole["node_ids"][t], w)
        for s, t, w in whole["array_edges"]
    } == set(whole["edges"])
    assert all(0 <= s < len(index) and 0 <= t < len(index)
               for s, t, _w in whole["array_edges"])


@pytest.mark.parametrize("small_page", [3], indirect=True)
def test_every_cluster_page_re_applies_the_published_generation_predicate(
    repo, small_page
):
    """W2 red line: version identity only ever counts the PUBLISHED cluster
    generation. Hoisting the predicate to the first page (a natural-looking
    "the cursor already narrows it" optimisation) leaks an unpublished
    generation's members into the graph from page two onward, AND loses the
    Index-Only Scan `idx_clusters_nb_canonical_member_gen`'s INCLUDE exists
    for. Mutation anchor: apply the predicate only when the cursor is None and
    this goes red."""
    notebook_id = _seed(repo)
    with repo._connect() as db:
        published = db.execute(
            "SELECT cluster_generation FROM unified_kg_state WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()["cluster_generation"]
        members = [
            row["id"] for row in db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=%s "
                "ORDER BY ordinal", (notebook_id,)
            ).fetchall()
        ]
    assert len(members) > small_page, "the leak must be reachable past page one"

    with repo._write() as db:
        for index, member in enumerate(members):
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,"
                "canonical_description,created_at,generation) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"cc-unpublished-{index}", notebook_id,
                    "unpublished-canonical", member, "unpublished", "concept",
                    "", NOW, int(published) + 1,
                ),
            )

    rows = repo._runtime.index_projections.graph_rows(
        notebook_id, None, synonym_edges=[]
    )
    assert "cluster:unpublished-canonical" not in rows.node_ids
    assert not any(
        "unpublished-canonical" in str(node) for node in rows.node_ids
    )
