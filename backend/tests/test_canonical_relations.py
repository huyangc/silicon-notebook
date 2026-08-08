
import json

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
    """两源:s1/s2 各有 A--kind_of-->B 的关系(A/B 同名跨源,会折叠到同 canonical);
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
            {"source_local_id": a, "target_local_id": b, "edge_type": "kind_of", "evidence": []},
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
    assert r["edge_type"] == "kind_of"
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
        {"source_local_id": "X", "target_local_id": "Y", "edge_type": "kind_of", "evidence": []},
        {"source_local_id": "Y", "target_local_id": "X", "edge_type": "kind_of", "evidence": []},
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
    # 表里存 K-cascode --kind_of--> K-gain;kg_neighbors 把边统一画成
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


def _ids_by_name(repo, nb_id, name):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
            (nb_id,)).fetchall()
    return [r["id"] for r in rows if json.loads(r["payload"])["name"] == name]


def test_relation_support_counts_matches_single_triple_implementation(repo):
    """T1(B1 有界化,批 1)差分钉:批量 relation_support_counts() 必须与保留的
    单条 relation_support_count() 在同一夹具上逐 triple 相等——命中(两个不同
    来源折到同一 canonical 边,support_count=2)→ source_count,canonical 折叠
    与定点查询双双 miss 的陌生 id → 1(MUT-1/MUT-2 的反向验证目标)。"""
    nb = _mk_nb_with_relations(repo)
    cascode_ids = _ids_by_name(repo, nb.id, "cascode")
    gain_ids = _ids_by_name(repo, nb.id, "gain")
    assert len(cascode_ids) == 2 and len(gain_ids) == 2   # A1/A2, B1/B2

    graph = repo.retrieval.graph
    triples = [
        (cascode_ids[0], "kind_of", gain_ids[0]),
        (cascode_ids[1], "kind_of", gain_ids[1]),
        ("no-such-object-src", "kind_of", "no-such-object-tgt"),
    ]

    batched = graph.relation_support_counts(nb.id, triples)
    for triple in triples:
        expected = graph.relation_support_count(nb.id, *triple)
        assert batched[triple] == expected, (triple, batched[triple], expected)
    # Both real triples fold to the same canonical edge (support_count=2 per
    # test_rebuild_aggregates_cross_source_support); the unknown triple must
    # default to 1, matching the single-triple implementation's `hit[1] if hit
    # else 1`.
    assert batched[triples[0]] == 2
    assert batched[triples[1]] == 2
    assert batched[triples[2]] == 1

    # MUT-1 pin: support_count and source_count coincide (both 2) in the
    # fixture above, so a mutation that reads support_count instead of
    # source_count would slip past the assertions so far. Force them apart
    # directly in canonical_relations and require the DISTINCT-source count.
    # Also bump canonical_rel_seq so the single-triple implementation's cached
    # `_edge_support_map` (keyed on that seq) sees the new row rather than
    # replaying the value it already cached earlier in this test.
    with repo._write() as db:
        db.execute(
            "UPDATE canonical_relations SET support_count=99, source_count=7 "
            "WHERE notebook_id=? AND canonical_src='K-cascode' AND canonical_tgt='K-gain'",
            (nb.id,),
        )
        db.execute(
            "UPDATE unified_kg_state SET canonical_rel_seq=canonical_rel_seq+1 "
            "WHERE notebook_id=?", (nb.id,),
        )
    assert graph.relation_support_counts(nb.id, [triples[0]])[triples[0]] == 7
    assert graph.relation_support_count(nb.id, *triples[0]) == 7


def test_relation_support_counts_falls_back_to_raw_id_when_not_clustered(repo):
    """T1 差分钉(MUT-3):canonical 折叠 miss(该 id 从未进入 concept_clusters,
    比如它就是自己的 canonical 形态)时必须退回原 id 本身去做定点查询——与现
    `clusters.get(source_id, source_id)` 逐字同语义。这个 notebook 从未
    rebuild_unified_kg,concept_clusters 里没有任何行,所以两个 id 的折叠必然
    全部 miss;canonical_relations 直接用它们的原始 id 造一行,只有回退成立
    时才会命中。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _mk_src(repo, nb.id, "s1")
    repo.store_kg(nb.id, "s1", [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "solo-x", "section_path": "1"}, "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "solo-y", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "X", "target_local_id": "Y", "edge_type": "kind_of", "evidence": []},
    ])
    x_id = _ids_by_name(repo, nb.id, "solo-x")[0]
    y_id = _ids_by_name(repo, nb.id, "solo-y")[0]

    with repo._write() as db:
        db.execute(
            "INSERT INTO canonical_relations "
            "(notebook_id, canonical_src, edge_type, canonical_tgt, "
            " support_count, source_count, sample_relation_ids, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb.id, x_id, "kind_of", y_id, 3, 3, "[]", "2024-01-01T00:00:00"),
        )

    graph = repo.retrieval.graph
    triple = (x_id, "kind_of", y_id)
    assert graph.relation_support_counts(nb.id, [triple])[triple] == 3
    assert graph.relation_support_count(nb.id, *triple) == 3


def test_relation_support_counts_groups_correctly_across_two_notebooks(repo):
    """T1:evidence_context 按 row["notebook_id"] 分组、每组一次批量查询——两个
    notebook 各自的 canonical_relations 互不干扰,不会把 A 库的 support 数错配
    给 B 库同名 canonical 边。"""
    nb1 = _mk_nb_with_relations(repo)   # support=2 (两源)
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    _mk_src(repo, nb2.id, "nb2-s1")
    repo.store_kg(nb2.id, "nb2-s1", [
        {"local_id": "A", "object_type": "concept",
         "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
        {"local_id": "B", "object_type": "concept",
         "payload": {"name": "gain", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "kind_of", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb2.id)   # single source -> support=1

    graph = repo.retrieval.graph
    nb1_triple = (_ids_by_name(repo, nb1.id, "cascode")[0], "kind_of",
                  _ids_by_name(repo, nb1.id, "gain")[0])
    nb2_triple = (_ids_by_name(repo, nb2.id, "cascode")[0], "kind_of",
                  _ids_by_name(repo, nb2.id, "gain")[0])

    assert graph.relation_support_counts(nb1.id, [nb1_triple])[nb1_triple] == 2
    assert graph.relation_support_counts(nb2.id, [nb2_triple])[nb2_triple] == 1


def test_answer_context_folds_boundedly_not_via_full_cluster_map(repo, monkeypatch):
    """T3(B2 有界化,批 1)守卫,真实 SQLite 栈:knowledge_context 装配路径绝不
    调用整表 cluster_map_rows(),且传给 cluster_fold_rows() 的 ids 恰为本次
    命中集合(不是全库、不是空)——镜像
    test_annotate_edge_support_folds_only_edge_endpoints 的 spy 手法
    (MUT-4/MUT-5 的反向验证目标)。刻意不建 A→B 的关系:两个 hit 之间若有边,
    relations 段会经 T1 的 relation_support_counts() 触发它*自己*的一次
    (独立于本函数要测的 T3 装配折叠)cluster_fold_rows 调用,那次调用会拿真实
    的关系端点 id 掩盖掉 T3 折叠被 MUT-5 打成空 ids 这件事——曾经在评审时真
    的踩过这个假阳性,留字面注释存档。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim Alpha", "section_path": "1"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim Beta", "section_path": "1"}, "evidence": []},
    ], [])
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,)).fetchall()
    ids_by_name = {json.loads(r["payload"])["name"]: r["id"] for r in rows}
    a_id, b_id = ids_by_name["Claim Alpha"], ids_by_name["Claim Beta"]

    graph = repo.retrieval.graph
    fold_calls: list = []
    real_fold_rows = graph.unified_kg.cluster_fold_rows

    def spy_fold_rows(db, notebook_id, batch):
        fold_calls.append((notebook_id, list(batch)))
        return real_fold_rows(db, notebook_id, batch)

    def _forbidden_cluster_map_rows(*args, **kwargs):
        raise AssertionError(
            "knowledge_context must not load the full cluster_map_rows table")

    monkeypatch.setattr(graph.unified_kg, "cluster_fold_rows", spy_fold_rows)
    monkeypatch.setattr(graph.unified_kg, "cluster_map_rows", _forbidden_cluster_map_rows)

    from app.services.retrieval import RetrievedKnowledge

    hits = [
        RetrievedKnowledge(object_id=a_id, object_type="claim",
                            payload={"name": "Claim Alpha"}, evidence=[]),
        RetrievedKnowledge(object_id=b_id, object_type="claim",
                            payload={"name": "Claim Beta"}, evidence=[]),
    ]
    repo._answer_context(nb.id, hits)

    assert fold_calls, "cluster_fold_rows must be called at least once"
    for _notebook_id, batch in fold_calls:
        assert set(batch) == {a_id, b_id}, (
            f"cluster_fold_rows must receive exactly the hit id set, got {batch}")


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
