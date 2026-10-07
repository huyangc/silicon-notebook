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

The closed-channel shape is PR-E1's: the default ceiling withholds the asker's
own Memory (``withheld_hidden_source_ids``) and the drift probe counts it back,
so no channel is switched off; the run's verdict binds (an unbounded read
would include the withheld sources).  These cases therefore exercise the
per-node check's BOUND path; its unbound path with verify-on-read is pinned by
the cases that take the verdict -- through the production verdict probes
(``_probes``), as every default ceiling does -- on a Memory-free notebook and
only then change it.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedElement
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.source_scope import (
    CeilingVerdictProbes, current_source_scope, run_ceiling_binds,
    source_scope_context,
)
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
    """One KG object, written as the store writes it: the row plus its
    reverse-index rows (``knowledge_object_sources``, one per evidence
    source).  A new notebook's reverse index is marked complete, so a reader
    that trusts it would see an object without those rows as sourceless."""
    sources = {item["source_id"] for item in json.loads(evidence_json)}
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,"
            "owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (object_id, nb, "concept", "approved", "", json.dumps({"name": name}),
             evidence_json, source_id, NOW, NOW),
        )
        db.executemany(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "VALUES (?,?,?)",
            [(object_id, source, nb) for source in sorted(sources)],
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


def _probes(repo):
    """The production verdict probes (``RepositoryRuntime.ceiling_readers``):
    with them the run's verdict (``run_ceiling_binds``) is computed from the
    store, as under every default ceiling; without them it always binds."""
    return repo._runtime.ceiling_readers().verdict_probes


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


def _overlay(repo, nb, scope=None, **context):
    with source_scope_context(nb, scope, **context):
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
    """Bob's own Memory withheld from the freeze (channel closed) and the
    drift probe agreeing: the walk still runs (no channel off), and neither the
    Memory-derived node, nor its name, nor a chain through a Memory-derived
    relation reaches the prompt or gets an anchor.  (The verdict binds here --
    the withheld sources are outside the ceiling -- so this is the bound path;
    see the module docstring.)"""
    repo, nb, bob, _alice = walk
    candidates = repo.retrieval.candidates
    closed = {"_withheld_hidden_source_ids": ["src-mem-bob"],
              "_verdict_probes": _probes(repo)}

    with source_scope_context(nb, _scope(["src-doc"], [], bob), **closed):
        assert candidates._unsafe_source_scope_restricted(nb) is False
        assert run_ceiling_binds(current_source_scope(), nb) is True
    block, id_map, supports = _overlay(repo, nb, _scope(["src-doc"], [], bob), **closed)

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


def _verdict_binds(nb) -> bool:
    """The run's verdict (``run_ceiling_binds``) as the public function
    reports it.  Monotone: once taken "does not bind" it turns True only on a
    drift the run recorded (verify-on-read) -- what the cases below observe,
    without reading the scope's private memo."""
    return run_ceiling_binds(current_source_scope(), nb)


# The cases below write KG rows attributed to ``src-late``, a source with no
# ``sources`` row the drift probe could see (one deleted while its graph rows
# linger, or one whose row the probe read missed): the walk still runs, so
# only verify-on-read stands between those rows and the prompt.


def test_a_node_outside_the_ceiling_after_the_verdict_is_verified_on_read(
    plain_walk,
):
    """The run's verdict was taken on a Memory-free, unchanged notebook -- it
    does not bind -- and then a node from outside the freeze joined the walk.
    The walk is otherwise used as read, but verify-on-read catches the node
    whose only source the freeze never admitted: it is pruned and the run
    binds from now on."""
    repo, nb, bob = plain_walk
    with source_scope_context(nb, _scope(["src-doc"], [], bob),
                              _verdict_probes=_probes(repo)):
        assert _verdict_binds(nb) is False     # the verdict, taken first
        _object(repo, nb, "e7", "src-late", "Late gate",
                _evidence("src-late", "elL", "late quote"))
        _relation(repo, nb, "rN", "src-doc", "e1", "e7",
                  _evidence("src-doc", "elA", "paper link"))
        block, id_map, _hits, _supports = (
            repo.retrieval.candidates._chunk_kg_overlay(nb, QUERY, QUERY, 1000)
        )
        binds_after = _verdict_binds(nb)

    assert "Mixture-of-Experts" in block
    assert "Late gate" not in block and "e7" not in _objects(id_map)
    assert binds_after is True


def test_an_edge_outside_the_ceiling_after_the_verdict_is_verified_on_read(
    plain_walk,
):
    """Same, for the incoming EDGE: after the verdict, a source outside the
    freeze yields a relation between two nodes the freeze admits.  Both nodes pass on their
    own evidence; the edge's evidence does not.  The chain step is not
    rendered (the node stays, as a plain arrival) and the run binds from now
    on (quality review P2-3)."""
    repo, nb, bob = plain_walk
    _object(repo, nb, "e8", "src-doc", "Gate network",
            _evidence("src-doc", "elG", "gating"))
    # No relation seeds: under a pushed-down verdict the relation channel
    # reads without the list, and rL as a SEED would hand e8 to the walk as a
    # seed of its own -- this case is about rL as a walked EDGE.
    repo.settings.chunk_kg_relation_seed_top_n = 0
    with source_scope_context(nb, _scope(["src-doc"], [], bob),
                              _verdict_probes=_probes(repo)):
        assert _verdict_binds(nb) is False
        # (``knowledge_relations.source_id`` references ``sources``; the
        # row is attributed by its evidence alone, the way a relation whose
        # source row is gone reads.)
        _relation(repo, nb, "rL", None, "e1", "e8",
                  _evidence("src-late", "elL", "late link"))
        block, _id_map, _hits, _supports = (
            repo.retrieval.candidates._chunk_kg_overlay(nb, QUERY, QUERY, 1000)
        )
        binds_after = _verdict_binds(nb)

    assert "Gate network" in block, "the walk reached e8 (only via rL)"
    assert not any(
        "--kind_of-->" in line and "Gate network" in line
        for line in block.splitlines()
    )
    assert "late link" not in block
    assert binds_after is True


def test_the_bound_path_drops_a_node_without_evidence(walk):
    """When the verdict binds (Alice: Bob's Memory is in the notebook), a walk
    node needs at least one in-ceiling evidence item, so an evidence-less node
    is dropped; when it does not bind (Bob, his own Memory in his ceiling) the
    same node is used as read (quality review P3-5)."""
    repo, nb, bob, alice = walk
    _object(repo, nb, "e6", "src-doc", "Capacity factor", "[]")
    _relation(repo, nb, "rE", "src-doc", "e1", "e6",
              _evidence("src-doc", "elA", "paper link"))

    probes = _probes(repo)
    bound, _id_map, _supports = _overlay(
        repo, nb, _scope(["src-doc"], [], alice), _verdict_probes=probes)
    unbound, _id_map, _supports = _overlay(
        repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob), _verdict_probes=probes)

    assert "Capacity factor" not in bound
    assert "Capacity factor" in unbound


def test_a_failing_verdict_probe_binds_instead_of_failing_the_ask(walk):
    """A verdict probe that cannot answer (a pool timeout, say) makes the
    overlay take its bound path (``source_scope._probe_or_bind``); a Stop
    still propagates (quality review P3-7).

    The case that tells "binds" from "does not bind" (fix review P3-a): Bob,
    his own Memory in his ceiling, nothing outside it -- the ordinary verdict
    does not bind, so the evidence-less node e6 is used as read; with the probe
    failing the run binds and e6 is dropped.  The failing probe is installed
    on the scope (``verdict_probes``): a probe-less scope binds regardless and
    would prove nothing."""
    from app.domain.cancellation import AskCancelled

    repo, nb, bob, alice = walk
    _object(repo, nb, "e6", "src-doc", "Capacity factor", "[]")
    _relation(repo, nb, "rE", "src-doc", "e1", "e6",
              _evidence("src-doc", "elA", "paper link"))
    bob_scope = _scope(["src-doc"], ["src-mem-bob"], bob)
    healthy, _id_map, _supports = _overlay(
        repo, nb, bob_scope, _verdict_probes=_probes(repo))
    assert "Capacity factor" in healthy, "control: unbound keeps e6"

    def _raising(exc):
        def probe(*_args):
            raise exc
        return CeilingVerdictProbes(universe_digests=probe, foreign_hidden=probe)

    failing, _id_map, _supports = _overlay(
        repo, nb, bob_scope, _verdict_probes=_raising(RuntimeError("pool timeout")))
    assert "Mixture-of-Experts" in failing
    assert "Capacity factor" not in failing, "a failed probe binds"
    block, id_map, _supports = _overlay(
        repo, nb, _scope(["src-doc"], [], alice),
        _verdict_probes=_raising(RuntimeError("pool timeout")))
    assert "Mixture-of-Experts" in block
    assert "ZEBRAQUARTZ" not in block and "e3" not in _objects(id_map)

    with pytest.raises(AskCancelled):
        _overlay(repo, nb, bob_scope, _verdict_probes=_raising(AskCancelled()))


def _seam(repo, nb, scope, **context):
    with source_scope_context(nb, scope, _verdict_probes=_probes(repo), **context):
        _chunks, block, id_map, hits, _ppr = repo.retrieval.mixed_chunk_candidates(
            nb, QUERY, QUERY, [QUERY],
        )
    return block, id_map, hits


def test_open_channel_through_the_mixed_candidate_seam(walk):
    """Positive control for the seam case below (spec review): with the same
    fixture -- reverse index written as the store writes it -- and the
    channel open, Bob's own Memory node does reach the block, so the closed
    case is not passing on an empty block."""
    repo, nb, bob, _alice = walk

    block, id_map, _hits = _seam(repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob))

    assert "ZEBRAQUARTZ" in block and "e3" in _objects(id_map)


def test_closed_channel_through_the_mixed_candidate_seam(walk):
    """Same closed-channel shape through ``mixed_chunk_candidates`` (the seam
    chunk mode reads): no Memory-derived node name or anchor, while the walk
    itself still renders the paper's nodes."""
    repo, nb, bob, _alice = walk

    block, id_map, hits = _seam(repo, nb, _scope(["src-doc"], [], bob),
                                _withheld_hidden_source_ids=["src-mem-bob"])

    assert "Mixture-of-Experts" in block
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

    checked = _overlay(repo, nb, scope, _verdict_probes=_probes(repo))
    monkeypatch.setattr(
        candidates, "_ceiling_scoped_subgraph", lambda subgraph, _scope: subgraph,
    )
    unchecked = _overlay(repo, nb, scope, _verdict_probes=_probes(repo))

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
    # X's only match is Bob's Memory quote, which Alice may not read: it does
    # not rank for her at all, whichever call built the cache.
    assert [object_id for object_id, _ in warm] == ["Y"]


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
    repo, nb, _bob, alice = mixed_object

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

    # Warmed by a scoped caller whose view of X no longer shows the Memory
    # quote: the decision is still taken on the evidence as READ.
    _cold(repo, nb)
    _ranking(repo, nb, _scope(["src-doc"], [], alice))
    tokens = repo.retrieval.candidates._vector_cache.get(
        f"{nb}:kwtok", ("kwtok", version_row["c"], version_row["ts"]),
        lambda: pytest.fail("the cache was just warmed"),
    )
    assert "Y" in tokens and "X" not in tokens


def test_an_object_whose_evidence_this_call_trimmed_is_tokenised_live(mixed_object):
    """Z carries a quote attributed to a source outside the freeze that is not
    Memory (``src-gone``: its row is gone, the evidence lingers), so the shared
    cache holds Z's tokens with that quote.  Bob's all-selected run trims the
    quote; his ranking must not be lifted by it, whichever call built the cache
    (quality review P3-11)."""
    repo, nb, bob, _alice = mixed_object
    _object(repo, nb, "Z", "src-doc", "Zeta widget", json.dumps([
        json.loads(_evidence("src-doc", "elZ", "zeta design"))[0],
        json.loads(_evidence("src-gone", "elG", "zebraquartz notes"))[0],
    ]))
    bob_scope = _scope(["src-doc"], ["src-mem-bob"], bob)

    cold = _ranking(repo, nb, bob_scope)
    _cold(repo, nb)
    _ranking(repo, nb, None)          # caches Z's tokens, quote included
    warm = _ranking(repo, nb, bob_scope)

    assert warm == cold
    assert "Z" not in [object_id for object_id, _ in warm]


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


def test_memory_relations_are_cached_not_read_per_request(
    relation_matrix, monkeypatch,
):
    """The Memory half is cached with the shared one (quality review P2-2):
    after the first call, no request reads Memory relations again, whoever
    asks; the per-request mask still keeps Bob's rF out of Alice's seats.
    The cold build maps both Memory sources in ONE batched read that returns
    each relation's source (``with_source_id=True``), not one read per
    source."""
    repo, nb, bob, alice = relation_matrix
    reads = _record_relation_delta_reads(repo, monkeypatch)
    alice_scope = _scope(["src-doc"], ["src-mem-alice"], alice)

    first = _relations(repo, nb, alice_scope)
    built = list(reads)
    later = [
        _relations(repo, nb, alice_scope),
        _relations(repo, nb, None),
        _relations(repo, nb, _scope(["src-doc"], ["src-mem-bob"], bob)),
    ]

    assert built == [(("src-mem-alice", "src-mem-bob"), {"with_source_id": True})]
    assert reads == built
    assert sorted(first) == sorted(later[0]) == ["rD", "rO"]
    assert sorted(later[1]) == ["rF", "rO"]
    assert sorted(later[2]) == ["rD", "rF"]


def test_the_memory_relation_map_reads_in_batches_of_the_in_chunk(
    relation_matrix, monkeypatch,
):
    """The cold build batches Memory sources at ``_IN_CHUNK`` (900 in
    production; 1 here, so two Memory sources take two reads), and every
    batch still maps its relations to their own source: Bob's rF stays out of
    Alice's seats and her own rO stays in."""
    repo, nb, _bob, alice = relation_matrix
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "_IN_CHUNK", 1)
    reads = _record_relation_delta_reads(repo, monkeypatch)

    got = _relations(repo, nb, _scope(["src-doc"], ["src-mem-alice"], alice))

    assert sorted(len(source_ids) for source_ids, _kwargs in reads) == [1, 1]
    assert all(kwargs == {"with_source_id": True} for _ids, kwargs in reads)
    assert sorted(got) == ["rD", "rO"]


def _record_relation_delta_reads(repo, monkeypatch) -> list:
    embeddings = repo.retrieval.candidates.embeddings
    real = embeddings.relation_delta_rows
    reads: list = []

    def recording(db, notebook_id, source_ids, **kwargs):
        reads.append((tuple(sorted(source_ids)), kwargs))
        return real(db, notebook_id, source_ids, **kwargs)

    monkeypatch.setattr(embeddings, "relation_delta_rows", recording)
    return reads


def test_tied_relations_keep_the_whole_matrix_order(store):
    """Four relations with the very same vector, two of them Memory-derived and
    interleaved with the paper's (r1 paper, r2 Memory, r3 paper, r4 Memory).
    With a recall of two, a run that reads every row must pick r1 and r2 --
    what one whole matrix ordered first -- not the two shared rows first
    (spec review P2)."""
    repo, bob, _alice = store
    nb = _notebook(repo, "ties")
    _doc_source(repo, nb, "src-doc")
    _memory_source(repo, nb, "src-mem", bob)
    for oid, source in (("a", "src-doc"), ("b", "src-mem"), ("c", "src-doc"),
                        ("d", "src-mem")):
        _object(repo, nb, oid, source, f"node {oid}",
                _evidence(source, f"el{oid}", "zebra"))
    rows = (("r1", "src-doc", "a", "c"), ("r2", "src-mem", "a", "b"),
            ("r3", "src-doc", "c", "d"), ("r4", "src-mem", "b", "d"))
    for rid, source, src, tgt in rows:
        _relation(repo, nb, rid, source, src, tgt,
                  _evidence(source, "el" + src, "zebra link"))
    vector = [float(v) for v in repo.retrieval.candidates._embed_query("zebra link")]
    with repo._write() as db:
        for rid, *_ in rows:
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
                "created_at) VALUES (?,?,?,?)",
                (rid, nb, json.dumps(vector), NOW),
            )
    repo.settings.relation_recall = 2
    own = _scope(["src-doc"], ["src-mem"], bob)

    for scope in (None, own, None, own):
        with source_scope_context(nb, scope):
            hits = repo.retrieval.candidates._retrieve_relations_scored(
                nb, "zebra link")
        assert sorted(hit.relation_id for hit in hits) == ["r1", "r2"]


def _vectors_notebook(repo, bob, alice, rows):
    """A notebook whose relations carry the given vectors, inserted in order.
    ``rows`` = ``[(relation_id, source_id, vector)]``; sources ``src-doc``,
    ``src-mem-bob`` and ``src-mem-alice`` exist."""
    nb = _notebook(repo, f"vectors-{len(rows)}-{rows[0][0]}")
    _doc_source(repo, nb, "src-doc")
    _memory_source(repo, nb, "src-mem-bob", bob)
    _memory_source(repo, nb, "src-mem-alice", alice)
    for oid, source in (("a", "src-doc"), ("b", "src-doc")):
        _object(repo, nb, oid, source, f"node {oid}",
                _evidence(source, f"el-{oid}", f"q {oid}"))
    for rid, source, _vector in rows:
        _relation(repo, nb, rid, source, "a", "b",
                  _evidence(source, "el-a", f"link {rid}"))
    with repo._write() as db:
        for rid, _source, vector in rows:
            if vector is None:
                continue
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,"
                "created_at) VALUES (?,?,?,?)",
                (rid, nb, json.dumps([float(v) for v in vector]), NOW),
            )
    return nb


def test_an_own_memory_relation_is_scored_with_its_own_vector(store):
    """Bob's own Memory relation rB sits AFTER Alice's rF in the Memory half;
    the sim Bob gets for rB must be rB's true cosine, not a neighbour row's
    (fix review P3-b: a masked index taken by count would hand him rF's)."""
    repo, bob, alice = store
    rng = np.random.default_rng(3)
    q = np.asarray(repo.retrieval.candidates._embed_query("orbital link"),
                   dtype=np.float32)
    v_foreign, v_own, v_doc = (rng.standard_normal(q.shape[0]) for _ in range(3))
    nb = _vectors_notebook(repo, bob, alice, [
        ("rD", "src-doc", v_doc), ("rF", "src-mem-alice", v_foreign),
        ("rB", "src-mem-bob", v_own),
    ])
    with source_scope_context(nb, _scope(["src-doc"], ["src-mem-bob"], bob)):
        with repo._connect() as db:
            _vectors, pairs, _fallback = repo.retrieval.candidates._relation_top_k(
                db, nb, q.tolist(), 5)
    sims = dict(pairs)
    expected = float(np.dot(q / np.linalg.norm(q), v_own / np.linalg.norm(v_own)))

    assert "rF" not in sims
    assert sims["rB"] == pytest.approx(expected, abs=1e-5)


@pytest.mark.parametrize("asker", ["bob", "unscoped"])
def test_mixed_dimensions_keep_one_vector_space_across_both_halves(store, asker):
    """Legacy rows of another dimension (codex #823 r3 P2): a valid 3-dim
    shared relation read first, then an old 2-dim Memory relation the asker
    may read.  One vector space for the whole notebook -- the first valid row
    in the notebook-wide read picks it, as the single matrix always did -- so
    the shared hit keeps its similarity 1.0 and the 2-dim Memory row is the
    one skipped, in both halves alike (built apart, each half picked its own
    dimension and the shared hit was lost)."""
    repo, bob, alice = store
    nb = _vectors_notebook(repo, bob, alice, [
        ("rS", "src-doc", [1.0, 0.0, 0.0]), ("rM", "src-mem-bob", [1.0, 0.0]),
    ])
    scope = (_scope(["src-doc"], ["src-mem-bob"], bob) if asker == "bob" else None)
    with source_scope_context(nb, scope):
        with repo._connect() as db:
            vectors, pairs, _fallback = repo.retrieval.candidates._relation_top_k(
                db, nb, [1.0, 0.0, 0.0], 5)
            shared_ids, shared_mat = repo.retrieval.candidates._vector_matrix(
                db, nb, "relation_embeddings", "relation_id")
    sims = dict(pairs)

    assert sims.get("rS") == pytest.approx(1.0, abs=1e-6), pairs
    assert "rM" not in sims
    assert vectors == 1
    assert shared_ids == ["rS"] and shared_mat.shape == (1, 3)


def test_the_first_valid_row_picks_the_space_wherever_it_lives(store):
    """The same rule when the FIRST valid row is the Memory one: the single
    matrix would have been 2-dim, so the halves are too -- the 3-dim shared
    row is the one skipped, never a second space for the shared half."""
    repo, bob, alice = store
    nb = _vectors_notebook(repo, bob, alice, [
        ("rM", "src-mem-bob", [1.0, 0.0]), ("rS", "src-doc", [1.0, 0.0, 0.0]),
    ])
    with repo._connect() as db:
        entry = repo.retrieval.candidates._relation_matrices(db, nb)
    shared_ids, shared_mat, _pos, memory_ids, memory_mat = entry[:5]

    assert (shared_ids, shared_mat.shape[0]) == ([], 0)
    assert memory_ids == ["rM"] and memory_mat.shape == (1, 2)


def test_the_relation_cache_keeps_the_key_the_cold_guard_peeks(relation_matrix):
    """The split relation entry lives under the key and version the large-
    notebook cold-matrix guard peeks (``_vector_matrix_warm``); moving it
    would make every warm large notebook look cold and skip relation
    scoring (fix review P3-c)."""
    repo, nb, _bob, alice = relation_matrix
    candidates = repo.retrieval.candidates
    with repo._connect() as db:
        assert candidates._vector_matrix_warm(db, nb, "relation_embeddings") is False

    _relations(repo, nb, _scope(["src-doc"], ["src-mem-alice"], alice))

    with repo._connect() as db:
        assert candidates._vector_matrix_warm(db, nb, "relation_embeddings") is True


def test_only_unreadable_memory_vectors_never_fall_back_to_the_full_join(
    store, monkeypatch,
):
    """The only relations with vectors are Alice's Memory relations, and Bob
    may not read them: there is vector coverage, so the leg stays bounded and
    returns nothing -- it does not fall back to the keyword-only JOIN over
    every relation (fix review P3-e)."""
    repo, bob, alice = store
    q = repo.retrieval.candidates._embed_query("orbital link")
    nb = _vectors_notebook(repo, bob, alice, [
        ("rD", "src-doc", None), ("rF", "src-mem-alice", q),
    ])
    candidates = repo.retrieval.candidates
    real = candidates._relations_with_names
    unbounded: list = []

    def _spy(db, notebook_id, relation_ids=None):
        if relation_ids is None:
            unbounded.append(notebook_id)
        return real(db, notebook_id, relation_ids=relation_ids)

    monkeypatch.setattr(candidates, "_relations_with_names", _spy)
    with source_scope_context(nb, _scope(["src-doc"], ["src-mem-bob"], bob)):
        hits = candidates._retrieve_relations_scored(nb, "orbital link")

    assert hits == [] and unbounded == []


def test_without_a_query_vector_the_bounded_fallback_is_readable_and_in_order(
    store, monkeypatch,
):
    """No query vector: the leg still hydrates only ``relation_recall`` rows,
    the first READABLE ones in whole-matrix order -- Alice's own rO (stored
    before the paper's rD) first, Bob's rF never."""
    repo, bob, alice = store
    q = repo.retrieval.candidates._embed_query("orbital link")
    nb = _vectors_notebook(repo, bob, alice, [
        ("rF", "src-mem-bob", q), ("rO", "src-mem-alice", q),
        ("rD", "src-doc", q),
    ])
    candidates = repo.retrieval.candidates
    monkeypatch.setattr(candidates, "_embed_query", lambda _query: None)
    repo.settings.relation_recall = 2
    hydrated: list = []
    real = candidates._relations_with_names
    monkeypatch.setattr(
        candidates, "_relations_with_names",
        lambda db, notebook_id, relation_ids=None: (
            hydrated.append(relation_ids),
            real(db, notebook_id, relation_ids=relation_ids))[1],
    )
    with source_scope_context(nb, _scope(["src-doc"], ["src-mem-alice"], alice)):
        candidates._retrieve_relations_scored(nb, "link rO")

    # Whole-matrix order (rO was stored before rD), never Bob's rF.
    assert hydrated == [["rO", "rD"]]


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
