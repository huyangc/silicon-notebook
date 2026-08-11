"""批 4 等价性钉子：跨库组合图 splice 向量化 + 跨层桥 ANN 批量查询。

两处改动都声明「数值/顺序逐位等价」，所以这里的 oracle 就是**旧实现的拷贝**：

1. ``si.splice_csr`` vs 历史的 ``si.csr_to_edges`` + ``si.splice_active`` 组合
   （_scale_combined_graph 的多 base 循环），覆盖共享 id、跨库重复边、以及与
   active 边同坐标（COO 累加语义）的组合；
2. ``_scale_xlayer_bridge_edges`` 的分块 ``knn_query`` vs 历史的逐行 ``knn_query``，
   钉住桥边**列表顺序**（行序 × 近邻序）与权重逐位相同，外加块级 fail-open。
"""
import json

import numpy as np
import pytest
import scipy.sparse as sp

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import graph_retrieval as gr
from app.services.embedding import FakeEmbedder
from app.services.kg import scale_index as si
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


# ─────────────────────────────────────────────────────────────────────────────
# 1. splice_csr：纯函数 oracle
# ─────────────────────────────────────────────────────────────────────────────

def _frozen_csr_to_edges(node_ids, transition):
    """**冻结基线，勿改。** 改动前 ``si.csr_to_edges`` 的逐字副本。

    刻意不调 ``si.csr_to_edges``：本批之后它已没有生产调用方，后人重写/优化它
    时不会有任何生产行为提醒他"这也是 oracle"——oracle 就会与被测实现同向漂移，
    等价断言随之变成自动通过。这份副本是独立的第二实现，所以它必须原地不动。
    """
    coo = transition.tocoo()
    return [(node_ids[i], node_ids[j], 1.0) for i, j in zip(coo.col, coo.row)]


def _old_multi_base_combine(libs):
    """_scale_combined_graph 多 base 循环的历史实现（逐字拷贝，改动前的形态）。

    每轮把下一个 base 的 CSR 展开成 (src, dst, 1.0) tuple 列表，再当作 "active"
    边喂给 splice_active。
    """
    combined_ids = list(libs[0][0])
    combined_A = libs[0][1]
    for ids, A in libs[1:]:
        combined_ids, combined_A = si.splice_active(
            combined_ids, combined_A, list(ids), _frozen_csr_to_edges(ids, A))
    return combined_ids, combined_A


def _new_multi_base_combine(libs):
    """改动后的形态：一次性组合，不反复复制累计 CSR。"""
    return si.splice_csr_many(
        list(libs[0][0]),
        libs[0][1],
        [(list(ids), A) for ids, A in libs[1:]],
    )


def _three_libraries():
    """三个小库：含跨库共享 id、以及**跨库重复的同一条边**。"""
    lib1_ids = [f"b{i}" for i in range(6)]
    lib1_edges = [
        ("b0", "b1", 1.0), ("b1", "b0", 1.0),
        ("b2", "b3", 1.0),                       # 与 lib2 重复的坐标
        ("b2", "b0", 1.0),                       # 让 b2 出度>1：重复边的权重和
                                                 # 不会被列归一化掩盖掉
        ("b3", "b4", 1.0), ("b4", "b5", 1.0),
        ("b5", "b0", 1.0), ("b0", "b0", 1.0),    # 自环
    ]
    lib1_A, _ = si.build_transition(lib1_ids, lib1_edges)

    lib2_ids = ["b2", "b3", "c0", "c1"]          # b2/b3 与 lib1 共享
    lib2_edges = [
        ("b2", "b3", 1.0),                       # 与 lib1 完全重复的边
        ("c0", "b3", 1.0), ("b3", "c0", 1.0),
        ("c1", "c0", 1.0), ("c0", "c1", 1.0),
    ]
    lib2_A, _ = si.build_transition(lib2_ids, lib2_edges)

    lib3_ids = ["b3", "c0", "c1", "d0", "d1"]    # b3(lib1&lib2)、c0/c1(lib2) 共享
    lib3_edges = [
        ("c1", "b3", 1.0),
        # 与 lib2 的 b3->c0 重复，且 b3 在前缀里出度>1 → COO 累加语义在**最终**
        # 矩阵里可见（中间轮的重复会被下一轮的「只取结构」抹掉，所以重复必须
        # 发生在最后一轮才有区分力）。
        ("b3", "c0", 1.0),
        ("d0", "b3", 1.0), ("b3", "d0", 1.0),
        ("d1", "d0", 1.0), ("d0", "d1", 1.0),
        ("d1", "c1", 1.0),
    ]
    lib3_A, _ = si.build_transition(lib3_ids, lib3_edges)
    return [(lib1_ids, lib1_A), (lib2_ids, lib2_A), (lib3_ids, lib3_A)]


def _assert_same_matrix(new_A, old_A, label):
    assert new_A.shape == old_A.shape, label
    assert np.allclose(new_A.toarray(), old_A.toarray()), label
    # 结构也逐位相同（不止数值接近）：同一组 nnz 坐标。
    n_coo, o_coo = new_A.tocoo(), old_A.tocoo()
    assert n_coo.nnz == o_coo.nnz, label
    assert set(zip(n_coo.row.tolist(), n_coo.col.tolist())) == \
        set(zip(o_coo.row.tolist(), o_coo.col.tolist())), label


def test_splice_csr_matches_csr_to_edges_oracle_multi_library():
    """三库依次拼接：combined_ids 精确相等 + 矩阵 allclose + 列随机守恒。"""
    libs = _three_libraries()
    new_ids, new_A = _new_multi_base_combine(libs)
    old_ids, old_A = _old_multi_base_combine(libs)

    assert new_ids == old_ids
    # 去重序：base 序 + extra 中新 id 的追加序。
    assert new_ids == ["b0", "b1", "b2", "b3", "b4", "b5",
                       "c0", "c1", "d0", "d1"]
    _assert_same_matrix(new_A, old_A, "multi-library splice must match oracle")
    cs = np.asarray(new_A.sum(axis=0)).ravel()
    assert np.allclose(cs[cs > 0], 1.0), "列随机性守恒"


def test_splice_csr_many_matches_ordered_fold_randomized():
    """随机 differential：2..6 库、节点交叠和重复边下逐位复现左折叠。"""
    rng = np.random.default_rng(20260811)
    universe = np.array([f"n{i}" for i in range(18)])
    for library_count in range(2, 7):
        for _case in range(20):
            libs = []
            for _ in range(library_count):
                ids = rng.choice(
                    universe,
                    size=int(rng.integers(3, 10)),
                    replace=False,
                ).tolist()
                # Sampling endpoint pairs with replacement intentionally creates
                # duplicate coordinates before build_transition collapses them.
                edges = [
                    (
                        ids[int(rng.integers(0, len(ids)))],
                        ids[int(rng.integers(0, len(ids)))],
                        float(rng.random() + 0.01),
                    )
                    for _ in range(int(rng.integers(0, 30)))
                ]
                transition, _ = si.build_transition(ids, edges)
                libs.append((ids, transition))

            old_ids, old_A = _old_multi_base_combine(libs)
            new_ids, new_A = _new_multi_base_combine(libs)
            assert new_ids == old_ids
            assert np.array_equal(new_A.indptr, old_A.indptr)
            assert np.array_equal(new_A.indices, old_A.indices)
            assert np.array_equal(new_A.data, old_A.data)


def test_splice_csr_many_normalizes_only_once_for_three_or_more_libraries(
    monkeypatch,
):
    """复杂度护栏：参与者再多也只建/归一最终累计矩阵一次。"""
    calls = 0
    original = si._column_stochastic

    def _counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(si, "_column_stochastic", _counted)
    _new_multi_base_combine(_three_libraries())
    assert calls == 1


def test_splice_csr_many_preserves_ppr_scores_and_ranking():
    """组合阵的 PPR 得分与排序都和历史左折叠相同，保护检索效果。"""
    libs = _three_libraries()
    old_ids, old_A = _old_multi_base_combine(libs)
    new_ids, new_A = _new_multi_base_combine(libs)
    assert new_ids == old_ids
    reset = np.zeros(len(old_ids), dtype=np.float64)
    reset[0], reset[-1] = 0.7, 0.3
    old_scores = si.personalized_ppr(old_A, reset)
    new_scores = si.personalized_ppr(new_A, reset)
    assert np.array_equal(new_scores, old_scores)
    assert np.argsort(-new_scores, kind="stable").tolist() == np.argsort(
        -old_scores, kind="stable"
    ).tolist()


def test_splice_csr_matches_oracle_with_overlapping_active_edges():
    """多库拼接 + 末尾 active splice：active 边与已有 base 坐标重合（COO 累加）
    时两条路径仍逐位一致——这是 splice_csr 最容易被写错的地方（若在 remap 时
    自作聪明地去重，重合坐标的权重和就会变）。"""
    libs = _three_libraries()
    active_ids = ["b0", "z0", "z1"]              # b0 共享、z* 新增
    active_edges = [
        ("b2", "b3", 0.37),                      # 与 base 结构边同坐标 → 累加
        ("z0", "b0", 0.9), ("b0", "z0", 0.9),
        ("z1", "z0", 0.5), ("z0", "z1", 0.5),
        ("z1", "NOPE", 1.0),                     # 悬空，应被丢弃
    ]
    new_ids, new_A = _new_multi_base_combine(libs)
    new_ids, new_A = si.splice_active(new_ids, new_A, active_ids, active_edges)
    old_ids, old_A = _old_multi_base_combine(libs)
    old_ids, old_A = si.splice_active(old_ids, old_A, active_ids, active_edges)

    assert new_ids == old_ids
    assert "NOPE" not in new_ids
    _assert_same_matrix(new_A, old_A, "active splice on top must match oracle")
    cs = np.asarray(new_A.sum(axis=0)).ravel()
    assert np.allclose(cs[cs > 0], 1.0)


def test_splice_csr_single_pair_is_bitwise_equal_to_old_pair():
    """单次 splice 的逐位等价（不只是 allclose）：同一份 indptr/indices/data。"""
    libs = _three_libraries()
    (b_ids, b_A), (e_ids, e_A) = libs[0], libs[1]
    new_ids, new_A = si.splice_csr(list(b_ids), b_A, list(e_ids), e_A)
    old_ids, old_A = si.splice_active(
        list(b_ids), b_A, list(e_ids), _frozen_csr_to_edges(e_ids, e_A))
    assert new_ids == old_ids
    assert np.array_equal(new_A.indptr, old_A.indptr)
    assert np.array_equal(new_A.indices, old_A.indices)
    assert np.array_equal(new_A.data, old_A.data)     # 逐位，不是 allclose


def test_splice_csr_many_single_extra_keeps_noncanonical_duplicate_semantics():
    """单 extra 前没有中间结构重置，base 的显式重复坐标仍须累加。"""
    base_ids = ["a", "b"]
    # Two stored entries at the same (target=b, source=a) coordinate.
    base_A = sp.csr_matrix(
        (
            np.array([0.25, 0.75]),
            np.array([0, 0]),
            np.array([0, 0, 2]),
        ),
        shape=(2, 2),
    )
    base_A.has_canonical_format = False
    extra_ids = ["c"]
    extra_A = sp.csr_matrix((1, 1), dtype=np.float64)

    old_ids, old_A = si.splice_csr(base_ids, base_A, extra_ids, extra_A)
    new_ids, new_A = si.splice_csr_many(
        base_ids, base_A, [(extra_ids, extra_A)]
    )
    assert new_ids == old_ids
    assert np.array_equal(new_A.indptr, old_A.indptr)
    assert np.array_equal(new_A.indices, old_A.indices)
    assert np.array_equal(new_A.data, old_A.data)


def test_splice_csr_handles_empty_and_disjoint_extras():
    """空 extra 图、以及与 base 完全不相交的 extra 图。"""
    b_ids = ["b0", "b1"]
    b_A, _ = si.build_transition(b_ids, [("b0", "b1", 1.0), ("b1", "b0", 1.0)])

    empty_ids = ["e0", "e1"]
    empty_A = sp.csr_matrix((2, 2), dtype=np.float64)
    new_ids, new_A = si.splice_csr(list(b_ids), b_A, empty_ids, empty_A)
    old_ids, old_A = si.splice_active(
        list(b_ids), b_A, empty_ids, _frozen_csr_to_edges(empty_ids, empty_A))
    assert new_ids == old_ids == ["b0", "b1", "e0", "e1"]
    _assert_same_matrix(new_A, old_A, "empty extra graph")

    dis_ids = ["x0", "x1"]
    dis_A, _ = si.build_transition(dis_ids, [("x0", "x1", 1.0)])
    new_ids, new_A = si.splice_csr(list(b_ids), b_A, dis_ids, dis_A)
    old_ids, old_A = si.splice_active(
        list(b_ids), b_A, dis_ids, _frozen_csr_to_edges(dis_ids, dis_A))
    assert new_ids == old_ids
    _assert_same_matrix(new_A, old_A, "disjoint extra graph")


def test_splice_csr_is_order_sensitive_on_remap():
    """反向护栏：把 extra 的 id 顺序打乱（等价于 remap 映射错序）必须改变结果,
    否则前面的等价断言就是自动通过的。"""
    libs = _three_libraries()
    (b_ids, b_A), (e_ids, e_A) = libs[0], libs[1]
    _, ref_A = si.splice_csr(list(b_ids), b_A, list(e_ids), e_A)
    shuffled = [e_ids[1], e_ids[0], e_ids[3], e_ids[2]]
    _, alt_A = si.splice_csr(list(b_ids), b_A, shuffled, e_A)
    assert not np.allclose(ref_A.toarray(), alt_A.toarray()), (
        "remap 顺序必须真的参与结果；若相等说明等价测试无区分力")


# ─────────────────────────────────────────────────────────────────────────────
# 2. 组合图集成：_scale_combined_graph 的多 base 循环
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _fake_scale_index(node_ids, edges, version):
    """最小可用的 ScaleIndex：只填 _scale_combined_graph 会读的字段。
    ann_labels 留空 → 跨层桥直接早退（本节只测拼接循环）。"""
    A, index = si.build_transition(node_ids, edges)
    return si.ScaleIndex(
        node_ids=list(node_ids),
        node_index=index,
        transition=A,
        idf=np.arange(1, len(node_ids) + 1, dtype=np.float64),
        chunk_index=np.array([0], dtype=np.int64),
        ann_labels=[],
        ann_path="",
        manifest={"version": version, "dim": 16},
    )


def test_scale_combined_graph_multi_base_matches_old_loop(repo, monkeypatch):
    """端到端：_scale_combined_graph 对多参考库的输出必须与历史循环一致。"""
    monkeypatch.setattr(repo.settings, "ppr_float32", False)
    active = repo.create_notebook(NotebookCreate(name="active"))   # 无 KG → 空 delta

    libs = _three_libraries()
    base_indexes = [
        (f"nb{i}", _fake_scale_index(ids, _frozen_csr_to_edges(ids, A), [i]))
        for i, (ids, A) in enumerate(libs)
    ]
    graph = repo.retrieval.graph
    out = graph._scale_combined_graph(active.id, base_indexes)

    old_ids, old_A = _old_multi_base_combine(
        [(idx.node_ids, idx.transition) for _, idx in base_indexes])
    assert out["combined_ids"] == old_ids
    _assert_same_matrix(out["combined_A"], old_A, "combined graph vs old loop")
    # idf/chunk 侧信息不受本次改动影响：仍是「首见者优先」。
    assert out["combined_idf"][0] == pytest.approx(1.0)
    assert len(out["combined_idf"]) == len(old_ids)
    assert out["combined_index"] == {n: i for i, n in enumerate(old_ids)}


# ─────────────────────────────────────────────────────────────────────────────
# 3. 跨层桥：批量 knn_query vs 逐行 knn_query
# ─────────────────────────────────────────────────────────────────────────────

def _frozen_scalar_similarity(dist):
    """**冻结基线，勿改。** 逐行版的相似度算术逐字副本：距离先精确提升到
    双精度，减法在双精度里做，再用 python 的 max 夹到 0（``max(0.0, nan)``
    取首参 → 0.0）。"""
    return max(0.0, 1.0 - float(dist))


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_xbridge_similarities_match_scalar_reference_bitwise(dtype):
    """相似度算术的直接单测（不需要真 ANN）：逐位等于旧的
    ``max(0.0, 1.0 - float(d))``，含 NaN 与 1-d 在单精度里不精确的那些值。"""
    raw = [0.0, 1e-7, 1e-6, 1e-4, 1e-3, 0.17, 0.3, 0.5, 0.83, 1.0,
           1.0000001, 1.5, 2.0, float("nan")]
    dists = np.array([raw, list(reversed(raw))], dtype=dtype)

    got = gr._xbridge_similarities(dists)

    assert got.dtype == np.float64
    assert got.shape == dists.shape
    assert not np.isnan(got).any(), "NaN 必须按旧语义归零，不得漏出"
    for r in range(dists.shape[0]):
        for c in range(dists.shape[1]):
            want = _frozen_scalar_similarity(dists[r, c])
            assert got[r, c] == want, (          # 逐位，不是 approx
                f"[{r},{c}] d={dists[r, c]!r}: {got[r, c]!r} != {want!r}")


def test_xbridge_similarities_float32_subtraction_would_differ():
    """反向护栏：证明上面的逐位断言**真的**能抓到「在 float32 里做减法」。
    若哪天 numpy 的提升规则变得让两者恒等，这条会先红，提醒上面那条已失去
    区分力（而不是让它静默变成自动通过）。"""
    dists = np.array([[1e-7, 1e-4, 0.17]], dtype=np.float32)
    correct = gr._xbridge_similarities(dists)
    single = np.maximum(1.0 - dists, 0.0)        # 变异形态：单精度减法
    assert not np.array_equal(correct, single.astype(np.float64)), (
        "float32/float64 减法在这些取值上必须给出不同结果")


def test_xbridge_nan_distance_produces_no_edge_at_zero_threshold(repo, monkeypatch):
    """NaN 归零的可观测后果：阈值 <= 0 时，NaN 距离既不该产出 NaN 权重的桥边，
    也不该因 `nan >= 0` 为假而与旧版的「产出一条 0 权重边」分叉。"""
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", True)
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_threshold", 0.0)
    base, active = _bridge_scenario(repo, n_base=2, n_active=2)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    graph = repo.retrieval.graph
    real_ann = graph._open_scale_ann(base_idx, "kg")

    class _NanAnn:
        def set_ef(self, v):
            return real_ann.set_ef(v)

        def knn_query(self, data, k):
            labs, dists = real_ann.knn_query(data, k=k)
            dists = np.asarray(dists, dtype=np.float32).copy()
            dists[0, 0] = np.nan
            return labs, dists

    monkeypatch.setattr(graph, "_open_scale_ann", lambda idx, kind: _NanAnn())
    got = graph._scale_xlayer_bridge_edges(
        active.id, [(base.id, base_idx)], [], active_node_ids=None)

    assert all(not np.isnan(w) for _, _, w in got), "桥边权重不得含 NaN"
    # 旧版 max(0.0, nan) == 0.0 且 0.0 >= 0.0 → 该近邻仍产出一条 0 权重边。
    assert any(w == 0.0 for _, _, w in got), "NaN 应按旧语义降为 0 权重边"


def _insert_concept(repo, nb_id, oid, name, sid, day=1):
    now = f"2026-06-{day:02d}T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (sid, nb_id, "t", "md", "ready", now, now))
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "approved", "", json.dumps({"name": name}), "[]",
             sid, now, now))
        vec = repo._runtime.models.embedding(
            "retrieval_query_embedding").embed_query(name)
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)", (oid, nb_id, json.dumps(vec), now))


def _bridge_scenario(repo, n_base=5, n_active=9):
    """一个已建索引的外部 base（多概念 → k>1）+ 一个未建索引的 active 库
    （节点数刻意多于测试用的块大小 → 至少三个块且末块不满）。"""
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    for i in range(n_base):
        _insert_concept(repo, base.id, f"eB{i}", f"MOSFET-{i}", "sB")
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)

    active = repo.create_notebook(NotebookCreate(name="active"))
    for i in range(n_active):
        # 一半与 base 同名（高相似度），一半不同名。
        name = f"MOSFET-{i}" if i % 2 == 0 else f"concept-{i}"
        _insert_concept(repo, active.id, f"oa{i}", name, "sA")
    repo.rebuild_unified_kg(active.id)
    return base, active


def _perrow_bridge_oracle(repo, base_idx, active_id):
    """逐行 knn_query 的历史实现（逐字拷贝改动前的内层循环）。"""
    import hnswlib

    syn_k = repo.settings.ppr_emb_synonym_topk
    syn_thr = repo.settings.ppr_emb_synonym_threshold
    with repo._connect() as db:
        a_ids, a_mat = repo.retrieval.graph._vector_matrix(
            db, active_id, "knowledge_embeddings", "object_id")
    a_mat_arr = np.asarray(a_mat, dtype=np.float32)
    dim = int(base_idx.manifest.get("dim", a_mat_arr.shape[1]))
    ann = hnswlib.Index(space="cosine", dim=dim)
    ann.load_index(base_idx.ann_path, max_elements=len(base_idx.ann_labels))
    ann.set_ef(max(syn_k + 1, 50))
    out = []
    for ai, a_id in enumerate(a_ids):
        k = min(syn_k, len(base_idx.ann_labels))
        labs, dists = ann.knn_query(a_mat_arr[ai], k=k)
        for lab, dist in zip(labs[0], dists[0]):
            base_nid = base_idx.ann_labels[int(lab)]
            if base_nid == a_id:
                continue
            sim = max(0.0, 1.0 - float(dist))
            if sim >= syn_thr:
                out.append((a_id, base_nid, sim))
                out.append((base_nid, a_id, sim))
    return out


@pytest.mark.parametrize("block", [1, 2, 4, 1000])
def test_batched_xbridge_matches_perrow_oracle(repo, monkeypatch, block):
    """批量版与逐行版产出的桥边列表**含顺序**逐位一致，且与块大小无关。"""
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", True)
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_threshold", 0.0)
    base, active = _bridge_scenario(repo)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    assert base_idx is not None and len(base_idx.ann_labels) > 1

    oracle = _perrow_bridge_oracle(repo, base_idx, active.id)
    assert len(oracle) > 2 * 4, "场景需产出足够多的桥边才有区分力"

    monkeypatch.setattr(gr, "_XBRIDGE_QUERY_BLOCK", block)
    seed = [("seed-a", "seed-b", 1.0)]
    got = repo.retrieval.graph._scale_xlayer_bridge_edges(
        active.id, [(base.id, base_idx)], list(seed), active_node_ids=None)

    assert got[:len(seed)] == seed, "既有 active 边必须原样保留在前"
    produced = got[len(seed):]
    assert len(produced) == len(oracle)
    for i, (new_e, old_e) in enumerate(zip(produced, oracle)):
        assert new_e[0] == old_e[0] and new_e[1] == old_e[1], (
            f"第 {i} 条桥边端点/顺序不一致: {new_e} vs {old_e}")
        assert new_e[2] == old_e[2], (          # 逐位，不是 approx
            f"第 {i} 条桥边权重不一致: {new_e[2]!r} vs {old_e[2]!r}")


def test_batched_xbridge_block_failure_is_fail_open(repo, monkeypatch):
    """块级 fail-open：一个块的 knn_query 抛错只丢该块的桥边，其余块照常，
    且只记一次 model error（登记过的可观测性差异）。"""
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", True)
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_threshold", 0.0)
    base, active = _bridge_scenario(repo)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    graph = repo.retrieval.graph
    monkeypatch.setattr(gr, "_XBRIDGE_QUERY_BLOCK", 3)

    healthy = graph._scale_xlayer_bridge_edges(
        active.id, [(base.id, base_idx)], [], active_node_ids=None)
    assert healthy

    real_ann = graph._open_scale_ann(base_idx, "kg")

    class _FlakyAnn:
        """第 2 个块（调用序号 1）抛错，其余转发给真 ANN。"""

        def __init__(self):
            self.calls = 0

        def set_ef(self, v):
            return real_ann.set_ef(v)

        def knn_query(self, data, k):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("boom")
            return real_ann.knn_query(data, k=k)

    flaky = _FlakyAnn()
    monkeypatch.setattr(graph, "_open_scale_ann", lambda idx, kind: flaky)
    noted = []
    monkeypatch.setattr(
        graph, "_note_model_error",
        lambda *a, **kw: noted.append((a, kw)))

    got = graph._scale_xlayer_bridge_edges(
        active.id, [(base.id, base_idx)], [], active_node_ids=None)

    assert len(noted) == 1, f"坏块应只记一次 model error，实际 {len(noted)}"
    assert noted[0][0][0] == "scale_ppr_xbridge_query"
    assert flaky.calls > 2, "坏块之后仍要继续查询后续块"
    assert got, "其余块的桥边必须照常产出"
    assert len(got) < len(healthy), "坏块的桥边应当缺失"
    # 缺失的正好是坏块那 3 行贡献的边，其余边逐位保留且顺序不变。
    assert set(got).issubset(set(healthy))
