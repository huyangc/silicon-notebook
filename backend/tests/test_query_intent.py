import json

import pytest

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
        return json.dumps({
            "normalized_question": "比较 PLL A 与 PLL B 的锁定时间和抖动",
            "intent_type": "compare",
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
