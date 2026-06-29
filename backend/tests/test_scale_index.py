import numpy as np
import scipy.sparse as sp
from app.services.kg.scale_index import personalized_ppr


def _line_graph_transition():
    # 三节点有向链 0->1->2，列随机转移阵 A（A[j,i]=i->j 的归一化权重）
    A = sp.csr_matrix(np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]))
    return A


def test_personalized_ppr_concentrates_mass_near_seed():
    A = _line_graph_transition()
    reset = np.array([1.0, 0.0, 0.0])
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-10, max_iter=200)
    assert x.shape == (3,)
    assert abs(x.sum() - 1.0) < 1e-6
    assert x[0] > x[1] > x[2]


def test_personalized_ppr_empty_reset_returns_zeros():
    A = _line_graph_transition()
    x = personalized_ppr(A, np.zeros(3), damping=0.5)
    assert np.allclose(x, 0.0)
