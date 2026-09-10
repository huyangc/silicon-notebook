"""per-call 表:`llm.jsonl` ⋈ `events.jsonl`,一次模型调用一行。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§8.1「单次调用」那一行(本地序号、所属臂/run、`call_wall_ms`、状态码、已知重试
次数、响应字符数)与 §11「核心纯分析放 `backend/app/eval/reflect_context_bench.py`,
CLI 保持薄适配」。

**这个模块纯逻辑、零 I/O、零模型**:输入是两串已经解析好的记录(行列表),输出
是一串行。文件怎么找、按天分了几份、偏移读到哪里,全是 `scripts/reflect_shadow_rig.py`
的事;分开之后「日志 ⋈ 事件」这件唯一容易写错的事能在标准门里用 fixture 钉住,
而不必起一台真实模型服务。

分工与 `reflect_ab.slice_llm_usage` 的边界:那一个按**时间窗**把一批日志行压成
一个 run 的成本三键(run 粒度、聚合);这一个按 `support_id` 把日志行与调度事件
**对上号**(call 粒度、不聚合)。两者读的是同一批文件,回答的不是同一个问题,
不要互相代入。

**`None` 是一等值**(沿用 T0 §4.1):对不上号、日志缺字段、provider 不回 usage,
一律 `None` = unknown,**绝不折成 0**——0 会被读成「这次调用没有重试 / 没有缓存
命中的 prompt」,而那是一句关于这次调用的假话。

**命名红线**:这张表里没有、也不许有任何 `cache_hit` / 命中率 / hit rate 形状的
列。`status` 是 `app/core/llm.py` 写进日志的既有事实字段,原样透传;provider 自
己的前缀缓存只由 `cached_tokens` 这一个计数表达,它是**一个 token 数**,不是一个
比率,不要在它之上造分母。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.domain.reasoning_trace_stats import assert_projection_values

#: 携带 per-call 调度事实的**唯一**事件类型。`events.jsonl` 里带 `support_id` 的
#: 不止这一种(`model_json_repair` / `model_error` 也带),但那两种是诊断事件:
#: 一次调用可能一条都没有、也可能有好几条,拿它们当 ⋈ 的右表会把「这次调用修过
#: 一次 JSON」变成「这次调用有两条排队时长」。
CALL_EVENT_KIND = "model_scheduler"

#: `join` 列的闭集。**每一格都是一句关于归因质量的话**,不是错误码:
#:
#: * `joined` —— 这个 `support_id` 在两侧各恰好一行,所有列都可信;
#: * `log_only` —— 有日志行、没有调度事件(事件日志被关掉、或这次调用压根没经
#:   过调度边界)。传输侧的列可信,排队/执行时长 unknown;
#: * `event_only` —— 有调度事件、没有日志行。`LLM_LOG_ENABLED=false`、以及
#:   `app/core/llm.py` 那条本地响应缓存命中的出口(它不写日志行)都长这样;
#: * `ambiguous` —— 同一个 `support_id` 在某一侧不止一行。**不猜**该配哪一条:
#:   跨侧的列一律 unknown(§8.1「取不到不猜」);
#: * `unattributed` —— 这一行压根没有 `support_id`(旧日志、或调用没走
#:   `interaction_support_scope`)。它自己那一侧的数值仍然是真的,跨侧 unknown。
CALL_JOIN_STATES: tuple[str, ...] = (
    "joined", "log_only", "event_only", "ambiguous", "unattributed",
)

#: 从日志行取的列(键 → llm.jsonl 上的字段名)。`latency_ms` 改名成
#: `call_wall_ms` 是刻意的:设计 §8.1 的词表里这个量叫后者,而日志里那个名字是
#: `app/core/llm.py` 的历史字段名。两处指的是**同一个** `perf_counter` 差
#: (见 `_record_call_stats` 的说明:一次出口只读一次钟)。
LOG_INT_FIELDS: Mapping[str, str] = {
    "call_wall_ms": "latency_ms",
    "attempts": "attempts",
    "response_chars": "response_chars",
}

#: 从日志行 `usage` 里取的列。provider 不回就是 unknown——**不折成 0**
#: (设计 §8.2:不把 cached 缺失写成 0,也不由响应字符数反推隐藏思考量)。
USAGE_INT_FIELDS: tuple[str, ...] = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "reasoning_tokens",
)

#: 从调度事件取的整数列。
EVENT_INT_FIELDS: tuple[str, str] = ("queue_latency_ms", "execution_latency_ms")

#: 调用方按 run 打的标签列(实验控制维度,§8.1 最后一行)。缺一律 `None`
#: ——并发跑批时 run→call 的归因不成立,那时这几列**必须**是 unknown 而不是
#: 某个看起来合理的臂名。
CALL_TAG_KEYS: tuple[str, ...] = (
    "arm", "optimization", "question_key", "corpus_cell", "effort", "repeat",
)

#: `calls-<arm>.jsonl` 每一行允许出现的**全部**顶层键。隐私守卫按它断言
#: `set(row) ⊆` 这个集合,所以往行里加一个 prompt / response 片段会直接把用例
#: 打红(与 `reflect_ab.AB_PROJECTION_KEYS` 同一条纪律)。
CALL_ROW_KEYS: frozenset[str] = frozenset({
    "call_index", "support_id", "join",
    "kind", "status", "finish_reason",
    *LOG_INT_FIELDS,
    *USAGE_INT_FIELDS,
    "workload_id", "scheduler_status",
    *EVENT_INT_FIELDS,
    *CALL_TAG_KEYS,
})


def assert_call_row_closed(row: Mapping) -> None:
    """per-call 行的形状自检。rig 在写每一行之前都调它一次。"""
    extra = set(row) - CALL_ROW_KEYS
    if extra:
        raise ValueError(
            "call row carries keys outside CALL_ROW_KEYS: "
            + ", ".join(sorted(extra))
        )


def _count(raw: object) -> int | None:
    """数值列的取值:非数(缺席、`None`、字符串、`bool`)⇒ unknown,不是 0。

    `bool` 单独挡掉:`isinstance(True, int)` 为真,不挡的话一个写错类型的字段会
    静默落成 `1`。
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _support_id(record: Mapping) -> str | None:
    """一行的 `support_id`。空串 / 非串 ⇒ `None`(= 这行没法归因)。"""
    raw = record.get("support_id")
    return raw if isinstance(raw, str) and raw else None


def _short_code(raw: object) -> str | None:
    """闭集外的短码列(`kind` / `status` / `workload_id` …)的取值。

    这些字段的词表由写侧拥有(`app/core/llm.py` 与 `model_provider.py`),读侧
    照抄一份只会分叉;所以这里不校验**词面**,只校验**形状**——非串、空串一律
    `None`,值本身的短码性由 `assert_projection_values` 在写行之前统一兜住。
    """
    return raw if isinstance(raw, str) and raw else None


def _log_side(record: Mapping) -> dict:
    """一条 llm.jsonl 记录 → 这一行的传输侧数值列。**只取数值与短码。**

    请求正文、响应片段、模型名、`ts`、`id` 一个都不取:请求文本不进任何测量
    产物(设计 §8.2 / 计划 §4「刻意不做」)。
    """
    usage = record.get("usage")
    usage_map = usage if isinstance(usage, Mapping) else {}
    row: dict[str, Any] = {
        "kind": _short_code(record.get("kind")),
        "status": _short_code(record.get("status")),
        "finish_reason": _short_code(record.get("finish_reason")),
    }
    for key, source in LOG_INT_FIELDS.items():
        row[key] = _count(record.get(source))
    for key in USAGE_INT_FIELDS:
        row[key] = _count(usage_map.get(key))
    return row


def _event_side(event: Mapping) -> dict:
    """一条 `model_scheduler` 事件 → 这一行的调度侧数值列。

    `status` 在两侧都叫这个名字而语义不同(传输结果 vs 调度结果),所以调度侧
    那一个改叫 `scheduler_status`:合成一列会让「调度成功、传输失败」这种最该
    被看见的组合变成一个值。
    """
    row: dict[str, Any] = {
        "workload_id": _short_code(event.get("workload_id")),
        "scheduler_status": _short_code(event.get("status")),
    }
    for key in EVENT_INT_FIELDS:
        row[key] = _count(event.get(key))
    return row


_LOG_ONLY_UNKNOWN = {
    "workload_id": None, "scheduler_status": None,
    **{key: None for key in EVENT_INT_FIELDS},
}
_EVENT_ONLY_UNKNOWN = {
    "kind": None, "status": None, "finish_reason": None,
    **{key: None for key in LOG_INT_FIELDS},
    **{key: None for key in USAGE_INT_FIELDS},
}


def _tag_columns(tags: Mapping | None) -> dict:
    """实验控制维度那几列。给了什么就是什么,没给就是 unknown。"""
    given = tags or {}
    return {key: given.get(key) for key in CALL_TAG_KEYS}


def join_calls(
    llm_records: Sequence[Mapping],
    events: Sequence[Mapping],
    *,
    tags: Mapping | None = None,
) -> list[dict]:
    """`llm.jsonl` ⋈ `events.jsonl` on `support_id` → per-call 行列表。

    并发跑批时同一段时间窗里躺着好几个 run 的调用,时间窗切片切不干净——但
    `support_id` 是**一次调用自己**的相关号(`model_provider` 在调度那一刻铸,
    `interaction_support_scope` 用 ContextVar 带进日志行),窗口重叠伤不到它。
    所以这条 ⋈ 在并发下照样成立;真正在并发下不成立的是 run 级标签,由调用方
    传空 `tags`(见 `CALL_TAG_KEYS`)。

    三种残缺各有确定行为,都由 `join` 那一列如实说出来(见 `CALL_JOIN_STATES`),
    **一条都不丢**:

    * **缺事件** ⇒ `log_only`,调度侧列 unknown;
    * **缺日志** ⇒ `event_only`,传输侧列 unknown;
    * **一对多**(任一侧同号多行)⇒ 每一行各出一行 `ambiguous`,**跨侧列全部
      unknown**。这里刻意不按出现顺序 zip:两侧的行序没有任何一条保证能对上
      (日志在调用返回后写、事件在调度完成后写,两者不同线程不同时刻),zip
      出来的配对看起来完整,却是一串猜测。

    行序:先按日志行的原序铺开(`call_index` 就是这个序号),再把只有事件的那
    几组按事件原序接在后面。`call_index` 因此在一批里唯一且稳定,可以当本地
    序号用(§8.1「单次调用 · 本地序号」)。
    """
    logs_by_id: dict[str, list[Mapping]] = {}
    loose_logs: list[Mapping] = []
    for record in llm_records:
        if not isinstance(record, Mapping):
            continue
        key = _support_id(record)
        if key is None:
            loose_logs.append(record)
        else:
            logs_by_id.setdefault(key, []).append(record)
    events_by_id, loose_events = _index_events(events)
    tag_columns = _tag_columns(tags)
    rows: list[dict] = []
    for key, records in logs_by_id.items():
        matched = events_by_id.get(key, ())
        ambiguous = len(records) > 1 or len(matched) > 1
        for record in records:
            row = {**tag_columns, "support_id": key, **_log_side(record)}
            if ambiguous:
                rows.append({**row, "join": "ambiguous", **_LOG_ONLY_UNKNOWN})
            elif matched:
                rows.append({**row, "join": "joined", **_event_side(matched[0])})
            else:
                rows.append({**row, "join": "log_only", **_LOG_ONLY_UNKNOWN})
    for record in loose_logs:
        rows.append({
            **tag_columns, "support_id": None, "join": "unattributed",
            **_log_side(record), **_LOG_ONLY_UNKNOWN,
        })
    rows.extend(_event_only_rows(events_by_id, loose_events, logs_by_id,
                                 tag_columns))
    for index, row in enumerate(rows):
        row["call_index"] = index
        assert_call_row_closed(row)
        assert_projection_values(row)
    return rows


def _index_events(
    events: Sequence[Mapping],
) -> tuple[dict[str, list[Mapping]], list[Mapping]]:
    """调度事件按 `support_id` 分组。`CALL_EVENT_KIND` 之外的一律丢掉。"""
    grouped: dict[str, list[Mapping]] = {}
    loose: list[Mapping] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") != CALL_EVENT_KIND:
            continue
        key = _support_id(event)
        if key is None:
            loose.append(event)
        else:
            grouped.setdefault(key, []).append(event)
    return grouped, loose


def _event_only_rows(
    events_by_id: Mapping[str, list[Mapping]],
    loose_events: Sequence[Mapping],
    logs_by_id: Mapping[str, list[Mapping]],
    tag_columns: Mapping,
) -> list[dict]:
    """没有对应日志行的那几组事件。同号多条仍然是 `ambiguous`。

    「有事件没日志」不是异常:`LLM_LOG_ENABLED=false` 的整批、以及
    `app/core/llm.py` 那条本地响应缓存命中的出口(它 return 之前不写日志行)
    都落在这里。丢掉它们会让一批 run 的调用数凭空少掉一截,而少掉的恰好是最
    便宜的那些。

    事件侧那几列在 `ambiguous` 下**照样落值**:它们是这一条事件自己的事实,不
    是跨侧的猜测。`ambiguous` 标的是「这个号在某一侧不止一行,别把这几行当成
    同一次调用的不同侧面」。
    """
    rows: list[dict] = []
    for key, matched in events_by_id.items():
        if key in logs_by_id:
            continue
        state = "ambiguous" if len(matched) > 1 else "event_only"
        for event in matched:
            rows.append({
                **tag_columns, "support_id": key, "join": state,
                **_EVENT_ONLY_UNKNOWN, **_event_side(event),
            })
    for event in loose_events:
        rows.append({
            **tag_columns, "support_id": None, "join": "unattributed",
            **_EVENT_ONLY_UNKNOWN, **_event_side(event),
        })
    return rows
