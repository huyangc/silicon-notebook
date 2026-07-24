"""viz-only 索引产物:save/load 往返 + 缺文件返回 None。"""
import json

import numpy as np
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
    assert idx.viz_edges == [["a", "b", "relates"], ["a", "c", "relates"]]
    assert list(idx.viz_deg) == [2, 1, 1]
    assert (idx.viz_adj.toarray() == adj.toarray()).all()
    assert idx.manifest["version"] == ["nb-1", 3, "2026-07-01T00:00:00"]
    assert idx.manifest["n_viz_nodes"] == 3


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
    assert idx.viz_edges == edges
    assert idx.viz_names == viz_names
