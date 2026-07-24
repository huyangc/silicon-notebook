"""规模化 KG 检索的紧凑基底：scipy CSR 图 + 个性化 PPR + active 拼接 + 构建/加载。

设计见 docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md。
本模块尽量纯函数、可单测；DB/IO 由 sqlite_repository 包装层提供数据。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import scipy.sparse as sp

_logger = logging.getLogger(__name__)


def encode_viz_edges(edges) -> "np.ndarray":
    """把 viz 边列表序列化成 uint8 一维字节数组,供 np.savez 落进 viz.npz。

    历史写法是把 ``json.dumps(edges)`` 当作**单个 unicode 标量**塞给 np.savez。
    numpy 字符串数组的单元素 itemsize 用 C ``int`` 存(unicode 每字符 4 字节),
    上限约 512Mi 字符(``(2**31-1)//4``);超大 base 图的边 JSON 会溢出并触发
    ``TypeError: string too large to store inside array``。改存 uint8 一维数组:
    元素数走 ``npy_intp``(64 位),且 numpy 以 ``force_zip64`` 写 npz 条目,
    彻底绕开这个 2^31 上限。与 decode_viz_edges 成对使用。"""
    payload = json.dumps(edges or []).encode("utf-8")
    return np.frombuffer(payload, dtype=np.uint8)


def decode_viz_edges(raw) -> list:
    """从 viz.npz 读回边列表,兼容两种落盘格式。

    新格式:encode_viz_edges 写的 uint8 一维字节数组 → 解码 UTF-8 再 json。
    旧格式:历史 ``json.dumps(...)`` 存成的 0-d unicode 标量 → 直接 ``str()``。
    老索引不重建也能继续加载(与 has_chunk_ann 的 older-index-stays-valid 同性质)。"""
    arr = np.asarray(raw)
    if arr.dtype == np.uint8:
        text = arr.tobytes().decode("utf-8")
    else:
        text = str(raw)
    return json.loads(text)


@dataclass
class ScaleIndex:
    node_ids: list
    node_index: dict
    transition: "sp.csr_matrix"
    idf: "np.ndarray"
    chunk_index: "np.ndarray"
    ann_labels: list
    ann_path: str
    manifest: dict
    # Folded concept-level viz graph (Task 4 / SP1). None when an older index has
    # no viz.npz (has_viz absent in manifest). Used to serve a bounded core for
    # the KG view + neighbors without re-deriving/folding the full graph.
    viz_ids: list = None          # folded node ids (canonical_id or raw object_id)
    viz_adj: "sp.csr_matrix" = None   # undirected adjacency over folded nodes
    viz_deg: "np.ndarray" = None  # int32 degree (viz_adj.getnnz(axis=1))
    viz_types: list = None        # object_type per folded node
    viz_names: list = None        # display name per folded node (hydration-free)
    viz_edges: list = None        # directed-deduped folded edges [[src,dst,edge_type],...]
    chunk_ann_labels: list = None   # chunk_id 列表(与 chunk_ann.bin 行对齐);无则 None
    chunk_ann_path: str = None      # chunk hnsw 文件路径;无则 None
    relation_ann_labels: list = None   # relation_id 列表(与 relation_ann.bin 行对齐);无则 None
    relation_ann_path: str = None      # relation hnsw 文件路径;无则 None
    ann_handle: object = None        # 惰性缓存的 hnswlib KG ANN handle(不落盘)
    chunk_ann_handle: object = None  # 惰性缓存的 chunk ANN handle(不落盘)
    relation_ann_handle: object = None  # 惰性缓存的 relation ANN handle(不落盘)


def _unusable(out_dir: str, detail: str):
    """索引产物不可信时的统一出口:记一条 warning(含目录与具体失配项,便于运维
    定位)并返回 None。None 对调用方等价于「没建过」,会触发全量重建。**绝不
    raise**——加载在检索热路径上,响亮地退化成「无索引」远好过把错配的索引
    静默返回。"""
    _logger.warning("scale index at %s is unusable: %s", out_dir, detail)
    return None


def _manifest_count(manifest: dict, key: str):
    """取 manifest 里的计数字段。缺键(老索引根本没写过 n_ann/n_viz_nodes 这类
    键)或不是整数 → None,表示「这一项不参与校验」。older-index-stays-valid:
    缺键一律放行,绝不能把老索引判成损坏。"""
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def estimated_ann_bytes(manifest: dict, runtime_dim: int) -> int:
    """Estimated resident bytes of a ScaleIndex's ANN matrices — its kg (n_ann),
    chunk (n_chunk_ann) and relation (n_relation_ann) hnsw handles, each
    rows×runtime_dim×4 (float32). This is the dominant term of a large index's
    footprint and drives the LargeAwareLRUCache 'is large' classification
    (PR-4 / codex PR#359 r1 P1 — n_nodes alone misses a chunk/relation-ANN-heavy
    source base). A missing count contributes 0 (older indexes stay classified
    small rather than being over-evicted on an absent key)."""
    rows = sum(
        (_manifest_count(manifest, key) or 0)
        for key in ("n_ann", "n_chunk_ann", "n_relation_ann")
    )
    return rows * int(runtime_dim) * 4


def _first_mismatch(checks) -> str | None:
    """checks: [(名称, 期望, 实际)]。期望为 None 表示该项不参与校验(缺键)。
    返回第一条失配的人可读描述;全部通过返回 None。"""
    for label, expected, actual in checks:
        if expected is not None and expected != actual:
            return f"{label}不一致(期望 {expected},实际 {actual})"
    return None


def load_scale_index(out_dir: str):
    """从 out_dir 加载持久化的 ScaleIndex。manifest 不存在或目录不存在时返回 None。
    ANN 索引不预加载（延迟由调用方按需用 ann_path 打开）。

    加载时顺带做一层零成本的自洽校验:manifest 计数 vs 数组长度、数组之间的
    形状关系。任一产物读不出来、或任一项失配 → 记 warning 并返回 None(与
    「没建过」等价,调用方据此走全量重建)。这一层是为了兜住已经躺在盘上的坏
    索引——历史上全量重建直写活目录,崩在中途会留下「旧 manifest + 半截数组」
    这种看着能加载、其实描述的是别的数据的目录。
    校验一律只在 manifest 里该键存在时生效(older-index-stays-valid),可选产物
    (viz / chunk_ann / relation_ann)只在本次真的读出来了才比对——文件不存在时
    维持既有的「跳过该可选产物」语义,不升级成整份索引损坏。"""
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(mpath):
        return None
    try:
        with open(mpath) as fh:
            manifest = json.load(fh)
    except Exception as exc:  # noqa: BLE001 — 半截/损坏的 manifest 也是「不可信」
        return _unusable(out_dir, f"manifest.json 读不出来({exc!r})")
    if not isinstance(manifest, dict):
        return _unusable(out_dir, "manifest.json 不是一个对象")

    # 核心产物:任一缺失/反序列化失败都视为损坏(broad except 是刻意的——任何
    # 读不出来的理由都指向同一个结论:这份索引不能用)。
    try:
        transition = sp.load_npz(os.path.join(out_dir, "graph.npz"))
        node_ids = list(np.load(os.path.join(out_dir, "node_ids.npy"), allow_pickle=True))
        idf = np.load(os.path.join(out_dir, "idf.npy"))
        chunk_index = np.load(os.path.join(out_dir, "chunk_index.npy"))
        ann_labels = list(np.load(os.path.join(out_dir, "ann_labels.npy"), allow_pickle=True))
    except Exception as exc:  # noqa: BLE001
        return _unusable(out_dir, f"核心产物读不出来({exc!r})")

    n_nodes = len(node_ids)
    detail = _first_mismatch([
        ("manifest.n_nodes 与 node_ids.npy 行数",
         _manifest_count(manifest, "n_nodes"), n_nodes),
        # 写入端两条路径都保证转移阵是 len(node_ids) 阶方阵:
        # build_transition_arrays 直接按 (n, n) 建,fold 走 splice_active 同样按
        # combined_ids 的长度建。冻结的 v9 老工件也满足(既有兼容用例已在断言)。
        ("graph.npz 行数与 node_ids.npy 行数", n_nodes, int(transition.shape[0])),
        ("graph.npz 列数与 node_ids.npy 行数", n_nodes, int(transition.shape[1])),
        # idf 同理:full 逐 node_id append,fold 里 fold_arrays 按 len(node_ids)
        # 开数组,两条路径都恰好一一对应。
        ("idf.npy 长度与 node_ids.npy 行数", n_nodes, int(len(idf))),
        ("manifest.n_ann 与 ann_labels.npy 行数",
         _manifest_count(manifest, "n_ann"), len(ann_labels)),
    ])
    if detail is not None:
        return _unusable(out_dir, detail)

    viz_ids = viz_adj = viz_deg = viz_types = viz_names = viz_edges = None
    chunk_ann_labels = chunk_ann_path = None
    relation_ann_labels = relation_ann_path = None
    try:
        if manifest.get("has_viz"):
            viz_npz = os.path.join(out_dir, "viz.npz")
            viz_adj_path = os.path.join(out_dir, "viz_adj.npz")
            if os.path.exists(viz_npz) and os.path.exists(viz_adj_path):
                with np.load(viz_npz, allow_pickle=True) as z:
                    viz_ids = list(z["viz_ids"])
                    viz_deg = z["viz_deg"]
                    viz_types = list(z["viz_types"])
                    viz_names = list(z["viz_names"])
                    viz_edges = decode_viz_edges(z["viz_edges"])
                viz_adj = sp.load_npz(viz_adj_path)

        if manifest.get("has_chunk_ann"):
            labpath = os.path.join(out_dir, "chunk_ann_labels.npy")
            if os.path.exists(labpath):
                chunk_ann_labels = list(np.load(labpath, allow_pickle=True))
                chunk_ann_path = os.path.join(out_dir, "chunk_ann.bin")

        if manifest.get("has_relation_ann"):
            rlabpath = os.path.join(out_dir, "relation_ann_labels.npy")
            if os.path.exists(rlabpath):
                relation_ann_labels = list(np.load(rlabpath, allow_pickle=True))
                relation_ann_path = os.path.join(out_dir, "relation_ann.bin")
    except Exception as exc:  # noqa: BLE001
        return _unusable(out_dir, f"可选产物读不出来({exc!r})")

    # 可选产物的计数与主索引出自同一次写入,失配即证明那次写入不完整——与主
    # 产物一视同仁判损坏。
    optional_checks = []
    if viz_ids is not None:
        optional_checks.append((
            "manifest.n_viz_nodes 与 viz.npz 节点数",
            _manifest_count(manifest, "n_viz_nodes"), len(viz_ids)))
    if chunk_ann_labels is not None:
        optional_checks.append((
            "manifest.n_chunk_ann 与 chunk_ann_labels.npy 行数",
            _manifest_count(manifest, "n_chunk_ann"), len(chunk_ann_labels)))
    if relation_ann_labels is not None:
        optional_checks.append((
            "manifest.n_relation_ann 与 relation_ann_labels.npy 行数",
            _manifest_count(manifest, "n_relation_ann"), len(relation_ann_labels)))
    detail = _first_mismatch(optional_checks)
    if detail is not None:
        return _unusable(out_dir, detail)

    return ScaleIndex(
        node_ids=node_ids,
        node_index={n: i for i, n in enumerate(node_ids)},
        transition=transition,
        idf=idf,
        chunk_index=chunk_index,
        ann_labels=ann_labels,
        ann_path=os.path.join(out_dir, "ann.bin"),
        manifest=manifest,
        viz_ids=viz_ids,
        viz_adj=viz_adj,
        viz_deg=viz_deg,
        viz_types=viz_types,
        viz_names=viz_names,
        viz_edges=viz_edges,
        chunk_ann_labels=chunk_ann_labels,
        chunk_ann_path=chunk_ann_path,
        relation_ann_labels=relation_ann_labels,
        relation_ann_path=relation_ann_path,
    )


def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
    stats: dict = None,
) -> "np.ndarray":
    """个性化 PageRank 幂迭代。

    transition: 列随机转移阵 A（A[j,i] = 边 i->j 的归一化权重，按 i 的出度归一）。
    reset:      personalization 向量（非负；内部归一为和=1 作为 teleport 分布）。
    返回稳态分布 x（和=1，dtype 跟随 transition：float32 输入 → float32 输出，
    float64 输入 → float64 输出）；全零 reset → 全零向量（调用方据此回退 dense）。
    stats: 可选出参 dict,写入 {"iters": 实际迭代轮数}(纯观测,不影响数值)。
    """
    s = float(reset.sum())
    if s <= 0:
        if stats is not None:
            stats["iters"] = 0
        return np.zeros(transition.shape[0], dtype=np.float64)
    # dtype 跟随转移阵(float32 时迭代全程 float32,SpMV 带宽减半);residual 求和
    # 用 float64 累积消掉求和噪声。float32 的 L1 残差噪声地板≈eps×Σ|x|≈1e-7,
    # tol 低于它会永远达不到→空转满 max_iter 反而更慢,防御 clamp 到 1e-6。
    dt = transition.dtype if transition.dtype in (np.float32, np.float64) else np.float64
    eff_tol = max(float(tol), 1e-6) if dt == np.float32 else float(tol)
    p = reset.astype(dt) / s
    x = p.copy()
    d = float(damping)
    iters = 0
    for _ in range(max_iter):
        iters += 1
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - float(x_new.sum())) * p
        if float(np.abs(x_new - x).sum(dtype=np.float64)) < eff_tol:
            x = x_new
            break
        x = x_new
    if stats is not None:
        stats["iters"] = iters
    total = float(x.sum())
    return x / total if total > 0 else x


def build_transition(
    node_ids: List[str],
    edges: List[Tuple[str, str, float]],
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """边列表 -> 列随机转移阵 A（A[j,i]=i->j 归一化权重）。

    端点不在 node_ids 的边丢弃（防悬空）。out-degree 加权归一。返回 (A_csr, index)。
    调用方负责把无向边拆成正反两条。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    if not edges:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    src = np.fromiter((index.get(s, -1) for s, _, _ in edges), dtype=np.int64, count=len(edges))
    tgt = np.fromiter((index.get(t, -1) for _, t, _ in edges), dtype=np.int64, count=len(edges))
    w = np.fromiter((float(wt) for _, _, wt in edges), dtype=np.float64, count=len(edges))
    keep = (src >= 0) & (tgt >= 0)
    src, tgt, w = src[keep], tgt[keep], w[keep]
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)   # A[j,i]=i->j
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index


def build_transition_arrays(
    node_ids: List[str],
    src: "np.ndarray",
    tgt: "np.ndarray",
    w: "np.ndarray",
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """Array fast-path of build_transition: edges already as int-indexed
    (src, tgt, w) numpy arrays (int32/int32/float32-or-float64) instead of
    (str, str, float) tuples — skips the Python-level id→index dict lookups
    per edge that build_transition does via index.get(s, -1)/index.get(t, -1)
    generator expressions (490k+ edges at scale, each a Python-object round
    trip). Caller is responsible for encoding edges against `index =
    {nid: i for i, nid in enumerate(node_ids)}` and for dropping/keeping
    dangling endpoints as it sees fit (this function does NOT re-validate
    src/tgt bounds — pass already-valid indices only). Output is element-wise
    identical to build_transition given the same node_ids and the same
    (decoded) edge set.
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    if src.size == 0:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    src64 = src.astype(np.int64, copy=False)
    tgt64 = tgt.astype(np.int64, copy=False)
    w64 = w.astype(np.float64, copy=False)
    M = sp.csr_matrix((w64, (tgt64, src64)), shape=(n, n), dtype=np.float64)  # A[j,i]=i->j
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index


def _persist_ann(out_dir, bin_name, prebuilt, vectors, n_labels, dim, ef) -> None:
    """Save one hnsw index to out_dir/bin_name. Reuse ``prebuilt`` when its
    element count matches ``n_labels`` (build() now pre-builds every ANN inline
    and frees the matrix before persist — so at scale the matrices never coexist
    with their indexes here); otherwise build from ``vectors`` (float32, may be
    None → empty index). Mirrors the frozen hnsw params (cosine / M=16 /
    random_seed=42). Never emits a row-count-mismatched .bin (defensive count
    check before trusting a prebuilt handle)."""
    import hnswlib

    idx = None
    if prebuilt is not None:
        try:
            if int(prebuilt.get_current_count()) == n_labels:
                idx = prebuilt
        except Exception:  # noqa: BLE001 — defensive: never trust a broken handle
            idx = None
    if idx is None:
        vecs = (
            np.asarray(vectors, dtype=np.float32)
            if vectors is not None and len(vectors)
            else None
        )
        # A count-mismatched (or absent) prebuilt handle with NO matrix to
        # rebuild from would silently write an EMPTY index alongside n_labels
        # labels — itself a row-count mismatch that collapses recall. Fail loud
        # rather than corrupt: build() always pre-builds from the same rows as
        # the labels, so this only fires on a genuinely inconsistent caller.
        if vecs is None and n_labels > 0:
            raise ValueError(
                f"_persist_ann({bin_name}): {n_labels} labels but no usable "
                f"vectors and no size-matching prebuilt index — refusing to "
                f"write a row-count-mismatched .bin"
            )
        n_rows = int(vecs.shape[0]) if vecs is not None else 0
        use_dim = int(vecs.shape[1]) if n_rows > 0 else dim
        if use_dim < 1:
            use_dim = 1
        idx = hnswlib.Index(space="cosine", dim=use_dim)
        idx.init_index(
            max_elements=max(1, n_rows or n_labels),
            ef_construction=ef, M=16, random_seed=42,
        )
        if n_rows > 0:
            idx.add_items(vecs, np.arange(n_rows))
    idx.save_index(os.path.join(out_dir, bin_name))


def save_scale_index(
    out_dir: str,
    *,
    node_ids: List[str],
    transition: "sp.csr_matrix",
    idf: List[float],
    chunk_index: List[int],
    ann_labels: List[str],
    manifest: dict,
    ann_vectors=None,
    viz_ids: List[str] = None,
    viz_adj: "sp.csr_matrix" = None,
    viz_deg=None,
    viz_types: List[str] = None,
    viz_names: List[str] = None,
    viz_payload: dict = None,
    chunk_ann_vectors=None,
    chunk_ann_labels: List[str] = None,
    relation_ann_vectors=None,
    relation_ann_labels: List[str] = None,
    prebuilt_ann=None,
    prebuilt_chunk_ann=None,
    prebuilt_relation_ann=None,
    ef_construction: int = 200,
) -> dict:
    """把构建好的数组落盘到 out_dir。

    ann_vectors: (m, dim) float32 numpy array（只含有 embeddings 的 KG 节点）。
    ann_labels: 对应 kg 节点 object_id 列表（与 ann_vectors 行对齐）。
    写出 7 个文件：graph.npz, node_ids.npy, idf.npy, chunk_index.npy,
    ann.bin, ann_labels.npy, manifest.json。返回传入的 manifest dict。

    relation_ann_vectors/relation_ann_labels: 镜像 chunk_ann_vectors/labels——
    只在传入 relation_ann_labels 非空时才写 relation_ann.bin +
    relation_ann_labels.npy 并置 manifest["has_relation_ann"]=True/
    n_relation_ann。旧索引（无这两个参数）不受影响——manifest 缺该键，
    load_scale_index 照常跳过，老索引继续可加载（同 has_chunk_ann 的
    older-index-stays-valid 性质）。

    当传入折叠 viz 图（viz_ids 非空）时额外写 viz.npz + viz_adj.npz 并把
    manifest["has_viz"]=True，供 unified_graph/neighbors 有界分派直接消费。

    prebuilt_ann: 调用方已建好并 add_items 完毕的 hnswlib.Index（比如
    emb_synonym_edges 那次 KNN 复用的同一个 index）。传入时跳过本函数自己的
    init_index+add_items，直接 save_index——避免 490k 节点规模下对同一批
    KG 向量重复构建 hnsw（流水线里最贵的计算）。防御性对齐检查：其元素数
    （get_current_count()）必须等于 len(ann_labels)，否则视为不可信（调用方
    传错/竞态）而回退到本函数自建，绝不静默产出行数错位的 ann.bin。
    """
    import hnswlib

    os.makedirs(out_dir, exist_ok=True)

    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(os.path.join(out_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))
    np.save(os.path.join(out_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))

    # Folded viz graph (Task 4). Persisted only when provided; manifest flag lets
    # load_scale_index skip when absent (older indexes stay valid).
    if viz_ids is not None and viz_adj is not None:
        np.savez(
            os.path.join(out_dir, "viz.npz"),
            viz_ids=np.asarray(viz_ids, dtype=object),
            viz_deg=np.asarray(viz_deg, dtype=np.int32),
            viz_types=np.asarray(viz_types, dtype=object),
            viz_names=np.asarray(viz_names, dtype=object),
            viz_edges=encode_viz_edges((viz_payload or {}).get("edges", [])),
        )
        sp.save_npz(os.path.join(out_dir, "viz_adj.npz"), viz_adj.tocsr())
        manifest = {**manifest, "has_viz": True}

    # KG dim: prefer the actual matrix width when a matrix is still passed
    # (fallback / older callers), else the manifest's built dim (build() frees
    # ann_vectors before persist now, so it is normally None here).
    dim = int(manifest.get("dim", 1))
    if ann_vectors is not None and len(ann_vectors):
        _av = np.asarray(ann_vectors, dtype=np.float32)
        if _av.shape[0] > 0:
            dim = int(_av.shape[1])
    if dim < 1:
        dim = 1

    _persist_ann(out_dir, "ann.bin", prebuilt_ann, ann_vectors,
                 len(ann_labels), dim, ef_construction)

    # Chunk-level ANN (Task 1): persisted only when chunk labels are provided;
    # reuses build()'s pre-built index (matrix already freed) or rebuilds from a
    # passed matrix. manifest flag lets load_scale_index skip when absent.
    if chunk_ann_labels:
        _persist_ann(out_dir, "chunk_ann.bin", prebuilt_chunk_ann,
                     chunk_ann_vectors, len(chunk_ann_labels), dim, ef_construction)
        np.save(os.path.join(out_dir, "chunk_ann_labels.npy"), np.asarray(chunk_ann_labels, dtype=object))
        manifest = {**manifest, "has_chunk_ann": True, "n_chunk_ann": len(chunk_ann_labels)}

    # Relation-level ANN (mirrors chunk): pre-built index or rebuild from matrix.
    if relation_ann_labels:
        _persist_ann(out_dir, "relation_ann.bin", prebuilt_relation_ann,
                     relation_ann_vectors, len(relation_ann_labels), dim, ef_construction)
        np.save(os.path.join(out_dir, "relation_ann_labels.npy"), np.asarray(relation_ann_labels, dtype=object))
        manifest = {**manifest, "has_relation_ann": True, "n_relation_ann": len(relation_ann_labels)}

    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)

    return manifest


def save_fold_core(out_dir, node_ids, transition, idf, chunk_index) -> None:
    """Persist the non-ANN fold files in their historical checkpoint order."""
    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(
        os.path.join(out_dir, "chunk_index.npy"),
        np.asarray(chunk_index, dtype=np.int32),
    )


def save_fold_ann(out_dir, index_name, labels_name, ann, labels) -> None:
    """Persist one fold ANN handle and its row-aligned object labels."""
    ann.save_index(os.path.join(out_dir, index_name))
    np.save(os.path.join(out_dir, labels_name), np.asarray(labels, dtype=object))


def copy_fold_viz(live_dir, temporary_dir) -> None:
    """Carry the optional UI-only viz artifacts into a fold directory."""
    for filename in ("viz.npz", "viz_adj.npz"):
        source = os.path.join(live_dir, filename)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(temporary_dir, filename))


def save_fold_manifest(out_dir, manifest) -> None:
    """Write the fold manifest last, immediately before the directory swap."""
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)


def csr_to_edges(
    node_ids: List[str],
    transition: "sp.csr_matrix",
) -> List[Tuple[str, str, float]]:
    """Reconstruct an edge list (src, dst, 1.0) from a column-stochastic CSR
    transition matrix.  Mirrors the base-edge reconstruction inside splice_active.
    A[target_row, source_col] → edge source->target = node_ids[col]->node_ids[row].
    """
    coo = transition.tocoo()
    return [(node_ids[i], node_ids[j], 1.0) for i, j in zip(coo.col, coo.row)]


def splice_active(
    base_ids: List[str],
    base_transition: "sp.csr_matrix",
    active_ids: List[str],
    active_edges: List[Tuple[str, str, float]],
) -> Tuple[List[str], "sp.csr_matrix"]:
    """把 active 的节点/边并入 base，按 id 合一（共享 canonical_id 自然合并）。
    返回 (combined_ids, combined_transition)。base 边从 base_transition 的稀疏结构还原
    （转移阵已列归一，权重信息有损，v1 用结构 + 权重 1.0 重算；等价测试证 top-k 稳健）。
    向量化：base 边直接复用 CSR/COO 结构数组（source=col, target=row，权重 1.0），
    active 边映射进 combined 索引空间后拼接，一次性建列随机阵，避免逐边 Python 循环。"""
    base_set = set(base_ids)
    combined_ids = list(base_ids) + [a for a in active_ids if a not in base_set]
    index = {nid: i for i, nid in enumerate(combined_ids)}
    n = len(combined_ids)
    coo = base_transition.tocoo()
    # base 结构还原:source=base_ids[col], target=base_ids[row],权重 1.0(原语义)。
    # combined 前 len(base_ids) 项与 base 对齐,索引可直接复用。
    base_src = coo.col.astype(np.int64)
    base_tgt = coo.row.astype(np.int64)
    base_w = np.ones(coo.nnz, dtype=np.float64)
    if active_edges:
        a_src = np.fromiter((index.get(s, -1) for s, _, _ in active_edges), dtype=np.int64, count=len(active_edges))
        a_tgt = np.fromiter((index.get(t, -1) for _, t, _ in active_edges), dtype=np.int64, count=len(active_edges))
        a_w = np.fromiter((float(w) for _, _, w in active_edges), dtype=np.float64, count=len(active_edges))
        keep = (a_src >= 0) & (a_tgt >= 0)
        src = np.concatenate([base_src, a_src[keep]])
        tgt = np.concatenate([base_tgt, a_tgt[keep]])
        w = np.concatenate([base_w, a_w[keep]])
    else:
        src, tgt, w = base_src, base_tgt, base_w
    if src.size == 0:
        return combined_ids, sp.csr_matrix((n, n), dtype=np.float64)
    M = sp.csr_matrix((w, (tgt, src)), shape=(n, n), dtype=np.float64)
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return combined_ids, (M @ D).tocsr()


def fold_arrays(base_node_ids, base_transition, base_idf, base_chunk_index,
                delta_node_ids, delta_edges, delta_chunk_ids, delta_idf_map):
    """把 delta splice 进 base 索引数组。base_node_ids 是 combined 的前缀,故 base
    的 chunk_index 位置不变;新节点追加在后。返回 (node_ids, transition, idf, chunk_index)。"""
    node_ids, transition = splice_active(list(base_node_ids), base_transition,
                                         list(delta_node_ids), list(delta_edges))
    base_n = len(base_node_ids)
    # idf:前缀复用 base.idf,新节点用 delta_idf_map(缺省 1.0)
    idf = np.ones(len(node_ids), dtype=np.float64)
    idf[:base_n] = np.asarray(base_idf, dtype=np.float64)[:base_n]
    for i in range(base_n, len(node_ids)):
        idf[i] = float(delta_idf_map.get(node_ids[i], 1.0))
    # chunk_index:base chunk 位置(前缀不变)+ 新 delta chunk 位置
    pos = {nid: i for i, nid in enumerate(node_ids)}
    chunk_index = list(np.asarray(base_chunk_index, dtype=np.int64)) + \
        [pos[c] for c in delta_chunk_ids if c in pos and pos[c] >= base_n]
    return node_ids, transition, idf, np.asarray(chunk_index, dtype=np.int32)


def add_items_to_ann(src_bin, dim, add_vectors, base_count):
    """load 现有 hnsw(base_count 个)→ resize 容纳 base_count+len(add)→ add_items
    (labels 从 base_count 递增)→ 返回 index(调用方 save)。add_vectors: (m,dim) float32。
    add_vectors 为空时纯 load(仍 resize 到 base_count 以保 max_elements 合法)。"""
    import hnswlib
    add = np.asarray(add_vectors, dtype=np.float32) if len(add_vectors) else None
    n_add = int(add.shape[0]) if add is not None else 0
    idx = hnswlib.Index(space="cosine", dim=dim)
    idx.load_index(src_bin, max_elements=base_count + n_add)
    if n_add:
        idx.add_items(add, np.arange(base_count, base_count + n_add))
    return idx


def viz_core(viz: dict, limit: int) -> dict:
    """折叠 viz 图取度数 top-N + 诱导边。viz: {viz_ids, viz_adj(csr), viz_deg, viz_types}。
    返回 {nodes:[{id,type,degree}], edges:[{source,target}], total_nodes, total_edges, truncated}。"""
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    total = len(ids)
    n = total if (not limit or limit <= 0) else min(limit, total)
    top = np.argsort(-deg)[:n]
    keep = set(int(i) for i in top)
    nodes = [{"id": ids[i], "type": types[i], "degree": int(deg[i])} for i in top]
    coo = adj.tocoo()
    seen = set(); edges = []
    for r, c in zip(coo.row.tolist(), coo.col.tolist()):
        if r in keep and c in keep and r < c and (r, c) not in seen:
            seen.add((r, c)); edges.append({"source": ids[r], "target": ids[c]})
    return {"nodes": nodes, "edges": edges, "total_nodes": total,
            "total_edges": int(adj.nnz // 2), "truncated": n < total}


def viz_neighbors(viz: dict, node_id: str, cap: int = 50) -> dict:
    """折叠图中 node_id 的 1-hop 邻域(≤cap),返回 {nodes,edges}。未知 id → 空。"""
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    index = {nid: i for i, nid in enumerate(ids)}
    i = index.get(node_id)
    if i is None:
        return {"nodes": [], "edges": []}
    nbr = [int(j) for j in adj.getrow(i).indices][:max(0, cap)]
    keep = [i] + nbr
    nodes = [{"id": ids[j], "type": types[j], "degree": int(deg[j])} for j in keep]
    edges = [{"source": node_id, "target": ids[j]} for j in nbr]
    return {"nodes": nodes, "edges": edges}
