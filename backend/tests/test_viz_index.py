"""viz-only 索引产物:save/load 往返 + 缺文件返回 None。"""
import numpy as np
import scipy.sparse as sp
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
