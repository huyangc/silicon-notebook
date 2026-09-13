"""T4:外部证据从检索终态一路走到答案、锚点与引用。

设计文档 ``docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md``
§六(6.1/6.2/6.3)与 §九 不变量 3/6/9。这里钉的是**贯通**,不是渲染细节
(那在 ``test_evidence_context_service``):

  ① ``ReasoningResult.external_evidence`` → ``ReasoningEvidenceSnapshot`` →
     ``ResponseDraftInput`` → 合成上下文里一条 ``[k6001]``;
  ② 答案里的 ``[k6001]`` 绑成 ``object_type="external"`` + 非空 ``url`` 的锚点,
     而且 ``grounded`` 语义不变(只引用外部证据的答案同样 grounded);
  ③ 引用列表尾部多出外部条目;
  ④ **零外部证据时 payload 逐键不变**——对 ``AskResponse.model_dump()`` 的键
     集合做前后对照,而不是「看起来差不多」。

走的是 ``ReasoningRetriever.run_stage`` 这一个类型化产出口(与
``test_reasoning_ask`` 的零证据用例同款),所以 ``_run_reasoning_stage`` 之后的
每一层——快照、``ResponseDraftInput``、``_answer_reasoning``、``parse_anchors``、
``AskResponse`` 装配——全都是生产代码。
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


EXTERNAL_KEY = "k6001"


class _RecordingSeqLLM:
    """计划 / 反思 / 作答三路定死,并记下每一次作答看到的 prompt。"""

    configured = True

    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
        self.answer_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        self.answer_prompts.append(messages[0]["content"])
        return json.dumps(self._answer)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    return repo


def _bind(repo, client):
    bind_chat_client(repo, "reasoning_agent", client)
    bind_chat_client(repo, "evidence_refine", client)
    bind_chat_client(repo, "ask_answer", client)


def _seed(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"},
         "evidence": []},
    ], [])
    return notebook


def _external(index: int = 1):
    from app.domain.reflect_action import ExternalEvidence

    return ExternalEvidence(
        key=f"ext:acme:{index}",
        plugin_id="acme",
        action="web_search",
        source_label="IEEE Xplore",
        title=f"外部论文-{index}",
        excerpt=f"外部摘录-{index}",
        url=f"https://example.org/paper/{index}",
        location_label=f"§{index}",
    )


def _patch_retrieval(monkeypatch, *, external):
    """把检索阶段换成确定性的「一条 KG 命中 + N 条外部证据」。"""
    from app.application.ask_reasoning import ReasoningEvidenceSnapshot
    from app.services.reasoning_retrieval import ReasoningRetriever

    def _run_stage(self, stage, runtime):
        return ReasoningEvidenceSnapshot(
            top_hits=(), elements=(), trace=(), chunks=(), chains=(),
            attempted=(), enumerations=(), collection_map_text="",
            outline=(), outline_evidence=(), baseline_manifest=None,
            external_evidence=tuple(external),
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _run_stage)


def _patch_external_onto_real_retrieval(monkeypatch, *, external):
    """只往**真实**检索终态上挂外部证据,库内那一半原样跑生产代码。

    开/关两轮的对照必须这样做:全空快照那条路根本不会进合成(库内零证据时
    合成整段短路),对照出来的会是「有没有答案」而不是「prompt 差了什么」。
    """
    import dataclasses

    from app.services.reasoning_retrieval import ReasoningRetriever

    original = ReasoningRetriever.run_stage

    def _run_stage(self, stage, runtime):
        return dataclasses.replace(
            original(self, stage, runtime), external_evidence=tuple(external)
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _run_stage)


def _ask(repo, notebook, question="RTL到GDSII流程"):
    return repo.ask(notebook.id, AskRequest(question=question, mode="reasoning"))


# --------------------------------------------------------------------------
# ① + ② + ③:一条外部证据贯通到 prompt、锚点与引用
# --------------------------------------------------------------------------


def test_external_evidence_reaches_the_synthesis_prompt_as_a_citable_item(
    arepo, monkeypatch
):
    notebook = _seed(arepo)
    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"库外资料这样说 [{EXTERNAL_KEY}]。", "grounded": True})
    _bind(arepo, llm)
    _patch_retrieval(monkeypatch, external=[_external()])

    response = _ask(arepo, notebook)

    prompt = llm.answer_prompts[-1]
    assert f"{EXTERNAL_KEY}: [external · IEEE Xplore] 外部论文-1 (§1) — 外部摘录-1" in prompt
    # §九 不变量 9 的合成侧一半:块带 [external] 前缀,prompt 带那条规则句。
    assert "14. Items tagged [external · <source>]" in prompt
    # 不变量 7:模型看不到 plugin_id。
    assert "acme" not in prompt


def test_the_external_marker_binds_to_an_external_anchor_with_a_url(
    arepo, monkeypatch
):
    notebook = _seed(arepo)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"库外资料这样说 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_retrieval(monkeypatch, external=[_external()])

    response = _ask(arepo, notebook)

    assert [anchor.key for anchor in response.anchors] == [EXTERNAL_KEY]
    anchor = response.anchors[0]
    assert anchor.object_type == "external"
    assert anchor.object_id == "ext:acme:1"
    assert anchor.tier == "external"
    assert anchor.url == "https://example.org/paper/1"
    # 不变量 3 的运行时一半:外部锚点没有本库句柄。
    assert anchor.source_id == "" and anchor.element_id == ""
    # plugin_id 只在 provenance 里,不在任何展示字段里。
    assert anchor.provenance["plugin_id"] == "acme"


def test_an_answer_grounded_only_in_external_evidence_stays_grounded(
    arepo, monkeypatch
):
    """§6.3:``grounded``/``evidence_level`` 语义不变——有合法锚点即 grounded,
    外部锚点同样算。把外部证据排除在证据池之外会让这条红。"""
    notebook = _seed(arepo)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"库外资料这样说 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_retrieval(monkeypatch, external=[_external()])

    response = _ask(arepo, notebook)

    assert response.grounded is True


def test_external_citations_are_appended_to_the_tail_of_the_citation_list(
    arepo, monkeypatch
):
    notebook = _seed(arepo)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"库外资料这样说 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_retrieval(monkeypatch, external=[_external(1), _external(2)])

    response = _ask(arepo, notebook)

    external = [c for c in response.citations if c.tier == "external"]
    assert [c.label for c in external] == [
        "IEEE Xplore · 外部论文-1", "IEEE Xplore · 外部论文-2",
    ]
    # 尾部:外部条目之后没有库内条目。
    assert response.citations[-len(external):] == external
    assert external[0].url == "https://example.org/paper/1"
    assert external[0].source_id == "" and external[0].element_id == ""


def test_two_external_items_take_consecutive_keys_in_their_own_segment(
    arepo, monkeypatch
):
    """号段是 6000+,不与集合清单预览的 k5001+ 相撞。"""
    notebook = _seed(arepo)
    llm = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "两条 [k6001][k6002]。", "grounded": True})
    _bind(arepo, llm)
    _patch_retrieval(monkeypatch, external=[_external(1), _external(2)])

    response = _ask(arepo, notebook)

    assert [anchor.key for anchor in response.anchors] == ["k6001", "k6002"]
    assert "k5001" not in llm.answer_prompts[-1]


# --------------------------------------------------------------------------
# ④:零外部证据时 payload 逐键不变
# --------------------------------------------------------------------------


def _payload_keys(response) -> set[str]:
    """答案 payload 的**全部**键(含锚点/引用每一条的键),摊平成一个集合。"""
    keys: set[str] = set()

    def walk(value, prefix=""):
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(f"{prefix}{key}")
                walk(item, f"{prefix}{key}.")
        elif isinstance(value, list):
            for item in value:
                walk(item, prefix)

    walk(response.model_dump())
    return keys


def test_a_run_without_external_evidence_emits_the_same_payload_keys(
    arepo, monkeypatch
):
    """接入这个特性之后,没有插件动作的一轮 payload 一个键都不多:``url`` 的
    ``exclude_if`` 让它整体缺席,``is_external`` 只活在公开投影上。

    对照的是**同一个问题、同一份库内证据**的开/关两轮 —— 开的那轮多出来的键
    恰好只有 ``anchors[].url`` 与 ``citations[].url``,其余逐键相等。"""
    notebook = _seed(arepo)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "库内答案 [k1]。", "grounded": True}))
    _patch_external_onto_real_retrieval(monkeypatch, external=[])
    off = _ask(arepo, notebook)

    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"库内答案 [k1] 与库外 [{EXTERNAL_KEY}]。",
                "grounded": True}))
    _patch_external_onto_real_retrieval(monkeypatch, external=[_external()])
    on = _ask(arepo, notebook)

    # 关的那轮:``url`` 这个键在整份 payload 里一次都不出现。
    assert "url" not in _payload_keys(off)
    assert off.anchors and all(anchor.url == "" for anchor in off.anchors)
    assert all(citation.url == "" for citation in off.citations)
    # 开的那轮:只有外部那几条多带 ``url``,库内条目的形状与关的那轮一模一样。
    payload = on.model_dump()
    library_anchors = [
        row for row in payload["anchors"] if row.get("tier") != "external"
    ]
    external_anchors = [
        row for row in payload["anchors"] if row.get("tier") == "external"
    ]
    assert library_anchors and external_anchors
    assert all("url" not in row for row in library_anchors)
    assert all(row["url"] for row in external_anchors)
    off_library_keys = {
        key for row in off.model_dump()["anchors"] for key in row
    }
    assert {key for row in library_anchors for key in row} == off_library_keys


def test_the_synthesis_prompt_is_byte_identical_without_external_evidence(
    arepo, monkeypatch
):
    """同一个问题、同一份库内证据,开/关外部证据两轮的 prompt 只差外部那一段
    与规则 14——把差集删掉之后必须逐字节相等。这是 §九 不变量 6 在合成侧的
    验收断言。"""
    notebook = _seed(arepo)
    llm_off = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "库内答案。", "grounded": False})
    _bind(arepo, llm_off)
    _patch_external_onto_real_retrieval(monkeypatch, external=[])
    _ask(arepo, notebook)
    off = llm_off.answer_prompts[-1]

    llm_on = _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "库内答案。", "grounded": False})
    _bind(arepo, llm_on)
    _patch_external_onto_real_retrieval(monkeypatch, external=[_external()])
    _ask(arepo, notebook)
    on = llm_on.answer_prompts[-1]

    assert off != on
    rule = on[on.index("14. Items tagged"):on.index("\n\n", on.index("14. Items tagged")) + 1]
    block_start = on.index("\n\n[External evidence]\n")
    block = on[block_start:on.index("\n\n", block_start + 2)]
    assert on.replace(rule, "", 1).replace(block, "", 1) == off


# --------------------------------------------------------------------------
# 评审修复:引用只发给进过 prompt 的条目 + 丢弃有披露 + 预算上界
# --------------------------------------------------------------------------


def _synthesis_detail(response) -> dict:
    steps = [step for step in (response.reasoning_trace or [])
             if step.step_type == "synthesis"]
    assert steps, "合成步必须在轨迹里"
    return steps[-1].detail


def test_a_budget_dropped_external_item_is_never_cited(arepo, monkeypatch):
    """P1(评审):引用是「答案可能引自这条材料」的声明。被外部段预算挤掉的
    条目模型从没见过——把它列进引用,就是给一份没看过它的答案挂来源。"""
    notebook = _seed(arepo)
    # 外部段预算收到只装得下一条。
    monkeypatch.setattr(
        "app.services.evidence_context.EXTERNAL_EVIDENCE_CONTEXT_CHARS", 60,
    )
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"只引第一条 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_retrieval(
        monkeypatch, external=[_external(1), _external(2), _external(3)])

    response = _ask(arepo, notebook)

    external = [c for c in response.citations if c.tier == "external"]
    assert [c.label for c in external] == ["IEEE Xplore · 外部论文-1"]
    # 被丢掉的两条既不在引用里,也不在答案的任何字段里。
    payload = json.dumps(response.model_dump(), ensure_ascii=False)
    assert "外部论文-2" not in payload and "外部论文-3" not in payload


def test_the_trace_discloses_how_many_external_items_were_dropped(
    arepo, monkeypatch
):
    """P2(评审):静默丢弃是零披露。五条进来、预算只装三条 ⇒ 合成轨迹步的
    detail 里 ``external_dropped == 2``。"""
    notebook = _seed(arepo)
    one_line = len(
        "k6001: [external · IEEE Xplore] 外部论文-1 (§1) — 外部摘录-1"
    )
    monkeypatch.setattr(
        "app.services.evidence_context.EXTERNAL_EVIDENCE_CONTEXT_CHARS",
        3 * one_line + 2,
    )
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"引一条 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_retrieval(
        monkeypatch, external=[_external(index) for index in range(1, 6)])

    response = _ask(arepo, notebook)

    detail = _synthesis_detail(response)
    assert detail["external_included"] == 3
    assert detail["external_dropped"] == 2
    assert len([c for c in response.citations if c.tier == "external"]) == 3


def test_a_run_without_external_evidence_writes_neither_disclosure_key(
    arepo, monkeypatch
):
    """关闭态是**稀疏**的:一个键都不写。

    恒零地写进去会改掉每一条既有答案的持久化 payload 与 synthesis 轨迹 detail
    ——`test_ask_repository_golden` 的冻结 oracle 正是这样红的,而「无外部证据时
    逐键不变」是这个特性自己许下的承诺。"""
    notebook = _seed(arepo)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "库内答案 [k1]。", "grounded": True}))
    _patch_external_onto_real_retrieval(monkeypatch, external=[])

    detail = _synthesis_detail(_ask(arepo, notebook))

    assert "external_included" not in detail
    assert "external_dropped" not in detail


def test_the_baseline_upper_bound_includes_what_the_external_block_used(
    arepo, monkeypatch
):
    """P2(评审):``budget_chars`` 的合同是「这段上下文不会超过它」
    (``test_answer_quality_contract`` 直接断言 ``len(context_block) <=
    budget_chars``)。外部段是在总预算截断**之后**另加的一份独立预算,所以
    上界必须含它实际占用的字符,否则那条不变量在有外部证据的一轮上假红。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    notebook = _seed(arepo)
    captured: dict = {}
    service = arepo._runtime.ask_service()
    original = service._answer_reasoning

    def _spy(*args, **kwargs):
        sink = kwargs.setdefault("baseline_sink", {})
        result = original(*args, **kwargs)
        captured.update(sink)
        return result

    monkeypatch.setattr(service, "_answer_reasoning", _spy)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": f"引外部 [{EXTERNAL_KEY}]。", "grounded": True}))
    _patch_external_onto_real_retrieval(monkeypatch, external=[_external()])

    _ask(arepo, notebook)

    block = captured["context_block"]
    limits = ask_retrieval_limits("standard")
    library_budget = limits.kg_context_chars + limits.chunk_context_chars
    assert "[External evidence]" in block
    # 上界成立……
    assert len(block) <= captured["budget_chars"]
    # ……而且它确实比纯库内预算大了「外部段实际占用」那么多,不是被放宽成一个
    # 名义额度(那样这条断言就会看到 +EXTERNAL_EVIDENCE_CONTEXT_CHARS)。
    from app.domain.reflect_action import EXTERNAL_EVIDENCE_CONTEXT_CHARS
    used = captured["budget_chars"] - library_budget
    assert 0 < used < EXTERNAL_EVIDENCE_CONTEXT_CHARS


# --------------------------------------------------------------------------
# 生产接线:`_run_reasoning_stage` 必须把 privacy 闸算出的外泄串交给检索器
# --------------------------------------------------------------------------
#
# ``ReasoningRetriever`` 与 ``_build_reasoning_retriever`` 都是 fail-closed 的:
# ``plugin_egress_question`` 为空 ⇒ 一条插件动作都不提供。所以「忘了传」这个
# bug 不会响亮失败,它会安静地把整条通道关掉——上线之后表现为「插件配了但从来
# 不被调用」。这两条用例就是那个静默失效的守卫。


def _plugin_spec():
    from app.domain.reflect_action import (
        ReflectActionDescriptor, ReflectActionParameter, ReflectActionSpec,
    )

    return ReflectActionSpec("acme.search", "acme_index", ReflectActionDescriptor(
        name="search_papers",
        description="Search an external paper index and return abstracts.",
        source_label="IEEE Xplore",
        parameters=(ReflectActionParameter(
            name="query", description="what to look for", kind="text",
            required=True,
        ),),
        max_calls_per_run=2,
    ))


class _RecordingHost:
    """宿主替身:记下 specs()/invoke() 各收到什么。"""

    def __init__(self, spec, outcome):
        self.spec, self._outcome = spec, outcome
        self.calls: list = []

    def specs(self, deadline_monotonic, *, cancellation=None, event_sink=None):
        return (self.spec,)

    def invoke(self, spec, call):
        self.calls.append(call)
        return self._outcome


def test_the_production_stage_hands_the_retriever_the_egress_question(
    arepo, monkeypatch
):
    """``_build_reasoning_retriever(egress_question=...)`` 真的被传了,而且传的
    正是 ``_egress_question(prepared)`` 的返回值——不是 ``research_question``
    (意图合同的合成串,§九 不变量 1 明令不得外送)。"""
    from app.services import ask_service as ask_service_module

    notebook = _seed(arepo)
    computed: list[str] = []
    original_egress = ask_service_module._egress_question

    def _spy_egress(prepared):
        value = original_egress(prepared)
        computed.append(value)
        return value

    monkeypatch.setattr(ask_service_module, "_egress_question", _spy_egress)

    service = arepo._runtime.ask_service()
    built: list = []
    original_build = service._build_reasoning_retriever

    def _spy_build(**kwargs):
        retriever = original_build(**kwargs)
        built.append((kwargs, retriever))
        return retriever

    monkeypatch.setattr(service, "_build_reasoning_retriever", _spy_build)
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "库内答案。", "grounded": False}))

    _ask(arepo, notebook)

    assert built, "生产路径必须经过 _build_reasoning_retriever"
    kwargs, retriever = built[-1]
    assert computed, "_egress_question 必须在这条路径上被调用"
    # 传了,而且传的就是 privacy 闸算出来的那一个串。
    assert kwargs["egress_question"] == computed[-1]
    assert retriever.plugin_egress_question == computed[-1]
    # fail-closed 的另一半:非空,否则整条通道静默关闭。
    assert retriever.plugin_egress_question


def test_the_host_receives_the_egress_question_not_the_research_question(
    arepo, monkeypatch
):
    """端到端:插件动作真的被提供、真的被调用,而宿主拿到的 ``question`` 就是
    外泄串——而不是检索器自己搜索用的 ``research_question``。"""
    from app.domain.reflect_action import ReflectActionItem, ReflectActionOutcome
    from app.services import ask_service as ask_service_module

    notebook = _seed(arepo)
    spec = _plugin_spec()
    host = _RecordingHost(spec, ReflectActionOutcome(
        (ReflectActionItem("外部论文", "外部摘录正文",
                           "https://example.org/p1", "§1"),),
        "", "", False,
    ))
    service = arepo._runtime.ask_service()
    service.reflect_action_host = host
    arepo.settings.reasoning_max_plugin_actions = 2

    computed: list[str] = []
    original_egress = ask_service_module._egress_question

    def _spy_egress(prepared):
        computed.append(original_egress(prepared))
        return computed[-1]

    monkeypatch.setattr(ask_service_module, "_egress_question", _spy_egress)

    searched: list[str] = []

    from app.services.reasoning_retrieval import ReasoningRetriever

    original_run = ReasoningRetriever.run_stage

    def _record_run(self, stage, runtime):
        searched.append(stage.question)
        return original_run(self, stage, runtime)

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _record_run)

    # 一轮先花掉一条库内通道并且没有新进展(按轮闸要求「库内已空手」),
    # 第二轮才轮得到插件动作。
    _bind(arepo, _RecordingSeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[
            {"next_action": "add_subquery", "reason": "换个说法再找一次",
             "new_sub_query": {"query": "RTL到GDSII流程", "types": [],
                               "prefer": "balanced", "reason": "同义改写"}},
            {"next_action": "search_papers", "reason": "库内查不到",
             "search_papers": {"query": "RTL to GDSII flow"}},
            {"next_action": "answer", "sufficient": True},
        ],
        answer={"answer": "库外资料这样说 [k6001]。", "grounded": True}))

    response = _ask(arepo, notebook)

    assert host.calls, "插件动作必须真的被提供并被调用"
    # 宿主拿到的就是 privacy 闸算出来的那一个串,而且它是用户**自己敲的**措辞。
    assert computed and host.calls[0].question == computed[-1]
    assert host.calls[0].question == "RTL到GDSII流程"
    # 检索器**自己**搜索用的是另一条路上的串(``ReasoningRunInput.question``,
    # 即 research_question);外泄的绝不是它 —— 除非二者本来就是同一句话,而
    # 上一行已经把「外泄的那句」钉死成用户原话了。
    assert searched, "run_stage 必须被走到(否则上面几条是空转)"
    # 材料一路走到了答案。
    assert [anchor.object_id for anchor in response.anchors] == [
        "ext:acme_index:1",
    ]
