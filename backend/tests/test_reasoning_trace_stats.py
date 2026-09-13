"""检索轨迹闭集投影的语义与隐私回归。

合成 fixture,不碰数据库、不碰模型。每个用例钉住一条**口径**,而不是一个当前
输出值:改了口径就该看见这里红,而不是「顺手把断言改成新值」。
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ASK_RETRIEVAL_LIMITS
from app.domain.reasoning_trace_stats import (
    RUN_PROJECTION_KEYS,
    TERMINATION_MODEL_DEGRADED,
    TERMINATION_MODEL_END,
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


def test_failed_run_keeps_workload_labels_without_zeroing_metrics():
    tags = {"effort": "deep", "question_key": "A-q01", "corpus_cell": "A_nokg"}
    row = project_run({"mode": "reasoning", "status": "failed"}, [], None,
                      rig_tags=tags)
    assert row["effort"] == "deep"
    assert row["question_key"] == "A-q01"
    assert row["corpus_cell"] == "A_nokg"
    assert row["status"] == "failed"
    assert row["total_ms"] is None
    assert row["anchors"] is None


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


def _search_row(steps, **overrides):
    kwargs = {
        "effort": "standard",
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
    row = _search_row(_search_steps(reflect("answer", True)))
    assert row["termination_reason"] == TERMINATION_MODEL_END
    assert row["termination_inferred"] is True


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


# --- T-BF6 收尾重排步 --------------------------------------------------------
def test_rerank_step_lands_in_durations_but_not_in_the_action_tallies():
    """`rerank`(T-BF6 收尾重排)只交代耗时:进 `durations_ms`,不进动作账。

    它是服务端每次收尾都会做的一段记账,不是模型选的一次检索动作——进
    `action_seq` 会给每条历史轨迹尾巴上挂一个恒定项,把「模型挑了哪些动作」这份
    序列稀释掉。

    变异 1:把 `rerank` 从 `STEP_TYPES` 里删掉 ⇒ 它被折成 `other`,第一条断言红。
    变异 2:把 `rerank` 从 `NON_ACTION_STEP_TYPES` 里删掉 ⇒ 后两条断言红。
    """
    steps = [
        step("ppr", {"phase": "seed", "found": 2}, duration_ms=30),
        reflect("answer", sufficient=True),
        step("rerank", {"queries": 2, "reused": 1, "researched": 1,
                        "researched_ms": 190000, "top_n": 20},
             duration_ms=195000),
        step("answer", {"kg": 5, "elements": 0}, duration_ms=2),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert row["durations_ms"]["rerank"] == 195000
    assert "other" not in row["durations_ms"]
    assert "rerank" not in row["action_seq"]
    assert "rerank" not in row["actions_by_type"]


def test_a_trace_without_a_rerank_step_still_projects():
    """历史行(以及所有关闭态的 run)没有 `rerank` 键,投影照读不误。

    新增一个 step_type 不许让旧轨迹变得不可投影:`durations_ms` 是按出现过的
    step_type 累加的开放字典,缺席就是没有这一项,不是 0、更不是报错。
    """
    steps = [
        step("ppr", {"phase": "seed", "found": 2}, duration_ms=30),
        step("answer", {"kg": 5, "elements": 0}, duration_ms=195000),
    ]
    row = project_run(JOB, steps, PAYLOAD)
    assert "rerank" not in row["durations_ms"]
    assert row["durations_ms"] == {"ppr": 30, "answer": 195000}
    assert row["total_ms"] == 195030


def test_recorded_retrieval_failure_wins_over_stale_inference():
    rows = [
        reflect("search_elements"),
        step("skip", {"reason": "stale_circuit_breaker", "stale": 3}),
        step("skip", {"reason": "retrieval_termination",
                      "termination": "retrieval_degraded"}),
    ]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == "retrieval_degraded"
    assert row["termination_inferred"] is False
    assert row["stale_breaker"] is True


def test_recorded_terminal_fact_is_not_overwritten_by_model_end_inference():
    rows = [reflect("answer", True), step("skip", {
        "reason": "retrieval_termination", "termination": "model_partial",
    })]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == "model_partial"
    assert row["termination_inferred"] is False


@pytest.mark.parametrize("kind", ["answer", "synthesis"])
def test_persisted_answer_terminal_fact_survives_export(kind):
    rows = [reflect("answer", True), step(kind, {
        "termination_reason": "model_degraded", "anchors": 2,
    })]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == "model_degraded"
    assert row["termination_inferred"] is False


def test_terminal_skip_fact_takes_precedence_over_synthesis_projection():
    rows = [
        step("skip", {"reason": "retrieval_termination",
                      "termination": "retrieval_degraded"}),
        step("synthesis", {"termination_reason": "model_sufficient"}),
    ]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == "retrieval_degraded"
    assert row["termination_inferred"] is False


@pytest.mark.parametrize("value", [None, "", "unsupported", "private source content", True, {}])
def test_invalid_recorded_terminal_fact_stays_unknown(value):
    rows = [reflect("answer", True), step("skip", {
        "reason": "retrieval_termination", "termination": value,
    })]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == UNKNOWN
    assert row["termination_inferred"] is False
    assert "private source content" not in json.dumps(row)


def test_unrelated_step_cannot_claim_a_terminal_fact():
    rows = [step("retrieve", {"termination_reason": "model_partial"}),
            reflect("answer", True)]
    row = project_run(JOB, rows, PAYLOAD)
    assert row["termination_reason"] == TERMINATION_MODEL_END
    assert row["termination_inferred"] is True
