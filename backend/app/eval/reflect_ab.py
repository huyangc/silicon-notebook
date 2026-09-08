"""reflect v2 开闸前 A/B:一次**完整 Ask** run 的闭集投影与 gold 判据。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-ab-design_zh.md`
(§3.2 gold 形态、§5.5 硬断言、§7 投影与隐私),上游是同目录的
`2026-09-07-retrieval-reflect-final-design_zh.md` §9.2。T0 的投影真源是
`app.domain.reasoning_trace_stats`;**这里只做它之上的增量**,不复制它的任何
一条口径。

分工与 `scripts/reflect_shadow_rig.py` 的边界:这个模块**纯逻辑、零 I/O、零
模型**——所有需要连库才能拿到的事实(锚点 → 元素 → `section_path`、来源 →
标题、LLM 日志行)都由 rig 查好、以 Mapping 形式传进来。这样 §7.1 里最容易
写错的那几条(三跳解析的 unknown 路径、缺 gold 的跳过、成本三键的时间窗切片)
全都能在标准门里用 fixture 钉住,而不必起一台真实模型服务。

**`None` 是一等值**(沿用 T0 §4.1):解析不到、没有 gold、并发下切不干净的
成本键,一律 `None` = unknown,**绝不折成 0 / False**——0 会被读成「一个都
没命中」,而那是一句关于这次 run 的假话。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.domain.reasoning_trace_stats import (
    RUN_PROJECTION_KEYS,
    assert_projection_values,
    project_run,
)
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys

# --- 闭集键 -----------------------------------------------------------------

#: §7.1 在 T0 `RUN_PROJECTION_KEYS` 之上新增的键。判分模型键(`gold_facts_*` /
#: `aspects_*`)、人工键(`human_*`)与带传染标的 `early_stop` 也在这里登记:
#: T-AB2 只把它们写成 `None`(留给 T-AB3 的 `ab-judge` 填),但键集**现在就
#: 闭上**——投影键集是数据集的形状合同,一次一半地扩它会让存档过的行与后来的
#: 行没法放进同一张表。
AB_ONLY_KEYS: frozenset[str] = frozenset({
    # --- 确定性(本任务全部实现) ---
    "answer_chars",
    "citations",
    "anchors_on_gold",
    "anchors_total",
    "anchors_unresolved",
    "citations_out_of_scope",
    "completeness_claim",
    "invalid_tool_calls",
    "model_calls",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms_total",
    "finish_reason_codes",
    "answer_empty",
    "paired",
    "arm",
    "repeat",
    "model_contract",
    "corpus_signature",
    # --- 判分模型 / 人工(T-AB3;本任务恒 None) ---
    "gold_facts_total",
    "gold_facts_hit",
    "aspects_declared",
    "aspects_missed",
    "human_factual_error",
    "human_completeness_false",
    "human_citation_bad",
    "early_stop",
})

#: `ab-runs.jsonl` 每一行允许出现的**全部**顶层键。隐私守卫(见
#: `backend/tests/test_reflect_ab.py`)断言 `set(row) ⊆` 这个集合,所以往投影
#: 里加一个 `answer` / `question` / `*_id` 键会直接把用例打红(§7.3、§10-2)。
AB_PROJECTION_KEYS: frozenset[str] = RUN_PROJECTION_KEYS | AB_ONLY_KEYS

#: 臂的闭集。与 T0 的 `POLICY_VERSIONS` 同值而**不同义**:`arm` 是 rig 的声明,
#: `policy_version` 是从这次 run 自己的轨迹反推出来的证据(§7.1)。
ARMS: tuple[str, ...] = ("legacy", "v2")

#: 本任务不实现、但键集已经闭上的那几个(§12 T-AB3)。投影一律写 `None`。
DEFERRED_JUDGED_KEYS: tuple[str, ...] = (
    "gold_facts_total", "gold_facts_hit", "aspects_declared", "aspects_missed",
    "human_factual_error", "human_completeness_false", "human_citation_bad",
    "early_stop",
)


def assert_ab_closed(row: Mapping) -> None:
    """A/B 投影行的形状自检。rig 在写每一行之前都调它一次。

    与 `reasoning_trace_stats.assert_closed` 同一形状、不同键集:那一个盯的是
    T0 的 `RUN_PROJECTION_KEYS`,A/B 行比它多 §7.1 那一批。**值**的形状仍由
    `assert_projection_values` 管——那一条不看键名,所以往闭集内任何一个键里
    塞一段自由文本都会被同一道闸拦住,不需要在这里再写一份。
    """
    extra = set(row) - AB_PROJECTION_KEYS
    if extra:
        raise ValueError(
            "ab projection row carries keys outside AB_PROJECTION_KEYS: "
            + ", ".join(sorted(extra))
        )


# --- gold 的加载与校验(§3.2 / §5.5-6) --------------------------------------


class GoldError(ValueError):
    """gold 配置本身有问题。**跑批之前**响亮失败,不静默退化成 0(§3.2)。"""


#: 一道题最多 3 条 `gold_facts`(§3.2)。这不是一个可调的舒适度阈值:`gold_facts`
#: 刻意**不覆盖整题**,它是「这几点必须在」的低方差粗筛量;放开条数会让它变成
#: 一个伪装成客观分的主观分。
GOLD_FACTS_MAX = 3
#: 每条 `gold_facts` 的字数上限(§3.2)。超了说明写的是提纲而不是可当场核对的
#: 短断言。
GOLD_FACT_MAX_CHARS = 40


@dataclass(frozen=True)
class AbGold:
    """一道题的 gold。三个字段都可能为空 = 这道题在对应指标上**跳过**。

    * `gold_section_path`:A 格(单篇)已有的字段,锚点按元素的
      `metadata.section_path` 前缀匹配它;
    * `gold_sources`:B 格的来源短名(论文短名),锚点按来源标题包含它匹配;
    * `gold_facts`:判分模型那一层要用的 ≤3 条短断言。T-AB2 只加载与校验它,
      不消费——消费在 T-AB3。
    """

    question_key: str
    corpus: str
    gold_section_path: tuple[str, ...] = ()
    gold_sources: tuple[str, ...] = ()
    gold_facts: tuple[str, ...] = ()

    @property
    def has_anchor_gold(self) -> bool:
        """这道题能不能算 `anchors_on_gold`。两个都空 ⇒ 该键恒 unknown。"""
        return bool(self.gold_section_path or self.gold_sources)


def _gold_strings(raw: object, *, field: str, key: str) -> tuple[str, ...]:
    """gold 的字符串列表字段。**形状不对就抛**,不做「尽力而为」的清洗。

    一个写错的 gold 字段(写成字符串而不是列表、混进 null)如果被悄悄清洗成
    一个更短的列表,这道题就会在整批数据里安静地少算命中——那正是 §3.2 要求
    「跑批之前响亮失败」的那类事故。
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise GoldError(f"{key}: {field} 必须是字符串列表,拿到 {type(raw).__name__}")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise GoldError(f"{key}: {field} 里有一项不是非空字符串: {item!r}")
        values.append(item.strip())
    return tuple(values)


def load_ab_gold(questions: Mapping) -> dict[str, AbGold]:
    """题集 → `{question_key: AbGold}`。畸形 gold 一律抛 `GoldError`。

    读的是 T0 那份**同一个** `questions.json`(§3.1 原地扩充,不复制不改名),
    所以这里既能拿到 A 格早就有的 `gold_section_path`,也能拿到 A/B 要补的
    `gold_sources` / `gold_facts`。题集里还没写 gold 的题不是错误——它返回一个
    三个字段都空的 `AbGold`,消费侧据此把相关键落成 `None`(§3.2「没 gold 的题
    在该指标上跳过,不计 0」)。

    **英文题面(`<key>-en`)共用同一份 gold**:题面语言不改变原文里那条事实
    落在哪一节、出自哪一篇。rig 的 `question_key` 带 `-en` 后缀,查 gold 之前
    先剥掉它(见 `gold_for`)。
    """
    out: dict[str, AbGold] = {}
    for row in questions.get("ask") or ():
        if not isinstance(row, Mapping):
            raise GoldError(f"题集 ask 里有一项不是对象: {row!r}")
        key = str(row.get("key") or "")
        if not key:
            raise GoldError("题集 ask 里有一项没有 key")
        facts = _gold_strings(row.get("gold_facts"), field="gold_facts", key=key)
        if len(facts) > GOLD_FACTS_MAX:
            raise GoldError(
                f"{key}: gold_facts 至多 {GOLD_FACTS_MAX} 条,拿到 {len(facts)} 条"
            )
        for fact in facts:
            if len(fact) > GOLD_FACT_MAX_CHARS:
                raise GoldError(
                    f"{key}: gold_facts 每条至多 {GOLD_FACT_MAX_CHARS} 字,"
                    f"这一条 {len(fact)} 字"
                )
        out[key] = AbGold(
            question_key=key,
            corpus=str(row.get("corpus") or ""),
            gold_section_path=_gold_strings(
                row.get("gold_section_path"), field="gold_section_path", key=key
            ),
            gold_sources=_gold_strings(
                row.get("gold_sources"), field="gold_sources", key=key
            ),
            gold_facts=facts,
        )
    return out


def base_question_key(question_key: str) -> str:
    """`A-q01-en` → `A-q01`。gold 与题面语言无关(见 `load_ab_gold`)。"""
    return question_key[:-3] if question_key.endswith("-en") else question_key


def gold_for(gold_by_key: Mapping[str, AbGold], question_key: str) -> AbGold | None:
    return gold_by_key.get(base_question_key(question_key))


def resolve_gold_sources(
    gold: AbGold, source_rows: Sequence[Mapping]
) -> dict[str, str]:
    """`gold_sources` 的短名 → 测试库里的 `source_id`。§5.5-6 的硬断言。

    `source_rows` 是 `[{"id":…, "title":…}]`,由 rig 只读查出来。匹配按**子串**
    ——与 `reflect_shadow_rig.resolve_scope_source_ids` 同一条约定,理由也一样:
    测试库的 `sources.title` 是「主库标题前面多一段存储层前缀」的超串,写精确
    相等则永远解析不到。

    每个短名必须**恰好**命中一个来源。命中 0 个或 ≥2 个都抛 `GoldError`:
    §3.2 明确「解析不到任何一个就是配置错误,跑批之前响亮失败,不能静默变成
    0」——命中多个同样坏,那意味着这条 gold 指的是哪一篇根本没定下来。
    """
    resolved: dict[str, str] = {}
    for name in gold.gold_sources:
        matches = [
            str(row.get("id") or "")
            for row in source_rows
            if name in str(row.get("title") or "")
        ]
        if len(matches) != 1:
            raise GoldError(
                f"{gold.question_key}: gold_sources 短名 {name!r} 在测试库里命中 "
                f"{len(matches)} 个来源(必须恰好 1 个)"
            )
        resolved[name] = matches[0]
    return resolved


# --- 锚点判据(§7.1) --------------------------------------------------------


def _anchor_field(anchor: object, field: str) -> str:
    if isinstance(anchor, Mapping):
        return str(anchor.get(field) or "")
    return str(getattr(anchor, field, "") or "")


def count_anchors_on_gold(
    anchors: Sequence[object],
    gold: AbGold | None,
    *,
    element_sections: Mapping[str, str] | None,
    source_titles: Mapping[str, str] | None,
) -> int | None:
    """落在 gold 上的**不同**锚点数。任一跳解析不到 ⇒ `None`,绝不写 0。

    两种格式(§7.1):

    * **A 格**:锚点 → `element_id` → `source_elements.metadata.section_path`,
      按前缀匹配 `gold_section_path`。`source_elements` 表**没有** `section_path`
      列(2026-09-09 复核 `0001_initial.sql`:该表只有 id / source_id /
      element_type / location_label / text / metadata / created_at / ordinal),
      这个值住在 `metadata` 这一列的 jsonb 里,两侧仓储都按
      `metadata.get("section_path")` 读(`postgres/catalog_store.py:445`、
      `sqlite/catalog_store.py:539`)。
    * **B 格**:锚点 → `source_id` → 标题包含 `gold_sources` 的某个短名。

    「解析不到」这三个字的口径,是这个函数里唯一容易写错的地方,所以写死在
    这里:

    * 查询表整体缺席(`element_sections is None`)= rig 那一步查库失败 ⇒ 整键
      unknown;
    * 一个**带了 id** 的锚点,其 id 在查询表里查不到(或查到空值)= 一次真实
      的解析失败 ⇒ 整键 unknown;
    * 一个**根本没带这个 id** 的锚点(KG 对象锚点没有 `element_id`)≠ 解析
      失败:它是另一类锚点,不进分子,但照常进 `anchors_total` 的分母。把它
      当成解析失败会让几乎每个 run 的这一列都变成 unknown,那等于把这条判据
      废掉。
    """
    if gold is None or not gold.has_anchor_gold:
        return None
    if gold.gold_section_path:
        return _anchors_on_section_gold(
            anchors, gold.gold_section_path, element_sections
        )
    return _anchors_on_source_gold(anchors, gold.gold_sources, source_titles)


def _anchors_on_section_gold(
    anchors: Sequence[object],
    gold_section_path: Sequence[str],
    element_sections: Mapping[str, str] | None,
) -> int | None:
    if element_sections is None:
        return None
    hits: set[str] = set()
    for anchor in anchors:
        element_id = _anchor_field(anchor, "element_id")
        if not element_id:
            continue
        if element_id not in element_sections:
            return None
        section = str(element_sections.get(element_id) or "")
        if not section:
            return None
        if any(section.startswith(prefix) for prefix in gold_section_path):
            hits.add(element_id)
    return len(hits)


def _anchors_on_source_gold(
    anchors: Sequence[object],
    gold_sources: Sequence[str],
    source_titles: Mapping[str, str] | None,
) -> int | None:
    if source_titles is None:
        return None
    hits: set[str] = set()
    for anchor in anchors:
        source_id = _anchor_field(anchor, "source_id")
        if not source_id:
            continue
        if source_id not in source_titles:
            return None
        title = str(source_titles.get(source_id) or "")
        if not title:
            return None
        if any(name in title for name in gold_sources):
            hits.add(source_id)
    return len(hits)


def count_unresolved_anchors(
    answer: str, anchors: Sequence[object]
) -> int:
    """答案里引了、却没有对应服务端签发证据的**不同**锚点键数(§7.1)。

    这是一个**纯**判据:`EvidenceContext.parse_anchors` 只为「键在
    `evidence_by_id` 里」的标记造锚点,所以 `AskResponse.anchors` 里结构上不
    可能出现解析不到的锚点——差集只能从答案正文这一侧来。

    正文侧用 `LOOSE_MARKER_RE`(而不是 `parse_anchors` 用的 `MARKER_RE`)是
    刻意的:两者的差(`[ k1 ]` 这类带空白的写法)本身就是一次「模型引了、系统
    没认出来」的真实失败,应当计进这个数,而不是从两边同时消失。
    """
    emitted = {
        key
        for marker in LOOSE_MARKER_RE.findall(answer or "")
        for key in marker_keys(marker)
    }
    resolved = {_anchor_field(anchor, "key") for anchor in anchors}
    return len(emitted - resolved)


def count_citations_out_of_scope(
    citations: Sequence[object],
    *,
    allowed_source_ids: frozenset[str] | None,
    notebook_id: str,
) -> int | None:
    """引用解析到本 run 声明范围之外的条数(§7.1;§9 硬判据 1)。

    `allowed_source_ids` 是这次 run 的范围上限:题目声明了 `scope_source_titles`
    时是解析出来的那几个 id,否则是这个笔记本自己的全部来源。查不出来
    (`None`)⇒ 整键 unknown,不当 0——「没查到范围」和「一条都没越界」是两件
    事,而这一列进的是硬判据。

    两类越界各算一条:

    * 引用的 `source_id` 非空、却不在允许集合里;
    * 引用的 `notebook_id` 非空、且不是本次 run 的笔记本(跨库命中)。

    `source_id` 为空的引用(记忆命中、纯 KG 对象)**不计**:它们不携带来源
    身份,判不出在不在范围内,而把「判不出」记成「越界」会让这条硬判据在每个
    带记忆的 run 上假红。
    """
    if allowed_source_ids is None:
        return None
    out = 0
    for citation in citations:
        source_id = _anchor_field(citation, "source_id")
        cited_notebook = _anchor_field(citation, "notebook_id")
        if source_id and source_id not in allowed_source_ids:
            out += 1
        elif cited_notebook and cited_notebook != notebook_id:
            out += 1
    return out


# --- 完整性虚报的**候选**信号(§7.1;裁决权在人工) --------------------------

#: 完整性断言的正则。**只产候选**:命中项 100% 进人工复核(§6 第二层),进
#: §9 硬判据 3 的是人工确认过的那一份。所以这里的取舍是「宁可多报、不可漏
#: 报」——精度由下一层的人兜,召回没人兜。
_COMPLETENESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(共|一共|总共|合计)\s*\d+\s*[篇个张条项份份]"),
    re.compile(r"全部\s*\d*\s*[篇个张条项份]"),
    re.compile(r"逐一列出|逐条列出|完整列出|全部列出|全部列举|无一遗漏|没有遗漏"),
    re.compile(r"所有(的)?(来源|文章|论文|文献|表格|公式|条目|文件|内容)"),
    re.compile(r"以上(就是)?(全部|所有)"),
    re.compile(
        r"\ball\s+(of\s+the\s+)?"
        r"(papers|sources|documents|tables|formulas|entries|files)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(complete list|in total|exhaustive)\b", re.IGNORECASE),
    re.compile(r"\bevery\s+(paper|source|document|table|formula)\b", re.IGNORECASE),
)


def completeness_claim_candidate(
    answer: str, *, coverage_complete: bool | None
) -> bool:
    """答案里出现完整性断言、**且**本 run 的枚举链终态不是 `complete`。

    `coverage_complete` 取 `AskResponse.result_coverage.complete`——集合级覆盖
    对象是 complete/partial 的权威(见 `AskResponse.result_sets` 的注释)。这次
    run 压根没走集合枚举时它是 `None`,而 `None` 在这里就是「没有任何东西背书
    这句完整性断言」,所以照样算候选。
    """
    if coverage_complete is True:
        return False
    text = answer or ""
    return any(pattern.search(text) for pattern in _COMPLETENESS_PATTERNS)


# --- 坏工具调用(§7.1,复用 T0 的 `skip_reasons`) ---------------------------

#: 「这次动作根本没打出去」的三类原因码(§7.1 点名的 invalid / unavailable /
#: duplicate)。按**子串**判,而不是抄一份会随服务层新增原因码而过期的枚举。
_INVALID_TOOL_CALL_MARKERS: tuple[str, ...] = ("invalid", "unavailable", "duplicate")

#: 唯一的例外。`kg_unavailable` 是「这个库里没有知识图谱」——一条语料事实,
#: 不是一次坏的工具调用;A 格(`A_nokg`)每个 run 都会有它,计进去会让这一列
#: 变成「这道题跑在哪个语料格」的复读。`kg_gap_unavailable` 不在例外里:那是
#: 一个动作通道的可用性(见 `reasoning_trace_stats.KG_UNAVAILABLE_REASONS`)。
_CORPUS_FACT_REASONS: frozenset[str] = frozenset({"kg_unavailable"})


def count_invalid_tool_calls(skip_reasons: object) -> int | None:
    """轨迹里 invalid / unavailable / duplicate 类观察的次数。

    直接吃 T0 投影已经数好的 `skip_reasons`(短码 → 次数),**不再数一遍轨迹**
    ——两处各数一遍必然分叉。`skip_reasons` 缺席(unknown)时返回 `None`。
    """
    if not isinstance(skip_reasons, Mapping):
        return None
    total = 0
    for reason, count in skip_reasons.items():
        code = str(reason)
        if code in _CORPUS_FACT_REASONS:
            continue
        if any(marker in code for marker in _INVALID_TOOL_CALL_MARKERS):
            total += int(count or 0)
    return total


# --- 成本三键:LLM 日志的时间窗切片(§7.2) ---------------------------------


@dataclass(frozen=True)
class AbUsage:
    """一个 run 窗口内的模型成本。全部字段都可能是 unknown。"""

    model_calls: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason_codes: Mapping[str, int] | None = None


#: 并发 > 1 时的成本读数。窗口重叠,切片会把别的 run 的 token 记到本 run 上;
#: §4.4 的原话是「宁可少一个成本表,不要一个对不上的」,所以整份 `AbUsage` 落
#: unknown,只留 rig 自己掐的 `latency_ms_total`。
UNKNOWN_USAGE = AbUsage()

#: `finish_reason` 缺席时的短码。空串过不了投影的短码校验,而「这次调用没有
#: 报 finish_reason」本身是要计数的一类(T0 的 20% 空正文靠它归因)。
FINISH_REASON_UNKNOWN = "unknown"


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def slice_llm_usage(
    records: Iterable[Mapping],
    *,
    start: datetime,
    end: datetime,
) -> AbUsage:
    """LLM 交互日志的时间窗切片 → 一个 run 的成本(§7.2)。

    日志记录里**没有** run 或 actor 标识(隔离是靠写进哪个文件),所以归因只能
    靠时间窗:rig 记下每个 run 的起止时刻,取该窗口内的记录求和。`app/core/llm.py`
    在**发起调用之前**就把 `ts` 写进 record,所以窗口按 `[start, end]` 闭区间取。

    只读数值字段:`usage.prompt_tokens` / `usage.completion_tokens` /
    `finish_reason`。日志正文(prompt / response 片段)一个字都不进数据集
    (§7.3)。

    `ts` 解不出来的记录**跳过**并不影响归因方向的正确性(它谁都不算),但会让
    这个 run 的成本偏低;实际日志里 `ts` 恒由 `datetime.now().isoformat()` 写
    入,解不出来只可能是日志被别的东西污染过。
    """
    calls = 0
    prompt = 0
    completion = 0
    reasons: dict[str, int] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        stamp = _parse_ts(record.get("ts"))
        if stamp is None or stamp < start or stamp > end:
            continue
        calls += 1
        usage = record.get("usage")
        if isinstance(usage, Mapping):
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
        raw_reason = record.get("finish_reason")
        reason = str(raw_reason).strip() if isinstance(raw_reason, str) else ""
        code = reason or FINISH_REASON_UNKNOWN
        reasons[code] = reasons.get(code, 0) + 1
    return AbUsage(
        model_calls=calls,
        prompt_tokens=prompt,
        completion_tokens=completion,
        finish_reason_codes=dict(sorted(reasons.items())),
    )


# --- 声明与证据的对号(§5.5-1) ----------------------------------------------


def assert_arm_matches_evidence(arm: str, policy_version: object) -> None:
    """rig 声明的臂 vs 轨迹反推出来的 `policy_version`。不符就当场停。

    v2 的判据是「这次 run 产出了 termination 事实」(T0 `project_search_run` 的
    口径)。整批跑完再发现 v2 那半其实跑的是 legacy,代价是几百次模型调用;
    这里的代价是一次比较。
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm!r}")
    if policy_version != arm:
        raise RuntimeError(
            f"声明 arm={arm!r},但这个 run 的证据是 policy_version="
            f"{policy_version!r}(v2 的判据是 run 产出了 termination 事实)。"
            "先修再跑整批"
        )


# --- 每 run 一行的闭集投影(§7.1) -------------------------------------------


def project_ab_run(
    steps: Sequence[object],
    *,
    arm: str,
    effort: str,
    repeat: int,
    question_key: str,
    corpus_cell: str,
    answer: str,
    citations: Sequence[object],
    anchors: Sequence[object],
    coverage_complete: bool | None,
    kg_in_scope: bool | None,
    sources_count: object = None,
    has_intent_contract: bool = False,
    notebook_id: str = "",
    gold: AbGold | None = None,
    element_sections: Mapping[str, str] | None = None,
    source_titles: Mapping[str, str] | None = None,
    allowed_source_ids: frozenset[str] | None = None,
    usage: AbUsage = UNKNOWN_USAGE,
    latency_ms_total: int | None = None,
    model_contract: str | None = None,
    corpus_signature: str | None = None,
    paired: bool | None = None,
    status: str = "done",
) -> dict:
    """一次**完整 Ask** run → 一行 A/B 闭集投影。

    T0 的那一半原封不动走 `project_run`(A/B 跑的就是同一条
    `ReasoningRetriever.run`,换一份投影只会让两边口径分叉),这里只在它上面加
    §7.1 那批答案级/成本级的键。`kg_in_scope` 与 `project_search_run` 同一条
    理由直接盖掉:调用方拿的是 `kg_in_scope_for` 的一次 EXISTS,比从轨迹形状
    反推更硬。

    `arm` 与投影出来的 `policy_version` **都**写进行里,冗余是刻意的(§7.1):
    一个是 rig 的声明,一个是从轨迹反推的证据,两者不符恰恰是最该被看见的那
    件事;对号由调用方在每条臂的第一个 run 之后做
    (`assert_arm_matches_evidence`),不在这里悄悄抹平。
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm!r}")
    row = project_run(
        {"mode": "reasoning", "status": status},
        list(steps),
        {
            "mode": "reasoning",
            "retrieval_effort": effort,
            # `project_run` 只读它的真假。契约内容是自由文本,这一层不碰。
            "intent": True if has_intent_contract else None,
        },
        sources_count=sources_count,
        rig_tags={
            "consumer": "ask_single",
            "trace_source": "in_process",
            "corpus_cell": corpus_cell,
            "question_key": question_key,
        },
    )
    row["kg_in_scope"] = kg_in_scope if isinstance(kg_in_scope, bool) else None
    text = answer or ""
    row.update({
        "arm": arm,
        "repeat": int(repeat),
        "paired": paired,
        "answer_chars": len(text),
        "answer_empty": not text,
        "citations": len(citations),
        "anchors_total": len(anchors),
        "anchors_on_gold": count_anchors_on_gold(
            anchors, gold,
            element_sections=element_sections, source_titles=source_titles,
        ),
        "anchors_unresolved": count_unresolved_anchors(text, anchors),
        "citations_out_of_scope": count_citations_out_of_scope(
            citations,
            allowed_source_ids=allowed_source_ids, notebook_id=notebook_id,
        ),
        "completeness_claim": completeness_claim_candidate(
            text, coverage_complete=coverage_complete
        ),
        "invalid_tool_calls": count_invalid_tool_calls(row.get("skip_reasons")),
        "model_calls": usage.model_calls,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "finish_reason_codes": (
            dict(usage.finish_reason_codes)
            if usage.finish_reason_codes is not None else None
        ),
        "latency_ms_total": latency_ms_total,
        "model_contract": model_contract,
        "corpus_signature": corpus_signature,
    })
    # 判分模型键与人工键在 T-AB2 里恒 unknown。写成 `None` 而不是留空,是为了让
    # 每一行的键集在两个任务之间保持逐字相同——`ab-judge` 之后只填值,不改形状。
    for key in DEFERRED_JUDGED_KEYS:
        row[key] = None
    assert_ab_closed(row)
    assert_projection_values(row)
    return row


# --- 配对(§7.1 `paired`;§8.1 差值表的准入) --------------------------------


def pair_id(row: Mapping) -> tuple[Any, ...]:
    """一行的配对身份:同题 + 同格 + 同档 + 同轮。`arm` 刻意不在里面。"""
    return (
        row.get("question_key"), row.get("corpus_cell"),
        row.get("effort"), row.get("repeat"),
    )


def mark_paired(rows: Sequence[dict]) -> None:
    """按 `pair_id` 就地回填 `paired`。两臂都在场才是 `True`。

    `--only-policy` 重跑出来的行只有一侧,`paired=False`,**不进配对差值表**,
    只进单臂基线表(§4.2)。`ab` 的跑批顺序把两臂放在最内层背靠背,所以正常
    情况下这个函数每次只看两行。
    """
    seen: dict[tuple[Any, ...], set[object]] = {}
    for row in rows:
        seen.setdefault(pair_id(row), set()).add(row.get("arm"))
    for row in rows:
        row["paired"] = len(seen.get(pair_id(row), set())) == len(ARMS)
