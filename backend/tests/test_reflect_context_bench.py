"""per-call 表的纯分析(`app/eval/reflect_context_bench.py`)。

计划真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-snapshot-plan_zh.md`
§3 T-PS5 的验收「日志⋈事件三种残缺」;设计 §8.1「单次调用」那一行。

这里一次 I/O 都不做:输入是两串已经解析好的记录,输出是一串行。rig 那一侧的
薄适配(找文件、按偏移读增量)由 `test_reflect_t0_scripts.py` 覆盖。
"""
from __future__ import annotations

import pytest

import app.eval.reflect_context_bench as bench
from app.domain.reasoning_trace_stats import assert_projection_values
from app.eval.reflect_context_bench import (
    CALL_EVENT_KIND,
    CALL_JOIN_STATES,
    CALL_ROW_KEYS,
    CALL_TAG_KEYS,
    assert_call_row_closed,
    join_calls,
)


def _log(support_id: str = "mdl-abc", **overrides) -> dict:
    """一条 llm.jsonl 终态行。字段名与 `app/core/llm.py` 写出去的逐字相同。"""
    record = {
        "ts": "2026-09-10T10:00:00",
        "id": "llm-0001",
        "kind": "chat",
        "model": "glm-4.6",
        "status": "ok",
        "latency_ms": 2100,
        "attempts": 2,
        "response_chars": 480,
        "usage": {
            "prompt_tokens": 9000, "completion_tokens": 120,
            "total_tokens": 9120, "cached_tokens": 7800,
        },
        "finish_reason": "stop",
        # 正文两块:它们**必须**一个字都不进 per-call 行。
        "request": {"messages": [{"role": "user", "content": "只看 Qwen-VL"}]},
        "response": {"content": "它这样处理图像输入 [k1]。"},
    }
    if support_id:
        record["support_id"] = support_id
    record.update(overrides)
    return record


def _event(support_id: str = "mdl-abc", **overrides) -> dict:
    """一条 `model_scheduler` 事件。字段名与 `model_provider._emit` 逐字相同。"""
    event = {
        "kind": CALL_EVENT_KIND,
        "status": "ok",
        "workload_id": "reasoning_reflect",
        "workload_label": "反思",
        "service_id": "svc-1",
        "model": "glm-4.6",
        "queue_latency_ms": 35,
        "execution_latency_ms": 2050,
    }
    if support_id:
        event["support_id"] = support_id
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# 完整配对
# ---------------------------------------------------------------------------


def test_a_clean_join_carries_both_sides_and_no_request_text():
    rows = join_calls([_log()], [_event()])
    assert len(rows) == 1
    row = rows[0]
    assert row["join"] == "joined"
    assert row["support_id"] == "mdl-abc"
    # 传输侧:`latency_ms` 在这张表里叫 `call_wall_ms`(设计 §8.1 的词表)。
    assert row["call_wall_ms"] == 2100
    assert row["attempts"] == 2
    assert row["response_chars"] == 480
    assert row["prompt_tokens"] == 9000
    assert row["cached_tokens"] == 7800
    assert row["finish_reason"] == "stop"
    assert row["status"] == "ok"
    # 调度侧:两个 status 分两列,合成一列会让「调度成功、传输失败」变成一个值。
    assert row["scheduler_status"] == "ok"
    assert row["workload_id"] == "reasoning_reflect"
    assert row["queue_latency_ms"] == 35
    assert row["execution_latency_ms"] == 2050
    assert row["call_index"] == 0
    assert set(row) <= CALL_ROW_KEYS
    assert_projection_values(row)


def test_every_join_state_is_in_the_closed_vocabulary():
    """`join` 是闭集:多一格没登记的状态会让读侧的分组表凭空多一行。"""
    rows = join_calls(
        [_log("mdl-a"), _log("mdl-dup"), _log("mdl-dup"), _log("")],
        [_event("mdl-a"), _event("mdl-dup"), _event("mdl-only"), _event("")],
    )
    assert {row["join"] for row in rows} <= set(CALL_JOIN_STATES)
    assert {row["join"] for row in rows} == {
        "joined", "ambiguous", "event_only", "unattributed"}


# ---------------------------------------------------------------------------
# 三种残缺(计划 §3 T-PS5 验收)
# ---------------------------------------------------------------------------


def test_a_log_row_without_its_scheduler_event_keeps_the_transport_columns():
    """**缺事件**:传输侧照落,排队/执行时长 unknown——不是 0。

    0 会被读成「这次调用零排队」,而真相是「没有这条事实」。事件日志被关掉、
    或调用压根没经过调度边界时都长这样。
    """
    rows = join_calls([_log("mdl-lonely")], [])
    assert [row["join"] for row in rows] == ["log_only"]
    row = rows[0]
    assert row["call_wall_ms"] == 2100 and row["attempts"] == 2
    assert row["queue_latency_ms"] is None
    assert row["execution_latency_ms"] is None
    assert row["workload_id"] is None and row["scheduler_status"] is None


def test_a_scheduler_event_without_its_log_row_still_lands_a_row():
    """**缺日志**:整行照落,传输侧 unknown。

    丢掉它会让一批 run 的调用数凭空少掉一截,而少掉的恰好是最便宜的那些
    (`LLM_LOG_ENABLED=false`、以及本地响应缓存命中那条不写日志行的出口)。
    """
    rows = join_calls([], [_event("mdl-nolog")])
    assert [row["join"] for row in rows] == ["event_only"]
    row = rows[0]
    assert row["queue_latency_ms"] == 35
    assert row["call_wall_ms"] is None
    assert row["attempts"] is None
    assert row["prompt_tokens"] is None and row["cached_tokens"] is None
    assert row["status"] is None


def test_a_support_id_with_fan_out_is_unknown_across_sides_never_guessed():
    """**一对多**:切不干净就标 unknown,不按顺序 zip 猜配对。

    两侧的行序没有任何一条保证能对上(日志在调用返回后写、事件在调度完成后写,
    不同线程不同时刻),zip 出来的配对看起来完整,却是一串猜测。
    """
    rows = join_calls(
        [_log("mdl-dup", latency_ms=100), _log("mdl-dup", latency_ms=900)],
        [_event("mdl-dup", queue_latency_ms=1),
         _event("mdl-dup", queue_latency_ms=2)],
    )
    assert [row["join"] for row in rows] == ["ambiguous", "ambiguous"]
    # 自己那一侧的数值是真的,照落;跨侧的一律 unknown。
    assert [row["call_wall_ms"] for row in rows] == [100, 900]
    assert all(row["queue_latency_ms"] is None for row in rows)
    assert all(row["workload_id"] is None for row in rows)


def test_one_log_against_two_events_is_ambiguous_and_drops_the_event_copies():
    """扇出在**任一侧**都算切不干净,不只是日志那一侧多行时。

    这一条同时钉住 docstring 里那句「哪些行会被刻意丢掉」:同号事件的副本**不**
    各出一行。日志侧那一行已经带着这个号进表并标了 `ambiguous`,再把两条事件铺
    开会让一次调用在表里变成三行,而多出来的两行连属不属于同一次调用都不知道。
    """
    rows = join_calls([_log("mdl-dup")], [_event("mdl-dup"), _event("mdl-dup")])
    assert [row["join"] for row in rows] == ["ambiguous"]
    assert rows[0]["queue_latency_ms"] is None


# ---------------------------------------------------------------------------
# 重试行:同号、非终态(`RETRY_STATUS`)
# ---------------------------------------------------------------------------


def test_a_retry_row_does_not_make_its_terminal_call_ambiguous():
    """重试过的调用照样两侧齐全——它正是最该被看见的那一批。

    `app/core/llm.py` 每次瞬时错误重试都再写一行,与终态行**同号**(重试循环整
    个在 `interaction_support_scope` 内),而调度事件只有一条。按行数判扇出会让
    每一次重试过的调用都落成两行 `ambiguous`、排队/执行时长与 workload 全线
    unknown,而下游按 `join == "joined"` 算分位数时被滤掉的恰好是最慢的那些。
    """
    retry = _log("mdl-abc", status="retry", latency_ms=300, attempt=0)
    retry.pop("attempts")          # 重试行刻意不带 `attempts`(计数还在爬)
    rows = join_calls([retry, _log("mdl-abc")], [_event("mdl-abc")])
    by_join = {row["join"]: row for row in rows}
    assert set(by_join) == {"retry", "joined"}
    # 终态行:调度侧列一个都不丢。
    assert by_join["joined"]["attempts"] == 2
    assert by_join["joined"]["queue_latency_ms"] == 35
    assert by_join["joined"]["execution_latency_ms"] == 2050
    assert by_join["joined"]["workload_id"] == "reasoning_reflect"
    # 重试行:自己那一侧的墙钟是真的,跨侧 unknown(事件属于终态行)。
    assert by_join["retry"]["call_wall_ms"] == 300
    assert by_join["retry"]["attempts"] is None
    assert by_join["retry"]["queue_latency_ms"] is None
    assert by_join["retry"]["workload_id"] is None
    # 「一次调用一格」:重试行不占 `call_index`,数格子不会把调用数数成两次。
    assert by_join["retry"]["call_index"] is None
    assert by_join["joined"]["call_index"] == 0
    assert "retry" in CALL_JOIN_STATES


def test_a_call_that_only_ever_retried_still_lands_its_scheduler_event():
    """只剩重试行的号(重试到一半整批被掐):那条事件仍然要落表。

    判「有没有日志」若按「有没有任何日志行」,这个号会被当成已有日志而把唯一
    一条调度事件整条丢掉,排队时长凭空消失。判据因此是「有没有**终态**行」。
    """
    retry = _log("mdl-gone", status="retry", latency_ms=120)
    rows = join_calls([retry], [_event("mdl-gone")])
    by_join = {row["join"]: row for row in rows}
    assert set(by_join) == {"retry", "event_only"}
    assert by_join["event_only"]["queue_latency_ms"] == 35
    assert by_join["event_only"]["call_index"] == 0


def test_a_row_without_a_support_id_is_unattributed_not_dropped():
    """没有相关号的行仍然进表:它自己那一侧的数值是真的。"""
    rows = join_calls([_log("")], [_event("", queue_latency_ms=7)])
    assert [row["join"] for row in rows] == ["unattributed", "unattributed"]
    assert rows[0]["support_id"] is None
    assert rows[0]["call_wall_ms"] == 2100
    assert rows[1]["queue_latency_ms"] == 7


def test_an_empty_string_support_id_is_the_same_as_no_support_id():
    """`interaction_support_scope` 没设时 ContextVar 的默认值就是**空串**。

    `_log("")` 造的是「压根没这个键」;这条造的是「键在、值是空串」——两者在
    `LLMInteractionLogger.log` 那一侧都对应「没有相关号」,读侧不能只认其中一种。
    """
    record = _log()
    record["support_id"] = ""
    rows = join_calls([record], [])
    assert rows[0]["support_id"] is None
    assert rows[0]["join"] == "unattributed"


# ---------------------------------------------------------------------------
# 归因只认 `support_id`,不认时间窗
# ---------------------------------------------------------------------------


def test_interleaved_calls_still_pair_by_support_id():
    """并发下两次调用的日志与事件交错落盘,⋈ 照样对得上号。

    这正是这条表在并发下仍然成立的理由:`support_id` 是**一次调用自己**的相关
    号(`model_provider` 在调度那一刻铸),窗口重叠伤不到它。
    """
    rows = join_calls(
        [_log("mdl-a", latency_ms=10), _log("mdl-b", latency_ms=20)],
        [_event("mdl-b", queue_latency_ms=2), _event("mdl-a", queue_latency_ms=1)],
    )
    by_id = {row["support_id"]: row for row in rows}
    assert by_id["mdl-a"]["call_wall_ms"] == 10
    assert by_id["mdl-a"]["queue_latency_ms"] == 1
    assert by_id["mdl-b"]["call_wall_ms"] == 20
    assert by_id["mdl-b"]["queue_latency_ms"] == 2
    assert all(row["join"] == "joined" for row in rows)


def test_only_scheduler_events_are_joined():
    """诊断事件也带 `support_id`,但它们不是 per-call 的调度事实。

    一次调用可能一条 `model_json_repair` 都没有、也可能有好几条,拿它们当右表
    会把「这次调用修过一次 JSON」变成「这次调用有两条排队时长」。
    """
    rows = join_calls(
        [_log("mdl-a")],
        [{"kind": "model_json_repair", "support_id": "mdl-a",
          "status": "repaired", "reason": "trailing_comma"}],
    )
    assert [row["join"] for row in rows] == ["log_only"]
    assert rows[0]["queue_latency_ms"] is None


# ---------------------------------------------------------------------------
# 标签、闭集与隐私
# ---------------------------------------------------------------------------


def test_run_tags_are_stamped_on_every_row():
    tags = {
        "arm": "v2", "optimization": "prefix_snapshot", "question_key": "A-q08",
        "corpus_cell": "A_nokg", "effort": "standard", "repeat": 2,
    }
    rows = join_calls([_log(), _log("mdl-two")], [_event()], tags=tags)
    assert len(rows) == 2
    for row in rows:
        for key, value in tags.items():
            assert row[key] == value


def test_without_tags_every_control_column_is_unknown_not_absent():
    """并发跑批那一批:run 级标签必须是 unknown,而不是某个看起来合理的臂名。

    列**在**而值是 `None`,不是整列不写:两批行(串行的与并发的)要能放进同
    一张表,键集一次一半地变会让存档过的行与后来的行对不上。
    """
    rows = join_calls([_log()], [_event()], tags=None)
    for key in CALL_TAG_KEYS:
        assert key in rows[0]
        assert rows[0][key] is None


def test_no_request_or_response_text_reaches_the_table():
    """隐私守卫:请求正文、响应片段、模型名、`ts`、`id` 一个都不进。"""
    rows = join_calls([_log()], [_event()])
    blob = repr(rows)
    for leaked in ("只看 Qwen-VL", "图像输入", "glm-4.6", "llm-0001",
                   "2026-09-10T10:00:00", "反思"):
        assert leaked not in blob
    assert "request" not in rows[0] and "response" not in rows[0]


def test_a_key_outside_the_closed_set_is_refused():
    with pytest.raises(ValueError, match="CALL_ROW_KEYS"):
        assert_call_row_closed({"support_id": "mdl-a", "prompt": "问题原文"})


def test_no_column_is_named_after_a_cache_hit_rate():
    """命名红线:这张表里不许有 cache_hit / 命中率形状的列。

    provider 自己的前缀缓存只由 `cached_tokens` 这一个**计数**表达,它不是一个
    比率;`status` 是 llm.jsonl 的既有事实字段,原样透传,不据它造分母。
    """
    for key in CALL_ROW_KEYS:
        lowered = key.lower()
        assert "cache_hit" not in lowered
        assert "hit_rate" not in lowered
        assert "hitrate" not in lowered


def test_a_missing_usage_block_is_unknown_not_zero():
    """provider 不回 usage ⇒ token 列 unknown。写 0 会把「没量到」说成「没花钱」。"""
    rows = join_calls([_log(usage=None)], [_event()])
    for key in ("prompt_tokens", "completion_tokens", "cached_tokens",
                "reasoning_tokens", "total_tokens"):
        assert rows[0][key] is None, key


def test_a_wordy_provider_finish_reason_is_unknown_not_an_exception():
    """provider 回一句人话 ⇒ 那一格 unknown,**不是**整批中止。

    `finish_reason` 是 `app/core/llm.py` 把 provider 返回值原样存下的那一个,而
    这个仓库明确要伺候 thin OpenAI-compatible servers。抛出去的代价不是丢一格:
    串行路这张表在 `_run_ab_arm` 的 `return` 表达式里求值,整批会在第 N 个单元
    中止、那个单元两行还没写盘就没了,前面几百次模型调用白烧。
    """
    rows = join_calls(
        [_log("mdl-abc", finish_reason="max tokens reached")], [_event()],
    )
    assert rows[0]["finish_reason"] is None
    assert rows[0]["join"] == "joined"
    # 别的列一个都不受影响——降级只降那一格。
    assert rows[0]["call_wall_ms"] == 2100
    assert rows[0]["queue_latency_ms"] == 35
    assert_projection_values(rows[0])


def test_an_over_long_support_id_still_joins_but_projects_as_unknown():
    """`support_id` 作 ⋈ 的**键**用原串,作投影**值**过短码形状。

    `model_safety` 那侧允许到 84 字符,投影短码上限是 64:超长的号仍然要能把
    日志行与它的调度事件对上号(归因不能因为它长而失效),只是那一格落 unknown。
    """
    long_id = "mdl-" + "a" * 80
    assert len(long_id) > 64
    rows = join_calls([_log(long_id)], [_event(long_id)])
    assert len(rows) == 1
    assert rows[0]["join"] == "joined"        # ⋈ 照做
    assert rows[0]["support_id"] is None      # 投影值 unknown
    assert rows[0]["queue_latency_ms"] == 35
    assert_projection_values(rows[0])


def test_every_row_is_self_checked_not_just_the_first_one(monkeypatch):
    """闭集/隐私自检是**逐行**的:脏的那一类行往往不是第 0 行。

    `_event_side` 哪天多带一个字段,只有 event-only 行会脏,而那类行恒排在日志
    行后面(行序:先日志、后只有事件的那几组)。只验 `rows[0]` 的写法对这一整类
    失败是瞎的——守卫本身也需要一层守卫。
    """
    clean = join_calls([_log("mdl-a")], [_event("mdl-only")])
    assert [row["join"] for row in clean] == ["log_only", "event_only"]

    original = bench._event_side
    monkeypatch.setattr(
        bench, "_event_side",
        lambda event: {**original(event), "prompt": "问题原文"},
    )
    # 第 0 行(log_only)干净、第 1 行(event_only)脏 ⇒ 整批必须炸。
    with pytest.raises(ValueError, match="CALL_ROW_KEYS"):
        join_calls([_log("mdl-a")], [_event("mdl-only")])


def test_a_boolean_never_becomes_a_count():
    """`isinstance(True, int)` 为真;不挡的话一个写错类型的字段会静默落成 1。"""
    rows = join_calls([_log(attempts=True)], [])
    assert rows[0]["attempts"] is None


def test_call_index_is_dense_and_unique_across_the_batch():
    rows = join_calls(
        [_log("mdl-a"), _log("mdl-b")],
        [_event("mdl-a"), _event("mdl-c")],
    )
    assert [row["call_index"] for row in rows] == list(range(len(rows)))


def test_non_mapping_records_are_skipped_without_blowing_up():
    """日志被别的东西污染过(半行、数组)不该让整张表出不来。"""
    rows = join_calls([_log(), "garbage", None], [_event(), 42])
    assert len(rows) == 1
    assert rows[0]["join"] == "joined"
