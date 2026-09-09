"""T0 闭集投影的形态与隐私守卫(设计规格 2026-09-08 §2.4)。

合成 fixture,不碰数据库、不碰模型。每个用例钉住一条**口径**,而不是一个当前
输出值:改了口径就该看见这里红,而不是「顺手把断言改成新值」。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.domain.reasoning_trace_stats import (
    ASSESSMENT_REJECTION_REASONS,
    RUN_PROJECTION_KEYS,
    TERMINATION_MODEL_DEGRADED,
    TERMINATION_MODEL_END,
    TERMINATION_SKIP_REASON,
    UNKNOWN,
    assert_closed,
    assert_projection_values,
    citation_contribution,
    merge_key,
    normalize_steps,
    project_report_section,
    project_run,
    project_search_run,
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


def test_assert_projection_values_accepts_real_action_seq_and_citation_shapes():
    """`action_seq`(短码列表)与 `citation_contribution`(两层短码→数值字典)
    是这道闸要放行的两种真实复合形状——闭集键之外,这是它唯一要认得的东西。
    """
    row = project_run(
        JOB,
        [
            step("ppr", {"phase": "seed", "result_ids": ["a", "b"]}),
            reflect("search_chunks"),
            step("search_chunks", {"result_ids": ["b", "c"]}),
            step("synthesis", {"anchors": 3,
                               "anchor_evidence_ids": ["a", "b", "c"]}),
        ],
        PAYLOAD,
    )
    assert row["action_seq"] == ["seed:ppr", "search_chunks"]
    assert row["citation_contribution"]["seed:ppr"]["cited_hits"] == 2
    assert_projection_values(row)  # 不炸就是通过


def test_assert_projection_values_rejects_a_free_text_value():
    """结构性值校验不看键名——自由文本值不管挂在闭集内哪个键下都会被拦住。

    这里刻意往一个**不在闭集里**的键(`scope`)塞一句人话:即便有人把这个键
    也加进 `RUN_PROJECTION_KEYS`,值本身的自由文本形态照样会被这道闸挡下,
    与 `assert_closed`(只管键名)是互补的两道闸。
    """
    row = dict(project_run(JOB, [], PAYLOAD))
    row["scope"] = "看 Qwen-VL 和 DeepSeek"
    with pytest.raises(ValueError):
        assert_projection_values(row)


def test_assert_projection_values_enforces_the_short_code_length_limit():
    row = dict(project_run(JOB, [], PAYLOAD))
    row["question_key"] = "q" * 65  # 字符集合规,纯粹是太长
    with pytest.raises(ValueError):
        assert_projection_values(row)


def test_assert_projection_values_rejects_a_non_short_code_dict_value():
    row = dict(project_run(JOB, [], PAYLOAD))
    row["actions_by_type"] = {"ppr": "很多次"}
    with pytest.raises(ValueError):
        assert_projection_values(row)


def test_assert_projection_values_accepts_the_auto_mode_arrow():
    """`→` 是 `MODES` 闭集自己的分隔符(`auto→reasoning`),不是自由文本。"""
    row = project_run(
        JOB, [], PAYLOAD, rig_tags={"requested_mode": "auto"},
    )
    assert row["mode"] == "auto→reasoning"
    assert_projection_values(row)  # 不炸就是通过


def test_assert_projection_values_rejects_a_non_short_code_nested_key():
    row = dict(project_run(JOB, [], PAYLOAD))
    row["citation_contribution"] = {"看起来像动作但其实是句话": {"steps": 1}}
    with pytest.raises(ValueError):
        assert_projection_values(row)


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


def test_explicit_step_ceiling_overrides_the_effort_table_for_reports():
    """报告逐节深挖的有效上限是 `min(depth, 档位上限)`(引擎传 `max_steps=depth`),
    depth=2 的节两轮就停;只按 standard 档 8 轮判会得到 unknown(codex #700 R10
    P2)。`step_ceiling` 给了就以它为准;没给仍按档位表。"""
    steps = [reflect("ppr") for _ in range(2)]
    assert project_run(JOB, steps, PAYLOAD)["termination_reason"] is None
    row = project_run(JOB, steps, PAYLOAD, step_ceiling=2)
    assert row["termination_reason"] == "step_budget"
    assert row["termination_inferred"] is True
    # 上限没顶到就不是预算收尾。
    assert project_run(JOB, steps, PAYLOAD, step_ceiling=3)["termination_reason"] is None


def test_failed_run_without_evidence_keeps_the_declared_rig_labels():
    """v2 的 Ask 在发出 termination 步、落答案之前就失败:没有协议证据、没有
    payload,按证据判会记成 legacy/unknown,失败从 v2 对照列里消失(codex #700 R10
    P2)。失败/取消的 run 用 rig 声明的标签;跑成的 run 仍以证据为准。"""
    tags = {"policy": "v2", "effort": "deep", "question_key": "A-q01",
            "corpus_cell": "A_nokg"}
    failed = project_run({"mode": "reasoning", "status": "failed"}, [], None,
                         rig_tags=tags)
    assert failed["policy_version"] == "v2"
    assert failed["effort"] == "deep"
    assert failed["status"] == "failed"
    # 跑成的 run:声明 v2 但轨迹里没有 termination 事实 ⇒ 证据说 legacy,声明不改证据。
    done = project_run(JOB, [reflect("ppr")], PAYLOAD, rig_tags=tags)
    assert done["policy_version"] == "legacy"
    assert done["effort"] == PAYLOAD["retrieval_effort"]
    # 声明不过闭集就还是 unknown/legacy,不猜。
    junk = project_run({"mode": "reasoning", "status": "failed"}, [], None,
                       rig_tags={"policy": "V2", "effort": "ultra"})
    assert junk["policy_version"] == "legacy"
    assert junk["effort"] == "unknown"


def test_intent_presence_is_recovered_from_the_trace_when_payload_is_missing():
    """失败/取消前没落答案的 run 没有 payload,但轨迹里的 `intent` 步只在按确认
    后的契约开跑时才记——读到它就该记 has_intent_contract=True(codex #700 R16
    P2);否则失败 run 恒 false,配对身份含此键后就配不上同契约的成功 run。"""
    intent_step = {"step_type": "intent", "detail": {}, "duration_ms": 1}
    failed = project_run({"mode": "reasoning", "status": "failed"},
                         [intent_step, reflect("ppr")], None)
    assert failed["has_intent_contract"] is True
    # 没有 intent 步也没有 payload:仍是 False,不猜。
    bare = project_run({"mode": "reasoning", "status": "failed"}, [reflect("ppr")], None)
    assert bare["has_intent_contract"] is False
    # payload 在时以 payload 为准,轨迹只补缺。
    done = project_run(JOB, [reflect("ppr")], {**PAYLOAD, "intent": {"x": 1}})
    assert done["has_intent_contract"] is True


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
    # v2 却一条都没被拒 ⇒ 0(而不是 unknown):这条 run 真的数过。
    assert row["assessment_rejections"] == 0


def test_assessment_rejections_counts_aspects_not_voided_turns():
    """`assessment_rejections` 只数**逐方面**被拒的那一族(T-BF7),按 `count` 累加。

    ⚠ 两列数的东西不同,**不要相加**:逐方面拒绝每轮只记一条 skip 步(条数在
    detail 的 `count` 里),所以 `skip_reasons` 那几项数的是**轮**,这一列数的是
    **方面**;因为自评而整轮作废了几次 = 全部 `invalid_assessment:*` 之和 − 逐方面
    那几项之和。

    变异:把整份形状那一族也计进来 ⇒ 这条红;`_rejection_count` 改回恒 1
    ⇒ 也红(会数成 2)。
    """
    steps = [
        step("skip", {"reason": "invalid_assessment:unknown_aspect",
                      "rejections": {"unknown_aspect": 1}, "count": 1,
                      "aspect_ids": []}),
        step("skip", {"reason": "invalid_assessment:gap_overflow",
                      "rejections": {"gap_overflow": 2, "invalid_status": 1},
                      "count": 3, "aspect_ids": ["a2", "a3", "a4"]}),
        # 整份形状不成立 ⇒ 那一轮真的整轮作废,不计进这一列。
        step("skip", {"reason": "invalid_assessment:item_not_object"}),
        step("synthesis", {"termination_reason": "model_partial"}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["policy_version"] == "v2"
    assert row["assessment_rejections"] == 4        # 1 + 3 个方面
    per_aspect_turns = sum(
        count for reason, count in row["skip_reasons"].items()
        if reason in ASSESSMENT_REJECTION_REASONS)
    assert per_aspect_turns == 2                    # 逐方面拒过 2 轮
    assert sum(
        count for reason, count in row["skip_reasons"].items()
        if reason.startswith("invalid_assessment:")
    ) - per_aspect_turns == 1                       # 整轮作废了 1 次


def test_assessment_rejections_reads_pre_merge_traces_as_one_aspect_each():
    """T-BF7 与「每轮一条」之间落盘的旧轨迹(无 `count`)按一条一个方面计。

    那时确实是「一个方面一条 skip」,所以按 1 计恰好等价——旧数据不必重投影就能
    和新数据摆在同一列里读。

    ⚠ 但 `skip_reasons` 那一列不等价:旧轨迹里它数的是方面,新轨迹里数的是轮。
    A/B 取样不得跨越这次改动的日期(已登记进 `fangan_todo.md`)。

    变异:`_rejection_count` 对缺席的 `count` 返回 0 ⇒ 这条红。
    """
    steps = [
        step("skip", {"reason": "invalid_assessment:unknown_aspect"}),
        step("skip", {"reason": "invalid_assessment:gap_overflow",
                      "aspect_id": "a2"}),
        step("synthesis", {"termination_reason": "model_partial"}),
    ]
    assert project_run(JOB, steps, PAYLOAD)["assessment_rejections"] == 2


def test_assessment_rejections_is_unknown_on_a_legacy_run():
    """legacy 没有这本账:恒 unknown,不折成 0。

    0 会被 A/B 读成「legacy 这一列表现更好」,而它其实**结构上**不可能有值。
    """
    row = project_run(JOB, [step("ppr", {"found": 1})], PAYLOAD)
    assert row["policy_version"] == "legacy"
    assert row["assessment_rejections"] is None


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


def test_shared_hits_are_unknown_when_anchors_are_unavailable():
    """只跑检索的 search run 没有 synthesis 步,锚点集合不可信,每一步都绕过了
    共享命中记账——此时 `shared_hits` 是「没法数」而不是 0(codex #700 R18 P2):
    分析脚本会把 0 当成观测到的零重叠样本。"""
    steps = [
        step("ppr", {"phase": "seed", "result_ids": ["a", "b"]}),
        reflect("search_chunks"),
        step("search_chunks", {"result_ids": ["b", "c"]}),
    ]
    contribution, shared = citation_contribution(normalize_steps(steps))
    assert shared is None
    assert contribution["seed:ppr"]["cited_hits"] is None
    row = project_run(JOB, steps, PAYLOAD)
    assert row["shared_hits"] is None and row["anchors"] is None


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


def test_skip_reason_codes_are_tallied():
    steps = [
        step("skip", {"reason": "kg_unavailable"}),
        step("skip", {"reason": "ppr_disabled"}),
        step("skip", {}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["skip_reasons"] == {
        "kg_unavailable": 1, "ppr_disabled": 1, UNKNOWN: 1,
    }
    assert row["kg_in_scope"] is False


def test_search_elements_fallback_step_type_does_not_count_as_model_fallback():
    """`step_type == "fallback"` 是 `search_elements` 的路由决策(初检索空手后
    补查原文),不是模型兜底(2026-09-08 口径修正前两者被混算)。它只应该出现在
    `actions_by_type`,`fallback_count`/`fallback_reasons` 必须保持 0/空。
    """
    steps = [
        step("fallback", {"reason": "initial_evidence_empty", "found": 0}),
        step("fallback", {"found": 1}),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["fallback_count"] == 0
    assert row["fallback_reasons"] == {}
    # 两步都在第一条 reflect 之前(没有一条),按位置判据算作播种。
    assert row["seed_actions_by_type"] == {"fallback": 2}


def test_reflect_fallback_reason_is_tallied_as_model_fallback():
    """真正的模型兜底(`reasoning_retrieval._reflect_fallback` 写的
    `fallback_reason` 键)才计入 `fallback_count`/`fallback_reasons`,原因码
    透传、不折成 unknown。"""
    steps = [
        reflect("ppr"),
        reflect("answer", sufficient=True, fallback_reason="provider_unavailable"),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["fallback_count"] == 1
    assert row["fallback_reasons"] == {"provider_unavailable": 1}


def test_reflect_fallback_reason_infers_model_degraded_not_model_end():
    """末尾 reflect 带 `fallback_reason` 时,反推的结束原因是
    `model_degraded`,不是 `model_end`——fail-open 兜底会把 `next_action` 写成
    `answer`,与「模型自己说够了」在 `next_action`/`sufficient` 上完全同形,
    唯一能分开两者的信号就是这个键在不在。"""
    steps = [
        reflect("ppr"),
        reflect("answer", sufficient=True, fallback_reason="provider_unavailable"),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["termination_reason"] == TERMINATION_MODEL_DEGRADED
    assert row["termination_inferred"] is True
    assert row["fallback_count"] == 1
    assert row["fallback_reasons"] == {"provider_unavailable": 1}


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


def test_kg_in_scope_only_reads_positive_evidence():
    """`kg_required` 默认 `False` 且总被序列化;它不再单独把结果判成 `True`。

    四种用例(设计规格 §3 该行订正后的用例):chunk 模式、早退(reasoning 但
    轨迹为空)、有图 reasoning、无图 reasoning。
    """
    # chunk 模式:kg_required=False 摆在那里,但没有图形状步——不能定案。
    chunk_row = project_run(
        {"mode": "chunk", "status": "done"}, [],
        {"mode": "chunk", "kg_required": False},
    )
    assert chunk_row["kg_in_scope"] is None

    # 早退:reasoning 模式,轨迹却是空的(一步检索动作都没跑到)。
    early_row = project_run(JOB, [], {"kg_required": False, "mode": "reasoning"})
    assert early_row["kg_in_scope"] is None

    # 有图 reasoning:轨迹里出现过图形状的步。
    kg_row = project_run(
        JOB, [step("ppr", {"phase": "seed", "found": 2})], PAYLOAD,
    )
    assert kg_row["kg_in_scope"] is True

    # 无图 reasoning:无图披露步在场,`kg_required` 的默认值盖不过它。
    nokg_row = project_run(
        JOB, [step("skip", {"reason": "kg_unavailable"})],
        {"kg_required": False, "mode": "reasoning"},
    )
    assert nokg_row["kg_in_scope"] is False


def test_kg_required_true_is_still_evidence_for_false():
    assert project_run(JOB, [], {"kg_required": True})["kg_in_scope"] is False


def test_ambiguous_retrieve_step_is_not_kg_evidence():
    """首轮"初检索"步只写 `count`(图/原文混合抓取),分不清就不算。"""
    row = project_run(JOB, [step("retrieve", {"count": 4})], PAYLOAD)
    assert row["kg_in_scope"] is None


def test_graphless_retrieve_half_is_not_kg_evidence():
    """`chunks_found` 在场说明这一步的 `new` 只可能是空手的图查询。"""
    row = project_run(
        JOB, [step("retrieve", {"new": 0, "chunks_found": 3})], PAYLOAD,
    )
    assert row["kg_in_scope"] is None


def test_retrieve_step_with_new_hits_is_kg_evidence():
    row = project_run(JOB, [step("retrieve", {"new": 2})], PAYLOAD)
    assert row["kg_in_scope"] is True


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


# --- search(rig 的进程内检索 run) ------------------------------------------


def _search_steps(*extra):
    """一条只检索、不合成的轨迹:plan → retrieve → reflect → answer。

    `answer` 是 `ReasoningRetriever.run` 自己的收尾步(候选池计数在它的 detail
    里);**synthesis 步不在其中**——那一步由 Ask 的合成阶段写,而这条路没有。
    """
    return [
        step("plan", {"count": 1}, duration_ms=10),
        step("retrieve", {"query": "q", "new": 3}, duration_ms=20),
        *extra,
        step("answer", {"kg": 7, "elements": 2}, duration_ms=5),
    ]


def _search_row(steps, *, result=None, policy="legacy", **overrides):
    kwargs = {
        "result": result,
        "effort": "standard",
        "policy": policy,
        "question_key": "B-q03",
        "corpus_cell": "B_kg",
        "kg_in_scope": True,
    }
    kwargs.update(overrides)
    return project_search_run(steps, **kwargs)


def test_search_run_declares_its_consumer_and_trace_source():
    """两个标签只能由 rig 声明(没有 job、没有 synthesis 步),但仍过闭集。"""
    row = _search_row(_search_steps(reflect("answer", True)))
    assert set(row) <= RUN_PROJECTION_KEYS
    assert row["consumer"] == "ask_single"
    assert row["trace_source"] == "in_process"
    assert row["corpus_cell"] == "B_kg" and row["question_key"] == "B-q03"
    assert row["status"] == "done" and row["mode"] == "reasoning"
    assert row["kg_in_scope"] is True
    assert row["candidates_kg"] == 7 and row["candidates_elements"] == 2


def test_search_run_without_a_termination_fact_infers_legacy():
    """`result.termination` 是 **v2-only**,`None` 就是 legacy。

    此时结束原因走 `project_run` 已经做过的那次反推,`termination_inferred`
    必须是 True —— 「反推出来的」和「run 自己记下来的」不是同一个可信度。
    """
    row = _search_row(_search_steps(reflect("answer", True)))
    assert row["policy_version"] == "legacy"
    assert row["termination_reason"] == TERMINATION_MODEL_END
    assert row["termination_inferred"] is True
    # 方面账是 v2 的东西,legacy 一条都读不出来 —— unknown,不是 0。
    assert row["aspects_total"] is None and row["aspects_pending"] is None
    assert row["unrecovered_channels_count"] is None


def test_search_run_reads_the_termination_dto_not_the_trace_step():
    """v2 的权威是 `RetrievalTermination` 这个 DTO,不是它在轨迹里的那份渲染。

    所以这条用例的轨迹里**一条 v2 终态步都没有**:只看轨迹的话
    `project_run` 会判成 legacy。DTO 在场就必须翻过来——它是构造期就过了闭集
    守卫的事实,而轨迹步只是它的一次渲染,可能被截尾、可能还没写。
    """
    from app.domain.retrieval_termination import (
        AspectSnapshot,
        RetrievalTermination,
    )

    termination = RetrievalTermination(
        reason="model_partial",
        unresolved_aspect_ids=("a2",),
        unrecovered_channels=("search_chunks", "add_subquery"),
        aspects=(
            AspectSnapshot(aspect_id="a1", question="Q1", status="supported"),
            AspectSnapshot(aspect_id="a2", question="Q2", status="partial"),
        ),
    )
    row = _search_row(
        _search_steps(reflect("answer", True)),
        result=type("_Result", (), {"termination": termination})(),
        policy="v2",
    )
    assert row["policy_version"] == "v2"
    assert row["termination_reason"] == "model_partial"
    assert row["termination_inferred"] is False
    assert row["aspects_total"] == 2 and row["aspects_pending"] == 1
    assert row["unrecovered_channels_count"] == 2


def test_search_run_leaves_every_synthesis_only_metric_unknown():
    """没跑合成 ⇒ 锚点/进 prompt 的证据数/每动作引用贡献一律 unknown。

    0 会被读成「一条证据都没进 prompt」「一个锚点都没有」,那是一句关于合成的
    假话:这条 run 压根没走到合成。`citation_contribution` 同理——每动作的
    `steps` 计数还在,但整张表的意义是"被引用了多少",在没有答案的 run 上
    不成立。
    """
    row = _search_row(_search_steps(reflect("ppr"), reflect("answer", True)))
    for key in ("anchors", "included_kg", "included_chunks",
                "included_elements", "citation_contribution"):
        assert row[key] is None, key
    assert row["reflect_turns"] == 2


def test_search_run_keeps_synthesis_metrics_when_a_synthesis_step_exists():
    """闸判的是「有没有 synthesis 步」,不是「是不是 search 这条路」。

    合成真的跑过的时候,那几列必须照常出——否则这个 unknown 会从"如实"变成
    "一律抹掉"。
    """
    steps = _search_steps(
        reflect("answer", True),
        step("synthesis", {"anchors": 4, "included_kg": 6,
                           "anchor_evidence_ids": ["e1"]}),
    )
    row = _search_row(steps)
    assert row["anchors"] == 4 and row["included_kg"] == 6
    assert isinstance(row["citation_contribution"], dict)


def test_search_run_rejects_a_policy_outside_the_closed_set():
    with pytest.raises(ValueError, match="unknown policy"):
        _search_row(_search_steps(), policy="reflect-v3")


def test_search_run_marks_kg_out_of_scope_when_the_caller_says_so():
    """`kg_in_scope` 由调用方的直接判定(一次 EXISTS)决定,盖掉轨迹反推。

    无图格里模型照样可能发出一个空手的图检索步,而那条步的形状会被
    `project_run` 读成"图有产出"。调用方手上的事实更硬。
    """
    row = _search_row(_search_steps(reflect("answer", True)), kg_in_scope=False)
    assert row["kg_in_scope"] is False


def test_search_run_records_whether_an_intent_contract_was_used():
    without = _search_row(_search_steps(reflect("answer", True)))
    with_intent = _search_row(
        _search_steps(reflect("answer", True)), has_intent_contract=True
    )
    assert without["has_intent_contract"] is False
    assert with_intent["has_intent_contract"] is True
    # 契约内容一个字都不进投影:行里只多一个布尔。
    assert set(without) == set(with_intent)
