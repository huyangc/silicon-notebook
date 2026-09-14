import json

import pytest
from pydantic import ValidationError

from app.core.ask_retrieval_policy import (
    ASK_RETRIEVAL_LIMITS,
    RETRIEVAL_EFFORTS,
    ask_retrieval_limits,
)
from app.models.ask import (
    AskIntentConfirmation,
    AskRequest,
    QueryIntentAnswer,
    QueryIntentContract,
)
from app.services.query_intent import (
    clarification_gate_message,
    confirmed_intent_queries,
    confirmed_research_question,
    finalize_query_intent,
    plan_query_intent,
)


def test_understanding_status_is_absent_from_wire_and_openapi_schema():
    contract = QueryIntentContract(objective="q", resolved_question="q")
    contract._understanding_succeeded = False

    assert "understanding_succeeded" not in contract.model_dump()
    assert "understanding_succeeded" not in str(
        QueryIntentContract.model_json_schema()
    )


class _IntentClient:
    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        assert "before seeing any corpus" in messages[-1]["content"]
        assert "mandatory_topics" in schema_hint
        assert '"result_scope"' in schema_hint
        assert '"completeness_required"' in schema_hint
        return json.dumps({
            "normalized_question": "比较 PLL A 与 PLL B 的锁定时间和抖动",
            "intent_type": "compare",
            "result_scope": "ranked",
            "completeness_required": False,
            "entities": ["PLL A", "PLL B"],
            "mandatory_topics": [{
                "title": "锁定时间",
                "question": "两者锁定时间如何比较？",
                "retrieval_queries": ["PLL A lock time", "PLL B lock time"],
            }],
            "comparison_axes": ["锁定时间", "抖动"],
            "constraints": ["相同工艺"],
            "ambiguities": [],
            "confidence": 0.91,
            "needs_clarification": False,
        })


class _IncompleteReferentClient:
    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        return json.dumps({
            "normalized_question": "锁定时间是多少？",
            "intent_type": "diagnose",
            "entities": [],
            "mandatory_topics": [],
            "ambiguities": [],
            "confidence": 0.9,
            "needs_clarification": False,
        })


def test_query_intent_is_corpus_blind_and_bounded():
    contract = plan_query_intent(
        _IntentClient(), "比较两个 PLL", max_topics=4,
        purpose="step-by-step evidence-grounded answer",
    )

    assert contract["resolved_question"].startswith("比较 PLL A")
    assert contract["entities"] == ["PLL A", "PLL B"]
    assert contract["mandatory_topics"][0]["id"] == "intent-1"
    assert contract["needs_clarification"] is False
    assert contract["confirmed"] is False
    assert contract["result_scope"] == "ranked"
    assert contract["completeness_required"] is False
    assert "source_refs" not in contract


def test_query_intent_does_not_treat_string_false_as_true():
    class _StringBooleanClient(_IntentClient):
        def chat_json(self, messages, schema_hint, **kwargs):
            data = json.loads(super().chat_json(messages, schema_hint, **kwargs))
            data["needs_clarification"] = "false"
            return json.dumps(data)

    contract = plan_query_intent(_StringBooleanClient(), "比较两个 PLL")

    assert contract["needs_clarification"] is False
    assert contract["ambiguities"] == []


@pytest.mark.parametrize(
    ("question", "scope"),
    [
        ("这个 knowhow 表格里所有方法有哪些？", "complete"),
        ("List every method in the table.", "complete"),
        ("这些方法一共有多少种？", "aggregate"),
        ("列出所有方法，并比较各自优缺点", "hybrid"),
    ],
)
def test_explicit_full_collection_wording_cannot_fall_back_to_ranked_top_n(
    question, scope
):
    contract = plan_query_intent(None, question)

    assert contract["result_scope"] == scope
    assert contract["completeness_required"] is True


def test_explicit_non_complete_wording_does_not_force_collection_scan():
    contract = plan_query_intent(None, "不需要所有方法，只给最相关的几个")

    assert contract["result_scope"] == "ranked"
    assert contract["completeness_required"] is False

    ranked = plan_query_intent(None, "并非必须列出所有方法，只给最相关的")
    assert ranked["result_scope"] == "ranked"
    assert ranked["completeness_required"] is False

    ownership = plan_query_intent(None, "解释所有权问题")
    assert ownership["result_scope"] == "ranked"

    scalar = plan_query_intent(None, "电源电压是多少？")
    assert scalar["result_scope"] == "ranked"

    for question in (
        "有哪些统计方法适合小样本？",
        "请解释统计方法的适用范围",
        "评估数据完整性的方法有哪些？",
    ):
        contract = plan_query_intent(None, question)
        assert contract["result_scope"] == "ranked", question
        assert contract["completeness_required"] is False, question


@pytest.mark.parametrize(
    ("question", "scope"),
    [
        ("介绍数量控制方法的适用范围", "ranked"),
        ("不是所有方法都适用，请列出所有方法", "complete"),
        ("列出每种方法", "complete"),
        ("统计各方法的数量并比较优缺点", "hybrid"),
    ],
)
def test_collection_scope_is_classified_per_instruction_clause(question, scope):
    contract = plan_query_intent(None, question)
    assert contract["result_scope"] == scope
    assert contract["completeness_required"] is (scope != "ranked")


def _scope_client(scope, *, completeness=True, confidence=0.9):
    class _Client:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "result_scope": scope,
                "completeness_required": completeness,
                "confidence": confidence,
                "intent_type": "other",
                "normalized_question": "解释统计方法的适用范围",
                "mandatory_topics": [], "ambiguities": [],
                "needs_clarification": False,
            })

    return _Client()


def test_the_model_decides_the_scope_when_the_wording_does_not():
    """Harness principle (user decision, 2026-09-14): the model owns the
    classification; without explicit full-set wording its widening stands
    when the reply is consistent and confident enough."""
    for scope in ("aggregate", "complete", "hybrid"):
        status: dict = {}
        contract = plan_query_intent(
            _scope_client(scope), "解释统计方法的适用范围", status=status,
        )
        assert contract["result_scope"] == scope
        assert contract["completeness_required"] is True
        assert status["scope_source"] == "model"


def test_a_guessed_or_inconsistent_widening_falls_back_to_ranked():
    # Low confidence: the expensive executor is not chosen on a guess.
    status: dict = {}
    contract = plan_query_intent(
        _scope_client("aggregate", confidence=0.3), "解释统计方法的适用范围",
        status=status,
    )
    assert contract["result_scope"] == "ranked"
    assert status["scope_source"] == "default"
    # Self-contradicting reply: non-ranked scope with completeness_required=false.
    contract = plan_query_intent(
        _scope_client("complete", completeness=False), "解释统计方法的适用范围",
    )
    assert contract["result_scope"] == "ranked"
    # Missing confidence counts as 0.
    contract = plan_query_intent(
        _scope_client("hybrid", confidence=None), "解释统计方法的适用范围",
    )
    assert contract["result_scope"] == "ranked"


def test_explicit_wording_bounds_the_model_in_both_directions():
    # Explicit full-set wording widens even when the model says ranked.
    status: dict = {}
    contract = plan_query_intent(
        _scope_client("ranked", completeness=False), "列出所有方法", status=status,
    )
    assert contract["result_scope"] == "complete"
    assert status["scope_source"] == "lexical"
    # An explicit refusal of the full set caps a confident model at ranked.
    status = {}
    contract = plan_query_intent(
        _scope_client("complete"), "不需要所有方法，只给最相关的几个", status=status,
    )
    assert contract["result_scope"] == "ranked"
    assert contract["completeness_required"] is False
    assert status["scope_source"] == "lexical"


def test_model_scope_is_bounded_and_non_ranked_scope_requires_completeness():
    seed = plan_query_intent(_IntentClient(), "比较两个 PLL")
    seed["result_scope"] = "aggregate"
    seed["completeness_required"] = False

    contract = QueryIntentContract(**seed)

    assert contract.result_scope == "aggregate"
    assert contract.completeness_required is True

    model_requests_completeness = QueryIntentContract(
        **{**seed, "result_scope": "ranked", "completeness_required": True}
    )
    assert model_requests_completeness.result_scope == "complete"


def test_ask_retrieval_effort_protocol_defaults_and_rejects_unknown_ids():
    assert AskRequest(question="q").retrieval_effort == "standard"
    assert AskRequest(question="q", retrieval_effort="exhaustive").retrieval_effort == "exhaustive"
    with pytest.raises(ValidationError):
        AskRequest(question="q", retrieval_effort="maximum")


def test_ask_retrieval_threshold_table_is_complete_monotonic_and_exact():
    assert tuple(ASK_RETRIEVAL_LIMITS) == RETRIEVAL_EFFORTS
    limits = [ask_retrieval_limits(effort) for effort in RETRIEVAL_EFFORTS]
    # 每一行都知道自己是哪一档:只在某一档提供的能力(逐步推理的大纲便签只在
    # exhaustive 开放)拿这个字段当闸,而 run() 手里除了这一行就没有别的档位信息。
    # 若改成从预算数字反推,任何一次 `replace(limits, 某预算=…)` 都会静默换档。
    for effort, row in zip(RETRIEVAL_EFFORTS, limits):
        assert row.effort == effort
    increasing_fields = (
        "ranked_final_floor",
        "ranked_per_aspect",
        "ranked_final_cap",
        "max_reasoning_steps",
        "max_initial_subqueries",
        "kg_context_chars",
        "chunk_context_chars",
        "answer_element_items",
        "enum_pages_per_run",
        "enum_rows_per_run",
    )
    for field in increasing_fields:
        values = [getattr(row, field) for row in limits]
        assert values == sorted(values), field
        assert len(set(values)) == len(values), field
    nondecreasing_fields = ("ranked_per_query_take",)
    for field in nondecreasing_fields:
        values = [getattr(row, field) for row in limits]
        assert values == sorted(values), field
    for row in limits:
        assert row.structured_page_size * row.structured_max_pages == row.structured_max_rows
        assert row.structured_page_size == 25
        assert row.structured_max_pages == 50
        assert row.structured_max_rows == 1_250
        assert row.structured_max_tables == 8
        assert row.structured_max_columns == 8
        assert row.cell_excerpt_chars == 1_000
        assert row.structured_payload_chars == 256_000
        assert row.inline_answer_rows == 100
        # 页大小是往返批量,不随档位变(与 structured_page_size 同口径)。
        assert row.enum_page_size == 50
        # 三个 enum 字段互相自洽:每 run 行数 = 页大小 × 每 run 额外页数。run()
        # 正是靠这条恒等式用「扫过的行数 ÷ 页大小」给额外翻页计费,改坏一个数就
        # 会让两个池不再同时耗尽。
        assert row.enum_page_size * row.enum_pages_per_run == row.enum_rows_per_run
        assert row.overflow_semantics == "explicit_partial"
    assert limits[0].structured_max_rows >= 100
    assert [row.ranked_per_query_take for row in limits] == [4, 8, 8, 12, 16]
    assert [row.ranked_final_floor for row in limits] == [8, 20, 24, 32, 40]
    assert [row.ranked_per_aspect for row in limits] == [2, 3, 4, 5, 6]
    assert [row.ranked_final_cap for row in limits] == [12, 36, 48, 64, 96]
    assert [row.max_reasoning_steps for row in limits] == [4, 8, 16, 32, 50]
    assert [row.max_initial_subqueries for row in limits] == [2, 5, 6, 8, 10]
    assert [row.kg_context_chars for row in limits] == [4_000, 6_000, 8_000, 12_000, 16_000]
    assert [row.chunk_context_chars for row in limits] == [12_000, 30_000, 50_000, 80_000, 120_000]
    assert [row.answer_element_items for row in limits] == [4, 6, 8, 12, 16]
    assert [row.enum_page_size for row in limits] == [50, 50, 50, 50, 50]
    assert [row.enum_pages_per_run for row in limits] == [2, 4, 6, 8, 12]
    assert [row.enum_rows_per_run for row in limits] == [100, 200, 300, 400, 600]


def test_generic_reasoning_question_requires_clarification_before_retrieval():
    contract = plan_query_intent(None, "帮我分析一下这个问题")

    assert contract["needs_clarification"] is True
    assert contract["ambiguities"][0]["id"] == "ambiguity-input"
    with pytest.raises(ValueError, match="必填澄清"):
        finalize_query_intent(contract)

    unresolved_followup = plan_query_intent(
        None,
        "它的锁定时间是多少？",
        history="User: 比较两个锁相环",
    )
    assert unresolved_followup["needs_clarification"] is True

    incomplete_model_resolution = plan_query_intent(
        _IncompleteReferentClient(),
        "它的锁定时间是多少？",
        history="User: 比较 PLL A 与 PLL B",
    )
    assert incomplete_model_resolution["needs_clarification"] is True


@pytest.mark.parametrize("question", [
    "这个笔记本包含哪些文章？请逐一列出标题。",
    "介绍这个知识库中的主要主题。",
    "这个库里的文章讨论了什么？",
    "Which articles are in this notebook?",
    "Summarize the main themes in this knowledge base.",
])
def test_current_container_reference_is_not_a_missing_research_object(question):
    # No corpus, model or guessed source identity is needed to identify the
    # active container that the request already supplies.
    contract = plan_query_intent(None, question)

    assert contract["needs_clarification"] is False
    assert contract["resolved_question"] == question
    assert finalize_query_intent(contract)["clarification_answers"] == []


@pytest.mark.parametrize("question", [
    "这个笔记本里，它的锁定时间是多少？",
    "Which article in this notebook supports that?",
    "那个笔记本包含哪些文章？",
    "介绍这个库存方案的局限。",
    "这个笔记本电脑怎么样？",
    "这个知识库系统的缓存如何实现？",
])
def test_container_reference_does_not_clear_another_missing_referent(question):
    contract = plan_query_intent(None, question)

    assert contract["needs_clarification"] is True
    with pytest.raises(ValueError, match="必填澄清"):
        finalize_query_intent(contract)


def test_model_reported_missing_comparison_side_still_requires_an_answer():
    class _AmbiguousComparisonClient(_IntentClient):
        def chat_json(self, *args, **kwargs):
            data = json.loads(super().chat_json(*args, **kwargs))
            data["ambiguities"] = [{
                "question": "要与基准方案比较的是哪个系统？",
                "required": True,
                "options": [],
            }]
            data["needs_clarification"] = True
            return json.dumps(data)

    contract = plan_query_intent(
        _AmbiguousComparisonClient(), "基准测试结果与参考方案相比如何？",
    )

    assert contract["needs_clarification"] is True
    with pytest.raises(ValueError, match="必填澄清"):
        finalize_query_intent(contract)


def test_confirmed_answers_are_frozen_into_authoritative_research_question():
    seed = plan_query_intent(None, "帮我分析一下这个问题")
    seed["assumptions"] = ["环路已正常上电"]
    seed["expected_output"] = "给出按优先级排序的排查步骤"
    final = finalize_query_intent(
        seed,
        resolved_question="分析电荷泵 PLL 的锁定失败",
        answers=[{"id": "ambiguity-input", "answer": "重点检查 PVT 角落"}],
    )

    assert final["confirmed"] is True
    assert final["ambiguities"] == []
    research = confirmed_research_question(final, "unused")
    assert research.startswith("分析电荷泵 PLL 的锁定失败")
    assert "重点检查 PVT 角落" in research
    assert "环路已正常上电" in research
    assert "给出按优先级排序的排查步骤" in research
    assert "帮我分析一下这个问题" not in research

    payload = AskRequest(
        question=seed["objective"],
        mode="reasoning",
        intent=AskIntentConfirmation(
            contract=QueryIntentContract(**seed),
            resolved_question="分析电荷泵 PLL 的锁定失败",
            answers=[QueryIntentAnswer(
                id="ambiguity-input", answer="重点检查 PVT 角落"
            )],
        ),
    )
    assert payload.intent is not None


def test_confirmation_reclassifies_scope_from_final_authoritative_wording():
    ranked_seed = plan_query_intent(None, "介绍常见方法")
    complete = finalize_query_intent(
        ranked_seed, resolved_question="列出所有方法"
    )
    assert complete["result_scope"] == "complete"
    assert complete["completeness_required"] is True

    complete_seed = plan_query_intent(None, "列出所有方法")
    ranked = finalize_query_intent(
        complete_seed, resolved_question="只介绍最相关的三个方法"
    )
    assert ranked["result_scope"] == "ranked"
    assert ranked["completeness_required"] is False


def test_clarification_answer_is_authoritative_for_collection_scope():
    ranked_seed = plan_query_intent(None, "列出方法")
    ranked_seed["ambiguities"] = [{
        "id": "scope", "question": "全部还是最相关？", "required": True,
    }]
    complete = finalize_query_intent(
        ranked_seed,
        answers=[{"id": "scope", "answer": "全部"}],
    )
    assert complete["result_scope"] == "complete"
    assert complete["completeness_required"] is True

    complete_seed = plan_query_intent(None, "列出所有方法")
    complete_seed["ambiguities"] = [{
        "id": "scope", "question": "全部还是最相关？", "required": True,
    }]
    ranked = finalize_query_intent(
        complete_seed,
        answers=[{"id": "scope", "answer": "只给最相关的 10 个"}],
    )
    assert ranked["result_scope"] == "ranked"
    assert ranked["completeness_required"] is False

    for answer in ("不要最相关的，要全部", "不是前 10 个，要全部"):
        complete_after_negated_rank = finalize_query_intent(
            ranked_seed,
            answers=[{"id": "scope", "answer": answer}],
        )
        assert complete_after_negated_rank["result_scope"] == "complete"
        assert complete_after_negated_rank["completeness_required"] is True

    for answer in (
        "不要统计数量，要全部",
        "全部，不要统计数量",
        "不需要总数，只要全部方法",
        "总数不用，给全部方法",
        "多少个不重要，把所有方法列出来",
    ):
        complete_after_negated_count = finalize_query_intent(
            ranked_seed,
            answers=[{"id": "scope", "answer": answer}],
        )
        assert complete_after_negated_count["result_scope"] == "complete"
        assert complete_after_negated_count["completeness_required"] is True


def test_clear_auto_confirmation_preserves_original_collection_scope():
    seed = plan_query_intent(None, "列出所有方法")
    seed["resolved_question"] = "介绍方法"

    final = finalize_query_intent(seed, resolved_question="介绍方法")

    assert final["result_scope"] == "complete"
    assert final["completeness_required"] is True


def test_confirmed_directions_keep_primary_question_then_round_robin_topics():
    contract = {
        "resolved_question": "比较 PLL A 与 PLL B 的锁定性能",
        "mandatory_topics": [
            {
                "question": "比较锁定时间",
                "retrieval_queries": ["PLL A 锁定时间", "PLL B 锁定时间"],
            },
            {
                "question": "比较抖动",
                "retrieval_queries": ["PLL A 与 PLL B 抖动"],
            },
        ],
        "constraints": ["相同工艺角"],
    }

    queries = confirmed_intent_queries(contract, "unused", max_queries=4)

    assert len(queries) == 4
    assert queries[0].startswith("比较 PLL A 与 PLL B 的锁定性能")
    assert queries[1].startswith("PLL A 锁定时间")
    assert queries[2].startswith("PLL A 与 PLL B 抖动")
    assert queries[3].startswith("PLL B 锁定时间")
    assert all("相同工艺角" in query for query in queries)


def test_clear_auto_confirm_keeps_user_wording_authoritative_over_model_rewrite():
    contract = {
        "objective": "比较两个 PLL 的锁定性能",
        "resolved_question": "分析 ADC 的静态线性度",
        "mandatory_topics": [],
    }

    research = confirmed_research_question(
        contract,
        "unused",
        objective_is_authoritative=True,
    )

    assert research.startswith("比较两个 PLL 的锁定性能")
    assert "分析 ADC 的静态线性度" in research


def test_deterministic_ambiguity_row_cannot_exceed_the_contract_ceiling():
    """一个含无法解析指代的普通问题不能因为条数上限而彻底失败。

    模型可以合法返回 8 条 ambiguity,而服务端还会为「指代无法解析」再插一条
    确定性的。两者相加是 9 条,超过 QueryIntentContract.ambiguities 的
    max_length=8 —— 契约构造不出来,`/ask/intent` 就以 pydantic ValidationError
    收场(它是 ValueError 子类,英文原文不该给用户看,更不该变成 500)。
    服务端自己那条排在最前、必须留下,被挤掉的应当是模型的最后一条。
    """
    class _Client:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({
                "normalized_question": "这个方案的优点是什么？",
                "intent_type": "explain",
                "result_scope": "ranked",
                "completeness_required": False,
                "entities": [],
                "mandatory_topics": [],
                "comparison_axes": [],
                "constraints": [],
                "excluded_topics": [],
                "expected_output": "",
                "assumptions": [],
                "ambiguities": [
                    {
                        "id": f"a{index}",
                        "question": f"请澄清第 {index} 点",
                        "reason": "模型自己提的",
                        "required": True,
                        "options": ["x"],
                    }
                    for index in range(8)
                ],
                "confidence": 0.5,
                "needs_clarification": True,
            })

    contract = plan_query_intent(
        _Client(), "这个方案的优点是什么？", "", max_topics=5
    )

    assert len(contract["ambiguities"]) == 8
    # 服务端的确定性行排第一且被保留;挤掉的是模型的最后一条。
    assert contract["ambiguities"][0]["id"] == "ambiguity-input"
    assert "请澄清第 7 点" not in [
        row["question"] for row in contract["ambiguities"]
    ]
    # 真正的验收:契约构造得出来,不抛 ValidationError。
    assert QueryIntentContract(**contract).needs_clarification is True


def _seed_with_ambiguities(questions: list[str]) -> dict:
    return {
        "objective": "这不该出现在文案里的用户原文",
        "ambiguities": [
            {
                "id": f"a{index}",
                "question": question,
                "reason": "这条 reason 也不该出现在文案里",
                "required": True,
                "options": [],
            }
            for index, question in enumerate(questions)
        ],
    }


def test_clarification_gate_message_with_zero_ambiguities():
    assert clarification_gate_message(_seed_with_ambiguities([])) == (
        "问题仍有关键歧义，请先确认问题理解"
    )


def test_clarification_gate_message_with_one_ambiguity():
    message = clarification_gate_message(
        _seed_with_ambiguities(["你提到的对象具体是什么？请给出名称或简要背景。"])
    )
    assert message == (
        "问题仍有关键歧义，请先确认问题理解："
        "① 你提到的对象具体是什么？请给出名称或简要背景。"
    )
    assert "reason" not in message
    assert "这不该出现在文案里的用户原文" not in message
    assert "这条 reason 也不该出现在文案里" not in message


def test_clarification_gate_message_with_eight_ambiguities_uses_all_circled_digits():
    questions = [f"第{i}个澄清问题？" for i in range(1, 9)]
    message = clarification_gate_message(_seed_with_ambiguities(questions))
    for digit, question in zip("①②③④⑤⑥⑦⑧", questions):
        assert f"{digit} {question}" in message
    assert message.count("；") == 7


def test_clarification_gate_message_drops_the_ninth_ambiguity():
    questions = [f"第{i}个澄清问题？" for i in range(1, 10)]
    message = clarification_gate_message(_seed_with_ambiguities(questions))
    assert "第9个澄清问题？" not in message
    assert "第8个澄清问题？" in message
    assert message.count("；") == 7


def test_clarification_gate_message_skips_blank_question_rows():
    seed = _seed_with_ambiguities(["", "  ", "唯一有效的澄清问题？"])
    message = clarification_gate_message(seed)
    assert message == (
        "问题仍有关键歧义，请先确认问题理解：① 唯一有效的澄清问题？"
    )


def test_clarification_gate_message_truncates_a_single_overlong_question():
    long_question = "问" * 600
    message = clarification_gate_message(_seed_with_ambiguities([long_question]))
    expected_body = "问" * 500
    assert message == (
        f"问题仍有关键歧义，请先确认问题理解：① {expected_body}"
    )
    assert len(long_question) > 500


def test_plan_query_intent_fallback_topic_fits_the_contract_for_a_long_question():
    """问题超过 1000 字且没有可用的模型主题(模型未配置/超时/JSON 坏了)时,兜底主题
    过去把整段原文塞进 `QueryIntentTopic.question`,合同本身构造不出来——HTTP
    `/ask/intent` 会 500,MCP `ask_notebook` 会吐一段裸 pydantic 转储。"""
    from app.models.ask import QueryIntentContract

    question = "CMOS 反相器" + "的阈值电压由什么决定" * 120
    assert len(question) > 1000
    contract = QueryIntentContract(**plan_query_intent(None, question))
    assert contract.objective == question
    assert contract.needs_clarification is False
    [topic] = contract.mandatory_topics
    assert len(topic.question) == 1000
    assert topic.retrieval_queries == [question[:1000]]


def test_conversation_intent_history_keeps_the_last_five_user_turns():
    """HTTP `/ask/intent` 与 MCP `ask_notebook` 共用的历史块:只取最近五轮、只取
    用户提问(助手回答是语料派生的,不得进入不读语料的理解步骤)。"""
    from types import SimpleNamespace

    from app.services.query_intent import conversation_intent_history

    turns = [
        SimpleNamespace(question=f"第{i}问", answer=f"助手回答{i}") for i in range(1, 7)
    ]
    assert conversation_intent_history(turns) == (
        "User: 第2问\nUser: 第3问\nUser: 第4问\nUser: 第5问\nUser: 第6问"
    )
    assert conversation_intent_history([]) == ""


def test_validate_confirmed_intent_raises_the_three_user_facing_messages():
    """HTTP 422 与 MCP 工具错误共用的冻结校验只会抛这三句中文用户文案。"""
    from app.services.query_intent import validate_confirmed_intent

    contract = {
        "objective": "分析一下",
        "resolved_question": "分析一下",
        "ambiguities": [{
            "id": "ambiguity-input", "question": "你希望分析的具体对象是什么？",
            "required": True, "options": [],
        }],
    }
    with pytest.raises(ValueError, match="问题理解与当前问题不匹配，请重新确认"):
        validate_confirmed_intent(
            "分析 CMOS 反相器", contract, resolved_question="x", answers=[]
        )
    with pytest.raises(ValueError, match="请先回答所有必填澄清问题"):
        validate_confirmed_intent(
            "分析一下", contract, resolved_question="分析一下", answers=[]
        )
    with pytest.raises(ValueError, match="确认后的问题不能为空"):
        validate_confirmed_intent(
            "  ", {"objective": "", "resolved_question": "", "ambiguities": []},
            resolved_question="", answers=[],
        )
    final = validate_confirmed_intent(
        "分析一下", contract, resolved_question="分析 CMOS 反相器的阈值电压",
        answers=[{"id": "ambiguity-input", "answer": "CMOS 反相器"}],
    )
    assert final["confirmed"] is True
    assert final["needs_clarification"] is False
    assert final["resolved_question"] == "分析 CMOS 反相器的阈值电压"
    assert final["clarification_answers"][0]["answer"] == "CMOS 反相器"


def test_off_type_topic_prose_is_dropped_not_stringified():
    """codex #720 R11: the shape boundary delivers container-valued topic
    titles/questions; they must be dropped (falling back to the original
    question), never planned as Python container syntax."""
    class _ContainerTopics(_IntentClient):
        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({
                "normalized_question": ["比较"],
                "intent_type": "compare",
                "result_scope": "ranked",
                "completeness_required": False,
                "entities": ["PLL A", {"name": "PLL B"}],
                "mandatory_topics": [{
                    "title": ["Caching"],
                    "question": {"query": "cache invalidation"},
                    "retrieval_queries": [["a"], "PLL A lock time"],
                }],
                "ambiguities": [{"question": ["哪个?"], "reason": {"zh": "x"}}],
                "expected_output": {"format": "table"},
                "confidence": 0.9,
                "needs_clarification": False,
            })

    contract = plan_query_intent(_ContainerTopics(), "比较两个 PLL", max_topics=4,
                                 purpose="step-by-step evidence-grounded answer")

    assert contract["resolved_question"] == "比较两个 PLL"
    assert contract["entities"] == ["PLL A"]
    assert [t["question"] for t in contract["mandatory_topics"]] == ["比较两个 PLL"]
    assert contract["expected_output"] == ""
    assert all("[" not in a["question"] for a in contract["ambiguities"])
    encoded = json.dumps(contract, ensure_ascii=False)
    assert "['" not in encoded and "{'" not in encoded


def test_confirmation_keeps_a_model_chosen_scope_unless_wording_overrides_it():
    """codex #725 R1: a scope the model chose (no lexical keywords) used to
    reset to ranked when the user answered an unrelated clarification or
    lightly edited the wording; only the authoritative wording rules may
    override it."""
    class _CompleteWithClarification(_IntentClient):
        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({
                "normalized_question": "当前笔记本有哪几篇文章",
                "intent_type": "other",
                "result_scope": "complete",
                "completeness_required": True,
                "confidence": 0.9,
                "entities": [],
                "mandatory_topics": [],
                "ambiguities": [{"id": "ambiguity-topic", "question": "关注哪个主题？",
                                 "required": False, "options": []}],
                "needs_clarification": True,
            })

    seed = plan_query_intent(_CompleteWithClarification(), "当前笔记本有哪几篇文章")
    assert seed["result_scope"] == "complete"
    ambiguity_id = seed["ambiguities"][0]["id"]

    # Unrelated clarification answer: the accepted scope survives.
    kept = finalize_query_intent(
        seed, resolved_question="当前笔记本有哪几篇文章",
        answers=[{"id": ambiguity_id, "answer": "机器学习"}],
    )
    assert kept["result_scope"] == "complete"
    assert kept["completeness_required"] is True

    # Light wording edit without scope words: still kept.
    edited = finalize_query_intent(seed, resolved_question="当前笔记本里有哪几篇文章？")
    assert edited["result_scope"] == "complete"

    # Authoritative wording that declines the full set still wins.
    capped = finalize_query_intent(seed, resolved_question="不需要所有文章，只给最相关的几篇")
    assert capped["result_scope"] == "ranked"
    assert capped["completeness_required"] is False


def test_an_obsolete_lexical_scope_is_not_frozen_by_confirmation():
    """codex #725 R2: a scope that came only from wording the user has since
    removed ("列出所有方法" → "介绍常见方法") is re-judged from the new
    wording; only a model-chosen scope is carried over."""
    lexical_seed = plan_query_intent(None, "列出所有方法")
    assert lexical_seed["result_scope"] == "complete"
    edited = finalize_query_intent(lexical_seed, resolved_question="介绍常见方法")
    assert edited["result_scope"] == "ranked"
    assert edited["completeness_required"] is False
