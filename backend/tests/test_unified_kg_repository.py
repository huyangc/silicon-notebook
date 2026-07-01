import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")  # embedder_configured=True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)               # inject; no real model loads (lazy)
    return r

def test_store_kg_batch_embeds_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": "1"}, "evidence": []},
    ]
    repo.store_kg(nb.id, None, objs, [])
    with repo._connect() as db:
        rows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchall()
    assert len(rows) == 2                      # both nodes embedded
    assert len(json.loads(rows[0]["vector"])) == 16

def test_cluster_and_candidate_crud(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [
        {"canonical_id": "K1", "member_object_id": "o1", "canonical_name": "MOSFET"},
        {"canonical_id": "K1", "member_object_id": "o2", "canonical_name": "MOSFET"},
    ])
    assert repo.cluster_map(nb.id) == {"o1": "K1", "o2": "K1"}
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    pend = repo.pending_merges(nb.id)
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    repo.set_merge_decision(nb.id, pend[0]["id"], "rejected")
    assert repo.pending_merges(nb.id) == []
    assert repo.decided_pairs(nb.id) == {("K1", "K2"): "rejected"}

def test_set_merge_decision_rejects_bad_status(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    cid = repo.pending_merges(nb.id)[0]["id"]
    with pytest.raises(ValueError):
        repo.set_merge_decision(nb.id, cid, "maybe")

def test_rebuild_merges_same_concept_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"mosfet","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2   # both MOSFET nodes one cluster

def test_rebuild_is_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"X","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id); first = repo.cluster_map(nb.id)
    repo.rebuild_unified_kg(nb.id); assert repo.cluster_map(nb.id).keys() == first.keys()

def test_unified_graph_concept_level_cached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]},
        {"local_id":"b","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]},
    ], [{"source_local_id":"b","target_local_id":"a","edge_type":"depends_on","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="concept")
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
    assert g["total_nodes"] == 2 and g["truncated"] is False   # metadata for "widen range"
    # the full graph is cached (same object); unified_graph wraps it with metadata
    assert repo._unified_graph_full(nb.id, "concept") is repo._unified_cache[(nb.id,"concept")]


def test_unified_graph_limit_returns_core_subgraph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # hub h linked to a/b/c (degree 3,1,1,1); iso has no edge (degree 0)
    objs = [{"local_id": i, "object_type": "concept",
             "payload": {"name": i, "section_path": ""}, "evidence": []}
            for i in ["h", "a", "b", "c", "iso"]]
    rels = [{"source_local_id": "h", "target_local_id": t, "edge_type": "rel", "evidence": []}
            for t in ["a", "b", "c"]]
    repo.store_kg(nb.id, None, objs, rels)
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="concept", limit=2)
    assert g["total_nodes"] == 5 and g["truncated"] is True
    assert len(g["nodes"]) == 2                                  # hub + one neighbor
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["source_object_id"] in ids and e["target_object_id"] in ids for e in g["edges"])
    gfull = repo.unified_graph(nb.id, level="concept", limit=99)
    assert gfull["truncated"] is False and len(gfull["nodes"]) == 5   # whole graph, isolated incl.

def test_store_kg_invalidates_unified_cache(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"A","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    g1 = repo.unified_graph(nb.id, level="concept")
    assert (nb.id, "concept") in repo._unified_cache
    # a new store_kg must evict the cache
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"B","section_path":""},"evidence":[]}], [])
    assert (nb.id, "concept") not in repo._unified_cache

def test_concept_detail_lists_members_and_attached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},
         "evidence":[{"source_id":"s","source_title":"D","element_id":"e","element_type":"p","location_label":"1","quoted_span":"MOSFET","confidence":1.0}]},
        {"local_id":"k","object_type":"claim","payload":{"name":"MOSFET has threshold","section_path":""},"evidence":[]},
    ], [{"source_local_id":"k","target_local_id":"a","edge_type":"about","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    detail = repo.concept_detail(nb.id, cid)
    assert detail["canonical_name"] == "MOSFET"
    assert any(x["object_type"]=="claim" for x in detail["attached"])
    assert detail["evidence"]

def test_confirm_merge_unions_clusters_on_rebuild(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"current source","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    a_cid, b_cid = cmap[list(cmap)[0]], cmap[list(cmap)[1]]
    assert a_cid != b_cid                                # distinct names -> separate clusters
    repo.write_merge_candidate(nb.id, a_cid, b_cid, 0.84)
    cand = repo.pending_merges(nb.id)[0]
    repo.confirm_merge(nb.id, cand["id"])
    repo.rebuild_unified_kg(nb.id)
    assert len(set(repo.cluster_map(nb.id).values())) == 1   # forced union held across rebuild


def test_rebuild_tolerates_mixed_dim_vectors(repo):
    import datetime
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"A","section_path":""},"evidence":[]}], [])
    with repo._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
            ("rogue", nb.id, json.dumps([0.1] * 999), datetime.datetime.now().isoformat()))
    assert repo.rebuild_unified_kg(nb.id) >= 1   # must NOT raise on mismatched-dim vector


def test_unified_kg_dirty_status_lifecycle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 0

    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []}
    ], [])
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is True

    repo.rebuild_unified_kg(nb.id)
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 1


def test_rebuild_applies_llm_confirmed_auto_candidate(repo):
    """LLM 兜底: auto_candidate 经 LLM 确认 merge 后, rebuild 应将两个概念合入同一簇。"""
    from app.services.sqlite_repository import _now
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    o1 = repo._test_insert_object(nb.id, "concept", {"name": "operational amplifier"})
    o2 = repo._test_insert_object(nb.id, "concept", {"name": "op amplifier circuit"})
    # 用16维向量(与 EMBED_DIM=16 匹配), 方向相同 -> 余弦相似度=1.0(≥hi=0.94)
    vec = [1.0] + [0.0] * 15
    with repo._write() as db:
        for oid in (o1, o2):
            db.execute(
                "INSERT OR REPLACE INTO knowledge_embeddings "
                "(object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (oid, nb.id, json.dumps(vec), _now()))

    class _LLM:
        configured = True
        def chat_json(self, messages, schema):
            return ('{"decisions":[{"candidate_id":"ac0","decision":"merge",'
                    '"canonical_name":"operational amplifier","confidence":0.99,'
                    '"rationale":"same concept"}]}')

    repo.llm_client = _LLM()
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert cmap.get(o1) == cmap.get(o2), (
        f"Expected same cluster after LLM-confirmed merge, got {cmap}")


class _CannedReviewLLM:
    """Mock LLM that returns a fixed (decision, confidence) for every candidate it
    is asked to review. Mirrors concept_merge_review's chat_json interface so
    review_pending_merges runs without a real model."""

    configured = True

    def __init__(self, decision: str, confidence: float):
        self._decision = decision
        self._confidence = confidence

    def chat_json(self, messages, schema):
        import re
        content = messages[0]["content"]
        ids = re.findall(r"id=(\S+)", content)
        decisions = [
            {
                "candidate_id": cid,
                "decision": self._decision,
                "canonical_name": "x",
                "confidence": self._confidence,
                "rationale": "canned",
            }
            for cid in ids
        ]
        return json.dumps({"decisions": decisions})


def _candidate_status(repo, nb_id, cid):
    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM concept_merge_candidates WHERE id=? AND notebook_id=?",
            (cid, nb_id)).fetchone()
    return row["status"]


def test_review_merge_below_confirm_threshold_becomes_deferred(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    repo.llm_client = _CannedReviewLLM("merge", 0.88)
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 0, "unsure": 1}
    assert _candidate_status(repo, nb.id, cid) == "deferred"


def test_review_merge_at_confirm_threshold_is_confirmed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    repo.llm_client = _CannedReviewLLM("merge", 0.92)
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 1, "rejected": 0, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "confirmed"


def test_review_keep_separate_at_threshold_is_rejected(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    repo.llm_client = _CannedReviewLLM("keep_separate", 0.85)
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 1, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "rejected"


def test_review_keep_separate_below_threshold_becomes_deferred(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    repo.llm_client = _CannedReviewLLM("keep_separate", 0.70)
    out = repo.review_pending_merges(nb.id, confirm_threshold=0.90, separate_threshold=0.80)
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 0, "unsure": 1}
    assert _candidate_status(repo, nb.id, cid) == "deferred"


def test_review_defaults_drain_keep_separate_that_old_single_threshold_left_pending(repo):
    """Under the old single 0.95 threshold a 0.88 keep_separate stayed pending forever.
    With the asymmetric settings defaults (confirm 0.90 / separate 0.80) it now drains
    to rejected without any explicit threshold args."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    assert repo.settings.kg_merge_confirm_threshold == 0.90
    assert repo.settings.kg_merge_separate_threshold == 0.80
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.8)
    cid = repo.pending_merges(nb.id)[0]["id"]
    repo.llm_client = _CannedReviewLLM("keep_separate", 0.88)
    out = repo.review_pending_merges(nb.id)  # no thresholds -> settings defaults
    assert out == {"reviewed": 1, "confirmed": 0, "rejected": 1, "unsure": 0}
    assert _candidate_status(repo, nb.id, cid) == "rejected"


def test_write_clusters_is_per_type_isolated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [{"canonical_id": "K-a", "member_object_id": "o1",
                                 "canonical_name": "A"}], object_type="concept")
    repo.write_clusters(nb.id, [{"canonical_id": "KL-b", "member_object_id": "o2",
                                 "canonical_name": "B"}], object_type="claim")
    cm = repo.cluster_map(nb.id)
    assert cm.get("o1") == "K-a" and cm.get("o2") == "KL-b"   # both persist
    # rewriting concept clusters must NOT delete claim clusters
    repo.write_clusters(nb.id, [{"canonical_id": "K-a2", "member_object_id": "o1b",
                                 "canonical_name": "A2"}], object_type="concept")
    cm2 = repo.cluster_map(nb.id)
    assert "o2" in cm2 and cm2.get("o1") is None and cm2.get("o1b") == "K-a2"


def test_review_pending_merges_fail_open_on_bad_llm_json(repo):
    """A categorical confidence ("high") from the LLM must not 500 this endpoint.

    The route (routes.py) only catches KeyError; review_merge_candidates must be
    total so review_pending_merges returns a summary instead of raising ValueError.
    """
    from app.services.sqlite_repository import _now

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'pending', ?, ?)",
            ("cand-1", nb.id, "K-vco", "K-voltage controlled oscillator", 0.93, now, now),
        )

    class _BadConfLLM:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            return json.dumps({"decisions": [
                {"candidate_id": "cand-1", "decision": "merge",
                 "canonical_name": "vco", "confidence": "high", "rationale": "r"},
            ]})

    repo.llm_client = _BadConfLLM()  # inject via setter (system default LLM)
    summary = repo.review_pending_merges(nb.id)  # must NOT raise / 500
    assert isinstance(summary, dict)
    # "high" confidence coerces to 0.0 -> below thresholds -> counted as unsure, not confirmed.
    assert summary["confirmed"] == 0
    assert summary["reviewed"] == 1
