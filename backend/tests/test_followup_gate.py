"""`followup_gate` —— 跟进问句的共用澄清闸(PR-C T1)。

这个纯函数把「先判原句、必要时判改写句、文案恒取自原句」这三条合成一处,供引擎
兼容分支、HTTP 入口与 MCP 入口共用。四条红线各有一条用例钉住:

1. 没有改写(`resolved` 空或等于原句)时,结论与今天直接调 `plan_query_intent` +
   `clarification_gate_message` 的路径逐字节相同;
2. 改写句清晰时放行,`resolved_question` 换成改写句;
3. 改写句仍模糊时命中,**错误文案来自原句 seed**——模型产物绝不进错误串;
4. 改写丢掉「全部」不得把 complete 静默降级成 ranked。
"""

from app.services.query_intent import (
    clarification_gate_message,
    followup_gate,
    plan_query_intent,
)


def _today(question: str, history: str = "") -> tuple[dict | None, str]:
    """今天(闸抽出来之前)那条路径的结论,逐字重写一遍作为对照。"""
    seed = plan_query_intent(None, question, history, max_topics=4)
    if seed.get("needs_clarification"):
        return None, clarification_gate_message(seed)
    return seed, ""


def test_no_rewrite_keeps_todays_verdict_byte_for_byte():
    """`resolved` 空、或等于原句,两种输入都退化成今天的单句判定。

    放行与命中各取一例:只钉命中那半边的话,一个恒命中的实现也能全绿。
    """
    clear = "RTL到GDSII流程"
    ambiguous = "分析一下"

    for question in (clear, ambiguous):
        expected_seed, expected_message = _today(question)
        for resolved in ("", question, f"  {question}  "):
            seed, message = followup_gate(question, resolved, "", max_topics=4)
            assert seed == expected_seed, (question, resolved)
            assert message == expected_message, (question, resolved)

    # 空转保护:上面两个问句确实分别落在放行与命中两侧。
    assert _today(clear)[1] == ""
    assert _today(ambiguous)[0] is None
    assert _today(ambiguous)[1].startswith("问题仍有关键歧义，请先确认问题理解：")


def test_a_clear_rewrite_opens_the_gate_and_supplies_resolved_question():
    """原句只有指代、改写句点名对象 → 放行,且检索用的是改写句。

    `objective` 仍是用户原话(落库与会话 turn 用它),`result_scope` 之类的判定
    一律留在原句 seed 上——改写句只负责补回指代。
    """
    original = "它的参数呢"
    rewritten = "set_db 的参数有哪些"
    original_seed = plan_query_intent(None, original, "", max_topics=4)
    assert original_seed["needs_clarification"] is True  # 原句独自过不了闸

    seed, message = followup_gate(original, rewritten, "", max_topics=4)

    assert message == ""
    assert seed is not None
    assert seed["resolved_question"] == rewritten
    assert seed["ambiguities"] == []
    assert seed["needs_clarification"] is False
    assert seed["objective"] == original
    assert seed["result_scope"] == original_seed["result_scope"]
    assert seed["completeness_required"] == original_seed["completeness_required"]


def test_a_still_ambiguous_rewrite_fails_closed_with_the_original_copy():
    """改写句仍然模糊 → 命中,文案逐字等于原句 seed 的文案。

    两句触发的是**不同**的确定性文案(原句撞 `_GENERIC_REQUEST`,改写句撞
    `_UNRESOLVED_REFERENCE`),所以「取原句还是取改写句」在这里可分辨——文案改取
    probe seed 时这条用例立刻红。
    """
    original = "分析一下"
    rewritten = "分析一下这个模块的实现"
    expected = clarification_gate_message(
        plan_query_intent(None, original, "", max_topics=4)
    )
    probe_message = clarification_gate_message(
        plan_query_intent(None, rewritten, "", max_topics=4)
    )
    assert probe_message != expected  # 空转保护:两侧文案确实不同

    seed, message = followup_gate(original, rewritten, "", max_topics=4)

    assert seed is None
    assert message == expected
    assert "你希望分析的具体对象" in message
    assert "你提到的对象具体是什么" not in message
    # 模型改写产物的任何片段都不得出现在用户看到的错误串里。
    assert "模块" not in message
    assert rewritten not in message


def test_a_rewrite_that_drops_all_does_not_downgrade_the_scope():
    """原句要「全部」、改写句丢了「全部」→ 放行,但范围仍按原句 complete。

    改写是为了补指代,不是重新协商交付范围;`result_scope` 取 probe 就会把一次
    完整枚举静默降级成 top-N。
    """
    original = "列出全部支持的接口"
    rewritten = "支持的接口有哪些"
    original_seed = plan_query_intent(None, original, "", max_topics=4)
    probe_seed = plan_query_intent(None, rewritten, "", max_topics=4)
    assert original_seed["result_scope"] == "complete"
    assert probe_seed["result_scope"] == "ranked"  # 空转保护:两侧范围确实不同

    seed, message = followup_gate(original, rewritten, "", max_topics=4)

    assert message == ""
    assert seed is not None
    assert seed["resolved_question"] == rewritten
    assert seed["result_scope"] == "complete"
    assert seed["completeness_required"] is True


def test_an_over_long_rewrite_is_a_failed_rewrite_not_a_shorter_one():
    """改写句超过合同上限(`RESOLVED_QUESTION_MAX_CHARS`)→ 按「没有改写」处理。

    `QueryIntentContract.resolved_question` 的 `max_length=4000` 在合同装配时才
    校验;入口闸若放行一条 4001 字的改写句,引擎会在 durable job 建好之后抛
    `ValidationError`,而那串异常文本里带着改写产物。这里钉住:超长改写等于改写
    失败,结论逐字节回到原句 seed 的判定——模糊原句仍 422 且文案来自原句,清晰
    原句照常放行且 `resolved_question` 不是那条超长句。
    """
    from app.services.query_intent import RESOLVED_QUESTION_MAX_CHARS

    too_long = "set_db 的参数有哪些" + "详" * RESOLVED_QUESTION_MAX_CHARS
    assert len(too_long) > RESOLVED_QUESTION_MAX_CHARS

    ambiguous = "它的参数呢"
    seed, message = followup_gate(ambiguous, too_long, "", max_topics=4)
    assert (seed, message) == _today(ambiguous)
    assert seed is None and "详详详" not in message

    clear = "RTL到GDSII流程"
    seed, message = followup_gate(clear, too_long, "", max_topics=4)
    assert (seed, message) == _today(clear)
    assert seed is not None and seed["resolved_question"] != too_long

    # 空转保护:同一条改写句只要在上限之内就真的会被采用。
    within = too_long[:RESOLVED_QUESTION_MAX_CHARS]
    seed, message = followup_gate(ambiguous, within, "", max_topics=4)
    assert message == "" and seed["resolved_question"] == within

