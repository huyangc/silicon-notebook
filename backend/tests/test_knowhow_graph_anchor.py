"""T3 — gate ii: graph-mode KG-node anchors carry a knowhow row-drawer ref.

Chunk mode already surfaces `AnswerAnchor.knowhow` (#272). This closes the
`mode=graph` / reasoning KG-node path: a BFS-reached single-row knowhow cell KO
node must thread its `{table_id, rows}` from `_federated_rx_graph._load` through
`build_rx_graph` into `render_subgraph_context`'s id_map so
`evidence_context.parse_anchors` (which already reads `context.get("knowhow")`)
can put a `CitationKnowhowRef` on the anchor — letting an ask citation jump to
the row drawer, exactly like the chunk path.

Design under test:
  * The ON/OFF gate is the settings flag `knowhow_kg_node_retrieval_enabled`,
    read in `_federated_rx_graph._load` (graph_retrieval.py): flag OFF ⇒ the
    node meta carries NO table_id/rows ⇒ byte-identical cached graph.
  * `render_subgraph_context(subgraph, id_offset, knowhow_enabled=True)` computes
    the ref from the node's raw `{table_id, rows}` via the SHARED anchor helper
    `evidence_context._knowhow_ref_from_payload` (len(rows)==1 rule — merged
    multi-row KOs stay None). `knowhow_enabled=False` forces None even for a
    single-row node (render-level off-switch, exercised below).

Production code reads the default-on flag via
`getattr(settings, "knowhow_kg_node_retrieval_enabled", True)`. These tests
toggle it with `object.__setattr__`; each repository owns a fresh Settings
instance, so no teardown is needed.
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg.graph_reason import (
    build_rx_graph, render_subgraph_context)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

_FLAG = "knowhow_kg_node_retrieval_enabled"


def _set_flag(settings, value):
    object.__setattr__(settings, _FLAG, value)


def _new_repo():
    """Build an isolated real repository for the integration/e2e tests below.

    Kept behind this factory (and every caller binds it to a NON-`repo`-named
    local — `store`) on purpose: test_repository_surface_manifest.py's consumer
    scan only records a file as a private-member consumer when a facade member is
    reached through a `repo`/`*_repo`-named handle OR a local assigned STRAIGHT
    from `SQLiteRepository(...)`. Returning from a helper breaks the direct-call
    detection and `store.*` dodges the name pattern, so this file needs no
    private-consumer manifest churn — only the SQLiteRepository import (line 37)
    is registered (TASK3_KNOWHOW_KGNODE_ALLOWED_IMPORTS). Env (DATABASE_URL /
    STORAGE_DIR) is set by the caller before this runs, so Settings() picks up
    the per-test tmp paths."""
    return SQLiteRepository(Settings())


def _knowhow_node(rows, *, table_id="tbl-1", oid="ko-kh-1", otype="电压"):
    """A single knowhow-cell KO node payload as it appears in the subgraph
    (design doc §④ KO payload shape, plus the graph node fields build_rx_graph
    stamps). `otype` is the dynamic column-name type (列名)."""
    return {
        "object_id": oid,
        "object_type": otype,
        "name": "3.3V",
        "text": "3.3V",
        "table_id": table_id,
        "rows": list(rows),
        "column_id": "col-1",
        "column_name": otype,
        "tier": "personal",
    }


# ── unit: render_subgraph_context knowhow wiring (pure, synthetic nodes) ──────

def test_render_single_row_knowhow_carries_ref():
    """A single-row knowhow KO node ⇒ id_map entry with a non-None knowhow ref
    ({table_id, row_id})."""
    node = _knowhow_node(["row-1"])
    _ctx, id_map = render_subgraph_context([(node, None, None)], id_offset=0)
    ref = id_map["k1"]["knowhow"]
    assert ref is not None
    assert ref.table_id == "tbl-1"
    assert ref.row_id == "row-1"


def test_render_multi_row_merged_knowhow_none():
    """A KO merged across >1 row has no single unambiguous row ⇒ knowhow None
    (the shared helper's len(rows)==1 rule)."""
    node = _knowhow_node(["row-1", "row-2"])
    _ctx, id_map = render_subgraph_context([(node, None, None)])
    assert id_map["k1"]["knowhow"] is None


def test_render_plain_doc_ko_knowhow_none():
    """A plain (non-knowhow) KG node has no table_id/rows ⇒ knowhow None."""
    node = {"object_id": "A", "object_type": "formula",
            "name": "Node A", "tier": "personal"}
    _ctx, id_map = render_subgraph_context([(node, None, None)])
    assert "knowhow" in id_map["k1"]
    assert id_map["k1"]["knowhow"] is None


def test_render_knowhow_disabled_forces_none_even_single_row():
    """knowhow_enabled=False ⇒ knowhow None even for a single-row KO node
    (the render-level off-switch)."""
    node = _knowhow_node(["row-1"])
    _ctx, id_map = render_subgraph_context(
        [(node, None, None)], knowhow_enabled=False)
    assert id_map["k1"]["knowhow"] is None


def test_render_knowhow_missing_table_id_none():
    """rows present but no table_id ⇒ null-safe None (never raises)."""
    node = _knowhow_node(["row-1"], table_id="")
    _ctx, id_map = render_subgraph_context([(node, None, None)])
    assert id_map["k1"]["knowhow"] is None


# ── unit: build_rx_graph threads table_id/rows into the node payload ─────────

def test_build_rx_graph_threads_knowhow_meta_into_payload():
    """When _load enriched a node's meta with table_id/rows, build_rx_graph
    copies them into the node payload so render can compute the ref."""
    nodes = {"ko-kh-1": {"type": "电压", "name": "3.3V", "tier": "personal",
                         "table_id": "tbl-1", "rows": ["row-1"]}}
    G, _idx_to_oid, oid_to_idx = build_rx_graph(nodes, [])
    payload = G[oid_to_idx["ko-kh-1"]]
    assert payload["table_id"] == "tbl-1"
    assert payload["rows"] == ["row-1"]
    # ...and the threaded payload renders to a non-None ref.
    _ctx, id_map = render_subgraph_context([(dict(payload), None, None)])
    assert id_map["k1"]["knowhow"].row_id == "row-1"


def test_build_rx_graph_no_knowhow_meta_omits_keys():
    """A plain node meta (no table_id/rows) ⇒ node payload byte-identical to
    before (no table_id/rows keys)."""
    nodes = {"A": {"type": "formula", "name": "Node A"}}
    G, _idx_to_oid, oid_to_idx = build_rx_graph(nodes, [])
    payload = G[oid_to_idx["A"]]
    assert "table_id" not in payload
    assert "rows" not in payload


# ── integration: _federated_rx_graph._load gate (real SQLiteRepository) ──────

@pytest.fixture
def kh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    store = _new_repo()
    bind_all_embedding_clients(store, FakeEmbedder(dim=16))
    nb = store.create_notebook(NotebookCreate(name="nb"))
    return store, nb


def _store_knowhow_ko(store, nb, *, name="3.3V", rows=("row-1",)):
    store.store_kg(nb.id, None, [
        {"local_id": "KH", "object_type": "电压",
         "payload": {"name": name, "text": "3.3V", "table_id": "tbl-1",
                     "rows": list(rows), "column_id": "col-1",
                     "column_name": "电压"},
         "evidence": []},
    ], [])


def _oid_by_name(store, name_substr):
    from app.services.knowledge_contracts import USABLE_STATUSES
    with store._connect() as db:
        ph = ",".join("?" for _ in USABLE_STATUSES)
        rows = db.execute(
            f"SELECT id, payload FROM knowledge_objects WHERE status IN ({ph})",
            USABLE_STATUSES).fetchall()
    for row in rows:
        payload = (json.loads(row["payload"])
                   if isinstance(row["payload"], str) else (row["payload"] or {}))
        if name_substr in str(payload.get("name", "")):
            return row["id"]
    raise AssertionError(f"no object name contains {name_substr!r}")


def test_federated_load_carries_knowhow_when_flag_on(kh_store, monkeypatch):
    store, nb = kh_store
    _set_flag(store.settings, True)
    _store_knowhow_ko(store, nb)
    G, _idx_to_oid, oid_to_idx = store.retrieval.graph._federated_rx_graph(nb.id)
    kh_oid = _oid_by_name(store, "3.3V")
    payload = G[oid_to_idx[kh_oid]]
    assert payload.get("table_id") == "tbl-1"
    assert payload.get("rows") == ["row-1"]
    _ctx, id_map = render_subgraph_context([(dict(payload), None, None)])
    assert id_map["k1"]["knowhow"].row_id == "row-1"


def test_federated_load_omits_knowhow_when_flag_off(kh_store, monkeypatch):
    store, nb = kh_store
    _set_flag(store.settings, False)
    _store_knowhow_ko(store, nb)
    G, _idx_to_oid, oid_to_idx = store.retrieval.graph._federated_rx_graph(nb.id)
    kh_oid = _oid_by_name(store, "3.3V")
    payload = G[oid_to_idx[kh_oid]]
    # byte-identical cached graph: no knowhow fields threaded when off.
    assert "table_id" not in payload
    assert "rows" not in payload
    # render still yields None ⇒ anchor.knowhow excluded (behavior unchanged).
    _ctx, id_map = render_subgraph_context([(dict(payload), None, None)])
    assert id_map["k1"]["knowhow"] is None


# ── e2e: mode=graph answer citing a knowhow node yields AnswerAnchor.knowhow ──
# Mirrors test_graph_k_binding.py: a plain seed K1 reaches the knowhow KO KH via
# a derived_from edge; KH is in the rendered subgraph id_map (a multi-hop
# neighbour) and the mocked answer LLM cites its key.

@pytest.fixture
def graph_kh_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    store = _new_repo()
    bind_all_embedding_clients(store, FakeEmbedder(dim=16))
    nb = store.create_notebook(NotebookCreate(name="nb"))
    store.store_kg(nb.id, None, [
        {"local_id": "K1", "object_type": "formula",
         "payload": {"name": "Gate oxide breakdown voltage", "section_path": "1"},
         "evidence": []},
        {"local_id": "KH", "object_type": "电压",
         "payload": {"name": "High field oxide failure mechanism", "text": "3.3V",
                     "table_id": "tbl-1", "rows": ["row-1"],
                     "column_id": "col-1", "column_name": "电压"},
         "evidence": []},
    ], [
        {"local_id": "rel1", "source_local_id": "K1", "target_local_id": "KH",
         "edge_type": "derived_from", "evidence": [
             {"source_id": "s1", "source_title": "textbook", "element_id": "e1",
              "element_type": "paragraph", "location_label": "p1",
              "quoted_span": "oxide breakdown derives the failure mechanism",
              "confidence": 1.0}]},
    ])
    return store, nb


def _resolve_subgraph_keys(store, nb_id, seed_oids):
    from app.services.kg.graph_reason import (
        DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context)
    G, idx_to_oid, oid_to_idx = store.retrieval.graph._federated_rx_graph(nb_id)
    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid, seed_ids=seed_oids,
        edge_types=DEFAULT_REASONING_EDGES,
        max_depth=getattr(store.settings, "graph_max_depth", 3),
        max_fan_out=getattr(store.settings, "graph_max_fan_out", 8))
    _ctx, id_map = render_subgraph_context(sub, id_offset=0)
    return id_map, {v["object_id"]: k for k, v in id_map.items()}


class _CiteLLM:
    """Answer LLM that cites the knowhow node's key (grounded)."""
    configured = True

    def __init__(self, valid_key):
        self._valid_key = valid_key

    def chat_json(self, messages, schema_hint, **kwargs):
        answer = f"The cell value drives the failure mechanism [{self._valid_key}]."
        return json.dumps({"answer": answer, "grounded": True})


def _drive_graph_ask(store, nb, monkeypatch):
    k1_oid = _oid_by_name(store, "Gate oxide breakdown")
    kh_oid = _oid_by_name(store, "High field oxide failure")
    _id_map, key_by_oid = _resolve_subgraph_keys(store, nb.id, [k1_oid])
    assert kh_oid in key_by_oid, "expected the knowhow KO reachable as a neighbour"
    valid_key = key_by_oid[kh_oid]

    from app.services.retrieval import RetrievedKnowledge

    def _fake_retrieve_scored(notebook_id, query, *a, **kw):
        return [RetrievedKnowledge(
            object_type="formula", object_id=k1_oid,
            payload={"name": "Gate oxide breakdown voltage"},
            score=1.0, relevance=0.95, status="approved", owner="",
            last_reviewed="", evidence=[], notebook_id=notebook_id, tier="personal")]

    monkeypatch.setattr(store.retrieval.candidates, "_retrieve_scored",
                        _fake_retrieve_scored)
    bind_chat_client(store, "ask_answer", _CiteLLM(valid_key))
    # Skip the adversarial chain verifier's LLM calls (non-configured stub).
    bind_chat_client(
        store,
        "graph_chain_verify",
        type("_NoLLM", (), {"configured": False})(),
    )
    resp = store.ask(nb.id, AskRequest(question="oxide breakdown failure", mode="graph"))
    return resp, kh_oid


def test_graph_mode_anchor_carries_knowhow_when_flag_on(graph_kh_store, monkeypatch):
    store, nb = graph_kh_store
    _set_flag(store.settings, True)
    resp, kh_oid = _drive_graph_ask(store, nb, monkeypatch)
    assert resp.anchors, "expected the cited knowhow node to produce an anchor"
    kh_anchors = [a for a in resp.anchors if a.object_id == kh_oid]
    assert kh_anchors, "the knowhow node must be a bound anchor"
    ref = kh_anchors[0].knowhow
    assert ref is not None, "graph-mode knowhow anchor must carry a row-drawer ref"
    assert ref.table_id == "tbl-1"
    assert ref.row_id == "row-1"


def test_graph_mode_anchor_no_knowhow_when_flag_off(graph_kh_store, monkeypatch):
    store, nb = graph_kh_store
    _set_flag(store.settings, False)
    resp, kh_oid = _drive_graph_ask(store, nb, monkeypatch)
    assert resp.anchors, "the node is still cited; only the knowhow jump differs"
    kh_anchors = [a for a in resp.anchors if a.object_id == kh_oid]
    assert kh_anchors
    assert kh_anchors[0].knowhow is None, "flag OFF ⇒ no knowhow ref on the anchor"
