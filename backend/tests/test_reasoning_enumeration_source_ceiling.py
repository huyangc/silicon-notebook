"""PR-B 推理侧:类型化集合枚举与 `read_document` 认本 run 的来源天花板。

用户裁决(2026-09-29):枚举与 `read_document` 处处认来源勾选,收窄或漂移时工具
**继续可用**;权限/来源范围的审核只归检索/读取层(动作执行体属于它)。这里钉住
推理侧的四件事,执行器按天花板过滤条目与分母的合同在执行器自己的测试里:

* 总闸:收窄(include / exclude)与漂移下,地图、reflect prompt、schema、白名单
  (以及循环里的纵深防御)同一个判据,工具照常提供;
* 披露:``source_scoped`` 进上屏摘要、回喂账目、合成分区标题、reflect prompt
  四个读者与 ``TypedCollectionResult``,且是清单身份(进续跑键);未收窄的响应
  逐字节不变(字段缺席);
* `read_document` 的来源级兜底:天花板外、参与集外的文档被拒,任何东西都不往下走;
* 对等模式:``scope:"current_notebook"`` 不提供(prompt / schema / 解析 / 执行体
  一致),Knowhow 完整枚举不在锚点库上跑(台账 E-2 在 ``_minimal_ask_service`` 上测)。
"""
from __future__ import annotations

import json

import pytest

from app.models.source_scope import SourceScope
from app.services import reasoning_retrieval as rr
from app.services.collection_enumeration import (
    LOCAL_ONLY_SCOPE_SUFFIX,
    SELECTED_SOURCES_SCOPE_SUFFIX,
    SourceEnumeration,
    SourceItem,
)
from app.services.collection_enumeration_answer import (
    enumeration_prompt_block,
    typed_collection_results,
)
from app.services.prompts import reflect_prompt, reflect_schema_hint
from app.services.reasoning_retrieval import (
    CollectionEnumerationOutcome,
    READ_DOCUMENT_ACTION,
    ReflectDecision,
)
from app.services.source_scope import source_scope_context
from tests.test_reasoning_document_read import (
    _notebook_with_documents,
    _reader,
)
from tests.test_reasoning_enumeration_tools import (  # noqa: F401
    _SeqLLM,
    _StubEnumeration,
    _add_source_row,
    _coverage,
    _enumerate_sources_action,
    _retriever,
    _seed,
    _skips,
    _steps,
    repo,  # pytest fixture, resolved by name
)


ANSWER = {"next_action": "answer", "sufficient": True}
SCOPED_SENTENCE = "Every listing in this run covers ONLY the sources the user ticked"


def _source_item(source_id="s1", title="论文一", notebook_id="nb"):
    return SourceItem(source_id=source_id, source_title=title,
                      doc_type_label="论文", summary="摘要",
                      notebook_id=notebook_id, tier="personal")


def _one_source_listing(notebook_id):
    return SourceEnumeration(
        items=(_source_item(notebook_id=notebook_id),), cursor=None,
        extra_pages=0, payload_chars=10, coverage=_coverage(),
    )


def _scope_for(kind, notebook_id):
    """三种「本库天花板收窄」形态。drift 由调用方在进入范围之后再加一篇来源造出。"""
    if kind == "include":
        return SourceScope(mode="include", source_ids=["s1"])
    if kind == "exclude":
        return SourceScope(mode="exclude", source_ids=["s2"])
    return SourceScope(mode="include", source_ids=["s1", "s2"], narrowed=False)


# --------------------------------------------------------------- B5 总闸


@pytest.mark.parametrize("kind", ["include", "exclude", "drift"])
def test_tool_stays_offered_under_narrowing_and_drift(repo, kind):  # noqa: F811
    """收窄 / 漂移下工具在**每一处**都在:prompt、schema、白名单(动作真的落成
    enumerate 步而不是未知动作兜底),并且清单带上 ``source_scoped``。

    此前 ``enumeration_active()`` 读 ``_unsafe_scope_restricted()``,这三种 run 的
    模型压根看不到清单工具。
    """
    notebook = _seed(repo, formulas=1)
    _add_source_row(repo, notebook.id, "s2", "论文二")
    llm = _SeqLLM([_enumerate_sources_action(), ANSWER])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = _StubEnumeration(
        [_one_source_listing(notebook.id)])

    with source_scope_context(notebook.id, _scope_for(kind, notebook.id)):
        if kind == "drift":
            _add_source_row(repo, notebook.id, "s3", "冻结之后上传的论文")
        assert retriever._unsafe_scope_restricted() is True
        assert retriever.enumeration_active() is True
        result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    reflect_hints = [h for h in llm.schema_hints if "sub_queries" not in h]
    assert all("enumerate_elements" in p for p in llm.reflect_prompts)
    assert all('"enumerate":{' in h for h in reflect_hints)
    assert "source_scope_unsafe_channel" not in _skips(result)
    assert "enumeration_disabled" not in _skips(result)
    [step] = _steps(result, "enumerate")
    assert step.summary == (
        f"枚举来源清单: 已全部列出 1 条{SELECTED_SOURCES_SCOPE_SUFFIX}")
    assert step.detail["source_scoped"] is True
    [outcome] = result.enumerations
    assert outcome.source_scoped is True
    # reflect prompt:第一轮起就说「清单只含勾选的来源」,第二轮的账目带同一个后缀。
    assert all(SCOPED_SENTENCE in p for p in llm.reflect_prompts)
    assert (
        f"「来源清单」已完整列出 1 条{SELECTED_SOURCES_SCOPE_SUFFIX}"
        in llm.reflect_prompts[1]
    )


def test_unnarrowed_run_carries_no_scoped_disclosure(repo):  # noqa: F811
    """对照臂:没有收窄 ⇒ 摘要、账目、prompt、detail 一个字都不多。"""
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([_enumerate_sources_action(), ANSWER])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = _StubEnumeration(
        [_one_source_listing(notebook.id)])

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    [step] = _steps(result, "enumerate")
    assert step.summary == "枚举来源清单: 已全部列出 1 条"
    assert "source_scoped" not in step.detail
    assert result.enumerations[0].source_scoped is False
    assert all(SELECTED_SOURCES_SCOPE_SUFFIX not in p for p in llm.reflect_prompts)
    assert all(SCOPED_SENTENCE not in p for p in llm.reflect_prompts)


def test_defense_in_depth_skip_follows_the_same_gate(repo):  # noqa: F811
    """循环里的纵深防御读的就是 ``enumeration_active()``:收窄 run 里放行(上一条),
    接线关掉时畸形响应仍然落不成 I/O。"""
    notebook = _seed(repo, formulas=1)
    repo.settings.reasoning_enum_tools_enabled = False
    retriever, limits = _retriever(repo, _SeqLLM([]))
    retriever.collection_enumeration = _StubEnumeration([])
    retriever.plan = lambda *a, **k: [rr.SubQuery(query="q")]
    decisions = iter([
        ReflectDecision(next_action=rr.ENUMERATE_ELEMENTS_ACTION,
                        enumerate_kind="formula"),
        ReflectDecision(next_action="answer", sufficient=True),
    ])
    retriever.reflect = lambda *a, **k: next(
        decisions, ReflectDecision(next_action="answer", sufficient=True))

    result = retriever.run(notebook.id, "q", "", limits=limits)

    assert _skips(result)["enumeration_disabled"].summary == (
        "跳过枚举(本次检索未提供清单工具)")
    assert result.enumerations == []


# --------------------------------------------------------------- B6 披露


def _outcome(**overrides):
    base = dict(collection="sources", kind="", source_id="",
                items=[_source_item()], coverage=_coverage())
    base.update(overrides)
    return CollectionEnumerationOutcome(**base)


def test_synthesis_header_carries_the_selected_sources_suffix():
    """合成证据块的分区标题:收窄的清单带后缀;与「仅当前笔记本」同时成立时两者
    按固定顺序拼接(本地范围在前,与结果卡标题一致)。"""
    scoped = enumeration_prompt_block(
        [_outcome(source_scoped=True)], inline_rows=10, budget_chars=10_000)
    both = enumeration_prompt_block(
        [_outcome(source_scoped=True, local_only=True)], inline_rows=10,
        budget_chars=10_000)
    plain = enumeration_prompt_block(
        [_outcome()], inline_rows=10, budget_chars=10_000)

    assert (f"[Enumeration: documents{SELECTED_SOURCES_SCOPE_SUFFIX}, listed 1/1, "
            "complete, previewed 1]") in scoped.text
    assert (f"[Enumeration: documents{LOCAL_ONLY_SCOPE_SUFFIX}"
            f"{SELECTED_SOURCES_SCOPE_SUFFIX}, listed 1/1") in both.text
    assert SELECTED_SOURCES_SCOPE_SUFFIX not in plain.text


def test_trace_and_ledger_use_the_same_order_for_both_suffixes():
    coverage = _coverage()
    summary = rr._enumeration_step_summary(
        "来源清单", coverage, "", local_only=True, source_scoped=True)
    assert summary.endswith(LOCAL_ONLY_SCOPE_SUFFIX + SELECTED_SOURCES_SCOPE_SUFFIX)
    chain = rr._EnumChain(_outcome(local_only=True, source_scoped=True),
                          state="complete")
    note = rr._enumeration_note({("sources", "", "", True, True): chain})
    assert (f"「来源清单」已完整列出 1 条{LOCAL_ONLY_SCOPE_SUFFIX}"
            f"{SELECTED_SOURCES_SCOPE_SUFFIX}") in note
    # 单一来源的元素清单只说「限指定来源」,不叠第二个括号。
    assert rr._enumeration_step_summary(
        "公式清单", coverage, "s1", source_scoped=True
    ).endswith("（限指定来源）")


def test_four_readers_share_the_one_literal():
    """四个服务端读者共用执行器模块的那一份字面,不许出现第二份字符串。"""
    from app.services import collection_enumeration_answer as answer
    from app.services import prompts

    assert SELECTED_SOURCES_SCOPE_SUFFIX == "（仅勾选的来源）"
    assert rr.SELECTED_SOURCES_SCOPE_SUFFIX is SELECTED_SOURCES_SCOPE_SUFFIX
    assert answer.SELECTED_SOURCES_SCOPE_SUFFIX is SELECTED_SOURCES_SCOPE_SUFFIX
    assert prompts.SELECTED_SOURCES_SCOPE_SUFFIX is SELECTED_SOURCES_SCOPE_SUFFIX
    prompt = reflect_prompt("q", "c", element_kinds=("formula",),
                            source_scoped=True)
    assert SELECTED_SOURCES_SCOPE_SUFFIX in prompt


def test_typed_result_carries_source_scoped_and_omits_it_when_false():
    """结果卡字段:为真时上线,为假时**缺席**(未收窄的单库响应逐字节不变)。
    与 ``scope`` 不同,它对三类集合都成立。"""
    scoped, plain, elements = typed_collection_results(
        [_outcome(source_scoped=True), _outcome(),
         _outcome(collection="elements", kind="formula", items=[],
                  source_scoped=True)],
        payload_chars=100_000,
    )
    assert scoped.source_scoped is True
    assert json.loads(scoped.model_dump_json())["source_scoped"] is True
    assert elements.source_scoped is True
    assert plain.source_scoped is False
    assert "source_scoped" not in json.loads(plain.model_dump_json())
    assert "source_scoped" not in plain.model_dump()


def test_source_scoped_is_part_of_the_continuation_key(repo):  # noqa: F811
    """run 中途来源集合漂移 ⇒ ``source_scoped`` 翻转 ⇒ 新开一条链,不续旧游标。

    两种天花板下读出来的条目拼成一份清单,既不完整也无法向用户解释;键里带着它,
    第二次请求拿到的是一条从头开始、带「(仅勾选的来源)」的新清单。
    """
    notebook = _seed(repo, formulas=1)
    partial = SourceEnumeration(
        items=(_source_item(notebook_id=notebook.id),), cursor="cursor-1",
        extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=1, complete=False,
                           has_more=True, total=2, truncated_reason="budget"),
    )
    fresh = _one_source_listing(notebook.id)
    drifted = {"now": False}

    class _DriftingStub(_StubEnumeration):
        def enumerate_sources(self, *args, **kwargs):
            try:
                return super().enumerate_sources(*args, **kwargs)
            finally:
                drifted["now"] = True

    stub = _DriftingStub([partial, fresh])
    llm = _SeqLLM([_enumerate_sources_action(), _enumerate_sources_action(),
                   ANSWER])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub
    retriever._unsafe_scope_restricted = lambda: drifted["now"]

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    assert stub.calls[1]["cursor"] is None
    assert [o.source_scoped for o in result.enumerations] == [False, True]


# --------------------------------------------------------- B7 read_document


def _roster_state(retriever, notebook_id, items):
    state = retriever._new_run_state(
        notebook_id, "q", "", None, max_steps=3, intent_queries=None,
        limits=None, intent_detail=None)
    state.enumerations.append(_outcome(items=list(items)))
    return state


class _NoReads:
    """任何原文读取都是失败:兜底必须在 I/O 之前拦住。"""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise AssertionError(f"out-of-scope document must not be read: {name}")
        return _boom


def _assert_refused(state, title):
    skip = state.trace[-1]
    assert skip.step_type == "skip"
    assert skip.detail == {"reason": "document_read_out_of_scope"}
    assert state.document_reads == []
    assert state.document_reads_done == 0
    assert not state.document_reads_by_id
    assert all(title not in step.summary and title not in json.dumps(
        step.detail, ensure_ascii=False) for step in state.trace)


def test_read_document_refuses_a_document_outside_the_ceiling(repo):  # noqa: F811
    notebook = _notebook_with_documents(repo)
    retriever = _reader(repo, _SeqLLM([]))
    retriever.sources = _NoReads()
    with source_scope_context(
        notebook.id, SourceScope(mode="include", source_ids=["s-summed"]),
    ):
        state = _roster_state(retriever, notebook.id, [
            _source_item("s-empty", "无摘要文档", notebook.id),
            _source_item("s-summed", "有摘要文档", notebook.id),
        ])
        retriever._action_read_document(state, ReflectDecision(
            next_action=READ_DOCUMENT_ACTION,
            read_document_source="无摘要文档"))

    _assert_refused(state, "无摘要文档")


def test_read_document_still_reads_a_document_inside_the_ceiling(repo):  # noqa: F811
    """对照臂:同一个天花板下,勾选了的那一篇照常读取。"""
    notebook = _notebook_with_documents(repo)
    retriever = _reader(repo, _SeqLLM([]))
    with source_scope_context(
        notebook.id, SourceScope(mode="include", source_ids=["s-summed"]),
    ):
        state = _roster_state(retriever, notebook.id, [
            _source_item("s-empty", "无摘要文档", notebook.id),
            _source_item("s-summed", "有摘要文档", notebook.id),
        ])
        retriever._action_read_document(state, ReflectDecision(
            next_action=READ_DOCUMENT_ACTION,
            read_document_source="有摘要文档"))

    assert state.trace[-1].step_type == "read_document"
    assert [read.source_id for read in state.document_reads] == ["s-summed"]


def test_read_document_refuses_a_library_outside_the_participant_set(repo):  # noqa: F811
    """对等模式的参与集就是带逐库天花板的那几本;花名册里混进一本不在其中的库,
    它的文档一律不读(``allows`` 对无天花板的库历史上是放行的)。"""
    notebook = _notebook_with_documents(repo)
    retriever = _reader(repo, _SeqLLM([]))
    retriever.sources = _NoReads()
    with source_scope_context(
        notebook.id, None, None,
        notebook_source_ceilings={notebook.id: ["s-empty", "s-summed"]},
        subjectless=True,
    ):
        state = _roster_state(retriever, notebook.id, [
            _source_item("s-foreign", "别的库的文档", "nb-not-a-participant"),
        ])
        retriever._action_read_document(state, ReflectDecision(
            next_action=READ_DOCUMENT_ACTION,
            read_document_source="别的库的文档"))

    _assert_refused(state, "别的库的文档")


def test_document_source_admitted_predicate():
    """谓词本身:无范围放行;本库勾选、逐库天花板、参考库勾选、对等参与集。"""
    assert rr.document_source_admitted("nb", "anything") is True
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["a"])):
        assert rr.document_source_admitted("nb", "a") is True
        assert rr.document_source_admitted("nb", "b") is False
    with source_scope_context(
        "nb", None, None, notebook_source_ceilings={"nb": ["a"], "peer": ["p"]},
        subjectless=True,
    ):
        assert rr.document_source_admitted("peer", "p") is True
        assert rr.document_source_admitted("peer", "q") is False
        assert rr.document_source_admitted("stranger", "x") is False


# --------------------------------------------------------- E-5 对等模式 local_only


def _peer(notebook_id):
    return source_scope_context(
        notebook_id, None, None,
        notebook_source_ceilings={notebook_id: ["s1"]}, subjectless=True)


def test_peer_mode_prompt_and_schema_drop_the_scope_knob():
    kinds = ("formula",)
    offered_prompt = reflect_prompt("q", "c", element_kinds=kinds)
    peer_prompt = reflect_prompt("q", "c", element_kinds=kinds,
                                 enumerate_scope=False)
    assert "enumerate.scope" in offered_prompt
    assert "current_notebook" not in peer_prompt
    assert "enumerate.scope" not in peer_prompt
    assert '"scope":"all|current_notebook"' in reflect_schema_hint(kinds)
    assert '"scope"' not in reflect_schema_hint(kinds, enumerate_scope=False)
    # 默认值即旧行为:不传新参数的调用方逐字节不变。
    assert reflect_prompt("q", "c", element_kinds=kinds) == reflect_prompt(
        "q", "c", element_kinds=kinds, enumerate_scope=True, source_scoped=False)


def test_peer_mode_reflect_neither_offers_nor_reads_the_scope(repo):  # noqa: F811
    """同一个判据落在 reflect 的三处:prompt、schema、解析。"""
    notebook = _seed(repo, formulas=1)
    llm = _SeqLLM([_enumerate_sources_action(scope="current_notebook")])
    retriever, limits = _retriever(repo, llm)

    with _peer(notebook.id):
        decision = retriever.reflect("q", "c", limits=limits)

    assert "enumerate.scope" not in llm.reflect_prompts[-1]
    assert '"scope"' not in llm.schema_hints[-1]
    assert decision.next_action == rr.ENUMERATE_ELEMENTS_ACTION
    assert decision.enumerate_scope == rr.ENUMERATE_SCOPE_ALL

    llm_single = _SeqLLM([_enumerate_sources_action(scope="current_notebook")])
    single, _ = _retriever(repo, llm_single)
    assert single.reflect("q", "c", limits=limits).enumerate_scope == (
        rr.ENUMERATE_SCOPE_CURRENT_NOTEBOOK)


def test_peer_mode_handler_never_lists_only_the_anchor(repo):  # noqa: F811
    """执行体同一个判据:替身硬塞 ``current_notebook`` 也按全部参与库列。"""
    notebook = _seed(repo, formulas=1)
    stub = _StubEnumeration([_one_source_listing(notebook.id)])
    retriever, _ = _retriever(repo, _SeqLLM([]))
    retriever.collection_enumeration = stub
    decision = ReflectDecision(
        next_action=rr.ENUMERATE_ELEMENTS_ACTION,
        enumerate_collection="sources",
        enumerate_scope=rr.ENUMERATE_SCOPE_CURRENT_NOTEBOOK)

    with _peer(notebook.id):
        assert rr.local_only_scope_offered() is False
        assert retriever._enumeration_scope(
            True, rr.ENUMERATE_SCOPE_CURRENT_NOTEBOOK) == (False, False)
        state = retriever._new_run_state(
            notebook.id, "q", "", None, max_steps=3, intent_queries=None,
            limits=None, intent_detail=None)
        retriever._run_enumeration(state, decision)

    assert stub.calls[0]["local_only"] is False
    assert state.enumerations[0].local_only is False
    assert retriever._enumeration_scope(
        True, rr.ENUMERATE_SCOPE_CURRENT_NOTEBOOK) == (True, False)


# ------------------------------------------------ E-2 Knowhow 完整枚举的闸


def _knowhow_service():
    from tests.test_ask_service_boundary import (
        _EnumerableKnowhow,
        _minimal_ask_service,
    )

    service = _minimal_ask_service()
    service.knowhow_store = _EnumerableKnowhow()
    return service


def _ask_complete(service):
    from app.models.schemas import AskRequest

    return service.ask_reasoning(
        "nb", AskRequest(question="所有方法有哪些？", mode="reasoning",
                         retrieval_effort="overview"),
        user_id="user",
    )


def test_knowhow_complete_enumeration_runs_under_an_intact_ceiling():
    """对照臂:无范围,以及冻结后没有漂移的全选天花板,照常走确定性完整枚举。"""
    service = _knowhow_service()
    assert _ask_complete(service).llm_mode == "structured"

    service = _knowhow_service()
    service.retrieval.unsafe_source_scope_restricted = lambda notebook_id: False
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1"], narrowed=False)):
        assert _ask_complete(service).llm_mode == "structured"


@pytest.mark.parametrize("shape", ["peer", "narrowed", "drift"])
def test_knowhow_complete_enumeration_steps_aside_outside_its_ceiling(shape):
    """对等模式(只会走锚点库、答不了一组库的问题)、收窄(隐藏投影源不在天花板
    里)、漂移(冻结之后出现的投影源)三种形态都不读 Knowhow 目录。"""
    service = _knowhow_service()
    service.retrieval.unsafe_source_scope_restricted = (
        lambda notebook_id: shape == "drift")
    if shape == "peer":
        scope = source_scope_context(
            "nb", None, None, notebook_source_ceilings={"nb": ["s1"]},
            subjectless=True)
    elif shape == "narrowed":
        scope = source_scope_context(
            "nb", SourceScope(mode="include", source_ids=["s1"], narrowed=True))
    else:
        scope = source_scope_context(
            "nb", SourceScope(mode="include", source_ids=["s1"], narrowed=False))
    with scope:
        assert service._knowhow_completeness_in_scope("nb") is False
        response = _ask_complete(service)

    assert service.knowhow_store.catalog_calls == 0
    assert response.llm_mode != "structured"


# ------------------------------------- chunk 引擎的文档目录/单篇概览(同一判据)


def _overview_service():
    from types import SimpleNamespace

    return SimpleNamespace(
        collection_enumeration=object(), overview_sources=_NoReads(),
        evidence_context=object(),
        settings=SimpleNamespace(chunk_answer_budget_chars=4000,
                                 document_overview_max_elements=20),
        model_clients=SimpleNamespace(
            chat=lambda name: SimpleNamespace(configured=False, model="m")),
        model_errors=SimpleNamespace(note_model_error=lambda *a, **k: None),
        overview_source_generation=None,
        _answer_with_retry=lambda synthesize, model: ("答案", None, [], True),
        _parse_answer_anchors=lambda answer, id_map: [],
        _save_answer=lambda *args, **kwargs: "",
    )


def _run_overview(monkeypatch, intent, catalog_items=()):
    from types import SimpleNamespace

    from app.services import document_catalog_overview, document_overview
    from app.services.ask_service import AskService

    captured: dict = {}
    monkeypatch.setattr(document_overview, "overview_intent",
                        lambda question: intent)

    def _catalog(*args, **kwargs):
        captured.update(kwargs)
        complete = SimpleNamespace(
            coverage=SimpleNamespace(complete=True, returned_total=1,
                                     total=1, truncated_reason=""),
            synthesis_rows=0, synthesis_complete=None)
        return SimpleNamespace(result_sets=[complete],
                               items=list(catalog_items), id_map={},
                               citations=[], coverage_note="",
                               context_block="")

    monkeypatch.setattr(document_catalog_overview, "prepare_catalog_overview",
                        _catalog)
    payload = SimpleNamespace(question="q", retrieval_effort="standard",
                              asked_at=None)
    response = AskService._try_document_overview(
        _overview_service(), "nb", payload, "conv", "", "",
        user_id="u", job_id="", cancel_event=None)
    return captured, response


def _catalog_kwargs(monkeypatch, intent):
    """只取 ``prepare_catalog_overview`` 收到的参数;之后的合成与本判据无关。"""
    from app.services import document_catalog_overview

    captured: dict = {}

    class _Captured(Exception):
        pass

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        raise _Captured

    from app.services import document_overview
    from app.services.ask_service import AskService
    from types import SimpleNamespace

    monkeypatch.setattr(document_overview, "overview_intent",
                        lambda question: intent)
    monkeypatch.setattr(document_catalog_overview, "prepare_catalog_overview",
                        _capture)
    with pytest.raises(_Captured):
        AskService._try_document_overview(
            _overview_service(), "nb",
            SimpleNamespace(question="q", retrieval_effort="standard",
                            asked_at=None),
            "conv", "", "", user_id="u", job_id="", cancel_event=None)
    return captured


def test_peer_mode_catalog_overview_never_lists_only_the_anchor(monkeypatch):
    """E-5 在 chunk 引擎的那一处:问法里的「当前笔记本」在对等模式下没有对象。"""
    from app.services.document_overview import OverviewIntent

    intent = OverviewIntent("catalog")
    with _peer("nb"):
        captured = _catalog_kwargs(monkeypatch, intent)
    assert captured["local_only"] is False
    assert _catalog_kwargs(monkeypatch, intent)["local_only"] is True


def test_single_document_overview_refuses_a_source_outside_the_ceiling(monkeypatch):
    """单篇概览读原文前同一道来源级兜底;越界按「范围内没有」处理,零原文读取。"""
    from app.services.document_overview import OverviewIntent

    intent = OverviewIntent("source", title="无摘要文档")
    item = _source_item("s-out", "无摘要文档", "nb")
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s-in"])):
        _, response = _run_overview(
            monkeypatch, intent,
            catalog_items=[item])
    assert "当前选择范围内未找到该标题的文档" in response.answer


def test_a_knowhow_projection_source_created_after_the_freeze_is_drift(repo):  # noqa: F811
    """冻结之后才出现的 Knowhow 投影源不在天花板里:真实漂移探针(本库可见 + 隐藏
    两半,按冻结时的属主重读)报漂移,完整枚举因此让路。对照:冻结前后一致时放行。"""
    from types import SimpleNamespace

    from app.services.ask_service import AskService

    notebook = _seed(repo, formulas=1)
    service = SimpleNamespace(retrieval=repo.retrieval)
    frozen = {"mode": "include", "source_ids": ["s1"], "narrowed": False,
              "hidden_source_ids": [], "owner_id": ""}
    with source_scope_context(notebook.id, frozen):
        assert AskService._knowhow_completeness_in_scope(
            service, notebook.id) is True
        with repo._write() as db:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("src-knowhow-new", notebook.id, "方法表", "knowhow",
                 "parsed", "parsed", "2026-09-29", "2026-09-29"),
            )
        assert AskService._knowhow_completeness_in_scope(
            service, notebook.id) is False
