from app.services.prompts import (
    answer_prompt,
    retrieval_experience_prompt,
    ANSWER_SCHEMA_HINT,
)
import json

import pytest


def test_answer_prompt_states_marker_and_inference_rules():
    p = answer_prompt("q?", "k1: [concept] Engram — def: ...")
    assert "[k1]" in p or "[k_i]" in p              # marker convention present
    assert "推断" in p or "inference" in p.lower()   # inference must be self-labelled
    assert "k1: [concept] Engram" in p               # context block embedded
    assert "answer" in ANSWER_SCHEMA_HINT and "grounded" in ANSWER_SCHEMA_HINT


def test_answer_prompt_includes_history_when_present():
    from app.services.prompts import answer_prompt
    p = answer_prompt("follow up?", "k1: [concept] X", history_block="User: prev q\nAssistant: prev a")
    assert "prev q" in p and "prev a" in p
    p2 = answer_prompt("q?", "k1: [concept] X")   # default no history
    assert "prev q" not in p2


def test_answer_prompt_forbids_fabricated_citation():
    from app.services.prompts import answer_prompt
    p = answer_prompt("q?", "k1: [concept] X")
    assert "DIRECTLY from that specific knowledge item" in p
    assert "MUST NOT contain any [k]" in p
    assert "NEVER attach [k]" in p
    # 原有推断标注规则仍在
    assert "推断" in p


def test_answer_prompt_rule_12_preserves_question_qualifiers():
    """T1:规则 12(合成限定词保真)存在,且落在规则 11 之后、Question 行之前;
    有 history 时同样落在 history 段之前(镜像 test_answer_prompt_includes_history_when_present
    与 test_style_block_lands_between_the_rules_and_the_question_in_answer_prompt 的定位写法)。"""
    from app.services.prompts import answer_prompt

    p = answer_prompt("q?", "k1: [concept] X")
    assert "12. Preserve every qualifier" in p
    # 承重的是后半句:证据只覆盖邻近情形时必须明说、外推标（推断）。只留第一句的
    # 规则 12 在功能上已不满足规格,所以单独钉住它。
    assert (
        "If the knowledge items cover only the unqualified case or an adjacent "
        "object, say so in one explicit sentence, keep any extrapolation to the "
        "asked case marked （推断）" in p
    )

    rule_11_idx = p.index("collection on its own.")
    rule_12_idx = p.index("12. Preserve every qualifier")
    question_idx = p.index("Question: q?")
    assert rule_11_idx < rule_12_idx < question_idx

    p_with_history = answer_prompt(
        "follow up?",
        "k1: [concept] X",
        history_block="User: prev q\nAssistant: prev a",
    )
    rule_12_idx_h = p_with_history.index("12. Preserve every qualifier")
    history_idx = p_with_history.index("Prior conversation")
    assert rule_12_idx_h < history_idx


def test_answer_prompt_rule_13_propagates_inference_to_conclusions():
    """T2-c:规则 13(推断状态传递)存在,落在规则 12 之后、Question 行之前;
    有 history 时同样落在 history 段之前。"""
    from app.services.prompts import answer_prompt

    p = answer_prompt("q?", "k1: [concept] X")
    assert "13. Inference status propagates" in p
    # 承重句:只有每条前提都挂 [k] 的结论才可以不标（推断）。
    assert (
        "Only a conclusion whose every premise is a [k]-cited sentence may be "
        "stated without the marker" in p
    )
    assert (
        "Never let a closing section state as established fact what the body "
        "only inferred." in p
    )

    rule_12_idx = p.index("substitute a related object.")
    rule_13_idx = p.index("13. Inference status propagates")
    question_idx = p.index("Question: q?")
    assert rule_12_idx < rule_13_idx < question_idx

    p_with_history = answer_prompt(
        "follow up?",
        "k1: [concept] X",
        history_block="User: prev q\nAssistant: prev a",
    )
    rule_13_idx_h = p_with_history.index("13. Inference status propagates")
    history_idx = p_with_history.index("Prior conversation")
    assert rule_13_idx_h < history_idx


def test_answer_prompt_rule_2_places_marker_after_list_syntax():
    """T2-d:规则 2 补一句位置要求——（推断）/Likely, 标记开句但要排在列表序号/
    项目符号/标题语法之后,不能挡在它们前面(会破坏 Markdown 列表识别)。"""
    from app.services.prompts import answer_prompt

    p = answer_prompt("q?", "k1: [concept] X")
    placement_sentence = (
        "The marker opens the sentence but goes AFTER any list number, bullet, "
        "or heading syntax (write `1. （推断）…`, never `（推断）1. …`, so "
        "Markdown lists stay intact)."
    )
    assert placement_sentence in p
    # 下界锚在规格点名的那半句:句子紧跟「(prefix with '（推断）' / 'Likely,')」。
    # 锚在更早的「NEVER attach [k]」会放过把这句挪到 `[k]` 规则后面的变异——那时
    # 「The marker」的最近先行词变成 [k],读起来像在要求 [k] 开句(评审 P2-1)。
    prefix_idx = p.index("(prefix with '（推断）' / 'Likely,')")
    rule_3_idx = p.index("3. If the items don't cover")
    placement_idx = p.index(placement_sentence)
    assert prefix_idx < placement_idx < rule_3_idx
    assert placement_idx - prefix_idx < 60, "位置句必须紧跟 prefix 那半句"


def test_query_intent_prompt_cross_tool_mapping_guidance():
    from app.services.prompts import query_intent_prompt
    p = query_intent_prompt("how do I do Innovus's place_opt_design in ICC2?")
    # 触发条件本身要写清楚是条件语句(仅在点名两个及以上工具/系统并要求对照时生效)
    assert "TWO OR MORE tools/systems/products" in p
    assert (
        "maps to, compares with, or is achieved in another" in p
    )
    # 每个工具必须拥有自己的必答主题,不得把目标侧折叠进来源侧主题
    assert "MUST own its own mandatory topic" in p
    assert "Never fold the target tool's side into the source tool's topic" in p
    # 目标侧检索方向必须配目标工具名+功能描述词,不能只用来源工具的命令/API名
    assert "pair the target tool's NAME" in p
    assert (
        "NEVER the source tool's command/API names alone" in p
    )
    assert "the target's documents do not mention the source's identifiers" in p
    # few-shot 对照例是这段指引的承重部分(正例串+反例串都要在)——它占块内
    # ~40% token,是「压 prompt」时最容易被顺手删掉的,删了模型就失去唯一示范。
    assert '"ICC2 placement optimization command"' in p
    assert '"place_opt_design usage"' in p
    # 预算冲突时的优先级(评审 P3-1):按工具拆分优先,目标侧主题绝不被截。
    assert "the target tool's topic is never the one dropped" in p
    # 位置守卫(评审 P2-2,移动变异曾打空):指引必须留在指令区——晚于
    # mandatory_topics 规则段的收尾句、早于 normalized_question 规则,且绝不
    # 落到 "User request:"(不可信用户文本)之后。
    block = p.index("TWO OR MORE tools/systems/products")
    assert p.index("Do not answer the question and do not mention corpus coverage.") < block
    assert block < p.index("normalized_question is a standalone")
    assert block < p.index("User request:")


def test_query_intent_prompt_single_topic_rules_unchanged():
    """新指引是追加段落,既有单主题产出规则文本必须逐字保留。"""
    from app.services.prompts import query_intent_prompt
    p = query_intent_prompt("q?")
    for literal in (
        "Freeze what the user actually asks; evidence availability must never change "
        "the requested topic. Split only genuinely distinct required questions. ",
        "Each topic needs a stable short "
        "id, a title in the user's language, the exact question it must answer, and "
        "1-4 retrieval queries. Preserve requested comparisons, constraints, scope, "
        "time range and output form. excluded_topics lists plausible but out-of-scope "
        "directions. Do not answer the question and do not mention corpus coverage.\n",
        "normalized_question is a standalone, precise formulation in the user's "
        "language. intent_type classifies the requested operation. entities lists "
        "the concrete research objects.",
    ):
        assert literal in p, f"既有规则文本被改动: {literal!r}"


def test_query_intent_prompt_preserves_requested_granularity_and_real_clarification():
    from app.services.prompts import query_intent_prompt

    p = query_intent_prompt("Compare the mechanisms of two compression methods.")

    assert "Keep the user's requested level of detail" in p
    assert "requirements from the user's wording" in p
    assert "Do not add mandatory formulas" in p
    assert "Preserve those details when the user explicitly asks for them" in p
    assert "Retrieval query variants are search aids" in p
    assert "Normalize phrasing without adding facts" in p
    assert "retain the original wording and ask for it in ambiguities" in p
    assert "options must be actual choices" in p
    assert "use an empty options list when a free-text answer is required" in p


def test_extract_prompt_excludes_enumerated_values_and_meta_claims():
    from app.services.kg.extract import _prompt
    p = _prompt("[1] sample text", "Section 1", "textbook")
    # concept:取值枚举不独立成节点
    assert "enumerated settings" in p and "Do NOT emit Concepts" in p
    # claim:不抽标题/前言/元叙述
    assert "stands alone as truth-evaluable" in p
    assert "section headings" in p
    assert "narrative/meta sentences about the document" in p


# --------------------------------------------------------------------------- #
# Agentic Memory P3 (T8) — style_block 新增形参:默认值空转 + 落点/双拼写覆盖。
# --------------------------------------------------------------------------- #
def test_style_block_default_is_byte_identical_to_omitting_it():
    """新形参默认空串 ⇒ 不传它与显式传 ``style_block=""`` 逐字节相同——三个
    消费点(合成 + 两份规划拼写)都要测,这是「关闭态回到接入前」在 prompts
    层的判据(镜像 profile_block/experience_block 的冻结基线先例)。"""
    from app.services.prompts import answer_prompt, expand_query_prompt, plan_prompt

    assert answer_prompt("q?", "k1: [concept] X") == answer_prompt(
        "q?", "k1: [concept] X", style_block="")
    assert plan_prompt("q") == plan_prompt("q", style_block="")
    assert expand_query_prompt("q") == expand_query_prompt("q", style_block="")


def test_style_block_lands_between_the_rules_and_the_question_in_answer_prompt():
    """合成侧:渲染在编号规则之后、``Question:`` 行之前(计划 T8 点 3)。"""
    from app.services.prompts import answer_prompt

    p = answer_prompt("q?", "k1: [concept] X", style_block="STYLE_MARKER_XYZ")
    assert "STYLE_MARKER_XYZ" in p
    rules_end = p.index("11. When the question asks you to enumerate")
    question_line = p.index("Question: q?")
    assert rules_end < p.index("STYLE_MARKER_XYZ") < question_line


def test_style_block_reaches_both_planning_prompt_spellings():
    """规划侧:``plan_prompt``(backup 拼写)与 ``expand_query_prompt``
    (production 实际发送的那份)必须都加上这个参数——「要到达规划模型的东西
    必须两份都加」,镜像 profile_block/experience_block 的既有钉法。"""
    from app.services.prompts import expand_query_prompt, plan_prompt

    assert "STYLE_MARKER_XYZ" in plan_prompt("q", style_block="STYLE_MARKER_XYZ")
    assert "STYLE_MARKER_XYZ" in expand_query_prompt(
        "q", style_block="STYLE_MARKER_XYZ")


def test_retrieval_experience_prompt_rule_3_explains_the_anchored_figure():
    """Agentic Memory P4 (T4):规则 3 的一份静态措辞——不按批次动态改写,
    只需在场就把"anchored= 是逐步成功证据、缺席则说明这批早于归因接线"
    这句话讲清楚,并保留"Prefer what FAILED"这句既有底线。"""
    p = retrieval_experience_prompt(
        "[Recent searches, grouped by question shape]\ns0: mode=reasoning",
        "[Existing entries for similar shapes]\n(none)",
        actions=("ppr", "retrieve"),
        rationale_max_chars=80,
    )
    assert "Prefer what FAILED" in p
    assert "anchored=" in p
    assert "per-action success" in p
    assert "predates this check" in p
    assert "total_citations" in p
    assert "must never be attributed to one particular action" in p


# --------------------------------------------------------------------------- #
# KG 可选化 T2 — 规划措辞的 kg_available 门(验收 7)。
# --------------------------------------------------------------------------- #
def test_plan_prompt_kg_available_true_is_byte_identical_to_omitting_it():
    """新门参数默认 True ⇒ 不传它与显式传 True 逐字节相同(有图侧零变化)。

    基线是同一个函数的默认渲染,不是快照:plan 的措辞今后怎么调这条都成立,
    它红只可能是因为有人让这把闸漏进了默认路径。
    """
    import itertools

    from app.services.prompts import plan_prompt

    for h, c, p, e, s in itertools.product(
            ["", "H"], ["", "C"], ["", "P"], ["", "E"], ["", "S"]):
        assert plan_prompt("q", h, c, p, e, style_block=s) == plan_prompt(
            "q", h, c, p, e, style_block=s, kg_available=True)


def test_plan_prompt_kg_available_false_changes_only_the_kg_bound_lines():
    """无图侧:只有首句与 `types` 字段说明换掉,JSON 合同与其余每一行原样。

    `types` 那行必须一起换:它原本写「subset of the 4」,指的是首句介绍的那 4 个
    KG 节点类型。首句一走,「the 4」就成了悬空指代——prompt 里再没有任何一处列出
    过这个 4。
    """
    from app.services.prompts import plan_prompt

    on = plan_prompt("布局布线怎么做", "H", "C", "P", "E", style_block="S")
    off = plan_prompt("布局布线怎么做", "H", "C", "P", "E", style_block="S",
                      kg_available=False)
    on_lines, off_lines = on.splitlines(), off.splitlines()
    assert len(on_lines) == len(off_lines)
    differing = [i for i, (a, b) in enumerate(zip(on_lines, off_lines)) if a != b]
    # 首句(0)与 `- types:` 那行(3);其余每一行逐字节不动。
    assert differing == [0, 3], [off_lines[i] for i in differing]
    assert off_lines[0] == (
        "You plan how to retrieve evidence from a document library to answer "
        "an engineer's question. Sub-queries are run against source passages; "
        "the `types` field is ignored when the library has no knowledge graph.")
    assert off_lines[3] == (
        "- types: ignored when the library has no knowledge graph; leave it "
        "empty.")
    assert on_lines[3].startswith("- types: which node types to search")
    assert "the 4" not in off
    assert "knowledge graph (KG)" not in off
    # JSON 合同不变(`types` 仍是合法字段,解析器零改动)。
    assert off.endswith(
        'Return JSON only: {"sub_queries":[{"query":"","types":[],'
        '"prefer":"balanced","reason":""}]}')


def test_reflect_prompt_kg_actions_true_is_byte_identical_to_omitting_it():
    """验收 7 的 reflect 半:同款「True == 不传」对账(动作段的对账在
    ``test_reasoning_retrieval`` 那一组)。"""
    from app.services.prompts import reflect_prompt, reflect_schema_hint

    assert reflect_prompt("q", "c") == reflect_prompt("q", "c", kg_actions=True)
    assert reflect_schema_hint() == reflect_schema_hint(kg_actions=True)


# ---------------------------------------------------------------------------
# PR-A(`read_document`,按篇读取有界原文取样):两态 × 全门组合。
#
# 这一组是那把闸的**唯一**逐字节对账:关门必须回到接入前,开门必须让三处同时
# 出现(schema 的 next_action 词、schema 的参数分支、prompt 的动作说明行)。三处
# 里少任何一处,模型都会看到一个它调不动的动作,或反过来调一个没被告知的动作。


_READ_DOCUMENT_GATE_COMBINATIONS = [
    {},
    {"kg_actions": False},
    {"outline": True},
    {"consult_memory": True},
    {"search_chunks": True},
    {"element_kinds": ("formula", "table"), "object_types": ("claim",)},
    {"element_kinds": ("formula",), "object_types": ("claim",),
     "outline": True, "consult_memory": True, "search_chunks": True,
     "kg_actions": False},
]


@pytest.mark.parametrize("gates", _READ_DOCUMENT_GATE_COMBINATIONS)
def test_read_document_closed_is_byte_identical_to_omitting_it(gates):
    """关门 == 不传 == 接入这个动作之前,在每一种其他闸的组合下都成立。"""
    from app.services.prompts import reflect_prompt, reflect_schema_hint

    assert reflect_schema_hint(**gates) == reflect_schema_hint(
        read_document=False, **gates)
    assert reflect_prompt("q", "c", **gates) == reflect_prompt(
        "q", "c", read_document=False, read_document_cap=4, **gates)
    # 关门态里一个字节都不许留下这个动作的痕迹。
    assert "read_document" not in reflect_schema_hint(**gates)
    assert "read_document" not in reflect_prompt("q", "c", **gates)


@pytest.mark.parametrize("gates", _READ_DOCUMENT_GATE_COMBINATIONS)
def test_read_document_open_shows_up_in_all_three_projections(gates):
    """开门 ⇒ schema 枚举词 + schema 参数分支 + prompt 动作行**同时**出现或同时缺席。

    这把闸是**双条件**的:`read_document` 自己,加上「本 run 有枚举工具」。三处投影
    读的必须是同一个合取式——服务端那把复合判据(`document_read_active`)本来就含
    枚举闸,而 schema 若只吃前一半,一个手工传参的调用方就能造出「schema 里有这个
    分支、prompt 里没有它的说明」的组合;模型照样会去填那个模板槽位,而它填出来的
    每一次调用都会被服务端以「本轮还没有列出过来源清单」跳掉。
    """
    from app.services.prompts import reflect_prompt, reflect_schema_hint

    has_enumeration = bool(gates.get("element_kinds") or gates.get("object_types"))
    schema = json.loads(reflect_schema_hint(read_document=True, **gates))
    assert ("read_document" in schema["next_action"].split("|")) is has_enumeration
    assert ("read_document" in schema) is has_enumeration
    if has_enumeration:
        assert schema["read_document"] == {
            "source": "", "coverage": "spread|opening"}

    prompt = reflect_prompt("q", "c", read_document=True,
                            read_document_cap=4, **gates)
    assert ("- read_document: sample the ORIGINAL TEXT" in prompt) is has_enumeration
    # 无枚举时这个动作在**两处投影里都**一个字节都不留(与关门态同形)。
    if not has_enumeration:
        assert "read_document" not in reflect_schema_hint(
            read_document=True, **gates)
        assert reflect_schema_hint(read_document=True, **gates) == (
            reflect_schema_hint(**gates))


def test_read_document_action_line_says_the_five_things_it_has_to_say():
    """五条要点各钉一次:标题来源、首选目标、与 search_chunks 的分工、有界取样的
    披露义务、coverage 两个取值。报出的次数就是调用方传进来的那个预算。"""
    from app.services.prompts import reflect_prompt

    prompt = reflect_prompt(
        "q", "c", element_kinds=("formula",), object_types=("claim",),
        read_document=True, read_document_cap=3)

    assert "copied EXACTLY as the roster above listed it" in prompt
    assert "if you have not listed the roster yet, do that FIRST" in prompt
    assert "Prefer the rows the roster showed with NO stored summary" in prompt
    assert "It is NOT search_chunks" in prompt
    assert "never state or imply that you have read the document in full" in prompt
    assert '"spread"' in prompt and '"opening"' in prompt
    assert "at most 3 document(s)" in prompt


def test_opening_the_gate_without_an_allowance_is_a_loud_error():
    """⑪ `read_document=True` 配上缺省的 `read_document_cap=0` 会渲染「at most 0
    document(s)」——一个自称存在、却一次都调不动的动作。两个参数说的是同一件事,
    而 0 正是部署方用来表达「这个动作不存在」的写法(总闸要求次数上限非零),所以
    这个组合是接线错误,必须响亮地失败而不是渲染出来。"""
    from app.services.prompts import reflect_prompt

    with pytest.raises(ValueError, match="read_document_cap"):
        reflect_prompt("q", "c", element_kinds=("formula",),
                       read_document=True)
    with pytest.raises(ValueError, match="read_document_cap"):
        reflect_prompt("q", "c", read_document=True, read_document_cap=0)
    # 关门态照旧接受缺省的 0(那正是它的含义)。
    assert reflect_prompt("q", "c", read_document=False) == reflect_prompt("q", "c")


def test_the_roster_follow_up_sentence_forks_with_read_document():
    """④ 清单动作的尾句必须与 `read_document` 同步分叉。

    没有这个动作时,按标题 add_subquery 是唯一能往下走的路,尾句逐字节保持接入前;
    有这个动作时,再教模型对一个「没有摘要」的行去做相关性检索,就是让它去检索一篇
    它还描述不出来的文档——那正是 read_document 存在要补的那个缺口。
    """
    from app.services.prompts import reflect_prompt

    kinds = {"element_kinds": ("formula",), "object_types": ("claim",)}
    closed = reflect_prompt("q", "c", **kinds)
    opened = reflect_prompt("q", "c", read_document=True,
                            read_document_cap=4, **kinds)

    assert ("then, for each document worth going deeper into, add_subquery "
            "using that document's TITLE from the list.") in closed
    assert "read_document" not in closed

    tail = opened[opened.index("the document roster is the outline"):]
    tail = tail[:tail.index("Relevance search cannot substitute")]
    assert "for a row with NO stored summary, read_document that title" in tail
    assert "add_subquery using that document's TITLE from the list" in tail


def test_read_document_source_is_not_listed_among_the_scope_fields():
    """`read_document.source` **不**进 scope 字段清单:那份清单管的是「别把范围词
    写进检索串」,而这个字段填的是一个从清单里逐字复制的标题,不是检索串。"""
    from app.services.prompts import reflect_prompt

    prompt = reflect_prompt(
        "q", "c", element_kinds=("formula",), object_types=("claim",),
        read_document=True, read_document_cap=4)

    rule = prompt[prompt.index("This applies to every retrieval field you fill"):]
    rule = rule[:rule.index("\n")]
    assert "read_document" not in rule
    assert "new_sub_query.query" in rule


def test_plan_prompt_neutral_opening_matches_expand_query_prompts_framing():
    """两份规划拼写不得说出互相矛盾的计划(``plan_prompt`` 函数体前 NOTE 的
    「must be added to BOTH」纪律)。

    这次改的是**首句框架**而不是分解指导:production 实际发送的
    ``expand_query_prompt`` 首句本来就是 KG 中性的("retrieval over a document
    corpus"),所以无图态的 backup 拼写是在向它靠拢,而不是背离——这条用例把
    「两份拼写的框架句都不宣称必有一张图」钉住。
    """
    from app.services.prompts import expand_query_prompt, plan_prompt

    production = expand_query_prompt("q")
    assert production.startswith(
        "You prepare an engineer's question for retrieval over a document "
        "corpus.")
    # 判据是「不宣称去检索一张图」,不是「不出现 knowledge graph 这个词」——
    # 中性版首句正是靠"the library has no knowledge graph"这半句解释 `types`。
    assert "retrieve a knowledge graph" not in production
    assert "retrieve a knowledge graph" not in plan_prompt(
        "q", kg_available=False)
    assert "retrieve a knowledge graph" in plan_prompt("q")


# ---------------------------------------------------------------------------
# T4(reflect 插件动作):`external_rules` 两态。设计文档
# docs/superpowers/specs/2026-09-13-reflect-plugin-action-design_zh.md §6.2。


def test_answer_prompt_is_byte_identical_without_external_evidence():
    """闸关着时 `answer_prompt` 与接入这个特性之前逐字节相等——缺省值与显式
    False 都是同一份字符串,history/style/按节三种形状各钉一次(任一分支把规则
    句无条件拼进去就会红)。"""
    from app.services.prompts import answer_prompt

    for kwargs in (
        {},
        {"history_block": "User: 上一问\nAssistant: 上一答"},
        {"style_block": "[风格] 先给结论"},
        {"sectioned": True, "section_title": "第一节",
         "section_index": 1, "section_total": 3},
    ):
        default = answer_prompt("q?", "k1: [concept] X", **kwargs)
        explicit = answer_prompt(
            "q?", "k1: [concept] X", external_rules=False, **kwargs)
        assert default == explicit
        assert "[external · " not in default


def test_answer_prompt_appends_the_external_rule_only_when_asked():
    """开着时规则 14 落在规则 13 之后、history/Question 之前,并且它说的正是
    「可以像其它条目一样 [k] 引用,但不得表述为笔记本内容」。"""
    from app.services.prompts import answer_prompt

    off = answer_prompt("q?", "k6001: [external · IEEE Xplore] T — E")
    on = answer_prompt(
        "q?", "k6001: [external · IEEE Xplore] T — E", external_rules=True)

    assert "14. Items tagged [external · <source>]" in on
    assert "14. Items tagged" not in off
    # 承重的两半:可引用,且不得冒充笔记本内容。
    assert "Cite them with their [k] marker exactly like any other item" in on
    assert "never describe them as notebook/library content" in on

    rule_13_idx = on.index("13. Inference status propagates")
    rule_14_idx = on.index("14. Items tagged [external · <source>]")
    question_idx = on.index("Question: q?")
    assert rule_13_idx < rule_14_idx < question_idx

    # 开关只多这一条规则,别的什么都没动。
    assert off == on.replace(
        on[rule_14_idx:on.index("\n\n", rule_14_idx) + 1], "", 1
    )


def test_answer_prompt_external_rule_survives_history_and_style_blocks():
    from app.services.prompts import answer_prompt

    prompt = answer_prompt(
        "q?", "k6001: [external · X] T — E",
        history_block="User: 上一问",
        style_block="[风格] 先给结论",
        external_rules=True,
    )
    rule_14_idx = prompt.index("14. Items tagged [external · <source>]")
    assert rule_14_idx < prompt.index("User: 上一问")
    assert rule_14_idx < prompt.index("[风格] 先给结论")
