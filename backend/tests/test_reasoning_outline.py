"""逐步推理的大纲便签(PR-3 O1,设计文档 §3.1)。

覆盖:动作与门控(档位 × 开关四态)、schema/解析的有界夹取、绑定键的服务端校验、
章节全量替换/同 id 证据并集语义、每 run 预算、账目回喂、进展判定与
stale 熔断的交互、trace 形状,以及一条 e2e:先枚举来源目录、再分两轮建大纲并补上
空节的绑定。

按节合成(O2)与前端标签(O3)不在本文件范围内。
"""
from __future__ import annotations

import json
import re

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    OUTLINE_ACTION,
    CollectionEnumerationOutcome,
    OutlineSection,
    ReasoningRetriever,
    bind_outline_evidence,
    carry_outline_overflow,
    merge_outline_evidence,
    outline_binding_keys,
    outline_signature,
    outline_wiring_active,
    parse_outline_sections,
    _EnumChain,
    _MAX_OUTLINE_UPDATES,
    _OUTLINE_MAX_PENDING_EVIDENCE,
    _OUTLINE_EVIDENCE_KEY_CHARS,
    _OUTLINE_ID_CHARS,
    _OUTLINE_MAX_EVIDENCE,
    _OUTLINE_MAX_SECTIONS,
    _OUTLINE_NUDGE_MAX_ROUNDS,
    _OUTLINE_NUDGE_MIN_ITEMS,
    _OUTLINE_TITLE_CHARS,
    _outline_note,
    _outline_nudge_note,
)
from app.services.retrieval import RetrievedChunk, RetrievedElement
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


def _seed(repo, *, sources=("论文一",), elements=0):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
    ], [])
    with repo._write() as db:
        for index, title in enumerate(sources, start=1):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,summary,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"s{index}", notebook.id, title, "pdf", "extracted",
                 "extracted", f"{title}的摘要", NOW, NOW),
            )
        for index in range(elements):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{index:03d}", "s1", "formula", f"p{index}",
                 f"版图设计要点 公式 {index}", "{}", NOW),
            )
    repo.collection_catalog.invalidate()
    return notebook


class _SeqLLM:
    """plan 固定;reflect 按序返回(耗尽后默认 answer)。

    序列里的条目可以是 dict,也可以是 **接收本轮 prompt 正文的可调用对象** ——
    「模型只能引用它在候选里看到的 id」这条合同只有让脚本真的从 prompt 里把 id
    读出来才测得到(写死一个 id 的话,测的是我自己拼的字符串)。
    """

    configured = True

    def __init__(self, reflects, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计要点"}]}
        self._reflects = list(reflects)
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []
        self.schema_hints: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        self.schema_hints.append(schema_hint)
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        if self._reflects:
            reply = self._reflects.pop(0)
            if callable(reply):
                reply = reply(content)
            return json.dumps(reply)
        return json.dumps({"next_action": "answer", "sufficient": True})


ANSWER = {"next_action": "answer", "sufficient": True}


def _outline_action(sections, reason="整理大纲"):
    return {"next_action": OUTLINE_ACTION, "reason": reason,
            "outline": {"sections": sections}}


def _retriever(repo, llm, *, effort="exhaustive", fail_closed=False):
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(
        repo, repo.settings, fail_closed=fail_closed
    )
    return retriever, ask_retrieval_limits(effort)


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


def _skips(result):
    return {step.detail.get("reason"): step for step in _steps(result, "skip")}


def _shown_ids(prompt: str) -> list[str]:
    """模型在**候选区**里真正看到的 id。

    只扫 `Candidates so far:` 之后的部分:动作说明里那句「copied exactly as they
    appear in (id=...)」本身也长这个样子,连它一起抓的话,脚本"模型"会拿一个字面量
    `...` 去当绑定键——测的就不再是「模型只能引用它看到的候选」了。
    """
    body = prompt.split("Candidates so far:", 1)[-1]
    return re.findall(r"\(id=([^)]+)\)", body)


# ------------------------------------------------------------------ 门控四态

def test_outline_gate_needs_both_the_switch_and_the_exhaustive_effort():
    settings = Settings()
    exhaustive = ask_retrieval_limits("exhaustive")

    assert outline_wiring_active(settings, exhaustive) is True
    # 低档位:开关开着也不提供(用户没选「穷尽」就没同意付按节合成的钱)。
    for effort in ("overview", "standard", "deep", "thorough"):
        assert outline_wiring_active(
            settings, ask_retrieval_limits(effort)) is False
    # 没有档位的调用方(深度报告逐节深挖、knowhow 补全)一律不提供。
    assert outline_wiring_active(settings, None) is False

    settings.reasoning_outline_enabled = False
    assert outline_wiring_active(settings, exhaustive) is False


def test_the_gate_survives_a_budget_override():
    """档位闸读的是 limits 自己的身份,不是从预算数字反推。

    反推的话,任何一次 `replace(limits, enum_rows_per_run=...)`(测试与未来的调用方
    都会这么干)都会静默改变「这是哪一档」的答案。「每一行都带着自己的档位 id」那条
    守卫住在真源旁边(`test_query_intent.py` 的档位表用例),这里只钉本特性依赖的
    那半:改预算不换档。
    """
    from dataclasses import replace

    tweaked = replace(ask_retrieval_limits("exhaustive"), enum_rows_per_run=1)
    assert outline_wiring_active(Settings(), tweaked) is True


def test_prompt_and_schema_offer_the_action_only_when_gated_on():
    from app.services.prompts import (
        REFLECT_SCHEMA_HINT, reflect_prompt, reflect_schema_hint,
    )

    off = reflect_prompt("q", "s")
    on = reflect_prompt("q", "s", outline=True)

    assert OUTLINE_ACTION not in off
    assert OUTLINE_ACTION in on
    # 适用面必须写清楚(不写的话模型要么不用,要么对单点事实题也建大纲)。
    for phrase in ("survey", "inventory", "per-document treatment",
                   "comparison of several subjects"):
        assert phrase in on, phrase
    # 章节结构全量替换,证据按稳定 section id 做 citation-persistent union。
    assert "REPLACES the section structure" in on
    assert "evidence is UNIONED" in on
    assert "remove_evidence" in on
    assert "outline scratchpad in the context below" in on
    # 空节是特性不是错误。
    assert "A section with NO evidence is not a failure" in on
    # 不适用面。
    assert "Do NOT open an outline for a single-fact question" in on

    schema_on = reflect_schema_hint(outline=True)
    assert ('"outline":{"sections":[{"id":"","title":"","parent":"",'
            '"evidence":[""],"remove_evidence":[""]}]}' in schema_on)
    assert f"|{OUTLINE_ACTION}" in schema_on
    # 关闭态逐字回到接入前:模块级常量(枚举工具的冻结基线钉的就是它)不受影响。
    assert "outline" not in reflect_schema_hint()
    assert "outline" not in REFLECT_SCHEMA_HINT
    assert reflect_schema_hint() == REFLECT_SCHEMA_HINT


def test_outline_and_enumeration_gates_are_independent():
    """两把闸各管各的:枚举在每一档都有,大纲只在穷尽档。"""
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.prompts import reflect_prompt, reflect_schema_hint

    enum_only = reflect_prompt("q", "s", element_kinds=ENUMERABLE_ELEMENT_KINDS,
                               object_types=ENUMERABLE_KG_OBJECT_TYPES)
    assert "enumerate_elements" in enum_only and OUTLINE_ACTION not in enum_only

    outline_only = reflect_prompt("q", "s", outline=True)
    assert OUTLINE_ACTION in outline_only
    assert "enumerate_elements" not in outline_only

    both = reflect_prompt("q", "s", element_kinds=ENUMERABLE_ELEMENT_KINDS,
                          object_types=ENUMERABLE_KG_OBJECT_TYPES, outline=True)
    assert "enumerate_elements" in both and OUTLINE_ACTION in both
    # 目录是大纲的天然种子——这句只在两者同时在场时才有意义,所以只在那时出现。
    assert "the natural seed" in both
    assert "the natural seed" not in outline_only

    schema = reflect_schema_hint(ENUMERABLE_ELEMENT_KINDS,
                                 ENUMERABLE_KG_OBJECT_TYPES, outline=True)
    assert '"enumerate":{' in schema and '"outline":{' in schema


@pytest.mark.parametrize("effort,enabled", [
    ("thorough", True), ("thorough", False), ("exhaustive", False),
])
def test_run_keeps_the_previous_behavior_off_gate(repo, effort, enabled):
    """三个关闭态(低档+开、低档+关、穷尽+关)必须与接入前逐字一致:动作不进
    prompt/schema/白名单,模型硬吐它也只能按既有未知动作合同退成 answer。"""
    notebook = _seed(repo)
    repo.settings.reasoning_outline_enabled = enabled
    llm = _SeqLLM([_outline_action([{"id": "s1", "title": "引言"}])])
    retriever, limits = _retriever(repo, llm, effort=effort)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    assert result.outline == []
    assert _steps(result, "outline") == []
    assert OUTLINE_ACTION not in llm.reflect_prompts[0]
    assert all(OUTLINE_ACTION not in hint for hint in llm.schema_hints[1:])
    assert result.trace[-1].step_type == "answer"


# --------------------------------------------------------------- 解析与夹取

def test_the_documented_bounds_are_the_ones_in_the_code():
    """契约里的数字与代码里的常量对上。

    单独钉一条,是因为下面那些夹取用例都拿常量本身当期望值——只改常量的话它们
    会跟着一起动、全绿放行(「替换打空」)。数字必须在某一处以字面量出现。
    """
    assert (_OUTLINE_MAX_SECTIONS, _OUTLINE_TITLE_CHARS, _OUTLINE_MAX_EVIDENCE,
            _MAX_OUTLINE_UPDATES) == (12, 60, 8, 6)
    assert (_OUTLINE_ID_CHARS, _OUTLINE_EVIDENCE_KEY_CHARS) == (32, 48)


def test_parse_clamps_every_documented_bound():
    raw = {"sections": [
        {"id": f"s{i}", "title": f"节{i}" * 40,
         "evidence": [f"k{i}-{j}" for j in range(12)]}
        for i in range(20)
    ]}

    sections = parse_outline_sections(raw)

    assert len(sections) == 12
    for section in sections:
        assert len(section.title) == 60
        assert len(section.evidence_keys) == 8
    # 截断保留的是**前** N 个,不是随机子集(同一份输入永远得到同一份大纲)。
    assert [s.id for s in sections] == [f"s{i}" for i in range(12)]
    assert sections[0].evidence_keys == [f"k0-{j}" for j in range(8)]


def test_parse_clamps_and_deduplicates_explicit_evidence_removals():
    section = parse_outline_sections({"sections": [{
        "id": "s1", "title": "一节",
        "remove_evidence": ["k1", None, "k1", *[f"k{n}" for n in range(2, 12)]],
    }]})[0]

    assert section.remove_evidence_keys == [f"k{n}" for n in range(1, 9)]


def test_parse_clamps_long_ids_and_evidence_keys():
    raw = {"sections": [{
        "id": "x" * 200, "title": "引言", "evidence": ["k" * 200],
    }]}

    section = parse_outline_sections(raw)[0]

    assert len(section.id) == 32
    assert len(section.evidence_keys[0]) == 48
    # 真实代理 id 是 `prefix-` + 32 位 uuid hex ≈ 35 字符:证据 key 的上界必须容得
    # 下它,否则每一个合法绑定都会被截短成一个对不上的键。
    assert _OUTLINE_EVIDENCE_KEY_CHARS >= 40


def test_parse_drops_malformed_entries_but_keeps_the_rest():
    """fail-open 逐项丢弃(与 enumerate 非白名单值同形),不是整份拒绝。"""
    sections = parse_outline_sections({"sections": [
        "不是对象",
        {"title": ""},                       # 无标题 = 不是一节
        {"title": "  引言  ", "evidence": "不是列表"},
        {"id": "b", "title": "方法", "evidence": ["k1", None, "k1", 7, "k2"]},
    ]})

    assert [s.title for s in sections] == ["引言", "方法"]
    assert sections[0].evidence_keys == []          # 非列表 evidence → 空
    assert sections[1].evidence_keys == ["k1", "k2"]  # 非字符串丢弃 + 去重


def test_parse_rejects_non_list_payloads():
    for raw in (None, "outline", 7, {"sections": "x"}, {}):
        assert parse_outline_sections(raw) == []


def test_parse_assigns_deterministic_ids_when_missing_or_duplicated():
    sections = parse_outline_sections({"sections": [
        {"title": "一"}, {"title": "二"}, {"id": "s2", "title": "三"},
    ]})

    ids = [s.id for s in sections]
    assert ids == ["s1", "s2", "s2-2"]
    assert len(set(ids)) == 3
    # 同一份输入必须给出同一份 id:模型下一轮要按这些 id 原样重提。
    assert ids == [s.id for s in parse_outline_sections({"sections": [
        {"title": "一"}, {"title": "二"}, {"id": "s2", "title": "三"},
    ]})]


def test_long_colliding_ids_terminate_and_stay_unique():
    """去重必须是**结构性**有界的,不是「大概会撞上一个空位」。

    这个函数跑在 Ask 的 worker 线程里:循环体内没有取消检查,`except Exception` 也
    接不到一个永不返回的函数。原来的 `while True` + `f"{base}-{n}"[:32]` 在两种真实
    输入下 100% CPU 死循环,只能重启后端:

    * 两个 33 字符、前 32 位相同的 id —— 模型给对比大纲起 `…-approach-a` /
      `…-approach-b` 这类 slug 是默认写法,截断后完全相同;
    * 三个 31 字符的同名 id —— `f"{base}-2"[:32]` 恰好把数字截掉,每个后缀都产出
      同一个「base + 尾随连字符」。
    """
    prefix32 = "outline-section-approach-alpha-a"          # 32 字符
    assert len(prefix32) == 32
    slugs = parse_outline_sections({"sections": [
        {"id": prefix32 + "a", "title": "方案甲"},
        {"id": prefix32 + "b", "title": "方案乙"},
    ]})

    ids = [s.id for s in slugs]
    assert len(set(ids)) == 2, ids
    assert all(len(i) <= 32 for i in ids)

    prefix31 = "outline-section-approach-alph-a"           # 31 字符
    assert len(prefix31) == 31
    same = parse_outline_sections({"sections": [
        {"id": prefix31, "title": "甲"},
        {"id": prefix31, "title": "乙"},
        {"id": prefix31, "title": "丙"},
    ]})

    ids = [s.id for s in same]
    assert len(set(ids)) == 3, ids
    assert all(len(i) <= 32 for i in ids)
    # 后缀必须**真的留在** id 里:那正是「拼之前先裁 base」买到的东西。少了那一步,
    # 长 base 的每个候选都截回同一串,去重只能靠位置 id 兜底——两个同名节于是拿到
    # 两个与 base 毫无关系的 id,模型下一轮认不出哪个是哪个。
    assert ids[1].endswith("-2") and ids[2].endswith("-3"), ids
    assert all(i.startswith(prefix31[:28]) for i in ids), ids
    # 确定性:同一份输入必须给出同一份 id(模型下一轮要按它们原样重提)。
    assert ids == [s.id for s in parse_outline_sections({"sections": [
        {"id": prefix31, "title": "甲"},
        {"id": prefix31, "title": "乙"},
        {"id": prefix31, "title": "丙"},
    ]})]


def test_id_dedup_falls_back_to_position_ids():
    """后缀段穷尽后落回位置 id;鸽笼原理保证 `s1..s{len(used)+1}` 必有空位。"""
    from app.services.reasoning_retrieval import _unique_outline_id

    used = {"a"} | {f"a-{n}" for n in range(2, _OUTLINE_MAX_SECTIONS + 2)}

    got = _unique_outline_id("a", used)

    assert got == "s1"
    assert got not in used


def test_parse_keeps_two_layers_and_drops_impossible_parents():
    sections = parse_outline_sections({"sections": [
        {"id": "a", "title": "顶层"},
        {"id": "b", "title": "子节", "parent": "a"},
        {"id": "c", "title": "孙节", "parent": "b"},     # 第三层 → 提为顶层
        {"id": "d", "title": "野指针", "parent": "zzz"},  # 不存在 → 顶层
        {"id": "e", "title": "自环", "parent": "e"},      # 指向自己 → 顶层
    ]})

    parents = {s.id: s.parent for s in sections}
    assert parents == {"a": "", "b": "a", "c": "", "d": "", "e": ""}


def test_parse_resolves_a_forward_parent_reference():
    """子节先于父节出现也算合法:两层判定必须在收齐所有节之后做。"""
    sections = parse_outline_sections({"sections": [
        {"id": "b", "title": "子节", "parent": "a"},
        {"id": "a", "title": "父节"},
    ]})

    assert {s.id: s.parent for s in sections} == {"b": "a", "a": ""}


def test_parse_breaks_a_parent_cycle():
    sections = parse_outline_sections({"sections": [
        {"id": "a", "title": "甲", "parent": "b"},
        {"id": "b", "title": "乙", "parent": "a"},
    ]})

    assert [s.parent for s in sections] == ["", ""]


# ----------------------------------------------------------- 绑定键服务端校验

def test_binding_keys_are_candidate_pool_intersected_with_visible_or_retained():
    """合法集 = 候选池 ∩ (曾展示 ∪ 已持有),且必须 **⊆ 模型可见或服务端状态**。

    枚举清单的条目 id 刻意**不在**内(v1):枚举账目按合同只回覆盖计数、不回条目
    id,模型根本拿不到那些 id,放进合法集只是一片没人能踩到的死面,同时放宽了
    「只能引用服务端给过你的东西」这条校验。让条目 id 可见时再连同可见性一起加回。
    来源 id 同样不在内:一份文档不是一条证据,绑上它给 O2 的按节合成产不出任何
    上下文切片。
    """
    import inspect

    keys = outline_binding_keys(
        {"ko-1": object(), "ko-hidden": object()},
        [RetrievedElement("el-1", "s1", "论文一", "p1", "formula", "文本")],
        [RetrievedChunk("ch-1", "s1", "论文一", "1", "文本")],
        {"ko-1", "el-1", "猜的"},
        {"ch-1"},
    )

    assert keys == {"ko-1", "el-1", "ch-1"}
    assert "ko-hidden" not in keys
    # 形参也钉住:悄悄把 enumerations 接回来(而账目仍不回条目 id)会重新打开那片
    # 死面,而只看返回值的用例抓不到。
    assert list(inspect.signature(outline_binding_keys).parameters) == [
        "collected", "elements", "chunks", "ever_shown_keys", "retained_keys"]


def test_evidence_key_width_fits_the_real_surrogate_ids():
    """尺子守卫:真实代理 id 必须装得下,将来变长要响亮而不是静默截断。

    绑定键被截短一个字符就永远匹配不上合法集——那是一条静默失效的路径(模型给的
    每个绑定都被判非法、每节都变空节),没有任何现象指向这个常量。
    """
    from app.services.repository_facade import _new_id

    for prefix in ("ko", "el", "ch", "chunk"):
        generated = _new_id(prefix)
        assert len(generated) < _OUTLINE_EVIDENCE_KEY_CHARS, generated
        # 也要真的活过解析层(那里才是截断发生的地方)。
        section = parse_outline_sections({"sections": [
            {"id": "a", "title": "引言", "evidence": [generated]},
        ]})[0]
        assert section.evidence_keys == [generated]


def test_illegal_binding_keys_are_dropped_and_empty_sections_survive():
    sections = [
        OutlineSection(id="a", title="引言", evidence_keys=["ko-1", "编的", "ch-1"]),
        OutlineSection(id="b", title="方法", evidence_keys=["也是编的"]),
    ]

    bound, dropped = bind_outline_evidence(sections, {"ko-1", "ch-1"})

    assert [s.evidence_keys for s in bound] == [["ko-1", "ch-1"], []]
    assert dropped == 2
    # 全非法的节保留为**空节**:它不是错误,它是下一步的检索方向。
    assert [s.title for s in bound] == ["引言", "方法"]
    # 入参不被就地改(run 会把返回值当新状态留存,共享对象会污染上一轮账目)。
    assert sections[1].evidence_keys == ["也是编的"]


def test_binding_preserves_order_and_section_shape():
    sections = [OutlineSection(id="a", title="引言", parent="p",
                               evidence_keys=["k2", "k1"])]

    bound, dropped = bind_outline_evidence(sections, {"k1", "k2"})

    assert bound[0].evidence_keys == ["k2", "k1"]
    assert (bound[0].id, bound[0].title, bound[0].parent) == ("a", "引言", "p")
    assert dropped == 0


def test_same_id_evidence_is_union_persistent_when_the_model_omits_it():
    previous = [OutlineSection(
        id="a", title="旧标题", evidence_keys=["k1", "k2"])]
    submitted = [OutlineSection(
        id="a", title="新标题", evidence_keys=["k3"])]

    merged, dropped, overflow, removed = merge_outline_evidence(
        previous, submitted, {"k1", "k2", "k3"})

    assert [(s.id, s.title, s.evidence_keys) for s in merged] == [
        ("a", "新标题", ["k1", "k2", "k3"])]
    assert dropped == 0 and overflow == {} and removed == 0


def test_explicit_removal_can_replace_old_evidence_without_resurrecting_it():
    previous = [OutlineSection(
        id="a", title="一节", evidence_keys=["k1", "k2"])]
    submitted = [OutlineSection(
        id="a", title="一节", evidence_keys=["k1", "k3"],
        remove_evidence_keys=["k1"])]

    merged, dropped, overflow, removed = merge_outline_evidence(
        previous, submitted, {"k1", "k2", "k3"})

    # 同一提交里 remove 优先于 evidence,避免字段顺序决定结果。
    assert merged[0].evidence_keys == ["k2", "k3"]
    assert dropped == 0 and overflow == {} and removed == 1


def test_union_overflow_keeps_old_keys_and_names_rejected_new_keys():
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    previous = [OutlineSection(
        id="a", title="一节", evidence_keys=old_keys)]
    submitted = [OutlineSection(
        id="a", title="一节", evidence_keys=["new-1", "new-2"])]

    merged, dropped, overflow, removed = merge_outline_evidence(
        previous, submitted, set(old_keys) | {"new-1", "new-2"})

    assert merged[0].evidence_keys == old_keys
    assert overflow == {"a": ["new-1", "new-2"]}
    assert dropped == 0 and removed == 0


def test_pending_overflow_persists_until_bound_or_explicitly_abandoned():
    merged = [OutlineSection(id="a", title="一节", evidence_keys=["old-1"])]
    submitted = [OutlineSection(
        id="a", title="一节", evidence_keys=[], remove_evidence_keys=[])]

    assert carry_outline_overflow(
        {"a": ["new-1"]}, submitted, merged, {},
    ) == {"a": ["new-1"]}

    submitted[0].remove_evidence_keys = ["new-1"]
    assert carry_outline_overflow(
        {"a": ["new-1"]}, submitted, merged, {},
    ) == {}


def test_persistent_pending_is_complete_in_state_but_windowed_in_prompt():
    submitted = [OutlineSection(id="a", title="一节")]
    merged = [OutlineSection(id="a", title="一节")]
    pending = {}
    for batch in range(_MAX_OUTLINE_UPDATES + 1):
        current = {
            "a": [
                f"pending-{batch}-{index}"
                for index in range(_OUTLINE_MAX_EVIDENCE)
            ]
        }
        pending = carry_outline_overflow(pending, submitted, merged, current)

    # 文档写死的是 56；同时钉常量值，避免实现和动态断言一起漂移仍全绿。
    assert _OUTLINE_MAX_PENDING_EVIDENCE == 56
    assert len(pending["a"]) == 56
    note = _outline_note(merged, 1, pending)
    for index in range(_OUTLINE_MAX_EVIDENCE):
        assert f"pending-0-{index}" in note
    assert "pending-1-0" not in note
    assert f"另有 {_OUTLINE_MAX_PENDING_EVIDENCE - _OUTLINE_MAX_EVIDENCE} 条" in note


def test_pending_note_does_not_offer_an_update_after_repair_is_consumed():
    note = _outline_note(
        [OutlineSection(id="a", title="一节")],
        0,
        {"a": ["pending-1"]},
        overflow_repair_available=False,
    )

    assert "不要再提交 update_outline" in note
    assert "下一次在该节 remove_evidence" not in note
    assert "未接纳 key 会在收尾轨迹中披露" in note


# --------------------------------------------------------------- 动作与 trace

def test_update_outline_records_a_bounded_trace_step(repo):
    notebook = _seed(repo)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "研究背景",
             "evidence": [_shown_ids(prompt)[0], "编的键"]},
            {"id": "b", "title": "待补的一节"},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下这个库", "", limits=limits)

    step = _steps(result, "outline")[0]
    assert step.summary == "更新大纲: 2 节(1 节待补证据)"
    assert [s["id"] for s in step.detail["sections"]] == ["a", "b"]
    assert step.detail["sections"][0]["evidence"] == [_shown_ids(
        llm.reflect_prompts[0])[0]]
    assert step.detail["empty_sections"] == ["待补的一节"]
    assert step.detail["replaced"] == 0
    assert step.detail["dropped_evidence"] == 1     # 编的键被静默丢掉
    assert step.detail["changed"] is True
    # 终态大纲随结果带出(O2 消费它),空节保留。
    assert [(s.id, s.title, s.evidence_keys) for s in result.outline] == [
        ("a", "研究背景", [_shown_ids(llm.reflect_prompts[0])[0]]),
        ("b", "待补的一节", []),
    ]


def test_the_action_replaces_the_whole_section_structure(repo):
    notebook = _seed(repo)
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一"}, {"id": "b", "title": "二"}]),
        _outline_action([{"id": "c", "title": "三"}]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert [s.id for s in result.outline] == ["c"]
    assert _steps(result, "outline")[1].detail["replaced"] == 2


def test_the_action_unions_same_id_evidence_and_feeds_back_overflow(repo, monkeypatch):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_keys = ["new-1", "new-2"]
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + new_keys),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        _outline_action([{"id": "a", "title": "一节", "evidence": new_keys}]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys
    second = _steps(result, "outline")[1]
    assert second.detail["overflow_evidence"] == {"a": new_keys}
    assert second.detail["removed_evidence"] == 0
    fed = llm.reflect_prompts[2]
    assert "未接纳新证据" in fed
    assert "new-1、new-2" in fed
    assert "remove_evidence" in fed


def test_sufficient_overflow_gets_one_repair_round(repo, monkeypatch):
    """最终大纲与 sufficient 同轮提交时,overflow 不能被短路吞掉。"""
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_keys = ["new-1", "new-2"]
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + new_keys),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        dict(_outline_action([
            {"id": "a", "title": "一节", "evidence": new_keys},
        ]), sufficient=True),
        dict(_outline_action([{
            "id": "a", "title": "一节",
            "evidence": new_keys,
            "remove_evidence": old_keys[:2],
        }]), sufficient=True),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys[2:] + new_keys
    assert len(llm.reflect_prompts) == 3
    assert "未接纳新证据" in llm.reflect_prompts[2]
    assert "终态溢出纠错轮" in llm.reflect_prompts[2]
    assert "不得改结构或执行检索" in llm.reflect_prompts[2]
    assert "outline_evidence_overflow_unresolved" not in _skips(result)


def test_repair_that_omits_the_pending_key_keeps_it_visible_at_run_end(
    repo, monkeypatch,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        dict(_outline_action([
            {"id": "a", "title": "一节", "evidence": [new_key]},
        ]), sufficient=True),
        # 腾出了位置,但转录时漏掉 new_key:服务端不能把 pending 当成已解决。
        _outline_action([{
            "id": "a", "title": "一节", "remove_evidence": [old_keys[0]],
        }]),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys[1:]
    unresolved = _skips(result)["outline_evidence_overflow_unresolved"]
    assert unresolved.detail["overflow_evidence"] == {"a": [new_key]}


def test_sufficient_overflow_repair_round_cannot_execute_retrieval(repo, monkeypatch):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        dict(_outline_action([
            {"id": "a", "title": "一节", "evidence": [new_key]},
        ]), sufficient=True),
        {"next_action": "search_elements", "elements_query": "不应执行"},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert "outline_overflow_repair_declined" in _skips(result)
    assert all(
        step.detail.get("query") != "不应执行"
        for step in _steps(result, "retrieve")
    )
    assert "outline_evidence_overflow_unresolved" in _skips(result)


@pytest.mark.parametrize("terminal_boundary", ["sufficient", "stale"])
def test_terminal_overflow_repair_rejects_section_structure_changes(
    repo, monkeypatch, terminal_boundary,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = (
        2 if terminal_boundary == "stale" else 99
    )
    overflow = _outline_action([
        {"id": "a", "title": "原结构", "evidence": [new_key]},
    ])
    if terminal_boundary == "sufficient":
        overflow = dict(overflow, sufficient=True)
    llm = _SeqLLM([
        _outline_action([
            {"id": "a", "title": "原结构", "evidence": old_keys},
        ]),
        overflow,
        _outline_action([{
            "id": "a", "title": "偷偷改结构", "evidence": [new_key],
            "remove_evidence": [old_keys[0]],
        }]),
    ])
    retriever, limits = _retriever(repo, llm)
    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].title == "原结构"
    assert result.outline[0].evidence_keys == old_keys
    assert "outline_repair_structure" in _skips(result)
    assert "outline_evidence_overflow_unresolved" in _skips(result)


def test_pending_overflow_does_not_freeze_an_ordinary_outline_update(
    repo, monkeypatch,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "原结构", "evidence": old_keys}]),
        _outline_action([{"id": "a", "title": "原结构", "evidence": [new_key]}]),
        # 普通额度仍在:同一载荷既可重排结构,也必须保住合法换键与新节绑定。
        _outline_action([{
            "id": "a", "title": "重排后", "evidence": [new_key],
            "remove_evidence": [old_keys[0]],
        }, {
            "id": "b", "title": "新增一节", "evidence": [old_keys[1]],
        }]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert [(section.id, section.title) for section in result.outline] == [
        ("a", "重排后"), ("b", "新增一节")]
    assert result.outline[0].evidence_keys == old_keys[1:] + [new_key]
    assert result.outline[1].evidence_keys == [old_keys[1]]
    assert _steps(result, "outline")[-1].detail["overflow_repair"] is False
    assert "outline_repair_structure" not in _skips(result)
    assert "outline_evidence_overflow_unresolved" not in _skips(result)


def test_max_steps_never_adds_an_overflow_repair_round(
    repo, monkeypatch,
):
    """步骤预算是绝对上限；末轮 overflow 只披露，不再花第 51 次 reflect。"""
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        _outline_action([{"id": "a", "title": "一节", "evidence": [new_key]}]),
        {"next_action": "search_elements", "elements_query": "不应执行"},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(
        notebook.id, "综述", "", limits=limits, max_steps=2,
    )

    assert len(llm.reflect_prompts) == 2
    assert "outline_overflow_repair_declined" not in _skips(result)
    assert "outline_repair_structure" not in _skips(result)
    assert "outline_evidence_overflow_unresolved" in _skips(result)
    assert all(
        step.detail.get("query") != "不应执行"
        for step in _steps(result, "retrieve")
    )


def test_stale_breaker_yields_to_one_overflow_repair_round(repo, monkeypatch):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 2
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一节", "evidence": old_keys}]),
        _outline_action([{"id": "a", "title": "一节", "evidence": [new_key]}]),
        _outline_action([{
            "id": "a", "title": "一节", "evidence": [new_key],
            "remove_evidence": [old_keys[0]],
        }]),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys[1:] + [new_key]
    assert len(llm.reflect_prompts) == 3
    assert _steps(result, "outline")[-1].detail["overflow_repair"] is True
    reasons = [step.detail.get("reason") for step in result.trace]
    assert "stale_circuit_breaker" in reasons
    breaker_index = reasons.index("stale_circuit_breaker")
    repair_index = next(
        index for index, step in enumerate(result.trace)
        if step.step_type == "outline" and step.detail.get("overflow_repair")
    )
    assert breaker_index < repair_index


def test_sixth_update_overflow_allows_one_same_structure_key_swap(
    repo, monkeypatch,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    same = {"id": "a", "title": "一节", "evidence": old_keys}
    llm = _SeqLLM(
        [_outline_action([same]) for _ in range(_MAX_OUTLINE_UPDATES - 1)]
        + [
            _outline_action([
                {"id": "a", "title": "一节", "evidence": [new_key]},
            ]),
            _outline_action([{
                "id": "a", "title": "一节", "evidence": [new_key],
                "remove_evidence": [old_keys[0]],
            }]),
            ANSWER,
        ]
    )
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys[1:] + [new_key]
    assert len(_steps(result, "outline")) == _MAX_OUTLINE_UPDATES + 1
    assert _steps(result, "outline")[-1].detail["overflow_repair"] is True
    assert "outline_budget" not in _skips(result)


def test_sixth_update_repair_is_consumed_even_when_structure_is_rejected(
    repo, monkeypatch,
):
    notebook = _seed(repo)
    old_keys = [f"old-{n}" for n in range(_OUTLINE_MAX_EVIDENCE)]
    new_key = "new-1"
    monkeypatch.setattr(
        "app.services.reasoning_retrieval.outline_binding_keys",
        lambda *_args: set(old_keys + [new_key]),
    )
    repo.settings.reasoning_stale_limit = 99
    same = {"id": "a", "title": "一节", "evidence": old_keys}
    llm = _SeqLLM(
        [_outline_action([same]) for _ in range(_MAX_OUTLINE_UPDATES - 1)]
        + [
            _outline_action([
                {"id": "a", "title": "一节", "evidence": [new_key]},
            ]),
            # 第一次专用提交改了标题:拒绝,但机会已经消费。
            _outline_action([{
                "id": "a", "title": "改标题", "evidence": [new_key],
                "remove_evidence": [old_keys[0]],
            }]),
            # 第二次即使结构正确也不能再使用同一份专用资格。
            _outline_action([{
                "id": "a", "title": "一节", "evidence": [new_key],
                "remove_evidence": [old_keys[0]],
            }]),
            ANSWER,
        ]
    )
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert result.outline[0].evidence_keys == old_keys
    assert "outline_repair_structure" in _skips(result)
    assert "outline_budget" in _skips(result)
    assert "outline_evidence_overflow_unresolved" in _skips(result)


def test_outline_updates_are_capped_per_run(repo):
    notebook = _seed(repo)
    # 大纲对 stale 中性(纯整理会推进熔断,见下面那条用例),所以这里把熔断关掉,
    # 单独测次数上限本身——两个机制各测各的,免得一条用例同时依赖两个阈值。
    repo.settings.reasoning_stale_limit = 99
    llm = _SeqLLM([
        _outline_action([{"id": f"s{i}", "title": f"第{i}节"}])
        for i in range(_MAX_OUTLINE_UPDATES + 1)
    ] + [ANSWER])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert len(_steps(result, "outline")) == _MAX_OUTLINE_UPDATES
    skip = _skips(result)["outline_budget"]
    assert str(_MAX_OUTLINE_UPDATES) in skip.summary
    # 教学式措辞:告诉它接下来该做什么,而不只是「不行」。
    assert "补齐空节" in skip.summary and "直接作答" in skip.summary
    assert skip.detail["updates"] == _MAX_OUTLINE_UPDATES
    # 超限的那次不改变大纲(上一份仍是终态)。
    assert [s.id for s in result.outline] == [
        f"s{_MAX_OUTLINE_UPDATES - 1}"]


def test_an_empty_submission_is_skipped_not_a_wipe(repo):
    """畸形/空提交不得把已经建好的大纲清空——那是最坏的一种「fail-open」。"""
    notebook = _seed(repo)
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "引言"}]),
        _outline_action([{"title": ""}, "垃圾"]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert [s.id for s in result.outline] == ["a"]
    assert len(_steps(result, "outline")) == 1
    skip = _skips(result)["outline_empty"]
    assert "整份大纲" in skip.summary and "标题" in skip.summary


# ------------------------------------------------------------------ 账目回喂

def test_the_scratchpad_is_fed_back_with_enough_to_resubmit(repo):
    """账目必须携带**整份大纲**,不只是节数与空节名。

    reflect 的 prompt 没有对话历史,章节结构仍是全量替换:模型手上唯一一份
    「我上次交了什么」就是这段回喂。证据有服务端 union 保底,结构仍要完整重提。
    """
    notebook = _seed(repo)
    llm = _SeqLLM([
        lambda prompt: _outline_action([
            {"id": "a", "title": "研究背景",
             "evidence": [_shown_ids(prompt)[0]]},
            {"id": "b", "title": "尚缺的方法", "parent": "a"},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits)

    fed = llm.reflect_prompts[1]
    bound_id = _shown_ids(llm.reflect_prompts[0])[0]
    assert "本轮大纲便签" in fed
    assert f"a「研究背景」 证据: {bound_id}" in fed          # id 原样可重提
    assert "b「尚缺的方法」(隶属 a) 证据: (无)" in fed        # 层级也带上
    assert "无绑定证据的节: 「尚缺的方法」" in fed
    assert "这些节就是下一步该定向检索的方向" in fed
    assert "整体替换章节结构" in fed
    assert "遗漏不会删除旧证据" in fed and "remove_evidence" in fed
    # 第一轮还没有大纲,那一段不该出现(空账目不占字符)。
    assert "本轮大纲便签" not in llm.reflect_prompts[0]


def test_the_scratchpad_note_is_constant_bounded():
    """最坏情况是常数级:12 节 × (32 id + 60 标题 + 32 parent + 8 × 48 key)。

    这份账目比其他几份都大,因为它同时是大纲的记忆。所以它的上界必须由结构夹死、
    与语料规模无关——这里直接按上界构造一份并量它。
    """
    worst = [
        OutlineSection(
            id=f"s{index}" + "i" * (_OUTLINE_ID_CHARS - len(f"s{index}")),
            title="标" * _OUTLINE_TITLE_CHARS,
            parent="p" * _OUTLINE_ID_CHARS,
            evidence_keys=[f"{n}" + "k" * (_OUTLINE_EVIDENCE_KEY_CHARS - 1)
                           for n in range(_OUTLINE_MAX_EVIDENCE)],
        )
        for index in range(_OUTLINE_MAX_SECTIONS)
    ]
    overflow = {
        section.id: [
            f"{n}" + "x" * (_OUTLINE_EVIDENCE_KEY_CHARS - 1)
            for n in range(_OUTLINE_MAX_EVIDENCE)
        ]
        for section in worst
    }

    # 两种措辞(还有额度 / 已定稿)都要量:上界不能只在其中一支成立。
    assert len(_outline_note(worst, _MAX_OUTLINE_UPDATES)) < 8000
    assert len(_outline_note(worst, 0)) < 8000
    # union 溢出是第二份同形有界列表;不能因为模型重复提交而无限累积。
    assert len(_outline_note(worst, _MAX_OUTLINE_UPDATES, overflow)) < 14000
    assert _outline_note([], _MAX_OUTLINE_UPDATES) == ""


# ------------------------------------------------------- 进展判定 / stale 交互

def test_outline_revisions_are_stale_neutral(repo):
    """大纲变化**不**算进展:它不带来任何新证据,而 stale 熔断数的正是「还在不在
    往前推进」。

    把「大纲变了」算成进展的那一版有一个实测可复现的洞:两份大纲交替提交,每轮都
    「有变化」、每轮都把 stale 清零,空转上限从 3 轮抬到 19 轮。这里用的正是那个
    交替脚本——纯整理必须在 stale_limit 轮内熔断。
    """
    notebook = _seed(repo)
    a = [{"id": "a", "title": "一"}]
    b = [{"id": "b", "title": "二"}]
    llm = _SeqLLM([_outline_action(a if i % 2 == 0 else b)
                   for i in range(12)] + [ANSWER])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    reflects = _steps(result, "reflect")
    # 每一轮都「有变化」,但每一轮都不是进展。
    assert all(step.detail["changed"] for step in _steps(result, "outline"))
    assert reflects[1].detail["no_progress"] is True
    assert "stale_circuit_breaker" in _skips(result)
    assert len(_steps(result, "outline")) <= repo.settings.reasoning_stale_limit
    # 也就绝不会一路烧到整理次数上限。
    assert len(_steps(result, "outline")) < _MAX_OUTLINE_UPDATES


def test_a_retrieval_step_between_outline_updates_keeps_the_run_alive(repo):
    """正当流程不受影响:绑定轮本来就伴随检索动作,那些动作自己会重置 stale。"""
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "一"}]),
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        _outline_action([{"id": "a", "title": "一"}, {"id": "b", "title": "二"}]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert "stale_circuit_breaker" not in _skips(result)
    assert len(_steps(result, "outline")) == 2
    assert [s.id for s in result.outline] == ["a", "b"]


def test_resubmitting_the_identical_outline_is_recorded_as_unchanged(repo):
    notebook = _seed(repo)
    same = [{"id": "a", "title": "一"}]
    llm = _SeqLLM([_outline_action(same) for _ in range(3)] + [ANSWER])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    steps = _steps(result, "outline")
    assert steps[0].detail["changed"] is True
    assert steps[1].detail["changed"] is False


def test_the_scratchpad_note_tracks_the_remaining_allowance(repo):
    """便签必须描述**现在**还能做什么。

    额度用完后它若还在说「再次提交时要带上……」,模型就会照做、撞上
    outline_budget、白烧一轮反思(照集合地图那条剩余额度行的先例)。
    """
    notebook = _seed(repo)
    repo.settings.reasoning_stale_limit = 99      # 单独测便签,不牵扯熔断
    llm = _SeqLLM([
        _outline_action([{"id": f"s{i}", "title": f"第{i}节"}])
        for i in range(_MAX_OUTLINE_UPDATES)
    ] + [ANSWER])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits)

    # 第 2 轮 prompt:已整理 1 次,还剩 5 次。
    assert f"还可以再整理 {_MAX_OUTLINE_UPDATES - 1} 次" in llm.reflect_prompts[1]
    # 最后一轮 prompt:额度耗尽,措辞整段换成「已定稿、别再提交」。
    last = llm.reflect_prompts[_MAX_OUTLINE_UPDATES]
    assert "已定稿" in last and "不要再提交 update_outline" in last
    assert "还可以再整理" not in last
    assert "再次提交时必须原样带上" not in last


def test_signature_ignores_nothing_that_matters():
    base = [OutlineSection(id="a", title="一", evidence_keys=["k1"])]

    assert outline_signature(base) == outline_signature(
        [OutlineSection(id="a", title="一", evidence_keys=["k1"])])
    for changed in (
        [OutlineSection(id="a", title="改了标题", evidence_keys=["k1"])],
        [OutlineSection(id="a", title="一", parent="p", evidence_keys=["k1"])],
        [OutlineSection(id="a", title="一", evidence_keys=["k1", "k2"])],
        [OutlineSection(id="b", title="一", evidence_keys=["k1"])],
        base + [OutlineSection(id="b", title="二")],
    ):
        assert outline_signature(changed) != outline_signature(base)


# --------------------------------------------------------- 候选 id 的展示口径

def test_source_passage_ids_are_shown_only_when_the_outline_is_live(repo):
    """原文候选此前没有任何动作需要指名道姓,所以不标 id。大纲把「这一节靠哪几条
    证据」变成了模型要表达的东西,而纯散文库里能绑的恰恰只有 chunk 与 element。"""
    retriever = ReasoningRetriever(
        retrieval=repo.retrieval, model_clients=repo,
        communities=repo.retrieval.community_queries(),
        settings=repo.settings,
    )
    elements = [RetrievedElement("el-1", "s1", "论文一", "p1", "formula", "文本")]
    chunks = [RetrievedChunk("ch-1", "s1", "论文一", "1", "正文")]

    off = retriever._summarize({}, elements, chunks)
    on = retriever._summarize({}, elements, chunks, show_ids=True)

    assert "(id=el-1)" not in off and "(id=ch-1)" not in off
    assert "(id=el-1)" in on and "(id=ch-1)" in on
    # 关闭态逐字节不变(默认参数就是关闭态)。
    assert off == retriever._summarize({}, elements, chunks, ())


@pytest.mark.parametrize("effort,ids_expected", [
    ("thorough", False), ("exhaustive", True),
])
def test_run_wires_the_id_display_to_the_gate(repo, effort, ids_expected):
    """run() 必须把**闸的值**交给 `_summarize`,不是无条件展示 id。

    上一版只测了 `_summarize` 本身(直接调用,两个方向都对),接线处没有任何守卫:
    把调用点改成 `show_ids=True` 时 225 条用例全绿——一个只在 exhaustive 才该出现
    的展示,悄悄对每一档都生效了。这里走真实 run:脚本先 search_elements 拿回原文
    候选,再看下一轮 prompt 的候选区里那些 [element] 行带不带 id。

    chunk 侧同一行代码、同一个开关,由 `_summarize` 的直接用例覆盖(把 chunk 喂进
    真实 run 要先建分块与 PPR,成本与收益不成比例)。
    """
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm, effort=effort)

    retriever.run(notebook.id, "综述一下", "", limits=limits)

    body = llm.reflect_prompts[1].split("Candidates so far:", 1)[-1]
    assert "[element]" in body, "前置条件:这一轮必须真的有原文候选"
    assert ("(id=el-000)" in body) is ids_expected
    assert ("(id=el-" in body) is ids_expected


def test_bindings_survive_the_candidate_window(repo):
    """合法键按**本 run 曾展示**累计,旧窗口滑走不失效、未展示中段不放行。

    `_summarize` 对候选池开的是头尾窗口,但服务端会持续持有旧绑定:按「当前展示」
    判合法的话,早先绑上的证据会因窗口滑动反过来被事实层判成非法。
    """
    from app.services.retrieval import RetrievedKnowledge

    collected = {
        f"ko-{i}": RetrievedKnowledge(
            object_id=f"ko-{i}", object_type="claim", payload={"name": f"n{i}"},
            evidence=[], score=0.0, relevance=0.0)
        for i in range(25)
    }
    retriever = ReasoningRetriever(
        retrieval=repo.retrieval, model_clients=repo,
        communities=repo.retrieval.community_queries(),
        settings=repo.settings,
    )

    ever_shown = set()
    retriever._summarize(
        collected, [], [], (), show_ids=True,
        shown_binding_keys=ever_shown,
    )
    slid_out = "ko-22"                         # 首轮 25 条时实际展示过
    assert slid_out in ever_shown
    for i in range(25, 60):
        collected[f"ko-{i}"] = RetrievedKnowledge(
            object_id=f"ko-{i}", object_type="claim",
            payload={"name": f"n{i}"}, evidence=[], score=0.0, relevance=0.0,
        )
    summary = retriever._summarize(
        collected, [], [], (), show_ids=True,
        shown_binding_keys=ever_shown,
    )
    retained = outline_binding_keys(collected, [], [], ever_shown, {slid_out})
    never_shown = "ko-40"

    assert slid_out not in summary               # 后来滑进中段
    assert slid_out in retained                  # 但曾展示/已绑定仍合法
    assert never_shown not in summary
    assert never_shown not in ever_shown
    assert never_shown not in retained            # 从未展示就不能猜 id 绑定


# ------------------------------------------------------------------ fail_closed

def test_fail_closed_rejects_an_outline_action_without_sections(repo):
    notebook = _seed(repo)
    llm = _SeqLLM([{"next_action": OUTLINE_ACTION, "outline": {"sections": []}}])
    retriever, limits = _retriever(repo, llm, fail_closed=True)

    with pytest.raises(ValueError, match="outline sections"):
        retriever.run(notebook.id, "综述", "", limits=limits)


def test_fail_closed_rejects_the_action_when_the_gate_is_off(repo):
    """关闭态下这个动作压根不存在,模型返回它就是畸形输出——既有未知动作合同。"""
    notebook = _seed(repo)
    llm = _SeqLLM([_outline_action([{"id": "a", "title": "一"}])])
    retriever, limits = _retriever(repo, llm, effort="deep", fail_closed=True)

    with pytest.raises(ValueError, match="invalid action"):
        retriever.run(notebook.id, "综述", "", limits=limits)


# ------------------------------------------------------------------------ e2e

def test_roster_then_two_outline_rounds_fill_an_empty_section(repo):
    """一条完整的路径:先枚举文档目录,按它建大纲(一节留空),再补上空节的绑定。

    这就是设计文档说的「集合地图与来源清单是天然的大纲种子」:先拿目录,再把目录
    变成节,空节驱动下一步检索。
    """
    notebook = _seed(repo, sources=("论文一", "论文二"))
    llm = _SeqLLM([
        # ① 先列出库里有哪几篇。
        {"next_action": "enumerate_elements", "reason": "先看有哪几篇",
         "enumerate": {"collection": "sources"}},
        # ② 按目录建大纲:一节绑上已有候选,另一节先留空。
        lambda prompt: _outline_action([
            {"id": "p1", "title": "论文一", "evidence": [_shown_ids(prompt)[0]]},
            {"id": "p2", "title": "论文二"},
        ], reason="按目录建大纲"),
        # ③ 补空节:把便签里已有的那一节原样重提(全量替换),再给 p2 绑上证据。
        lambda prompt: _outline_action([
            {"id": "p1", "title": "论文一", "evidence": [_shown_ids(prompt)[0]]},
            {"id": "p2", "title": "论文二", "evidence": [_shown_ids(prompt)[0]]},
        ], reason="补齐空节"),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "逐篇分析当前notebook", "", limits=limits)

    # 目录到手。
    assert [item.source_title for item in result.enumerations[0].items] == [
        "论文一", "论文二"]
    # 轨迹顺序:枚举 → 大纲 → 大纲 → 合成。
    assert [s.step_type for s in result.trace if s.step_type in
            ("enumerate", "outline", "answer")] == [
        "enumerate", "outline", "outline", "answer"]
    # 第一轮之后有空节,第二轮补齐。
    assert _steps(result, "outline")[0].summary == "更新大纲: 2 节(1 节待补证据)"
    assert _steps(result, "outline")[1].summary == "更新大纲: 2 节(0 节待补证据)"
    assert all(s.evidence_keys for s in result.outline)
    assert [s.id for s in result.outline] == ["p1", "p2"]
    # 第二轮的 prompt 里,大纲便签与目录账目同时在场(账目分区拼接)。
    # 位置断言只在**候选区**里做:动作说明本身也提到 [Collections in scope],
    # 拿整份 prompt 找首次出现会量到那句指令上。
    body = llm.reflect_prompts[2].split("Candidates so far:", 1)[-1]
    assert "本轮大纲便签" in body
    assert "「来源清单」已完整列出 2 条" in body
    # 顺序:几份账目 → 大纲便签 → 集合地图(账目讲「本轮做了什么」,地图讲
    # 「库里有什么」)。
    assert body.index("「来源清单」已完整列出") < body.index("本轮大纲便签")
    assert body.index("本轮大纲便签") < body.index("[Collections in scope]")


# ------------------------------------- sufficient 与最终一次 update_outline

def test_the_final_update_outline_lands_even_with_sufficient(repo):
    """「交最终版大纲」与「宣布证据够了」是同一轮的事。

    reflect prompt 教模型「每个方面都有证据了才置 sufficient」——那正是它把最后一批
    绑定补上、交出定稿大纲的时刻。`sufficient` 短路若排在动作分发之前,这份载荷就被
    静默丢掉:那一节的证据明明检索到了、模型也绑上了,按节合成拿到的却是上一轮那份
    还带着空节的旧大纲(codex PR#407 R2 P2)。
    """
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        # ① 先建大纲,第二节故意留空。
        lambda prompt: _outline_action([
            {"id": "a", "title": "已有证据的一节",
             "evidence": [_shown_ids(prompt)[0]]},
            {"id": "b", "title": "待补的一节"},
        ]),
        # ② 为空节做一次定向检索(它也把 stale 清零,与真实用法同形)。
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        # ③ 最终版大纲 + sufficient=true:同一轮里既补上绑定又宣布收尾。
        lambda prompt: dict(
            _outline_action([
                {"id": "a", "title": "已有证据的一节",
                 "evidence": [_shown_ids(prompt)[0]]},
                {"id": "b", "title": "待补的一节",
                 "evidence": [_shown_ids(prompt)[-1]]},
            ], reason="补齐后收尾"),
            sufficient=True,
        ),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述一下", "", limits=limits)

    # 最后一次载荷真的落到了终态大纲上:没有空节。
    assert [s.id for s in result.outline] == ["a", "b"]
    assert all(s.evidence_keys for s in result.outline), [
        (s.id, s.evidence_keys) for s in result.outline]
    # 它也照常留下一条 outline 轨迹步(与正常分支同一套语义)。
    steps = _steps(result, "outline")
    assert len(steps) == 2
    assert steps[-1].summary == "更新大纲: 2 节(0 节待补证据)"
    assert steps[-1].detail["empty_sections"] == []
    # 循环确实终止:应用完就收尾,不再多跑一轮反思。
    assert result.trace[-1].step_type == "answer"
    assert result.trace[-2].step_type == "outline"
    assert len(_steps(result, "reflect")) == 3


def test_answer_with_sufficient_applies_no_outline_payload(repo):
    """对照:next_action=answer 立即断路,绝不顺手应用任何 outline 载荷。

    模型没说要改结构(它选的是「作答」),把它上一轮的模板残留当成一次提交会让终态
    大纲被一份没人要求过的东西替换掉。
    """
    notebook = _seed(repo)
    llm = _SeqLLM([
        _outline_action([{"id": "a", "title": "保留的一节"}]),
        {"next_action": "answer", "sufficient": True,
         "outline": {"sections": [{"id": "z", "title": "不该生效的一节"}]}},
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert [s.title for s in result.outline] == ["保留的一节"]
    assert len(_steps(result, "outline")) == 1


def test_the_final_update_outline_still_obeys_the_budget(repo):
    """短路前那次应用与分发链共用同一个函数,所以预算语义不可能分叉:额度用完
    时它同样只记 outline_budget,不改大纲。"""
    notebook = _seed(repo)
    repo.settings.reasoning_stale_limit = 99      # 单独测预算,不牵扯熔断
    llm = _SeqLLM([
        _outline_action([{"id": f"s{i}", "title": f"第{i}节"}])
        for i in range(_MAX_OUTLINE_UPDATES)
    ] + [
        dict(_outline_action([{"id": "late", "title": "迟到的一节"}]),
             sufficient=True),
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert len(_steps(result, "outline")) == _MAX_OUTLINE_UPDATES
    assert "outline_budget" in _skips(result)
    assert [s.id for s in result.outline] == [
        f"s{_MAX_OUTLINE_UPDATES - 1}"]
    assert result.trace[-1].step_type == "answer"


def test_parse_strips_citation_markers_from_titles():
    """codex r6: 标题里的 `[k12]`/`[k12, k13]` 是引用形标记,不属于标题文案。
    留着的话 `## 标题` 进最终 Markdown 后,前端对合并全文扫标记建引用表——
    标题里的号要么显示成绑不上的裸引用,要么恰好撞上别节号段、绑到毫不相干
    的证据。解析入口单点剥掉,trace 步/账目回喂/最终标题共用同一份产物。"""
    sections = parse_outline_sections({"sections": [
        {"id": "s1", "title": "结论 [k12] 汇总 [ k3, k4 ]", "evidence": []},
    ]})
    assert len(sections) == 1
    assert sections[0].title == "结论 汇总"
    # 整个标题都是标记时,剥完为空 → 与无标题条目同判,整条丢弃。
    assert parse_outline_sections({"sections": [
        {"id": "s2", "title": "[k5001]", "evidence": []},
    ]}) == []


# --------------------------------------------- 大纲采用引导(设计文档 §3.1.1)
#
# 真机动机:穷尽档三次 run 的 update_outline 采用率 0/3。最有说服力的一次是模型
# 用 enumerate(collection=sources)一次性完整列出 84 篇文档,随后连开 4 次 PPR 找
# 「核心贡献」、撞上上限,最后自述「多次子查询未返回具体每篇文章的核心贡献」并
# 作答 —— 它手里有完整清单,缺的正是「把清单变成结构、再逐节补证」。

def _roster_chain(returned_total, *, state="complete", collection="sources"):
    """一条来源清单的续跑账目(真对象,不是 SimpleNamespace:字段改名要报红)。"""
    from app.models.ask import TypedCollectionCoverage

    return _EnumChain(
        outcome=CollectionEnumerationOutcome(
            collection=collection, kind="", source_id="",
            coverage=TypedCollectionCoverage(
                returned_total=returned_total,
                total=returned_total,
                complete=(state == "complete"),
            ),
        ),
        state=state,
    )


def _nudge(sections=(), chains=None, directions=0, *, active=True, used=0):
    return _outline_nudge_note(
        list(sections), chains, directions, active=active, nudges_used=used)


def test_the_nudge_needs_the_gate_the_empty_outline_and_a_structural_reason():
    """三个前置条件缺一不可,再加「结构性理由二选一」。

    少任何一条都会把引导变成噪音:门关着的档位根本没有 update_outline 可调(教一个
    调不动的动作是纯亏);大纲已经建起来时 `_outline_note` 接管同一区位;而没有清单
    也没有多个必答方向的单点事实题,建大纲比不建更慢。
    """
    roster = {("sources", "", ""): _roster_chain(84)}

    assert "本轮尚未建立大纲" in _nudge(chains=roster)
    # ① 门关着(低档位 / REASONING_OUTLINE_ENABLED=false)→ 零字节。
    assert _nudge(chains=roster, active=False) == ""
    # ② 大纲已非空 → 便签接管,引导退场。
    assert _nudge([OutlineSection(id="a", title="第一节")], chains=roster) == ""
    # ③ 本 run 引导额度用尽。
    assert _nudge(chains=roster, used=_OUTLINE_NUDGE_MAX_ROUNDS) == ""
    # ④ 两条结构性理由都不成立 → 不引导(空账目 + 零方向)。
    assert _nudge(chains={}) == ""
    assert _nudge(chains=None) == ""


def test_the_roster_wording_wins_over_the_direction_count():
    """两条理由同时成立时用清单那条:它更具体,而且带着一个真实条数。

    「84 篇」是模型手上确实有的东西,「有 3 个方面」只是一个抽象;真机那次卡住的正是
    「清单在手却不知道拿它做什么」,所以更具体的那条才是要说的。
    """
    roster = {("sources", "", ""): _roster_chain(84)}

    both = _nudge(chains=roster, directions=3)
    assert "已完整列出 84 篇文档" in both
    assert "必答方向" not in both

    only_directions = _nudge(chains={}, directions=3)
    assert "本题有 3 个已确认的必答方向" in only_directions
    assert "篇文档" not in only_directions


def test_an_unfinished_roster_never_triggers_the_nudge():
    """`state == "complete"` 是硬判据,不能放宽成「列过就算」。

    半份清单变成的大纲天然缺节,而模型此刻并不知道自己缺了哪几节 —— 引导它按一份
    没列完的目录定稿,比不引导更糟。`conflict`(枚举期间资料变了)同理。
    """
    for state in ("open", "conflict"):
        assert _nudge(chains={("sources", "", ""): _roster_chain(84, state=state)}) == "", state
    # 别的集合列完了也不算:元素/知识对象清单不是「每篇一节」的天然骨架。
    assert _nudge(chains={
        ("elements", "formula", ""): _roster_chain(84, collection="elements"),
    }) == ""


def test_a_one_item_scope_is_below_the_nudge_threshold():
    """1 篇文档 / 1 个方向的问题一次合成就答完了,给它建大纲纯属噪音。"""
    assert _OUTLINE_NUDGE_MIN_ITEMS == 2
    assert _nudge(chains={("sources", "", ""): _roster_chain(1)}) == ""
    assert _nudge(chains={}, directions=1) == ""
    # 恰好到线就引导。
    assert "已完整列出 2 篇文档" in _nudge(
        chains={("sources", "", ""): _roster_chain(2)})
    assert "本题有 2 个已确认的必答方向" in _nudge(chains={}, directions=2)


def test_the_nudge_states_the_fact_and_offers_a_way_out_within_one_line():
    """三要素:现状(未建大纲)+ 具体依据(N 篇 / M 方向)+ 可忽略的出口。

    出口不是客套。引导是**账目**不是命令:服务端并不知道这一题到底需不需要大纲,
    没有出口的措辞会让模型对单点事实题也硬建一份,把省下来的轮次又赔回去。
    """
    for text in (_nudge(chains={("sources", "", ""): _roster_chain(84)}),
                 _nudge(chains={}, directions=4)):
        assert "本轮尚未建立大纲" in text
        assert "update_outline" in text
        assert "忽略本行" in text
        assert "\n" not in text, "一行,不许换行撑开便签区位"
        assert len(text) <= 160, len(text)


def test_the_biggest_finished_roster_sets_the_count():
    """限定单源与全作用域是两条独立的链;取最大的那份,别把两份加起来。"""
    chains = {
        ("sources", "", ""): _roster_chain(84),
        ("sources", "", "s1"): _roster_chain(3),
    }
    assert "已完整列出 84 篇文档" in _nudge(chains=chains)


def test_a_finished_roster_nudges_the_model_at_most_twice(repo):
    """e2e:列完目录却不建大纲 → 接下来两轮各引导一次,第三轮起闭嘴。

    上界是成本合同:账目要教新东西,同一句话喊第三遍只烧 prompt 预算 —— 模型看过
    两次仍不采用,就是它判断这题不需要大纲,而那正是「忽略本行」这个出口的意义。
    """
    notebook = _seed(repo, sources=("论文一", "论文二", "论文三"), elements=2)
    repo.settings.reasoning_stale_limit = 99      # 单独测引导,不牵扯熔断
    llm = _SeqLLM([
        {"next_action": "enumerate_elements", "reason": "先看有哪几篇",
         "enumerate": {"collection": "sources"}},
        {"next_action": "search_elements", "elements_query": "版图设计要点 一"},
        {"next_action": "search_elements", "elements_query": "版图设计要点 二"},
        {"next_action": "search_elements", "elements_query": "版图设计要点 三"},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述这个notebook里的文章", "", limits=limits)

    nudged = ["本轮尚未建立大纲" in prompt for prompt in llm.reflect_prompts]
    # 第 1 轮还没有清单 → 不引导;第 2、3 轮引导;第 4 轮起额度用尽。
    assert nudged == [False, True, True, False, False], nudged
    assert sum(nudged) == _OUTLINE_NUDGE_MAX_ROUNDS
    assert "已完整列出 3 篇文档" in llm.reflect_prompts[1]
    # 轨迹键只在真的发出的那两轮出现。
    assert [step.detail.get("outline_nudged")
            for step in _steps(result, "reflect")] == [
        None, True, True, None, None]


def test_the_nudge_sits_between_the_ledgers_and_the_collection_map(repo):
    """区位与大纲便签同一处:账目讲「本轮做了什么」,地图讲「库里有什么」。"""
    notebook = _seed(repo, sources=("论文一", "论文二"), elements=2)
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"collection": "sources"}},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits)

    body = llm.reflect_prompts[1].split("Candidates so far:", 1)[-1]
    assert body.index("「来源清单」已完整列出") < body.index("本轮尚未建立大纲")
    assert body.index("本轮尚未建立大纲") < body.index("[Collections in scope]")


def test_the_scratchpad_replaces_the_nudge_once_the_outline_exists(repo):
    """引导与便签互斥:模型一旦真的建了大纲,同一区位由便签接管。"""
    notebook = _seed(repo, sources=("论文一", "论文二"), elements=2)
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"collection": "sources"}},
        lambda prompt: _outline_action([
            {"id": "p1", "title": "论文一", "evidence": [_shown_ids(prompt)[0]]},
        ]),
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits)

    assert "本轮尚未建立大纲" in llm.reflect_prompts[1]
    # 建完之后那一轮:便签在,引导不在。
    assert "本轮大纲便签" in llm.reflect_prompts[2]
    assert "本轮尚未建立大纲" not in llm.reflect_prompts[2]
    assert [step.detail.get("outline_nudged")
            for step in _steps(result, "reflect")] == [None, True, None]


def test_confirmed_directions_nudge_without_any_roster(repo):
    """理由 (b):已确认意图给出多个必答方向时,不必先枚举也该建大纲。

    方向数取已确认方向清单长度减一 —— `confirmed_intent_queries` 的第一条恒为完整
    的已确认问题本身,其余才是必答主题派生的方向。
    """
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits,
                  intent_queries=["完整的已确认问题", "方向一", "方向二"])

    assert "本题有 2 个已确认的必答方向" in llm.reflect_prompts[0]


def test_a_single_confirmed_direction_is_not_a_reason(repo):
    """只有整体问题一条种子(零必答方向)→ 不引导。"""
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits,
                  intent_queries=["完整的已确认问题"])

    assert not any("本轮尚未建立大纲" in prompt
                   for prompt in llm.reflect_prompts)


def test_exactly_one_direction_is_still_not_a_reason(repo):
    """真边界:一条权威问题 + 恰好 1 个方向 → 仍不引导。

    上一条用的是零方向,它挡不住「把 >=2 写成 >=1」这种 off-by-one —— 那个写法
    会让任何带一条补充方向的普通问题都被劝去建大纲,而单点事实题被拆出一两个
    检索角度是常态。理由 (b) 的阈值必须由这条钉住。
    """
    notebook = _seed(repo, elements=2)
    llm = _SeqLLM([
        {"next_action": "search_elements", "elements_query": "版图设计要点"},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    retriever.run(notebook.id, "综述", "", limits=limits,
                  intent_queries=["完整的已确认问题", "唯一的一个方向"])

    assert not any("本轮尚未建立大纲" in prompt
                   for prompt in llm.reflect_prompts)


@pytest.mark.parametrize("effort", ["overview", "standard", "deep", "thorough"])
def test_the_nudge_is_absent_below_the_exhaustive_effort(repo, effort):
    """低档位逐字回到接入前:没有 update_outline 可调,就没有可引导的东西。"""
    notebook = _seed(repo, sources=("论文一", "论文二"), elements=2)
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"collection": "sources"}},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm, effort=effort)

    result = retriever.run(notebook.id, "综述", "", limits=limits,
                           intent_queries=["整体问题", "方向一", "方向二"])

    assert not any("本轮尚未建立大纲" in prompt
                   for prompt in llm.reflect_prompts)
    # detail 逐**键**不变(不是值不变):关闭态不该多出一个键。
    assert all("outline_nudged" not in step.detail
               for step in _steps(result, "reflect"))


def test_the_nudge_is_absent_when_the_switch_is_off(repo):
    """穷尽档 + REASONING_OUTLINE_ENABLED=false:同样零字节。"""
    notebook = _seed(repo, sources=("论文一", "论文二"), elements=2)
    repo.settings.reasoning_outline_enabled = False
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"collection": "sources"}},
        ANSWER,
    ])
    retriever, limits = _retriever(repo, llm)

    result = retriever.run(notebook.id, "综述", "", limits=limits,
                           intent_queries=["整体问题", "方向一", "方向二"])

    assert not any("本轮尚未建立大纲" in prompt
                   for prompt in llm.reflect_prompts)
    assert all("outline_nudged" not in step.detail
               for step in _steps(result, "reflect"))
