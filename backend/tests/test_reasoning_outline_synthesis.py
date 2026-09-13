"""按节合成(PR-3 O2,设计文档 §3.1)。

覆盖:切片装配(含被 top_n 截断的绑定对象仍能进本节)、各节 `[k]` 号段互不相交、
锚点按节解析后合并、拼接的标题层级与顺序、空节跳过、触发条件(≥2 个非空节 + 穷尽
档 + 总开关)、任一节失败的整体回退,以及 synthesis 轨迹的新字段。

大纲**怎么建**(动作/校验/账目回喂/门控)是 O1 的地盘,在 test_reasoning_outline.py。
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.ask_service import AskService
from app.services.embedding import FakeEmbedder
from app.services.outline_synthesis import (
    OUTLINE_SECTION_KEY_STRIDE,
    outline_answer_text,
    plan_outline_sections,
)
from app.services.reasoning_retrieval import (
    OUTLINE_ACTION,
    OutlineSection,
    ReasoningResult,
    ReasoningRetriever,
    _OUTLINE_MAX_EVIDENCE,
    outline_truncated_kg_evidence,
)
from app.services.retrieval import (
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-07-30T00:00:00+08:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _notebook(repo, *, kg=()):
    """建一本能进 reasoning 循环的库。

    一条来源 + 一个元素是必需的:没有图谱、也没有任何可枚举集合的库会在检索之前
    就被「先建知识图谱」早退挡下,压根走不到合成。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    if kg:
        repo.store_kg(notebook.id, None, [
            {"local_id": f"C{index}", "object_type": "claim",
             "payload": {"name": name, "section_path": str(index)},
             "evidence": []}
            for index, name in enumerate(kg, start=1)
        ], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", notebook.id, "论文一", "pdf", "extracted", "extracted",
             NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-000", "s1", "formula", "p1", "版图设计要点 公式", "{}", NOW),
        )
    repo.collection_catalog.invalidate()
    return notebook


def _section(section_id, title, *keys, parent=""):
    return OutlineSection(id=section_id, title=title, parent=parent,
                          evidence_keys=list(keys))


def _element(element_id, text="版图设计要点", source_id="s1"):
    return RetrievedElement(
        element_id=element_id, source_id=source_id, source_title="论文一",
        location_label="p1", element_type="formula", text=text, score=0.9,
    )


def _chunk(chunk_id, text="一段原文", source_id="s1"):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id, source_title="论文一",
        section_path="1", text=text, relevance=0.8,
    )


def _knowledge(object_id, name="概念", relevance=0.9):
    return RetrievedKnowledge(
        object_id=object_id, object_type="concept", payload={"name": name},
        relevance=relevance, score=relevance,
    )


# --------------------------------------------------------------- 命名空间偏移

def test_the_stride_clears_every_existing_key_base():
    """节偏移必须跨过合成上下文里所有既有分区基址。

    这不是"取个大点的数"就完了:任何一个基址长到步长之上,第 i 节的那一段就会
    落进第 i+1 节的号段里,而合并锚点时**不会报错**——只会把一处引用悄悄指到
    另一节的证据上。
    """
    bases = (
        AskService._MIX_KG_KEY_BASE,
        AskService._MEMORY_KEY_BASE,
        AskService._ELEMENT_KEY_BASE,
        AskService._COLLECTION_KEY_BASE,
        2000,   # follow_chain 的推导链段
    )
    assert max(bases) < OUTLINE_SECTION_KEY_STRIDE
    # 单节内部的号段上限 = 最大基址 + 单节证据上限。上限从 O1 的常量 import,不写
    # 字面量:那个数改大时,本断言必须跟着变紧,而不是继续对着一个过期的 8 报绿。
    assert max(bases) + _OUTLINE_MAX_EVIDENCE < OUTLINE_SECTION_KEY_STRIDE


def test_each_planned_section_gets_a_disjoint_offset():
    slices, skipped = plan_outline_sections(
        [_section("a", "第一节", "e1"),
         _section("b", "第二节", "c1"),
         _section("c", "第三节", "k1")],
        kg_by_id={"k1": _knowledge("k1")},
        element_by_id={"e1": _element("e1")},
        chunk_by_id={"c1": _chunk("c1")},
    )
    assert skipped == []
    assert [item.key_offset for item in slices] == [
        0, OUTLINE_SECTION_KEY_STRIDE, 2 * OUTLINE_SECTION_KEY_STRIDE
    ]
    assert len({item.key_offset for item in slices}) == len(slices)


def test_skipped_sections_do_not_consume_an_offset():
    """空节不占号段:偏移按**实际合成**的序号算,否则 detail 里的节数与号段会分叉。"""
    slices, skipped = plan_outline_sections(
        [_section("a", "有证据的", "e1"),
         _section("b", "空的"),
         _section("c", "也有证据的", "e2")],
        kg_by_id={},
        element_by_id={"e1": _element("e1"), "e2": _element("e2")},
        chunk_by_id={},
    )
    assert skipped == ["空的"]
    assert [item.index for item in slices] == [0, 1]
    assert [item.key_offset for item in slices] == [0, OUTLINE_SECTION_KEY_STRIDE]


# ------------------------------------------------------------------- 切片装配

def test_each_key_goes_to_its_own_pool():
    slices, _ = plan_outline_sections(
        [_section("a", "混合一节", "k1", "e1", "c1")],
        kg_by_id={"k1": _knowledge("k1")},
        element_by_id={"e1": _element("e1")},
        chunk_by_id={"c1": _chunk("c1")},
    )
    item = slices[0]
    assert [hit.object_id for hit in item.hits] == ["k1"]
    assert [el.element_id for el in item.elements] == ["e1"]
    assert [chunk.chunk_id for chunk in item.chunks] == ["c1"]
    assert item.evidence_count == 3


def test_a_section_whose_keys_resolve_to_nothing_is_skipped():
    """键在池子里查不到 = 这一节实际上是空的,与 evidence_keys 为空等价。"""
    slices, skipped = plan_outline_sections(
        [_section("a", "查不到的", "gone"), _section("b", "查得到的", "e1")],
        kg_by_id={}, element_by_id={"e1": _element("e1")}, chunk_by_id={},
    )
    assert skipped == ["查不到的"]
    assert [item.section.id for item in slices] == ["b"]


def test_a_section_keeps_the_order_the_model_bound():
    slices, _ = plan_outline_sections(
        [_section("a", "一节", "e2", "e1")],
        kg_by_id={},
        element_by_id={"e1": _element("e1"), "e2": _element("e2")},
        chunk_by_id={},
    )
    assert [el.element_id for el in slices[0].elements] == ["e2", "e1"]


# --------------------------------------------------------------------- 拼接

def test_headings_follow_the_outline_hierarchy_and_order():
    slices, _ = plan_outline_sections(
        [_section("a", "顶层一", "e1"),
         _section("b", "子节", "e2", parent="a"),
         _section("c", "顶层二", "e3")],
        kg_by_id={},
        element_by_id={f"e{n}": _element(f"e{n}") for n in (1, 2, 3)},
        chunk_by_id={},
    )
    text = outline_answer_text([(item, f"正文{item.index}") for item in slices])
    assert text == (
        "## 顶层一\n\n正文0\n\n"
        "### 子节\n\n正文1\n\n"
        "## 顶层二\n\n正文2"
    )
    assert text.index("## 顶层一") < text.index("### 子节") < text.index("## 顶层二")


def test_a_child_submitted_before_its_parent_is_rendered_after_it():
    """提交序 ≠ 文档序。

    `parse_outline_sections` **明确允许**先写子节再写父节(它的两层裁剪刻意等收齐
    所有节之后才做,正是为了不把合法的前向引用误判成非法)。照提交序渲染就会得到
    `### 子节` 排在 `## 父节` 之前 —— Markdown 里那是个层级倒挂的文档结构,前端
    h2/h3 会照它渲染。
    """
    slices, _ = plan_outline_sections(
        [_section("b", "子节", "e2", parent="a"),      # 子在前
         _section("a", "父节", "e1"),
         _section("c", "另一个顶层", "e3")],
        kg_by_id={},
        element_by_id={f"e{n}": _element(f"e{n}") for n in (1, 2, 3)},
        chunk_by_id={},
    )
    # 序号与号段偏移也按文档序分配:节级 prompt 的「(2 of 3)」、进度步的
    # 「第 2/共 3 节」与答案里的位置说的必须是同一件事。
    assert [item.section.title for item in slices] == ["父节", "子节", "另一个顶层"]
    assert [item.index for item in slices] == [0, 1, 2]
    assert [item.key_offset for item in slices] == [
        0, OUTLINE_SECTION_KEY_STRIDE, 2 * OUTLINE_SECTION_KEY_STRIDE
    ]
    text = outline_answer_text([(item, f"正文{item.index}") for item in slices])
    assert text == (
        "## 父节\n\n正文0\n\n"
        "### 子节\n\n正文1\n\n"
        "## 另一个顶层\n\n正文2"
    )


def test_a_child_whose_parent_was_skipped_is_promoted_to_a_top_level_heading():
    """父节是空节、被跳过时,子节晋升为 `##`,不留孤儿 `###`。

    空节是合法终态(O1 合同),它不进答案;而 `section.parent` 仍指着那个不会出现的
    id。照原值渲染就是一个没有父标题的 `### 子节` —— 读者会以为上面漏了一段。
    """
    slices, skipped = plan_outline_sections(
        [_section("a", "父节没有证据"),                  # 空节 → 被跳过
         _section("b", "实子节", "e2", parent="a"),
         _section("c", "另一个顶层", "e3")],
        kg_by_id={},
        element_by_id={f"e{n}": _element(f"e{n}") for n in (2, 3)},
        chunk_by_id={},
    )
    assert skipped == ["父节没有证据"]
    assert [item.section.title for item in slices] == ["实子节", "另一个顶层"]
    assert [item.parent_id for item in slices] == ["", ""]
    text = outline_answer_text([(item, f"正文{item.index}") for item in slices])
    assert "###" not in text
    assert text == "## 实子节\n\n正文0\n\n## 另一个顶层\n\n正文1"


def test_document_order_keeps_siblings_and_roots_in_submission_order():
    """重排只按父子分组,同层之间**不动**提交序 —— 那是模型表达的叙事顺序。"""
    slices, _ = plan_outline_sections(
        [_section("r2", "后写的顶层", "e1"),
         _section("c2", "第二个子节", "e2", parent="r2"),
         _section("r1", "先写的顶层", "e3"),
         _section("c1", "第一个子节", "e4", parent="r2")],
        kg_by_id={},
        element_by_id={f"e{n}": _element(f"e{n}") for n in (1, 2, 3, 4)},
        chunk_by_id={},
    )
    assert [item.section.title for item in slices] == [
        "后写的顶层", "第二个子节", "第一个子节", "先写的顶层"
    ]
    assert [item.parent_id for item in slices] == ["", "r2", "r2", ""]


# ------------------------------------------- 被 top_n 截断的绑定对象仍要进切片

def test_bound_objects_cut_by_the_top_n_selection_travel_out():
    collected = {f"ko-{n}": _knowledge(f"ko-{n}") for n in range(4)}
    top_hits = [collected["ko-0"], collected["ko-1"]]
    extra = outline_truncated_kg_evidence(
        [_section("a", "一节", "ko-0", "ko-3"),
         _section("b", "二节", "ko-2", "ko-3")],
        collected, top_hits,
    )
    # 已在 top_hits 里的不重复带出;重复绑定只带一次;顺序按大纲。
    assert [hit.object_id for hit in extra] == ["ko-3", "ko-2"]


def test_unknown_and_empty_outlines_produce_nothing_extra():
    collected = {"ko-0": _knowledge("ko-0")}
    assert outline_truncated_kg_evidence([], collected, []) == []
    assert outline_truncated_kg_evidence(
        [_section("a", "一节", "not-a-candidate")], collected, []) == []


def test_the_carried_relevance_comes_from_the_same_rerank_as_the_selection():
    """首收分不能当相关度带出去。

    `collected[oid]` 存的是**第一个**捞到它的产出方给的分,而 `top_hits` 每条都经过
    对整个问题的重排。一个首收 0.95、重排 0.12 的对象带着 0.95 进证据池,它一条就能
    把整篇抬到 grounded —— 而它恰恰是被截断掉的那种"其实不相关"。
    """
    collected = {"ko-hot": _knowledge("ko-hot", relevance=0.95)}
    rescored = {"ko-hot": _knowledge("ko-hot", relevance=0.12)}
    sections = [_section("a", "一节", "ko-hot")]

    carried = outline_truncated_kg_evidence(
        sections, collected, [], rescored=rescored)
    assert [hit.relevance for hit in carried] == [0.12]
    # 重排 map 里没有的对象与 top_hits 对自己成员的处理一致:沿用原对象。
    assert outline_truncated_kg_evidence(
        sections, collected, [], rescored={})[0].relevance == 0.95


def test_the_quota_path_clamps_the_carried_relevance_to_the_selection_floor():
    """配额融合没有全局重排 map,只能夹一刀:补集是被选集挤出去的那一批,
    不可能比选集里最差的一条更相关。"""
    collected = {"ko-hot": _knowledge("ko-hot", relevance=0.95),
                 "ko-cold": _knowledge("ko-cold", relevance=0.05)}
    sections = [_section("a", "一节", "ko-hot", "ko-cold")]

    carried = outline_truncated_kg_evidence(
        sections, collected, [], relevance_ceiling=0.2)
    assert [hit.relevance for hit in carried] == [0.2, 0.05]
    # 选集为空 ⇒ 天花板 0.0 ⇒ 补集一条都抬不动(「没有任何排名证据」该有的结果)。
    zeroed = outline_truncated_kg_evidence(
        sections, collected, [], relevance_ceiling=0.0)
    assert [hit.relevance for hit in zeroed] == [0.0, 0.0]
    # 夹取不就地改 collected —— 那份池子后面还要被 top_hits 用。
    assert collected["ko-hot"].relevance == 0.95


def _run_with_one_outline_round(
    repo, monkeypatch, *, rescore=None, quota_selection=None
):
    """跑一轮:两个候选、最终选集只留 1 条(必有截断)、大纲把两个都绑进同一节。

    ``rescore`` 非空时替换收尾那次全局重排的结果(它走 ``retrieve_scored``,与
    候选检索走的 ``federated_retrieve`` 是两条路,所以只影响重排、不影响 collected)。

    ``quota_selection`` 非空时改走配额融合分支(计划给两条子查询),并把融合结果
    的相关度按给定值压低 —— 天花板夹取要可判定,就必须让"选集的最低分"确定地
    低于候选的原始分。
    """
    import app.services.reasoning_retrieval as rr

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点甲", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "claim",
         "payload": {"name": "版图设计要点乙", "section_path": "2"}, "evidence": []},
    ], [])
    # 最终选集只留 1 条,于是至少一个候选注定被截断。
    monkeypatch.setattr(rr, "effective_top_n", lambda *a, **k: 1)

    seen_ids: list[str] = []

    class _LLM:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            if "sub_queries" in schema_hint:
                queries = ["版图设计要点"]
                if quota_selection is not None:
                    # 配额融合的触发条件是 used_queries >= 2。
                    queries.append("版图设计要点乙")
                return json.dumps(
                    {"sub_queries": [{"query": q} for q in queries]})
            body = content.split("Candidates so far:", 1)[-1]
            ids = re.findall(r"\(id=([^)]+)\)", body)
            if ids and not seen_ids:
                seen_ids.extend(ids)
                return json.dumps({
                    "next_action": OUTLINE_ACTION, "reason": "整理",
                    "outline": {"sections": [
                        {"id": "s1", "title": "全部要点",
                         "evidence": seen_ids[:2]},
                    ]},
                })
            return json.dumps({"next_action": "answer", "sufficient": True})

    bind_chat_client(repo, "reasoning_agent", _LLM())
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    if rescore is not None:
        real = retriever.retrieval.retrieve_scored
        monkeypatch.setattr(
            retriever.retrieval, "retrieve_scored",
            lambda nb, query, *a, **k: [
                replace(hit, relevance=rescore) for hit in real(nb, query, *a, **k)
            ],
        )
    if quota_selection is not None:
        real_quota = retriever._quota_rerank
        monkeypatch.setattr(
            retriever, "_quota_rerank",
            lambda *a, **k: (
                [replace(hit, relevance=quota_selection)
                 for hit in real_quota(*a, **k)[0]],
                real_quota(*a, **k)[1],
            ),
        )
    result = retriever.run(
        notebook.id, "版图设计要点", "", limits=ask_retrieval_limits("exhaustive"),
    )
    assert len(seen_ids) >= 2, "需要至少两个候选才能演示截断"
    return result


def test_the_run_carries_the_truncated_bindings_out(repo, monkeypatch):
    """真跑一轮:大纲绑上一个被 top_n 挤出选集的对象,它仍要出现在结果里。

    没有这一份,按节合成给那一节装配出来的上下文会凭空少一条证据 —— 而且悄无声息。
    """
    result = _run_with_one_outline_round(repo, monkeypatch)
    assert len(result.top_hits) == 1
    bound = set(result.outline[0].evidence_keys)
    carried = {hit.object_id for hit in result.top_hits}
    carried |= {hit.object_id for hit in result.outline_evidence}
    assert bound <= carried
    assert result.outline_evidence, "被截断的绑定对象必须单独带出"


def test_the_run_scores_the_carried_bindings_with_the_final_rerank(
    repo, monkeypatch
):
    """接线验证:带出值走的是收尾那次全局重排,不是首收分。"""
    result = _run_with_one_outline_round(repo, monkeypatch, rescore=0.11)
    assert result.outline_evidence
    assert [hit.relevance for hit in result.outline_evidence] == [0.11]
    # 选集自己也是同一个 map 出来的 —— 补集与选集同口径,这正是本条要钉的。
    assert [hit.relevance for hit in result.top_hits] == [0.11]


def test_the_quota_run_clamps_the_carried_bindings_to_the_selection_floor(
    repo, monkeypatch
):
    """配额融合分支的接线:没有全局重排 map,补集必须被夹到选集最低分。"""
    result = _run_with_one_outline_round(
        repo, monkeypatch, quota_selection=0.03)
    assert result.top_hits and result.outline_evidence
    floor = min(hit.relevance for hit in result.top_hits)
    assert floor == 0.03
    assert [hit.relevance for hit in result.outline_evidence] == [0.03]


# ------------------------------------------------- _answer_reasoning 的节模式


class _CaptureAnswerLLM:
    """把每次 ask_answer 的 prompt 记下来,并引用它在自己那一份上下文里看到的第一个 id。

    引用的 key 从 prompt 里读出来,而不是写死:「每节只看得见自己的号段」这条合同
    只有让脚本"模型"真的去读才测得到。
    """

    configured = True
    model = "stub"

    def __init__(self, answers=None):
        self.prompts: list[str] = []
        self._answers = list(answers or [])

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "版图设计要点"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": ["精炼结果"]})
        self.prompts.append(content)
        if self._answers:
            return json.dumps(self._answers.pop(0))
        keys = re.findall(r"(?m)^(k\d+):", content)
        cite = f"[{keys[0]}]" if keys else ""
        return json.dumps({"answer": f"本节结论{cite}", "grounded": True})


def _ask_service(repo, llm):
    for service in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, service, llm)
    return repo._runtime.ask_service()


def test_the_offset_moves_every_namespace_in_one_assembly(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    llm = _CaptureAnswerLLM()
    service = _ask_service(repo, llm)
    offset = OUTLINE_SECTION_KEY_STRIDE
    _answer, _grounded, anchors, counts = service._answer_reasoning(
        notebook.id, "问题", [], [_element("e1")],
        chunks=[_chunk("c1")], sectioned=True, key_offset=offset,
        section_title="第二节", section_index=2, section_total=2,
    )
    prompt = llm.prompts[0]
    assert f"k{offset + 1}:" in prompt                      # 原文段
    assert f"k{offset + AskService._ELEMENT_KEY_BASE + 1}:" in prompt
    assert "\nk1:" not in prompt and not prompt.startswith("k1:")
    # 计数按去掉偏移后的号段分区:不减掉的话 chunk 会被记成"集合清单"。
    assert counts["included_chunks"] == 1
    assert counts["included_elements"] == 1
    assert counts["included_collections"] == 0
    assert [anchor.key for anchor in anchors] == [f"k{offset + 1}"]


def test_section_mode_skips_evidence_refinement(repo):
    """精炼是每次装配一次模型调用;节模式下它会把 k 次合成变成 2k 次。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    llm = _CaptureAnswerLLM()
    service = _ask_service(repo, llm)
    service._answer_reasoning(
        notebook.id, "问题", [], [_element("e1")], sectioned=True,
        section_title="第一节", section_index=1, section_total=2,
    )
    assert "精炼结果" not in llm.prompts[0]
    service._answer_reasoning(notebook.id, "问题", [], [_element("e1")])
    assert "精炼结果" in llm.prompts[1], "单次合成仍然精炼"


def test_the_section_directive_only_appears_in_section_mode(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    llm = _CaptureAnswerLLM()
    service = _ask_service(repo, llm)
    service._answer_reasoning(notebook.id, "问题", [], [_element("e1")])
    service._answer_reasoning(
        notebook.id, "问题", [], [_element("e1")], sectioned=True,
        section_title="本节标题", section_index=1, section_total=3,
    )
    assert "ONE section" not in llm.prompts[0]
    assert "ONE section" in llm.prompts[1]
    assert "本节标题" in llm.prompts[1]
    assert "(1 of 3)" in llm.prompts[1]


def test_the_mode_switch_is_the_flag_not_the_title(repo):
    """节模式判定必须是显式布尔:标题是内容,不是开关。

    靠标题真值判的话,一个绕过 `parse_outline_sections`(它保证标题非空)的构造会落进
    「号段偏移生效了、节级指令却没发」的混合态——prompt 看着正常,模型却按整篇答案
    的口径写每一节,最难发现的一种。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    llm = _CaptureAnswerLLM()
    service = _ask_service(repo, llm)
    # 只给标题、不开 flag:仍然是单次合成(发精炼、不发节级指令)。
    service._answer_reasoning(
        notebook.id, "问题", [], [_element("e1")],
        section_title="看起来像一节", section_index=1, section_total=2,
    )
    assert "ONE section" not in llm.prompts[0]
    assert "精炼结果" in llm.prompts[0]
    # 只开 flag、标题缺席(防御性形状):节级指令照发,精炼照跳。
    service._answer_reasoning(
        notebook.id, "问题", [], [_element("e1")], sectioned=True,
        section_index=1, section_total=2,
    )
    assert "ONE section" in llm.prompts[1]
    assert "精炼结果" not in llm.prompts[1]


def test_the_new_prompt_parameters_are_keyword_only():
    """三个新形参必须是 keyword-only:既有调用方的形状是三个位置参数,把模式开关
    也做成位置参数,只会让「第四个位置传了什么」变成数逗号的问题。"""
    import inspect

    from app.services.prompts import answer_prompt

    parameters = inspect.signature(answer_prompt).parameters
    positional = [
        name for name, spec in parameters.items()
        if spec.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert positional == ["question", "context_block", "history_block"]
    for name in ("sectioned", "section_title", "section_index", "section_total"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name


# ----------------------------------------------------------------------- e2e


def _stub_run(monkeypatch, result: ReasoningResult):
    """把检索整段替换掉,只留合成:本文件测的是产出侧。"""
    monkeypatch.setattr(ReasoningRetriever, "run",
                        lambda self, *a, **k: result, raising=True)


def _reasoning_result(**overrides) -> ReasoningResult:
    elements = [_element("e1", "甲要点"), _element("e2", "乙要点")]
    outline = [
        _section("s1", "第一节", "e1"),
        _section("s2", "第二节", "e2"),
    ]
    payload = {"elements": elements, "outline": outline}
    payload.update(overrides)
    return ReasoningResult(**payload)


def _ask(repo, notebook, llm, *, effort="exhaustive"):
    service = _ask_service(repo, llm)
    return service.ask_reasoning(
        notebook.id,
        AskRequest(question="综述一下版图设计要点", mode="reasoning",
                   retrieval_effort=effort),
        user_id=repo.current_user().id,
    )


def _synthesis_steps(response):
    return [step for step in (response.reasoning_trace or [])
            if step.step_type == "synthesis"]


def _section_progress_steps(response):
    """分节进度步 = 带 section_index 的那些(收尾那条总合成步不带)。"""
    return [step for step in _synthesis_steps(response)
            if "section_index" in (step.detail or {})]


def _synthesis_detail(response):
    steps = _synthesis_steps(response)
    assert steps, "合成步必须在轨迹里"
    return steps[-1].detail


def test_two_bound_sections_are_synthesised_separately_and_concatenated(
    repo, monkeypatch
):
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 2, "两节 = 两次合成调用"
    first, second = llm.prompts
    # 每节只看得见自家条目,连对方的正文都不该出现。
    assert "甲要点" in first and "乙要点" not in first
    assert "乙要点" in second and "甲要点" not in second
    assert "第一节" in first and "(1 of 2)" in first
    assert "第二节" in second and "(2 of 2)" in second

    assert response.answer.startswith("## 第一节")
    assert "## 第二节" in response.answer
    # 两节各自绑上自己的锚点,号段互不相交。
    keys = {anchor.key for anchor in response.anchors}
    assert keys == {
        f"k{AskService._ELEMENT_KEY_BASE + 1}",
        f"k{OUTLINE_SECTION_KEY_STRIDE + AskService._ELEMENT_KEY_BASE + 1}",
    }
    assert {anchor.object_id for anchor in response.anchors} == {"e1", "e2"}

    detail = _synthesis_detail(response)
    assert detail["outline_sections"] == 2
    assert detail["outline_fallback"] is False
    assert detail["outline_skipped"] == []
    assert detail["included_elements"] == 2, "各节装配计数求和"
    assert [item["grounded"] for item in detail["section_grounded"]] == [True, True]
    assert detail["ungrounded_sections"] == []
    assert response.evidence_level == "grounded"


def test_a_child_first_outline_reaches_the_answer_in_document_order(
    repo, monkeypatch
):
    """端到端:提交序 子→父,答案里必须 父→子,且三处序号说的是同一件事。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(outline=[
        _section("s2", "子节", "e2", parent="s1"),
        _section("s1", "父节", "e1"),
    ]))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert response.answer.startswith("## 父节")
    assert "### 子节" in response.answer
    assert response.answer.index("## 父节") < response.answer.index("### 子节")
    # 节级 prompt 的位置、进度步的序号与答案里的位置一致。
    assert "父节" in llm.prompts[0] and "(1 of 2)" in llm.prompts[0]
    assert "子节" in llm.prompts[1] and "(2 of 2)" in llm.prompts[1]
    assert [step.detail["section_title"]
            for step in _section_progress_steps(response)] == ["父节", "子节"]


def test_empty_sections_are_skipped_and_named_in_the_trace(repo, monkeypatch):
    outline = [
        _section("s1", "第一节", "e1"),
        _section("s0", "还没找到证据的一节"),
        _section("s2", "第二节", "e2"),
    ]
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(outline=outline))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 2
    assert "还没找到证据的一节" not in response.answer
    detail = _synthesis_detail(response)
    assert detail["outline_skipped"] == ["还没找到证据的一节"]
    assert detail["outline_sections"] == 2


def test_a_failed_section_falls_back_to_one_shot(repo, monkeypatch):
    """第二节两次都吐不出内容 → 整体回退,绝不交付半篇。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文", "grounded": True},   # 第 1 节成功
        {"answer": "", "grounded": False},            # 第 2 节:空 content
        {"answer": "", "grounded": False},            # 重试仍空 → 判失败
        {"answer": "整篇回退答案", "grounded": True},  # 单次合成
    ])
    response = _ask(repo, notebook, llm)

    assert response.answer == "整篇回退答案"
    assert "## 第一节" not in response.answer, "半节残留就是这条回退要防的东西"
    detail = _synthesis_detail(response)
    assert detail["outline_fallback"] is True
    assert detail["outline_sections"] == 0
    # 回退路径把 Memory/推导链/集合地图那些不可绑定的块也带回来了 —— 它走的是
    # 原来那条单次合成,精炼照做。
    assert "精炼结果" in llm.prompts[-1]
    # 回退**成功**后不留假报警:那次故障已被同一轮吸收,用户拿到了完整答案,
    # 再挂一条红色横幅是在报一个并没有影响到结果的错误。可观测性由
    # outline_fallback 与 events.jsonl 承担。
    assert response.model_errors == []
    # 进度步只报**真的写完了**的那些:第 2 节失败,它不该有一条「已写完」。
    # (进度若在动手前发,这里会看到 [1, 2] —— 那是在报一件没发生的事。)
    assert [step.detail["section_index"]
            for step in _section_progress_steps(response)] == [1]


def test_a_section_that_recovers_on_retry_leaves_no_alarm(repo, monkeypatch):
    """节内重试成功 → 分节合成整体成功,响应里一条假报警都不留(设计文档 §3.1.1)。

    这条与下面那条回退用例是同一条规则的两端:节**自己**恢复了,由
    `_answer_with_retry` 就地摘掉;节彻底失败、由回退救回来,则由回退成功那一段
    区间删除覆盖。两条路径都不该把一次已经被吸收的故障挂到用户面上。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文", "grounded": True},
        {"answer": "", "grounded": False},            # 第 2 节:空 content
        {"answer": "第二节正文", "grounded": True},    # 重试成功
    ])
    response = _ask(repo, notebook, llm)

    assert "第二节正文" in response.answer
    assert _synthesis_detail(response)["outline_sections"] == 2
    assert _synthesis_detail(response)["outline_fallback"] is False
    assert response.model_errors == []


def test_a_failed_section_recovered_then_a_later_section_fails(repo, monkeypatch):
    """第 1 节重试后成功、第 2 节彻底失败 → 回退。第 1 节那条早就被自摘了,
    区间删除只需负责第 2 节那条 —— 两段机制叠加后仍然一条不剩、也一条不多摘。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "", "grounded": False},            # 第 1 节:空
        {"answer": "第一节正文", "grounded": True},    # 第 1 节:重试成功
        {"answer": "", "grounded": False},            # 第 2 节:空
        {"answer": "", "grounded": False},            # 第 2 节:重试仍空 → 失败
        {"answer": "整篇回退答案", "grounded": True},  # 单次合成
    ])
    response = _ask(repo, notebook, llm)

    assert response.answer == "整篇回退答案"
    assert _synthesis_detail(response)["outline_fallback"] is True
    assert response.model_errors == []


def test_a_failed_fallback_keeps_every_alarm(repo, monkeypatch):
    """回退也失败 = 这一轮真的没答出来,**两个阶段**的报警都要留着。

    只断言「非空」不够:摘除逻辑若忘了挂在「回退成功」这个条件上,分节那条会被无条件
    摘掉,而单次合成自己那条还在 —— 响应仍然非空,断言照样绿。所以这里数的是两条:
    分节阶段一条 + 回退阶段一条(各自都是"两次都空 content"后 _answer_with_retry
    记的那一条终态错误)。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文", "grounded": True},
        {"answer": "", "grounded": False},
        {"answer": "", "grounded": False},   # 第 2 节判失败
        {"answer": "", "grounded": False},   # 单次合成也空
        {"answer": "", "grounded": False},
    ])
    response = _ask(repo, notebook, llm)

    assert response.llm_mode == "synthesis_failed"
    assert len(response.model_errors) == 2, (
        "分节阶段与回退阶段各一条,一条都不许被摘掉"
    )
    assert {error.stage for error in response.model_errors} == {"answer"}
    assert _synthesis_detail(response)["outline_fallback"] is True


def test_each_finished_section_reports_progress(repo, monkeypatch):
    """分节合成是整轮最长的一段(k 次模型调用):不发进度,实时轨迹会静止几分钟。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    progress = _section_progress_steps(response)
    assert [step.detail["section_index"] for step in progress] == [1, 2]
    assert all(step.detail["section_total"] == 2 for step in progress)
    assert [step.detail["section_title"] for step in progress] == ["第一节", "第二节"]
    # 收尾那条总合成步排在进度步之后,且不带 section_index。
    steps = _synthesis_steps(response)
    assert steps[-1] not in progress
    assert steps[-1].detail["outline_sections"] == 2


def test_the_synthesis_span_is_counted_exactly_once(repo, monkeypatch):
    """进度步不带 duration_ms —— 否则合成耗时在轨迹总耗时里被计两遍。

    前端的「轨迹总耗时」是**所有**步 duration_ms 的求和(reasoning-trace.ts)。收尾
    那条总 synthesis 步已经独家记了整段合成时间;进度步再各记一份,分节答案的合成
    耗时就报成约两倍(回退 run 还会把已完成节的那几次尝试也算进去)。进度步是**进度
    标记**,不是独立的耗时区间。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    progress = _section_progress_steps(response)
    assert len(progress) == 2
    assert [step.duration_ms for step in progress] == [None, None]
    # 计时区间恰好一个:整条轨迹里带 duration_ms 的 synthesis 步只有收尾那条。
    timed = [step for step in _synthesis_steps(response)
             if step.duration_ms is not None]
    assert len(timed) == 1
    assert "outline_sections" in timed[0].detail
    # 前端口径:总耗时 = 各步 duration_ms 之和。合成段贡献的那一份必须**恰好等于**
    # 总步自己记的区间 —— 进度步一旦带上时长,这个和就会大于它(双计)。
    synthesis_total = sum(
        step.duration_ms or 0 for step in _synthesis_steps(response))
    assert synthesis_total == timed[0].duration_ms


def test_one_grounded_section_caps_the_whole_answer_at_overview(repo, monkeypatch):
    """部分支撑必须保留逐节测量信号,并把整体徽章封顶在 overview。"""
    count = 12
    elements = [_element(f"e{n}", f"要点{n}") for n in range(count)]
    outline = [_section(f"s{n}", f"第{n}节", f"e{n}") for n in range(count)]
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(elements=elements, outline=outline))
    # 前 11 节自报 ungrounded 且不引用;最后一节引用自己的元素并自报 grounded。
    last_key = (
        (count - 1) * OUTLINE_SECTION_KEY_STRIDE
        + AskService._ELEMENT_KEY_BASE + 1
    )
    llm = _CaptureAnswerLLM(answers=[
        *[{"answer": f"第{n}节正文（推断）。", "grounded": False}
          for n in range(count - 1)],
        {"answer": f"末节正文[k{last_key}]", "grounded": True},
    ])
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == count
    assert [anchor.key for anchor in response.anchors] == [f"k{last_key}"]
    detail = _synthesis_detail(response)
    assert detail["evidence_level"] == "overview"
    assert [item["grounded"] for item in detail["section_grounded"]] == [
        *[False] * (count - 1), True]
    assert detail["ungrounded_sections"] == [
        f"第{n}节" for n in range(count - 1)]
    assert response.evidence_level == "overview"
    assert response.grounded is False


def test_zero_grounded_sections_preserve_overview_without_claiming_no_evidence(
    repo, monkeypatch,
):
    """两节各有高分引用但模型保守时保留 overview，不误报「未命中依据」。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    first_key = AskService._ELEMENT_KEY_BASE + 1
    second_key = OUTLINE_SECTION_KEY_STRIDE + AskService._ELEMENT_KEY_BASE + 1
    llm = _CaptureAnswerLLM(answers=[
        {"answer": f"第一节正文[k{first_key}]", "grounded": False},
        {"answer": f"第二节正文[k{second_key}]", "grounded": False},
    ])

    response = _ask(repo, notebook, llm)

    detail = _synthesis_detail(response)
    assert [item["evidence_level"] for item in detail["section_grounded"]] == [
        "overview", "overview"]
    assert detail["ungrounded_sections"] == ["第一节", "第二节"]
    assert detail["evidence_level"] == "overview"
    assert response.evidence_level == "overview"
    assert response.grounded is False


def test_one_bound_section_keeps_the_one_shot_path(repo, monkeypatch):
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(
        outline=[_section("s1", "唯一一节", "e1"), _section("s2", "空的")]))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 1
    assert not response.answer.startswith("##")
    detail = _synthesis_detail(response)
    # codex r6 翻转:绕过按节合成不再抹掉披露键——规划跑过就写键集,空节
    # 「没找到」的记录随之保留(见 test_bypassed_sectioned_synthesis_still_
    # discloses_skipped_sections)。
    assert detail["outline_sections"] == 0
    assert detail["outline_fallback"] is False
    assert detail["outline_skipped"] == ["空的"]


def test_no_outline_leaves_the_synthesis_step_untouched(repo, monkeypatch):
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(outline=[]))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 1
    detail = _synthesis_detail(response)
    assert not any(key.startswith("outline_") for key in detail)


@pytest.mark.parametrize("effort", ["standard", "thorough"])
def test_a_lower_effort_never_sections_even_with_an_outline(
    repo, monkeypatch, effort
):
    """防御性闸:大纲天然只在穷尽档建得起来,但按节合成自己也要判一次。

    去掉这道闸,任何一条把大纲塞进结果的路径(未来的报告管线、测试替身)都会在
    低档位上悄悄付出 k 次合成调用的钱。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm, effort=effort)

    assert len(llm.prompts) == 1
    assert not any(key.startswith("outline_")
                   for key in _synthesis_detail(response))


def test_the_kill_switch_also_turns_off_sectioned_synthesis(repo, monkeypatch):
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    repo.settings.reasoning_outline_enabled = False
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 1
    assert not any(key.startswith("outline_")
                   for key in _synthesis_detail(response))


def _frontend_bound_keys(response):
    """按**前端**的口径解析:正文里的标记 × 合并后的引用表。

    前端 `buildAnswerReferences` 就是这么做的 —— `answer.matchAll(标记正则)` 去查
    `anchorsByKey`(整篇合并的那一份),而不是按节查。所以「跨节标记不许绑」这条合同
    必须在**送到读者眼前的那份文本**上成立,只让后端的按节解析不发锚点是不够的。
    整组里有一个键查不到就整组不绑(前端同款规则)。
    """
    by_key = {anchor.key: anchor for anchor in response.anchors}
    bound = []
    for group in re.findall(r"\[((?:k\d+\s*,\s*)*k\d+)\]", response.answer or ""):
        keys = [part.strip() for part in group.split(",")]
        if any(key not in by_key for key in keys):
            continue
        bound.extend(keys)
    return bound


def test_a_marker_from_another_sections_namespace_is_scrubbed_before_joining(
    repo, monkeypatch
):
    """一节写出别节的号 = 幻觉。锚点不发**而且**正文里的那个标记要洗掉。

    **两种口径必须在这里分叉**,所以第一节刻意**不**引用任何东西:第一节也合法引用
    同一个 key 的话,合并解析同样会产出那一条锚点,两种口径结果重合,这条用例就成了
    空转。

    只丢锚点、留着正文里的 `[k4001]` 是不够的(codex PR#407 R3 P1):前端拿正文标记
    去查**合并后**的引用表,那个号照样绑到第一节的证据上 —— 按节解析防住的误绑,从
    渲染这道后门原样回来。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    first_section_key = f"k{AskService._ELEMENT_KEY_BASE + 1}"
    llm = _CaptureAnswerLLM(answers=[
        # 第一节合法引用它自己的号 —— 合并表里于是**真的有** k4001 这条锚点,
        # 第二节抄来的那个号才有东西可以误绑上去(否则前端本来也绑不上,空转)。
        {"answer": f"第一节正文[{first_section_key}]", "grounded": True},
        # 第二节的合法号段是 k1400x,这里故意抄第一节的号。
        {"answer": f"第二节正文[{first_section_key}]", "grounded": True},
    ])
    response = _ask(repo, notebook, llm)

    # 第一节自己那个标记逐字保留(合法的一份不受清洗影响)。
    assert response.answer.count(f"[{first_section_key}]") == 1
    assert response.answer.split("## 第二节")[1].strip() == "第二节正文"
    assert [anchor.key for anchor in response.anchors] == [first_section_key]
    # 前端口径:整篇只解析出第一节那一处引用,第二节一处都没有。
    assert _frontend_bound_keys(response) == [first_section_key]


@pytest.mark.parametrize("localized", [False, True])
def test_a_mixed_marker_group_keeps_only_this_sections_key(
    repo, monkeypatch, localized,
):
    """半角/中文混合组都只保留本节合法键,规范成 `[本节合法键]` 并绑上。

    混合组整组丢弃在这里是错的:号段互不相交是服务端造的事实,同组里那个合法键是
    服务端确确实实发给这一节、它也确确实实看见了的证据 —— 连着好的一半一起丢,
    是拿一次幻觉惩罚一条真引用。

    顺序也在这里被钉住:`parse_anchors` 对混合组整组失效,所以清洗必须在解析**之前**
    做,否则正文写着 `[k14001]`、锚点却没有,变成一个永远绑不上的标记。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    alien = f"k{AskService._ELEMENT_KEY_BASE + 1}"                    # 第一节的号
    own = f"k{OUTLINE_SECTION_KEY_STRIDE + AskService._ELEMENT_KEY_BASE + 1}"
    marker = f"【{own}，{alien}】" if localized else f"[{own}, {alien}]"
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文,不带任何引用。", "grounded": False},
        {"answer": f"第二节正文{marker}", "grounded": True},
    ])
    response = _ask(repo, notebook, llm)

    assert alien not in response.answer
    assert f"[{own}]" in response.answer
    assert [anchor.key for anchor in response.anchors] == [own]
    assert _frontend_bound_keys(response) == [own]


def test_an_entirely_alien_group_leaves_no_residue(repo, monkeypatch):
    """整组全非法 → 连方括号一起移除,不留空括号、多余逗号或双空格。"""
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    alien_a = f"k{AskService._ELEMENT_KEY_BASE + 1}"
    alien_b = f"k{AskService._ELEMENT_KEY_BASE + 2}"
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文。", "grounded": False},
        {"answer": f"前半句 [{alien_a}, {alien_b}] 后半句。", "grounded": True},
    ])
    response = _ask(repo, notebook, llm)

    body = response.answer.split("## 第二节")[1].strip()
    assert body == "前半句 后半句。"
    for residue in ("[]", "[,", ",]", "  "):
        assert residue not in body, residue
    assert response.anchors == []


def test_scrubbing_leaves_indentation_and_legal_markers_alone(repo, monkeypatch):
    """清洗只收敛「删标记留下的那个空格」,不碰行首缩进。

    分节答案是长文,嵌套列表(`  - 项`)与四空格代码块都很常见;无差别地把 2+ 空格压成
    1 个,会把它们一起压平 —— 那对读者比漏洗一个标记严重得多。
    """
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result())
    own = f"k{OUTLINE_SECTION_KEY_STRIDE + AskService._ELEMENT_KEY_BASE + 1}"
    llm = _CaptureAnswerLLM(answers=[
        {"answer": "第一节正文。", "grounded": False},
        {"answer": f"要点如下:\n- 一级\n  - 二级缩进\n结论[{own}]", "grounded": True},
    ])
    response = _ask(repo, notebook, llm)

    body = response.answer.split("## 第二节")[1].strip()
    assert "\n  - 二级缩进\n" in body, "行首缩进必须原样保留"
    assert body.endswith(f"结论[{own}]"), "合法标记逐字保留"


def test_a_bound_object_outside_the_ranked_selection_still_grounds_the_answer(
    repo, monkeypatch
):
    """按节合成喂给模型的知识对象若不在 top_hits 里,引用它不能被判成"引了空气"。

    **grounded 必须唯一由 outline_pool_extra 决定**,所以这里两节绑的都是被截断的
    知识对象、`elements=[]`:留一个 score 0.9 的元素在场的话,`classify_evidence` 会
    从元素那边拿到足够的 anchored_rel,拿掉 outline_pool_extra 照样 grounded,这条
    用例就成了空转。
    """
    notebook = _notebook(repo)
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "被截断的要点甲", "section_path": "1"},
         "evidence": []},
        {"local_id": "C2", "object_type": "claim",
         "payload": {"name": "被截断的要点乙", "section_path": "2"},
         "evidence": []},
    ], [])
    with repo._connect() as db:
        object_ids = [row["id"] for row in db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? "
            "ORDER BY created_at, id", (notebook.id,)).fetchall()]
    assert len(object_ids) == 2
    truncated = [_knowledge(oid, f"被截断的要点{n}", relevance=0.95)
                 for oid, n in zip(object_ids, ("甲", "乙"))]
    _stub_run(monkeypatch, _reasoning_result(
        top_hits=[],
        elements=[],                      # 唯一的证据来源就是那两个被截断的对象
        outline_evidence=truncated,
        outline=[_section("s1", "第一节", object_ids[0]),
                 _section("s2", "第二节", object_ids[1])],
    ))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    assert len(llm.prompts) == 2
    assert "被截断的要点甲" in llm.prompts[0]
    assert "被截断的要点乙" in llm.prompts[1]
    assert {anchor.object_id for anchor in response.anchors} == set(object_ids)
    assert _synthesis_detail(response)["evidence_level"] == "grounded"


def test_a_real_run_builds_the_outline_and_writes_it_section_by_section(repo):
    """端到端:reflect 建两节大纲 → 合成走按节路径 → 答案带两级标题。"""
    notebook = _notebook(repo)
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点甲", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "claim",
         "payload": {"name": "版图设计要点乙", "section_path": "2"}, "evidence": []},
    ], [])

    class _LLM(_CaptureAnswerLLM):
        def __init__(self):
            super().__init__()
            self.bound = False

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            if "next_action" in schema_hint and not self.bound:
                body = content.split("Candidates so far:", 1)[-1]
                ids = re.findall(r"\(id=([^)]+)\)", body)
                if len(ids) >= 2:
                    self.bound = True
                    return json.dumps({
                        "next_action": OUTLINE_ACTION, "reason": "整理",
                        "outline": {"sections": [
                            {"id": "s1", "title": "甲部分",
                             "evidence": [ids[0]]},
                            {"id": "s2", "title": "乙部分",
                             "evidence": [ids[1]]},
                        ]},
                    })
            return super().chat_json(messages, schema_hint, **kwargs)

    llm = _LLM()
    response = _ask(repo, notebook, llm)

    assert llm.bound, "脚本模型必须真的提交过一份大纲"
    assert len(llm.prompts) == 2
    assert response.answer.startswith("## 甲部分")
    assert "## 乙部分" in response.answer
    detail = _synthesis_detail(response)
    assert detail["outline_sections"] == 2
    assert detail["outline_fallback"] is False


def test_a_final_outline_submitted_with_sufficient_reaches_the_synthesis(repo):
    """端到端:定稿大纲与 `sufficient=true` 在**同一轮**交上来,按节合成照样吃到它。

    这是最常见的收尾形态——reflect prompt 教模型「每个方面都有证据了才置
    sufficient」,那正是它交出最终版大纲的时刻。`sufficient` 短路若排在动作分发之前,
    这份载荷会被静默丢弃:检索循环里没有任何报错,答案却退回一次性合成,用户拿到的
    是一篇没有结构的答案(codex PR#407 R2 P2)。
    """
    notebook = _notebook(repo)
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点甲", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "claim",
         "payload": {"name": "版图设计要点乙", "section_path": "2"}, "evidence": []},
    ], [])

    class _LLM(_CaptureAnswerLLM):
        def __init__(self):
            super().__init__()
            self.bound = False

        def chat_json(self, messages, schema_hint, **kwargs):
            content = messages[-1]["content"]
            if "next_action" in schema_hint and not self.bound:
                body = content.split("Candidates so far:", 1)[-1]
                ids = re.findall(r"\(id=([^)]+)\)", body)
                if len(ids) >= 2:
                    self.bound = True
                    # 与上一条用例的唯一差别:同一份响应里还置了 sufficient。
                    return json.dumps({
                        "next_action": OUTLINE_ACTION, "reason": "补齐后收尾",
                        "sufficient": True,
                        "outline": {"sections": [
                            {"id": "s1", "title": "甲部分",
                             "evidence": [ids[0]]},
                            {"id": "s2", "title": "乙部分",
                             "evidence": [ids[1]]},
                        ]},
                    })
            return super().chat_json(messages, schema_hint, **kwargs)

    llm = _LLM()
    response = _ask(repo, notebook, llm)

    assert llm.bound
    assert len(llm.prompts) == 2, "两节 = 两次合成调用(载荷被丢的话只会有一次)"
    assert response.answer.startswith("## 甲部分")
    assert "## 乙部分" in response.answer
    detail = _synthesis_detail(response)
    assert detail["outline_sections"] == 2
    assert detail["outline_fallback"] is False
    # 那一轮只发生一次反思(应用完就收尾,不再多问一轮)。
    outline_steps = [step for step in (response.reasoning_trace or [])
                     if step.step_type == "outline"]
    assert len(outline_steps) == 1
    assert outline_steps[0].detail["empty_sections"] == []


def test_bypassed_sectioned_synthesis_still_discloses_skipped_sections(
    repo, monkeypatch
):
    """codex r6: 大纲只装配出 1 个有证据节 + 1 个空节时,按节合成被绕过
    (<2 节走普通单次合成),但空节「问到了没找到」的披露不能跟着消失——旧
    条件挂在 outline_attempted 上,这种单节答案会显得完整。键集条件放宽到
    outline_planned(规划真的跑过);没有大纲/低档位/关闭态下规划不会跑,
    synthesis 步 detail 仍逐键不变(冻结基线口径)。"""
    outline = [
        _section("s1", "有证据的一节", "e1"),
        _section("s0", "没找到证据的一节"),
    ]
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(outline=outline))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    # 按节合成没有跑:单节走普通单次合成,答案里没有服务端加的节标题。
    assert len(llm.prompts) == 1
    assert "## 有证据的一节" not in response.answer
    detail = _synthesis_detail(response)
    assert detail["outline_skipped"] == ["没找到证据的一节"]
    assert detail["outline_sections"] == 0
    assert detail["outline_fallback"] is False
    assert detail["section_grounded"] == []
    assert detail["ungrounded_sections"] == []


def test_an_enumerating_run_keeps_the_one_shot_path(repo, monkeypatch):
    """codex r7 P1: 产出过集合清单的 run 不做按节合成——节切片刻意只装该节
    绑定证据,清单块/覆盖披露不进切片,「按来源列出全部公式」这类请求被节化
    后合成只拿 ranked 样本写散文,丢掉手上已有的完整清单,甚至自称不完整。
    清单类 run 保持单次合成(清单预览与覆盖披露完整进入合成上下文);清单进
    节切片是 v2 的设计题,不在绕过里偷做。"""
    from app.services.collection_enumeration import ElementItem, EnumerationCoverage
    from app.services.reasoning_retrieval import CollectionEnumerationOutcome

    outcome = CollectionEnumerationOutcome(
        collection="elements", kind="formula", source_id="",
        items=[ElementItem(
            element_id="el-9", source_id="s1", source_title="论文一",
            element_type="formula", location_label="p1", text="公式甲",
            asset_id="", notebook_id="nb", tier="personal",
        )],
        coverage=EnumerationCoverage(
            returned=1, returned_total=1, scanned=1, total=1, has_more=False,
            complete=True, truncated_reason="", overflow_semantics="",
        ),
    )
    notebook = _notebook(repo)
    _stub_run(monkeypatch, _reasoning_result(enumerations=[outcome]))
    llm = _CaptureAnswerLLM()
    response = _ask(repo, notebook, llm)

    # 大纲有 2 个可装配节,但因为有清单,按节合成不跑:单次合成,清单预览
    # 仍进那唯一一份合成 prompt(这正是绕过要保住的东西)。
    assert len(llm.prompts) == 1
    assert "公式甲" in llm.prompts[0]
    assert "## 第一节" not in response.answer
    detail = _synthesis_detail(response)
    assert detail["outline_sections"] == 0
    assert detail["outline_fallback"] is False
