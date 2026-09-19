"""``knowledge_context`` 的 definition / snippet / 引用必须过**来源**天花板。

被关的缺口(评审 A1):``_admit`` 刻意不读 ``hit.evidence``(``source_scope.py``
的 knowledge 支逐字写明了理由——清空证据不够,对象名照样会渲染并铸一个活的
``k{n}`` 锚点),改按 ``node_context(origin, object_id)`` 重查。可那条重查坐在
``GraphRetrievalService.node_context`` 这个**刻意不设闸**的座位上,它按
``knowledge_objects.evidence`` 整列返回 occurrences,``occurrences[0]`` 同时决定:

* 提示词里那句 ``— def: <原文>``(definition 缺席时兜底成 snippet);
* 引用卡的 ``source_id`` / ``element_id`` / ``source_title`` / ``location_label``。

于是一个对象可以凭天花板**内**的那条证据被合法召回(``filter_retrieval_items``
的 knowledge 支只需要一条幸存证据),却把天花板**外**那条的原文写进提示词、
并让引用指向一个用户已经取消勾选的来源。

⚠ 这不是联邦专有,**单库的 LOCAL 勾选天花板今天线上就能触发**(第一组用例),
所以它是一条对既有线上行为的修复,不只是「第一个写入方之前必须关掉」。

PR-C 的 ``RetrievalService.node_context`` 没有覆盖到这里:那一层是 reasoning 的
链路补水专用,``evidence_context`` 直连 graph 服务,根本不经过它(两处的
docstring 互相点名了这个分工)。

**变异锚点**:删掉 ``_admit`` 里那次 ``filter_evidence`` → 第 1/2/3 组全红;
把 fail-closed 的 ``continue`` 改成回落未过滤的 occurrences → 第 4 组红。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedKnowledge
from app.services.evidence_context import EvidenceContextService
from app.services.source_scope import source_scope_context


ACTIVE = "nbA"
PEER = "nbB"

OPEN_SOURCE = "doc-open"        # 天花板之内
HIDDEN_SOURCE = "mem-hidden"    # 天花板之外(隐藏 Memory 投影的替身)

OPEN_TEXT = "公开原文:这条可以进提示词"
HIDDEN_TEXT = "私密原文:这条一个字都不许进提示词"
OBJECT_NAME = "被两条来源共同支撑的对象"


class _Notebooks:
    def tier_map(self, notebook_ids):
        return {
            notebook_id: ("personal" if notebook_id == ACTIVE else "base")
            for notebook_id in notebook_ids
        }

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id, PEER]


class _Sources:
    def evidence_elements(self, element_ids):
        return {}

    def source_metadata(self, source_ids):
        return {
            source_id: {
                "title": source_id, "file_name": source_id,
                "notebook_id": ACTIVE, "is_paper": False, "paper_title": "",
            }
            for source_id in source_ids if source_id
        }


def _occurrence(source_id: str, text: str) -> dict:
    """``_enrich_evidence`` 交回的那种形状(element 行已命中时的完整列)。"""
    return {
        "source_id": source_id,
        "element_id": f"el-{source_id}",
        "element_type": "paragraph",
        "location_label": "p1",
        "section_path": f"§{source_id}",
        "element_text": text,
        "quoted_span": text,
        "source_title": source_id,
    }


class _Knowledge:
    """``node_context`` 按整列 evidence 返回 —— 与真实 store 逐字同形。

    真实实现(``{sqlite,postgres}/knowledge_store.py::node_context``)对
    ``occurrences = _enrich_evidence(整条 evidence)`` 不看任何 scope,所以这个
    假体**必须**照样不看:一旦在这里先过滤一遍,用例就永远绿,被测的那道闸
    是否存在完全不影响结果。
    """

    def __init__(self, occurrences, *, definition=None):
        self.occurrences = list(occurrences)
        self.definition = definition
        self.node_context_calls: list[tuple[str, str]] = []

    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id):
        self.node_context_calls.append((notebook_id, object_id))
        return {
            "id": object_id,
            "occurrences": [dict(row) for row in self.occurrences],
            "definition": self.definition,
            "steps": None,
        }

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 1 for triple in triples}


def _service(knowledge: _Knowledge) -> EvidenceContextService:
    return EvidenceContextService(
        notebooks=_Notebooks(), sources=_Sources(),
        knowledge=knowledge, settings=Settings(),
    )


def _hit(notebook_id: str) -> RetrievedKnowledge:
    """一条**已经过 ``filter_retrieval_items`` 复核**的命中。

    ``evidence`` 只剩天花板内那条(那道复核干的就是这件事),正是这一点让对象
    合法进入 ``knowledge_context``——而本文件钉的是它之后那次重查。
    """
    return RetrievedKnowledge(
        object_id="ko-mixed", object_type="concept",
        payload={"name": OBJECT_NAME}, evidence=[],
        notebook_id=notebook_id, tier="personal", relevance=0.9,
    )


def _both_sources_knowledge(**kwargs) -> _Knowledge:
    # 隐藏那条排在**前面**:未加闸时 ``occurrences[0]`` 恰好是它。
    return _Knowledge(
        [_occurrence(HIDDEN_SOURCE, HIDDEN_TEXT),
         _occurrence(OPEN_SOURCE, OPEN_TEXT)],
        **kwargs,
    )


def _assert_only_open_source(block: str, id_map: dict) -> None:
    assert HIDDEN_TEXT not in block
    assert OPEN_TEXT in block
    assert OBJECT_NAME in block, "对象本身是合法召回的,不能被连坐丢掉"
    assert len(id_map) == 1
    (entry,) = id_map.values()
    assert entry["source_id"] == OPEN_SOURCE
    assert entry["element_id"] == f"el-{OPEN_SOURCE}"
    assert entry["snippet"] == OPEN_TEXT
    assert entry["definition"] == OPEN_TEXT
    assert entry["location_label"] == f"§{OPEN_SOURCE}"
    assert entry["source_title"] == OPEN_SOURCE


# --------------------------------------------------------------------------- #
# 1. LOCAL 勾选天花板 —— 今天线上就在用的那一种
# --------------------------------------------------------------------------- #
def test_local_source_ceiling_gates_definition_snippet_and_citation():
    """用户取消勾选某来源后,该来源的元素正文不得作为 KG 对象的定义出现。

    ``narrowed=True`` 是浏览器真实收窄时边界算出来的那个位:它只决定「走哪条
    通道」,天花板本身由 mode/source_ids 表达——两者都在,正是线上形态。
    """
    knowledge = _both_sources_knowledge()
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE,
        {"mode": "include", "source_ids": [OPEN_SOURCE], "narrowed": True},
        None,
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    _assert_only_open_source(block, id_map)


def test_local_exclude_ceiling_gates_the_same_way():
    """``exclude:[被取消勾选的那篇]`` 是同一件事的另一种表达形态。"""
    knowledge = _both_sources_knowledge()
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE,
        {"mode": "exclude", "source_ids": [HIDDEN_SOURCE], "narrowed": True},
        None,
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    _assert_only_open_source(block, id_map)


# --------------------------------------------------------------------------- #
# 2/3. 逐库冻结天花板:名义 active 的那一份,以及 peer 库对象的那一份
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origin", [ACTIVE, PEER])
def test_per_notebook_ceiling_gates_definition_snippet_and_citation(origin):
    """每个参与库各自一份冻结清单;判据必须是**对象自己那一库**的那一份。

    peer 那一半尤其要命:``allows()`` 的本地支对任何 peer 恒放行,所以只按名义
    active 提问的写法在 peer 上完全不设防。
    """
    knowledge = _both_sources_knowledge()
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={
            ACTIVE: frozenset({OPEN_SOURCE}),
            PEER: frozenset({OPEN_SOURCE}),
        },
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_hit(origin)])
    _assert_only_open_source(block, id_map)
    assert knowledge.node_context_calls == [(origin, "ko-mixed")], (
        "重查仍按对象自己的归属库发,闸只作用在它的结果上"
    )


def test_a_library_without_a_ceiling_is_untouched():
    """别的库有天花板、这一库没有 → 该库对象逐值不变(``is not None`` 分支)。"""
    baseline_service = _service(_both_sources_knowledge())
    baseline = baseline_service.knowledge_context(ACTIVE, [_hit(PEER)])
    service = _service(_both_sources_knowledge())
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={ACTIVE: frozenset({OPEN_SOURCE})},
    ):
        scoped = service.knowledge_context(ACTIVE, [_hit(PEER)])
    assert scoped == baseline
    assert HIDDEN_TEXT in scoped[0]


# --------------------------------------------------------------------------- #
# 4. FAIL-CLOSED:闸把整列清空时不回落,整条不 admit
# --------------------------------------------------------------------------- #
def test_object_whose_every_occurrence_is_out_of_ceiling_is_dropped():
    """与 ``filter_retrieval_items`` 的 knowledge 支同口径:整条丢掉。

    只清空 snippet/definition 不够——对象名照样渲染成一行、照样铸一个活的
    ``k{n}`` 锚点(``_admit`` 上面那个 ``notebook_in_scope`` 分支整条跳过,理由
    逐字相同)。回落到未过滤的 occurrences 更是把闸直接取消。
    """
    knowledge = _Knowledge([_occurrence(HIDDEN_SOURCE, HIDDEN_TEXT)])
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={ACTIVE: frozenset({OPEN_SOURCE})},
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    # 与「一条命中都没有」逐字相同的那个空块(本函数既有的空态文案)。
    assert block == _service(_Knowledge([])).knowledge_context(ACTIVE, [])[0]
    assert id_map == {}
    assert OBJECT_NAME not in block
    assert HIDDEN_TEXT not in block


def test_an_object_with_no_occurrences_at_all_still_renders():
    """空列 ≠ 被闸清空:本来就没有 occurrences 的对象保持既有行为。

    这条守住 fail-closed 的边界——判据是「非空被清空」,不是「空」。
    """
    knowledge = _Knowledge([], definition="对象级描述")
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={ACTIVE: frozenset({OPEN_SOURCE})},
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert OBJECT_NAME in block
    assert "对象级描述" in block
    assert len(id_map) == 1


# --------------------------------------------------------------------------- #
# 5. 缺席时逐值不变
# --------------------------------------------------------------------------- #
def test_absent_ceiling_is_value_identical():
    """无 scope / 有 scope 但无任何天花板 → 与今天逐值相同(含隐藏那条)。"""
    bare = _service(_both_sources_knowledge()).knowledge_context(
        ACTIVE, [_hit(ACTIVE)])
    with source_scope_context(ACTIVE, None, None):
        empty_scope = _service(_both_sources_knowledge()).knowledge_context(
            ACTIVE, [_hit(ACTIVE)])
    assert empty_scope == bare
    # 对照臂:未加闸时进提示词的确实是隐藏那条 —— 上面的断言不是凭空成立。
    assert HIDDEN_TEXT in bare[0]
    (entry,) = bare[1].values()
    assert entry["source_id"] == HIDDEN_SOURCE
