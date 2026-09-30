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

``evidence_context`` 直连 graph 服务、不经过 ``RetrievalService.node_context``,
但两处对重查回来的行套用**同一条**裁决 ``evidence_context.scoped_node_context_row``。

PR-A·A3 起的两层:① ``origin`` 那一库的天花板经 ``allowed_source_ids`` 下推给
store(只在非 None 时传),store 过滤 occurrences、definition 两条产出路径与
legacy steps;② 服务层按同一天花板复核(兜底一个忽略了参数的 store)。

**变异锚点**:删掉服务层 occurrences 复核 → 第 1/2/3 组全红;把 fail-closed 的
``continue`` 改成回落未过滤的 occurrences → 第 4 组红;不再下推
``allowed_source_ids`` → 第 6 组(``_Store`` 那些)红;删掉 definition 归因兜底
→ 第 7 组红。
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
    """一个**忽略** ``allowed_source_ids`` 的 store —— 服务层复核的试金石。

    真实 store 自 PR-A·A1 起会按参数过滤;这个假体刻意不看它,于是它交回的就是
    「store 没过滤」时服务层要面对的那一行:一旦在这里先过滤一遍,用例就永远绿,
    被测的那道服务层闸是否存在完全不影响结果。下推本身由 ``_Store`` 钉。
    """

    def __init__(self, occurrences, *, definition=None,
                 definition_basis=None, definition_source_id=None, steps=None):
        self.occurrences = list(occurrences)
        self.definition = definition
        self.definition_basis = definition_basis
        self.definition_source_id = definition_source_id
        self.steps = steps
        self.node_context_calls: list[tuple[str, str]] = []
        self.pushed: list[object] = []

    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id, **kwargs):
        self.node_context_calls.append((notebook_id, object_id))
        # 缺席 ≠ None:没有天花板时参数必须**根本不传**(逐字节不变)。
        self.pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        return {
            "id": object_id,
            "occurrences": [dict(row) for row in self.occurrences],
            "definition": self.definition,
            "definition_basis": self.definition_basis,
            "definition_source_id": self.definition_source_id,
            "definition_element_id": (
                f"el-{self.definition_source_id}"
                if self.definition_source_id else None
            ),
            "steps": [dict(step) for step in self.steps] if self.steps else self.steps,
        }

    def in_network_relations(self, participant_ids, object_ids, *, source_ceilings=None):
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


def test_an_object_with_no_occurrences_at_all_renders_only_without_a_ceiling():
    """无证据对象:没有天花板绑住它那一库时照旧渲染;天花板绑住时整条丢。

    天花板绑住时它无从归因到任何天花板内来源,与 ``filter_retrieval_items`` 的
    knowledge 支同口径丢掉。另一个理由是结构性的:store 按天花板过滤过的空列与
    「本来就没有证据」无从区分,只按「非空被清空」判,被 store 清空的对象会连名
    带锚点漏过去(见 ``test_store_emptied_object_is_dropped``)。
    """
    def knowledge():
        return _Knowledge([], definition="对象级描述",
                          definition_basis="cluster_description")

    block, id_map = _service(knowledge()).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert OBJECT_NAME in block and "对象级描述" in block
    assert len(id_map) == 1

    # 别的库有天花板、这一库没有 → 仍渲染(判据是**对象自己那一库**)。
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={PEER: frozenset({OPEN_SOURCE})},
    ):
        block, id_map = _service(knowledge()).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert OBJECT_NAME in block and len(id_map) == 1

    for scope_args in (
        {"notebook_source_ceilings": {ACTIVE: frozenset({OPEN_SOURCE})}},
        {"scope": {"mode": "include", "source_ids": [OPEN_SOURCE], "narrowed": True}},
        {"scope": {"mode": "exclude", "source_ids": [HIDDEN_SOURCE], "narrowed": True}},
    ):
        with source_scope_context(
            ACTIVE, scope_args.get("scope"), None,
            notebook_source_ceilings=scope_args.get("notebook_source_ceilings"),
        ):
            block, id_map = _service(knowledge()).knowledge_context(
                ACTIVE, [_hit(ACTIVE)])
        assert id_map == {}, scope_args
        assert OBJECT_NAME not in block and "对象级描述" not in block


# --------------------------------------------------------------------------- #
# 5. 缺席时逐值不变
# --------------------------------------------------------------------------- #
def test_absent_ceiling_is_value_identical():
    """无 scope / 有 scope 但无任何天花板 → 与今天逐值相同(含隐藏那条)。

    钉的是**原语**在无 scope 时不动。生产不可达:每个问答入口都经
    ``AskService._retrieval_ceiling`` 装默认天花板,由
    ``test_default_ceiling_guard.py`` 保证。"""
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


# --------------------------------------------------------------------------- #
# 6. 下推:天花板经 ``allowed_source_ids`` 交给 store(PR-A·A3)
# --------------------------------------------------------------------------- #
DEFINE_OUT_TEXT = "越界定义原文:排在第一条 defines"
DEFINE_IN_TEXT = "界内定义原文:排在第二条 defines"
CLUSTER_TEXT = "簇融合描述"
DEFINER_NAME = "定义者对象名"


class _Store(_Knowledge):
    """按真实 store 的 ``allowed_source_ids`` 语义行事的替身(PR-A·A1 契约)。

    * ``cluster``:``(描述, 全部成员的出处来源)``;Q1 严格谓词——任一成员来源在
      天花板外就不用描述。
    * ``defines``:按 ``r.id`` 排好的 ``(source_id, text)``;无天花板取第一条,
      有天花板取第一条界内的。
    * ``defines_name``:定义者没有证据时的名字回落;天花板生效时不返回。
    * ``steps``:``(步骤名, 支撑来源)``;天花板生效时只留过谓词的兄弟过程。

    不传参数时逐值同「没有天花板」,于是「服务层没下推」在这里表现为越界内容
    原样交回——服务层兜底能拦住的(``defines_*`` 归因)与拦不住的(簇描述、
    steps 在天花板下推时由 store 负责)都在下面各钉一条。
    """

    def __init__(self, occurrences, *, cluster=None, defines=(),
                 defines_name=None, steps=None):
        super().__init__(occurrences)
        self.cluster = cluster
        self.defines = list(defines)
        self.defines_name = defines_name
        self.store_steps = steps

    def node_context(self, notebook_id, object_id, **kwargs):
        self.node_context_calls.append((notebook_id, object_id))
        self.pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        raw = kwargs.get("allowed_source_ids")
        allowed = None if raw is None else set(raw)

        def ok(source_id):
            return allowed is None or source_id in allowed

        row = {
            "id": object_id,
            "occurrences": [dict(o) for o in self.occurrences if ok(o["source_id"])],
            "definition": None, "definition_basis": None,
            "definition_source_id": None, "definition_element_id": None,
            "steps": None,
        }
        if self.cluster and all(ok(s) for s in self.cluster[1]):
            row.update(definition=self.cluster[0], definition_basis="cluster_description")
        else:
            pick = (self.defines[:1] if allowed is None
                    else [d for d in self.defines if ok(d[0])][:1])
            if pick:
                source_id, text = pick[0]
                row.update(definition=text, definition_basis="defines_evidence",
                           definition_source_id=source_id,
                           definition_element_id=f"el-def-{source_id}")
            elif allowed is None and self.defines_name:
                row.update(definition=self.defines_name, definition_basis="defines_name")
        if self.store_steps is not None:
            # 天花板下,支撑元素越界(或已不存在、无从归因)的步骤**整条**不返回
            # ——名字与原文一起,不是只清空原文。
            row["steps"] = [
                {"name": name, "element_text": f"{name}的原文", "section_path": ""}
                for name, source_id in self.store_steps if ok(source_id)
            ]
        return row


def _include_open():
    return source_scope_context(
        ACTIVE,
        {"mode": "include", "source_ids": [OPEN_SOURCE], "narrowed": True},
        None,
    )


def test_no_ceiling_passes_no_kwarg_at_all():
    """无天花板 → 不传 ``allowed_source_ids``(``None`` 也不传):逐字节不变。"""
    store = _Store([_occurrence(OPEN_SOURCE, OPEN_TEXT)])
    _service(store).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    with source_scope_context(ACTIVE, None, None):
        _service(store).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    with source_scope_context(
        ACTIVE, None, None, notebook_source_ceilings={PEER: frozenset({OPEN_SOURCE})},
    ):
        _service(store).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert store.pushed == ["<absent>"] * 3


def test_defines_evidence_falls_through_to_the_first_in_ceiling_definer():
    """第一条 defines 的证据越界 → 用第二条界内的,prompt/锚点/id_map 一致。"""
    store = _Store(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        defines=[(HIDDEN_SOURCE, DEFINE_OUT_TEXT), (OPEN_SOURCE, DEFINE_IN_TEXT)],
    )
    service = _service(store)
    with _include_open():
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert store.pushed == [frozenset({OPEN_SOURCE})]
    assert f"— def: {DEFINE_IN_TEXT}" in block
    assert DEFINE_OUT_TEXT not in block
    (key, entry), = id_map.items()
    assert entry["definition"] == DEFINE_IN_TEXT
    (anchor,) = service.parse_anchors(f"见 [{key}]", id_map)
    assert anchor.definition == DEFINE_IN_TEXT

    # 对照臂:无天花板时 store 交回的是第一条(越界那条)——上面不是凭空成立。
    plain_block, _ = _service(_Store(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        defines=[(HIDDEN_SOURCE, DEFINE_OUT_TEXT), (OPEN_SOURCE, DEFINE_IN_TEXT)],
    )).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert DEFINE_OUT_TEXT in plain_block


def test_defines_name_is_not_used_under_a_ceiling():
    """``defines_name`` 无从归因:天花板生效时回落到界内出处原文。"""
    def store():
        return _Store([_occurrence(OPEN_SOURCE, OPEN_TEXT)], defines_name=DEFINER_NAME)

    assert DEFINER_NAME in _service(store()).knowledge_context(
        ACTIVE, [_hit(ACTIVE)])[0]
    with _include_open():
        block, id_map = _service(store()).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert DEFINER_NAME not in block
    (entry,) = id_map.values()
    assert entry["definition"] == OPEN_TEXT


@pytest.mark.parametrize("members, expect_cluster", [
    ((OPEN_SOURCE, HIDDEN_SOURCE), False),   # 混合簇:Q1 严格 → 不用
    ((OPEN_SOURCE,), True),                  # 全内簇:可用
])
def test_cluster_description_follows_the_strict_member_predicate(members, expect_cluster):
    store = _Store(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        cluster=(CLUSTER_TEXT, members),
        defines=[(OPEN_SOURCE, DEFINE_IN_TEXT)],
    )
    with _include_open():
        block, id_map = _service(store).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    (entry,) = id_map.values()
    if expect_cluster:
        assert f"— def: {CLUSTER_TEXT}" in block
        assert entry["definition"] == CLUSTER_TEXT
    else:
        assert CLUSTER_TEXT not in block
        assert entry["definition"] == DEFINE_IN_TEXT


def test_peer_object_pushes_the_peer_librarys_own_ceiling():
    """对等/挂载库命中按**那一库**的天花板下推,不是名义 active 的。"""
    store = _Store(
        [_occurrence(HIDDEN_SOURCE, HIDDEN_TEXT), _occurrence("peer-open", "参考库界内原文")],
        defines=[(HIDDEN_SOURCE, DEFINE_OUT_TEXT), ("peer-open", DEFINE_IN_TEXT)],
    )
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={
            ACTIVE: frozenset({OPEN_SOURCE}),
            PEER: frozenset({"peer-open"}),
        },
    ):
        block, id_map = _service(store).knowledge_context(ACTIVE, [_hit(PEER)])
    assert store.node_context_calls == [(PEER, "ko-mixed")]
    assert store.pushed == [frozenset({"peer-open"})]
    assert DEFINE_IN_TEXT in block
    assert HIDDEN_TEXT not in block and DEFINE_OUT_TEXT not in block
    (entry,) = id_map.values()
    assert entry["source_id"] == "peer-open"


def test_steps_only_render_in_ceiling_siblings():
    """steps 由 store 按天花板过滤(越界步骤名字与原文一起不返回);服务层只拼
    store 交回的步骤名,不回填任何名字或文字。"""
    store = _Store(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        steps=[("界内步骤", OPEN_SOURCE), ("越界步骤", HIDDEN_SOURCE)],
    )
    with _include_open():
        block, id_map = _service(store).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert block.count("; steps: 界内步骤") == 1
    assert "; steps: 界内步骤 ->" not in block, "越界步骤不占位"
    rendered = block + repr(id_map)
    assert "越界步骤" not in rendered, "越界步骤的名字不进 prompt/id_map"
    assert "越界步骤的原文" not in rendered, "越界步骤的原文不进 prompt/id_map"

    # 对照臂:无天花板时 store 交回两步,两步名字都渲染。
    plain_block, _ = _service(_Store(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        steps=[("界内步骤", OPEN_SOURCE), ("越界步骤", HIDDEN_SOURCE)],
    )).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert "; steps: 界内步骤 -> 越界步骤" in plain_block


def test_store_emptied_object_is_dropped():
    """store 按天花板把唯一出处过滤掉 → 交回空列;对象整条不 admit。

    只按「非空被清空」判时这里会放行(store 交回的本来就是空列),对象名和锚点
    照样进 prompt —— 这正是下推之后那条判据必须改成「天花板绑住且无幸存」的原因。
    """
    store = _Store([_occurrence(HIDDEN_SOURCE, HIDDEN_TEXT)],
                   cluster=(CLUSTER_TEXT, (OPEN_SOURCE,)),
                   steps=[("界内步骤", OPEN_SOURCE)])
    service = _service(store)
    with _include_open():
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert store.pushed == [frozenset({OPEN_SOURCE})]
    assert id_map == {}, "不铸锚点、不产生空 source_id 的引用"
    assert OBJECT_NAME not in block and CLUSTER_TEXT not in block
    assert "界内步骤" not in block, "整条丢:步骤名也不渲染"
    assert service.parse_anchors("见 [k1]", id_map) == []
    assert block == _service(_Knowledge([])).knowledge_context(ACTIVE, [])[0]


# --------------------------------------------------------------------------- #
# 7. 服务层兜底:store 忽略了参数时,definition 仍不越界
# --------------------------------------------------------------------------- #
def test_backstop_drops_an_out_of_ceiling_defines_evidence_from_a_store_ignoring_the_kwarg():
    """变异锚点:删掉 ``scoped_node_context_row`` 的 definition 归因 → 本条红。"""
    knowledge = _Knowledge(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        definition=DEFINE_OUT_TEXT, definition_basis="defines_evidence",
        definition_source_id=HIDDEN_SOURCE,
    )
    service = _service(knowledge)
    with _include_open():
        block, id_map = service.knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert knowledge.pushed == [frozenset({OPEN_SOURCE})], "参数照传,是 store 没理它"
    assert DEFINE_OUT_TEXT not in block
    (key, entry), = id_map.items()
    assert entry["definition"] == OPEN_TEXT, "回落到界内出处原文"
    (anchor,) = service.parse_anchors(f"见 [{key}]", id_map)
    assert anchor.definition == OPEN_TEXT


@pytest.mark.parametrize("basis", ["defines_name", None, "unknown_basis"])
def test_backstop_drops_unattributable_definitions(basis):
    knowledge = _Knowledge([_occurrence(OPEN_SOURCE, OPEN_TEXT)],
                           definition=DEFINER_NAME, definition_basis=basis)
    with _include_open():
        block, id_map = _service(knowledge).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert DEFINER_NAME not in block
    (entry,) = id_map.values()
    assert entry["definition"] == OPEN_TEXT


def test_exclude_ceiling_cannot_be_pushed_so_cluster_description_and_steps_drop():
    """LOCAL ``exclude`` 形态没有物化清单,天花板下推不了:store 没判过的簇描述与
    steps 服务层无从归因,一并丢掉;界内 ``defines_evidence`` 照常可用。"""
    knowledge = _Knowledge(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        definition=CLUSTER_TEXT, definition_basis="cluster_description",
        steps=[{"name": "某步骤", "element_text": "", "section_path": ""}],
    )
    with source_scope_context(
        ACTIVE, {"mode": "exclude", "source_ids": [HIDDEN_SOURCE], "narrowed": True}, None,
    ):
        block, id_map = _service(knowledge).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert knowledge.pushed == ["<absent>"]
    assert CLUSTER_TEXT not in block and "某步骤" not in block
    (entry,) = id_map.values()
    assert entry["definition"] == OPEN_TEXT

    in_ceiling = _Knowledge(
        [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        definition=DEFINE_IN_TEXT, definition_basis="defines_evidence",
        definition_source_id=OPEN_SOURCE,
    )
    with source_scope_context(
        ACTIVE, {"mode": "exclude", "source_ids": [HIDDEN_SOURCE], "narrowed": True}, None,
    ):
        block, _ = _service(in_ceiling).knowledge_context(ACTIVE, [_hit(ACTIVE)])
    assert f"— def: {DEFINE_IN_TEXT}" in block


# --------------------------------------------------------------------------- #
# 9. 天花板只绑它自己那一库;冻结成空集是显式拒绝;簇去重只算真被接纳的成员
# --------------------------------------------------------------------------- #
def _peer_hit(object_id="ko-peer"):
    return RetrievedKnowledge(
        object_id=object_id, object_type="concept",
        payload={"name": OBJECT_NAME}, evidence=[],
        notebook_id=PEER, tier="base", relevance=0.9,
    )


def _peer_knowledge():
    """参考库对象:有簇描述、有 steps、出处来自参考库自己的来源(本地天花板之外
    的 id,但本地天花板本来就不管参考库)。"""
    return _Knowledge(
        [_occurrence("peer-src", "参考库原文")],
        definition=CLUSTER_TEXT, definition_basis="cluster_description",
        steps=[{"name": "参考库步骤", "element_text": "", "section_path": ""}],
    )


def test_a_local_ceiling_leaves_a_peer_library_value_identical():
    """本地勾选天花板(include、收窄)只绑本库:参考库命中与无 scope 时逐值相同
    (簇描述与 steps 都在,不下推参数)。**变异锚点**:让本地天花板也绑住参考库
    → 簇描述与 steps 被当成「没下推、无从归因」清掉,本条红。"""
    bare = _service(_peer_knowledge()).knowledge_context(ACTIVE, [_peer_hit()])
    knowledge = _peer_knowledge()
    with _include_open():
        scoped = _service(knowledge).knowledge_context(ACTIVE, [_peer_hit()])
    assert scoped == bare
    assert f"— def: {CLUSTER_TEXT}" in bare[0] and "steps: 参考库步骤" in bare[0]
    assert knowledge.pushed == ["<absent>"]


def test_a_peer_frozen_to_zero_sources_is_dropped_whole():
    """参考库被冻结成 ``frozenset()``(显式全部拒绝):对象整条不 admit——没有名字、
    没有锚点;空集照样下推。**变异锚点**:``is not None`` 换成真值判断 → 空集被当成
    没有天花板,对象原样渲染,本条红。"""
    knowledge = _peer_knowledge()
    service = _service(knowledge)
    with source_scope_context(
        ACTIVE, None, None, notebook_source_ceilings={PEER: frozenset()},
    ):
        block, id_map = service.knowledge_context(ACTIVE, [_peer_hit()])
    assert knowledge.pushed == [frozenset()]
    assert id_map == {} and OBJECT_NAME not in block
    assert service.parse_anchors("见 [k1]", id_map) == []


class _ClusterKnowledge(_Knowledge):
    """两个命中同簇(``cluster_fold`` 都折到 ``K``),各有各的出处。"""

    def __init__(self, occurrences_by_object):
        super().__init__([])
        self.by_object = occurrences_by_object

    def cluster_fold(self, notebook_id, object_ids):
        return {object_id: "K" for object_id in object_ids if object_id in self.by_object}

    def node_context(self, notebook_id, object_id, **kwargs):
        self.occurrences = self.by_object[object_id]
        return super().node_context(notebook_id, object_id, **kwargs)


def _cluster_hit(object_id):
    return RetrievedKnowledge(
        object_id=object_id, object_type="concept",
        payload={"name": object_id}, evidence=[],
        notebook_id=ACTIVE, tier="personal", relevance=0.9,
    )


def test_a_cluster_is_marked_seen_only_when_a_member_is_admitted():
    """同簇两个命中:第一个只有天花板外出处(整条丢掉),第二个有天花板内出处。
    簇只在有成员真被接纳时才记为已见,所以第二个顶上、拿到锚点;无天花板时照旧
    第一个成员胜出、第二个被去重。"""
    def knowledge():
        return _ClusterKnowledge({
            "ko-first": [_occurrence(HIDDEN_SOURCE, HIDDEN_TEXT)],
            "ko-second": [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        })

    hits = [_cluster_hit("ko-first"), _cluster_hit("ko-second")]
    with _include_open():
        block, id_map = _service(knowledge()).knowledge_context(ACTIVE, hits)
    assert [entry["object_id"] for entry in id_map.values()] == ["ko-second"]
    assert list(id_map) == ["k1"] and "k1: [concept][personal] ko-second" in block
    assert HIDDEN_TEXT not in block

    plain_block, plain_map = _service(knowledge()).knowledge_context(ACTIVE, hits)
    assert [entry["object_id"] for entry in plain_map.values()] == ["ko-first"]
    assert "ko-second" not in plain_block


def test_the_ceiling_is_normalised_once_per_library_not_once_per_hit(monkeypatch):
    """整库可见来源 ~49k 个 id、40 个命中:绑定的天花板每次 run 只归一化一次
    (曾经每个命中各排一遍 49k,~8 ms/条),而且交给 store 的就是那同一个
    frozenset 对象;天花板不绑的库(本地勾选管不到的 peer)一次都不算。按调用
    次数计,不按墙钟。"""
    from app.services import source_scope

    calls = []
    real = source_scope._library_ceiling_uncached

    def counting(scope, notebook_id):
        calls.append(notebook_id)
        return real(scope, notebook_id)

    monkeypatch.setattr(source_scope, "_library_ceiling_uncached", counting)
    wide = [OPEN_SOURCE, *(f"{index:032x}" for index in range(49_000))]
    knowledge = _Knowledge([_occurrence(OPEN_SOURCE, OPEN_TEXT)])
    hits = [
        RetrievedKnowledge(
            object_id=f"ko-{index}", object_type="concept",
            payload={"name": f"对象{index}"}, evidence=[],
            notebook_id=ACTIVE if index % 2 else PEER, tier="personal", relevance=0.5,
        )
        for index in range(40)
    ]
    with source_scope_context(
        ACTIVE, {"mode": "include", "source_ids": wide, "narrowed": False}, None,
        notebook_source_ceilings=None,
    ):
        _block, id_map = _service(knowledge).knowledge_context(
            ACTIVE, hits, budget_chars=100_000)
    assert len(id_map) == 40
    assert calls == [ACTIVE], calls
    pushed = [value for value in knowledge.pushed if value != "<absent>"]
    assert len(pushed) == 20 and len({id(value) for value in pushed}) == 1


# --------------------------------------------------------------------------- #
# 8. 真实 SQLite store 端到端
# --------------------------------------------------------------------------- #
def test_real_sqlite_store_keeps_every_out_of_ceiling_text_out_of_the_prompt(
    tmp_path, monkeypatch,
):
    from app.services.sqlite_repository import SQLiteRepository
    from tests.test_node_context import NC_IN, NC_OUT, _seed_node_context_ceiling

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    nb = _seed_node_context_ceiling(repo)
    service = repo._runtime.evidence_context_component

    def hit(object_id, object_type="concept"):
        return RetrievedKnowledge(
            object_id=object_id, object_type=object_type,
            payload={"name": object_id}, evidence=[],
            notebook_id=nb, tier="personal", relevance=0.9,
        )

    hits = [hit("ko-nc-def"), hit("ko-nc-named"), hit("ko-nc-mixed"),
            hit("ko-nc-allin"), hit("ko-nc-p-in", "procedure"),
            hit("ko-nc-payload", "procedure"), hit("ko-nc-payload-merged", "procedure")]
    plain_block, _ = service.knowledge_context(nb, hits, budget_chars=10_000)
    # 对照臂:无天花板时越界文字确实会进 prompt。
    assert "OUT definition text" in plain_block
    assert "NAME-ONLY definer" in plain_block
    assert "MIXED fused description" in plain_block
    assert "step out" in plain_block
    # payload 过程:越界元素的步骤(s-out / s-out2)与归因到越界来源的步骤
    # (合并对象的 s-gone2 / s-bare2,对象自己来自 NC_OUT)无天花板时都渲染。
    for name in ("s-out", "s-out2", "s-gone2", "s-bare2"):
        assert name in plain_block, name

    with source_scope_context(
        nb, {"mode": "include", "source_ids": [NC_IN], "narrowed": True}, None,
    ):
        block, id_map = service.knowledge_context(nb, hits, budget_chars=10_000)
    for leaked in ("OUT definition text", "OUT occurrence text", "NAME-ONLY definer",
                   "MIXED fused description", "step out"):
        assert leaked not in block, leaked
    by_object = {entry["object_id"]: entry for entry in id_map.values()}
    assert by_object["ko-nc-def"]["definition"] == "IN definition text"
    assert by_object["ko-nc-named"]["definition"] == "IN occurrence text"
    assert by_object["ko-nc-mixed"]["definition"] == "IN definition text"
    assert by_object["ko-nc-allin"]["definition"] == "ALL-IN fused description"
    assert {entry["source_id"] for entry in id_map.values()} == {NC_IN}
    assert "steps: step in" in block
    assert NC_OUT not in {entry["source_id"] for entry in id_map.values()}
    # payload 过程:越界步骤与归因不到天花板内来源的步骤,名字与原文都不进
    # prompt / id_map;天花板内的步骤(含归因到对象自己来源 NC_IN 的)照常。
    rendered = block + repr(id_map)
    for leaked in ("s-out", "s-gone2", "s-bare2", "q-out", "q-gone2", "q-bare2",
                   "OUT step text"):
        assert leaked not in rendered, leaked
    assert "steps: s-in -> s-gone -> s-bare" in block
    assert by_object["ko-nc-payload-merged"]["source_id"] == NC_IN
