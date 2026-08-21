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
    confirmed_intent_queries,
    confirmed_research_question,
    finalize_query_intent,
    plan_query_intent,
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


def test_model_cannot_upgrade_ambiguous_nouns_to_collection_enumeration():
    class _OvereagerScopeClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "result_scope": "aggregate",
                "completeness_required": True,
                "normalized_question": "解释统计方法的适用范围",
            })

    contract = plan_query_intent(
        _OvereagerScopeClient(), "解释统计方法的适用范围"
    )
    assert contract["result_scope"] == "ranked"
    assert contract["completeness_required"] is False


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
