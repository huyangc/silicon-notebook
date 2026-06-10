"""Regression: graph-mode [k] citation binding + evidence_level consistency.

Reproduces the eval bug (mode='graph', q17/q18): the answer cited specific
`[k]` markers yet (a) some markers were "fabricated" — malformed `[ k1]` or an
id not in id_map — and (b) the self-reported `evidence_level` was `overview`
while the answer cited specifics (an internal contradiction the judge flagged).

Root cause traced to ask_graph: the answer LLM's id_map is built from the
BFS *subgraph* (render_subgraph_context), which contains multi-hop neighbour
nodes that are NOT in `top_hits` (only the seeds are). classify_evidence then
computes `anchored_rel` over `top_hits` only, so citing a real neighbour node
yields anchored_rel=0 → it can never reach "grounded" and falls through to
"overview" — hence "overview while citing specifics". Separately, malformed /
out-of-map markers left in the answer text look like fabricated citations.

These tests drive ask_graph with a mocked answer LLM. They assert both
invariants and must fail pre-fix, pass post-fix. They reuse the same
single-notebook store_kg harness as the existing Track C/D tests.
"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import AskRequest, NotebookCreate


@pytest.fixture
def graph_repo(tmp_path, monkeypatch):
    """A notebook with a 2-node chain K1 --derived_from--> K2.

    K1 is a Formula, K2 is a Claim. The edge carries an evidence quote so
    render_subgraph_context emits a real chain line with both [k] markers.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)

    nb = r.create_notebook(NotebookCreate(name="nb"))
    r.store_kg(nb.id, None, [
        {"local_id": "K1", "object_type": "formula",
         "payload": {"name": "Gate oxide breakdown voltage", "section_path": "1"},
         "evidence": []},
        {"local_id": "K2", "object_type": "claim",
         "payload": {"name": "High field oxide failure mechanism", "section_path": "2"},
         "evidence": []},
    ], [
        {"local_id": "rel1", "source_local_id": "K1", "target_local_id": "K2",
         "edge_type": "derived_from", "evidence": [
             {"source_id": "s1", "source_title": "textbook", "element_id": "e1",
              "element_type": "paragraph", "location_label": "p1",
              "quoted_span": "oxide breakdown derives the failure mechanism",
              "confidence": 1.0}]},
    ])
    return r, nb


def _resolve_subgraph_keys(repo, nb_id, seed_oids):
    """Render the same subgraph ask_graph will build and return (id_map, key_by_oid)."""
    from app.services.kg.graph_reason import (
        DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context)
    G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(nb_id)
    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid, seed_ids=seed_oids,
        edge_types=DEFAULT_REASONING_EDGES,
        max_depth=getattr(repo.settings, "graph_max_depth", 3),
        max_fan_out=getattr(repo.settings, "graph_max_fan_out", 8))
    _, id_map = render_subgraph_context(sub, id_offset=0)
    key_by_oid = {v["object_id"]: k for k, v in id_map.items()}
    return id_map, key_by_oid


def _oid_by_name(repo, name_substr):
    from app.services.sqlite_repository import USABLE_STATUSES
    with repo._connect() as db:
        ph = ",".join("?" for _ in USABLE_STATUSES)
        rows = db.execute(
            f"SELECT id, payload FROM knowledge_objects WHERE status IN ({ph})",
            USABLE_STATUSES).fetchall()
    for row in rows:
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else (row["payload"] or {})
        if name_substr in str(payload.get("name", "")):
            return row["id"]
    raise AssertionError(f"no object name contains {name_substr!r}")


class _CitingLLM:
    """Answer LLM that cites a *valid* neighbour key plus an invalid id and a
    malformed (spaced) marker — exactly the shapes the eval judge flagged."""
    configured = True

    def __init__(self, valid_key):
        self._valid_key = valid_key

    def chat_json(self, messages, schema_hint, **kwargs):
        # Cite the real neighbour node [valid_key], an out-of-map [k99], and a
        # malformed "[ k1]" (space). Only [valid_key] should ever bind.
        answer = (
            f"Gate oxide breakdown drives the failure mechanism [{self._valid_key}]. "
            f"An unrelated claim [k99] and a spaced marker [ k1] also appear."
        )
        return json.dumps({"answer": answer, "grounded": True})


def _drive_ask_graph(repo, nb, monkeypatch):
    """Force K1 to be the only seed (top_hit) so K2 is a multi-hop neighbour
    present in id_map but absent from top_hits — the bug's trigger condition.
    Returns (response, valid_key for K2, k1_oid, k2_oid)."""
    k1_oid = _oid_by_name(repo, "Gate oxide breakdown")
    k2_oid = _oid_by_name(repo, "High field oxide failure")

    id_map, key_by_oid = _resolve_subgraph_keys(repo, nb.id, [k1_oid])
    assert k2_oid in key_by_oid, "expected K2 to be reachable as a neighbour"
    valid_key = key_by_oid[k2_oid]

    # Seed retrieval returns ONLY K1 (high relevance). K2 is therefore not in
    # top_hits, but it IS in the rendered subgraph id_map.
    from app.services.sqlite_repository import RetrievedKnowledge

    def _fake_retrieve_scored(notebook_id, query, *a, **kw):
        return [RetrievedKnowledge(
            object_type="formula", object_id=k1_oid,
            payload={"name": "Gate oxide breakdown voltage"},
            score=1.0, relevance=0.95, status="approved", owner="",
            last_reviewed="", evidence=[], notebook_id=notebook_id, tier="personal")]

    monkeypatch.setattr(repo, "_retrieve_scored", _fake_retrieve_scored)
    repo.llm_client = _CitingLLM(valid_key)
    # Skip the adversarial chain verifier (and its LLM calls) for a focused test.
    # reasoning_llm_client is a read-only property that falls back to llm_client
    # when _reasoning_llm_client is None; install a non-configured stub instead.
    repo._reasoning_llm_client = type("_NoLLM", (), {"configured": False})()

    resp = repo.ask(nb.id, AskRequest(question="oxide breakdown failure", mode="graph"))
    return resp, valid_key, k1_oid, k2_oid


def test_graph_anchors_only_bind_real_idmap_entries(graph_repo, monkeypatch):
    """(a) No fabricated/malformed markers become anchors: every returned
    anchor maps to a real id_map entry / KG object. [k99] and [ k1] must not
    appear as anchors."""
    repo, nb = graph_repo
    resp, valid_key, _k1, k2_oid = _drive_ask_graph(repo, nb, monkeypatch)

    assert resp.anchors, "expected the valid citation to produce an anchor"
    keys = {a.key for a in resp.anchors}
    assert keys == {valid_key}, f"only the valid key should bind, got {keys}"
    # The bound anchor resolves to the real neighbour object.
    assert resp.anchors[0].object_id == k2_oid
    # Fabricated/malformed markers never surface as anchors.
    assert "k99" not in keys
    assert all(a.object_id for a in resp.anchors)


def test_graph_no_fabricated_markers_reach_user(graph_repo, monkeypatch):
    """(a-cont) The user-facing answer/conclusion must not retain markers that
    fail to bind to an anchor (the invalid [k99] / malformed [ k1] the judge
    flagged as '虚构引用')."""
    repo, nb = graph_repo
    resp, valid_key, _k1, _k2 = _drive_ask_graph(repo, nb, monkeypatch)

    bound = {a.key for a in resp.anchors}
    import re
    # Any [k\d+]-shaped token left in the answer must be a bound anchor.
    for tok in re.findall(r"\[\s*k\d+\s*\]", resp.answer or ""):
        key = tok.strip("[]").strip()
        assert key in bound, f"unbound/fabricated marker {tok!r} leaked into answer"
    # The specific bogus markers must be gone.
    assert "k99" not in (resp.answer or "")
    assert "[ k1]" not in (resp.answer or "")


def test_graph_evidence_level_consistent_with_citations(graph_repo, monkeypatch):
    """(b) evidence_level must be consistent with the cited specifics. Citing a
    real, relevant graph node with grounded=True must NOT yield 'overview while
    citing specifics' — it must be 'grounded'."""
    repo, nb = graph_repo
    resp, _vk, _k1, _k2 = _drive_ask_graph(repo, nb, monkeypatch)

    assert resp.anchors, "precondition: the answer cites a specific anchor"
    # The contradiction the judge flagged: anchors present but level == overview.
    assert resp.evidence_level != "overview", (
        "graph mode reported 'overview' while citing specific [k] anchors")
    assert resp.evidence_level == "grounded"
    assert resp.grounded is True
