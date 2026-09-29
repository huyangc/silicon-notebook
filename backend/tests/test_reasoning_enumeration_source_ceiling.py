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
    # 披露就是传给执行器的判词:执行器按天花板过滤了这份清单。
    assert retriever.collection_enumeration.calls[0]["ceiling_binds"] is True
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


def _seed_evidenced(repo):
    """与 ``_seed`` 同形,但那个知识对象带着来自 s1 的证据——候选检索在冻结天花板下
    按证据过滤(既有行为,与枚举无关),没有证据的对象会让两次 run 的候选摘要不同。"""
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        for source_id, title in (("s1", "论文一"), ("s2", "论文二")):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (source_id, notebook.id, title, "pdf", "extracted", "extracted",
                 "2026-09-29", "2026-09-29"),
            )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-001", "s1", "formula", "p1", "公式 1", "{}", "2026-09-29"),
        )
    repo.store_kg(notebook.id, None, [{
        "local_id": "C1", "object_type": "claim",
        "payload": {"name": "版图设计要点", "section_path": "1"},
        "evidence": [{
            "source_id": "s1", "source_title": "论文一", "element_id": "el-001",
            "element_type": "formula", "location_label": "p1",
            "quoted_span": "公式 1", "confidence": 1.0,
        }],
    }], [])
    repo.collection_catalog.invalidate()
    return notebook


def test_an_all_ticked_frozen_scope_discloses_nothing(repo):  # noqa: F811
    """全选的冻结天花板(每个 UI 请求的默认形态)不是收窄:没有任何披露,且模型看到
    的 prompt 与不带范围的 run **逐字节相同**,地图照常注入。

    这条路径是回归面最大的一条——把 ``source_scoped`` 判成「有天花板就算」会让每个
    UI 请求的每份清单都带上后缀,而此前没有任何测试会红。
    """
    notebook = _seed_evidenced(repo)

    stubs = []

    def run(scope):
        llm = _SeqLLM([_enumerate_sources_action(), ANSWER])
        retriever, limits = _retriever(repo, llm)
        retriever.collection_enumeration = _StubEnumeration(
            [_one_source_listing(notebook.id)])
        stubs.append(retriever.collection_enumeration)
        if scope is None:
            return llm, retriever.run(notebook.id, "有哪几篇", "", limits=limits)
        with source_scope_context(notebook.id, scope):
            assert retriever._unsafe_scope_restricted() is False
            return llm, retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    all_ticked = {"mode": "include", "source_ids": ["s1", "s2"],
                  "narrowed": False, "hidden_source_ids": [], "owner_id": ""}
    ticked_llm, ticked = run(all_ticked)
    plain_llm, plain = run(None)

    [step] = _steps(ticked, "enumerate")
    assert step.summary == "枚举来源清单: 已全部列出 1 条"
    assert "source_scoped" not in step.detail
    assert ticked.enumerations[0].source_scoped is False
    assert all(SELECTED_SOURCES_SCOPE_SUFFIX not in p
               for p in ticked_llm.reflect_prompts + ticked_llm.plan_prompts)
    assert all(SCOPED_SENTENCE not in p for p in ticked_llm.reflect_prompts)
    [card] = typed_collection_results(ticked.enumerations, payload_chars=100_000)
    assert "source_scoped" not in json.loads(card.model_dump_json())
    assert ticked.collection_map_text.startswith("[Collections in scope]")
    assert ticked.collection_map_text == plain.collection_map_text
    assert ticked_llm.reflect_prompts == plain_llm.reflect_prompts
    assert ticked_llm.plan_prompts == plain_llm.plan_prompts
    # 判词如实传给执行器:全选不约束集合读取(与没有天花板时同一种读法)。
    assert stubs[0].calls[0]["ceiling_binds"] is False


def test_the_map_binds_the_ceiling_only_when_the_scope_is_narrowed(repo):  # noqa: F811
    """集合地图读的是本 run 的判词:全选冻结时与没有天花板逐字节相同(没有证据的
    知识对象照样计数),真收窄时才按天花板计(它不在任何勾选来源里,计为 0)。"""
    notebook = _seed(repo, formulas=1)
    _add_source_row(repo, notebook.id, "s2", "论文二")

    def map_text(scope):
        retriever, limits = _retriever(repo, _SeqLLM([ANSWER]))
        if scope is None:
            return retriever.run(notebook.id, "q", "", limits=limits).collection_map_text
        with source_scope_context(notebook.id, scope):
            return retriever.run(notebook.id, "q", "", limits=limits).collection_map_text

    plain = map_text(None)
    ticked = map_text({"mode": "include", "source_ids": ["s1", "s2"],
                       "narrowed": False, "hidden_source_ids": [], "owner_id": ""})
    narrowed = map_text(SourceScope(mode="include", source_ids=["s1"]))
    assert "claim 1" in plain
    assert ticked == plain
    assert "claim 0" in narrowed


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


def test_every_reader_takes_its_suffix_from_the_one_function():
    """同一份清单在上屏摘要、回喂账目、合成分区标题三处说出**同一串**后缀,顺序
    固定:「(限指定来源)」→「(仅当前笔记本)」→「(仅勾选的来源)」;「(仅勾选的来源)」
    只要 ``source_scoped`` 为真就无条件追加(与前端结果卡同一判据),单一来源的元素
    清单也不例外。"""
    cases = [
        (_outcome(local_only=True, source_scoped=True),
         LOCAL_ONLY_SCOPE_SUFFIX + SELECTED_SOURCES_SCOPE_SUFFIX),
        (_outcome(collection="elements", kind="formula", source_id="s1",
                  items=[], source_scoped=True),
         rr.SINGLE_SOURCE_SCOPE_SUFFIX + SELECTED_SOURCES_SCOPE_SUFFIX),
        (_outcome(collection="elements", kind="formula", source_id="s1",
                  items=[]),
         rr.SINGLE_SOURCE_SCOPE_SUFFIX),
        (_outcome(), ""),
    ]
    for outcome, expected in cases:
        assert rr.enumeration_scope_suffix(outcome) == expected
        summary = rr._enumeration_step_summary("清单", outcome.coverage, outcome)
        assert summary.endswith(f"条{expected}"), summary
        note = rr._enumeration_note(
            {("k",): rr._EnumChain(outcome, state="complete")})
        assert f"已完整列出 1 条{expected}" in note, note
        header = enumeration_prompt_block(
            [outcome], inline_rows=10, budget_chars=10_000).text
        assert f"{expected}, listed" in header, header
    assert rr.SINGLE_SOURCE_SCOPE_SUFFIX == "（限指定来源）"


def test_four_readers_share_the_one_literal():
    """服务端读者共用一份实现与一份字面,不许出现第二份字符串。"""
    from app.services import collection_enumeration_answer as answer
    from app.services import prompts

    assert SELECTED_SOURCES_SCOPE_SUFFIX == "（仅勾选的来源）"
    assert answer.enumeration_scope_suffix is rr.enumeration_scope_suffix
    assert rr.SELECTED_SOURCES_SCOPE_SUFFIX is SELECTED_SOURCES_SCOPE_SUFFIX
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


def test_drift_mid_run_resumes_the_same_listing_and_marks_it(repo):  # noqa: F811
    """run 中途来源集合漂移 ⇒ **同一条链**从游标续列,标记粘性地变成真。

    天花板在 run 内冻结,漂移只让「清单只含勾选的来源」这句话变得成立,执行器读到
    的集合不变。所以:第二次请求续上第一次的游标(不从第 1 页重列)、结果里只有一份
    清单、行预算只按两页的真实条数扣一次;这份清单的一部分是在漂移之后列出的,所以
    整份清单(摘要、账目、结果卡)都带「(仅勾选的来源)」。
    """
    notebook = _seed(repo, formulas=1)
    partial = SourceEnumeration(
        items=(_source_item(notebook_id=notebook.id),), cursor="cursor-1",
        extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=1, complete=False,
                           has_more=True, total=2, truncated_reason="budget"),
    )
    rest = SourceEnumeration(
        items=(_source_item("s2", "论文二", notebook.id),), cursor=None,
        extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=2, total=2),
    )
    drifted = {"now": False}

    class _DriftingStub(_StubEnumeration):
        def enumerate_sources(self, *args, **kwargs):
            try:
                return super().enumerate_sources(*args, **kwargs)
            finally:
                drifted["now"] = True

    stub = _DriftingStub([partial, rest])
    llm = _SeqLLM([_enumerate_sources_action(), _enumerate_sources_action(),
                   ANSWER])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = stub
    retriever._run_ceiling_binds = lambda: drifted["now"]

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    assert stub.calls[1]["cursor"] == "cursor-1"
    assert [call["ceiling_binds"] for call in stub.calls] == [False, True]
    [outcome] = result.enumerations
    assert [item.source_id for item in outcome.items] == ["s1", "s2"]
    assert outcome.source_scoped is True
    first, second = _steps(result, "enumerate")
    assert not first.summary.endswith(SELECTED_SOURCES_SCOPE_SUFFIX)
    assert "source_scoped" not in first.detail
    assert second.summary == (
        f"枚举来源清单: 已全部列出 2 条{SELECTED_SOURCES_SCOPE_SUFFIX}")
    assert second.detail["source_scoped"] is True
    [answer_step] = _steps(result, "answer")
    assert answer_step.detail["enumerated_items"] == 2
    [card] = typed_collection_results(result.enumerations, payload_chars=100_000)
    assert card.source_scoped is True


def test_the_scoped_mark_stays_once_set(repo):  # noqa: F811
    """粘性的另一半:第一页在收窄/漂移下列出,之后探针转回假,标记也不会掉。"""
    notebook = _seed(repo, formulas=1)
    partial = SourceEnumeration(
        items=(_source_item(notebook_id=notebook.id),), cursor="cursor-1",
        extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=1, complete=False,
                           has_more=True, total=2, truncated_reason="budget"),
    )
    rest = SourceEnumeration(
        items=(_source_item("s2", "论文二", notebook.id),), cursor=None,
        extra_pages=0, payload_chars=10,
        coverage=_coverage(returned=1, returned_total=2, total=2),
    )
    bound = {"now": True}

    class _ReleasingStub(_StubEnumeration):
        def enumerate_sources(self, *args, **kwargs):
            try:
                return super().enumerate_sources(*args, **kwargs)
            finally:
                bound["now"] = False

    llm = _SeqLLM([_enumerate_sources_action(), _enumerate_sources_action(),
                   ANSWER])
    retriever, limits = _retriever(repo, llm)
    retriever.collection_enumeration = _ReleasingStub([partial, rest])
    retriever._run_ceiling_binds = lambda: bound["now"]

    result = retriever.run(notebook.id, "有哪几篇", "", limits=limits)

    [outcome] = result.enumerations
    assert outcome.source_scoped is True
    assert _steps(result, "enumerate")[-1].summary.endswith(
        SELECTED_SOURCES_SCOPE_SUFFIX)


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


def _unresolved_step(retriever, notebook_id, title):
    """同一个标题、清单里根本没有它时的那条 skip——越界拒绝必须与它逐字段相同。"""
    state = _roster_state(retriever, notebook_id, [
        _source_item("s-other", "另一篇", notebook_id)])
    retriever._action_read_document(state, ReflectDecision(
        next_action=READ_DOCUMENT_ACTION, read_document_source=title))
    return state.trace[-1]


def _assert_refused(state, unresolved):
    """越界拒绝与「清单里没有这个标题」不可区分(同一句话、同一 detail),且这一篇的
    任何东西都没有往下走。"""
    skip = state.trace[-1]
    assert skip.step_type == "skip"
    assert skip.summary == unresolved.summary
    assert skip.detail == unresolved.detail
    assert skip.detail["reason"] == "document_read_unresolved"
    assert skip.detail["matches"] == 0
    assert state.document_reads == []
    assert state.document_reads_done == 0
    assert not state.document_reads_by_id


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
        unresolved = _unresolved_step(retriever, notebook.id, "无摘要文档")

    _assert_refused(state, unresolved)


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
        unresolved = _unresolved_step(retriever, notebook.id, "别的库的文档")

    _assert_refused(state, unresolved)


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



# ------------------------------------------- 集合读取判词:每个入口显式传、每 run 一次


_CEILING_ENTRY_POINTS = frozenset({
    "enumerate_elements", "enumerate_kg_objects", "enumerate_sources",
    "resolve_source_title", "collection_map", "collection_map_text",
})
_CEILING_CALLER_FILES = (
    "backend/app/services/reasoning_retrieval.py",
    "backend/app/services/ask_service.py",
)
# 以 getattr 取出入口、再在同一个函数里带着判词调用的座位。
_CEILING_GETATTR_SEATS = frozenset({"_plugin_collection_overview"})


def test_every_collection_entry_call_passes_the_ceiling_verdict():
    """推理/问答侧对执行器与目录服务的每一次调用都**显式**传 ``ceiling_binds``。

    入口的默认值是 True(过度过滤而不是泄漏),所以漏传不会报错,只会让浏览器默认
    的全选请求按整份天花板下推、把没有证据的知识对象从地图和清单里悄悄丢掉。这条
    守卫按 AST 数:属性调用必须带这个关键字;以 ``getattr(x, "<入口>")`` 取出的入口
    只许出现在登记过的座位里,且那个座位自己必须带着关键字调用它。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders, seen_calls, seen_seats = [], 0, set()
    for relative in _CEILING_CALLER_FILES:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {kw.arg for kw in node.keywords}
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr in _CEILING_ENTRY_POINTS):
                    seen_calls += 1
                    if "ceiling_binds" not in keywords:
                        offenders.append(f"{relative}:{node.lineno} {func.name}")
                if (isinstance(node.func, ast.Name) and node.func.id == "getattr"
                        and len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value in _CEILING_ENTRY_POINTS):
                    if func.name not in _CEILING_GETATTR_SEATS:
                        offenders.append(
                            f"{relative}:{node.lineno} getattr in {func.name}")
                        continue
                    seen_seats.add(func.name)
                    if not any(
                        isinstance(inner, ast.Call)
                        and any(kw.arg == "ceiling_binds" for kw in inner.keywords)
                        for inner in ast.walk(func)
                    ):
                        offenders.append(f"{relative}:{node.lineno} {func.name}")
    assert offenders == []
    # 活性:守卫真的看见了调用点(枚举三动作 + 标题解析 + 首轮地图 + 无图早退)。
    assert seen_calls >= 6
    assert seen_seats == _CEILING_GETATTR_SEATS


def test_the_ceiling_verdict_is_computed_once_per_retrieval_run():
    """同一个检索 run 里判词只探一次(集合读取的所有读者说同一件事);run 之外现算。"""
    from app.services.retrieval_run import retrieval_run

    class _Probe:
        def __init__(self, answers):
            self.answers = list(answers)
            self.calls = 0

        def unsafe_source_scope_restricted(self, notebook_id):
            self.calls += 1
            return self.answers.pop(0)

    frozen = {"mode": "include", "source_ids": ["s1"], "narrowed": False,
              "hidden_source_ids": [], "owner_id": ""}
    probe = _Probe([False, True])
    with retrieval_run(run_kind="ask_chunk", actor_id="u"):
        with source_scope_context("nb", frozen):
            assert rr.ceiling_binds_for_run(probe) is False
            assert rr.ceiling_binds_for_run(probe) is False
            assert rr.knowhow_completeness_reachable(probe, "nb") is True
    assert probe.calls == 1
    with source_scope_context("nb", frozen):
        assert rr.ceiling_binds_for_run(probe) is True
    assert probe.calls == 2


def test_plugin_collection_overview_carries_the_run_verdict():
    """插件引擎的地图回调带着判词读,而不是落到入口的默认值。"""
    from types import SimpleNamespace

    from app.services.ask_service import AskService

    received = []
    service = SimpleNamespace(
        collection_catalog=SimpleNamespace(
            collection_map_text=lambda nb, **kw: received.append(kw) or "map"),
        retrieval=SimpleNamespace(
            unsafe_source_scope_restricted=lambda nb: False),
    )
    overview = AskService._plugin_collection_overview(service)
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert overview("nb") == "map"
    assert overview("nb") == "map"
    assert received == [{"ceiling_binds": True}, {"ceiling_binds": False}]
    assert AskService._plugin_collection_overview(
        SimpleNamespace(collection_catalog=object())) is None


def test_chunk_engine_overviews_pass_the_run_verdict(monkeypatch):
    """chunk 引擎的目录概览把同一个判词作为 ``source_scoped`` 交给目录拼装(拼装方
    把它原样作为 ``ceiling_binds`` 交给执行器),无图早退读地图时同样带着它。"""
    from types import SimpleNamespace

    from app.services.ask_service import AskService
    from app.services.document_overview import OverviewIntent

    intent = OverviewIntent("catalog")
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert _catalog_kwargs(monkeypatch, intent)["source_scoped"] is True
    assert _catalog_kwargs(monkeypatch, intent)["source_scoped"] is False

    received = []

    class _Catalog:
        def collection_map(self, notebook_id, **kwargs):
            received.append(kwargs)
            raise RuntimeError("stop after the call")

    service = SimpleNamespace(
        settings=SimpleNamespace(reasoning_enum_tools_enabled=True,
                                 reasoning_chunk_search_enabled=True),
        collection_catalog=_Catalog(), collection_enumeration=object(),
        retrieval=SimpleNamespace(
            unsafe_source_scope_restricted=lambda nb: True),
    )
    frozen = {"mode": "include", "source_ids": ["s1"], "narrowed": False,
              "hidden_source_ids": [], "owner_id": ""}
    with source_scope_context("nb", frozen):
        assert AskService._no_kg_scope_admits_run(service, "nb") is False
    assert received == [{"ceiling_binds": True}]
