"""viz-only 索引:折叠可视化图的紧凑数组(与检索 scale 索引隔离)。

普通(非 base 层)大 notebook 没有完整 scale 索引 → KG 视图慢路径。给它单独持久化
这一份只含 viz 的产物(canonical 折叠图),unified_graph/kg_neighbors 快路径即可点亮。
落盘目录与检索用的 kg_index/ 严格分开,避免污染 _scale_index/scale_ppr。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import scipy.sparse as sp


@dataclass
class VizIndex:
    """折叠 viz 图。属性名与 ScaleIndex 的 viz_* 对齐,so unified_graph 的有界分派与
    kg_neighbors 可鸭子类型地消费任一来源(base 库的 ScaleIndex 或本轻量索引)。"""
    viz_ids: list
    viz_adj: "sp.csr_matrix"
    viz_deg: "np.ndarray"
    viz_types: list
    viz_names: list
    viz_edges: list
    manifest: dict


def arrays_from_graph(full: dict):
    """Build the compact viz arrays from a folded object-level graph."""
    nodes = full["nodes"]
    edges = full["edges"]
    viz_ids = [node["id"] for node in nodes]
    viz_types = [node["object_type"] for node in nodes]
    viz_names = [(node.get("payload") or {}).get("name", "") for node in nodes]
    index = {node_id: i for i, node_id in enumerate(viz_ids)}
    count = len(viz_ids)

    degree = np.zeros(count, dtype=np.int64)
    undirected_rows = []
    undirected_columns = []
    seen = set()
    edge_list = []
    for edge in edges:
        source = edge["source_object_id"]
        target = edge["target_object_id"]
        source_index = index.get(source)
        target_index = index.get(target)
        if source_index is None or target_index is None:
            continue
        edge_list.append([source, target, edge["edge_type"]])
        degree[source_index] += 1
        degree[target_index] += 1
        if source_index != target_index:
            pair = (
                (source_index, target_index)
                if source_index < target_index
                else (target_index, source_index)
            )
            if pair not in seen:
                seen.add(pair)
                undirected_rows += [pair[0], pair[1]]
                undirected_columns += [pair[1], pair[0]]

    if undirected_rows:
        data = np.ones(len(undirected_rows), dtype=np.int8)
        viz_adj = sp.csr_matrix(
            (data, (undirected_rows, undirected_columns)),
            shape=(count, count),
        )
    else:
        viz_adj = sp.csr_matrix((count, count), dtype=np.int8)
    return (
        viz_ids,
        viz_adj,
        degree.astype(np.int32),
        viz_types,
        viz_names,
        {"edges": edge_list},
    )


def save_viz_index(out_dir: str, *, viz_ids, viz_adj, viz_deg, viz_types,
                   viz_names, viz_payload: dict, manifest: dict) -> dict:
    """写 viz.npz + viz_adj.npz + manifest.json 到 out_dir。返回 manifest。"""
    os.makedirs(out_dir, exist_ok=True)
    np.savez(
        os.path.join(out_dir, "viz.npz"),
        viz_ids=np.asarray(viz_ids, dtype=object),
        viz_deg=np.asarray(viz_deg, dtype=np.int32),
        viz_types=np.asarray(viz_types, dtype=object),
        viz_names=np.asarray(viz_names, dtype=object),
        viz_edges=json.dumps((viz_payload or {}).get("edges", [])),
    )
    sp.save_npz(os.path.join(out_dir, "viz_adj.npz"), viz_adj.tocsr())
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    return manifest


def load_viz_index(out_dir: str) -> Optional[VizIndex]:
    """加载持久化 VizIndex。manifest 或数组文件缺失 → None。"""
    mpath = os.path.join(out_dir, "manifest.json")
    viz_npz = os.path.join(out_dir, "viz.npz")
    viz_adj_path = os.path.join(out_dir, "viz_adj.npz")
    if not (os.path.exists(mpath) and os.path.exists(viz_npz) and os.path.exists(viz_adj_path)):
        return None
    with open(mpath) as fh:
        manifest = json.load(fh)
    with np.load(viz_npz, allow_pickle=True) as z:
        viz_ids = list(z["viz_ids"])
        viz_deg = z["viz_deg"]
        viz_types = list(z["viz_types"])
        viz_names = list(z["viz_names"])
        viz_edges = json.loads(str(z["viz_edges"]))
    viz_adj = sp.load_npz(viz_adj_path)
    return VizIndex(viz_ids=viz_ids, viz_adj=viz_adj, viz_deg=viz_deg,
                    viz_types=viz_types, viz_names=viz_names,
                    viz_edges=viz_edges, manifest=manifest)
