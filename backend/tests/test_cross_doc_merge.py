import json
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg_merge import seed_claim, seed_formula, seed_procedure
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


def test_seed_normalizers():
    assert seed_claim("Cascode RAISES output resistance.") == seed_claim("cascode raises output resistance")
    assert seed_formula("g_m / g_ds") == seed_formula("g_m/g_ds")     # whitespace-insensitive
    assert seed_procedure({"name": "Foundation Flow",
                           "payload": {"steps": [{"name": "B"}, {"name": "a"}]}}) == \
           seed_procedure({"name": "foundation flow",
                           "payload": {"steps": [{"name": "A"}, {"name": "b"}]}})  # order-insensitive


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _ids_by_name(repo, nb_id, object_type):
    with repo._connect() as db:
        rows = db.execute("SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND object_type=?",
                          (nb_id, object_type)).fetchall()
    return {json.loads(r["payload"])["name"]: r["id"] for r in rows}


def test_rebuild_merges_same_claim_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # two sources, same claim text (different casing/punct) -> should merge
    repo.store_kg(nb.id, "s1", [{"local_id": "C1", "object_type": "claim",
        "payload": {"name": "Cascode raises output resistance.", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "C2", "object_type": "claim",
        "payload": {"name": "cascode raises output resistance", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    ids = _ids_by_name(repo, nb.id, "claim")
    # both claim ids map to the same canonical_id (KL- prefix)
    vals = {cm.get(i) for i in ids.values()}
    assert len(ids) == 2 and len(vals) == 1 and next(iter(vals)).startswith("KL-")


def test_rebuild_keeps_concept_behavior(repo):
    # concept clustering must still work (regression)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "cascode", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "Cascode", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    ids = _ids_by_name(repo, nb.id, "concept")
    vals = {cm.get(i) for i in ids.values()}
    assert len(vals) == 1 and next(iter(vals)).startswith("K-")


def test_answer_context_dedups_merged_claims(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "C1", "object_type": "claim",
        "payload": {"name": "cascode raises output resistance", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "C2", "object_type": "claim",
        "payload": {"name": "Cascode raises output resistance.", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    ids = _ids_by_name(repo, nb.id, "claim")
    hits = [RetrievedKnowledge(object_id=i, object_type="claim",
                               payload={"name": "cascode raises output resistance"}, evidence=[])
            for i in ids.values()]
    block, id_map = repo._answer_context(nb.id, hits)
    assert len(id_map) == 1   # the two merged claims collapse to one context line


def test_unified_graph_object_level_no_dangling_after_merge(repo):
    # regression: non-concept merge must not create dangling edges at level=object
    # (the level the frontend requests). Non-concept nodes stay raw; concept->claim
    # edge endpoints must still resolve to real nodes.
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # source_id=None (no real source rows here): the relation FK requires a valid
    # sources(id), and the cross-doc merge is by content seed, not source_id.
    repo.store_kg(nb.id, None, [
        {"local_id": "K1", "object_type": "concept",
         "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "cascode raises output resistance", "section_path": "1"}, "evidence": []},
    ], [{"source_local_id": "K1", "target_local_id": "C1", "edge_type": "about", "evidence": []}])
    repo.store_kg(nb.id, None, [
        {"local_id": "C2", "object_type": "claim",
         "payload": {"name": "Cascode raises output resistance.", "section_path": "2"}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="object")
    node_ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source_object_id"] in node_ids
        assert e["target_object_id"] in node_ids   # no dangling refs after merge


# --- Unicode/CJK cross-doc clustering (P0 fix) --------------------------------

def _all_concept_ids(repo, nb_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
            (nb_id,)).fetchall()
    return [r["id"] for r in rows]


def test_rebuild_merges_same_cjk_concept_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "1"}, "evidence": []}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "2"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    ids = _all_concept_ids(repo, nb.id)
    vals = {cm.get(i) for i in ids}
    assert len(ids) == 2 and vals == {"K-铸币平价"}


def test_rebuild_no_empty_seed_mega_cluster(repo):
    # 互不相关的中文名+纯符号名必须各自独立成簇;旧行为全部塌缩进 canonical "K-"
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for i, nm in enumerate(["双重汇率制", "组织策略", "→"]):
        repo.store_kg(nb.id, f"s{i}", [{"local_id": f"C{i}", "object_type": "concept",
            "payload": {"name": nm, "section_path": "1"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cm = repo.cluster_map(nb.id)
    vals = [cm.get(i) for i in _all_concept_ids(repo, nb.id)]
    assert len(set(vals)) == 3
    assert "K-" not in vals


def test_incremental_fuse_cjk_joins_existing_cluster(repo):
    # Tier1 名种子 append:新源中文 concept 落进已有簇(不必等全量 rebuild)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [{"local_id": "X", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "1"}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.store_kg(nb.id, "s2", [{"local_id": "Y", "object_type": "concept",
        "payload": {"name": "铸币平价", "section_path": "2"}, "evidence": []}], [])
    repo.incremental_fuse_source(nb.id, "s2")
    cm = repo.cluster_map(nb.id)
    vals = {cm.get(i) for i in _all_concept_ids(repo, nb.id)}
    assert vals == {"K-铸币平价"}
