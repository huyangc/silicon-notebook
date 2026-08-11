"""viz-only 索引产物:save/load 往返 + 缺文件返回 None。"""
import json
import os

import numpy as np
import pytest
import scipy.sparse as sp
from app.services.kg import scale_index as si
from app.services.kg import viz_index as vi


def _arrays():
    viz_ids = ["a", "b", "c"]
    # undirected a-b, a-c
    adj = sp.csr_matrix(np.array([[0, 1, 1], [1, 0, 0], [1, 0, 0]], dtype=np.int8))
    viz_deg = np.array([2, 1, 1], dtype=np.int32)
    viz_types = ["concept", "concept", "concept"]
    viz_names = ["MOSFET", "gain", "bias"]
    viz_payload = {"edges": [["a", "b", "relates"], ["a", "c", "relates"]]}
    return viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload


def test_save_load_roundtrip(tmp_path):
    viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload = _arrays()
    out = str(tmp_path / "kg_viz" / "nb-1")
    manifest = {"version": ["nb-1", 3, "2026-07-01T00:00:00"], "n_viz_nodes": 3, "n_viz_edges": 2}
    vi.save_viz_index(out, viz_ids=viz_ids, viz_adj=adj, viz_deg=viz_deg,
                      viz_types=viz_types, viz_names=viz_names,
                      viz_payload=viz_payload, manifest=manifest)
    idx = vi.load_viz_index(out)
    assert idx is not None
    assert idx.viz_ids == viz_ids
    assert idx.viz_types == viz_types
    assert idx.viz_names == viz_names
    # 边集是紧凑列式表示;语义断言走 rows()(原边序的三元组)。
    assert list(idx.viz_edges.rows(idx.viz_ids)) == \
        [["a", "b", "relates"], ["a", "c", "relates"]]
    assert list(idx.viz_deg) == [2, 1, 1]
    assert (idx.viz_adj.toarray() == adj.toarray()).all()
    assert idx.manifest["version"] == ["nb-1", 3, "2026-07-01T00:00:00"]
    assert idx.manifest["n_viz_nodes"] == 3
    # 新产物把两个昂贵派生索引一起落盘并在 load 时装入缓存；第一次请求不再
    # 做 degree O(V log V) / edge O(E log E) 排序。
    assert np.array_equal(idx.viz_deg_order(), np.asarray([0, 1, 2]))
    edge_order, edge_indptr = idx.viz_edges.by_src()
    assert np.array_equal(edge_order, np.asarray([0, 1]))
    assert np.array_equal(edge_indptr, np.asarray([0, 2, 2, 2]))
    assert "_viz_deg_order_cache" in idx.__dict__
    assert "_by_src_cache" in idx.viz_edges.__dict__
    with np.load(os.path.join(out, "viz.npz"), allow_pickle=True) as z:
        assert "viz_deg_order" in z.files
        assert "viz_edge_src_order" in z.files
        assert "viz_edge_src_indptr" in z.files


def test_load_missing_returns_none(tmp_path):
    assert vi.load_viz_index(str(tmp_path / "nope")) is None


def test_encode_viz_edges_is_byte_array_not_string_scalar():
    """viz_edges 必须落成 uint8 一维字节数组,而非 unicode 标量。

    历史写法把 json.dumps(edges) 当单个 unicode 标量塞给 np.savez;numpy 字符串
    数组单元素 itemsize 用 C int 存(unicode ×4 字节),超大 base 图会溢出触发
    'string too large to store inside array'。这条把「用字节数组绕开该上限」钉死:
    退回字符串标量会让本断言直接失败。"""
    arr = si.encode_viz_edges([["a", "b", "relates"]])
    assert arr.dtype == np.uint8
    assert arr.ndim == 1
    # 字节路径可承载远超 512Mi 字符上限的载荷(此处只验解码正确,不真造 GB 级)。
    assert si.decode_viz_edges(arr) == [["a", "b", "relates"]]
    assert si.decode_viz_edges(si.encode_viz_edges([])) == []


def test_decode_viz_edges_reads_legacy_string_scalar():
    """旧索引把 viz_edges 存成 0-d unicode 标量(json.dumps 的产物)。decode 必须
    仍能读回——老索引不重建即可继续加载(older-index-stays-valid)。两个读路径
    (scale_index.load_scale_index 与 viz_index.load_viz_index)共用此函数,故直接
    覆盖它即等价覆盖两侧。"""
    legacy = np.asarray(json.dumps([["x", "y", "z"], ["p", "q", "r"]]))
    assert legacy.ndim == 0 and legacy.dtype.kind == "U"
    assert si.decode_viz_edges(legacy) == [["x", "y", "z"], ["p", "q", "r"]]


def test_save_load_roundtrip_many_edges(tmp_path):
    """稠密边表走字节路径的整链往返(save_viz_index → load_viz_index)。"""
    n = 5000
    viz_ids = [f"n{i}" for i in range(n)]
    adj = sp.csr_matrix((n, n), dtype=np.int8)
    viz_deg = np.zeros(n, dtype=np.int32)
    viz_types = ["concept"] * n
    viz_names = [f"名字{i}" for i in range(n)]  # 含非 ASCII,确认 UTF-8 编解码无损
    edges = [[f"n{i}", f"n{(i + 1) % n}", "relates"] for i in range(n)]
    out = str(tmp_path / "kg_viz" / "nb-big")
    manifest = {"version": ["nb-big", 1, "2026-07-24T00:00:00"],
                "n_viz_nodes": n, "n_viz_edges": len(edges)}
    vi.save_viz_index(out, viz_ids=viz_ids, viz_adj=adj, viz_deg=viz_deg,
                      viz_types=viz_types, viz_names=viz_names,
                      viz_payload={"edges": edges}, manifest=manifest)
    idx = vi.load_viz_index(out)
    assert idx is not None
    assert list(idx.viz_edges.rows(idx.viz_ids)) == edges
    assert idx.viz_names == viz_names
    # 紧凑落盘:边不再是一坨 JSON,而是三条并列数组 + 去重类型表(此图只有
    # 一种 edge_type,表就一项)。
    with np.load(os.path.join(out, "viz.npz"), allow_pickle=True) as z:
        assert "viz_edges" not in z.files
        assert z["viz_edge_src"].dtype == np.int32
        assert len(z["viz_edge_src"]) == len(edges)
        assert list(z["viz_edge_types"]) == ["relates"]


def _save_scale_with_viz(tmp_path, *, n_viz_edges, name="nb-scale"):
    """写一份带 viz 的 scale 索引;manifest 的 n_viz_edges 由调用方指定,好让
    对账逻辑既能被证明生效、也能被证明不误伤。"""
    viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload = _arrays()
    out = str(tmp_path / "kg_index" / name)
    si.save_scale_index(
        out,
        node_ids=["a", "b"],
        transition=sp.csr_matrix(np.eye(2, dtype=np.float64)),
        idf=[1.0, 1.0],
        chunk_index=[0],
        ann_vectors=np.eye(2, 4, dtype=np.float32),
        ann_labels=["a", "b"],
        manifest={"version": ["v", 1], "dim": 4, "n_nodes": 2, "n_ann": 2,
                  "n_viz_nodes": 3, "n_viz_edges": n_viz_edges},
        viz_ids=viz_ids, viz_adj=adj, viz_deg=viz_deg,
        viz_types=viz_types, viz_names=viz_names, viz_payload=viz_payload,
    )
    return out


def test_scale_index_reconciles_compact_edge_count_with_manifest(tmp_path):
    """紧凑边键必须与 manifest.n_viz_edges 对上,对不上判整份索引不可信。

    没有这条,一份被裁过/半截写入的 viz.npz 会安静地给出空边集,KG 视图退化成
    「只有点没有边」而无人察觉(fail-silent)。"""
    good = _save_scale_with_viz(tmp_path, n_viz_edges=2, name="ok")
    idx = si.load_scale_index(good)
    assert idx is not None and len(idx.viz_edges) == 2
    assert "_viz_deg_order_cache" in idx.__dict__
    assert "_by_src_cache" in idx.viz_edges.__dict__

    lying = _save_scale_with_viz(tmp_path, n_viz_edges=3, name="lying")
    assert si.load_scale_index(lying) is None


def test_scale_index_rejects_viz_npz_whose_compact_edge_keys_vanished(tmp_path):
    """键整个丢了(而 manifest 仍声称有边)同样判损坏,不静默当零边图。"""
    out = _save_scale_with_viz(tmp_path, n_viz_edges=2, name="stripped")
    viz_npz = os.path.join(out, "viz.npz")
    with np.load(viz_npz, allow_pickle=True) as z:
        kept = {k: z[k] for k in z.files if not k.startswith("viz_edge_")}
    np.savez(viz_npz, **kept)
    assert si.load_scale_index(out) is None


def test_scale_index_does_not_reconcile_legacy_json_edge_key(tmp_path):
    """旧 JSON 边键不参与对账——from_rows 对远古形状的行有合法丢弃,老索引
    绝不能因此被判损坏(older-index-stays-valid)。"""
    out = _save_scale_with_viz(tmp_path, n_viz_edges=99, name="legacy")
    viz_ids, _, _, _, _, viz_payload = _arrays()
    viz_npz = os.path.join(out, "viz.npz")
    with np.load(viz_npz, allow_pickle=True) as z:
        kept = {k: z[k] for k in z.files if not k.startswith("viz_edge_")}
    np.savez(viz_npz, **kept, viz_edges=si.encode_viz_edges(viz_payload["edges"]))
    idx = si.load_scale_index(out)
    assert idx is not None
    assert list(idx.viz_edges.rows(idx.viz_ids)) == viz_payload["edges"]


def _save_standalone_viz(tmp_path, *, n_viz_edges, name="nb-1"):
    viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload = _arrays()
    out = str(tmp_path / "kg_viz" / name)
    vi.save_viz_index(out, viz_ids=viz_ids, viz_adj=adj, viz_deg=viz_deg,
                      viz_types=viz_types, viz_names=viz_names,
                      viz_payload=viz_payload,
                      manifest={"version": ["v", 1], "n_viz_nodes": 3,
                                "n_viz_edges": n_viz_edges})
    return out


def _rewrite_viz_npz(out: str, **updates) -> None:
    path = os.path.join(out, "viz.npz")
    with np.load(path, allow_pickle=True) as z:
        arrays = {key: z[key] for key in z.files}
    arrays.update(updates)
    np.savez(path, **arrays)


@pytest.mark.parametrize("artifact_kind", ["scale", "standalone"])
@pytest.mark.parametrize(
    "corruption", ["bad_order", "bad_aux_dtype", "short_deg", "bad_deg_dtype"]
)
def test_viz_loaders_handle_malformed_aux_and_degree_arrays(
    tmp_path, artifact_kind, corruption
):
    """两条 loader 都不得让畸形 order/degree 从请求热路径抛 IndexError。"""
    if artifact_kind == "scale":
        out = _save_scale_with_viz(
            tmp_path, n_viz_edges=2, name=f"malformed-{corruption}"
        )
        load = si.load_scale_index
    else:
        out = _save_standalone_viz(
            tmp_path, n_viz_edges=2, name=f"malformed-{corruption}"
        )
        load = vi.load_viz_index

    if corruption in {"bad_order", "bad_aux_dtype"}:
        order_dtype = np.int64 if corruption == "bad_order" else object
        _rewrite_viz_npz(
            out,
            viz_deg_order=np.asarray([0, 0, 2], dtype=order_dtype),
            viz_edge_src_order=np.asarray([1, 0], dtype=order_dtype),
        )
        idx = load(out)
        assert idx is not None
        assert "_viz_deg_order_cache" not in idx.__dict__
        assert "_by_src_cache" not in idx.viz_edges.__dict__
        assert idx.viz_deg_order().tolist() == [0, 1, 2]
        assert idx.viz_edges.edge_type_at(0, 1) == "relates"
    elif corruption == "short_deg":
        _rewrite_viz_npz(out, viz_deg=np.asarray([2, 1], dtype=np.int32))
        assert load(out) is None
    else:
        _rewrite_viz_npz(
            out, viz_deg=np.asarray(["2", "1", "1"], dtype=object)
        )
        assert load(out) is None


def test_standalone_viz_reconciles_edge_count_with_manifest(tmp_path):
    """独立 viz 产物与 scale-embedded 的一样要对账。

    不对账时,一份被裁过的 viz.npz 会被当健康产物返回:viz_probe 照报 manifest
    的边数、图请求静默缺边,而 manifest 的 version/cluster_seq 仍新鲜 → 永远
    不触发重建。失配走该 loader 既有的「不可用」出口(None),调用方据此重建。"""
    assert vi.load_viz_index(_save_standalone_viz(tmp_path, n_viz_edges=2)) is not None

    lying = _save_standalone_viz(tmp_path, n_viz_edges=3, name="lying")
    assert vi.load_viz_index(lying) is None

    stripped = _save_standalone_viz(tmp_path, n_viz_edges=2, name="stripped")
    viz_npz = os.path.join(stripped, "viz.npz")
    with np.load(viz_npz, allow_pickle=True) as z:
        kept = {k: z[k] for k in z.files if not k.startswith("viz_edge_")}
    np.savez(viz_npz, **kept)
    assert vi.load_viz_index(stripped) is None


def test_standalone_viz_does_not_reconcile_legacy_json_edge_key(tmp_path):
    """旧 JSON 边键豁免;manifest 没写过 n_viz_edges 的老产物同样放行。"""
    _, _, _, _, _, viz_payload = _arrays()
    legacy = _save_standalone_viz(tmp_path, n_viz_edges=99, name="legacy")
    viz_npz = os.path.join(legacy, "viz.npz")
    with np.load(viz_npz, allow_pickle=True) as z:
        kept = {k: z[k] for k in z.files if not k.startswith("viz_edge_")}
    np.savez(viz_npz, **kept, viz_edges=si.encode_viz_edges(viz_payload["edges"]))
    idx = vi.load_viz_index(legacy)
    assert idx is not None
    assert list(idx.viz_edges.rows(idx.viz_ids)) == viz_payload["edges"]

    countless = _save_standalone_viz(tmp_path, n_viz_edges=2, name="countless")
    mpath = os.path.join(countless, "manifest.json")
    with open(mpath, "w") as fh:
        json.dump({"version": ["v", 1], "n_viz_nodes": 3}, fh)
    assert vi.load_viz_index(countless) is not None


def test_compact_artifact_without_auxiliary_indexes_lazily_falls_back(tmp_path):
    """上一版紧凑产物尚无 degree/by-source 辅助键，不重建也必须保持可读。"""
    out = _save_standalone_viz(tmp_path, n_viz_edges=2, name="compact-old")
    viz_npz = os.path.join(out, "viz.npz")
    omitted = {"viz_deg_order", "viz_edge_src_order", "viz_edge_src_indptr"}
    with np.load(viz_npz, allow_pickle=True) as z:
        kept = {key: z[key] for key in z.files if key not in omitted}
    np.savez(viz_npz, **kept)

    idx = vi.load_viz_index(out)
    assert idx is not None
    assert "_viz_deg_order_cache" not in idx.__dict__
    assert "_by_src_cache" not in idx.viz_edges.__dict__
    assert idx.viz_deg_order().tolist() == [0, 1, 2]
    assert idx.viz_edges.edge_type_at(0, 2) == "relates"


def test_save_rejects_an_edge_set_built_against_a_different_node_table(tmp_path):
    """端点存的是下标;边集的 n_nodes 与要落盘的 viz_ids 对不上就是接错了节点表。"""
    viz_ids, adj, viz_deg, viz_types, viz_names, _ = _arrays()
    foreign = si.VizEdgeSet.from_rows([["x", "y", "relates"]], ["x", "y"])
    with pytest.raises(ValueError):
        vi.save_viz_index(str(tmp_path / "bad"), viz_ids=viz_ids, viz_adj=adj,
                          viz_deg=viz_deg, viz_types=viz_types,
                          viz_names=viz_names, viz_payload={"edges": foreign},
                          manifest={"version": ["v", 1]})


def test_legacy_viz_edges_json_key_still_loads(tmp_path):
    """盘上已有的 viz.npz(旧 JSON 边键)不重建也要能加载,并等价成紧凑边集。

    older-index-stays-valid:上线这次紧凑化不得让任何既有索引失效。"""
    viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload = _arrays()
    out = tmp_path / "kg_viz" / "nb-legacy"
    out.mkdir(parents=True)
    np.savez(
        str(out / "viz.npz"),
        viz_ids=np.asarray(viz_ids, dtype=object),
        viz_deg=np.asarray(viz_deg, dtype=np.int32),
        viz_types=np.asarray(viz_types, dtype=object),
        viz_names=np.asarray(viz_names, dtype=object),
        viz_edges=si.encode_viz_edges(viz_payload["edges"]),
    )
    sp.save_npz(str(out / "viz_adj.npz"), adj.tocsr())
    (out / "manifest.json").write_text(json.dumps({"version": ["nb-legacy", 1]}))

    idx = vi.load_viz_index(str(out))
    assert idx is not None
    # 旧产物没有辅助数组时仍可加载；只有真正使用它们时才惰性重建。
    assert "_viz_deg_order_cache" not in idx.__dict__
    assert "_by_src_cache" not in idx.viz_edges.__dict__
    assert list(idx.viz_edges.rows(idx.viz_ids)) == viz_payload["edges"]
    assert len(idx.viz_edges) == 2
    assert list(idx.viz_deg_order()) == [0, 1, 2]
    assert idx.viz_edges.edge_type_at(0, 1) == "relates"
    assert "_viz_deg_order_cache" in idx.__dict__
    assert "_by_src_cache" in idx.viz_edges.__dict__
