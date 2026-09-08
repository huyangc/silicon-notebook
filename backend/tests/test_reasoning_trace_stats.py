"""T0 闭集投影的形态与隐私守卫(设计规格 2026-09-08 §2.4)。

合成 fixture,不碰数据库、不碰模型。每个用例钉住一条**口径**,而不是一个当前
输出值:改了口径就该看见这里红,而不是「顺手把断言改成新值」。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.domain.reasoning_trace_stats import (
    RUN_PROJECTION_KEYS,
    TERMINATION_MODEL_END,
    TERMINATION_SKIP_REASON,
    UNKNOWN,
    assert_closed,
    citation_contribution,
    merge_key,
    normalize_steps,
    project_report_section,
    project_run,
    source_bucket,
)

JOB = {"mode": "reasoning", "status": "done"}
PAYLOAD = {"retrieval_effort": "standard", "mode": "reasoning"}


def step(step_type, detail=None, duration_ms=None, summary="人话摘要"):
    return {
        "step_type": step_type, "summary": summary,
        "detail": dict(detail or {}), "duration_ms": duration_ms,
    }


def reflect(next_action="ppr", sufficient=False, **extra):
    return step("reflect", {"next_action": next_action,
                            "sufficient": sufficient, **extra})


# --- 隐私守卫 ---------------------------------------------------------------


#: 整键禁词:这些名字本身就是「一段人写的话」。
FORBIDDEN_EXACT = frozenset({
    "id", "question", "summary", "reason", "title", "text", "answer",
    "conclusion", "markdown", "claims",
})
#: 后缀禁词。`_id` 一律禁;`_key` **不**禁——`question_key` 是题号、
#: `merge_key` 是单向哈希,两者都是编号而不是内容(§6)。
FORBIDDEN_SUFFIXES = ("_id", "_text", "_title", "_summary", "_question",
                      "_markdown", "_answer")


def test_projection_keys_carry_no_free_text_or_ids():
    """闭集本身就不许出现自由文本键或 id 键。

    只断言「行 ⊆ 闭集」是挡不住的:把 `question` **同时**加进闭集与投影,那条
    断言照样通过。所以闭集自己也要过一遍禁词。`termination_reason` /
    `fallback_reasons` / `skip_reasons` 是**原因码**(闭集短串)而不是理由原文,
    所以整键 `reason` 禁、带前缀的原因码键放行。
    """
    for key in RUN_PROJECTION_KEYS:
        assert key not in FORBIDDEN_EXACT, key
        for suffix in FORBIDDEN_SUFFIXES:
            assert not key.endswith(suffix), (key, suffix)


def test_project_run_row_is_within_the_closed_set():
    row = project_run(JOB, [step("ppr", {"found": 1})], PAYLOAD)
    assert set(row) <= RUN_PROJECTION_KEYS


def test_assert_closed_rejects_an_extra_key():
    """守卫的变异形态:往投影里加一个 `question` 键必须炸。"""
    row = dict(project_run(JOB, [], PAYLOAD))
    row["question"] = "这行不该存在"
    with pytest.raises(ValueError, match="RUN_PROJECTION_KEYS"):
        assert_closed(row)


def test_projection_never_carries_trace_summaries():
    row = project_run(
        JOB, [step("ppr", {"found": 0}, summary="模型写的一句话")], PAYLOAD
    )
    assert "模型写的一句话" not in json.dumps(row, ensure_ascii=False)


def test_termination_skip_reason_matches_the_service_constant():
    """domain 不许 import services,所以这个码在两处各写了一份字面量。

    分叉了就当场红:v2 的终态步会被读成一个普通 skip,整批 v2 run 会被误判成
    legacy 并进入反推分支。
    """
    from app.services.reasoning_aspects import TERMINATION_SKIP_REASON as service

    assert TERMINATION_SKIP_REASON == service


# --- legacy 缺字段 ----------------------------------------------------------


def test_legacy_steps_without_result_ids_are_unknown_not_zero():
    row = project_run(JOB, [step("ppr", {"found": 3}), reflect()], PAYLOAD)
    entry = row["citation_contribution"]["seed:ppr"]
    assert entry["steps_with_ids"] == 0
    assert entry["unknown_steps"] == 1
    assert entry["cited_hits"] is None


def test_missing_termination_step_is_inferred_and_flagged():
    row = project_run(
        JOB, [reflect("ppr"), step("ppr", {"count": 1}), reflect("answer")],
        PAYLOAD,
    )
    assert row["termination_reason"] == TERMINATION_MODEL_END
    assert row["termination_inferred"] is True
    assert row["policy_version"] == "legacy"


def test_empty_trace_leaves_every_metric_unknown():
    row = project_run(JOB, [], {"retrieval_effort": "overview"})
    assert row["termination_reason"] is None
    assert row["termination_inferred"] is None
    assert row["total_ms"] is None
    assert row["stale_max"] is None
    assert row["anchors"] is None
    assert row["reflect_turns"] == 0
    assert row["trace_steps"] == 0


def test_failed_run_reports_status_and_not_zeroed_metrics():
    row = project_run({"mode": "reasoning", "status": "failed"}, [], {})
    assert row["status"] == "failed"
    assert row["effort"] == UNKNOWN
    assert row["kg_in_scope"] is None
    assert row["candidates_kg"] is None


def test_unknown_effort_blocks_the_step_budget_inference():
    """反推 `step_budget` 要档位上限;没有档位就只能是 unknown,不能猜。"""
    steps = [reflect() for _ in range(40)]
    assert project_run(JOB, steps, {})["termination_reason"] is None


def test_step_budget_inference_uses_the_policy_ceiling():
    ceiling = ASK_RETRIEVAL_LIMITS["standard"].max_reasoning_steps
    steps = [reflect("ppr") for _ in range(ceiling)]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["termination_reason"] == "step_budget"
    assert row["termination_inferred"] is True


def test_stale_breaker_wins_over_the_budget_inference():
    ceiling = ASK_RETRIEVAL_LIMITS["standard"].max_reasoning_steps
    steps = [reflect("ppr") for _ in range(ceiling)]
    steps.append(step("skip", {"reason": "stale_circuit_breaker", "stale": 3}))
    row = project_run(JOB, steps, PAYLOAD)
    assert row["termination_reason"] == "stale"
    assert row["stale_breaker"] is True
    assert row["stale_max"] == 3


# --- v2 与 legacy 不重复计数 ------------------------------------------------


def test_v2_termination_step_is_read_not_inferred():
    steps = [
        reflect("ppr"),
        step("skip", {"reason": "stale_circuit_breaker", "stale": 3}),
        step("skip", {
            "reason": TERMINATION_SKIP_REASON, "termination": "model_partial",
            "aspects": 4, "unresolved_aspects": 2,
            "unrecovered_channels": ["ppr", "expand"],
        }),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    # 同一条 run 上熔断步与终态步同时存在:结果只能有一个,而且必须是 v2 读到
    # 的那个,不是反推出来的 "stale"。
    assert row["termination_reason"] == "model_partial"
    assert row["termination_inferred"] is False
    assert row["policy_version"] == "v2"
    assert row["unrecovered_channels_count"] == 2


def test_v2_synthesis_keys_are_projected():
    steps = [step("synthesis", {
        "anchors": 3, "anchor_evidence_ids": ["a", "b", "c"],
        "termination_reason": "model_sufficient",
        "aspects_total": 5, "aspects_pending": 1, "aspects_undelivered": 2,
        "included_kg": 4, "included_chunks": 9, "included_elements": 1,
    })]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["policy_version"] == "v2"
    assert row["termination_reason"] == "model_sufficient"
    assert (row["aspects_total"], row["aspects_pending"],
            row["aspects_undelivered"]) == (5, 1, 2)
    assert row["anchors"] == 3


# --- 引用贡献(§4.4) --------------------------------------------------------


def test_first_hit_attribution_and_shared_hits():
    """同一个证据被两步命中:归首次,重复的只进 `shared_hits`,不均摊。"""
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a", "b"]}),
        reflect("search_chunks"),
        step("search_chunks", {"result_ids": ["b", "c"]}),
        step("synthesis", {"anchors": 3,
                           "anchor_evidence_ids": ["a", "b", "c"]}),
    ]
    contribution, shared = citation_contribution(normalize_steps(steps))
    assert contribution["seed:ppr"]["cited_hits"] == 2
    assert contribution["search_chunks"]["cited_hits"] == 1
    assert shared == 1


def test_truncated_result_ids_poison_only_that_step():
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a"],
                     "result_ids_truncated": True}),
        reflect("search_chunks"),
        step("search_chunks", {"result_ids": ["b"]}),
        step("synthesis", {"anchors": 2, "anchor_evidence_ids": ["a", "b"]}),
    ]
    contribution, _ = citation_contribution(normalize_steps(steps))
    assert contribution["seed:ppr"]["unknown_steps"] == 1
    assert contribution["seed:ppr"]["cited_hits"] is None
    assert contribution["search_chunks"]["cited_hits"] == 1


def test_truncated_anchor_ids_poison_the_whole_run():
    """整轮锚点不可信时,不许拿 `anchors` 这个数顶替逐步交集。"""
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a"]}),
        step("synthesis", {"anchors": 40, "anchor_evidence_ids": ["a"],
                           "anchor_evidence_ids_truncated": True}),
    ]
    contribution, _ = citation_contribution(normalize_steps(steps))
    assert contribution["seed:ppr"]["cited_hits"] is None
    assert contribution["seed:ppr"]["unknown_steps"] == 1


def test_synthesis_without_anchor_key_is_not_zero_anchors():
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a"]}),
        step("answer", {"kg": 3}),
    ]
    contribution, _ = citation_contribution(normalize_steps(steps))
    assert contribution["seed:ppr"]["cited_hits"] is None


def test_empty_result_ids_list_is_observed_zero_not_unknown():
    steps = [
        step("ppr", {"phase": "seed", "result_ids": []}),
        step("synthesis", {"anchors": 0, "anchor_evidence_ids": []}),
    ]
    contribution, _ = citation_contribution(normalize_steps(steps))
    assert contribution["seed:ppr"]["cited_hits"] == 0
    assert contribution["seed:ppr"]["unknown_steps"] == 0


# --- seed / action 分离 -----------------------------------------------------


def test_phase_key_separates_seed_from_loop_actions():
    steps = [
        step("ppr", {"phase": "seed", "found": 2}),
        reflect("ppr"),
        step("ppr", {"count": 1}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["action_seq"] == ["seed:ppr", "ppr"]
    assert row["seed_actions_by_type"] == {"ppr": 1}
    assert row["actions_by_type"] == {"ppr": 1}


def test_missing_phase_key_falls_back_to_position():
    """旧轨迹没有 `phase`:第一条 reflect 之前的一切按定义是播种。"""
    steps = [
        step("retrieve", {"count": 4}),
        step("ppr", {"found": 2}),
        reflect("search_chunks"),
        step("search_chunks", {"count": 3}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["action_seq"] == ["seed:retrieve", "seed:ppr", "search_chunks"]


def test_empty_actions_are_counted_per_action_key():
    steps = [
        step("ppr", {"phase": "seed", "found": 0}),
        reflect("search_chunks"),
        step("search_chunks", {"count": 0}),
        step("expand", {"found": 2}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["empty_actions_by_type"] == {"search_chunks": 1, "seed:ppr": 1}


def test_skip_and_fallback_reason_codes_are_tallied():
    steps = [
        step("skip", {"reason": "kg_unavailable"}),
        step("skip", {"reason": "ppr_disabled"}),
        step("skip", {}),
        step("fallback", {"reason": "initial_evidence_empty", "found": 0}),
        step("fallback", {"found": 1}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["skip_reasons"] == {
        "kg_unavailable": 1, "ppr_disabled": 1, UNKNOWN: 1,
    }
    assert row["fallback_count"] == 2
    assert row["fallback_reasons"] == {"initial_evidence_empty": 1, UNKNOWN: 1}
    assert row["kg_in_scope"] is False


# --- 两种 step_json 类型 ----------------------------------------------------


def test_sqlite_text_and_postgres_jsonb_project_identically():
    """SQLite 递来 TEXT、PostgreSQL 递来 dict。一份投影,两种类型,同一行。"""
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a"]}, duration_ms=12),
        reflect("answer", sufficient=True),
        step("synthesis", {"anchors": 1, "anchor_evidence_ids": ["a"]},
             duration_ms=30),
    ]
    as_text = [json.dumps(item, ensure_ascii=False) for item in steps]
    assert project_run(JOB, steps, PAYLOAD) == project_run(JOB, as_text, PAYLOAD)


def test_corrupt_rows_are_skipped_not_fatal():
    steps = ["{ not json", None, 42, step("ppr", {"count": 1})]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["trace_steps"] == 1


def test_unknown_step_types_collapse_to_other():
    row = project_run(JOB, [step("brand_new_action", {"count": 1})], PAYLOAD)
    assert row["action_seq"] == ["seed:other"]


# --- 维度 -------------------------------------------------------------------


def test_source_bucket_boundaries():
    assert source_bucket(1) == "1"
    assert source_bucket(2) == "2-5"
    assert source_bucket(5) == "2-5"
    assert source_bucket(6) == "6-20"
    assert source_bucket(21) == "21+"
    assert source_bucket(None) == UNKNOWN


def test_sectioned_consumer_is_read_from_the_synthesis_step():
    steps = [step("synthesis", {"section_index": 1, "section_total": 3})]
    assert project_run(JOB, steps, PAYLOAD)["consumer"] == "ask_sectioned"
    assert project_run(JOB, [], PAYLOAD)["consumer"] == "ask_single"


def test_rig_tags_are_closed_and_case_sensitive():
    row = project_run(
        JOB, [], PAYLOAD,
        rig_tags={"corpus_cell": "B_kg", "question_key": "B-q07",
                  "requested_mode": "auto", "consumer": "report_section"},
    )
    assert row["corpus_cell"] == "B_kg"
    assert row["question_key"] == "B-q07"
    assert row["mode"] == "auto→reasoning"
    assert row["consumer"] == "report_section"
    assert project_run(
        JOB, [], PAYLOAD, rig_tags={"corpus_cell": "C_kg"},
    )["corpus_cell"] == UNKNOWN


def test_kg_required_payload_drives_kg_in_scope():
    assert project_run(JOB, [], {"kg_required": True})["kg_in_scope"] is False
    assert project_run(JOB, [], {"kg_required": False})["kg_in_scope"] is True
    assert project_run(JOB, [], {})["kg_in_scope"] is None


# --- 报告段 -----------------------------------------------------------------


def test_report_section_projection_keeps_only_result_level_facts():
    section = {
        "title": "KV 缓存压缩",
        "markdown": "很长的正文……",
        "evidence_level": "grounded",
        "grounded": True,
        "top_relevance": 0.72,
        "attempted": [{"query": "kv cache", "new": 3},
                      {"query": "eviction", "new": 0, "failed": True}],
    }
    row = project_report_section(
        section, section_index=2, section_total=5, report_depth=8,
        report_id="rep-1", rig_tags={"corpus_cell": "B_kg",
                                     "question_key": "R-q03"},
    )
    assert set(row) <= RUN_PROJECTION_KEYS
    assert row["attempted"] == 2
    assert row["attempted_failed"] == 1
    assert row["evidence_level"] == "grounded"
    assert row["failed"] is False
    assert row["merge_key"] == merge_key("rep-1", 2)
    body = json.dumps(row, ensure_ascii=False)
    assert "KV 缓存压缩" not in body and "rep-1" not in body


def test_report_section_without_grounding_is_unknown_not_false():
    row = project_report_section({}, section_index=0)
    assert row["grounded"] is None
    assert row["evidence_level"] == UNKNOWN
    assert row["top_relevance"] is None
