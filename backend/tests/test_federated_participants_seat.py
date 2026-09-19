"""B1 -- 联邦检索的参与集访问器收口(纯重构,零行为变化)。

``_federated_retrieve_impl`` / ``_federated_retrieve_relations_impl`` /
``_federated_retrieve_elements_impl`` 曾各自直调
``self.notebooks.participant_tiers`` 再逐个过 ``notebook_in_scope``。这三处
现在全部收口到 ``_RetrievalState._retrieval_participants``,作为
PR-C 参与集覆盖的唯一座位。

本文件钉两件事:
  1. 静态扫描 ``retrieval_candidates.py``,``participant_tiers`` 的直调点必须
     恰好剩一处(访问器自身),证明三条腿真的都改道了,而不是"看起来改了"。
  2. 访问器自身的行为:确定序(active 在前、其余 MOUNT_ORDER)+ 库维度过滤
     (``notebook_in_scope``/``BaseNotebookScope``)。

既有 ``test_base_scope_federated.py`` / ``test_two_tier_federated.py`` 等端到端
套件覆盖"零行为变化"的主证据,不在本文件重复。
"""
from __future__ import annotations

import contextlib
from pathlib import Path

from tests.architecture.semantic_source import PythonSourceIndex

from app.models.source_scope import BaseNotebookScope
from app.services.retrieval_candidates import CandidateRetrievalService
from app.services.source_scope import source_scope_context


_SEAT_FILE = "app/services/retrieval_candidates.py"
_LEGS = (
    "_federated_retrieve_impl",
    "_federated_retrieve_relations_impl",
    "_federated_retrieve_elements_impl",
)


def _seat_file_index() -> PythonSourceIndex:
    """只解析这一个文件。会话级 ``python_source_index`` 会 ``ast.parse`` 整个仓库
    (千余个文件、约 3 秒、数十万条常驻记录),而这条守卫从头到尾只看一个文件;
    把全仓索引拖进每 PR 必跑的车道,是为一行过滤条件付整仓的账。"""
    path = Path(__file__).resolve().parents[1] / _SEAT_FILE
    return PythonSourceIndex.from_sources({_SEAT_FILE: path.read_text(encoding="utf-8")})


def test_three_federated_legs_read_one_seat():
    """参与集只有一个读入口,且三条联邦腿都坐在它上面。

    两个方向都要钉:
      * 负向——``self.notebooks.participant_tiers`` 在本文件只出现一处(访问器
        自身)。重构前是 3。按 *attribute* 而不是 *call* 计数:先把绑定方法存进
        局部变量再调用的写法,call 记账看不见,attribute 记账看得见。
      * 正向——三条腿各调一次 ``self._retrieval_participants``。只有负向的话,
        某条腿改回 ``participant_notebook_ids``(丢掉库维度过滤)时计数仍是 1。

    用 AST 而不是 grep:docstring 里也写着这些名字。
    """
    index = _seat_file_index()
    direct = index.attributes(target="self.notebooks.participant_tiers")
    assert sum(finding.count for finding in direct) == 1, direct
    assert all(finding.key.scope.endswith("._retrieval_participants") for finding in direct), direct

    seated = {
        finding.key.scope.rsplit(".", 1)[-1]: finding.count
        for finding in index.calls(target="self._retrieval_participants")
    }
    assert seated == {leg: 1 for leg in _LEGS}, seated


def test_the_seat_lives_on_the_shared_base_class():
    """``GraphRetrievalService`` 是 ``CandidateRetrievalService`` 的兄弟子类;联邦图
    与 PPR 的参与集也要读这个座位,所以它必须定义在两者共同的基类上。"""
    from app.services.graph_retrieval import GraphRetrievalService
    from app.services.retrieval_candidates import _RetrievalState

    assert "_retrieval_participants" in vars(_RetrievalState)
    assert GraphRetrievalService._retrieval_participants is _RetrievalState._retrieval_participants


class _FakeNotebooks:
    """最小的 notebooks 端口替身:只实现 participant_tiers。"""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = list(pairs)

    def participant_tiers(self, db, active_notebook_id: str):
        assert active_notebook_id == self._pairs[0][0], (
            "resolve_participants 的首项恒为 active 本身,替身必须照抄这个约定"
        )
        notebook_ids = [nid for nid, _tier in self._pairs]
        tier_map = dict(self._pairs)
        return notebook_ids, tier_map


class _FakeCandidates:
    """不接真数据库:``_connect`` 只需要能被 ``with`` 住即可,
    ``_FakeNotebooks.participant_tiers`` 从不读它传入的 ``db``。"""

    def __init__(self, notebooks: _FakeNotebooks) -> None:
        self.notebooks = notebooks

    def _connect(self):
        return contextlib.nullcontext(None)


def test_seat_preserves_mount_order_and_scope_filter():
    """active + 3 个 base,库维度勾选排除中间那个 -> 只剩 (active, b1, b3)。

    顺序与 tier 都要对:确定序是 active 在前、其余按 MOUNT_ORDER(这里就是
    ``_FakeNotebooks`` 构造时给的顺序,与 ``resolve_participants`` 的 SQL 输出
    顺序同源)。
    """
    active, b1, b2, b3 = "active-nb", "base-1", "base-2", "base-3"
    notebooks = _FakeNotebooks([
        (active, "personal"),
        (b1, "base"),
        (b2, "base"),
        (b3, "base"),
    ])
    candidates = _FakeCandidates(notebooks)

    # 无 scope 时四本全在,顺序不变 —— 这是"零行为变化"的基线,先钉住它,否则
    # 下面的排除断言证明不了任何东西。
    unscoped = CandidateRetrievalService._retrieval_participants(candidates, active)
    assert unscoped == (
        (active, "personal"), (b1, "base"), (b2, "base"), (b3, "base"),
    )

    # 库维度勾选排除 b2:BaseNotebookScope 的 include 模式只声明"仍然勾选着
    # 的库",与 source_routes 里把复选框冻结成这个模型的路径一致。
    with source_scope_context(
        active, None, BaseNotebookScope(mode="include", notebook_ids=[b1, b3]),
    ):
        scoped = CandidateRetrievalService._retrieval_participants(candidates, active)

    assert scoped == ((active, "personal"), (b1, "base"), (b3, "base"))
