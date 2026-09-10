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


def _pd6_full_house_catalog():
    """P 臂 S golden 用的 full-house 目录:与
    ``test_reasoning_retrieval._full_house_facts`` 同构造(什么都开着、预算
    都还有),这里独立复制一份,避免跨测试文件引用另一个模块的私有 helper。
    """
    from app.services.collection_catalog import (
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES,
    )
    from app.services.reasoning_actions import (
        ReflectCapabilityFacts, build_reflect_capabilities,
    )
    facts = ReflectCapabilityFacts(
        kg_in_scope=True, scope_restricted=False, has_candidates=True,
        chunk_search_active=True, exact_lookup_active=True, ppr_active=True,
        community_active=True, enumeration_active=True,
        consult_memory_active=True, outline_active=True,
        element_searches_left=5, chunk_searches_left=3, exact_lookups_left=3,
        ppr_left=3, follow_chain_left=3, consult_left=2,
        outline_updates_left=6, enum_rows_left=200, enum_pages_left=4,
        enum_payload_left=256_000,
        element_kinds=tuple(ENUMERABLE_ELEMENT_KINDS),
        object_types=tuple(ENUMERABLE_KG_OBJECT_TYPES),
        last_turn=False, outline_repair_available=False,
        terminal_overflow_repair=False,
    )
    return build_reflect_capabilities(facts)


def test_reflect_v2_static_prompt_delta_false_is_byte_identical_to_omitting_it():
    """``delta`` 省略 == 显式传 `False`(独立的 ≥200 组随机
    ``ReflectCapabilityFacts`` 对 HEAD(38fc400f3)的比对见任务报告——仓库没有
    `git show` 型用例先例,这里只钉一个能在 CI 里跑的不变量)。

    这条抓的是**默认值**:变异把签名改成 `delta: bool = True` ⇒ 省略调用会带
    上 delta 文本、显式 `delta=False` 不会,两边不再相等,这条红(已实测)。
    去掉 `(_V2_DELTA_INSTRUCTION if delta else "")` 短路让它恒为真,是下一条
    `..._appears_only_when_requested` 抓的——那个变异下 `omitted`/`explicit`
    仍然相等(两边都恒定带上 delta 文本),这条不会红。
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

    变异:去掉 `(_V2_DELTA_INSTRUCTION if delta else "")` 的短路,让它恒为真
    ⇒ 这条红(`off` 也会带上 delta 文本,`phrase not in off` 先炸)。删第 (2)
    句(同一 `key` 多张卡的说明)⇒ 对应短语的正向断言红;把
    `_V2_DELTA_INSTRUCTION` 塞到 `_v2_action_lines` 之后 ⇒ 拼接位置断言红。
    """
    from app.services.prompts import (
        _V2_DELTA_INSTRUCTION, _V2_STATIC_CATALOG_INSTRUCTION,
        reflect_v2_static_prompt,
    )

    catalog = _pd6_catalog()
    off = reflect_v2_static_prompt(catalog)
    on = reflect_v2_static_prompt(catalog, delta=True)

    # (1) 追加式历史:不会就地改写一行,折叠成快照后那一行仍然发生过,推翻
    # 一行的权限只交给后来的服务端行 / 末尾块,证据卡永远没有这个权限。
    assert "never rewrites a line in place" in _V2_DELTA_INSTRUCTION
    assert "never removes" not in _V2_DELTA_INSTRUCTION
    assert "evidence card says otherwise" not in _V2_DELTA_INSTRUCTION
    # (2) 同一 key 多张卡 = 同一条证据的新摘录,不是新证据;版本标记紧跟在
    # key 那一格之后(与 Q3 的渲染位置对齐),前一张仍然有效。
    assert "fresh excerpt of the SAME evidence" in _V2_DELTA_INSTRUCTION
    assert "right after that key" in _V2_DELTA_INSTRUCTION
    assert "still valid" in _V2_DELTA_INSTRUCTION
    # (3) 两种披露分开说:未展开是"还剩多少",折算计数是"已经发生过什么"。
    assert "remain unshown" in _V2_DELTA_INSTRUCTION
    assert "already happened" in _V2_DELTA_INSTRUCTION
    # (4) 末尾块的执行限制依旧优先,且不扩大到该块其余部分——四类措辞与
    # `_V2_STATIC_CATALOG_INSTRUCTION` 同一口径,不得扩大;授予优先级的表述
    # 只出现一次,不能被追加的第五句悄悄放宽到整块。
    assert ("this turn's callable actions, the tools withheld and why, the "
            "current status of every mandatory aspect, and the keys of "
            "collections enumerated to completion") in _V2_STATIC_CATALOG_INSTRUCTION
    assert ("this turn's callable actions, the tools withheld and why, the "
            "current status of every mandatory aspect, and the keys of "
            "collections enumerated to completion") in _V2_DELTA_INSTRUCTION
    assert "does not reach the rest of that block" in _V2_DELTA_INSTRUCTION
    assert _V2_DELTA_INSTRUCTION.count("outrank") == 1

    for phrase in ("never rewrites a line in place",
                   "fresh excerpt of the SAME evidence",
                   "remain unshown", "already happened",
                   "does not reach the rest of that block"):
        assert phrase not in off, phrase
        assert phrase in on, phrase

    # 位置:紧跟 catalog 段之后、其余部分(动作清单起)逐字节不动。
    assert _V2_STATIC_CATALOG_INSTRUCTION + _V2_DELTA_INSTRUCTION in on
    assert on.replace(_V2_DELTA_INSTRUCTION, "", 1) == off


def test_reflect_v2_static_prompt_delta_instruction_rejects_semantic_regressions():
    """第 (2) 句的"前一张仍然有效"是绑定资格能继续成立的那半句,必须真的抓住
    语义反转,不能只断"提到了版本标记"。

    变异:把 `the earlier card under that key is still valid` 改成
    `…is superseded` ⇒ 这条红。
    """
    from app.services.prompts import _V2_DELTA_INSTRUCTION

    assert "the earlier card under that key is still valid" in (
        _V2_DELTA_INSTRUCTION)
    assert "superseded" not in _V2_DELTA_INSTRUCTION


def test_reflect_v2_static_prompt_delta_is_keyword_only():
    """``delta`` 是 keyword-only:生产唯一调用点也是按关键字传,位置传参必须
    在签名层面就被拒绝。

    变异:签名把 `*, delta: bool = False` 改成 `delta: bool = False`(去掉
    keyword-only 标记)⇒ 不再抛 `TypeError`,这条红。
    """
    import pytest
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    with pytest.raises(TypeError):
        reflect_v2_static_prompt(catalog, True)


def test_reflect_v2_static_prompt_delta_true_is_stable_across_repeated_calls():
    """``reflect_v2_static_prompt`` 是纯函数:同一份 `catalog` 反复调用、经
    生产 `provider_messages()` / `serialize_provider_messages()` 序列化后都
    必须逐字节相同——这是"S 在 run 内不随轮数变"要成立的前提,真正的多轮
    run 级验收(`system_prompt(turn)` 全等)留给 T-PD5;端到端(经真实
    `_GatedV2LLM` run)同样留给 T-PD5。

    变异:让 `reflect_v2_static_prompt` 在 `delta=True` 时插入任何非确定量
    (例如时间戳)⇒ `len(set(calls))` 与 `len(set(serialized))` 都变成 >1,
    这条红。
    """
    from app.core.llm import provider_messages, serialize_provider_messages
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    calls = [reflect_v2_static_prompt(catalog, delta=True) for _ in range(5)]
    assert len(set(calls)) == 1

    system_text = calls[0]
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
    # 经生产序列化(长度前缀原文帧)后,em-dash、弯引号、反引号都没有被转义
    # 打散——这是这条用例唯一不被前一半覆盖的断言。
    assert system_text.encode("utf-8") in serialized[0]


# ---------------------------------------------------------------------------
# 质量评审 P3-1:P 臂 S(``prefix_snapshot``/``prefix_delta`` 共用的静态目录
# 段)至今没有字节 golden——`test_reasoning_retrieval.py` 里 `off` 臂的那条
# golden(`test_off_v2_system_prompt_is_byte_frozen_against_a_golden`)不覆盖
# `_V2_STATIC_CATALOG_INSTRUCTION`,它是 P 臂独有的。改这段措辞、或改
# `_V2_DELTA_INSTRUCTION`,必须在同一个 diff 里改下面对应的 golden。
# ---------------------------------------------------------------------------

#: `reflect_v2_static_prompt(full_house_catalog, delta=False)` 的 golden。
_PD6_STATIC_P_LEN = 10851
_PD6_STATIC_P_SHA256 = (
    "c0186cad0daef780103a58739622e0f62cf78770d7799ca3e8550df7d4bb12ee")

#: `reflect_v2_static_prompt(full_house_catalog, delta=True)` 的 golden——
#: `delta=False` 的输出之后紧跟 `_V2_DELTA_INSTRUCTION`。
_PD6_STATIC_DELTA_LEN = 12358
_PD6_STATIC_DELTA_SHA256 = (
    "cf53d6ad1b2f545a2b6aee5bf30163cc878d2c52cf3c45c7ab3a8a1c50bf8ead")


def test_reflect_v2_static_prompt_p_arm_delta_false_is_byte_frozen_against_a_golden():
    """P 臂 S(``delta=False``)= 这一串确定的字节(质量评审 P3-1)。

    变异:改 `_V2_STATIC_CATALOG_INSTRUCTION` 一个词 ⇒ 这条红。
    """
    import hashlib
    from app.services.prompts import reflect_v2_static_prompt

    text = reflect_v2_static_prompt(_pd6_full_house_catalog(), delta=False)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (len(text), digest) == (_PD6_STATIC_P_LEN, _PD6_STATIC_P_SHA256), (
        "P 臂(prefix_snapshot/prefix_delta 共用)的静态目录段变了。改动 "
        "`_V2_STATIC_CATALOG_INSTRUCTION` 必须在同一个 diff 里改这里的 "
        f"golden。实测长度={len(text)} sha256={digest}"
    )


def test_reflect_v2_static_prompt_p_arm_delta_true_is_byte_frozen_against_a_golden():
    """P 臂 S(``delta=True``)钉住 `delta=False` 的 golden 之后紧跟
    `_V2_DELTA_INSTRUCTION` 那份完整拼接——同一份 golden 机制,另外钉住
    delta 臂独有的四句。

    变异:改 `_V2_DELTA_INSTRUCTION` 任意一句 ⇒ 这条红。
    """
    import hashlib
    from app.services.prompts import reflect_v2_static_prompt

    text = reflect_v2_static_prompt(_pd6_full_house_catalog(), delta=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (len(text), digest) == (
        _PD6_STATIC_DELTA_LEN, _PD6_STATIC_DELTA_SHA256), (
        "prefix_delta 的静态目录段(含四句)变了。实测长度="
        f"{len(text)} sha256={digest}"
    )


# ---------------------------------------------------------------------------
# T-PL3(reflect 前缀复用 PR-4 lean 计划 §3):``reflect_v2_static_prompt`` 加
# ``lean``。``off``/``prefix_snapshot``/``prefix_delta`` 从不传 ``lean=True``,
# 所以这里的第一条判据与 T-PD6 的 ``delta`` 完全同构:省略或显式 ``False`` 必须
# 与这个形参加入之前逐字节相同——四臂字节等价面之一(计划 §5 风险 1)。
# ---------------------------------------------------------------------------

def test_reflect_v2_static_prompt_lean_false_is_byte_identical_to_omitting_it():
    """``lean`` 省略 == 显式传 `False`,且与不传 ``lean`` 参数时完全相同——这是
    「默认值中性」的直接证据(计划 §1 M4)。

    变异:把签名改成 `lean: bool = True` ⇒ 省略调用会换上 lean 段、显式
    `lean=False` 不会,两边不再相等,这条红。
    """
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    for delta in (False, True):
        omitted = reflect_v2_static_prompt(catalog, delta=delta)
        explicit_false = reflect_v2_static_prompt(catalog, delta=delta, lean=False)
        assert omitted == explicit_false
        assert omitted


def test_reflect_v2_static_prompt_lean_replaces_not_appends_the_assessment_paragraph():
    """``lean=True`` **替换** `_V2_ASSESSMENT_INSTRUCTION`,不追加——两份自评
    合同互相矛盾(整轮重述 vs 只报变化),同一份系统提示词里两句都在是最坏形态
    (计划 §5 风险 2)。

    变异:把 `reflect_v2_static_prompt` 的选择表达式从
    `(_V2_LEAN_ASSESSMENT_INSTRUCTION if lean else _V2_ASSESSMENT_INSTRUCTION)`
    改成 `_V2_ASSESSMENT_INSTRUCTION + (_V2_LEAN_ASSESSMENT_INSTRUCTION if lean
    else "")`(追加式)⇒ 这条红:旧段的关键短语在 lean 输出里也会出现。
    """
    from app.services.prompts import (
        _V2_ASSESSMENT_INSTRUCTION, _V2_LEAN_ASSESSMENT_INSTRUCTION,
        reflect_v2_static_prompt,
    )

    catalog = _pd6_catalog()
    off = reflect_v2_static_prompt(catalog)
    on = reflect_v2_static_prompt(catalog, lean=True)

    # 旧段的独有短语(整轮重述 + 整份 WHOLE 拒绝的恐吓句)必须只在 off 出现。
    assert "fill `assessment` for the aspects you can judge now" in off
    assert "fill `assessment` for the aspects you can judge now" not in on
    assert "rejected WHOLE" in off
    assert "rejected WHOLE" not in on
    # lean 段的独有短语必须只在 lean 输出出现。
    assert "report only what CHANGED" not in off
    assert "report only what CHANGED" in on
    # 两份合同互斥:旧段与新段不能同时出现在同一份文本里。
    assert _V2_ASSESSMENT_INSTRUCTION not in on
    assert _V2_LEAN_ASSESSMENT_INSTRUCTION not in off
    # 替换发生在同一个槽位:除了这一段,其余部分逐字节不变。
    assert on.replace(_V2_LEAN_ASSESSMENT_INSTRUCTION, "", 1) == off.replace(
        _V2_ASSESSMENT_INSTRUCTION, "", 1)


def test_reflect_v2_lean_assessment_instruction_states_the_five_rules():
    """五句关键短语(计划 T-PL3 要点)逐条都在:(1) 只报变化/省略保留/不追问;
    (2) 收尾轮同一份 JSON 给最终变化与缺口/不为记账退回一轮;(3) 字段与上界
    照旧;(4) 独立校验/只作废那一个方面/整轮才作废的三个条件;(5) 方面清单
    只能用户改/省略不等于证据不存在。

    变异:删掉任意一句的关键短语 ⇒ 对应断言红。
    """
    from app.services.prompts import _V2_LEAN_ASSESSMENT_INSTRUCTION as lean

    # (1) 只报本轮变化;省略的方面保留服务端记着的状态;已支撑项不必重述;
    # 省略不花代价也不会被追问。
    assert "report only what CHANGED" in lean
    assert "keeps the status the server already has for it" in lean
    assert "does not need restating" in lean
    assert "costs nothing" in lean
    assert "will not be chased with a follow-up question" in lean
    # (2) 收尾轮(answer 或 sufficient=true)同一份 JSON 给出最终变化与缺口;
    # 没走到的方面留着不评;服务端不会为补齐账目退回一轮。
    assert "closing turn" in lean
    assert "next_action is answer, or sufficient is true" in lean
    assert "final changes and gaps you can judge as of this turn" in lean
    assert "stays unassessed" in lean
    assert "will not send you back for another turn just to square the ledger" in lean
    # (3) 字段与上界照旧。
    assert "Fields and bounds are unchanged" in lean
    # (4) 独立校验;只作废那一个方面(保留旧状态、下一轮状态块告知原因);
    # 检索动作照常执行;只有整份读不出归属才作废整轮。
    assert "validated independently" in lean
    assert "invalidates only THAT aspect" in lean
    assert "keeps its prior status and next turn's status block tells you why" in lean
    assert "this turn's retrieval action, still go through as normal" in lean
    assert "cannot attribute at all" in lean
    assert "invalidates the whole turn" in lean
    # (5) 方面清单只能由用户改;省略不等于「这条证据不存在」。
    assert "that list comes from the user and only the user changes it" in lean
    assert "never that the evidence for it does not exist" in lean


def test_reflect_v2_lean_assessment_instruction_bounds_share_the_protocol_constants():
    """三个上界数字(status 枚举、每方面证据键数、gap 字符数上限)与协议常量
    同源插值,不是手抄的字面量——改常量,文案跟着变(计划 T-PL3 要点)。

    变异:把插值换成手写数字 ⇒ 改常量后这条红(字面量与常量不再一致)。
    """
    from app.domain.retrieval_termination import (
        ASPECT_UNRESOLVED_STATUSES, REFLECT_ASPECT_GAP_MAX_CHARS,
        REFLECT_ASPECT_MAX_EVIDENCE_KEYS,
    )
    from app.services.prompts import _V2_LEAN_ASSESSMENT_INSTRUCTION as lean

    assert f"`{'|'.join(ASPECT_UNRESOLVED_STATUSES)}`" in lean
    assert f"{REFLECT_ASPECT_MAX_EVIDENCE_KEYS} evidence keys" in lean
    assert f"{REFLECT_ASPECT_GAP_MAX_CHARS} characters of gap" in lean


def test_reflect_v2_static_prompt_lean_is_keyword_only():
    """``lean``(``delta`` 同款)是 keyword-only:位置传参必须在签名层面就被拒绝。

    变异:签名把 `lean: bool = False` 改成不带 `*` 的位置参数 ⇒ 不再抛
    `TypeError`,这条红。
    """
    import pytest
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    with pytest.raises(TypeError):
        reflect_v2_static_prompt(catalog, False, True)  # delta, lean 都位置传


def test_reflect_v2_static_prompt_lean_true_is_stable_across_repeated_calls():
    """``reflect_v2_static_prompt(..., lean=True, delta=True)`` 是纯函数:同一份
    ``catalog`` 反复调用、经生产 ``provider_messages()`` /
    ``serialize_provider_messages()`` 序列化后都必须逐字节相同——这是"S 在 run
    内不随轮数变"要成立的前提。

    这条覆盖用例 (d):过 `_GatedV2LLM` 一次完整 L run 需要 `prefix_delta_lean`
    先放行(T-PL1,本分支未落地),这里改为直接构造 ``catalog`` 调
    `reflect_v2_static_prompt(catalog, lean=True, delta=True)` 并经生产序列化
    路径验证稳定性——完整端到端 run(经真实 `_GatedV2LLM`、四臂消息序列)留给
    T-PL5。

    变异:让 lean 段在渲染时插入任何非确定量 ⇒ `len(set(calls))` 与
    `len(set(serialized))` 都变成 >1,这条红。
    """
    from app.core.llm import provider_messages, serialize_provider_messages
    from app.services.prompts import reflect_v2_static_prompt

    catalog = _pd6_catalog()
    calls = [
        reflect_v2_static_prompt(catalog, lean=True, delta=True)
        for _ in range(5)
    ]
    assert len(set(calls)) == 1

    system_text = calls[0]
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


#: `reflect_v2_static_prompt(full_house_catalog, delta=False, lean=True)` 的
#: golden(用例 (f),照 P 臂 golden 先例)。
_PL3_STATIC_LEAN_LEN = 11453
_PL3_STATIC_LEAN_SHA256 = (
    "01be5fcaa90c487223d895d8a8dbca7780a6244373b634cadea2462a85de12c4")

#: `reflect_v2_static_prompt(full_house_catalog, delta=True, lean=True)` 的
#: golden——生产唯一真实组合(`prefix_delta_lean` 是 `prefix_delta` + lean 段)。
_PL3_STATIC_DELTA_LEAN_LEN = 12960
_PL3_STATIC_DELTA_LEAN_SHA256 = (
    "2deb65442492e7d8f5264dcd495fc2ac38c862cd5f7e7e5a44b40e07e8377af1")


def test_reflect_v2_static_prompt_lean_arm_delta_false_is_byte_frozen_against_a_golden():
    """L 臂 S(``delta=False``, ``lean=True``)= 这一串确定的字节。

    变异:改 `_V2_LEAN_ASSESSMENT_INSTRUCTION` 一个词 ⇒ 这条红。
    """
    import hashlib
    from app.services.prompts import reflect_v2_static_prompt

    text = reflect_v2_static_prompt(
        _pd6_full_house_catalog(), delta=False, lean=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (len(text), digest) == (
        _PL3_STATIC_LEAN_LEN, _PL3_STATIC_LEAN_SHA256), (
        "lean 自评段(delta=False 组合)变了。实测长度="
        f"{len(text)} sha256={digest}"
    )


def test_reflect_v2_static_prompt_lean_arm_delta_true_is_byte_frozen_against_a_golden():
    """``prefix_delta_lean`` 生产组合(``delta=True``, ``lean=True``)= 这一串
    确定的字节——`_prefix_context` 建 S 时唯一会用到的一组参数(T-PL5)。

    变异:改 `_V2_DELTA_INSTRUCTION` 或 `_V2_LEAN_ASSESSMENT_INSTRUCTION` 任一
    句 ⇒ 这条红。
    """
    import hashlib
    from app.services.prompts import reflect_v2_static_prompt

    text = reflect_v2_static_prompt(
        _pd6_full_house_catalog(), delta=True, lean=True)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert (len(text), digest) == (
        _PL3_STATIC_DELTA_LEAN_LEN, _PL3_STATIC_DELTA_LEAN_SHA256), (
        "prefix_delta_lean 的静态目录段(含 delta 四句 + lean 自评段)变了。"
        f"实测长度={len(text)} sha256={digest}"
    )
