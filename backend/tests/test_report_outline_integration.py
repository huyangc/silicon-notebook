"""深度报告接入大纲共演化(PR-5,设计文档 §3.4)。

覆盖四件事:五档映射(研究深度 → 检索档位)、穷尽档在节深挖里自动激活大纲、
子大纲整理成有界「发现的结构」块进节撰写 prompt、节 phase 的大纲进度文案。
外加一条负向守卫:用户确认过的 `reports.outline_json` 绝不被回写。

失败场景就是这个特性的四种坏掉方式:档位表错位(滑块买到别档的预算)、
穷尽档拿不到大纲动作(整个特性在报告里是死的)、结构块无界或 [k] 指到撰写模型
看不见的 id(模型引用一个不存在的东西)、以及最危险的一种——深挖发现的子结构
悄悄改掉了用户确认的章节合同。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.ask import TraceStep
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.prompts import report_section_prompt
from app.services.reasoning_retrieval import (
    OUTLINE_ACTION, OutlineSection, ReasoningResult, ReasoningRetriever,
    outline_wiring_active,
)
from app.services.report_engine import (
    REPORT_DEPTH_EFFORTS, ReportEngine, _STRUCTURE_MAX_CHARS,
    _STRUCTURE_MAX_LINES, _STRUCTURE_MAX_LINE_CHARS,
    knowledge_context_with_outline, outline_structure_block,
    report_retrieval_effort, report_retrieval_limits,
)
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-07-31T00:00:00+08:00"

# 深挖阶段带大纲进度的 phase 文案(2 节时)。跨栈耦合的后端那一半:下面的 phase
# 用例断言引擎真的产出它,前端对照用例断言它以前端匹配的前缀开头。
_OUTLINE_PROGRESS_PHASE = "深挖中（已整理大纲 2 节）"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL", "REWRITE_LLM_API_KEY",
                "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _notebook(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
    ], [])
    return notebook


def _engine(repo, llm=None):
    if llm is not None:
        for workload_id in ("report_outline", "report_sufficiency",
                            "report_section", "report_summary", "query_rewrite",
                            "reasoning_agent", "ask_answer"):
            bind_chat_client(repo, workload_id, llm)
    return ReportEngine.from_repository(repo, repo.settings)


class _SectionLLM:
    """报告侧的脚本模型:大纲/充分性/节撰写/摘要各按 prompt 特征应答,
    并把节深挖的 reflect prompt 原样留档(动作是否在场只有 prompt 说了算)。"""

    configured = True

    def __init__(self, reflects=None):
        self.section_prompts: list[str] = []
        self.reflect_prompts: list[str] = []
        self._reflects = list(reflects or [])

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "OUTLINE" in content:
            return json.dumps({"sections": [
                {"title": "A", "scope": "sa", "sub_queries": ["版图设计要点"]}]})
        if "ONE section" in content:
            self.section_prompts.append(content)
            return json.dumps({"markdown": "## A\nbody [k1]", "grounded": True})
        if "EXECUTIVE SUMMARY" in content:
            return json.dumps({"summary": "总结"})
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "版图设计要点"}]})
        if "next_action" in schema_hint:
            self.reflect_prompts.append(content)
            if self._reflects:
                return json.dumps(self._reflects.pop(0))
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({})


# --------------------------------------------------------------- 五档映射

def test_the_depth_table_pins_every_one_of_the_five_levels():
    """研究深度 → 检索档位逐档钉死。

    错位一档(16 判成 thorough)就是两件事同时坏:用户在「穷尽」上付的钱买到
    thorough 的预算,而大纲便签在报告里永远不出现——它的闸判的就是档位 id。
    """
    assert REPORT_DEPTH_EFFORTS == (
        (1, "overview"), (2, "standard"), (4, "deep"), (8, "thorough"),
        (16, "exhaustive"),
    )
    assert [report_retrieval_effort(depth) for depth in (1, 2, 4, 8, 16)] == [
        "overview", "standard", "deep", "thorough", "exhaustive"]
    assert [report_retrieval_limits(depth).effort for depth in (1, 2, 4, 8, 16)] == [
        "overview", "standard", "deep", "thorough", "exhaustive"]


def _report_view_source() -> str:
    return (Path(__file__).resolve().parents[2]
            / "frontend" / "app" / "report-view.tsx").read_text(encoding="utf-8")


def test_the_depth_table_matches_the_slider_in_the_frontend():
    """档位下标必须与前端滑块的 DEPTHS 逐档对应。

    这两张表是同一件事的两半:前端按下标发 depth 值,后端按 depth 值选档位。
    只改一侧的话,用户选的第 5 档会在后端解析成第 4 档,而两边各自的测试都绿。
    """
    source = _report_view_source()
    match = re.search(r"const DEPTHS = \[([^\]]+)\];", source)
    assert match, "前端 report-view.tsx 的 DEPTHS 表找不到了"
    depths = [int(value.strip()) for value in match.group(1).split(",")]

    assert depths == [threshold for threshold, _ in REPORT_DEPTH_EFFORTS]


def test_the_frontend_matches_the_phase_prefix_not_the_exact_word():
    """节进度的「第 N 步」按 phase **前缀**判定,这是一条跨栈耦合。

    后端把深挖阶段的文案细化成了「深挖中（已整理大纲 N 节）」;前端一旦退回
    `s.phase === "深挖"` 的精确相等(或后端改掉文案开头),步数计数器就在穷尽档
    报告里静默消失 —— 而两边各自的单测都绿,因为各自都自洽。所以这条断言必须
    落在能同时看到两侧的地方。
    """
    source = _report_view_source()

    assert 'startsWith("深挖")' in source, (
        "前端必须按前缀判定深挖阶段;改回精确相等会让大纲进度出现时步数消失")
    assert 's.phase === "深挖"' not in source
    # 另一半:后端的细化文案必须真的以前端匹配的那个前缀开头。前缀从前端源码里
    # 读出来,而 _OUTLINE_PROGRESS_PHASE 由下面的 phase 用例钉在后端实际产出上——
    # 任何一侧单方面改文案,这条就红。
    prefixes = re.findall(r's\.phase\.startsWith\("([^"]+)"\)', source)
    assert prefixes, "前端不再按 phase 前缀判定深挖阶段"
    assert all(_OUTLINE_PROGRESS_PHASE.startswith(prefix) for prefix in prefixes)


def test_depths_outside_the_table_still_resolve_deterministically():
    """API 只把 depth clamp 到 [1,16]:3/5/7 必须有确定答案,不能抛 KeyError。"""
    assert report_retrieval_effort(3) == "standard"
    assert report_retrieval_effort(5) == "deep"
    assert report_retrieval_effort(15) == "thorough"
    assert report_retrieval_effort(0) == "overview"
    assert report_retrieval_effort(99) == "exhaustive"
    # 没有深度的调用方保持接入前的「无 limits」。
    assert report_retrieval_limits(None) is None


def test_only_the_top_depth_activates_the_outline_wiring():
    """大纲便签(与叠在它上面的弱支撑边回喂)只在穷尽档为真——零新 flag,
    报告侧的激活条件就是「档位选到穷尽」。"""
    settings = Settings()
    assert [outline_wiring_active(settings, report_retrieval_limits(depth))
            for depth in (1, 2, 4, 8, 16)] == [False, False, False, False, True]

    settings.reasoning_outline_enabled = False
    assert outline_wiring_active(settings, report_retrieval_limits(16)) is False


def test_deep_dive_passes_the_limits_and_keeps_depth_as_max_steps(repo, monkeypatch):
    """`limits` 按档位走,`max_steps` 仍是报告自己的 depth 值。

    把 max_steps 换成档位表里的 50,穷尽档报告的成本会按节数放大到用户没同意
    的量级(6 节 × 50 轮)。
    """
    captured: list[dict] = []

    def _run(self, notebook_id, question, history="", **kwargs):
        captured.append(kwargs)
        return ReasoningResult()

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    engine = _engine(repo, _SectionLLM())
    section = {"title": "t", "scope": "s", "sub_queries": []}

    for depth in (1, 2, 4, 8, 16):
        engine._deep_dive("nb", section, "Q", depth=depth)
    engine._deep_dive("nb", section, "Q")

    assert [call["limits"].effort for call in captured[:5]] == [
        "overview", "standard", "deep", "thorough", "exhaustive"]
    assert [call["max_steps"] for call in captured[:5]] == [1, 2, 4, 8, 16]
    assert captured[5]["limits"] is None and captured[5]["max_steps"] is None


# ------------------------------------------ 穷尽档在节深挖里激活大纲便签

@pytest.mark.parametrize("depth,offered", [(16, True), (8, False)])
def test_the_section_reflect_prompt_offers_the_outline_only_at_exhaustive(
    repo, depth, offered
):
    """零新机制:传了 limits,穷尽档的节深挖 reflect prompt 里就有 update_outline。

    低档位必须逐字回到接入前——动作不进 prompt,模型看不到它。
    """
    notebook = _notebook(repo)
    llm = _SectionLLM()
    engine = _engine(repo, llm)

    engine._deep_dive(
        notebook.id, {"title": "A", "scope": "sa", "sub_queries": ["版图设计要点"]},
        "Q", depth=depth,
    )

    assert llm.reflect_prompts
    assert all((OUTLINE_ACTION in prompt) is offered
               for prompt in llm.reflect_prompts)


def test_collection_enumeration_stays_unreachable_in_the_report_path(repo):
    """集合枚举在报告路径保持不可达(v1 刻意不接):构造 retriever 时不传
    catalog/enumeration,穷尽档也不该出现清单动作或集合地图。"""
    notebook = _notebook(repo)
    llm = _SectionLLM()
    engine = _engine(repo, llm)

    engine._deep_dive(
        notebook.id, {"title": "A", "scope": "sa", "sub_queries": ["版图设计要点"]},
        "Q", depth=16,
    )

    for prompt in llm.reflect_prompts:
        assert "enumerate_elements" not in prompt
        assert "[Collections in scope]" not in prompt


def test_deep_dive_merges_the_outline_bound_truncated_objects(repo, monkeypatch):
    """被最终选集挤出的大纲绑定对象必须并入本节证据池(规则同 ask_service)。

    漏掉它:「发现的结构」里指向它的 [k] 解析不出来,而它恰恰是模型自己指名
    要的那条支撑。
    """
    kept = SimpleNamespace(object_id="o-kept", relevance=0.9)
    extra = SimpleNamespace(object_id="o-extra", relevance=0.3)

    def _run(self, notebook_id, question, history="", **kwargs):
        return ReasoningResult(top_hits=[kept], outline_evidence=[extra, kept])

    monkeypatch.setattr(ReasoningRetriever, "run", _run)
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a, q: [])
    engine = _engine(repo, _SectionLLM())

    result = engine._deep_dive("nb", {"title": "t", "scope": "s"}, "Q", depth=16)

    assert [hit.object_id for hit in result.top_hits] == ["o-kept", "o-extra"]


def test_a_real_outline_action_reaches_the_section_writer(repo):
    """端到端:节深挖的 reflect 真的返回 `update_outline` → `ReasoningResult.outline`
    → 「发现的结构」进节撰写 prompt。

    上面那些块级用例都是直接喂 `OutlineSection` 的。这一条是唯一一条证明**整条链**
    接通的用例:模型提交 → 绑定校验(只留合法候选键) → run 收尾把大纲带回结果 →
    `_draft_section` 按 id_map 反查。任何一环断掉,块级用例全都照绿。
    """
    notebook = _notebook(repo)
    hits = repo.retrieval.federated_retrieve(notebook.id, "版图设计要点")
    assert hits, "夹具的知识对象没被检索到,绑定就无从谈起"
    object_id = hits[0].object_id
    llm = _SectionLLM(reflects=[{
        "next_action": "update_outline", "sufficient": False,
        "outline": {"sections": [
            {"id": "o1", "title": "工艺角对比", "evidence": [object_id]},
            {"id": "o2", "title": "还没找到证据的一节",
             "evidence": ["ko-从来不是候选"]},
        ]},
    }])
    engine = _engine(repo, llm)
    section = {"title": "A", "scope": "sa", "sub_queries": ["版图设计要点"]}

    result = engine._deep_dive(notebook.id, section, "Q", depth=16)

    assert [s.title for s in result.outline] == ["工艺角对比", "还没找到证据的一节"]
    assert result.outline[0].evidence_keys == [object_id]
    assert result.outline[1].evidence_keys == []      # 非法键静默丢弃

    engine._draft_section(notebook.id, section, "Q", result)

    prompt = llm.section_prompts[0]
    match = re.search(r"### 工艺角对比 — 证据: \[(k\d+)\]", prompt)
    assert match, prompt
    assert f"\n{match.group(1)}: [claim]" in prompt
    # 绑定解析不出来的那一节降级成裸标题(而不是消失,也不是留一个悬空 [k])。
    assert "### 还没找到证据的一节\n" in prompt


# --------------------------------------------------- 「发现的结构」块

def _section(section_id, title, keys=(), parent=""):
    return OutlineSection(id=section_id, title=title, parent=parent,
                          evidence_keys=list(keys))


def _id_map(*object_ids):
    return {f"k{index}": {"object_id": object_id, "relevance": 0.5}
            for index, object_id in enumerate(object_ids, 1)}


def test_the_structure_block_maps_bindings_into_the_writers_own_id_space():
    """绑定键是检索期 id,`[k]` 是撰写上下文分配的号——必须按 id_map 反查。

    直接把绑定键写进块里,模型看到的就是一串它在 Knowledge items 里找不到的 id。
    """
    block = outline_structure_block(
        [_section("s1", "工艺角", ["obj-b", "obj-a"]),
         _section("s2", "版图约束", ["obj-a"], parent="s1")],
        _id_map("obj-a", "obj-b"),
    )

    assert block == "### 工艺角 — 证据: [k2, k1]\n#### 版图约束 — 证据: [k1]"


def test_unresolvable_bindings_are_dropped_but_the_title_survives():
    """被上下文预算截断、没进本节 id_map 的绑定如实丢弃,子话题标题仍保留
    (prompt 教模型「缺证据就略过」,那比凭空少一个子话题诚实)。"""
    block = outline_structure_block(
        [_section("s1", "工艺角", ["obj-a", "不在上下文里"]),
         _section("s2", "全空的一节", ["也不在"]),
         _section("s3", "本来就没绑")],
        _id_map("obj-a"),
    )

    assert block == "### 工艺角 — 证据: [k1]\n### 全空的一节\n### 本来就没绑"


def test_the_structure_block_is_absent_without_an_outline():
    assert outline_structure_block([], _id_map("obj-a")) == ""
    assert outline_structure_block([_section("s1", "  ")], _id_map("obj-a")) == ""
    # 大纲在、但一个绑定都解析不出来时块仍在:标题本身就是结构。
    assert outline_structure_block([_section("s1", "一节")], {}) == "### 一节"


def test_the_documented_bounds_are_the_ones_in_the_code():
    """契约里的三个数字与代码常量对上(下面的夹取用例拿常量当期望值,
    只改常量的话它们会跟着一起动、全绿放行)。"""
    assert (_STRUCTURE_MAX_LINES, _STRUCTURE_MAX_LINE_CHARS,
            _STRUCTURE_MAX_CHARS) == (12, 80, 1200)


def test_the_structure_block_clamps_lines_and_reports_what_it_dropped():
    sections = [_section(f"s{i}", f"子话题{i}", ["obj-a"]) for i in range(20)]

    block = outline_structure_block(sections, _id_map("obj-a"))

    lines = block.split("\n")
    assert len(lines) == _STRUCTURE_MAX_LINES + 1        # 12 行正文 + 账目行
    assert lines[0] == "### 子话题0 — 证据: [k1]"
    assert lines[-1] == "(+8 子节略)"                     # 静默丢行是不允许的
    assert len(block) <= _STRUCTURE_MAX_CHARS


def test_a_long_title_is_clamped_per_line():
    block = outline_structure_block(
        [_section("s1", "长" * 200, ["obj-a"])], _id_map("obj-a"))

    assert len(block) == _STRUCTURE_MAX_LINE_CHARS
    assert block.startswith("### 长长长")


def test_markers_that_do_not_fit_are_dropped_whole_not_cut_in_half():
    """行内先裁标题、再逐个装 [k]。

    直接裁整行的话,60 字符标题(parse 的上限)配几个绑定就会切出 `[k1, k` ——
    一个看着像引用、模型却解释不了的半截标记。
    """
    ids = [f"obj-{index}" for index in range(8)]
    block = outline_structure_block(
        [_section("s1", "长" * 55, ids)], _id_map(*ids))

    assert len(block) <= _STRUCTURE_MAX_LINE_CHARS
    assert block.endswith("]") and block.count("[") == block.count("]") == 1
    # 装得下几个是几个,不是全丢也不是切半截。
    kept = block.split("[")[1].rstrip("]").split(", ")
    assert 0 < len(kept) < len(ids)
    assert kept == [f"k{index}" for index in range(1, len(kept) + 1)]


def test_the_character_budget_is_an_independent_second_guard(monkeypatch):
    """字符闸是与行数闸并列的第二道闸,不是同一道闸的两种写法。

    当前常量下行数闸总是先到(12 行 × 80 字符 + 换行 = 971 < 1200),所以这条用例
    把**测试局部**阈值调低来走字符闸那条分支,生产 floor 单独断言——否则那个分支
    永远不被执行,删掉它也全绿。
    """
    from app.services import report_engine as engine_module

    # 生产 floor:三个常量的组合让行数闸先到,字符闸留作纵深防御。
    assert (_STRUCTURE_MAX_LINES * (_STRUCTURE_MAX_LINE_CHARS + 1)
            <= _STRUCTURE_MAX_CHARS)

    monkeypatch.setattr(engine_module, "_STRUCTURE_MAX_CHARS", 120)
    sections = [_section(f"s{i}", "长" * 60, ["obj-a"]) for i in range(6)]

    block = outline_structure_block(sections, _id_map("obj-a"))

    assert len(block) <= 120
    assert block.split("\n")[-1] == "(+5 子节略)"
    # 连一行加账目都装不下时,诚实的输出是空块,而不是一条只剩账目的孤行。
    monkeypatch.setattr(engine_module, "_STRUCTURE_MAX_CHARS", 80)
    assert outline_structure_block(sections, _id_map("obj-a")) == ""


# ------------------------------------------------ 节撰写 prompt 的三态

def test_the_section_prompt_is_byte_identical_without_the_block():
    """缺席态(非穷尽档、或本节没整理出大纲)必须逐字回到接入前。"""
    base = report_section_prompt("T", "S", "Q", "CTX")

    assert report_section_prompt("T", "S", "Q", "CTX",
                                 discovered_structure="") == base
    assert "Discovered structure" not in base
    assert base.endswith('Return JSON only: {"markdown":"","grounded":true|false}')


def test_the_section_prompt_teaches_the_block_as_a_suggestion():
    """措辞必须同时说清三件事:用 ### 组织、只是建议、缺证据如实略过。

    少了「建议」那半,模型会为了填满结构编内容——那正是这个块最贵的失败方式。
    """
    prompt = report_section_prompt("T", "S", "Q", "CTX",
                                   discovered_structure="### 子话题 — 证据: [k1]")

    assert "Discovered structure" in prompt
    assert "### 子话题 — 证据: [k1]" in prompt
    assert "'###' sub-headings" in prompt
    assert "SUGGESTION, not a contract" in prompt
    assert "skip any sub-topic" in prompt
    assert "never invent content" in prompt
    # 块里的 id 长得像标题的一部分,措辞必须把它们赶回正文句子:留在 `###` 标题里
    # 的 [k] 既不被 parse_anchors 当成句子的归因,读者也会在小标题上看到一串号。
    assert "never in the heading" in prompt
    # 位置:块必须排在 Knowledge items 之后、返回格式之前(规则出现在它约束的
    # 输入之前才有效)。
    assert (prompt.index("Knowledge items")
            < prompt.index("Discovered structure")
            < prompt.index("Return JSON only"))


def test_draft_section_hands_the_structure_block_to_the_writer(repo):
    chunk = RetrievedChunk(
        chunk_id="c1", source_id="s1", source_title="论文一",
        section_path="1.1 工艺角", text="工艺角正文", relevance=0.9,
    )
    llm = _SectionLLM()
    engine = _engine(repo, llm)
    result = ReasoningResult(
        chunks=[chunk],
        outline=[_section("s1", "工艺角对比", ["c1"]),
                 _section("s2", "尚无证据的一节")],
    )

    engine._draft_section("nb", {"title": "A", "scope": "sa",
                                 "sub_queries": ["q"]}, "Q", result)

    prompt = llm.section_prompts[0]
    assert "### 工艺角对比 — 证据: [k1]" in prompt
    assert "### 尚无证据的一节" in prompt


def test_draft_section_without_an_outline_keeps_the_prompt_unchanged(repo):
    """低档位/无大纲:节撰写 prompt 里连块头都不该出现。"""
    llm = _SectionLLM()
    engine = _engine(repo, llm)

    engine._draft_section("nb", {"title": "A", "scope": "sa",
                                 "sub_queries": ["q"]}, "Q", ReasoningResult())

    assert "Discovered structure" not in llm.section_prompts[0]


# -------------------------------------- 大纲绑定证据的 KG 上下文子预算(P1)

def _claims(repo, notebook_id, count):
    """建 count 条知识对象,返回它们的 DB id(store_kg 就地回填 `_oid`)。"""
    objects = [{"local_id": f"B{index}", "object_type": "claim",
                "payload": {"name": f"版图设计要点{index}" + "细" * 20,
                            "section_path": "1"},
                "evidence": []}
               for index in range(count)]
    repo.store_kg(notebook_id, None, objects, [])
    return [obj["_oid"] for obj in objects]


def _kg_hits(object_ids):
    return [RetrievedKnowledge(object_id=object_id, object_type="claim",
                               payload={"name": f"名{index}"}, relevance=0.9)
            for index, object_id in enumerate(object_ids)]


def test_outline_bound_evidence_survives_a_full_kg_budget(repo):
    """P1:候选池挤满 KG 预算时,大纲绑定的证据仍然必须进得了撰写上下文。

    `_deep_dive` 把绑定的截断对象追加在 `top_hits` **尾部**(它们的相关度本来就低于
    选集),而 `knowledge_context` 按序渲染、预算用尽即 break。只靠尾插的话,穷尽档
    (最终相关性 floor=40)在大候选池下必然把它们挤出上下文 → 结构块反查不到就丢弃
    绑定 → 子话题退成裸标题 → prompt 教「缺证据略过」→ 它从成稿里整条消失。

    这里把**测试局部**的 KG 预算调小来复现同一个形状(生产 floor 另行断言):
    12 条候选、预算只装得下前 4 条,而绑定的那条排在第 12 位。
    """
    # 生产 floor:预算是固定的,而穷尽档的相关性 floor 是 40 —— 「候选比预算多」
    # 在生产上是常态,不是测试构造出来的边角。
    assert Settings().answer_context_budget_chars == 6000
    assert ask_retrieval_limits("exhaustive").ranked_final_floor == 40

    notebook = _notebook(repo)
    object_ids = _claims(repo, notebook.id, 12)
    bound = object_ids[-1]
    llm = _SectionLLM()
    engine = _engine(repo, llm)
    engine.settings.answer_context_budget_chars = 200
    result = ReasoningResult(
        top_hits=_kg_hits(object_ids),
        outline=[_section("s1", "尾部绑定的一节", [bound])],
    )

    engine._draft_section(notebook.id, {"title": "A", "scope": "sa",
                                        "sub_queries": ["q"]}, "Q", result)

    prompt = llm.section_prompts[0]
    match = re.search(r"### 尾部绑定的一节 — 证据: \[(k\d+)\]", prompt)
    assert match, "尾部的大纲绑定被预算挤掉了,子话题只剩裸标题"
    # 结构块引用的 [k] 必须在 Knowledge items 里真实存在(否则模型引用的是一个
    # 它看不见的 id)。
    # …而且就是**绑定的那一条**(第 12 个候选),不是恰好排在同一个号上的别人。
    assert f"\n{match.group(1)}: [claim][personal] 名11" in prompt
    # ranked 命中没有被绑定子集饿死:剩余预算仍渲染了别的候选。
    keys = re.findall(r"\n(k\d+): \[claim\]", prompt)
    assert len(keys) >= 3
    # 两遍的 [k] 连续且不重叠。
    assert keys == [f"k{index}" for index in range(1, len(keys) + 1)]


def test_the_kg_context_stays_within_the_budget_across_both_passes(repo):
    """两遍共用一份总预算,不是各拿一份。"""
    notebook = _notebook(repo)
    object_ids = _claims(repo, notebook.id, 12)
    context = ReportEngine.from_repository(
        repo, repo.settings).dependencies.evidence_context

    block, id_map = knowledge_context_with_outline(
        context, notebook.id, _kg_hits(object_ids),
        [_section("s1", "一节", [object_ids[-1]])],
        id_offset=0, budget_chars=200)

    assert len(block) <= 200
    assert list(id_map) == [f"k{index}" for index in range(1, len(id_map) + 1)]
    assert id_map["k1"]["object_id"] == object_ids[-1]   # 绑定的先渲染
    assert len(block.split("\n")) == len(id_map)


def test_without_bindings_the_kg_context_is_a_single_untouched_pass(repo):
    """关闭态(没有大纲、或绑定的全不是知识对象)必须逐字回到接入前:一次调用、
    同一份输出。多一次调用就是多一份预算账,输出会与单遍不同。"""
    notebook = _notebook(repo)
    object_ids = _claims(repo, notebook.id, 12)
    hits = _kg_hits(object_ids)
    inner = ReportEngine.from_repository(
        repo, repo.settings).dependencies.evidence_context
    calls: list[tuple] = []

    class _Spy:
        def knowledge_context(self, notebook_id, hits, *, id_offset=0,
                              budget_chars=None):
            calls.append((len(hits), id_offset, budget_chars))
            return inner.knowledge_context(notebook_id, hits,
                                           id_offset=id_offset,
                                           budget_chars=budget_chars)

    baseline = inner.knowledge_context(notebook.id, hits, id_offset=7,
                                       budget_chars=200)

    for sections in ([], [_section("s1", "一节")],
                     [_section("s1", "一节", ["el-不是知识对象"])]):
        calls.clear()
        assert knowledge_context_with_outline(
            _Spy(), notebook.id, hits, sections,
            id_offset=7, budget_chars=200) == baseline
        assert len(calls) == 1
        assert calls[0] == (len(hits), 7, 200)


# ------------------------------------------- 负向守卫:确认过的大纲不被回写

def test_the_confirmed_outline_json_is_never_written_back(repo, monkeypatch):
    """深挖发现的子结构只影响节内 `###`,`reports.outline_json` 逐字不变。

    这是这个特性最危险的坏法:用户确认的章节/必答主题绑定是不可替换、不可收窄
    的合同,一旦被模型自己整理的大纲覆盖,确认门就形同虚设。
    """
    notebook = _notebook(repo)
    llm = _SectionLLM()
    engine = _engine(repo, llm)
    monkeypatch.setattr(ReportEngine, "_build_corpus_map", lambda self, n, q: "MAP")
    monkeypatch.setattr(repo.retrieval, "federated_retrieve", lambda a, q: [])
    monkeypatch.setattr(
        engine, "_deep_dive",
        lambda nb_id, section, question, depth=None, on_step=None: ReasoningResult(
            outline=[_section("d1", "深挖发现的子话题"),
                     _section("d2", "另一个子话题")]),
    )
    report_id = repo.create_report(notebook.id, "Q", depth=16)
    confirmed = [{"title": "确认过的一节", "scope": "sc",
                  "sub_queries": ["版图设计要点"], "intent_ids": []}]
    repo.update_report(notebook.id, report_id, outline=confirmed,
                       status="outline_ready")

    engine.generate(notebook.id, report_id, "Q", depth=16)

    detail = repo.get_report(notebook.id, report_id)
    assert detail["status"] == "done"
    assert [s["title"] for s in detail["outline"]] == ["确认过的一节"]
    assert detail["outline"][0]["sub_queries"] == ["版图设计要点"]
    assert "深挖发现的子话题" not in json.dumps(detail["outline"], ensure_ascii=False)


# ------------------------------------------------------- 节 phase 大纲进度

def test_the_section_phase_shows_the_outline_progress(repo, monkeypatch):
    """深挖阶段的 phase 文案带大纲进度;没有大纲步时逐字回到「深挖」。

    进度只走既有的 2 秒节流持久化(这里用假时钟让节流窗过去,而不是绕开它)。
    """
    from app.services import report_engine as engine_module

    notebook = _notebook(repo)
    llm = _SectionLLM()
    engine = _engine(repo, llm)
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr(engine_module, "time",
                        SimpleNamespace(monotonic=lambda: float(next(ticks))))
    recorded: list[list[dict]] = []
    reports_port = engine.dependencies.reports
    original = reports_port.update_report

    def _spy(notebook_id, report_id, **kwargs):
        if kwargs.get("section_status") is not None:
            recorded.append(kwargs["section_status"])
        return original(notebook_id, report_id, **kwargs)

    monkeypatch.setattr(reports_port, "update_report", _spy)

    def _dive(nb_id, section, question, depth=None, on_step=None):
        on_step(TraceStep(step_type="retrieve", summary="检索"))
        if section["title"] == "有大纲的一节":
            on_step(TraceStep(step_type="outline", summary="更新大纲: 2 节",
                              detail={"sections": [{"id": "a"}, {"id": "b"}]}))
            on_step(TraceStep(step_type="reflect", summary="反思"))
        return ReasoningResult()

    monkeypatch.setattr(engine, "_deep_dive", _dive)
    monkeypatch.setattr(engine, "_draft_section",
                        lambda nb, section, q, result: {"title": section["title"],
                                                        "markdown": "x"})
    report_id = repo.create_report(notebook.id, "Q", depth=16)
    outline = [{"title": "有大纲的一节", "scope": "s", "sub_queries": ["q"]},
               {"title": "没大纲的一节", "scope": "s", "sub_queries": ["q"]}]

    engine._run_sections(notebook.id, report_id, outline, "Q", 16)

    phases = {row["phase"] for snapshot in recorded for row in snapshot}
    assert _OUTLINE_PROGRESS_PHASE in phases
    assert "深挖" in phases            # 没有大纲步的那一节文案不变
    # 大纲步之后的检索/反思步不得把文案退回「深挖」——那会让进度来回跳。
    with_outline = [row for snapshot in recorded for row in snapshot
                    if row["title"] == "有大纲的一节"]
    assert with_outline[-1]["phase"] in ("完成", _OUTLINE_PROGRESS_PHASE)
    assert any(row["phase"] == _OUTLINE_PROGRESS_PHASE and row["step"] == 1
               for row in with_outline)
    # 大纲计数是**按节**的:没有大纲步的那一节,它的每一条快照都必须逐字是「深挖」
    # (或非深挖阶段的文案),一个字都不能借到别节的计数。把按节下标的列表改成
    # 一个共享标量时,上面那几条集合断言全部照绿——只有这一条会红。
    without_outline = [row for snapshot in recorded for row in snapshot
                       if row["title"] == "没大纲的一节"]
    assert any(row["phase"] == "深挖" for row in without_outline), (
        "这一节根本没被快照到深挖阶段,下面的断言就没有约束力")
    assert all(row["phase"] == "深挖" for row in without_outline
               if row["phase"].startswith("深挖"))
