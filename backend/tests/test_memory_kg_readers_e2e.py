"""E4-8: the knowledge-graph readers of the Memory isolation (M1), end to end
through the REAL HTTP routes and the real auth dependency.

Two registered members of one shared notebook: A (the viewer; owns the
notebook) and B, who owns a confirmed Memory there.  Every B-derived name,
text, evidence item and id carries the marker ``ZQPRIV`` (or comes from the
source ``src-mb``); the shared world never does.  A must never see any of it
through any KG surface; B sees its own Memory objects on the browse surfaces
(list, types, graph, search, neighbours) and -- the governance surfaces
belong to the whole notebook -- none on pending merges, conflicts, duplicates,
the edge review queue and the analysis, which leave Memory out for everybody.

Three worlds (``build_world``):

* ``isolated`` -- the graph as an isolated rebuild leaves it (no Memory object
  in any shared cluster), with the stored previews built;
* ``legacy``   -- the graph as it was built BEFORE the isolation (a Memory
  object clustered with a shared one, tests/pre_isolation_graph.py) in the
  state that awaits its isolated rebuild (marker 0);
* ``plain``    -- the same shared content and no Memory at all.

The routes carry no viewer argument: the viewer is the authenticated user the
auth dependency puts in the request context, and every service read takes it
from there.  The Memory channel switch (``memory:read``) is the single seam
``kg_viewer_scope.memory_channel_allowed``; this branch does not carry E1, so
the cases that close the channel patch that one function.

PostgreSQL twin: ``tests/postgres/test_memory_kg_readers_e2e_pg.py`` runs the
same cases on a real PostgreSQL schema (``q`` rewrites ``?``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable

import pytest

from app.domain.vector_index import encode_vector
from app.services import kg_viewer_scope
from tests.model_testkit import bind_chat_client
from tests.pre_isolation_graph import build_pre_isolation

NOW = "2026-09-30T00:00:00+00:00"
PRIVATE = "zqpriv"          # lower-cased marker of everything B-derived
MEMORY_SOURCE = "src-mb"
SEED = "K-zqpriv seedname"  # a legacy cluster id minted from B's Memory
_PAD = [0.0] * 14
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?")


@dataclass
class World:
    client: object
    repo: object
    q: Callable[[str], str]
    postgres: bool
    kind: str
    nb: str
    a_id: str
    b_id: str
    a: dict
    b: dict
    ids: SimpleNamespace
    needles: list


def _register(client, username: str) -> tuple[dict, str]:
    body = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    ).json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _evidence(source_id: str, element_id: str, span: str) -> dict:
    return {"source_id": source_id, "source_title": source_id,
            "element_id": element_id, "element_type": "paragraph",
            "location_label": "p0", "quoted_span": span, "confidence": 1.0}


def _concept(local_id: str, name: str, evidence: list) -> dict:
    return {"local_id": local_id, "object_type": "concept",
            "payload": {"name": name, "section_path": "1"}, "evidence": evidence}


def _claim(local_id: str, name: str, evidence: list) -> dict:
    return {"local_id": local_id, "object_type": "claim",
            "payload": {"name": name, "section_path": "1"}, "evidence": evidence}


def _edge(source: str, target: str, edge_type: str = "depends_on") -> dict:
    return {"source_local_id": source, "target_local_id": target,
            "edge_type": edge_type, "evidence": []}


def _insert_source(q, db, notebook: str, source_id: str, source_type: str,
                   memory_id, elements: list, postgres: bool) -> None:
    db.execute(
        q("INSERT INTO sources (id,notebook_id,title,source_type,status,"
          "memory_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)"),
        (source_id, notebook, source_id, source_type, "ready", memory_id, NOW, NOW),
    )
    for index, (element_id, text) in enumerate(elements):
        columns = "id,source_id,element_type,location_label,text,created_at"
        values = "?,?,'paragraph',?,?,?"
        if not postgres:
            columns += ",metadata"
            values += ",'{}'"
        db.execute(
            q(f"INSERT INTO source_elements ({columns}) VALUES ({values})"),
            (element_id, source_id, f"p{index}", text, NOW),
        )


def _object_id(world: World, name: str, source_id: str) -> str:
    expression = "payload ->> 'name'" if world.postgres else "json_extract(payload,'$.name')"
    with world.repo._connect() as db:
        row = db.execute(
            world.q(f"SELECT id FROM knowledge_objects WHERE notebook_id=? "
                    f"AND source_id=? AND {expression}=?"),
            (world.nb, source_id, name),
        ).fetchone()
    return row["id"]


def _put_vector(world: World, object_id: str, vector: list) -> None:
    value = encode_vector(vector) if world.postgres else json.dumps(vector)
    with world.repo._write() as db:
        db.execute(world.q("DELETE FROM knowledge_embeddings WHERE object_id=?"),
                   (object_id,))
        db.execute(
            world.q("INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,"
                    "created_at) VALUES (?,?,?,?)"),
            (object_id, world.nb, value, NOW),
        )


# ---------------------------------------------------------------- the worlds
GQA = "Grouped-query attention (GQA)"
MQA = "Multi-Query Attention (MQA)"


def _seed(client, repo, q, postgres: bool, kind: str) -> World:
    memory_world = kind != "plain"
    a, a_id = _register(client, "a00800001")
    b, b_id = _register(client, "b00800002")
    nb = client.post("/api/notebooks", headers=a, json={"name": "Shared"}).json()["id"]
    repo.add_member(nb, b_id)
    repo.settings.community_min_size = 2
    repo.settings.embed_dim = 16        # the vectors below are 16-dimensional
    memory_id = ""
    if memory_world:
        memory = repo.create_memory_candidate(
            nb, b_id, None, "req-e48", "Private title ZQPRIV", "ZQPRIV private text",
            [], "", {}, [],
        )
        memory_id = repo.confirm_memory(memory.id, b_id).id
    with repo._write() as db:
        _insert_source(q, db, nb, "src-s1", "markdown", None, [
            ("el-s1-a", "Shared text on grouped-query attention"),
            ("el-s1-b", "Shared text on multi-query attention"),
            ("el-s1-c", "Shared text on attention sinks")], postgres)
        _insert_source(q, db, nb, "src-s2", "markdown", None, [
            ("el-s2-a", "Shared text on GQA again"),
            ("el-s2-b", "Shared text on the KV cache"),
            ("el-s2-c", "Shared text on expert routing")], postgres)
        _insert_source(q, db, nb, "src-s3", "markdown", None, [
            ("el-s3-a", "Shared claim text on KV heads"),
            ("el-s3-b", "Shared claim text on loop gain")], postgres)
        if memory_world:
            _insert_source(q, db, nb, MEMORY_SOURCE, "memory", memory_id, [
                ("el-mb-1", "ZQPRIV private multi-query note"),
                ("el-mb-2", "ZQPRIV private alpha plan text"),
                ("el-mb-3", "ZQPRIV private beta budget text"),
                ("el-mb-4", "ZQPRIV private attention sink note")], postgres)
    sink_evidence = [_evidence("src-s1", "el-s1-c", "sinks")]
    if memory_world:
        # A shared object whose evidence ALSO quotes B's Memory: A keeps the
        # object and only the shared evidence item.
        sink_evidence.append(_evidence(MEMORY_SOURCE, "el-mb-4", "ZQPRIV sink note"))
    repo.store_kg(nb, "src-s1", [
        _concept("g", GQA, [_evidence("src-s1", "el-s1-a", "grouped")]),
        _concept("m", MQA, [_evidence("src-s1", "el-s1-b", "multi")]),
        _concept("k", "Attention sink", sink_evidence),
    ], [_edge("g", "m", "related_to")])
    repo.store_kg(nb, "src-s2", [
        _concept("g", GQA, [_evidence("src-s2", "el-s2-a", "gqa")]),
        _concept("v", "KV cache", [_evidence("src-s2", "el-s2-b", "kv")]),
        _concept("e", "Expert Routing", [_evidence("src-s2", "el-s2-c", "routing")]),
    ], [_edge("g", "v")])
    repo.store_kg(nb, "src-s3", [
        _claim("c", "GQA uses fewer KV heads than MQA",
               [_evidence("src-s3", "el-s3-a", "kv heads")]),
        # the shared conflict shape (the control of the conflict producer)
        _claim("p", "positive loop gain holds", [_evidence("src-s3", "el-s3-b", "p")]),
        _claim("n", "negative loop gain holds", [_evidence("src-s3", "el-s3-b", "n")]),
    ], [])
    world = World(client, repo, q, postgres, kind, nb, a_id, b_id, a, b,
                  SimpleNamespace(memory_id=memory_id), [])
    ids = world.ids
    ids.gqa = _object_id(world, GQA, "src-s1")
    ids.gqa2 = _object_id(world, GQA, "src-s2")
    ids.mqa = _object_id(world, MQA, "src-s1")
    ids.sink = _object_id(world, "Attention sink", "src-s1")
    ids.kv = _object_id(world, "KV cache", "src-s2")
    ids.routing = _object_id(world, "Expert Routing", "src-s2")
    if memory_world:
        repo.store_kg(nb, MEMORY_SOURCE, [
            # Same name as a shared concept: an isolated graph keeps it out of
            # the shared cluster (and the duplicate finder out of its group).
            _concept("m", MQA, [_evidence(MEMORY_SOURCE, "el-mb-1", "ZQPRIV multi")]),
            _concept("a", "ZQPRIV alpha plan",
                     [_evidence(MEMORY_SOURCE, "el-mb-2", "ZQPRIV alpha")]),
            _concept("b", "ZQPRIV beta budget",
                     [_evidence(MEMORY_SOURCE, "el-mb-3", "ZQPRIV beta")]),
            _claim("c", "ZQPRIV claim: the alpha plan relies on GQA and MQA",
                   [_evidence(MEMORY_SOURCE, "el-mb-2", "ZQPRIV claim")]),
            _concept("r", "ZQPRIV expert routing note",
                     [_evidence(MEMORY_SOURCE, "el-mb-3", "ZQPRIV route")]),
            _claim("p", "ZQPRIV positive bias holds",
                   [_evidence(MEMORY_SOURCE, "el-mb-3", "ZQPRIV p")]),
            _claim("n", "ZQPRIV negative bias holds",
                   [_evidence(MEMORY_SOURCE, "el-mb-3", "ZQPRIV n")]),
        ], [_edge("a", "m"), _edge("b", "a"), _edge("c", "a", "supports"),
            _edge("p", "n", "supports"), _edge("p", "n", "contradicts")])
        ids.mqa_b = _object_id(world, MQA, MEMORY_SOURCE)
        ids.alpha = _object_id(world, "ZQPRIV alpha plan", MEMORY_SOURCE)
        ids.beta = _object_id(world, "ZQPRIV beta budget", MEMORY_SOURCE)
        ids.claim_b = _object_id(
            world, "ZQPRIV claim: the alpha plan relies on GQA and MQA", MEMORY_SOURCE)
        ids.route_b = _object_id(world, "ZQPRIV expert routing note", MEMORY_SOURCE)
        # A relation of B's Memory that joins a Memory object to a SHARED one.
        with repo._write() as db:
            db.execute(
                q("INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                  "source_object_id,target_object_id,edge_type,evidence,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)"),
                ("rel-b-shared", nb, MEMORY_SOURCE, ids.alpha, ids.gqa, "mentions",
                 "[]", NOW),
            )
    return world


class EchoReviewer:
    """The conflict adjudicator double: a low-confidence verdict whose
    rationale and resolved payload quote the PROMPT it was shown, so whatever
    the producer fed the model reaches the route's response."""

    configured = True

    def chat_json(self, messages, schema_hint=None, **_kwargs) -> str:
        # the tail of the prompt is where the two entries' names and passages are
        seen = " | ".join(str(m.get("content", "")) for m in messages)[-1500:]
        return json.dumps({
            "conflict_type": "mutual", "resolution": "modify", "winner_ref": None,
            "resolved_payload": {"seen": seen}, "confidence": 0.4,
            "rationale": "saw: " + seen[-400:],
        })


def _run_producers(world: World) -> None:
    """The real governance producers over the built graph: Tier-2 fusion of a
    new shared source (merge candidates) and conflict adjudication."""
    repo, nb, ids = world.repo, world.nb, world.ids
    with repo._write() as db:
        _insert_source(world.q, db, nb, "src-s4", "markdown", None,
                       [("el-s4-a", "Shared text on MoE gating")], world.postgres)
    repo.store_kg(nb, "src-s4", [
        _concept("n", "MoE Gating", [_evidence("src-s4", "el-s4-a", "moe")])], [])
    ids.moe = _object_id(world, "MoE Gating", "src-s4")
    _put_vector(world, ids.routing, [1.0, 0.05] + _PAD)
    _put_vector(world, ids.moe, [0.99, 0.04] + _PAD)
    if world.kind != "plain":
        # B's nearest neighbour of the new concept: without the Tier-2
        # exclusion it would be the candidate (a pre-isolation cluster holds it).
        _put_vector(world, ids.route_b, [1.0, 0.04] + _PAD)
    repo.incremental_fuse_source(nb, "src-s4")
    bind_chat_client(repo, "kg_conflict_review", EchoReviewer())
    repo.resolve_notebook_conflicts(nb)


def build_world(client, repo, q, *, postgres: bool, kind: str) -> World:
    """``kind``: ``isolated`` | ``legacy`` | ``plain`` (see the module doc)."""
    world = _seed(client, repo, q, postgres, kind)
    if kind == "legacy":
        build_pre_isolation(repo, world.nb)
    else:
        repo.rebuild_unified_kg(world.nb)
    # what the rebuild's end-state snapshot must count: the shared rows
    world.ids.shared_counts = _shared_counts(world)
    _run_producers(world)
    repo.rebuild_communities(world.nb, force=True)
    ids = world.ids
    if kind == "legacy":
        # A cluster id minted from B's Memory name, and the marker the upgrade
        # leaves until the isolated rebuild has run.
        cluster = repo.cluster_map(world.nb)[ids.mqa]
        with repo._write() as db:
            db.execute(q("UPDATE concept_clusters SET canonical_id=? "
                         "WHERE notebook_id=? AND canonical_id=?"),
                       (SEED, world.nb, cluster))
            db.execute(q("UPDATE canonical_relations SET canonical_src=? "
                         "WHERE notebook_id=? AND canonical_src=?"),
                       (SEED, world.nb, cluster))
            db.execute(q("UPDATE canonical_relations SET canonical_tgt=? "
                         "WHERE notebook_id=? AND canonical_tgt=?"),
                       (SEED, world.nb, cluster))
            db.execute(q("UPDATE concept_clusters SET canonical_description=? "
                         "WHERE notebook_id=? AND canonical_id=?"),
                       ("FUSED ZQPRIV description", world.nb, SEED))
            db.execute(q("UPDATE unified_kg_state SET memory_isolation_version=0 "
                         "WHERE notebook_id=?"), (world.nb,))
        repo._invalidate_unified_cache(world.nb)
    else:
        assert repo._runtime.scale_artifacts.build_viz(world.nb) is not None
    if kind != "plain":
        world.needles = [PRIVATE, MEMORY_SOURCE, "el-mb-", ids.memory_id,
                         "rel-b-shared", "seedname", ids.mqa_b, ids.alpha,
                         ids.beta, ids.claim_b, ids.route_b]
    return world


# -------------------------------------------------------------------- helpers
def get(world: World, who: str, path: str, *, status: int = 200):
    headers = {"A": world.a, "B": world.b}[who]
    response = world.client.get(f"/api/notebooks/{world.nb}{path}", headers=headers)
    assert response.status_code == status, (who, path, response.status_code, response.text)
    return response


def assert_clean(world: World, response, what: str, *, tolerate=()) -> None:
    """Nothing B-derived in a response: no name, text, source, evidence, id.
    The echo of the request's own ``query`` is not a leak; ``tolerate`` lists
    strings (a known id) cut out before scanning."""
    text = response.text
    try:
        body = response.json()
        if isinstance(body, dict):
            body.pop("query", None)
        text = json.dumps(body, ensure_ascii=False)
    except ValueError:
        pass
    for allowed in tolerate:
        text = text.replace(allowed, "<allowed>")
    low = text.lower()
    found = [needle for needle in world.needles if needle.lower() in low]
    assert not found, f"{what}: leaked {found} in {text[:600]}"


SHARED_CONCEPTS = {GQA, MQA, "Attention sink", "KV cache", "Expert Routing", "MoE Gating"}
SHARED_CLAIMS = {"GQA uses fewer KV heads than MQA", "positive loop gain holds",
                 "negative loop gain holds"}
OWN_CONCEPTS = {MQA, "ZQPRIV alpha plan", "ZQPRIV beta budget",
                "ZQPRIV expert routing note"}
OWN_CLAIMS = {"ZQPRIV claim: the alpha plan relies on GQA and MQA",
              "ZQPRIV positive bias holds", "ZQPRIV negative bias holds"}


def _rows(world, who, object_type, expect_total=None):
    body = get(world, who, f"/knowledge?type={object_type}&limit=200").json()
    if expect_total is not None:
        assert body["total_count"] == expect_total, (who, object_type, body["total_count"])
    return body


# ---------------------------------------------------------------- the cases
# Each takes a world and (when it patches something) the monkeypatch.

def case_knowledge_rows_total_and_evidence(world, monkeypatch):
    """GET /knowledge: rows, total, evidence ``source_id``s."""
    a_rows = _rows(world, "A", "concept", len(SHARED_CONCEPTS) + 1)
    assert_clean(world, get(world, "A", "/knowledge?type=concept&limit=200"), "A concepts")
    # GQA is in two shared sources, so it is two rows.
    assert sorted(i["headline"] for i in a_rows["items"]) == sorted(
        list(SHARED_CONCEPTS) + [GQA])
    sink = [i for i in a_rows["items"] if i["headline"] == "Attention sink"][0]
    assert [e["source_id"] for e in sink["evidence"]] == ["src-s1"]
    claims = _rows(world, "A", "claim", len(SHARED_CLAIMS))
    assert {i["headline"] for i in claims["items"]} == SHARED_CLAIMS
    assert_clean(world, get(world, "A", "/knowledge?type=claim&limit=200"), "A claims")
    # B browsing its own Memory: the own rows, the total, the own evidence.
    b_rows = _rows(world, "B", "concept", len(SHARED_CONCEPTS) + 1 + len(OWN_CONCEPTS))
    b_sink = [i for i in b_rows["items"] if i["headline"] == "Attention sink"][0]
    assert {e["source_id"] for e in b_sink["evidence"]} == {"src-s1", MEMORY_SOURCE}
    assert OWN_CONCEPTS - {MQA} <= {i["headline"] for i in b_rows["items"]}
    _rows(world, "B", "claim", len(SHARED_CLAIMS) + len(OWN_CLAIMS))
    # paging does not move the total
    page = get(world, "A", "/knowledge?type=concept&limit=2&offset=2").json()
    assert page["total_count"] == len(SHARED_CONCEPTS) + 1 and len(page["items"]) == 2


def case_knowledge_types(world, monkeypatch):
    """GET /knowledge-types."""
    a = {t["object_type"]: t["count"] for t in get(world, "A", "/knowledge-types").json()}
    assert a == {"concept": len(SHARED_CONCEPTS) + 1, "claim": len(SHARED_CLAIMS)}
    assert_clean(world, get(world, "A", "/knowledge-types"), "A types")
    b = {t["object_type"]: t["count"] for t in get(world, "B", "/knowledge-types").json()}
    assert b == {"concept": len(SHARED_CONCEPTS) + 1 + len(OWN_CONCEPTS),
                 "claim": len(SHARED_CLAIMS) + len(OWN_CLAIMS)}


def case_legacy_graph(world, monkeypatch):
    """GET /graph: nodes and edges."""
    a = get(world, "A", "/graph")
    assert_clean(world, a, "A graph")
    assert {n["headline"] for n in a.json()["nodes"]} == SHARED_CONCEPTS | SHARED_CLAIMS
    b = get(world, "B", "/graph").json()
    assert OWN_CONCEPTS - {MQA} <= {n["headline"] for n in b["nodes"]}
    # B's relation between its own and a shared object is B's own edge
    assert any(e["relation"] == "mentions" for e in b["edges"])
    assert not any(e["relation"] == "mentions" for e in a.json()["edges"])


def _node_names(graph) -> set:
    return {n["payload"]["name"] for n in graph["nodes"]}


def _unified(world, path):
    query = "level=object" + ("&limit=80" if path == "artifact" else "")
    a = get(world, "A", f"/unified-kg?{query}")
    assert_clean(world, a, f"A unified-kg ({path})")
    shared = _node_names(a.json())
    assert {GQA, MQA, "KV cache"} <= shared and not any(PRIVATE in n.lower() for n in shared)
    b = get(world, "B", f"/unified-kg?{query}").json()
    assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= _node_names(b)
    assert b["total_nodes"] > a.json()["total_nodes"]
    # the shared half is the same graph for both viewers
    assert shared <= _node_names(b)
    if path == "full":
        concept = get(world, "A", "/unified-kg?level=concept")
        assert_clean(world, concept, "A unified-kg concept level")
        assert "ZQPRIV alpha plan" in _node_names(
            get(world, "B", "/unified-kg?level=concept").json())


def case_unified_graph_full_path(world, monkeypatch):
    """GET /unified-kg without a stored preview in the way (full path)."""
    _unified(world, "full")


def case_unified_graph_artifact_path(world, monkeypatch):
    """GET /unified-kg served from the persisted viz artifact."""
    probe = get(world, "A", "/unified-kg/status").json()
    assert probe["viz_indexed"] is True, probe
    _unified(world, "artifact")


def case_unified_graph_large_library_artifact_only(world, monkeypatch):
    """GET /unified-kg of a library past the synchronous-build size: it is
    answered from the persisted artifact alone (never the live tables), plus
    the viewer's own Memory objects for B."""
    monkeypatch.setattr(world.repo.settings, "viz_sync_build_max_objects", 1)
    a = get(world, "A", "/unified-kg?level=object")
    assert_clean(world, a, "A unified-kg (large library)")
    assert {GQA, MQA, "KV cache"} <= _node_names(a.json())
    b = get(world, "B", "/unified-kg?level=object").json()
    assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= _node_names(b)
    assert b["total_nodes"] > a.json()["total_nodes"]
    # the legacy full graph of such a library is refused for everyone (413)
    get(world, "A", "/graph", status=413)


def case_search_lexical_and_folded_names(world, monkeypatch):
    """GET /kg/search: FTS, and the folded cluster names."""
    for query in ("ZQPRIV", "zqpriv alpha", "alpha plan", "Secret"):
        assert_clean(world, get(world, "A", f"/kg/search?q={query}"), f"A kg/search {query}")
    hits = get(world, "B", "/kg/search?q=ZQPRIV").json()["hits"]
    assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= {h["name"] for h in hits}
    # MQA exists as a shared cluster AND as B's own Memory concept: A gets the
    # shared one only (a folded hit), B gets both.
    a_hits = get(world, "A", "/kg/search?q=Multi-Query").json()["hits"]
    assert [h["name"] for h in a_hits if h["name"] == MQA] == [MQA]
    assert_clean(world, get(world, "A", "/kg/search?q=Multi-Query"), "A folded")
    b_hits = get(world, "B", "/kg/search?q=Multi-Query").json()["hits"]
    assert len([h for h in b_hits if h["name"] == MQA]) == 2


def case_search_semantic_leg(world, monkeypatch):
    """GET /kg/search, ANN leg: an index that still names B's objects (an
    index built before the isolation) answers the route for A without them."""
    query = world.repo._runtime.knowledge_query
    own = [world.ids.alpha, world.ids.beta, world.ids.claim_b, world.ids.mqa_b]
    shared = [world.ids.gqa, world.ids.kv]
    monkeypatch.setattr(query, "semantic_search", lambda _nb, _q, _limit: [
        {"object_id": oid, "name": "", "score": 0.9 - 0.01 * index, "match": "semantic"}
        for index, oid in enumerate(own + shared)])
    a = get(world, "A", "/kg/search?q=nothing-lexical")
    assert_clean(world, a, "A semantic")
    assert {h["name"] for h in a.json()["hits"]} == {GQA, "KV cache"}
    b = get(world, "B", "/kg/search?q=nothing-lexical").json()["hits"]
    assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= {h["name"] for h in b}


def case_search_semantic_leg_over_a_real_index(world, monkeypatch):
    """GET /kg/search, ANN leg over the notebook's REAL scale index: an index
    built now never holds B's Memory (its nearest-neighbour note is embedded
    and is still not in it), so neither viewer's semantic hits name it."""
    from app.services.embedding import FakeEmbedder
    from tests.model_testkit import bind_all_embedding_clients

    bind_all_embedding_clients(world.repo, FakeEmbedder(dim=16))
    world.repo.build_scale_index(world.nb)
    index = world.repo._runtime.scale_artifacts.load(world.nb, allow_stale=True)
    assert index is not None and index.ann_labels, "the index holds the embedded objects"
    assert world.ids.route_b not in index.ann_labels
    # the index counts are the shared ones, whoever asks
    scale = {who: get(world, who, "/index-status").json()["scale_index"] for who in ("A", "B")}
    assert scale["A"] == scale["B"] and scale["A"]["n_ann"] == 2, scale
    for who in ("A", "B"):
        response = get(world, who, "/kg/search?q=nothing-lexical")
        hits = response.json()["hits"]
        assert hits and {h["match"] for h in hits} == {"semantic"}, (who, hits)
        assert {h["name"] for h in hits} <= {"Expert Routing", "MoE Gating"}, (who, hits)
        assert_clean(world, response, f"{who} semantic hits over a real index")


def case_pending_isolation_answers_a_folded_hit_by_a_visible_member(world, monkeypatch):
    """The notebook still awaits its isolated rebuild and a cluster's id was
    minted from B's Memory (``K-<seed>``): A's folded hit is answered by the
    first visible member's object id, never by the cluster id."""
    a = get(world, "A", "/kg/search?q=Multi-Query")
    assert_clean(world, a, "A pending folded hit")
    hits = [h for h in a.json()["hits"] if h["name"] == MQA]
    assert [h["object_id"] for h in hits] == [world.ids.mqa], hits
    # the answered id opens for A
    get(world, "A", f"/objects/{world.ids.mqa}/context")
    # B reads every member of the cluster: the cluster id is B's to see
    b_hits = [h for h in get(world, "B", "/kg/search?q=Multi-Query").json()["hits"]
              if h["name"] == MQA]
    assert SEED in {h["object_id"] for h in b_hits}


def case_legacy_mixed_cluster_reads(world, monkeypatch):
    """A cluster that still mixes a shared and a Memory member: every read
    that shows the cluster or its parts shows only what A may read."""
    # (the cluster's own id is the one thing a cluster that still mixes
    # members may carry on these reads: the plan calls the seeded id out for
    # the search only, and the search case above pins that)
    for path in ("/knowledge?type=concept&limit=200", "/knowledge-types", "/graph",
                 "/unified-kg?level=object", "/unified-kg?level=concept",
                 f"/objects/{world.ids.mqa}/neighbors",
                 f"/objects/{world.ids.gqa}/neighbors",
                 f"/objects/{world.ids.mqa}/context",
                 f"/concepts/{SEED}/detail"):
        assert_clean(world, get(world, "A", path), f"A legacy {path}", tolerate=(SEED,))
    detail = get(world, "A", f"/concepts/{SEED}/detail").json()
    assert [m["id"] for m in detail["members"]] == [world.ids.mqa]
    assert detail["member_total"] == 1
    # B's own Memory object is B's: its context opens for B, is missing for A
    get(world, "B", f"/objects/{world.ids.alpha}/context")
    get(world, "A", f"/objects/{world.ids.alpha}/context", status=404)
    b_detail = get(world, "B", f"/concepts/{SEED}/detail").json()
    assert {m["id"] for m in b_detail["members"]} == {world.ids.mqa, world.ids.mqa_b}


def case_neighbours(world, monkeypatch):
    """GET /objects/{id}/neighbors: the edges."""
    for focus in (world.ids.gqa, world.ids.mqa, world.ids.kv):
        assert_clean(world, get(world, "A", f"/objects/{focus}/neighbors"),
                     f"A neighbours of {focus}")
    # B's Memory object is missing for A: no nodes, no edges, no name
    hidden = get(world, "A", f"/objects/{world.ids.alpha}/neighbors")
    assert hidden.json()["nodes"] == [] and hidden.json()["edges"] == []
    # B's own neighbourhood: its own Memory objects and the relations between them
    own = get(world, "B", f"/objects/{world.ids.alpha}/neighbors").json()
    assert {n["payload"]["name"] for n in own["nodes"]} >= {
        "ZQPRIV alpha plan", "ZQPRIV beta budget"}
    assert own["edges"]
    # the relation joining B's object to the shared GQA is not in the shared graph
    shared = get(world, "B", f"/objects/{world.ids.gqa}/neighbors").json()
    assert "mentions" not in {e["edge_type"] for e in shared["edges"]}


def case_governance_lists(world, monkeypatch):
    """Pending merges, pending conflicts (rationale, resolved payload),
    duplicates and the edge review queue -- the producers ran with B's Memory in
    the notebook; the controls prove they ran."""
    for who in ("A", "B"):
        merges = get(world, who, "/unified-kg/pending-merges")
        assert_clean(world, merges, f"{who} pending merges")
        pairs = {frozenset((m["canonical_a"], m["canonical_b"])) for m in merges.json()}
        assert frozenset(("K-moe gating", "K-expert routing")) in pairs, merges.text
        conflicts = get(world, who, "/kg/conflicts/pending")
        assert_clean(world, conflicts, f"{who} pending conflicts")
        body = conflicts.json()
        assert body and all(c["rationale"] and c["resolved_payload"] for c in body)
        assert "loop gain" in conflicts.text, "control: the shared pair was adjudicated"
        duplicates = get(world, who, "/duplicates?type=concept")
        assert_clean(world, duplicates, f"{who} duplicates")
        groups = duplicates.json()
        assert [sorted(m["headline"] for m in g["members"]) for g in groups] == [[GQA, GQA]]
        queue = get(world, who, "/edge-review-queue")
        assert_clean(world, queue, f"{who} edge review queue")
        # the two shared relations only (B's Memory relations are not ranked)
        assert queue.json()["total"] == len(queue.json()["items"]) == 2


def _shared_counts(world):
    """The shared object / relation counts a rebuild snapshot must hold."""
    with world.repo._connect() as db:
        objects = db.execute(world.q(
            "SELECT COUNT(*) AS c FROM knowledge_objects ko JOIN sources s ON s.id=ko.source_id "
            "WHERE ko.notebook_id=? AND ko.status<>'deprecated' AND s.source_type<>'memory'"),
            (world.nb,)).fetchone()["c"]
        relations = db.execute(world.q(
            "SELECT COUNT(*) AS c FROM knowledge_relations kr JOIN sources s ON s.id=kr.source_id "
            "WHERE kr.notebook_id=? AND s.source_type<>'memory'"),
            (world.nb,)).fetchone()["c"]
    return int(objects), int(relations)


def case_analysis_and_status_counts(world, monkeypatch):
    """GET /kg-analysis (board member names, largest clusters, counts), the
    status routes' counts and the notebook card."""
    objects, relations = world.ids.shared_counts
    analysis, statuses = {}, {}
    for who in ("A", "B"):
        response = get(world, who, "/kg-analysis")
        assert_clean(world, response, f"{who} kg-analysis")
        body = response.json()
        analysis[who] = _TIMESTAMP.sub("<t>", response.text)
        last = body["state"]["last_rebuild"]
        assert (last["object_count"], last["relation_count"]) == (objects, relations)
        boards = body["boards"]["payload"]["communities"]
        assert boards and all(PRIVATE not in m.lower()
                              for c in boards for m in c["top_members"])
        largest = [a for a in body["artifacts"] if a["kind"] == "largest_clusters"][0]
        assert {c["canonical_name"] for c in largest["payload"]["clusters"]} <= (
            SHARED_CONCEPTS)
        status = get(world, who, "/unified-kg/status").json()
        assert (status["objects"], status["relations"]) == (objects, relations)
        statuses[who] = status
        assert_clean(world, get(world, who, "/index-status"), f"{who} index-status")
    # neither the analysis nor the status counts depend on who reads them
    assert analysis["A"] == analysis["B"]
    assert statuses["A"] == statuses["B"]
    assert statuses["A"]["viz_nodes"] <= objects
    sources = get(world, "A", "/kg-analysis/sources")
    assert_clean(world, sources, "A kg-analysis sources")
    assert {row["source_id"] for row in sources.json()["rows"]} == {
        "src-s1", "src-s2", "src-s3", "src-s4"}
    # the analytics card's knowledge counts: the shared view plus one's own
    shared_k = {"concept": len(SHARED_CONCEPTS) + 1, "claim": len(SHARED_CLAIMS)}
    assert get(world, "A", "/analytics").json()["knowledge_counts"] == shared_k
    assert get(world, "B", "/analytics").json()["knowledge_counts"] == {
        "concept": shared_k["concept"] + len(OWN_CONCEPTS),
        "claim": shared_k["claim"] + len(OWN_CLAIMS)}
    # the notebook card: a Memory count only for its owner
    card = {who: get(world, who, "").json() for who in ("A", "B")}
    assert card["A"]["counts"]["memories"] == 0 and card["B"]["counts"]["memories"] == 1
    assert card["A"]["kg_ready"] and card["B"]["kg_ready"]
    assert_clean(world, get(world, "A", ""), "A card")
    listed = {who: world.client.get("/api/notebooks", headers=h).json()
              for who, h in (("A", world.a), ("B", world.b))}
    memories = {who: [n for n in rows if n["id"] == world.nb][0]["counts"]["memories"]
                for who, rows in listed.items()}
    assert memories == {"A": 0, "B": 1}


def case_http_search(world, monkeypatch):
    """GET /search: the knowledge leg of the search box."""
    for query in ("ZQPRIV", "alpha plan", "Multi-Query", "Secret"):
        assert_clean(world, get(world, "A", f"/search?q={query}"), f"A /search {query}")
    b = get(world, "B", "/search?q=ZQPRIV").json()["hits"]
    assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= {h["label"] for h in b}
    a = get(world, "A", "/search?q=Multi-Query").json()["hits"]
    assert [h["label"] for h in a if h["scope"] != "Element"] == [MQA]


class _ClosedMemoryChannel:
    """ASGI wrapper that runs every request inside E1's real
    ``source_scope.memory_access_context(False)`` -- the state a caller
    without ``memory:read`` runs in.  (Over HTTP only browser sessions reach
    these routes and they always hold the channel; a token without
    ``memory:read`` reaches the same services through MCP, pinned end to end
    by ``run_mcp_cases``.  This drives every route surface under the closed
    channel.)  Sync endpoints inherit it: anyio's worker threads copy the
    request task's context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from app.services.source_scope import memory_access_context

        with memory_access_context(False):
            await self.app(scope, receive, send)


def case_channel_closed(world, monkeypatch):
    """A caller without ``memory:read``: no Memory-derived row at all (B's own
    included) on every browse surface, no Memory-item hit in either search,
    and the Memory count is 0."""
    from fastapi.testclient import TestClient

    closed = TestClient(_ClosedMemoryChannel(world.client.app))

    def get(world, who, path, *, status=200):
        headers = {"A": world.a, "B": world.b}[who]
        url = path if path.startswith("/api/") else f"/api/notebooks/{world.nb}{path}"
        response = closed.get(url, headers=headers)
        assert response.status_code == status, (who, path, response.status_code, response.text)
        return response

    for path in ("/knowledge?type=concept&limit=200", "/knowledge?type=claim&limit=200",
                 "/knowledge-types", "/graph", "/unified-kg?level=object",
                 "/unified-kg?level=object&limit=80", "/unified-kg?level=concept",
                 "/kg/search?q=Multi-Query",
                 f"/objects/{world.ids.gqa}/neighbors",
                 f"/objects/{world.ids.alpha}/neighbors"):
        # (a neighbour answer echoes the id that was asked for)
        assert_clean(world, get(world, "B", path), f"B without memory:read {path}",
                     tolerate=(world.ids.alpha,))
    own = get(world, "B", f"/objects/{world.ids.alpha}/neighbors").json()
    assert own["nodes"] == [] and own["edges"] == []
    # Neither search answers anything of B's Memory: no Memory-derived
    # knowledge-graph hit and no Memory-item hit (object_type / scope Memory).
    assert get(world, "B", "/kg/search?q=ZQPRIV").json()["hits"] == []
    assert get(world, "B", "/search?q=ZQPRIV").json()["hits"] == []
    types = {t["object_type"]: t["count"] for t in get(world, "B", "/knowledge-types").json()}
    assert types == {"concept": len(SHARED_CONCEPTS) + 1, "claim": len(SHARED_CLAIMS)}
    get(world, "B", f"/objects/{world.ids.alpha}/context", status=404)
    assert get(world, "B", "").json()["counts"]["memories"] == 0
    rows = get(world, "B", "/api/notebooks").json()
    assert [n for n in rows if n["id"] == world.nb][0]["counts"]["memories"] == 0
    # control: the same requests with the channel open do find B's Memory
    open_hits = [h["object_type"] for h in globals()["get"](
        world, "B", "/kg/search?q=ZQPRIV").json()["hits"]]
    assert open_hits, "fixture: B's Memory is searchable with the channel open"


ISOLATED_CASES = [
    case_knowledge_rows_total_and_evidence,
    case_knowledge_types,
    case_legacy_graph,
    case_unified_graph_full_path,
    case_unified_graph_artifact_path,
    case_unified_graph_large_library_artifact_only,
    case_search_lexical_and_folded_names,
    case_search_semantic_leg,
    case_search_semantic_leg_over_a_real_index,
    case_neighbours,
    case_governance_lists,
    case_analysis_and_status_counts,
    case_http_search,
    case_channel_closed,
]
LEGACY_CASES = [
    case_pending_isolation_answers_a_folded_hit_by_a_visible_member,
    case_legacy_mixed_cluster_reads,
]

# ---------------------------------------------------------- a notebook without Memory
PLAIN_ROUTES = (
    "/knowledge?type=concept&limit=200", "/knowledge?type=claim&limit=200",
    "/knowledge-types", "/graph", "/unified-kg?level=object",
    "/unified-kg?level=concept", "/unified-kg?level=object&limit=80",
    "/kg/search?q=Multi-Query", "/unified-kg/pending-merges",
    "/kg/conflicts/pending", "/duplicates?type=concept", "/edge-review-queue",
    "/kg-analysis", "/unified-kg/status", "/index-status", "/search?q=Multi-Query",
    "",
)


def _capture_plain(world) -> dict:
    out = {}
    for who in ("A", "B"):
        paths = list(PLAIN_ROUTES) + [
            f"/objects/{world.ids.gqa}/neighbors", f"/objects/{world.ids.mqa}/context",
            "/concepts/K-grouped query attention/detail"]
        for path in paths:
            for _warm in range(2):      # the second read is the recorded one
                response = world.client.get(
                    f"/api/notebooks/{world.nb}{path}",
                    headers={"A": world.a, "B": world.b}[who])
            body = _TIMESTAMP.sub("<t>", json.dumps(
                response.json(), sort_keys=True, ensure_ascii=False))
            out[(who, path)] = (response.status_code, body)
    return out


def case_no_memory_notebook_is_byte_identical(world, monkeypatch):
    """A notebook with no Memory answers every route byte for byte as it does
    with the viewer plumbing switched off (the baseline is captured in this
    process, store switch forced off)."""
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", False)
    baseline = _capture_plain(world)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    with_plumbing = _capture_plain(world)
    assert set(baseline) == set(with_plumbing)
    # the answers are real ones, not a pile of errors
    assert all(code == 200 for code, _ in baseline.values()), {
        k: v[0] for k, v in baseline.items() if v[0] != 200}
    for key in baseline:
        assert with_plumbing[key] == baseline[key], key


# ------------------------------------------------------------------ MCP
async def run_mcp_cases(world, monkeypatch) -> None:
    """MCP ``search_notebook_context``: its knowledge leg for A and for B."""
    from tests.test_memory_mcp import OfficialMcpClient, _payload

    repo = world.repo
    tokens = {}
    for who, user_id, scopes in (
        ("A", world.a_id, ["knowledge:read", "memory:read"]),
        ("B", world.b_id, ["knowledge:read", "memory:read"]),
        ("B-no-memory", world.b_id, ["knowledge:read"]),
    ):
        profile = repo.create_agent_profile(user_id, f"agent-{who}", "")
        tokens[who] = repo.issue_agent_token(
            user_id, profile.id, scopes, world.nb, [world.nb], None)
    app = world.client.app

    async def search(who: str, query: str) -> dict:
        async with OfficialMcpClient(app, tokens[who].token, manage_lifespan=False) as client:
            _payload(await client.call("select_notebook", {"notebook_id": world.nb}))
            return _payload(await client.call(
                "search_notebook_context", {"query": query, "limit": 40}))

    def labels(payload) -> set:
        return {item["label"] for item in payload["items"]}

    async with app.router.lifespan_context(app):
        for query in ("ZQPRIV", "alpha plan", "Multi-Query"):
            blob = json.dumps(await search("A", query), ensure_ascii=False).lower()
            assert [n for n in world.needles if n.lower() in blob] == [], (query, blob[:500])
        own = await search("B", "ZQPRIV")
        assert {"ZQPRIV alpha plan", "ZQPRIV beta budget"} <= labels(own)
        shared = await search("A", "Multi-Query")
        assert MQA in labels(shared)
        # B's own token WITHOUT memory:read: the tool closes E1's real Memory
        # channel, so neither a Memory-derived knowledge row nor a Memory item
        # comes back (with memory:read, above, both do).
        assert any(item["type"] == "memory" for item in own["items"]), own
        closed_payload = await search("B-no-memory", "ZQPRIV")
        closed = json.dumps(closed_payload, ensure_ascii=False)
        assert PRIVATE not in closed.lower(), closed[:500]
        assert [item for item in closed_payload["items"]
                if item["type"] == "memory" or item.get("memory_id")] == []


# ------------------------------------------------------------------- SQLite fixtures
def _sqlite_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'e48.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://memory.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
    return TestClient(create_app()), repository()


def _world_of(kind, tmp_path, monkeypatch) -> World:
    client, repo = _sqlite_app(tmp_path, monkeypatch)
    return build_world(client, repo, lambda sql: sql, postgres=False, kind=kind)


@pytest.mark.parametrize("case", ISOLATED_CASES, ids=lambda c: c.__name__)
def test_isolated_notebook_through_the_routes(case, tmp_path, monkeypatch):
    case(_world_of("isolated", tmp_path, monkeypatch), monkeypatch)


@pytest.mark.parametrize("case", LEGACY_CASES, ids=lambda c: c.__name__)
def test_notebook_awaiting_its_isolated_rebuild_through_the_routes(case, tmp_path, monkeypatch):
    case(_world_of("legacy", tmp_path, monkeypatch), monkeypatch)


def test_a_notebook_without_memory_is_byte_identical_through_the_routes(tmp_path, monkeypatch):
    case_no_memory_notebook_is_byte_identical(
        _world_of("plain", tmp_path, monkeypatch), monkeypatch)


@pytest.mark.anyio
async def test_mcp_search_notebook_context_knowledge_leg(tmp_path, monkeypatch):
    await run_mcp_cases(_world_of("isolated", tmp_path, monkeypatch), monkeypatch)
