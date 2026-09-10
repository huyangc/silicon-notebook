from app.services.prompts import (
    answer_prompt,
    retrieval_experience_prompt,
    ANSWER_SCHEMA_HINT,
)
import json


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
# T-PD6:``reflect_v2_static_prompt`` 的 ``prefix_delta`` 四句(reflect 前缀复用
# PR-3 计划 §3 T-PD6)。``off``/``prefix_snapshot`` 从不传 ``delta=True``,所以
# 这里的判据只有一条:``delta`` 省略或显式 ``False`` 时,返回值必须与这个形参
# 加入之前逐字节相同——风险 §5.1 点名的两臂字节等价面之一。
# ---------------------------------------------------------------------------

def _pd6_catalog():
    from app.services.reasoning_actions import (
        ReflectCapabilityFacts, build_reflect_capabilities,
    )
    return build_reflect_capabilities(ReflectCapabilityFacts())


def test_reflect_v2_static_prompt_delta_false_is_byte_identical_to_omitting_it():
    """``delta`` 省略 == 显式传 `False`,且与该形参加入前的实现逐字节相同(独立
    的 ≥200 组随机 ``ReflectCapabilityFacts`` 对 HEAD(38fc400f3)的比对见任务
    报告——仓库没有 `git show` 型用例先例,这里只钉一个能在 CI 里跑的不变量)。

    变异:去掉 `(_V2_DELTA_INSTRUCTION if delta else "")` 的短路,让它恒为真
    ⇒ 这条红。
    """
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    omitted = reflect_v2_static_prompt(catalog)
    explicit = reflect_v2_static_prompt(catalog, delta=False)
    assert omitted == explicit
    assert omitted                                # 反面:真的渲染出了内容


def test_reflect_v2_static_prompt_delta_instruction_appears_only_when_requested():
    """四句关键短语只在 `delta=True` 出现,且紧跟在 `_V2_STATIC_CATALOG_INSTRUCTION`
    之后、动作清单之前——不是散落在别处或与目录段之间夹了别的文本。

    变异:删第 (2) 句(同一 `key` 多张卡的说明)⇒ 对应短语的正向断言红;把
    `_V2_DELTA_INSTRUCTION` 塞到 `_v2_action_lines` 之后 ⇒ 拼接位置断言红。
    """
    from app.services.prompts import (
        _V2_DELTA_INSTRUCTION, _V2_STATIC_CATALOG_INSTRUCTION,
        reflect_v2_static_prompt,
    )

    catalog = _pd6_catalog()
    off = reflect_v2_static_prompt(catalog)
    on = reflect_v2_static_prompt(catalog, delta=True)

    # (1) append-only 历史块。
    assert "APPEND-ONLY" in _V2_DELTA_INSTRUCTION
    # (2) 同一 key 多张卡 = 同一条证据的新摘录,不是新证据。
    assert "fresh excerpt of the SAME evidence" in _V2_DELTA_INSTRUCTION
    # (3) 折算计数/「N 条未展开」是披露还有多少,不是没找到。
    assert "discloses HOW MANY items" in _V2_DELTA_INSTRUCTION
    # (4) 末尾块的执行限制依旧优先,且不扩大到该块其余部分——四类措辞与
    # `_V2_STATIC_CATALOG_INSTRUCTION` 同一口径,不得扩大。
    assert ("this turn's callable actions, the tools withheld and why, the "
            "current status of every mandatory aspect, and the keys of "
            "collections enumerated to completion") in _V2_STATIC_CATALOG_INSTRUCTION
    assert ("this turn's callable actions, the tools withheld and why, the "
            "current status of every mandatory aspect, and the keys of "
            "collections enumerated to completion") in _V2_DELTA_INSTRUCTION
    assert "does not reach the rest of that block" in _V2_DELTA_INSTRUCTION

    for phrase in ("APPEND-ONLY", "fresh excerpt of the SAME evidence",
                   "discloses HOW MANY items",
                   "does not reach the rest of that block"):
        assert phrase not in off, phrase
        assert phrase in on, phrase

    # 位置:紧跟 catalog 段之后、其余部分(动作清单起)逐字节不动。
    assert _V2_STATIC_CATALOG_INSTRUCTION + _V2_DELTA_INSTRUCTION in on
    assert on.replace(_V2_DELTA_INSTRUCTION, "", 1) == off


def test_reflect_v2_static_prompt_delta_true_is_stable_across_repeated_calls():
    """T-PD5(重建/接线)未落地,这里过不了一次完整多轮 run;但
    `reflect_v2_static_prompt` 是纯函数,同一份 `catalog` 反复调用必须逐字节
    相同——这正是"S 在 run 内不随轮数变"要成立的前提,真正的多轮 run 级验收
    (`system_prompt(turn)` 全等)留给 T-PD5。
    """
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    calls = [reflect_v2_static_prompt(catalog, delta=True) for _ in range(5)]
    assert len(set(calls)) == 1


def test_reflect_v2_static_prompt_delta_survives_provider_serialization_stably():
    """T-PD6 用例 (c) 的可行版本:`_GatedV2LLM` 一次完整 delta run 本任务不可行
    (T-PD1 的 `prefix_delta` 枚举、T-PD5 的接线都还没落地),改为直接构造
    `catalog`、渲染 `delta=True` 的 S,经生产 `provider_messages()` /
    `serialize_provider_messages()` 走一遍真实序列化,断多次调用逐字节稳定。
    端到端(经真实 `_GatedV2LLM` run)留给 T-PD5。

    变异:让 `reflect_v2_static_prompt` 在 `delta=True` 时插入任何非确定量
    (例如时间戳)⇒ `len(set(serialized))` 变成 3,这条红。
    """
    from app.core.llm import provider_messages, serialize_provider_messages
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    system_text = reflect_v2_static_prompt(catalog, delta=True)
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "[Question]\nq?\n\nReturn JSON only."},
    ]
    schema_hint = '{"next_action": ""}'

    serialized = [
        serialize_provider_messages(provider_messages(messages, schema_hint))
        for _ in range(3)
    ]
    assert len(set(serialized)) == 1
    assert system_text.encode("utf-8") in serialized[0]
