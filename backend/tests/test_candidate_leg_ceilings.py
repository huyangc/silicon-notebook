"""E2-2: candidate-layer legs under the run's source ceiling (real SQLite store).

Audit rows pinned here, each by what an asker can observe:

* B-5 (candidate side) -- the 1-hop graph walk of the chunk KG overlay
  (``_chunk_kg_overlay`` → ``_ceiling_scoped_subgraph``) judges the CURRENT
  notebook's nodes by the local ceiling (``allows``), not only mounted
  libraries' per-notebook ones.  Two leaks close with the one check: another
  member's Memory-derived node, and -- with the Memory channel closed -- the
  asker's OWN Memory-derived node.  Before, both reached the answer prompt
  (``kg_block``) behind a live anchor.
* C-5 -- the per-notebook keyword-token cache and the relation vector matrix
  are shared by every asker, so they hold only content no Memory source
  derives; Memory content is scored live, per caller.
* B-10 -- the ANN leg's returned matrix holds only the candidates it kept.
* D-5 (plugin half) -- a plugin element hit whose source is outside the frozen
  universe is dropped, not attributed to the active notebook.

The closed-channel shape.  Master has no ``withheld_hidden_source_ids`` yet
(E1-1 adds it and, until this lands, switches four channels off whenever it is
non-empty).  The state that fail-closed line guards is "the freeze's hidden
half lacks the asker's own Memory AND the drift probe agrees with the freeze".
It is built here as a frozen include whose hidden half is empty and whose
``owner_id`` is blank, so the probe re-reads the same (empty) hidden half and
answers "no drift" -- exactly what E1-1's probe answers after subtracting the
withheld sources.  So these tests prove the leak is closed by the per-node
check alone, with no channel switched off.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedElement
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import source_scope_context
from tests.model_testkit import bind_all_embedding_clients

NOW = "2026-09-30T00:00:00"
QUERY = "Mixture-of-Experts MoE"


def _evidence(source_id: str, element_id: str, quote: str) -> str:
    return json.dumps([{
        "source_id": source_id, "source_title": "", "element_id": element_id,
        "element_type": "paragraph", "location_label": "p1",
        "quoted_span": quote, "quote": quote, "confidence": 1.0,
    }])


@pytest.fixture
def store(tmp_path, monkeypatch):
    from app.services.sqlite_repository import (
        SQLiteRepository, reset_request_user, set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'legs.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    repo.settings.graph_ppr_enabled = False
    bob = repo.create_user("b00654321", "password-12")
    alice = repo.create_user("a00123456", "password-12")
    token = set_request_user(bob)
    try:
        yield repo, bob.id, alice.id
    finally:
        reset_request_user(token)


def _notebook(repo, name: str) -> str:
    return repo.create_notebook(NotebookCreate(name=name)).id


def _memory_source(repo, nb: str, source_id: str, owner: str) -> None:
    memory_id = f"mem-{source_id}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed',?,?,?,?)",
            (memory_id, nb, owner, "m", "private", NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, nb, "memory", "memory", "ready", memory_id, NOW, NOW),
        )


def _doc_source(repo, nb: str, source_id: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (source_id, nb, "paper", "md", "ready", NOW, NOW),
        )


def _object(repo, nb, object_id, source_id, name, evidence_json) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (object_id, nb, "concept", "approved", "", json.dumps({"name": name}),
             evidence_json, source_id, NOW, NOW),
        )


def _relation(repo, nb, relation_id, source_id, src, tgt, evidence_json) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (relation_id, nb, source_id, src, tgt, "kind_of", evidence_json, NOW),
        )


def _scope(visible, hidden, owner):
    """A frozen all-selected local ceiling, as the entry points freeze it."""
    return {
        "mode": "include", "source_ids": list(visible),
        "hidden_source_ids": list(hidden), "narrowed": False, "owner_id": owner,
    }


# ---------------------------------------------------------------------------
# B-5: the overlay's 1-hop walk under the local ceiling
# ---------------------------------------------------------------------------


@pytest.fixture
def walk(store):
    """Bob's shared notebook: a paper (``src-doc``) and Bob's confirmed Memory
    (``src-mem-bob``).  Concept e1 (paper) is the walk's seed; e3
    "ZEBRAQUARTZ plan" is Memory-derived, reached through the Memory-derived
    relation rM; e4 (paper) is reached through rX, a relation whose ONLY
    evidence is Bob's Memory."""
    repo, bob, alice = store
    nb = _notebook(repo, "kb")
    _doc_source(repo, nb, "src-doc")
    _memory_source(repo, nb, "src-mem-bob", bob)
    doc = _evidence("src-doc", "elA", "MoE routing")
    memory = _evidence("src-mem-bob", "elM", "SECRETMEMO ZEBRAQUARTZ")
    _object(repo, nb, "e1", "src-doc", "Mixture-of-Experts (MoE)", doc)
    _object(repo, nb, "e3", "src-mem-bob", "ZEBRAQUARTZ plan", memory)
    # Nothing in e4 matches the query, so the KG search never seeds it: under
    # a ceiling it is reached only by walking rX.
    _object(repo, nb, "e4", "src-doc", "Router balance",
            _evidence("src-doc", "elC", "load balance loss"))
    _relation(repo, nb, "rM", "src-mem-bob", "e1", "e3", memory)
    _relation(repo, nb, "rX", "src-mem-bob", "e1", "e4", memory)
    return repo, nb, bob, alice


def _overlay(repo, nb, scope=None):
    with source_scope_context(nb, scope):
        block, id_map, _hits, supports = (
            repo.retrieval.candidates._chunk_kg_overlay(nb, QUERY, QUERY, 1000)
        )
    return block, id_map, supports


def _objects(id_map) -> set:
    return {str(entry.get("object_id") or "") for entry in id_map.values()}


def test_open_channel_control_renders_the_askers_own_memory_node(walk):
    """Control: Bob with his own Memory in his ceiling sees e3 and both chains,
    so the pruned cases below are not vacuous -- the fixture's walk does reach
    the Memory-derived rows, and the check does not over-prune them."""
    repo, nb, bob, _alice = walk

    block, id_map, _supports = _overlay(
        repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob),
    )

    assert "ZEBRAQUARTZ" in block and "--kind_of-->" in block
    assert {"e1", "e3", "e4"} <= _objects(id_map)


def test_closed_channel_never_renders_the_askers_own_memory(walk):
    """The asker's own Memory withheld from the freeze (channel closed) and the
    drift probe agreeing: the walk still runs (no channel off), and neither the
    Memory-derived node, nor its name, nor a chain through a Memory-derived
    relation reaches the prompt or gets an anchor."""
    repo, nb, _bob, _alice = walk
    candidates = repo.retrieval.candidates

    with source_scope_context(nb, _scope(["src-doc"], [], "")):
        assert candidates._unsafe_source_scope_restricted(nb) is False
    block, id_map, supports = _overlay(repo, nb, _scope(["src-doc"], [], ""))

    assert "Mixture-of-Experts" in block, "the walk ran: the seed is rendered"
    assert "ZEBRAQUARTZ" not in block and "SECRETMEMO" not in block
    assert "--kind_of-->" not in block
    assert "e3" not in _objects(id_map)
    assert not {"rM", "rX"} & {
        support.support_id for values in supports.values() for support in values
    }


def test_another_members_memory_never_reaches_the_prompt(walk):
    """Alice asks in Bob's notebook under the ordinary all-selected freeze.
    Bob's Memory-derived node e3 is dropped, and the paper node e4 she may
    read stays -- but as a plain arrival: the only thing linking it to e1 is
    a relation Bob's Memory derived, so that chain line is not rendered."""
    repo, nb, _bob, alice = walk

    block, id_map, supports = _overlay(repo, nb, _scope(["src-doc"], [], alice))

    assert "ZEBRAQUARTZ" not in block and "SECRETMEMO" not in block
    assert "Router balance" in block and "e4" in _objects(id_map)
    assert "--kind_of-->" not in block
    assert "e3" not in _objects(id_map)
    assert not {"rM", "rX"} & {
        support.support_id for values in supports.values() for support in values
    }


def test_the_walk_reaches_router_balance_only_through_rx(walk):
    """Fixture check for the case above: under Bob's own open ceiling e4 is a
    walk arrival with rX's chain line, so dropping that line for Alice is the
    edge rule at work, not a seed that never had an edge."""
    repo, nb, bob, _alice = walk

    block, _id_map, _supports = _overlay(
        repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob),
    )

    assert any(
        "--kind_of-->" in line and "Router balance" in line
        for line in block.splitlines()
    )


def test_a_node_outside_the_ceiling_after_the_verdict_is_verified_on_read(walk):
    """The run's verdict said the ceiling does not bind (all selected, nothing
    drifted when it was taken), and then the library changed: the walk now
    holds a node whose only source the freeze never admitted.  Verify-on-read
    catches it, records the drift for the rest of the run, and prunes it."""
    from app.services.source_scope import current_source_scope

    repo, nb, _bob, alice = walk
    with source_scope_context(nb, _scope(["src-doc"], [], alice)):
        scope = current_source_scope()
        scope._ceiling_binds_memo[nb] = False    # the verdict, taken earlier
        block, id_map, _hits, _supports = (
            repo.retrieval.candidates._chunk_kg_overlay(nb, QUERY, QUERY, 1000)
        )
        drift_recorded = scope._ceiling_binds_memo[nb]

    assert "ZEBRAQUARTZ" not in block and "e3" not in _objects(id_map)
    assert drift_recorded is True


def test_closed_channel_through_the_mixed_candidate_seam(walk):
    """Same closed-channel shape through ``mixed_chunk_candidates`` (the seam
    chunk mode reads): no Memory-derived node name or anchor."""
    repo, nb, _bob, _alice = walk

    with source_scope_context(nb, _scope(["src-doc"], [], "")):
        _chunks, block, id_map, hits, _ppr = repo.retrieval.mixed_chunk_candidates(
            nb, QUERY, QUERY, [QUERY],
        )

    assert "ZEBRAQUARTZ" not in block
    assert "e3" not in _objects(id_map)
    assert "e3" not in {hit.object_id for hit in hits}


@pytest.fixture
def plain_walk(store):
    """A notebook with no Memory at all: e1 → e4 (paper evidence) and e1 → e6,
    where e6 carries NO evidence (a legitimate evidence-less object)."""
    repo, bob, _alice = store
    nb = _notebook(repo, "plain")
    _doc_source(repo, nb, "src-doc")
    doc = _evidence("src-doc", "elA", "MoE routing")
    _object(repo, nb, "e1", "src-doc", "Mixture-of-Experts (MoE)", doc)
    _object(repo, nb, "e4", "src-doc", "Router balance", doc)
    # Named so the KG search never returns it: a KG hit passes the separate
    # result-boundary filter, which drops an evidence-less hit under any
    # ceiling (unchanged here).  It enters the walk as a relation endpoint.
    _object(repo, nb, "e6", "src-doc", "Capacity factor", "[]")
    _relation(repo, nb, "rD", "src-doc", "e1", "e4", doc)
    _relation(repo, nb, "rE", "src-doc", "e1", "e6", doc)
    return repo, nb, bob


def test_a_notebook_without_memory_renders_byte_identically(plain_walk, monkeypatch):
    """All selected, nothing drifted, nobody's Memory: the ceiling cannot
    exclude anything, so the walk is used as read -- the evidence-less node
    included -- and the block, anchors and supports equal what the same run
    rendered before the local ceiling was checked at all (the check replaced
    by the identity, which is the pre-E2-2 path for a single-notebook scope)."""
    repo, nb, bob = plain_walk
    candidates = repo.retrieval.candidates
    scope = _scope(["src-doc"], [], bob)

    checked = _overlay(repo, nb, scope)
    monkeypatch.setattr(
        candidates, "_ceiling_scoped_subgraph", lambda subgraph, _scope: subgraph,
    )
    unchecked = _overlay(repo, nb, scope)

    assert "Capacity factor" in checked[0]
    assert checked == unchecked


# ---------------------------------------------------------------------------
# C-5: the shared keyword-token cache
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_object(store):
    """X is a paper object that ALSO carries a quote from Bob's Memory (the
    mixed-evidence shape pre-isolation data has); the query term appears ONLY
    in that Memory quote.  Y is an ordinary paper object."""
    repo, bob, alice = store
    nb = _notebook(repo, "tokens")
    _doc_source(repo, nb, "src-doc")
    _memory_source(repo, nb, "src-mem-bob", bob)
    mixed = json.dumps([
        json.loads(_evidence("src-doc", "elA", "alpha widget design"))[0],
        json.loads(_evidence("src-mem-bob", "elM", "zebraquartz zebraquartz"))[0],
    ])
    _object(repo, nb, "X", "src-doc", "Alpha widget", mixed)
    _object(repo, nb, "Y", "src-doc", "Zebraquartz lens",
            _evidence("src-doc", "elB", "a lens"))
    return repo, nb, bob, alice


def _ranking(repo, nb, scope):
    with source_scope_context(nb, scope):
        hits = repo.retrieval.candidates._retrieve_scored(nb, "zebraquartz")
    return [(hit.object_id, round(hit.relevance, 9)) for hit in hits]


def _cold(repo, nb) -> None:
    repo.retrieval.candidates._vector_cache.invalidate(f"{nb}:kwtok")


def test_an_unscoped_first_call_does_not_rank_a_later_scoped_one(mixed_object):
    """Alice's ranking must not depend on whether an unscoped call warmed the
    cache first: Bob's Memory quote may not lift X for her."""
    repo, nb, _bob, alice = mixed_object
    alice_scope = _scope(["src-doc"], [], alice)

    cold = _ranking(repo, nb, alice_scope)
    _cold(repo, nb)
    _ranking(repo, nb, None)          # warms the per-notebook cache
    warm = _ranking(repo, nb, alice_scope)

    assert warm == cold
    assert [object_id for object_id, _ in warm][:1] == ["Y"]


def test_a_scoped_first_call_does_not_rank_a_later_unscoped_one(mixed_object):
    """…and the other way round: a scoped first caller no longer leaves its
    filtered view behind for everyone else."""
    repo, nb, _bob, alice = mixed_object

    cold = _ranking(repo, nb, None)
    _cold(repo, nb)
    _ranking(repo, nb, _scope(["src-doc"], [], alice))
    warm = _ranking(repo, nb, None)

    assert warm == cold


def test_the_shared_token_cache_holds_no_memory_evidenced_object(mixed_object):
    repo, nb, _bob, _alice = mixed_object

    _ranking(repo, nb, None)
    with repo._connect() as db:
        version_row = repo.retrieval.candidates.knowledge.object_version_row(db, nb)
    cached = repo.retrieval.candidates._vector_cache.peek(
        f"{nb}:kwtok", ("kwtok", version_row["c"], version_row["ts"]),
    )

    assert cached is not None
    tokens = repo.retrieval.candidates._vector_cache.get(
        f"{nb}:kwtok", ("kwtok", version_row["c"], version_row["ts"]),
        lambda: pytest.fail("the cache was just warmed"),
    )
    assert "Y" in tokens and "X" not in tokens


# ---------------------------------------------------------------------------
# C-5: the shared relation vector matrix
# ---------------------------------------------------------------------------


@pytest.fixture
def relation_matrix(store):
    """rD (paper), rF (Bob's Memory), rO (Alice's own Memory).  The query's
    own vector is given to rF and rO, a nearby one to rD, so with a recall of
    two the whole-notebook matrix would pick rF and rO."""
    repo, bob, alice = store
    nb = _notebook(repo, "relations")
    _doc_source(repo, nb, "src-doc")
    _memory_source(repo, nb, "src-mem-bob", bob)
    _memory_source(repo, nb, "src-mem-alice", alice)
    for oid, source in (("a", "src-doc"), ("b", "src-doc"), ("c", "src-mem-bob"),
                        ("d", "src-mem-alice")):
        _object(repo, nb, oid, source, f"node {oid}",
                _evidence(source, f"el-{oid}", f"quote {oid}"))
    _relation(repo, nb, "rD", "src-doc", "a", "b",
              _evidence("src-doc", "el-a", "paper link"))
    _relation(repo, nb, "rF", "src-mem-bob", "a", "c",
              _evidence("src-mem-bob", "el-c", "bob link"))
    _relation(repo, nb, "rO", "src-mem-alice", "a", "d",
              _evidence("src-mem-alice", "el-d", "alice link"))
    query_vector = np.asarray(
        repo.retrieval.candidates._embed_query("orbital link"), dtype=np.float32,
    )
    near = query_vector + 0.05 * np.roll(query_vector, 1)
    with repo._write() as db:
        for relation_id, vector in (("rD", near), ("rF", query_vector),
                                    ("rO", query_vector)):
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
                "created_at) VALUES (?,?,?,?)",
                (relation_id, nb, json.dumps([float(v) for v in vector]), NOW),
            )
    repo.settings.relation_recall = 2
    return repo, nb, bob, alice


def _relations(repo, nb, scope):
    with source_scope_context(nb, scope):
        hits = repo.retrieval.candidates._retrieve_relations_scored(nb, "orbital link")
    return [hit.relation_id for hit in hits]


def test_another_members_memory_relation_never_takes_a_matrix_seat(relation_matrix):
    """Alice (her own Memory in her ceiling): Bob's rF is not in the shared
    matrix, so her two seats go to rD and her own rO, scored live -- before,
    rF took a seat and was dropped at the boundary, costing her rD."""
    repo, nb, _bob, alice = relation_matrix

    got = _relations(repo, nb, _scope(["src-doc"], ["src-mem-alice"], alice))

    assert "rF" not in got
    assert sorted(got) == ["rD", "rO"]


def test_an_unscoped_run_still_scores_every_relation(relation_matrix):
    """No scope: shared matrix ∪ every Memory relation is the set it always
    scored, so the top two are still rF and rO."""
    repo, nb, _bob, _alice = relation_matrix

    assert sorted(_relations(repo, nb, None)) == ["rF", "rO"]


def test_the_shared_relation_matrix_holds_no_memory_relation(relation_matrix):
    repo, nb, _bob, _alice = relation_matrix

    with repo._connect() as db:
        ids, _mat = repo.retrieval.candidates._vector_matrix(
            db, nb, "relation_embeddings", "relation_id")

    assert ids == ["rD"]


# ---------------------------------------------------------------------------
# B-10: the ANN leg's matrix
# ---------------------------------------------------------------------------


def test_ann_candidate_matrix_holds_only_the_kept_rows(store, monkeypatch):
    repo, _bob, _alice = store
    candidates = repo.retrieval.candidates

    class _Ann:
        @staticmethod
        def set_ef(_value):
            return None

        @staticmethod
        def knn_query(_query, *, k, **_kwargs):
            # A stale sidecar / vacuous filter: both rows come back.
            return (np.asarray([[0, 1]], dtype=np.int64),
                    np.asarray([[0.0, 0.1]], dtype=np.float32))

    index = type("Index", (), dict(
        chunk_ann_labels=["kept", "dropped"],
        chunk_ann_source_names=["allowed", "denied"],
        chunk_ann_source_codes=np.asarray([0, 0], dtype=np.int32),
        chunk_ann_source_counts=np.asarray([2, 0], dtype=np.int64),
        manifest={"dim": 16},
    ))()
    rows = [
        {"chunk_id": cid, "source_id": sid, "source_title": "t",
         "section_path": "", "text": f"text {cid}", "element_ids": []}
        for cid, sid in (("kept", "allowed"), ("dropped", "denied"))
    ]
    matrix = np.eye(2, 16, dtype=np.float32)
    monkeypatch.setattr(candidates, "_open_scale_ann", lambda *_args: _Ann())
    monkeypatch.setattr(
        candidates, "_hydrate_chunk_candidates",
        lambda _ids: (rows, ["kept", "dropped"], matrix),
    )
    monkeypatch.setattr(candidates, "_chunk_fts_hits", lambda *_a, **_k: [])

    scored, ids, mat = candidates._retrieve_chunks_ann(
        "nb", "text", [1.0] + [0.0] * 15, index, recall=4,
        allowed_source_ids=("allowed",),
    )

    assert [chunk.chunk_id for chunk in scored] == ["kept"]
    assert ids == ["kept"]
    assert mat.shape == (1, 16)
    # The shared hydration matrix was copied, never written.
    assert matrix.shape == (2, 16)


# ---------------------------------------------------------------------------
# D-5 (plugin half)
# ---------------------------------------------------------------------------


def test_a_plugin_hit_outside_the_frozen_universe_is_dropped():
    from app.services.plugin_ask_engine import PluginRetrievalAccess

    def hit(element_id, source_id):
        return RetrievedElement(
            element_id=element_id, source_id=source_id, source_title="t",
            location_label="p1", element_type="paragraph", text="text",
            score=0.5,
        )

    access = PluginRetrievalAccess(
        active_notebook_id="notebook-1",
        actor_id="user-1",
        cancellation=None,
        participant_notebook_ids=lambda _notebook_id: ("notebook-1",),
        all_visible_source_ids=lambda _notebook_id: ("source-1",),
        hidden_source_ids=lambda _notebook_id, _actor_id: (),
        search_elements=lambda *_args, **_kwargs: [
            hit("element-1", "source-1"), hit("element-2", "source-foreign"),
        ],
        source_info=lambda source_ids: {
            source_id: {"title": source_id, "file_name": ""}
            for source_id in source_ids
        },
        max_k=4,
        max_calls=2,
        evidence_chars=100,
        query_chars=100,
    )

    issued = access.search("query", 4)

    assert [evidence.source_title for evidence in issued] == ["source-1"]
