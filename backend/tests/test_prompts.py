from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
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
