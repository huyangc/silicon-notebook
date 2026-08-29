
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


def test_relation_support_lookup_folds_boundedly_and_never_scans_full_support_table(
    repo, monkeypatch,
):
    """T1(B1 有界化,批 1)第二夹具 —— 与上面
    ``test_answer_context_folds_boundedly_not_via_full_cluster_map`` 的「无关系」
    夹具互补(那条刻意不建 A→B 的关系,只钉 T3 自己的折叠;这里刻意要建一条
    真实关系边,让 relations 段真正跑到 relation_support_counts() →
    relation_support_rows(),T1 自己的代码路径才会被真正触发)。回答装配全程
    绝不允许调用整表 cluster_map_rows() 或 edge_support_rows()(旧逐条实现
    relation_support_count 的两个全表扫描点),且 relations 行必须照常渲染、带
    ×N源 support 后缀(support_count=2,两源折叠,见
    test_rebuild_aggregates_cross_source_support)。

    这是两条移动变异(评审第 2 轮)的反向验证目标 —— 把它们中任意一条实施到
    代码上,这条测试都必须报红:
    - MUT-C: relation_support_counts 内部把有界 ``_candidate_cluster_map``
      换成整表 ``cluster_map(notebook_id)`` → 会触发 cluster_map_rows()。
    - MUT-C2: 整个 relation_support_counts 换回逐条循环调用旧的
      relation_support_count(内部经 _edge_support_map() 调
      edge_support_rows()) → 会触发 edge_support_rows()。

    ``_mk_nb_with_relations`` 内部的 ``rebuild_unified_kg`` 会把 viz 工件构建
    路径(``scale_index_builder._derive_object_graph_lite``)顺带经
    ``cluster_map(notebook_id)`` 预热进程内 ``:clustermap`` 缓存 —— 若不显式
    失效它,MUT-C 会命中这份"恰好还新鲜"的缓存值、完全不再调
    ``cluster_map_rows()``,让这条守卫失去意义(真机确认过这个假阴性)。T1 自
    己要折叠的 ``_candidate_cluster_map``/``cluster_fold_rows`` 路径本身不经
    过这份缓存,失效它不影响正确实现的行为。
    """
    nb = _mk_nb_with_relations(repo)   # A--kind_of-->B,两源折叠,support_count=2
    cascode_id = _ids_by_name(repo, nb.id, "cascode")[0]
    gain_id = _ids_by_name(repo, nb.id, "gain")[0]

    graph = repo.retrieval.graph
    repo._vector_cache.invalidate(f"{nb.id}:clustermap")

    def _forbidden_cluster_map_rows(*args, **kwargs):
        raise AssertionError(
            "relation support lookup must not load the full cluster_map_rows table")

    def _forbidden_edge_support_rows(*args, **kwargs):
        raise AssertionError(
            "relation support lookup must not load the full edge_support_rows table")

    monkeypatch.setattr(graph.unified_kg, "cluster_map_rows", _forbidden_cluster_map_rows)
    monkeypatch.setattr(graph.unified_kg, "edge_support_rows", _forbidden_edge_support_rows)

    from app.services.retrieval import RetrievedKnowledge

    hits = [
        RetrievedKnowledge(object_id=cascode_id, object_type="concept",
                            payload={"name": "cascode"}, evidence=[]),
        RetrievedKnowledge(object_id=gain_id, object_type="concept",
                            payload={"name": "gain"}, evidence=[]),
    ]
    block, _id_map = repo._answer_context(nb.id, hits)

    assert "relations:" in block
    assert "kind_of" in block
    assert "(×2源)" in block, (
        f"support_count=2 must render the ×N源 suffix, got: {block!r}")


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


# ─────────────────────── R2-1(热路径修复批 2 / 审计 KG-1)等价 oracle ──
def _legacy_annotate_edge_support(kq, notebook_id, edges):
    """``annotate_edge_support`` 改造**前**的正文,原样抄成 oracle。

    唯一差别在支撑数的取法:这里仍走 ``retrieval.edge_support_map()`` 的整表
    dict(生产 8.35M 行),新实现改成按本次边集的两个朝向做 ``relation_support_rows``
    定点查询。折叠、键构造、正/反向偏好、未命中原样返回,三处逐字保持相同,
    所以两者在同一数据上必须逐字段相等。
    """
    support = kq.retrieval().edge_support_map(notebook_id)
    if not support:
        return edges
    ids = sorted({
        oid
        for edge in edges
        for oid in (edge["source_object_id"], edge["target_object_id"])
    })
    clusters: dict = {}
    if ids:
        with kq.database.connect() as db:
            for start in range(0, len(ids), 900):
                for row in kq.unified_kg.cluster_fold_rows(
                    db, notebook_id, ids[start:start + 900]
                ):
                    clusters[row["member_object_id"]] = row["canonical_id"]
    result = []
    for edge in edges:
        key = (
            clusters.get(edge["source_object_id"], edge["source_object_id"]),
            edge["edge_type"],
            clusters.get(edge["target_object_id"], edge["target_object_id"]),
        )
        hit = support.get(key) or support.get((key[2], key[1], key[0]))
        result.append(
            {**edge, "support_count": hit[0], "source_count": hit[1]}
            if hit else edge
        )
    return result


def _annotate_oracle_fixture(repo):
    """一个把 annotate 的每条分支都盖到的库 + 边集。

    - K-cascode --kind_of--> K-gain:两源折叠,support=2/source=2(经簇折叠命中);
    - K-solo --cites--> K-other:直接写进 canonical_relations 的**未聚簇**行,
      support=5/source=3(两个计数**故意不相等**,取错列会立刻可见),用来盖
      「折叠 miss → 回退原 id → 定点命中」这条;
    - K-alpha --refines--> K-beta(support=7/source=4):边集里**只**放它的反向
      朝向。这一条是对称回退的真正钉子 —— 前两条边互为正反,反向那条即便实现
      忘了查反向朝向,也会靠正向那条边贡献的同一个 key 蒙混过关(第一轮变异
      自检真的这样漏过);只有一条「反向存在、正向不在边集里」的边能钉住它。
    - 反向朝向的边(kg_neighbors 把入边画成「查询节点→邻居」时的形状);
    - 完全没有 support 的边、自环边:必须原样返回、不带任何新字段。
    """
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        for canonical_src, edge_type, canonical_tgt, support, source in (
            ("K-solo", "cites", "K-other", 5, 3),
            ("K-alpha", "refines", "K-beta", 7, 4),
            # 互为反向、取值不同的一对(评审 P2-2):边集里只放 X→Y,断言必须
            # 拿到正向那一行 (9,9)。少了这一对,「正反向偏好反转」的变异会
            # 全绿——两边都命中、值又相等,断言看不出区别。
            ("K-x", "leads_to", "K-y", 9, 9),
            ("K-y", "leads_to", "K-x", 1, 1),
        ):
            db.execute(
                "INSERT INTO canonical_relations "
                "(notebook_id, canonical_src, edge_type, canonical_tgt, "
                " support_count, source_count, sample_relation_ids, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (nb.id, canonical_src, edge_type, canonical_tgt, support, source,
                 "[]", "2024-01-01T00:00:00"),
            )
        db.execute(
            "UPDATE unified_kg_state SET canonical_rel_seq=canonical_rel_seq+1 "
            "WHERE notebook_id=?", (nb.id,),
        )
    cascode_id = _ids_by_name(repo, nb.id, "cascode")[0]
    gain_id = _ids_by_name(repo, nb.id, "gain")[0]
    edges = [
        # 正向:两个端点都要经簇折叠才命中。
        {"source_object_id": cascode_id, "edge_type": "kind_of",
         "target_object_id": gain_id},
        # 反向:正向 key 查不到,必须经对称回退命中同一行。
        {"source_object_id": gain_id, "edge_type": "kind_of",
         "target_object_id": cascode_id},
        # 未聚簇 id:折叠 miss → 回退原 id → 定点命中(support≠source)。
        {"source_object_id": "K-solo", "edge_type": "cites",
         "target_object_id": "K-other"},
        # 只有反向朝向进边集:正向 key 不由任何别的边贡献,必须自己查反向。
        {"source_object_id": "K-beta", "edge_type": "refines",
         "target_object_id": "K-alpha"},
        # 正反向**都存在、取值不同**:必须优先正向 (9,9),不是反向 (1,1)。
        {"source_object_id": "K-x", "edge_type": "leads_to",
         "target_object_id": "K-y"},
        # 边类型不同:同一对端点但 canonical_relations 里没有这条 → 不注解。
        {"source_object_id": cascode_id, "edge_type": "supports",
         "target_object_id": gain_id},
        # 两端都陌生 → 不注解。
        {"source_object_id": "K-nobody", "edge_type": "relates",
         "target_object_id": "K-nowhere"},
        # 自环 → 正反向是同一个 key,不得因此重复注解或崩。
        {"source_object_id": cascode_id, "edge_type": "kind_of",
         "target_object_id": cascode_id},
    ]
    return nb, edges


def test_annotate_edge_support_matches_full_map_oracle(repo):
    """R2-1 等价 oracle:定点查询版与整表 map 版在同一数据上逐字段相等。

    整表版跑在前面(它会把 ``:edge_support`` 整表结果暖进 VectorCache),新版
    随后跑;两者对同一条边必须给出同一个 dict —— 键集合、``support_count``、
    ``source_count`` 全部逐字相同,未命中的边则两侧都不带这两个字段。
    """
    nb, edges = _annotate_oracle_fixture(repo)
    kq = repo._runtime.knowledge_query

    expected = _legacy_annotate_edge_support(kq, nb.id, [dict(e) for e in edges])
    actual = kq.annotate_edge_support(nb.id, [dict(e) for e in edges])

    assert actual == expected, f"\nnew: {actual}\nold: {expected}"
    # 夹具真的覆盖到了「命中」与「未命中」两侧(否则这条 oracle 会在两边都
    # 什么都不注解的空断言上通过)。
    annotated = [e for e in actual if "support_count" in e]
    assert [(e["support_count"], e["source_count"]) for e in annotated] == [
        (2, 2), (2, 2), (5, 3), (7, 4), (9, 9),
    ]
    assert len(actual) - len(annotated) == 3


def test_annotate_edge_support_matches_oracle_on_notebook_without_canonical_rows(repo):
    """同一 oracle,但库里一条 canonical 关系都没有 —— 旧实现在这里靠
    ``if not support: return edges`` 早退,新实现靠「定点查询零行」。两者必须
    仍然逐字相等(所有边原样、不带注解字段)。"""
    nb, edges = _annotate_oracle_fixture(repo)
    with repo._write() as db:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (nb.id,))
        db.execute(
            "UPDATE unified_kg_state SET canonical_rel_seq=canonical_rel_seq+1 "
            "WHERE notebook_id=?", (nb.id,),
        )
    kq = repo._runtime.knowledge_query

    expected = _legacy_annotate_edge_support(kq, nb.id, [dict(e) for e in edges])
    actual = kq.annotate_edge_support(nb.id, [dict(e) for e in edges])

    assert actual == expected
    assert all("support_count" not in edge for edge in actual)


def test_annotate_edge_support_never_scans_the_full_support_table(repo, monkeypatch):
    """R2-1 的方向钉:标注绝不允许再触碰整表 ``edge_support_rows``
    (canonical_relations 的 per-notebook 全表扫,生产 8.35M 行 ≈3.6GB)。

    **变异锚点**:把实现改回 ``retrieval.edge_support_map(notebook_id)``
    → 这条立刻报红。上面的等价 oracle 按定义对这条变异是绿的(它就是 oracle
    自己),所以「少扫」这一半必须由本条守卫钉住。"""
    nb, edges = _annotate_oracle_fixture(repo)
    kq = repo._runtime.knowledge_query
    # 上面的夹具没走过 annotate,但同一进程里别的用例可能已把整表结果暖进
    # VectorCache;显式失效,免得变异版靠缓存命中绕开这条守卫(与
    # test_relation_support_lookup_... 同款的假阴性)。
    repo._vector_cache.invalidate(f"{nb.id}:edge_support")

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "annotate_edge_support must not load the full edge_support_rows table")

    monkeypatch.setattr(kq.unified_kg, "edge_support_rows", _forbidden)

    annotated = kq.annotate_edge_support(nb.id, [dict(e) for e in edges])
    assert [(e["support_count"], e["source_count"])
            for e in annotated if "support_count" in e] == [
        (2, 2), (2, 2), (5, 3), (7, 4), (9, 9)]


def test_annotate_edge_support_on_empty_edges_issues_no_query(repo, monkeypatch):
    """空响应必须零查询。旧实现把整表取值排在判空**之前**,所以一次空边的
    KG 视图也要付一次全表扫描(审计 KG-1 的第二半)。

    **变异锚点**:把 ``if not edges: return edges`` 早退挪到查询之后(或删掉)
    → 这条报红。"""
    nb, _edges = _annotate_oracle_fixture(repo)
    kq = repo._runtime.knowledge_query
    repo._vector_cache.invalidate(f"{nb.id}:edge_support")

    calls: list = []
    real_connect = kq.database.connect
    monkeypatch.setattr(
        kq.database, "connect",
        lambda *a, **k: (calls.append("connect"), real_connect(*a, **k))[1],
    )
    for name in ("edge_support_rows", "relation_support_rows", "cluster_fold_rows"):
        real = getattr(kq.unified_kg, name)
        monkeypatch.setattr(
            kq.unified_kg, name,
            (lambda _name, _real: (
                lambda *a, **k: (calls.append(_name), _real(*a, **k))[1]
            ))(name, real),
        )

    assert kq.annotate_edge_support(nb.id, []) == []
    assert calls == [], f"empty edge list must issue zero queries, got {calls}"


def test_relation_support_rows_returns_rows_in_ascending_canonical_order(repo):
    """P3(评审 E2):relation_support_rows 的行序不应该靠 SQLite 恰好保留了
    请求 triples 的字面顺序,或恰好保留了物理插入顺序——那两者都不是 SQL 标准
    保证的东西,行值 IN 不带 ORDER BY 时行序未指定。显式
    ``ORDER BY canonical_src, edge_type, canonical_tgt`` 是唯一按标准语义算
    「有保证」的写法,即使这里的调用方(把行拼进一个无序 dict)其实不消费顺序。

    夹具把插入顺序与查询顺序都反过来(先插字典序更大的 canonical_src、查询时
    triples 列表同样逆序传入),断言返回是升序。**如实记录一个变异验证的负
    结果**:在本仓库固定的 SQLite 3.51.0 上,删掉这条 ORDER BY 子句并不会让
    这条测试报红——row-value IN 在这个查询形状下被规划成
    ``LIST SUBQUERY``/``CREATE BLOOM FILTER`` 策略,经验上总是按索引(即
    canonical_src 升序)的自然顺序吐行,与去掉 ORDER BY 前后一致,已用更大规模
    (200k 行表、300 triples、随机插入与随机查询顺序)复现验证过、结论不随规模
    变化。ORDER BY 仍然保留,因为它是唯一按 SQL 语义有保证的写法(不同 SQLite
    版本/构建选项、不同数据分布都可能选择不同的执行计划),只是这条测试目前
    没能力钉住它被删除——评审要求"删 ORDER BY 变异必须报红"这一条因此如实
    报告为未达成,而不是伪造一个会通过的断言。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    triples = [
        ("K-zzz", "kind_of", "K-zzz-tgt"),
        ("K-mmm", "kind_of", "K-mmm-tgt"),
        ("K-aaa", "kind_of", "K-aaa-tgt"),
    ]
    with repo._write() as db:
        for canonical_src, edge_type, canonical_tgt in triples:
            db.execute(
                "INSERT INTO canonical_relations "
                "(notebook_id, canonical_src, edge_type, canonical_tgt, "
                " support_count, source_count, sample_relation_ids, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (nb.id, canonical_src, edge_type, canonical_tgt, 1, 1, "[]",
                 "2024-01-01T00:00:00"),
            )

    graph = repo.retrieval.graph
    with repo._connect() as db:
        rows = graph.unified_kg.relation_support_rows(
            db, nb.id, list(reversed(triples)),
        )
    assert [row["canonical_src"] for row in rows] == ["K-aaa", "K-mmm", "K-zzz"], (
        f"expected ascending canonical_src order, got: "
        f"{[r['canonical_src'] for r in rows]}")


def test_relation_support_counts_batches_large_triple_lists():
    """P3(评审 F4):批处理没有守卫等于没批处理——``relation_support_counts``
    必须把超过 ``_RELATION_SUPPORT_IN_CHUNK``(=300)的 triples 列表拆成多次
    ``relation_support_rows`` 调用,不能整批一次性塞进一条 SQL(见该常量处的
    悬崖注释:SQLite 表达式树上限、PostgreSQL 规划耗时二次增长/栈深度上限)。

    用一个不落地任何真实存储的假 ``unified_kg`` 直接测服务层的批处理循环本身
    ——不依赖某个后端的报错行为(真报错要么在很大的 N 才炸,要么两个后端悬崖
    位置不同),只记录每次调用收到的 triples 长度。301 个 triple 在 300/批下
    必须落在 >=2 次调用里;删掉分批循环(整批一次性传入)会让这条测试报红。"""
    from contextlib import nullcontext

    from app.services.graph_retrieval import GraphRetrievalService

    class _FakeDatabase:
        def connect(self):
            return nullcontext(object())

    class _FakeUnifiedKg:
        def __init__(self):
            self.batch_sizes: list[int] = []

        def cluster_fold_rows(self, db, notebook_id, ids):
            return {}   # identity fold: canonical == raw for every id

        def relation_support_rows(self, db, notebook_id, triples):
            self.batch_sizes.append(len(triples))
            return []

    graph = GraphRetrievalService.__new__(GraphRetrievalService)
    graph.database = _FakeDatabase()
    graph.unified_kg = _FakeUnifiedKg()

    triples = [(f"src-{i}", "kind_of", f"tgt-{i}") for i in range(301)]
    result = graph.relation_support_counts("nb", triples)

    assert len(result) == 301
    assert all(support == 1 for support in result.values())   # every triple misses (fake returns no rows)
    assert len(graph.unified_kg.batch_sizes) >= 2, (
        f"301 triples at a 300-per-batch chunk size must issue >=2 "
        f"relation_support_rows calls, got {graph.unified_kg.batch_sizes}")
    assert sum(graph.unified_kg.batch_sizes) == 301, (
        f"batches must partition (not duplicate or drop) the requested "
        f"triples, got sizes {graph.unified_kg.batch_sizes}")
    assert max(graph.unified_kg.batch_sizes) <= 300, (
        f"no single batch may exceed the 300-triple chunk size, got "
        f"{graph.unified_kg.batch_sizes}")


def test_relation_support_rows_issues_row_value_in_not_or_chain():
    """P3(评审 F5):行值 IN 是这条查询在生产规模下唯一被证实不退化成整表
    扫描的写法(见 ``relation_support_rows`` docstring 里的复现数据:OR 链在
    N≈30+ 分支、无 ANALYZE 时从「每分支一次 PK 索引 seek」退化成
    ``SEARCH ... USING COVERING INDEX (notebook_id=?)``,即整个 notebook
    分区扫描)。这个退化只在较大规模的表上才在 EXPLAIN QUERY PLAN 里可见——
    小夹具下 SQLite 的 MULTI-INDEX OR 优化同样会走 PK 索引,两种写法在几行
    数据的表上无法用 EXPLAIN 区分;为不引入大规模夹具拖慢/弱化这条守卫,这里
    直接钉住适配器发出的 SQL **文本形状**:必须是单条
    ``(canonical_src, edge_type, canonical_tgt) IN ((?,?,?),...)``,不能是
    ``(canonical_src=? AND edge_type=? AND canonical_tgt=?) OR (...)`` 的
    析取链——不需要真实连接,直接喂一个只记录调用参数的假 ``db``。把实现
    改回 OR 链会让这条测试报红。"""
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    class _Cursor:
        def fetchall(self):
            return []

    class _SqlCapture:
        def __init__(self):
            self.sql: str | None = None
            self.params: list | None = None

        def execute(self, sql, params):
            self.sql = sql
            self.params = params
            return _Cursor()

    capture = _SqlCapture()
    UnifiedKgStore.relation_support_rows(
        capture, "nb", [("K-a", "kind_of", "K-b"), ("K-c", "part_of", "K-d")],
    )

    assert capture.sql is not None, "relation_support_rows must call db.execute"
    assert "IN (" in capture.sql, (
        f"expected a row-value IN clause, got: {capture.sql}")
    assert " OR " not in capture.sql, (
        f"expected no OR-chain disjunction (the ~440x-slower form this "
        f"replaced), got: {capture.sql}")
