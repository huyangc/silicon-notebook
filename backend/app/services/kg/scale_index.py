"""规模化 KG 检索的紧凑基底：scipy CSR 图 + 个性化 PPR + active 拼接 + 构建/加载。

设计见 docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md。
本模块尽量纯函数、可单测；DB/IO 由 sqlite_repository 包装层提供数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import scipy.sparse as sp


def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> "np.ndarray":
    """个性化 PageRank 幂迭代。

    transition: 列随机转移阵 A（A[j,i] = 边 i->j 的归一化权重，按 i 的出度归一）。
    reset:      personalization 向量（非负；内部归一为和=1 作为 teleport 分布）。
    返回稳态分布 x（和=1）；全零 reset → 全零向量（调用方据此回退 dense）。
    """
    s = float(reset.sum())
    if s <= 0:
        return np.zeros(transition.shape[0], dtype=np.float64)
    p = (reset.astype(np.float64) / s)
    x = p.copy()
    d = float(damping)
    for _ in range(max_iter):
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - x_new.sum()) * p
        if np.abs(x_new - x).sum() < tol:
            x = x_new
            break
        x = x_new
    total = x.sum()
    return x / total if total > 0 else x
