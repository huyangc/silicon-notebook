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
