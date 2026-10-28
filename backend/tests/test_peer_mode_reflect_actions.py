"""对等(全局)模式下 reflect 的两把逐动作减法闸。

``ppr_retrieve``(PPR 走按名义 active 缓存的联邦图,生产者 D0-5 整条关)在对等 run
里只会空手而归,所以不提供。``search_elements`` 的元素臂已逐库联邦化
(``test_peer_mode_element_arm.py``),对等 run 默认照常提供;只有回退开关
``GLOBAL_ASK_ELEMENT_ARM_ENABLED=false`` 把它关回去时才不提供。两者与「无图时摘五个
图动作」那把 ``kg_actions`` 闸同形:prompt 行、schema 枚举、``allowed_actions`` 白名单
三处一起消失。闸是**逐动作**的——``reasoning_retrieval.element_search_offered`` 与
``ppr_retrieve_offered`` 各管一个,渲染函数各收一个同名参数。首轮 PPR 播种(含预取)
读 PPR 那一把。本文件里「两个都不提供」的用例都显式关掉元素臂开关。

单库 run 的 prompt/schema 逐字节不变由两份摘要钉住(有图、无图各一),白名单与接线
由对照臂钉住。
"""
from __future__ import annotations

import hashlib
import inspect
import json

import pytest

from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
)
from app.services.prompts import reflect_prompt, reflect_schema_hint
from tests.model_testkit import bind_chat_client
from tests.test_peer_mode_ask_steps import _peer_scope
from tests.test_reasoning_retrieval import (  # noqa: F401 — rrepo 是夹具
    _SeqLLM, _seed_notebook_without_kg, _seed_two_nodes, rrepo,
)


#: 两把逐动作闸,动作名即渲染函数的参数名。
_GATED_ACTIONS = ("search_elements", "ppr_retrieve")

#: 对等 run 里**保留**的图动作:闸只摘那两条,不能顺手把别的图动作也带走。
_PEER_KEPT_GRAPH_ACTIONS = ("expand_graph", "expand_community", "follow_chain")


def _actions_in(schema: str) -> set:
    return set(json.loads(schema)["next_action"].split("|"))


class _Capture:
    """反射替身:记下模型**实际收到**的 prompt/schema,并硬吐一个指定动作。"""

    configured = True

    def __init__(self, action):
        self._action = action
        self.seen = []

    def chat_json(self, messages, schema_hint, **kwargs):
        self.seen.append((messages[-1]["content"], schema_hint))
        return json.dumps({"next_action": self._action, "query": "布局"})


def _close_element_arm(rrepo):
    """回退开关 ``GLOBAL_ASK_ELEMENT_ARM_ENABLED=false``:对等 run 里元素臂不可用。"""
    rrepo.settings.global_ask_element_arm_enabled = False


def _reflect(rrepo, action, *, kg_actions, peer_ids=None, fail_closed=False):
    from app.services.reasoning_retrieval import ReasoningRetriever

    capture = _Capture(action)
    bind_chat_client(rrepo, "reasoning_agent", capture)
    rr = ReasoningRetriever.from_repository(
        rrepo, rrepo.settings, fail_closed=fail_closed)
    if peer_ids is None:
        decision = rr.reflect("布局布线怎么做", "候选", kg_actions=kg_actions)
    else:
        with _peer_scope(peer_ids):
            decision = rr.reflect("布局布线怎么做", "候选", kg_actions=kg_actions)
    prompt, schema = capture.seen[-1]
    return decision, prompt, schema


@pytest.mark.parametrize("has_kg", [True, False])
def test_peer_reflect_offers_neither_action_in_prompt_schema_or_whitelist(rrepo, has_kg):
    """对等 run + 元素臂回退开关关:两个动作在模型实际收到的 prompt 与 schema 里都
    不出现;模型硬吐其中一个时,决定标成「本次不提供」(`withheld`,执行处记 skip
    继续),不兜底。

    同一个用例里跑单库对照臂:同一份响应在单库 run 里是合法动作,三处都在(单库
    不读这个开关)。
    """
    _close_element_arm(rrepo)
    nb = _seed_two_nodes(rrepo) if has_kg else _seed_notebook_without_kg(rrepo)
    other = _seed_notebook_without_kg(rrepo)
    for action in _GATED_ACTIONS:
        decision, prompt, schema = _reflect(
            rrepo, action, kg_actions=has_kg, peer_ids=(nb.id, other.id))
        for name in _GATED_ACTIONS:
            assert name not in prompt, name
            assert name not in _actions_in(schema), name
        assert decision.fallback is False and decision.withheld is True
        assert decision.next_action == action and decision.sufficient is False
        # 闸只摘这两条:其余动作(有图时含三个图动作)原样留着。
        kept = {"answer", "add_subquery", "exact_lookup", "search_chunks"}
        if has_kg:
            kept |= set(_PEER_KEPT_GRAPH_ACTIONS)
        assert kept <= _actions_in(schema)
        for name in kept:
            assert f"- {name}:" in prompt, name

        # 对照臂:单库 run 三处都在,同一份响应是合法决定。
        decision, prompt, schema = _reflect(rrepo, action, kg_actions=has_kg)
        if has_kg or action == "search_elements":
            assert f"- {action}:" in prompt
            assert action in _actions_in(schema)
            assert decision.fallback is False and decision.next_action == action
            assert decision.withheld is False


@pytest.mark.parametrize("has_kg", [True, False])
def test_peer_reflect_offers_search_elements_once_the_arm_is_federated(rrepo, has_kg):
    """元素臂开关开着(默认):对等 run 的 prompt、schema 与白名单里都有
    ``search_elements``,同一份响应是合法决定;``ppr_retrieve`` 照旧不提供。"""
    nb = _seed_two_nodes(rrepo) if has_kg else _seed_notebook_without_kg(rrepo)
    other = _seed_notebook_without_kg(rrepo)
    decision, prompt, schema = _reflect(
        rrepo, "search_elements", kg_actions=has_kg, peer_ids=(nb.id, other.id))
    assert "- search_elements:" in prompt
    assert "search_elements" in _actions_in(schema)
    assert decision.fallback is False and decision.withheld is False
    assert decision.next_action == "search_elements"
    assert "ppr_retrieve" not in prompt
    assert "ppr_retrieve" not in _actions_in(schema)


def test_peer_reflect_rejects_both_actions_when_fail_closed(rrepo):
    """严格调用方的既有合同不变:任何不在白名单里的动作都抛 `invalid action`
    (`test_no_kg_run_rejects_every_graph_action_when_fail_closed` 对无图闸钉的
    是同一条),不走「本次不提供」那条宽松路径。"""
    _close_element_arm(rrepo)
    nb = _seed_two_nodes(rrepo)
    for action in _GATED_ACTIONS:
        with pytest.raises(ValueError, match="invalid action"):
            _reflect(rrepo, action, kg_actions=True, peer_ids=(nb.id,),
                     fail_closed=True)
        decision, _prompt, _schema = _reflect(
            rrepo, action, kg_actions=True, fail_closed=True)
        assert decision.next_action == action


@pytest.mark.parametrize("closed", _GATED_ACTIONS)
def test_each_gate_closes_only_its_own_action(rrepo, monkeypatch, closed):
    """两把闸互相独立:只关一把时,另一个动作在 prompt、schema 与白名单里都还在。
    元素臂联邦化之后只翻 `element_search_offered`,PPR 不受牵连(反之亦然)。"""
    import app.services.reasoning_retrieval as rr_mod

    predicate = {"search_elements": "element_search_offered",
                 "ppr_retrieve": "ppr_retrieve_offered"}[closed]
    monkeypatch.setattr(rr_mod, predicate, lambda *_args: False)
    _seed_two_nodes(rrepo)
    (still,) = [name for name in _GATED_ACTIONS if name != closed]

    decision, prompt, schema = _reflect(rrepo, closed, kg_actions=True)
    assert closed not in _actions_in(schema)
    assert f"- {closed}:" not in prompt
    assert decision.withheld is True and decision.next_action == closed

    decision, prompt, schema = _reflect(rrepo, still, kg_actions=True)
    assert still in _actions_in(schema)
    assert f"- {still}:" in prompt
    assert decision.fallback is False and decision.next_action == still


_NOT_PASSED = "<未传>"


def _captured_gates(rrepo, peer_ids):
    """`reflect()` 传给两个渲染函数的两把闸实参(按签名绑定取值)。"""
    import app.services.reasoning_retrieval as rr_mod

    seen = []
    originals = {"prompt": rr_mod.reflect_prompt,
                 "schema": rr_mod.reflect_schema_hint}

    def _spy(kind):
        original = originals[kind]

        def _wrapped(*args, **kwargs):
            bound = inspect.signature(original).bind(*args, **kwargs)
            seen.append((kind, tuple(
                bound.arguments.get(name, _NOT_PASSED)
                for name in _GATED_ACTIONS)))
            return original(*args, **kwargs)

        return _wrapped

    rr_mod.reflect_prompt = _spy("prompt")
    rr_mod.reflect_schema_hint = _spy("schema")
    try:
        _reflect(rrepo, "answer", kg_actions=True, peer_ids=peer_ids)
    finally:
        rr_mod.reflect_prompt = originals["prompt"]
        rr_mod.reflect_schema_hint = originals["schema"]
    assert {kind for kind, _value in seen} == {"prompt", "schema"}
    return {value for _kind, value in seen}


def test_reflect_passes_the_gates_from_the_run_mode(rrepo):
    """接线:单库 run 两个渲染函数都拿到 (True, True)(开着 = 接入前的渲染);对等
    run 元素臂开着(默认)时拿到 (True, False),回退开关关掉时拿到 (False, False)。
    单库 run 不读这个开关。闸在 `reflect()` 里现算,`run()` 的调用形状没变,所以接线
    点就是这一处。"""
    nb = _seed_two_nodes(rrepo)
    assert _captured_gates(rrepo, None) == {(True, True)}
    assert _captured_gates(rrepo, (nb.id,)) == {(True, False)}
    _close_element_arm(rrepo)
    assert _captured_gates(rrepo, None) == {(True, True)}
    assert _captured_gates(rrepo, (nb.id,)) == {(False, False)}


def _all_gates_open(kg_actions):
    return dict(
        element_kinds=ENUMERABLE_ELEMENT_KINDS,
        object_types=ENUMERABLE_KG_OBJECT_TYPES,
        outline=True, consult_memory=True, search_chunks=True,
        kg_actions=kg_actions, read_document=True, keyword_search=True,
        section_nav=True,
    )


def _digest(kg_actions, **extra):
    gates = _all_gates_open(kg_actions)
    prompt = reflect_prompt(
        "布局布线怎么做", "候选", **gates, read_document_cap=2,
        section_browse_cap=3, section_read_cap=3,
        corpus_langs=["zh", "en"], source_scoped=True, **extra)
    schema = reflect_schema_hint(**gates, **extra)
    return hashlib.sha256(
        prompt.encode("utf-8") + b"\0" + schema.encode("utf-8")).hexdigest()


#: 单库 run 的 reflect prompt + schema 摘要,取自加这两把闸**之前**的代码(各把闸
#: 全开,含这两把闸改写过的每一句:search_chunks 的对比句、enumerate 的偏好句、
#: consult_memory 的示例清单、两条动作说明)。改动 reflect prompt 本身时按新渲染
#: 重算;只为这两把闸改了单库渲染的一个字节,这里就会红。
#: 2026-10-22 重算:全开的闸里加入文档内导航(``section_nav``,两条动作说明、两个
#: schema 分支、scope 字段清单里的 ``browse_sections.query``)。
_SINGLE_LIBRARY_DIGESTS = {
    True: "17f332ea9f991ffc6c3fea5e34869f82736a1ecbd9983be1984ccaa29c02da4a",
    False: "f65b72d3dd815cc1c69e0eb4e85f70fd9dd69b06d70bd97b01e56720f869cb35",
}


@pytest.mark.parametrize("has_kg", [True, False])
def test_single_library_reflect_rendering_is_byte_identical(has_kg):
    """单库 run(两把闸开着,即默认)逐字节等于加闸之前的渲染,有图、无图各一。"""
    assert _digest(has_kg) == _SINGLE_LIBRARY_DIGESTS[has_kg]
    assert _digest(
        has_kg, search_elements=True, ppr_retrieve=True,
    ) == _SINGLE_LIBRARY_DIGESTS[has_kg]


_ELEMENTS_CONTRAST = ("search_elements (which returns TYPED document elements "
                      "— formulas, tables, figures)")
_PPR_CONTRAST = "ppr_retrieve (which propagates through the graph)"


def _conditioned(has_kg, elements, ppr):
    """被这两把闸条件化的措辞:(关闸拼写, 开闸拼写)。"""
    contrasts = ([_ELEMENTS_CONTRAST] if elements else []) + (
        [_PPR_CONTRAST] if has_kg and ppr else [])
    full = [_ELEMENTS_CONTRAST] + ([_PPR_CONTRAST] if has_kg else [])
    off_contrast = ("It differs from " + " and from ".join(contrasts)
                    + ": this one searches" if contrasts else "It searches")
    examples_on = (["ppr_retrieve", "exact_lookup", "expand_graph", "follow_chain"]
                   if has_kg else ["exact_lookup", "search_elements"])
    closed = {name for name, offered in
              (("search_elements", elements), ("ppr_retrieve", ppr)) if not offered}
    examples_off = [name for name in examples_on if name not in closed]
    pairs = [
        (off_contrast + " the passage text",
         "It differs from " + " and from ".join(full)
         + ": this one searches the passage text"),
        ("(" + ", ".join(examples_off) + ")", "(" + ", ".join(examples_on) + ")"),
    ]
    if not elements:
        pairs.append(("PREFER this over a relevance search",
                      "PREFER this over search_elements"))
    return pairs


@pytest.mark.parametrize("has_kg", [True, False])
@pytest.mark.parametrize("elements,ppr", [(False, False), (False, True), (True, False)])
def test_closed_rendering_only_drops_the_closed_actions(has_kg, elements, ppr):
    """关闸渲染相对开闸渲染**只减不改**:把条件化过的措辞还原回开闸拼写后,每一行
    都是开闸渲染里原样存在的行,少掉的恰好是被关动作自己那一行;schema 枚举只少
    被关的那几个词、其余顺序不变,其它字节一个不动。"""
    gates = _all_gates_open(has_kg)
    closed = [name for name, offered in
              (("search_elements", elements), ("ppr_retrieve", ppr)) if not offered]
    on = reflect_prompt("Q", "C", **gates, read_document_cap=2)
    off = reflect_prompt("Q", "C", **gates, read_document_cap=2,
                         search_elements=elements, ppr_retrieve=ppr)
    normalised = off
    for off_text, on_text in _conditioned(has_kg, elements, ppr):
        assert off_text in normalised, off_text
        assert on_text in on, on_text
        normalised = normalised.replace(off_text, on_text, 1)
    on_lines = on.splitlines()
    normalised_lines = normalised.splitlines()
    for line in normalised_lines:
        assert line in on_lines, line
    dropped = [line for line in on_lines if line not in normalised_lines]
    assert sorted(line.split(":")[0] for line in dropped) == sorted(
        f"- {name}" for name in closed if has_kg or name == "search_elements")

    on_schema = reflect_schema_hint(**gates)
    off_schema = reflect_schema_hint(
        **gates, search_elements=elements, ppr_retrieve=ppr)
    on_enum = json.loads(on_schema)["next_action"]
    off_enum = json.loads(off_schema)["next_action"]
    assert off_schema.replace(off_enum, on_enum) == on_schema
    assert off_enum.split("|") == [
        word for word in on_enum.split("|") if word not in closed]


def test_peer_run_skips_the_ppr_seed_and_its_prefetch(rrepo):
    """首轮 PPR 播种与预取读 PPR 那把判据:对等 run 一次 `ppr_retrieve` 都不发、轨迹
    没有那步「概念漫游:跨文档检索,得到 0 段原文」;单库对照臂照旧播种。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    rrepo.settings.graph_ppr_enabled = True

    def _run(peer):
        bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
            plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
            reflects=[{"next_action": "answer", "sufficient": True}]))
        rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
        calls = []
        original = rr.ppr_retrieve

        def _ppr(notebook_id, query):
            calls.append(query)
            return original(notebook_id, query)

        rr.ppr_retrieve = _ppr
        if peer:
            with _peer_scope((nb.id,)):
                res = rr.run(nb.id, "RTL到GDSII流程", "")
        else:
            res = rr.run(nb.id, "RTL到GDSII流程", "")
        seeds = [t for t in res.trace
                 if t.step_type == "ppr" and t.detail.get("phase") == "seed"]
        return calls, seeds

    calls, seeds = _run(peer=False)
    assert calls == ["RTL到GDSII流程"] and len(seeds) == 1
    calls, seeds = _run(peer=True)
    assert calls == [] and seeds == []


def _run_with(rrepo, reflects, *, peer, notebook=None):
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = notebook or _seed_two_nodes(rrepo)
    llm = _SeqLLM(plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
                  reflects=reflects)
    bind_chat_client(rrepo, "reasoning_agent", llm)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    if peer:
        with _peer_scope((nb.id,)):
            return rr.run(nb.id, "RTL到GDSII流程", "")
    return rr.run(nb.id, "RTL到GDSII流程", "")


def _not_offered_skips(result):
    return [t.detail["action"] for t in result.trace
            if t.step_type == "skip"
            and t.detail.get("reason") == "action_not_offered"]


def test_a_peer_run_skips_an_unoffered_action_and_keeps_going(rrepo):
    """对等 run(元素臂回退开关关)硬吐两个不提供的动作:各记一条 skip,循环继续,
    直到模型自己作答——不兜底成 answer 提前收尾,反思步上也没有 `fallback_reason`。"""
    _close_element_arm(rrepo)
    rrepo.settings.reasoning_stale_limit = 10
    result = _run_with(rrepo, [
        {"next_action": "search_elements", "query": "布局"},
        {"next_action": "ppr_retrieve", "query": "布局"},
        {"next_action": "answer", "sufficient": True, "reason": "够了"},
    ], peer=True)
    assert _not_offered_skips(result) == ["search_elements", "ppr_retrieve"]
    reflects = [t for t in result.trace if t.step_type == "reflect"]
    assert [t.detail["next_action"] for t in reflects] == [
        "search_elements", "ppr_retrieve", "answer"]
    assert not any("fallback_reason" in t.detail for t in reflects)
    assert result.trace[-1].step_type == "answer"


def test_a_graphless_run_skips_a_graph_action_and_keeps_going(rrepo):
    rrepo.settings.reasoning_stale_limit = 10
    rrepo.settings.graph_ppr_enabled = False
    nb = _seed_notebook_without_kg(rrepo)
    result = _run_with(rrepo, [
        {"next_action": "expand_graph", "expand": {"object_id": "x"}},
        {"next_action": "answer", "sufficient": True},
    ], peer=False, notebook=nb)
    assert _not_offered_skips(result) == ["expand_graph"]
    reflects = [t for t in result.trace if t.step_type == "reflect"]
    assert len(reflects) == 2
    assert not any("fallback_reason" in t.detail for t in reflects)


def test_an_unknown_action_name_still_takes_the_fallback(rrepo):
    """集合外的名字才是真正的未知动作:照旧兜底收尾,原因带上名字。"""
    result = _run_with(rrepo, [{"next_action": "teleport", "query": "x"}],
                       peer=True)
    assert _not_offered_skips(result) == []
    reflects = [t for t in result.trace if t.step_type == "reflect"]
    assert [t.detail.get("fallback_reason") for t in reflects] == [
        "invalid_action:teleport"]


def test_a_peer_runs_truncation_ledger_names_no_unoffered_action(rrepo, monkeypatch):
    """对等 run 里 expand_graph 照常提供、截断照常可能发生:元素臂回退开关关掉时,
    回喂账目的「下一步」只点名 add_subquery,不点名 search_elements;开关开着(默认)
    时与单库逐字相同;单库对照臂逐字照旧。"""
    from app.services.reasoning_retrieval import (
        NeighborExpansion, ReasoningRetriever,
    )
    from tests.test_reasoning_retrieval import _mk_rk

    rrepo.settings.reasoning_stale_limit = 10
    nb = _seed_two_nodes(rrepo)
    monkeypatch.setattr(
        ReasoningRetriever, "search",
        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(
        ReasoningRetriever, "neighbors",
        lambda self, n, oid, edge_type=None, direction="both": NeighborExpansion(
            [_mk_rk("B", "nodeB")], True))

    def _prompts(peer):
        class _Rec(_SeqLLM):
            def __init__(self):
                super().__init__(
                    plan={"sub_queries": [{"query": "q"}]},
                    reflects=[{"next_action": "expand_graph",
                               "expand": {"object_id": "A"}},
                              {"next_action": "answer", "sufficient": True}])
                self.prompts = []

            def chat_json(self, messages, schema_hint, **kwargs):
                if "sub_queries" not in schema_hint:
                    self.prompts.append(messages[-1]["content"])
                return super().chat_json(messages, schema_hint, **kwargs)

        llm = _Rec()
        bind_chat_client(rrepo, "reasoning_agent", llm)
        rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
        if peer:
            with _peer_scope((nb.id,)):
                rr.run(nb.id, "q", "")
        else:
            rr.run(nb.id, "q", "")
        return llm.prompts

    federated = _prompts(True)
    assert "add_subquery 或search_elements 定向检索" in federated[1]
    _close_element_arm(rrepo)
    peer_prompts = _prompts(True)
    assert "只展开了其中一部分邻居" in peer_prompts[1]
    assert "add_subquery 定向检索" in peer_prompts[1]
    assert all("search_elements" not in p for p in peer_prompts)
    single = _prompts(False)
    assert "add_subquery 或search_elements 定向检索" in single[1]


def test_core_action_ids_are_every_gate_open():
    """纯函数:闸全开的集合就是 CORE_REFLECT_ACTION_IDS;经验词表的每个词在全集下
    都被判为「提供」(词表与动作 id 的映射没有漏项)。"""
    from app.services.reasoning_retrieval import (
        CORE_REFLECT_ACTION_IDS, core_reflect_actions, experience_action_offered,
    )
    from app.services.retrieval_experience_projection import RETRIEVAL_ACTIONS

    every = core_reflect_actions(
        kg_actions=True, chunk_search=True, enumeration=True, read_document=True,
        outline=True, consult_memory=True, search_elements=True,
        ppr_retrieve=True, section_nav=True, find_sources=True,
        paper_cards=True, extract=True)
    assert len(every) == len(set(every))
    assert frozenset(every) == CORE_REFLECT_ACTION_IDS
    for word in RETRIEVAL_ACTIONS:
        assert experience_action_offered(word, CORE_REFLECT_ACTION_IDS), word
