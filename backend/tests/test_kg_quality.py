import pytest
from app.services.retrieval import _payload_text
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


def test_payload_text_excludes_section_path():
    # name 干净,section_path 是纯定位元数据,不该进检索文本
    t = _payload_text({"name": "Mixtral", "section_path": "3 > 3.1"})
    assert t == "Mixtral"
    assert ">" not in t


def test_payload_text_keeps_other_fields():
    t = _payload_text({"name": "KV cache", "steps": ["a", "b"]})
    assert "KV cache" in t and "a" in t and "b" in t


def test_fold_by_canonical_keeps_highest_per_canonical():
    from app.services.retrieval import fold_by_canonical, RetrievedKnowledge
    a = RetrievedKnowledge(object_id="o1", object_type="concept", payload={}, score=0.9, relevance=0.9)
    b = RetrievedKnowledge(object_id="o2", object_type="concept", payload={}, score=0.5, relevance=0.5)
    c = RetrievedKnowledge(object_id="o3", object_type="concept", payload={}, score=0.4, relevance=0.4)
    cmap = {"o1": "K", "o2": "K", "o3": "other"}        # o1,o2 同 canonical
    out = fold_by_canonical([a, b, c], cmap)            # 输入已按 score 降序
    assert [h.object_id for h in out] == ["o1", "o3"]   # o2(同 K 但更低)被折掉


def test_fold_by_canonical_unmapped_passthrough():
    from app.services.retrieval import fold_by_canonical, RetrievedKnowledge
    a = RetrievedKnowledge(object_id="o1", object_type="concept", payload={}, score=0.9, relevance=0.9)
    b = RetrievedKnowledge(object_id="o2", object_type="concept", payload={}, score=0.5, relevance=0.5)
    out = fold_by_canonical([a, b], {})                 # 无映射 → 按自身 id,不折
    assert [h.object_id for h in out] == ["o1", "o2"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_retrieve_scored_fold_flag(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 两次 store_kg 各写一个同名概念 → 不同批,canonicalize 不合 → 2 个对象
    for _ in range(2):
        repo.store_kg(nb.id, None,
            [{"local_id": "k", "object_type": "concept", "payload": {"name": "KV cache"}, "evidence": []}], [])
    with repo._connect() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall()]
    assert len(ids) == 2
    monkeypatch.setattr(repo, "cluster_map", lambda n: {ids[0]: "K", ids[1]: "K"})
    monkeypatch.setattr(repo.settings, "kg_canonical_fold_enabled", False)
    off = repo._retrieve_scored(nb.id, "KV cache")
    monkeypatch.setattr(repo.settings, "kg_canonical_fold_enabled", True)
    on = repo._retrieve_scored(nb.id, "KV cache")
    assert len([h for h in off if h.object_id in ids]) == 2     # 关:两碎节点都在
    assert len([h for h in on if h.object_id in ids]) == 1      # 开:折成一个


def test_score_relations_about_downweight_rank_only():
    from app.services.retrieval import score_relations
    rels = [
        {"id": "r1", "source_object_id": "s", "target_object_id": "t", "edge_type": "about", "text": "cascode output resistance"},
        {"id": "r2", "source_object_id": "s", "target_object_id": "t", "edge_type": "supports", "text": "cascode output resistance"},
    ]
    # 不降权:关键词相同 → relevance 相同
    base = {h.relation_id: h for h in score_relations("cascode output resistance", rels)}
    assert abs(base["r1"].relevance - base["r2"].relevance) < 1e-9
    # 降权:about 的 score(排序用)被压低,但 relevance(tau 用)不变
    dw = {h.relation_id: h for h in score_relations("cascode output resistance", rels, downweight_edges=True)}
    assert abs(dw["r1"].relevance - base["r1"].relevance) < 1e-9    # relevance 不动
    assert dw["r1"].score < dw["r2"].score                         # about 排序被压


import ast, pathlib


def test_offline_clis_parse():
    for f in ("reembed_kg.py", "recluster_kg.py"):
        p = pathlib.Path("app/scripts") / f
        ast.parse(p.read_text(encoding="utf-8"))
