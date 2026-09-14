"""PR-B 甲 T2:reasoning 的**原文段引用卡**腿。

计划 ``~/.claude/plans/serialized-dazzling-pie.md`` 的「PR-B → 甲 引用卡」。
在这条腿之前,reasoning 的 ``citations`` 只由 KG 命中 / 记忆 / element 锚点 /
集合行产生,一轮**纯 chunk 命中**的问答引用列表是空的——而前端的引用展示是
anchor 优先、全有全无(``frontend/app/answer-formatting.ts``),所以「零锚点」
的那几种形态(模型没吐 ``[k]``、合成两次都失败、公开分享页、tier 徽章计数)
在纯原文命中的库上什么来源都看不到。chunk 模式一直有这个能力,归一到
reasoning 之前必须补齐,否则第 ④ 步退役 chunk 流水线就是功能回退。

这里钉的是**候选集口径**与**回退可见性**,不是卡片渲染细节(那在
``test_evidence_context_service`` 的 ``chunk_citations`` 四条):

  ① 零锚点(模型正文里没有一个 ``[k]``)→ ``anchors == []`` 但引用非空,
     且卡上的 label / location_label / quoted_span 对得上那一段;
  ② 合成两次都抛错(``_answer_with_retry`` 用尽)→ 引用**仍然**非空。
     这是这条腿最该生效的场景:``baseline_sink`` 在模型调用前就填好了
     (``ask_service.py`` 的 "Fill the sink BEFORE the model call"),所以
     「答案没写出来」不该连「查到了哪几段」也一起丢掉;
  ③ 卡数 == **进了合成 prompt 的** chunk 数,不随模型绑了几个锚点变化;
  ④ 被 ``chunk_context_chars`` 字符预算挤掉的那一段**不发卡**——引用是
     「答案可能引自这一段」的声明,模型从没见过的段落列进去就是给一份没读过
     它的答案挂来源(与外部证据段 ``_append_external_context`` 同一条规则);
  ⑤ 外部证据仍在引用列表**尾部**(库内新腿插在它之前)。

检索侧整体替身(``run_stage``)与 ``test_reasoning_external_evidence`` 同款:
候选集口径是装配侧的性质,用确定性的 ``stage.chunks`` 才能把「预算挤掉的那一
条」钉死;``_run_reasoning_stage`` 之后的每一层都是生产代码。
"""
import dataclasses
import json

import pytest
from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


class _SeqLLM:
    """计划 / 反思 / 作答三路定死。``answer`` 是 None 时作答两次都抛错。"""

    configured = True
    model = "fake-answer-llm"

    def __init__(self, answer):
        self._answer = answer
        self.answer_calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "增益"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        self.answer_calls += 1
        if self._answer is None:
            raise RuntimeError("合成模型这一轮答不出来")
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
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, client)
    return client


def _seed(repo):
    """一个知识对象只为让「无图无集合」的早退闸不生效。

    它进不了引用:下面的 ``run_stage`` 替身把 ``top_hits`` 交成空元组,
    所以本文件里每一条引用都只可能来自原文段那条新腿(``graph_ppr_enabled``
    之类的旋钮同理被整体替身覆盖,不必单独关)。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "增益概述", "section_path": "1"}, "evidence": []},
    ], [])
    return notebook


def _chunk(index: int, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        source_id=f"src-{index}",
        source_title=f"手册-{index}",
        section_path=f"§{index} 增益",
        text=text,
        element_ids=[f"el-{index}-a", f"el-{index}-b"],
        score=1.0,
        # 降序 = 入参序,好让「id_map 序」与「stage.chunks 序」在断言里可比。
        relevance=1.0 - index * 0.1,
    )


def _patch_retrieval(monkeypatch, *, chunks, external=()):
    """把检索阶段换成确定性的「N 条原文段(+ M 条外部证据)」终态。"""
    from app.application.ask_reasoning import ReasoningEvidenceSnapshot
    from app.services.reasoning_retrieval import ReasoningRetriever

    def _run_stage(self, stage, runtime):
        return ReasoningEvidenceSnapshot(
            top_hits=(), elements=(), trace=(), chunks=tuple(chunks), chains=(),
            attempted=(), enumerations=(), collection_map_text="",
            outline=(), outline_evidence=(), baseline_manifest=None,
            external_evidence=tuple(external),
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _run_stage)


def _squeeze_chunk_budget(monkeypatch, chars: int):
    """把 ``chunk_context_chars`` 压到只装得下一条原文段。

    它是 ``ask_retrieval_policy`` 里冻结的档位常量(不是 Settings 旋钮),所以
    对照实验只能在 ``ask_service`` 读取它的那一跳上做。
    """
    from app.services import ask_service as ask_service_module

    real = ask_service_module.ask_retrieval_limits
    monkeypatch.setattr(
        ask_service_module, "ask_retrieval_limits",
        lambda effort: dataclasses.replace(
            real(effort), chunk_context_chars=chars),
    )


def _ask(repo, notebook, question="增益"):
    return repo.ask(notebook.id, AskRequest(question=question, mode="reasoning"))


# --------------------------------------------------------------------------
# ① 零锚点回退
# --------------------------------------------------------------------------


def test_an_answer_without_any_marker_still_lists_the_passages_it_was_given(
    arepo, monkeypatch
):
    notebook = _seed(arepo)
    _bind(arepo, _SeqLLM({"answer": "增益基本稳定。", "grounded": False}))
    _patch_retrieval(monkeypatch, chunks=[
        _chunk(1, "源极负反馈让增益稳定。"),
        _chunk(2, "温度漂移对增益的影响有限。"),
    ])

    response = _ask(arepo, notebook)

    assert response.answer, (response.conclusion, response.model_errors)
    # 前提:这一轮模型一个 [k] 都没吐 —— 前端因此走 citations 回退分支。
    assert response.anchors == []
    assert [c.label for c in response.citations] == [
        "手册-1 · §1 增益", "手册-2 · §2 增益",
    ]
    first = response.citations[0]
    assert first.source_id == "src-1"
    assert first.location_label == "§1 增益"
    assert first.quoted_span == "源极负反馈让增益稳定。"
    assert first.element_id == "el-1-a"


def test_the_cards_survive_a_synthesis_that_never_produced_an_answer(
    arepo, monkeypatch
):
    """合成两次都抛错时引用仍非空。

    ``baseline_sink`` 在模型调用前填好,候选集因此照样成立——这条腿最该生效
    的正是这个形态:用户拿不到答案时,「本次查到了哪几段原文」是响应里仅剩的
    有用东西。把候选集改成读一个合成**之后**才写的字段,这条会红。
    """
    notebook = _seed(arepo)
    llm = _bind(arepo, _SeqLLM(None))
    _patch_retrieval(monkeypatch, chunks=[_chunk(1, "源极负反馈让增益稳定。")])

    response = _ask(arepo, notebook)

    # 前提:合成真的被叫过并且两次尝试都用掉了(客户端层自己还会再重掷一次,
    # 所以这里只钉下界,不钉一个会随重试策略漂移的精确数)。
    assert llm.answer_calls >= 2, llm.answer_calls
    assert response.answer == ""
    # 前提:走的是「诚实降级」那条分支,不是「压根没检索到东西」。
    assert "本次答案合成未产出内容" in response.conclusion
    assert response.anchors == []
    assert [c.label for c in response.citations] == ["手册-1 · §1 增益"]


# --------------------------------------------------------------------------
# ③ 卡数跟着 prompt 准入走,不跟着锚点走
# --------------------------------------------------------------------------


@pytest.mark.parametrize("answer_text", [
    "第一段这样说 [k1]。",
    "两段都这样说 [k1][k2]。",
])
def test_card_count_tracks_prompt_admission_not_how_many_anchors_were_bound(
    arepo, monkeypatch, answer_text
):
    notebook = _seed(arepo)
    _bind(arepo, _SeqLLM({"answer": answer_text, "grounded": True}))
    _patch_retrieval(monkeypatch, chunks=[
        _chunk(1, "源极负反馈让增益稳定。"),
        _chunk(2, "温度漂移对增益的影响有限。"),
        _chunk(3, "版图失配是另一项误差来源。"),
    ])

    response = _ask(arepo, notebook)

    assert [a.object_type for a in response.anchors] == (
        ["chunk"] * answer_text.count("[k"))
    # 三条都进了 prompt(预算远大于它们),所以三条都发卡——与绑了 1 个还是
    # 2 个锚点无关。改成「按锚点发卡」的话,这两个参数化用例会分别得到 1 和 2。
    assert [c.label for c in response.citations] == [
        "手册-1 · §1 增益", "手册-2 · §2 增益", "手册-3 · §3 增益",
    ]


# --------------------------------------------------------------------------
# ④ 被字符预算挤掉的那一段不发卡
# --------------------------------------------------------------------------


def test_a_passage_squeezed_out_by_the_character_budget_gets_no_card(
    arepo, monkeypatch
):
    notebook = _seed(arepo)
    _bind(arepo, _SeqLLM({"answer": "增益基本稳定。", "grounded": False}))
    # 第一条就把 400 字符的分区吃满,第二条连 "k2: " 的位置都没有。
    _patch_retrieval(monkeypatch, chunks=[
        _chunk(1, "增" * 1_000), _chunk(2, "漂" * 1_000),
    ])
    _squeeze_chunk_budget(monkeypatch, 400)

    response = _ask(arepo, notebook)

    assert response.anchors == []
    assert [c.label for c in response.citations] == ["手册-1 · §1 增益"]


def test_the_same_two_passages_both_get_a_card_when_the_budget_fits_them(
    arepo, monkeypatch
):
    """上一条的对照:证明那条红的是**预算**,不是「第二条 chunk 本身不合格」。"""
    notebook = _seed(arepo)
    _bind(arepo, _SeqLLM({"answer": "增益基本稳定。", "grounded": False}))
    _patch_retrieval(monkeypatch, chunks=[
        _chunk(1, "增" * 1_000), _chunk(2, "漂" * 1_000),
    ])
    _squeeze_chunk_budget(monkeypatch, 4_000)

    response = _ask(arepo, notebook)

    assert [c.label for c in response.citations] == [
        "手册-1 · §1 增益", "手册-2 · §2 增益",
    ]


# --------------------------------------------------------------------------
# ⑤ 外部证据仍在尾部
# --------------------------------------------------------------------------


def test_external_citations_stay_at_the_tail_behind_the_new_chunk_leg(
    arepo, monkeypatch
):
    from app.domain.reflect_action import ExternalEvidence

    notebook = _seed(arepo)
    _bind(arepo, _SeqLLM({"answer": "库内与库外都这样说。", "grounded": False}))
    _patch_retrieval(
        monkeypatch,
        chunks=[_chunk(1, "源极负反馈让增益稳定。")],
        external=[ExternalEvidence(
            key="ext:acme:1", plugin_id="acme", action="web_search",
            source_label="IEEE Xplore", title="外部论文-1",
            excerpt="外部摘录-1", url="https://example.org/paper/1",
            location_label="§1",
        )],
    )

    response = _ask(arepo, notebook)

    assert [c.tier for c in response.citations] == ["personal", "external"]
    assert response.citations[-1].label == "IEEE Xplore · 外部论文-1"
