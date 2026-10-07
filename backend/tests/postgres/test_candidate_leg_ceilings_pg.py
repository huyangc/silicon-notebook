"""PostgreSQL twin of tests/test_candidate_leg_ceilings.py (E2-2).

Same scenarios, seeded with PostgreSQL SQL through the repository's own
database: the overlay's 1-hop walk judges the current notebook's nodes by the
local ceiling (another member's Memory, and the asker's own Memory with the
channel closed, never reach the prompt); a notebook without Memory renders
exactly what the unchecked walk renders; the shared relation matrix and the
keyword-token cache hold no Memory content.  See the SQLite file's docstring
for the closed-channel shape (blank ``owner_id``, empty hidden half).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.domain.vector_index import encode_vector
from app.models.notebooks import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_candidate_leg_ceilings"),
]

T0 = datetime(2026, 9, 30, tzinfo=timezone.utc)
QUERY = "Mixture-of-Experts MoE"


@pytest.fixture
def repo(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    repository.settings.graph_ppr_enabled = False
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def people(repo):
    bob = repo.create_user("b00654321", "pw123456")
    alice = repo.create_user("a00123456", "pw123456")
    token = set_request_user(bob)
    try:
        yield bob.id, alice.id
    finally:
        reset_request_user(token)


def _ev(source_id, element_id, quote):
    return {"source_id": source_id, "source_title": "", "element_id": element_id,
            "element_type": "paragraph", "location_label": "p1",
            "quoted_span": quote, "quote": quote, "confidence": 1.0}


class _Seed:
    def __init__(self, repo, name):
        self.repo = repo
        self.nb = repo.create_notebook(NotebookCreate(name=name)).id

    def _write(self):
        return self.repo._runtime.database.write()

    def doc(self, source_id):
        with self._write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES (%s,%s,'paper','markdown','extracted','parsed',%s,%s)",
                (source_id, self.nb, T0, T0),
            )

    def memory(self, source_id, owner):
        memory_id = f"mem-{source_id}"
        with self._write() as db:
            db.execute(
                "INSERT INTO memory_items (id,notebook_id,created_by,origin,status,"
                "title,content_md,created_at,updated_at) VALUES "
                "(%s,%s,%s,'ask_answer','confirmed','t','c',%s,%s)",
                (memory_id, self.nb, owner, T0, T0),
            )
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,memory_id,created_at,updated_at) "
                "VALUES (%s,%s,'memory','memory','extracted','parsed',%s,%s,%s)",
                (source_id, self.nb, memory_id, T0, T0),
            )

    def obj(self, object_id, source_id, name, evidence):
        """Row plus reverse-index rows, as the store writes them."""
        with self._write() as db:
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
                "owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (%s,%s,'concept','approved','',%s::jsonb,%s::jsonb,%s,%s,%s)",
                (object_id, self.nb, json.dumps({"name": name}),
                 json.dumps(evidence), source_id, T0, T0),
            )
            for source in sorted({item["source_id"] for item in evidence}):
                db.execute(
                    "INSERT INTO knowledge_object_sources (object_id,source_id,"
                    "notebook_id) VALUES (%s,%s,%s)",
                    (object_id, source, self.nb),
                )

    def rel(self, relation_id, source_id, src, tgt, evidence):
        with self._write() as db:
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,created_at) "
                "VALUES (%s,%s,%s,%s,%s,'kind_of',%s::jsonb,%s)",
                (relation_id, self.nb, source_id, src, tgt, json.dumps(evidence), T0),
            )


def _scope(visible, hidden, owner):
    return {
        "mode": "include", "source_ids": list(visible),
        "hidden_source_ids": list(hidden), "narrowed": False, "owner_id": owner,
    }


def _overlay(repo, nb, scope=None):
    with source_scope_context(nb, scope):
        block, id_map, _hits, supports = (
            repo.retrieval.candidates._chunk_kg_overlay(nb, QUERY, QUERY, 1000)
        )
    return block, id_map, supports


def _objects(id_map):
    return {str(entry.get("object_id") or "") for entry in id_map.values()}


@pytest.fixture
def walk(repo, people):
    bob, alice = people
    seed = _Seed(repo, "kb")
    seed.doc("src-doc")
    seed.memory("src-mem-bob", bob)
    doc = [_ev("src-doc", "elA", "MoE routing")]
    memory = [_ev("src-mem-bob", "elM", "SECRETMEMO ZEBRAQUARTZ")]
    seed.obj("e1", "src-doc", "Mixture-of-Experts (MoE)", doc)
    seed.obj("e3", "src-mem-bob", "ZEBRAQUARTZ plan", memory)
    seed.obj("e4", "src-doc", "Router balance",
             [_ev("src-doc", "elC", "load balance loss")])
    seed.rel("rM", "src-mem-bob", "e1", "e3", memory)
    seed.rel("rX", "src-mem-bob", "e1", "e4", memory)
    return repo, seed.nb, bob, alice


def test_pg_open_channel_control_renders_the_askers_own_memory_node(walk):
    repo, nb, bob, _alice = walk

    block, id_map, _ = _overlay(repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob))

    assert "ZEBRAQUARTZ" in block and "--kind_of-->" in block
    assert {"e1", "e3", "e4"} <= _objects(id_map)


def test_pg_closed_channel_never_renders_the_askers_own_memory(walk):
    """The drift probe agrees with the freeze (no channel off); the verdict
    binds (a blank owner reads no Memory, so every Memory source counts as
    foreign), so this is the per-node check's bound path."""
    repo, nb, _bob, _alice = walk

    with source_scope_context(nb, _scope(["src-doc"], [], "")):
        assert repo.retrieval.candidates._unsafe_source_scope_restricted(nb) is False
    block, id_map, _ = _overlay(repo, nb, _scope(["src-doc"], [], ""))

    assert "Mixture-of-Experts" in block
    assert "ZEBRAQUARTZ" not in block and "--kind_of-->" not in block
    assert "e3" not in _objects(id_map)


def test_pg_another_members_memory_never_reaches_the_prompt(walk):
    repo, nb, _bob, alice = walk

    block, id_map, _ = _overlay(repo, nb, _scope(["src-doc"], [], alice))

    assert "ZEBRAQUARTZ" not in block and "--kind_of-->" not in block
    assert "Router balance" in block and "e3" not in _objects(id_map)


def test_pg_a_notebook_without_memory_renders_byte_identically(
    repo, people, monkeypatch,
):
    bob, _alice = people
    seed = _Seed(repo, "plain")
    seed.doc("src-doc")
    doc = [_ev("src-doc", "elA", "MoE routing")]
    seed.obj("e1", "src-doc", "Mixture-of-Experts (MoE)", doc)
    seed.obj("e4", "src-doc", "Router balance", doc)
    seed.obj("e6", "src-doc", "Capacity factor", [])
    seed.rel("rD", "src-doc", "e1", "e4", doc)
    seed.rel("rE", "src-doc", "e1", "e6", doc)
    scope = _scope(["src-doc"], [], bob)

    checked = _overlay(repo, seed.nb, scope)
    monkeypatch.setattr(
        repo.retrieval.candidates, "_ceiling_scoped_subgraph",
        lambda subgraph, _scope: subgraph,
    )
    unchecked = _overlay(repo, seed.nb, scope)

    assert "Capacity factor" in checked[0]
    assert checked == unchecked


def test_pg_shared_relation_matrix_and_live_memory_relations(repo, people):
    bob, alice = people
    seed = _Seed(repo, "relations")
    seed.doc("src-doc")
    seed.memory("src-mem-bob", bob)
    seed.memory("src-mem-alice", alice)
    for oid, source in (("a", "src-doc"), ("b", "src-doc"), ("c", "src-mem-bob"),
                        ("d", "src-mem-alice")):
        seed.obj(oid, source, f"node {oid}", [_ev(source, f"el-{oid}", f"q {oid}")])
    seed.rel("rD", "src-doc", "a", "b", [_ev("src-doc", "el-a", "paper link")])
    seed.rel("rF", "src-mem-bob", "a", "c", [_ev("src-mem-bob", "el-c", "bob")])
    seed.rel("rO", "src-mem-alice", "a", "d", [_ev("src-mem-alice", "el-d", "al")])
    candidates = repo.retrieval.candidates
    query_vector = np.asarray(candidates._embed_query("orbital link"), dtype=np.float32)
    near = query_vector + 0.05 * np.roll(query_vector, 1)
    with seed._write() as db:
        for relation_id, vector in (("rD", near), ("rF", query_vector),
                                    ("rO", query_vector)):
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
                "created_at) VALUES (%s,%s,%s,%s)",
                (relation_id, seed.nb, encode_vector([float(v) for v in vector]), T0),
            )
    repo.settings.relation_recall = 2

    def relations(scope):
        with source_scope_context(seed.nb, scope):
            hits = candidates._retrieve_relations_scored(seed.nb, "orbital link")
        return sorted(hit.relation_id for hit in hits)

    assert relations(_scope(["src-doc"], ["src-mem-alice"], alice)) == ["rD", "rO"]
    assert relations(None) == ["rF", "rO"]
    with repo._runtime.database.connect() as db:
        ids, _mat = candidates._vector_matrix(
            db, seed.nb, "relation_embeddings", "relation_id")
    assert ids == ["rD"]


def test_pg_unscoped_first_call_does_not_rank_a_later_scoped_one(repo, people):
    bob, alice = people
    seed = _Seed(repo, "tokens")
    seed.doc("src-doc")
    seed.memory("src-mem-bob", bob)
    seed.obj("X", "src-doc", "Alpha widget", [
        _ev("src-doc", "elA", "alpha widget design"),
        _ev("src-mem-bob", "elM", "zebraquartz zebraquartz"),
    ])
    seed.obj("Y", "src-doc", "Zebraquartz lens", [_ev("src-doc", "elB", "a lens")])
    candidates = repo.retrieval.candidates
    alice_scope = _scope(["src-doc"], [], alice)

    def ranking(scope):
        with source_scope_context(seed.nb, scope):
            hits = candidates._retrieve_scored(seed.nb, "zebraquartz")
        return [(hit.object_id, round(hit.relevance, 9)) for hit in hits]

    cold = ranking(alice_scope)
    candidates._vector_cache.invalidate(f"{seed.nb}:kwtok")
    ranking(None)
    warm = ranking(alice_scope)
    assert warm == cold
    # X's only match is Bob's Memory quote: it never ranks for Alice.
    assert [object_id for object_id, _ in warm] == ["Y"]

    # The shared cache itself holds no Memory-evidenced object, whoever built
    # it (Alice's call just did; the unscoped one before it too).
    with repo._runtime.database.connect() as db:
        version_row = candidates.knowledge.object_version_row(db, seed.nb)
    for warm_with in (None, alice_scope):
        candidates._vector_cache.invalidate(f"{seed.nb}:kwtok")
        ranking(warm_with)
        tokens = candidates._vector_cache.get(
            f"{seed.nb}:kwtok", ("kwtok", version_row["c"], version_row["ts"]),
            lambda: pytest.fail("the cache was just warmed"),
        )
        assert "Y" in tokens and "X" not in tokens

    # ...and the other order (spec review P3): a scoped first caller leaves
    # no trimmed token set behind for a later unscoped one.
    candidates._vector_cache.invalidate(f"{seed.nb}:kwtok")
    unscoped_cold = ranking(None)
    candidates._vector_cache.invalidate(f"{seed.nb}:kwtok")
    ranking(alice_scope)
    assert ranking(None) == unscoped_cold


def test_pg_an_edge_outside_the_ceiling_after_the_verdict_is_verified_on_read(
    repo, people,
):
    """Twin of the SQLite case: verdict taken on a Memory-free notebook (does
    not bind), then a relation attributed only to a source outside the freeze
    links two admitted nodes -- the chain step is not rendered and the run
    binds from now on."""
    from app.services.source_scope import ceiling_binds, current_source_scope

    bob, _alice = people
    seed = _Seed(repo, "late-edge")
    seed.doc("src-doc")
    doc = [_ev("src-doc", "elA", "MoE routing")]
    seed.obj("e1", "src-doc", "Mixture-of-Experts (MoE)", doc)
    seed.obj("e8", "src-doc", "Gate network", [_ev("src-doc", "elG", "gating")])
    with source_scope_context(seed.nb, _scope(["src-doc"], [], bob)):
        assert repo.retrieval._ceiling_binds(seed.nb) is False
        seed.rel("rL", None, "e1", "e8", [_ev("src-late", "elL", "late link")])
        block, _id_map, _hits, _supports = (
            repo.retrieval.candidates._chunk_kg_overlay(
                seed.nb, QUERY, QUERY, 1000)
        )
        binds_after = ceiling_binds(
            current_source_scope(), seed.nb,
            drifted=lambda: False, foreign_hidden=lambda: False,
        )

    assert "Gate network" in block
    assert not any(
        "--kind_of-->" in line and "Gate network" in line
        for line in block.splitlines()
    )
    assert binds_after is True


def test_pg_tied_relations_keep_the_whole_matrix_order(repo, people):
    bob, _alice = people
    seed = _Seed(repo, "ties")
    seed.doc("src-doc")
    seed.memory("src-mem", bob)
    for oid, source in (("a", "src-doc"), ("b", "src-mem"), ("c", "src-doc"),
                        ("d", "src-mem")):
        seed.obj(oid, source, f"node {oid}", [_ev(source, f"el{oid}", "zebra")])
    rows = (("r1", "src-doc", "a", "c"), ("r2", "src-mem", "a", "b"),
            ("r3", "src-doc", "c", "d"), ("r4", "src-mem", "b", "d"))
    for rid, source, src, tgt in rows:
        seed.rel(rid, source, src, tgt, [_ev(source, "el" + src, "zebra link")])
    candidates = repo.retrieval.candidates
    vector = [float(v) for v in candidates._embed_query("zebra link")]
    with seed._write() as db:
        for rid, *_ in rows:
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
                "created_at) VALUES (%s,%s,%s,%s)",
                (rid, seed.nb, encode_vector(vector), T0),
            )
    repo.settings.relation_recall = 2

    for scope in (None, _scope(["src-doc"], ["src-mem"], bob)):
        with source_scope_context(seed.nb, scope):
            hits = candidates._retrieve_relations_scored(seed.nb, "zebra link")
        assert sorted(hit.relation_id for hit in hits) == ["r1", "r2"]
