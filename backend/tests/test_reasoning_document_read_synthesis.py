"""`read_document` 的合成侧接线(PR-A T5)。

覆盖的是**取样产物怎么进答案**这一段:生产接线点(`AskService._build_reasoning_
retriever` 把 `sources` 座位接上,没有它整个动作在生产上永远是关闭态)、装配位与
导读指引、花名册键反查、`[kN]` → anchor → 引用卡的三跳,以及分区计数。

动作本身的合同(闸、六条 skip、两个预算池、回喂账目)在
``test_reasoning_document_read``;取样执行体的合同在 ``test_document_overview`` /
``test_document_source_overview``。这里都不重测。

用例一律走 ``AskService.ask_reasoning`` 的真链路而不是裸 ``ReasoningRetriever``
——本任务要钉的恰恰是那条链路上的接线,裸 retriever 的用例对「ask_service 有没有
把座位接上」一无所知(变异实测:删掉 ``sources=self.overview_sources`` 一行,
``test_reasoning_document_read`` 全绿,只有本文件会红)。
"""
from __future__ import annotations

import json

import pytest

from app.models.schemas import AskRequest, NotebookCreate
from app.services import reasoning_retrieval as rr_module
from app.services.ask_service import AskService
from app.services.document_read_answer import (
    DOCUMENT_READ_GUIDANCE,
    document_read_prompt_block,
)
from tests.model_testkit import bind_chat_client
from tests.test_reasoning_enumeration_tools import (  # noqa: F401
    _ValidatingLLM,
    _enumerate_sources_action,
    repo,  # pytest fixture, resolved by name
)
# 夹具从**检索侧那份用例**导入,不在这里抄第二份(先例:
# ``test_trace_result_ids`` 从 ``test_reasoning_enumeration_tools`` 借 `repo` 与
# `_ValidatingLLM`)。两份手抄的种子数据迟早会分叉——而这两个文件断言的恰恰是
# 同一条链路的两端(检索侧的产物 / 合成侧怎么消费它),种子一分叉,两边就在测
# 两个不同的库,却都显示为绿。
from tests.test_reasoning_document_read import (
    ANSWER,
    NOW,  # noqa: F401 - 种子时间戳,与检索侧同一份
    _read_action,
    _seed_document,
)


# --------------------------------------------------------------------- 夹具


class _AnswerClient:
    """``ask_answer`` 的替身:记下合成 prompt,吐一句带 ``[kN]`` 的答案。"""

    configured = True
    model = "test"

    def __init__(self, answer="依据取样 [k7003]。"):
        self.prompts: list[str] = []
        self.answer = answer

    def chat_json(self, messages, *args, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return json.dumps({"answer": self.answer, "grounded": True})


def _ask(repo, notebook_id, reflects, *, answer_client, question="这个库的文档分别讲了什么"):
    """走生产链路的一轮 reasoning Ask,返回 (response, reflect 替身)。"""
    llm = _ValidatingLLM(reflects, plan={"sub_queries": [{"query": "取样"}]})
    bind_chat_client(repo, "reasoning_agent", llm)
    bind_chat_client(repo, "ask_answer", answer_client)
    response = repo._runtime.ask_service().ask_reasoning(
        notebook_id, AskRequest(question=question, mode="reasoning"),
        user_id=repo.current_user().id,
    )
    return response, llm


def _notebook_with_documents(repo):
    """一篇无摘要(会被读)、一篇有摘要(不会被读)。

    元素池压到 12:池是**按次数上限切份额**的(默认最多读 4 篇),所以这一次读到
    手的是 3 个元素 < 这一篇的 4 个,取样因此走**有界摘录**那条分支——合成侧要
    披露的正是这一条(「读全了」那条分支说的是另一回事)。直接把池设成 3 的话
    份额会变成 0,整个动作在预算闸上就 skip 掉了。
    """
    repo.settings.document_overview_max_elements = 12
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-empty", "无摘要文档", "", (
        "起点:本文讲的是版图取样方法。",
        "中一:实验设置。",
        "中二:参数表。",
        "末尾结论:该方法优于既有做法。",
    ))
    _seed_document(repo, notebook.id, "s-summed", "部署手册", "已存摘要", (
        "部署手册正文不应进合成。",
    ))
    return notebook


def _synthesis_step(response):
    return next(step for step in response.reasoning_trace
                if step.step_type == "synthesis")


# ------------------------------------------------------------- 号段一致性


def test_document_read_key_base_agrees_with_the_retrieval_side():
    """号段是**跨层合同**:检索侧按它算 key_offset,合成侧按它做分区计数。

    两边写成不同的数不会抛任何异常——只会让每一段原文摘录在 synthesis 计数里被
    算成一行集合清单。这里是那条合同的唯一执行者(模块级 assert 会把 ask_service
    变成 reasoning_retrieval 的 import 期依赖,那是反向依赖)。
    """
    assert (AskService._DOCUMENT_READ_KEY_BASE
            == rr_module.DOCUMENT_READ_KEY_BASE == 7000)


# ------------------------------------------------------------------ e2e


def test_sampled_document_reaches_synthesis_with_guidance_header_and_citation(repo):  # noqa: F811
    """①`enumerate sources → read_document → answer` 的合成侧终态。

    一次断言五件事,因为它们是同一条接线的五个可观测面:指引进了 prompt、header
    带的是这一篇在花名册里的那把键、取样正文(含末尾结论)与 coverage 披露都在、
    没被读的那一篇的正文**不在**、以及 `[kN]` → anchor → 引用卡这三跳接得上。
    """
    notebook = _notebook_with_documents(repo)
    client = _AnswerClient()
    response, _llm = _ask(repo, notebook.id, [
        _enumerate_sources_action(),
        _read_action("无摘要文档"),
        ANSWER,
    ], answer_client=client)

    assert client.prompts, response.answer
    prompt = client.prompts[0]
    # 导读指引的首句(整段太长,钉首句足以证明 wrapper 头真的在)。
    assert DOCUMENT_READ_GUIDANCE.split(",")[0] in prompt
    # 花名册键:header 用的是这一篇在枚举预览里占的那把键,不是新造的号。
    assert "[Supplemental original excerpts for document k5001;" in prompt
    assert "无摘要文档" in prompt
    # 取样正文与 coverage 披露行(3/4 是有界摘录,不是「读全了」)。
    assert "末尾结论:该方法优于既有做法。" in prompt
    assert "3/4" in prompt and "不代表覆盖所有章节" in prompt
    # 没被读的那一篇只以目录行出现,正文绝不出现。
    assert "部署手册正文不应进合成" not in prompt

    # [k7003] → anchor(末元素)→ 引用卡。
    anchors = [a for a in response.anchors if a.object_type == "element"]
    assert [a.object_id for a in anchors] == ["s-empty-003"]
    assert anchors[0].element_id == "s-empty-003"
    assert anchors[0].notebook_id == "", "本库来源的徽章位必须是空串"
    cited = [c for c in response.citations if c.element_id == "s-empty-003"]
    assert len(cited) == 1
    assert cited[0].source_id == "s-empty" and cited[0].notebook_id == ""

    # 结果卡仍然只有来源清单那一种:本特性**不新增** result_set 类型。
    assert [r.collection for r in response.result_sets] == ["sources"]

    # 分区计数:取样元素不被算进 chunk / 清单。
    detail = _synthesis_step(response).detail
    assert detail["included_document_reads"] == 3
    assert detail["included_chunks"] == 0


def test_reference_library_sample_keeps_its_own_notebook_badge(repo):  # noqa: F811
    """参考库来源被读到时,anchor 与引用卡的 notebook_id 是那个参考库的 id。

    A1 徽章守卫(``test_citation_notebook_id_guard``)的行为版:走到这里的是活动库
    的一轮 Ask,如果装配把来源归属打成活动库(或空),前端会把参考库的原文标成
    本库内容。
    """
    active = repo.create_notebook(NotebookCreate(name="活动库"))
    reference = repo.create_notebook(NotebookCreate(name="参考库"))
    repo.mark_notebook_base(reference.id)
    _seed_document(repo, reference.id, "s-ref", "参考库文档", "", (
        "参考库开头。", "参考库末尾。",
    ))
    repo.replace_notebook_bases(active.id, [reference.id], "user-local")
    repo.collection_catalog.invalidate()

    client = _AnswerClient("参考库依据 [k7002]。")
    response, _llm = _ask(repo, active.id, [
        _enumerate_sources_action(),
        _read_action("参考库文档"),
        ANSWER,
    ], answer_client=client)

    assert client.prompts, response.answer
    anchors = [a for a in response.anchors if a.object_type == "element"]
    assert [a.object_id for a in anchors] == ["s-ref-001"]
    assert anchors[0].notebook_id == reference.id
    cited = [c for c in response.citations if c.element_id == "s-ref-001"]
    assert len(cited) == 1 and cited[0].notebook_id == reference.id


def test_empty_product_contributes_no_block_no_evidence_no_citation(repo):  # noqa: F811
    """见证失败的空产物只属于回喂账目:合成侧一个字都不进。

    没有原文元素的那一篇会产出 ``context_block == ""`` 的 outcome。它照样在
    ``result.document_reads`` 里(模型要知道这一篇读失败了),但它既不该拼出
    header,也不该往 ``structured_map`` / 引用表里塞任何东西。
    """
    notebook = repo.create_notebook(NotebookCreate(name="资料"))
    _seed_document(repo, notebook.id, "s-bare", "空壳文档", "", ())
    client = _AnswerClient("没有可用原文。")
    response, _llm = _ask(repo, notebook.id, [
        _enumerate_sources_action(),
        _read_action("空壳文档"),
        ANSWER,
    ], answer_client=client)

    assert client.prompts, response.answer
    prompt = client.prompts[0]
    assert DOCUMENT_READ_GUIDANCE.split(",")[0] not in prompt
    assert "Supplemental original excerpts" not in prompt
    assert not [c for c in response.citations if c.source_id == "s-bare"
                and c.element_id]
    detail = _synthesis_step(response).detail
    assert "included_document_reads" not in detail, "稀疏:零取样的一轮不加这个键"


# ---------------------------------------------------- 纯函数:花名册键反查


class _Outcome:
    """``DocumentReadOutcome`` 的鸭子替身(纯函数只读这几个字段)。"""

    def __init__(self, source_id="s1", source_title="某文档",
                 context_block="k7001: \"正文\"", coverage_note="覆盖说明",
                 id_map=None, citations=()):
        self.source_id = source_id
        self.source_title = source_title
        self.context_block = context_block
        self.coverage_note = coverage_note
        self.id_map = {"k7001": {"object_id": "e1"}} if id_map is None else id_map
        self.citations = list(citations)


def test_roster_lookup_failure_degrades_to_a_title_only_header():
    """花名册键反查不到时 header 只带标题,**绝不**编一个 kN 出来。

    行数预算把某一行挤出枚举预览、或整块枚举被丢掉时都会走到这里。编号是反向
    绑定的键:造一个 id_map 里不存在的号,等于教模型引一个解析不回来的引用。
    """
    preview = document_read_prompt_block([_Outcome()], roster_map={})

    assert "the document named here" in preview.text
    assert "k5001" not in preview.text and "for document k" not in preview.text
    assert "某文档" in preview.text and "覆盖说明" in preview.text

    tethered = document_read_prompt_block([_Outcome()], roster_map={
        "k5001": {"object_type": "source", "object_id": "s1"},
    })
    assert "[Supplemental original excerpts for document k5001;" in tethered.text


def test_roster_lookup_ignores_rows_of_other_types_and_other_documents():
    """反查同时按 ``object_type == "source"`` 与 ``object_id`` 两个条件。

    只按 object_id 匹配的话,一个恰好同 id 的 element 行会把别人的键借给这一篇。
    """
    preview = document_read_prompt_block([_Outcome()], roster_map={
        "k5001": {"object_type": "element", "object_id": "s1"},
        "k5002": {"object_type": "source", "object_id": "s-other"},
    })
    assert "the document named here" in preview.text


def test_empty_reads_render_nothing_at_all():
    assert document_read_prompt_block([], roster_map={}).text == ""
    assert document_read_prompt_block(
        [_Outcome(context_block="")], roster_map={}) == document_read_prompt_block(
        [], roster_map={})


# ------------------------------------------------------ 取样块的子预算与披露


def _citation(element_id="e1"):
    from app.models.ask import Citation

    return Citation(label="某文档", source_id="s1", element_id=element_id,
                    location_label="第1节", quoted_span="正文", tier="personal",
                    notebook_id="")


def _sampled_outcome(body_chars=400):
    return _Outcome(
        context_block='k7001: "' + "正文" * (body_chars // 2) + '"',
        id_map={"k7001": {"object_id": "e1", "object_type": "element"}},
        citations=[_citation()],
    )


def test_the_roster_preview_reserves_space_for_the_sampled_block():
    """codex #724 R2:80 篇的花名册预览能把共享的那一半预算(standard 档 15000)
    吃到只剩几十字,随后拼上来的 2500 字取样块整块被挡——读成功了却白读。

    修法是先按取样块的真实渲染长度预留、再给枚举预览分预算,而且预留要从
    **半预算上限**里扣(整份预算减两千仍远大于一半,夹到一半后花名册照样拿满)。
    这里钉住两件事:预留 = 渲染长度 + 拼接符;扣完之后「花名册预算 + 取样块」
    仍装得进一半——正是 `_assemble_document_read_block` 那道整块不装的判据。
    """
    from app.services.collection_enumeration_answer import enumeration_sub_budget
    from app.services.document_read_answer import document_read_block_reserve

    assert document_read_block_reserve([]) == 0
    outcome = _sampled_outcome(body_chars=2400)
    reserve = document_read_block_reserve([outcome])
    rendered = document_read_prompt_block([outcome], roster_map={})
    assert reserve == len(rendered.text) + 2

    chunk_context_chars = 30000
    roster_budget = max(0, enumeration_sub_budget(
        chunk_context_chars=chunk_context_chars, structured_block_len=0,
    ) - reserve)
    assert roster_budget < chunk_context_chars // 2
    # 花名册按预算拿满、取样块再拼上去,仍不超过一半——不会被整块挡下。
    assert roster_budget + len(rendered.text) + 2 <= chunk_context_chars // 2


def test_the_sampled_block_is_dropped_whole_when_it_would_eat_half_the_budget(repo):  # noqa: F811
    """④ structured 段(枚举预览 + 取样块)不得超过 `chunk_context_chars` 的一半。

    两块同住 `structured_block`,而这个参数整体与 chunks / elements 争同一份预算。
    枚举那一侧已经把自己夹在一半以内;取样块拼在它后面,不夹的话两块加起来能把
    另一半问题的证据预算整个挤空——而这个动作恰恰常与 `search_chunks` 同轮发生。

    夹法是**整块不装**而不是截断:取样块的内部结构(包头 + coverage 披露行 + 带
    `[kN]` 的正文)在字符级截断下会碎成「有正文但披露行被切掉」,那正是「模型以为
    自己读全了」的那条路。
    """
    service = repo._runtime.ask_service()
    structured_map = {"k5001": {"object_type": "source", "object_id": "s1"}}
    citations: dict = {}

    block, dropped = service._assemble_document_read_block(
        [_sampled_outcome()], "", structured_map, citations,
        _AnswerClient(), 100)

    assert dropped is True
    assert block == ""
    # 反向绑定与引用卡**一起**不发生:留下「块没进去但 k7001 在 map 里」这种
    # 半状态,就等于给模型一个它看不到证据的引用键。
    assert not [key for key in structured_map if key.startswith("k7")]
    assert citations == {}


def test_a_block_inside_the_sub_budget_binds_evidence_and_citations(repo):  # noqa: F811
    """④ 装得下时三件事一起发生:块进 prompt、`[kN]` 进反向绑定、Citation 进引用表。"""
    service = repo._runtime.ask_service()
    structured_map = {"k5001": {"object_type": "source", "object_id": "s1"}}
    citations: dict = {}

    block, dropped = service._assemble_document_read_block(
        [_sampled_outcome()], "", structured_map, citations,
        _AnswerClient(), 100_000)

    assert dropped is False
    assert "k7001" in block and DOCUMENT_READ_GUIDANCE.split(",")[0] in block
    assert structured_map["k7001"]["object_id"] == "e1"
    assert list(citations) == ["e1"]


def test_a_rendering_failure_leaves_no_half_state_and_is_logged(repo, monkeypatch):  # noqa: F811
    """④ 渲染中途抛错时,两个字典一个字都没被写过(它们是调用方的,撤不回来)。

    所以三份产物先各自算好,两次 `update` 放在渲染**全部成功之后**一起做。同时
    按 `_add_sheet_prompt` 的先例记一条 warning:静默吞掉异常会让「模型明明读了
    却引不到」这类故障在生产上完全不可观测。
    """
    import app.services.document_read_answer as answer_module

    service = repo._runtime.ask_service()
    structured_map = {"k5001": {"object_type": "source", "object_id": "s1"}}
    citations: dict = {}
    monkeypatch.setattr(
        answer_module, "document_read_prompt_block",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    warnings: list = []
    monkeypatch.setattr(service.event_log.logger, "warning",
                        lambda *a, **k: warnings.append(a))

    block, dropped = service._assemble_document_read_block(
        [_sampled_outcome()], "seed", structured_map, citations,
        _AnswerClient(), 100_000)

    assert block == "seed" and dropped is False
    assert structured_map == {"k5001": {"object_type": "source", "object_id": "s1"}}
    assert citations == {}
    assert warnings and "document read prompt rendering failed" in warnings[0][0]


def test_the_dropped_marker_is_sparse_in_the_synthesis_detail():
    """④ `document_read_block_dropped` 是**稀疏键**:没被挤掉的一轮里它缺席。

    恒为 False 地写进去会改掉每一条既有答案的持久化 payload(golden oracle 会红),
    而「没有被挤掉」本来就是缺席该表达的事实——与两个外部证据键同一条纪律。
    """
    from app.services.ask_service import _synthesis_step_detail

    base = dict(
        citations=0, anchors=[], evidence_level="overview", counts={},
        enumerated_collections=1, enumeration_block_dropped=False,
        outline_planned=False, sectioned=None, outline_fallback=False,
        outline_skipped=(),
    )
    quiet = _synthesis_step_detail(document_read_block_dropped=False, **base)
    loud = _synthesis_step_detail(document_read_block_dropped=True, **base)

    assert "document_read_block_dropped" not in quiet
    assert loud["document_read_block_dropped"] is True
    # 枚举那一份是**非**稀疏的既有键,不能被这次改动带成稀疏。
    assert quiet["enumeration_block_dropped"] is False


@pytest.mark.parametrize("note", ["", "覆盖说明"])
def test_block_shape_is_header_then_coverage_then_samples(note):
    """块内顺序是 header → coverage 披露 → 取样正文,且 coverage 是**每篇一行**。

    单篇的 coverage 不能被汇总到整段末尾:一篇可能被读全、下一篇只给了 3/40,
    一句话在末尾的话模型会把其中一个套到另一篇头上。
    """
    preview = document_read_prompt_block(
        [_Outcome(coverage_note=note, context_block="k7001: \"正文\"")],
        roster_map={"k5001": {"object_type": "source", "object_id": "s1"}},
    )
    body = preview.text[len(DOCUMENT_READ_GUIDANCE):]
    expected = ("\n\n[Supplemental original excerpts for document k5001; "
                "bounded sampling, not a full reading] \"某文档\"\n"
                + (f"{note}\n" if note else "") + "k7001: \"正文\"")
    assert body == expected
