
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _table_cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _mk_src(repo, nb_id, sid):
    """插一行最小 sources 满足 knowledge_relations.source_id 的 FK(_connect 常开
    PRAGMA foreign_keys=ON,brief helper 直接传 's1'/'s2' 作 source_id + 关系会 FK
    失败;补建真实源行 — 适配已在报告说明)。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, created_at, updated_at) "
            "VALUES (?, ?, ?, 'markdown', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (sid, nb_id, sid))


def _mk_nb_with_relations(repo):
    """两源:s1/s2 各有 A--supports-->B 的关系(A/B 同名跨源,会折叠到同 canonical);
    另有 s1 内 rejected 边与自环候选。返回 (nb_id, ids)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for src, (a, b) in {"s1": ("A1", "B1"), "s2": ("A2", "B2")}.items():
        _mk_src(repo, nb.id, src)
        repo.store_kg(nb.id, src, [
            {"local_id": a, "object_type": "concept",
             "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
            {"local_id": b, "object_type": "concept",
             "payload": {"name": "gain", "section_path": "1"}, "evidence": []},
        ], [
            {"source_local_id": a, "target_local_id": b, "edge_type": "supports", "evidence": []},
        ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def _canon_rows(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT * FROM canonical_relations WHERE notebook_id=?", (nb_id,)).fetchall()


def test_rebuild_aggregates_cross_source_support(repo):
    nb = _mk_nb_with_relations(repo)
    rows = _canon_rows(repo, nb.id)
    assert len(rows) == 1                      # 两源同一逻辑边折叠成一行
    r = rows[0]
    assert r["canonical_src"] == "K-cascode" and r["canonical_tgt"] == "K-gain"
    assert r["edge_type"] == "supports"
    assert r["support_count"] == 2 and r["source_count"] == 2
    import json as _j
    assert 1 <= len(_j.loads(r["sample_relation_ids"])) <= 5


def test_rejected_edges_excluded(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='rejected' "
                   "WHERE notebook_id=? AND source_id='s2'", (nb.id,))
    repo.rebuild_canonical_relations(nb.id, force=True)
    r = _canon_rows(repo, nb.id)[0]
    assert r["support_count"] == 1 and r["source_count"] == 1


def test_direction_preserved(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _mk_src(repo, nb.id, "s1")
    repo.store_kg(nb.id, "s1", [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "a", "section_path": "1"}, "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "b", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "X", "target_local_id": "Y", "edge_type": "supports", "evidence": []},
        {"source_local_id": "Y", "target_local_id": "X", "edge_type": "supports", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    assert len(_canon_rows(repo, nb.id)) == 2   # A→B 与 B→A 不合并


def test_seq_gate_skips_then_force_recomputes(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._connect() as db:
        seq0 = db.execute("SELECT canonical_rel_seq FROM unified_kg_state WHERE notebook_id=?",
                          (nb.id,)).fetchone()["canonical_rel_seq"]
    assert seq0 >= 0                           # rebuild 后闸已写
    assert repo.rebuild_canonical_relations(nb.id) >= 0   # 未变 → 跳过不炸
    assert repo.rebuild_canonical_relations(nb.id, force=True) == 1


def test_unified_graph_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    sup = [e for e in g["edges"] if e.get("source_count")]
    assert sup and sup[0]["support_count"] == 2 and sup[0]["source_count"] == 2


def test_neighbors_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    nid = next(n["id"] for n in g["nodes"])
    nbres = repo.kg_neighbors(nb.id, nid)
    assert any(e.get("source_count") == 2 for e in nbres["edges"])


def test_neighbors_incoming_edge_carries_support(repo):
    # 表里存 K-cascode --supports--> K-gain;kg_neighbors 把边统一画成
    # 「查询节点→邻居」,查 TARGET 侧(K-gain)时展示方向与存储方向相反,
    # 注解必须经对称回退仍命中(否则入边永远丢 support/source_count)。
    nb = _mk_nb_with_relations(repo)
    res = repo.kg_neighbors(nb.id, "K-gain")
    assert any(e.get("source_count") == 2 for e in res["edges"])


def test_empty_table_leaves_edges_bare(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (nb.id,))
        db.execute("UPDATE unified_kg_state SET canonical_rel_seq=-1 WHERE notebook_id=?", (nb.id,))
    g = repo.unified_graph(nb.id, level="object")
    assert all("support_count" not in e for e in g["edges"])


def test_annotation_does_not_stick_to_unified_cache(repo):
    # _annotate_edge_support 必须拷贝而非就地改边 dict——full-graph 路径下这些
    # dict 与 _unified_cache 共享引用,就地写字段会把注解粘进缓存(违反缓存
    # 应保持不含注解的设计,导致后续读到滞后的旧计数)。
    nb = _mk_nb_with_relations(repo)
    g1 = repo.unified_graph(nb.id, level="object")
    assert any(e.get("source_count") for e in g1["edges"])
    cached = repo._unified_cache.get((nb.id, "object"))
    assert cached is not None
    assert all("support_count" not in e for e in cached["edges"])


def test_annotate_edge_support_folds_only_edge_endpoints(repo, monkeypatch):
    """OOM audit P1-5: annotating edges must fold endpoints via the BOUNDED
    cluster_fold_rows (≤2·#edges ids), never the full 8M-entry cluster_map (which
    this ran on every KG-view / kanban open). Reverting to the full map (no
    cluster_fold_rows call) fails here."""
    nb = _mk_nb_with_relations(repo)   # populates canonical_relations → support non-empty
    kq = repo._runtime.knowledge_query
    fold_ids: list = []
    real_fold = kq.unified_kg.cluster_fold_rows

    def spy_fold(db, notebook_id, ids):
        fold_ids.extend(ids)
        return real_fold(db, notebook_id, ids)

    monkeypatch.setattr(kq.unified_kg, "cluster_fold_rows", spy_fold)

    edges = [{"source_object_id": "o-src", "edge_type": "supports",
              "target_object_id": "o-tgt"}]
    kq.annotate_edge_support(nb.id, edges)

    # bounded fold over EXACTLY the edges' endpoints — not the whole cluster_map
    assert set(fold_ids) == {"o-src", "o-tgt"}
